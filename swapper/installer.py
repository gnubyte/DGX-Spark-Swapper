"""Provisioning: detect what is already on the box and install what isn't.

Infrastructure actions depend on the platform:
  both          git, OpenClaw, the HTTPS front (nginx on :11443, checks the API key)
  DGX Spark     Docker with NVIDIA GPU passthrough (+ the two original recipe clones)
  Strix Halo    Vulkan drivers, llama.cpp (Unsloth's build, which has MTP drafts),
                the GPU memory limit (how much RAM the integrated GPU may map)

Each model in the catalog also has an install action, "model:<key>", driven by
the steps in its catalog entry (clone a recipe, run its prepare script, pull an
image, download GGUF files from Hugging Face …). Every action is a Job you can
kick off from the UI; it streams its own log like a model swap. `report()`
returns a live presence snapshot of the infrastructure actions.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import fetch
import hardware
from engine import DEMO, FRONT_CONF, FRONT_MARK, Job, SwapFailed, key_digest, meminfo, port_open, sh

LLAMACPP_REPO = "unslothai/llama.cpp"
_PROGRESS = re.compile(r"\d+(\.\d+)?%|\d+(\.\d+)?\s*[KMGT]i?B/s|\bETA\b", re.I)

INFRA = {
    "git": ("git", "Needed to fetch the model recipes and OpenClaw."),
    "docker": ("Docker + NVIDIA GPU passthrough", "Hosts the model containers."),
    "sglang_recipe": ("Qwen3.8 27B (SGLang) recipe", "MiaAI-Lab/Qwen3.8-27B-SGLang-DGX-Spark"),
    "flash_recipe": ("Qwen3.8 Flash Next (vLLM) recipe", "MiaAI-Lab/Qwen3.8-Flash-Next-Single-DGX-Spark"),
    "vulkan": ("AMD GPU drivers (Vulkan)", "Mesa's RADV driver, which llama.cpp uses to run models on the Radeon 8060S."),
    "llamacpp": ("llama.cpp (Vulkan, with MTP drafts)", "Unsloth's prebuilt llama.cpp — it has the MTP drafting the Qwen3.8 recipes need. Checksum-verified."),
    "gpumem": ("GPU memory limit", "Lets the GPU map up to ~108 GiB of the 128 GB, which Qwen3.8 Flash Next needs. Changes a boot setting; takes effect after a reboot."),
    "openclaw": ("OpenClaw", "Agent gateway; the Swapper repoints it at the active model."),
    "ssl": ("HTTPS front for the model API", "nginx on :11443 with this page's certificate. It requires the API key, so models without a key of their own aren't open to the network."),
}

ORDER = ["git", "docker", "vulkan", "llamacpp", "gpumem", "sglang_recipe", "flash_recipe", "openclaw", "ssl"]


class InstallFailed(Exception):
    pass


def _is_root() -> bool:
    return os.geteuid() == 0


def _host_apk() -> str:
    """Return the host package manager: 'apt' or 'apk' (best guess)."""
    if Path("/etc/debian_version").exists():
        return "apt"
    if Path("/etc/alpine-release").exists():
        return "apk"
    if shutil.which("apt-get"):
        return "apt"
    return "apk"


def nginx_front_conf(cfg: dict, cert: str, key_file: str) -> str:
    """The nginx config for the :11443 front. /v1/ goes to the model server
    (unbuffered, so replies stream) and requires the API key — as a Bearer
    token or an x-api-key header (Anthropic-style clients); everything else
    goes to the OpenClaw gateway, which has its own token."""
    port = int(cfg.get("nginx_port", 11443))
    api = int(cfg.get("api_port", 8888))
    gw = int((cfg.get("openclaw") or {}).get("gateway_port", 18789))
    api_key = str(cfg.get("api_key") or "")
    if api_key and not re.fullmatch(r"[A-Za-z0-9._\-]+", api_key):
        raise InstallFailed("The API key may only use letters, digits, '.', '_' and '-' for the HTTPS front.")
    esc = re.escape(api_key)
    lines = [
        f"{FRONT_MARK} — re-run Provisioning ▸ HTTPS front to regenerate; edits are overwritten.",
        "# HTTPS front for the model API (/v1/, API key required) and the OpenClaw gateway (/).",
        f"# api-key-sha256: {key_digest(api_key) if api_key else 'none'}",
        "map $http_upgrade $swapper_connection_upgrade { default upgrade; '' close; }",
    ]
    if api_key:
        # Regex keys: the token compares case-sensitively (only the word "Bearer"
        # is case-insensitive), and a key like "default" can't collide with
        # map's own keywords.
        lines += [
            f"map $http_authorization $swapper_bearer_ok {{ default 0; \"~^(?i:bearer)[ \\t]+{esc}$\" 1; }}",
            f"map $http_x_api_key $swapper_xkey_ok {{ default 0; \"~^{esc}$\" 1; }}",
        ]
    lines += [
        "server {",
        f"    listen {port} ssl;",
        "    server_name _;",
        f"    ssl_certificate     {cert};",
        f"    ssl_certificate_key {key_file};",
        "    ssl_protocols TLSv1.2 TLSv1.3;",
        "    client_max_body_size 100m;",
        "    location /v1/ {",
    ]
    if api_key:
        lines += [
            "        default_type application/json;",
            '        set $swapper_auth "$swapper_bearer_ok$swapper_xkey_ok";',
            '        if ($swapper_auth = "00") {',
            """            return 401 '{"error":{"message":"Missing or wrong API key (see the Connect tab of Spark Swapper)","type":"invalid_request_error","code":"invalid_api_key"}}';""",
            "        }",
        ]
    lines += [
        f"        proxy_pass http://127.0.0.1:{api};",
        "        proxy_http_version 1.1;",
        "        proxy_set_header Host $host;",
        '        proxy_set_header Connection "";',
        "        proxy_buffering off;",
        "        proxy_request_buffering off;",
        "        proxy_read_timeout 3600s;",
        "        proxy_send_timeout 3600s;",
        "    }",
        "    location / {",
        f"        proxy_pass http://127.0.0.1:{gw};",
        "        proxy_http_version 1.1;",
        "        proxy_set_header Host $host;",
        "        proxy_set_header Upgrade $http_upgrade;",
        "        proxy_set_header Connection $swapper_connection_upgrade;",
        "        proxy_read_timeout 3600s;",
        "    }",
        "}",
        "",
    ]
    return "\n".join(lines)


def grub_with_gpu_limit(text: str, pages: int) -> str:
    """/etc/default/grub with ttm.pages_limit / ttm.page_pool_size set in
    GRUB_CMDLINE_LINUX_DEFAULT (older amdgpu.gttsize values removed)."""
    def fix(m: re.Match) -> str:
        args = [a for a in m.group(2).split()
                if not a.startswith(("ttm.pages_limit=", "ttm.page_pool_size=", "amdgpu.gttsize="))]
        args += [f"ttm.pages_limit={pages}", f"ttm.page_pool_size={pages}"]
        return f'{m.group(1)}"{" ".join(args)}"'
    pattern = re.compile(r'^(GRUB_CMDLINE_LINUX_DEFAULT=)"([^"]*)"', re.M)
    if pattern.search(text):
        return pattern.sub(fix, text, count=1)
    return text.rstrip("\n") + f'\nGRUB_CMDLINE_LINUX_DEFAULT="ttm.pages_limit={pages} ttm.page_pool_size={pages}"\n'


def world_readable(root: Path):
    """Directories 0755 and files at least 0644 under `root`: the sandboxed
    llama-server (a throwaway user) has to read the weights and the build."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        os.chmod(dirpath, 0o755)
        for name in filenames:
            f = os.path.join(dirpath, name)
            if not os.path.islink(f):
                os.chmod(f, os.stat(f).st_mode | 0o444)


class Installer:
    def __init__(self, engine):
        self.engine = engine
        self.cfg = engine.cfg
        self.job: Job | None = None
        self._lock = threading.Lock()

    @property
    def platform(self) -> str:
        return self.engine.platform

    @property
    def infra_actions(self) -> list[str]:
        if self.platform == hardware.AMD_STRIX_HALO:
            keep = {"git", "vulkan", "llamacpp", "gpumem", "openclaw", "ssl"}
        else:
            keep = {"git", "docker", "sglang_recipe", "flash_recipe", "openclaw", "ssl"}
        return [a for a in ORDER if a in keep]

    @property
    def ACTIONS(self) -> set[str]:
        models = {f"model:{k}" for k, m in self.engine.models.items() if m.get("install")}
        return set(self.infra_actions) | models

    # ── config: where things live ──
    @property
    def recipes(self) -> dict:
        return self.cfg.get("recipes", {})

    def recipe_dir(self, which: str) -> Path | None:
        return Path(self.recipes.get(which, {}).get("dir", "")) if self.recipes.get(which) else None

    # ── presence detection ──
    def report(self) -> dict:
        if DEMO:
            demo = {
                "git": {"present": True, "detail": "git version 2.43.0"},
                "docker": {"present": True, "detail": "26.1.0"},
                "sglang_recipe": {"present": True, "dir": "/home/demo/Qwen3.8-27B-SGLang-DGX-Spark",
                                  "rev": "a1b2c3d", "git": True, "env": True,
                                  "detail": "cloned at /home/demo/Qwen3.8-27B-SGLang-DGX-Spark (rev a1b2c3d)"},
                "flash_recipe": {"present": False, "dir": "/home/demo/Qwen3.8-Flash-Next-Single-DGX-Spark",
                                 "detail": "directory missing"},
                "vulkan": {"present": True, "detail": "AMD Radeon 8060S Graphics (RADV GFX1151)"},
                "llamacpp": {"present": True, "detail": "b11541-mix-2500203 (Vulkan, MTP drafts)"},
                "gpumem": {"present": True, "detail": "The GPU may use 108 GiB (enough for every model here)."},
                "openclaw": {"present": True, "running": True, "detail": "openclaw 1.4.2"},
                "ssl": {"present": True, "swapper_cert": True, "nginx_up": True, "nginx_configured": True,
                        "ours": True, "port": 11443, "detail": "nginx on :11443, API key required"},
            }
            out = {a: demo[a] for a in self.infra_actions}
        else:
            out = {}
            for a in self.infra_actions:
                try:
                    out[a] = getattr(self, f"_state_{a}")()
                except Exception as e:  # noqa: BLE001 — one broken probe must not hide the rest
                    out[a] = {"present": False, "detail": f"couldn't check: {e}"}
        for i, a in enumerate(self.infra_actions):
            label, desc = INFRA[a]
            out[a].setdefault("label", label)
            out[a].setdefault("desc", desc)
            out[a]["order"] = i
        return out

    def check(self, action: str) -> dict:
        if DEMO:
            return {"present": True, "detail": "demo"}
        return getattr(self, f"_state_{action}")()

    def _state_git(self) -> dict:
        return self._have("git")

    @staticmethod
    def _have(bin_: str) -> dict:
        rc, out = sh([bin_, "--version"], timeout=10)
        present = rc == 0
        line = out.splitlines()[0] if out and rc == 0 else ""
        return {"present": present, "detail": line or ("found" if present else "not found")}

    def _state_docker(self) -> dict:
        rc, out = sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        if rc != 0:
            # docker binary there but daemon down / nvidia not configured?
            rc2, _ = sh(["which", "docker"], timeout=5)
            if rc2 == 0:
                return {"present": True, "detail": "installed but daemon not answering"}
            return {"present": False, "detail": "not installed"}
        return {"present": True, "detail": out}

    def _state_sglang_recipe(self) -> dict:
        return self._recipe_state("sglang_recipe")

    def _state_flash_recipe(self) -> dict:
        return self._recipe_state("flash_recipe")

    def _recipe_state(self, which: str) -> dict:
        d = self.recipe_dir(which)
        if not d or not str(d).strip():
            return {"present": False, "dir": None, "detail": "no path configured"}
        d = Path(d)
        if not d.is_dir() or not (d / ".git").is_dir():
            # maybe a plain checkout without git
            if not (d / "start.sh").is_file():
                return {"present": False, "dir": str(d), "detail": "directory missing"}
            return {"present": True, "dir": str(d), "detail": "present (no git)", "git": False}
        rc, out = sh(["git", "-C", str(d), "rev-parse", "--short", "HEAD"], timeout=10)
        rev = out if rc == 0 else "?"
        env = (d / ".env").is_file()
        download = (d / "download.sh").is_file()
        return {"present": True, "dir": str(d), "rev": rev, "git": True,
                "env": env, "download": download,
                "detail": f"cloned at {d} (rev {rev})" + ("" if env else " — .env missing")}

    def _state_openclaw(self) -> dict:
        oc = self.cfg.get("openclaw", {})
        binpath = oc.get("bin")
        present = False
        detail = "not installed"
        if binpath and Path(binpath).exists():
            present = True
            rc, out = sh([binpath, "--version"], timeout=15)
            detail = out.splitlines()[0] if out and rc == 0 else "present"
        else:
            rc, out = sh(["which", "openclaw"], timeout=8)
            if rc == 0:
                present = True
                detail = out
        running = port_open(oc.get("gateway_port", 18789))
        return {"present": present, "running": running, "detail": detail}

    def _state_ssl(self) -> dict:
        ng = self.cfg.get("nginx_port", 11443)
        swapper_cert = Path(os.environ.get("SWAPPER_CONF", "/etc/spark-swapper")) / "tls.crt"
        front = self.engine.front_state()
        nginx_up, ours, enforces = front["up"], front["ours"], front["enforces_key"]
        configured = ours or self._other_front(ng)
        if ours and nginx_up and enforces:
            detail = f"nginx on :{ng}, API key required"
        elif ours and nginx_up and not self.engine.key:
            detail = f"nginx on :{ng}; no API key is set in the config, so none is required"
        elif ours and nginx_up:
            detail = f"nginx on :{ng}, but set up for an older API key — run this again"
        elif nginx_up:
            detail = f"something already serves :{ng} (not set up by Spark Swapper — it may not check the API key)"
        elif configured:
            detail = f"configured, but nothing answers on :{ng}"
        else:
            detail = "not set up"
        return {"swapper_cert": swapper_cert.exists(), "nginx_up": nginx_up,
                "nginx_configured": configured, "ours": ours, "port": ng, "enforces_key": enforces,
                "present": bool(ours and nginx_up and (enforces or not self.engine.key)), "detail": detail}

    @staticmethod
    def _other_front(port: int) -> bool:
        for p in ("/etc/nginx/sites-enabled/", "/etc/nginx/conf.d/"):
            d = Path(p)
            if not d.is_dir():
                continue
            for f in d.iterdir():
                if f == FRONT_CONF:
                    continue
                try:
                    if re.search(rf"listen[^;]*\b{port}\b", f.read_text(errors="replace")):
                        return True
                except OSError:
                    continue
        return False

    def _state_vulkan(self) -> dict:
        rc, out = sh(["vulkaninfo", "--summary"], timeout=30)
        if rc == 0:
            names = [n.strip() for n in re.findall(r"deviceName\s*=\s*(.+)", out)]
            gpus = [n for n in names if "llvmpipe" not in n.lower()]
            if gpus:
                return {"present": True, "detail": ", ".join(dict.fromkeys(gpus))}
            return {"present": False, "detail": "Vulkan works but only the software renderer was found"}
        icd = any(Path(p).exists() for p in ("/usr/share/vulkan/icd.d/radeon_icd.x86_64.json",
                                             "/usr/share/vulkan/icd.d/radeon_icd.json"))
        loader = any(Path(p).exists() for p in ("/usr/lib/x86_64-linux-gnu/libvulkan.so.1",
                                                "/usr/lib/libvulkan.so.1", "/usr/lib64/libvulkan.so.1"))
        if icd and loader:
            return {"present": True, "detail": "RADV driver installed (vulkaninfo isn't, so the GPU wasn't listed)"}
        return {"present": False, "detail": "not installed"}

    def _state_llamacpp(self) -> dict:
        info = self.engine.load_state().get("llamacpp") or {}
        exe = self.engine.llama_server()
        if not exe:
            return {"present": False, "detail": "not installed"}
        mtp = self.engine.mtp_supported(exe)
        return {"present": True, "tag": info.get("tag"), "mtp": mtp,
                "detail": f"{info.get('tag', '?')} ({info.get('backend', 'vulkan').capitalize()}"
                          + (", MTP drafts)" if mtp else ", no MTP drafts — update it)")}

    def _biggest_gpu_need(self) -> int:
        return max([int(m.get("gpu_gib") or 0) for m in self.engine.models.values()
                    if m["kind"] == "llamacpp"] or [0])

    def _state_gpumem(self) -> dict:
        gpu = hardware.amd_gpu_memory()
        need = self._biggest_gpu_need()
        pending = False
        try:
            grub = Path("/etc/default/grub").read_text()
            want = re.search(r"ttm\.pages_limit=(\d+)", grub)
            pending = bool(want) and hardware.ttm_pages_limit() != int(want.group(1))
        except OSError:
            pass
        if not gpu:
            return {"present": False, "detail": "couldn't read the GPU's memory limits", "pending_reboot": pending}
        enough = gpu["total_gib"] >= need
        detail = f"The GPU may use {gpu['total_gib']:.0f} GiB"
        detail += " (enough for every model here)." if enough else f"; Qwen3.8 Flash Next needs about {need} GiB."
        if pending:
            detail += " A new limit is set — reboot to apply it."
        return {"present": enough, "detail": detail, "pending_reboot": pending, **gpu}

    # ── jobs ──
    def begin(self, action: str) -> Job:
        if action not in self.ACTIONS:
            raise ValueError("unknown action")
        if self.engine.updating():
            raise RuntimeError("Spark Swapper is updating itself — try again in a minute.")
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("An install is already in progress.")
        try:
            if action.startswith("model:"):
                key = action.split(":", 1)[1]
                job = Job(f"install:{action}", None, ["requires", "fetch", "check"])
            else:
                job = Job(f"install:{action}", None, ["run"])
                job.steps[0]["label"] = f"Installing {INFRA.get(action, (action,))[0]}"
            self.job = job
            threading.Thread(target=self._run, args=(job, action), daemon=True).start()
            return job
        except Exception:
            self._lock.release()
            raise

    def _run(self, job: Job, action: str):
        cur = "run"
        try:
            if action.startswith("model:"):
                self._do_model(job, action.split(":", 1)[1])
            else:
                job.step(cur, "running")
                getattr(self, f"_do_{action}")(job)
                job.step(cur, "done")
            job.state = "done"
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, (SwapFailed, InstallFailed, fetch.FetchError)) else f"{type(e).__name__}: {e}"
            job.say(f"✗ {msg}")
            job.error = msg
            job.state = "failed"
            running = next((s["key"] for s in job.steps if s["state"] == "running"), job.steps[-1]["key"])
            job.step(running, "failed", msg)
        finally:
            job.finished = time.time()
            self.engine.forget_install_cache()
            self._lock.release()

    # ── running things with a live log ──
    def _stream(self, job: Job, argv: list[str], cwd: str | None = None, env: dict | None = None,
                timeout: float = 3600) -> int:
        """Run argv, sending its output to the job log line by line (progress bars
        throttled to one line every 10 s). Kills the process group on timeout."""
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, text=True, errors="replace", bufsize=1,
                                    start_new_session=True)
        except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
            raise InstallFailed(f"couldn't run {argv[0]}: {e}") from e
        timed_out = threading.Event()

        def killer():
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out.set()
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(10)
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        threading.Thread(target=killer, daemon=True).start()
        last_progress = 0.0
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            if _PROGRESS.search(line):
                now = time.time()
                if now - last_progress < 10:
                    continue
                last_progress = now
            job.say(line[-2000:])
        rc = proc.wait()
        if timed_out.is_set():
            raise InstallFailed(f"{' '.join(argv[:3])} didn't finish within {int(timeout / 60)} minutes and was stopped.")
        return rc

    def _apt(self, job: Job, *packages: str):
        if not _is_root():
            raise InstallFailed("Installing packages needs root.")
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        job.say("apt-get update…")
        self._stream(job, ["apt-get", "-o", "DPkg::Lock::Timeout=300", "update", "-q"], env=env, timeout=900)
        job.say(f"apt-get install {' '.join(packages)}…")
        rc = self._stream(job, ["apt-get", "-o", "DPkg::Lock::Timeout=300", "install", "-y", "-q", *packages],
                          env=env, timeout=1800)
        if rc != 0:
            raise InstallFailed(f"apt-get install {' '.join(packages)} failed (exit {rc}).")

    # ── infrastructure ──
    def _do_git(self, job: Job):
        job.say("Checking for git…")
        rc, out = sh(["git", "--version"], timeout=10)
        if rc == 0:
            job.say(f"git already present: {out}")
            return
        pm = _host_apk()
        if not _is_root():
            raise InstallFailed("Installing git needs root. Run the host install script, or `sudo apt install git`.")
        if pm == "apt":
            self._apt(job, "git")
        else:
            rc, out = sh(["apk", "add", "git"], timeout=600)
            job.say(out[-800:])
            if rc != 0:
                raise InstallFailed(f"git install failed (exit {rc}).")
        rc, out = sh(["git", "--version"], timeout=10)
        if rc != 0:
            raise InstallFailed("git still missing after install.")
        job.say(f"Done: {out}")

    def _do_docker(self, job: Job):
        job.say("Checking for docker…")
        rc, _ = sh(["which", "docker"], timeout=5)
        if rc == 0:
            job.say("docker already installed.")
            job.say("Verifying daemon…")
            rc, out = sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
            if rc == 0:
                job.say(f"docker daemon is running (server {out}).")
                self.engine.ensure_docker_group(job)
                job.say("Verify GPU passthrough: `docker run --rm --gpus all nvidia/cuda nvidia-smi`")
                return
            job.say(f"daemon not answering ({out[-200:]}). Restarting…")
            rc, out = sh(["systemctl", "restart", "docker"], timeout=60)
            job.say(out[-200:])
            return
        if not _is_root():
            raise InstallFailed("Installing docker needs root. Use `sudo get.docker.com -o install.sh` (see README).")
        job.say("Downloading and running Docker's official install script (this takes a few minutes)…")
        rc = self._stream(job, ["bash", "-c", "curl -fsSL https://get.docker.com | sh"], timeout=1800)
        if rc != 0:
            raise InstallFailed(f"docker install failed (exit {rc}).")
        if self.platform == hardware.NVIDIA_GB10:
            job.say("Installing nvidia-container-toolkit (GPU passthrough)…")
            rc = self._stream(job, ["bash", "-c",
                              "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && "
                              "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list.template | "
                              "sed -e s#\\$\\(ARCH\\)#$(dpkg --print-architecture)#g -e s#\\$\\(DISTRO\\)#$(lsb_release -cs)#g > /etc/apt/sources.list.d/nvidia-container-toolkit.list && "
                              "apt-get update -y && apt-get install -y nvidia-container-toolkit"], timeout=1200)
            if rc != 0:
                job.say("nvidia-container-toolkit install failed (you can finish manually — see README).")
            sh(["bash", "-c", "nvidia-ctk runtime configure --runtime=docker && systemctl restart docker"], timeout=120)
        self.engine.ensure_docker_group(job)
        job.say("Done. Test: `docker run --rm --gpus all nvidia/cuda nvidia-smi`")

    def _do_sglang_recipe(self, job: Job):
        self._clone_recipe(job, "sglang_recipe", "Qwen3.8-27B-SGLang-DGX-Spark")

    def _do_flash_recipe(self, job: Job):
        self._clone_recipe(job, "flash_recipe", "Qwen3.8-Flash-Next-Single-DGX-Spark", download=True)

    def _clone_recipe(self, job: Job, which: str, repo_name: str, download: bool = False):
        rc, _ = sh(["git", "--version"], timeout=10)
        if rc != 0:
            raise InstallFailed("git is missing — install git first (there's a button for that).")
        rec = self.recipes.get(which, {})
        url = rec.get("url", f"https://github.com/MiaAI-Lab/{repo_name}.git")
        d = Path(rec.get("dir") or f"/opt/{repo_name}")
        job.say(f"Target directory: {d}")
        if d.is_dir() and (d / ".git").is_dir():
            job.say("Recipe already cloned — updating…")
            rc, out = sh(["git", "-C", str(d), "pull", "--ff-only"], timeout=120)
            job.say(out[-300:])
            if rc != 0:
                job.say(f"(pull failed, leaving as-is: {out[-200:]})")
        else:
            job.say(f"Cloning {url} …")
            d.parent.mkdir(parents=True, exist_ok=True)
            rc, out = sh(["git", "clone", url, str(d)], timeout=600)
            job.say(out[-300:])
            if rc != 0:
                raise InstallFailed(f"clone failed: {out[-300:]}")
        # ensure .env
        if (d / ".env.sample").exists() and not (d / ".env").exists():
            job.say("Creating .env from .env.sample…")
            shutil.copyfile(d / ".env.sample", d / ".env")
        if download and (d / "download.sh").is_file():
            job.say("Checkpoint present? Checking…")
            if not self._checkpoint_present(d, which):
                job.say("Checkpoint not found — install the model from the Models tab to download it "
                        "(~99 GiB), or run the recipe's download.sh yourself.")
        else:
            job.say("Recipe is ready. Use the model card's 'Switch to this' to start it.")

    def _checkpoint_present(self, d: Path, which: str) -> bool:
        import pwd
        homes = ["/root", self.cfg.get("openclaw", {}).get("home", ""),
                 pwd.getpwuid(os.getuid()).pw_dir, str(Path.home())]
        if which == "flash_recipe":
            for home in homes:
                if home and (Path(home) / ".cache/huggingface/hub" / "models--Mia-AiLab--Qwen3.8-Flash-Next-NVFP4").is_dir():
                    return True
            # or a local file the download.sh would have created
            if (d / "Qwen3.8-Flash-Next-NVFP4").is_dir():
                return True
        else:
            for home in homes:
                if home and (Path(home) / ".cache/huggingface/hub").is_dir():
                    return True
        return False

    def _do_openclaw(self, job: Job):
        oc = self.cfg.get("openclaw", {})
        binpath = oc.get("bin")
        if binpath and Path(binpath).exists():
            job.say(f"OpenClaw already present at {binpath}.")
            return
        job.say("OpenClaw installs as an npm package. Checking for node/npm…")
        rc, out = sh(["node", "--version"], timeout=10)
        if rc != 0:
            job.say("node not found. Installing Node.js 22 LTS via NodeSource…")
            rc = self._stream(job, ["bash", "-c", "curl -fsSL https://deb.nodesource.com/setup_22.x | bash -"], timeout=600)
            self._apt(job, "nodejs")
        job.say("Installing openclaw globally (npm install -g openclaw)…")
        rc = self._stream(job, ["bash", "-c", "npm install -g openclaw"], timeout=1800)
        if rc != 0:
            raise InstallFailed(f"openclaw install failed (exit {rc}).")
        job.say("Done. Run `openclaw onboard` as your user to configure it; the Swapper detects it on the next check "
                "and points it at whichever model is serving.")

    def _do_ssl(self, job: Job):
        import app as _app
        conf_dir = Path(os.environ.get("SWAPPER_CONF", "/etc/spark-swapper"))
        cert, key = conf_dir / "tls.crt", conf_dir / "tls.key"
        if cert.exists():
            job.say(f"This page's certificate is at {cert} (download it from the Connect tab).")
        else:
            job.say("Generating a fresh self-signed certificate (10 years)…")
            _app._gen_cert()
            job.say(f"Wrote {cert}.")
        ng = int(self.cfg.get("nginx_port", 11443))
        if hardware.in_container():
            job.say(f"Spark Swapper runs in a container, so it can't set up nginx on the host. On the host, "
                    f"serve :{ng} with nginx using {cert} — see the README.")
            return
        if not _is_root():
            raise InstallFailed("Setting up nginx needs root.")
        if not FRONT_CONF.exists() and self._other_front(ng):
            job.say(f"Another nginx site already listens on :{ng}; leaving it alone. Remove it and run this "
                    "again if you want Spark Swapper's front (it requires the API key).")
            return
        fresh = False
        if not shutil.which("nginx"):
            job.say("Installing nginx (a small web server)…")
            self._apt(job, "nginx")
            fresh = True
        FRONT_CONF.parent.mkdir(parents=True, exist_ok=True)
        job.say(f"Writing {FRONT_CONF} (requires the API key on /v1/)…")
        previous = FRONT_CONF.read_bytes() if FRONT_CONF.exists() else None
        text = nginx_front_conf(self.cfg, str(cert), str(key))   # validates the key first
        tmp = FRONT_CONF.with_name(FRONT_CONF.name + ".new")      # not *.conf: nginx ignores it
        tmp.write_text(text)
        tmp.chmod(0o600)
        tmp.replace(FRONT_CONF)
        default_site = Path("/etc/nginx/sites-enabled/default")
        if fresh and default_site.is_symlink():
            default_site.unlink()
        rc, out = sh(["nginx", "-t"], timeout=30)
        job.say(out)
        if rc != 0:
            # Never leave a config that stops nginx from (re)starting.
            if previous is None:
                FRONT_CONF.unlink(missing_ok=True)
            else:
                FRONT_CONF.write_bytes(previous)
            raise InstallFailed("nginx rejected the new configuration (see above); the previous one was put back.")
        sh(["systemctl", "enable", "nginx"], timeout=30)
        rc, out = sh(["systemctl", "restart", "nginx"], timeout=60)
        if rc != 0:
            raise InstallFailed(f"nginx didn't restart: {out[-300:]}")
        rc, out = sh(["bash", "-c", "command -v ufw >/dev/null && ufw status | grep -q 'Status: active' "
                                    f"&& ufw allow {ng}/tcp"], timeout=30)
        if rc == 0:
            job.say(f"Firewall: allowed {ng}/tcp.")
        time.sleep(1)
        anon = self.engine_http_status(f"https://127.0.0.1:{ng}/v1/models", None)
        keyed = self.engine_http_status(f"https://127.0.0.1:{ng}/v1/models", self.engine.key)
        job.say(f"Check: without the key → {anon}; with the key → {keyed}"
                + (" (no model is serving right now)" if keyed in (502, 503, 504) else ""))
        if self.engine.key and anon != 401:
            raise InstallFailed(f"The front answered {anon} without the API key; expected 401.")
        job.say(f"Done: https://<this box>:{ng}/v1 serves the model API and requires the API key.")

    @staticmethod
    def engine_http_status(url: str, key: str | None):
        from engine import http_status
        return http_status(url, key)

    def _do_vulkan(self, job: Job):
        job.say("Installing Mesa's Vulkan driver (RADV), the Vulkan loader and vulkaninfo…")
        self._apt(job, "mesa-vulkan-drivers", "libvulkan1", "vulkan-tools")
        st = self._state_vulkan()
        job.say(f"Vulkan: {st['detail']}")
        if not st["present"]:
            raise InstallFailed("The drivers installed, but Vulkan doesn't list the Radeon GPU. A reboot "
                                "(or a newer kernel/Mesa) may be needed.")

    def _do_llamacpp(self, job: Job):
        backend = self.cfg.get("llamacpp_backend", "vulkan")
        repo = self.cfg.get("llamacpp_repo", LLAMACPP_REPO)
        arch = fetch.machine()
        job.say(f"Looking up the latest llama.cpp build from github.com/{repo}…")
        rel = fetch.gh_latest_release(repo)
        tag = rel.get("tag_name") or "?"
        assets = {a["name"]: a for a in rel.get("assets") or []}
        suffix = f"-linux-{arch}-{backend}.tar.gz"
        name = next((n for n in assets if n.startswith("app-") and n.endswith(suffix)), None)
        if not name:
            raise InstallFailed(f"Release {tag} has no {suffix} build.")
        sums_asset = assets.get("llama-prebuilt-sha256.json")
        if not sums_asset:
            raise InstallFailed(f"Release {tag} publishes no checksums, so it won't be installed.")
        sums = fetch.get_json(sums_asset["browser_download_url"])
        sha = (((sums or {}).get("artifacts") or {}).get(name) or {}).get("sha256")
        if not sha:
            raise InstallFailed(f"No checksum for {name} in {tag}'s checksum list.")
        current = self.engine.load_state().get("llamacpp") or {}
        if current.get("tag") == tag and self.engine.llama_server():
            job.say(f"llama.cpp {tag} is already installed — that's the latest.")
            return
        downloads = self.engine.state_dir / "downloads"
        archive = downloads / name
        fetch.download(assets[name]["browser_download_url"], archive, size=assets[name].get("size"),
                       sha256=sha, say=job.say, label=name)
        dest = self.engine.runtime_dir / "llama.cpp" / tag
        if dest.exists():
            shutil.rmtree(dest)
        job.say(f"Unpacking into {dest}…")
        fetch.safe_extract(archive, dest)
        world_readable(self.engine.runtime_dir)
        exe = next((p for p in dest.rglob("llama-server") if p.is_file()), None)
        if not exe:
            raise InstallFailed("The archive has no llama-server.")
        for p in exe.parent.iterdir():
            if p.is_file() and (p.name.startswith("llama-") or p.suffix == ""):
                p.chmod(0o755)
        rc, out = sh([str(exe), "--version"], timeout=60, env={**os.environ, "LD_LIBRARY_PATH": str(exe.parent)})
        job.say(out[-600:])
        if rc != 0:
            raise InstallFailed("llama-server won't start" + (" — install the AMD GPU drivers (Vulkan) first."
                                                              if "vulkan" in out.lower() else "."))
        self.engine._mtp_cache.pop(str(exe), None)
        mtp = self.engine.mtp_supported(exe)
        job.say("MTP drafts: supported." if mtp else
                "Warning: this build has no MTP drafts (--spec-type draft-mtp); models will run at about half speed.")
        self.engine.save_state(llamacpp={"tag": tag, "bin": str(exe), "backend": backend, "mtp": mtp,
                                         "installed_at": time.time()})
        archive.unlink(missing_ok=True)
        # Keep this build and the one before it.
        builds = sorted((p for p in dest.parent.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)
        for old in builds[:-2]:
            shutil.rmtree(old, ignore_errors=True)
        job.say(f"Done: llama.cpp {tag}. Running models pick it up the next time they start.")

    def _do_gpumem(self, job: Job):
        if not _is_root():
            raise InstallFailed("Changing the boot settings needs root.")
        total = meminfo()["total"]
        target = float(self.cfg.get("gpu_memory_gib") or 108)
        target = max(16.0, min(target, total - 12))
        pages = int(target * 262144)  # 4 KiB pages
        grub = Path("/etc/default/grub")
        if not grub.exists():
            raise InstallFailed(
                "This system doesn't boot with GRUB, so the setting can't be added automatically. Add "
                f"`ttm.pages_limit={pages} ttm.page_pool_size={pages}` to the kernel command line, then reboot.")
        before = grub.read_text()
        after = grub_with_gpu_limit(before, pages)
        backup = grub.with_name("grub.spark-swapper.bak")
        if not backup.exists():
            backup.write_text(before)
            job.say(f"Saved the original as {backup}.")
        grub.write_text(after)
        job.say(f"Set ttm.pages_limit={pages} (≈{target:.0f} GiB the GPU may use) in {grub}.")
        cmd = ["update-grub"] if shutil.which("update-grub") else ["grub-mkconfig", "-o", "/boot/grub/grub.cfg"]
        rc = self._stream(job, cmd, timeout=300)
        if rc != 0:
            grub.write_text(before)
            raise InstallFailed(f"{cmd[0]} failed (exit {rc}); the original settings were put back.")
        job.note = "Reboot the box to apply the new GPU memory limit. Models resume on their own after the reboot."
        job.say(job.note)

    # ── models ──
    def _do_model(self, job: Job, key: str):
        m = self.engine.models.get(key)
        if not m or not m.get("install"):
            raise InstallFailed("That model has no install steps.")
        inst = m["install"]
        job.say(f"Installing {m['title']} — {inst.get('time', '')}".rstrip(" —"))
        job.step("requires", "running")
        for req in inst.get("requires", []):
            if req not in INFRA:
                continue
            state = self.check(req)
            label = INFRA[req][0]
            if state.get("present"):
                job.say(f"✓ {label}: {state.get('detail', 'present')}")
                continue
            if req == "gpumem":
                job.say(f"⚠ {label}: {state.get('detail')} The download goes ahead; the model won't start "
                        "until the limit is raised (Provisioning tab) and the box rebooted.")
                continue
            job.say(f"{label} is missing — installing it first.")
            getattr(self, f"_do_{req}")(job)
        job.step("requires", "done")

        job.step("fetch", "running")
        if m.get("run_as") == "user" or any(s.get("as") == "user" for s in inst.get("steps", [])):
            if "docker" in inst.get("requires", []):
                self.engine.ensure_docker_group(job)
        for step in inst.get("steps", []):
            getattr(self, f"_step_{step['do']}")(job, m, step)
        job.step("fetch", "done")

        job.step("check", "running")
        self.engine.forget_install_cache()
        st = self.engine.install_state(key, fresh=True)
        if not st["installed"]:
            raise InstallFailed(f"{m['title']} still isn't ready: {st['detail']}.")
        job.step("check", "done")
        job.say(f"✓ {m['title']} is installed. Press “Switch to this” on its card to start it.")

    def _who(self, m: dict, step: dict) -> str:
        return step.get("as") or m.get("run_as") or "root"

    def _step_git(self, job: Job, m: dict, step: dict):
        d = Path(step["dir"])
        who = self._who(m, step)
        if (d / ".git").is_dir():
            job.say(f"Updating {d}…")
            argv, env = self.engine.run_cmd({"as": who}, ["git", "-C", str(d), "pull", "--ff-only"])
            rc = self._stream(job, argv, env=env, timeout=600)
            if rc != 0:
                job.say("(couldn't fast-forward; keeping the checkout as it is)")
            return
        if d.exists() and any(d.iterdir()):
            raise InstallFailed(f"{d} exists and isn't a git checkout; move it aside and try again.")
        job.say(f"Cloning {step['url']} → {d}")
        argv, env = self.engine.run_cmd({"as": who}, ["git", "clone", "--depth", "1", step["url"], str(d)])
        rc = self._stream(job, argv, env=env, timeout=900)
        if rc != 0:
            raise InstallFailed(f"git clone failed (exit {rc}).")

    def _step_env_file(self, job: Job, m: dict, step: dict):
        d = Path(step["dir"])
        if (d / ".env.sample").exists() and not (d / ".env").exists():
            shutil.copyfile(d / ".env.sample", d / ".env")
            job.say("Created .env from .env.sample.")

    def _step_run(self, job: Job, m: dict, step: dict):
        who = self._who(m, step)
        argv, env = self.engine.run_cmd({"as": who}, step["cmd"], step.get("env"))
        job.say(f"$ {' '.join(step['cmd'])}   (in {step.get('cwd', '.')}, as {self.engine.user if who == 'user' else 'root'})")
        rc = self._stream(job, argv, cwd=step.get("cwd"), env=env, timeout=step.get("timeout", 7200))
        if rc != 0:
            raise InstallFailed(f"{' '.join(step['cmd'])} failed (exit {rc}). The log above says why.")

    def _step_docker_pull(self, job: Job, m: dict, step: dict):
        who = self._who(m, step) if step.get("as") else "root"
        argv, env = self.engine.run_cmd({"as": who}, ["docker", "pull", step["image"]])
        job.say(f"$ docker pull {step['image']}")
        rc = self._stream(job, argv, env=env, timeout=step.get("timeout", 7200))
        if rc != 0:
            raise InstallFailed(f"docker pull {step['image']} failed (exit {rc}).")

    def _step_hf_docker(self, job: Job, m: dict, step: dict):
        cache = Path(step["cache"])
        cache.mkdir(parents=True, exist_ok=True)
        code = ("from huggingface_hub import snapshot_download; "
                f"print(snapshot_download({step['repo']!r}))")
        job.say(f"Downloading {step['repo']} into {cache} (inside {step['image']})…")
        rc = self._stream(job, ["docker", "run", "--rm", "-v", f"{cache}:/root/.cache/huggingface",
                                "--entrypoint", "python3", step["image"], "-c", code],
                          timeout=step.get("timeout", 10800))
        if rc != 0:
            raise InstallFailed(f"Downloading {step['repo']} failed (exit {rc}).")

    def _step_hf_files(self, job: Job, m: dict, step: dict):
        files = m.get("files") or []
        todo = [f for f in files if not self.engine.file_verified(f["repo"], f["path"])]
        if not todo:
            job.say("All files are already downloaded and verified.")
            return
        listings: dict[str, dict] = {}
        for f in todo:
            if f["repo"] not in listings:
                job.say(f"Asking Hugging Face about {f['repo']}…")
                listings[f["repo"]] = fetch.hf_listing(f["repo"])
        need = 0
        for f in todo:
            meta = listings[f["repo"]].get(f["path"])
            if not meta:
                raise InstallFailed(f"{f['repo']} has no file {f['path']} any more.")
            part = self.engine.local_file(f["repo"], f["path"]).with_name(Path(f["path"]).name + ".part")
            need += (meta.get("size") or 0) - (part.stat().st_size if part.exists() else 0)
        self.engine.models_dir.mkdir(parents=True, exist_ok=True)
        world_readable(self.engine.runtime_dir)
        free = shutil.disk_usage(self.engine.models_dir).free
        job.say(f"To download: {fetch.human(need)}; free on {self.engine.models_dir}: {fetch.human(free)}.")
        if need > free - 5 * 1024**3:
            raise InstallFailed(f"Not enough disk space: {fetch.human(need)} needed, {fetch.human(free)} free.")
        for f in todo:
            meta = listings[f["repo"]][f["path"]]
            dest = self.engine.local_file(f["repo"], f["path"])
            fetch.download(fetch.hf_url(f["repo"], f["path"]), dest, size=meta.get("size"),
                           sha256=meta.get("sha256"), say=job.say, label=Path(f["path"]).name)
            marker = dest.with_name(dest.name + ".ok")
            marker.write_text(json.dumps({"size": dest.stat().st_size, "sha256": meta.get("sha256"),
                                          "repo": f["repo"], "path": f["path"], "at": time.time()}))
            for p in (dest, marker):
                p.chmod(0o644)
            world_readable(self.engine.runtime_dir)
