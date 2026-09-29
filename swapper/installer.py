"""Provisioning: detect what is already on the Spark and install what isn't.

Each "action" (install git, install docker, install the SGLang recipe, install
the Flash recipe, install OpenClaw, set up self-signed SSL) is a Job you can
kick off from the UI; it streams its own log like a model swap. `report()`
returns a live presence snapshot the UI uses to show what's already present
versus what still needs installing.

Everything runs as root (docker, nvidia, apt need it). On a plain host the
installer shells out to the system package manager; inside the Docker
container most of it is a no-op / reports "present" and the host-side install
script does the real work.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from engine import Job, SwapFailed, sh, port_open


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


class Installer:
    ACTIONS = {
        "git", "docker", "sglang_recipe", "flash_recipe",
        "openclaw", "ssl",
    }

    def __init__(self, engine):
        self.engine = engine
        self.cfg = engine.cfg
        self.job: Job | None = None
        self._lock = threading.Lock()

    # ── config: where things live ──
    @property
    def recipes(self) -> dict:
        return self.cfg.get("recipes", {})

    def recipe_dir(self, which: str) -> Path | None:
        return Path(self.recipes.get(which, {}).get("dir", "")) if self.recipes.get(which) else None

    # ── presence detection ──
    def report(self) -> dict:
        if os.environ.get("SWAPPER_DEMO", "") == "1":
            return {
                "git": {"present": True, "detail": "git version 2.43.0"},
                "docker": {"present": True, "detail": "26.1.0"},
                "sglang_recipe": {"present": True, "dir": "/home/demo/Qwen3.8-27B-SGLang-DGX-Spark",
                                  "rev": "a1b2c3d", "git": True, "env": True,
                                  "detail": "cloned at /home/demo/Qwen3.8-27B-SGLang-DGX-Spark (rev a1b2c3d)"},
                "flash_recipe": {"present": True, "dir": "/home/demo/Qwen3.8-Flash-Next-Single-DGX-Spark",
                                  "rev": "e4f5g6h", "git": True, "env": True, "download": True,
                                  "detail": "cloned at /home/demo/Qwen3.8-Flash-Next-Single-DGX-Spark (rev e4f5g6h)"},
                "openclaw": {"present": True, "running": True, "detail": "openclaw 1.4.2"},
                "ssl": {"swapper_cert": True, "nginx_up": True, "nginx_configured": True,
                         "port": 11443, "present": True},
            }
        return {
            "git": self._have("git"),
            "docker": self._have_docker(),
            "sglang_recipe": self._recipe_state("sglang_recipe"),
            "flash_recipe": self._recipe_state("flash_recipe"),
            "openclaw": self._openclaw_state(),
            "ssl": self._ssl_state(),
        }

    @staticmethod
    def _have(bin_: str) -> dict:
        rc, out = sh([bin_, "--version"], timeout=10)
        present = rc == 0
        line = out.splitlines()[0] if out and rc == 0 else ""
        return {"present": present, "detail": line or ("found" if present else "not found")}

    def _have_docker(self) -> dict:
        rc, out = sh(["docker", "version", "--format", "{{.Server.Version}}"], timeout=15)
        if rc != 0:
            # docker binary there but daemon down / nvidia not configured?
            rc2, _ = sh(["which", "docker"], timeout=5)
            if rc2 == 0:
                return {"present": True, "detail": "installed but daemon not answering"}
            return {"present": False, "detail": "not installed"}
        return {"present": True, "detail": out}

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

    def _openclaw_state(self) -> dict:
        oc = self.cfg.get("openclaw", {})
        binpath = oc.get("bin")
        present = False
        detail = "not installed"
        if binpath and Path(binpath).exists():
            present = True
            rc, out = sh([binpath, "--version"], timeout=15)
            detail = (out.splitlines()[0] if out and rc == 0 else "present") if present else "present"
        else:
            rc, out = sh(["which", "openclaw"], timeout=8)
            if rc == 0:
                present = True
                detail = out
        running = port_open(oc.get("gateway_port", 18789))
        return {"present": present, "running": running, "detail": detail}

    def _ssl_state(self) -> dict:
        # Swapper's own cert is (re)generated at boot, so it's "present" once
        # the app is up. The interesting bit is the nginx TLS front on :11443.
        ng = self.cfg.get("nginx_port", 11443)
        swapper_cert = Path(os.environ.get("SWAPPER_CONF", "/etc/spark-swapper")) / "tls.crt"
        nginx_up = port_open(ng)
        nginx_cert = False
        for p in ("/etc/nginx/sites-enabled/", "/etc/nginx/conf.d/"):
            if Path(p).is_dir():
                for f in Path(p).iterdir():
                    try:
                        if "11443" in f.read_text(errors="replace"):
                            nginx_cert = True
                            break
                    except OSError:
                        continue
        return {"swapper_cert": swapper_cert.exists(), "nginx_up": nginx_up,
                "nginx_configured": nginx_cert, "port": ng,
                "present": nginx_up or nginx_cert}

    # ── jobs ──
    def begin(self, action: str) -> Job:
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("An install is already in progress.")
        steps = ["running"]
        job = Job(f"install:{action}", None, steps)
        self.job = job
        self._steps = ["run"]
        job.steps = [{"key": "run", "label": "Installing", "state": "pending",
                      "started": None, "finished": None, "detail": ""}]
        threading.Thread(target=self._run, args=(job, action), daemon=True).start()
        return job

    def _run(self, job: Job, action: str):
        cur = "run"
        job.step(cur, "running")
        try:
            getattr(self, f"_do_{action}")(job)
            job.state = "done"
            job.step(cur, "done")
        except Exception as e:  # noqa: BLE001
            msg = str(e) if isinstance(e, (SwapFailed, InstallFailed)) else f"{type(e).__name__}: {e}"
            job.say(f"✗ {msg}")
            job.error = msg
            job.state = "failed"
            job.step(cur, "failed", msg)
        finally:
            job.finished = time.time()
            self.engine._status_cache = None
            self._lock.release()

    # ── the actual work ──
    def _do_git(self, job: Job):
        job.say("Checking for git…")
        rc, out = sh(["git", "--version"], timeout=10)
        if rc == 0:
            job.say(f"git already present: {out}")
            return
        pm = _host_apk()
        if not _is_root():
            raise InstallFailed("Installing git needs root. Run the host install script, or `sudo apt install git`.")
        job.say(f"Installing git via {pm} (this can take a minute)…")
        rc, out = sh(["apt-get", "update", "-y"], timeout=300) if pm == "apt" else sh(["apk", "update"], timeout=300)
        job.say(out[-400:])
        cmd = ["apt-get", "install", "-y", "git"] if pm == "apt" else ["apk", "add", "git"]
        rc, out = sh(cmd, timeout=600)
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
                job.say("Verify GPU passthrough: `docker run --rm --gpus all nvidia/cuda nvidia-smi`")
                return
            job.say(f"daemon not answering ({out[-200:]}). Restarting…")
            rc, out = sh(["systemctl", "restart", "docker"], timeout=60)
            job.say(out[-200:])
            return
        if not _is_root():
            raise InstallFailed("Installing docker needs root. Use `sudo get.docker.com -o install.sh` (see README).")
        job.say("Downloading and running Docker's official install script (this takes a few minutes)…")
        rc, out = sh(["bash", "-c", "curl -fsSL https://get.docker.com | sh"], timeout=1800)
        job.say(out[-800:])
        if rc != 0:
            raise InstallFailed(f"docker install failed (exit {rc}).")
        job.say("Installing nvidia-container-toolkit (GPU passthrough)…")
        rc, out = sh(["bash", "-c",
                      "curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg && "
                      "curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list.template | "
                      "sed -e s#\\$\\(ARCH\\)#$(dpkg --print-architecture)#g -e s#\\$\\(DISTRO\\)#$(lsb_release -cs)#g > /etc/apt/sources.list.d/nvidia-container-toolkit.list && "
                      "apt-get update -y && apt-get install -y nvidia-container-toolkit"], timeout=1200)
        job.say(out[-600:])
        if rc != 0:
            job.say("nvidia-container-toolkit install failed (you can finish manually — see README).")
        sh(["bash", "-c", "nvidia-ctk runtime configure --runtime=docker && systemctl restart docker"], timeout=120)
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
            rc, out = sh(["bash", "-c", f"cp {d}/.env.sample {d}/.env"], timeout=20)
            job.say(out[-200:])
        if download and (d / "download.sh").is_file():
            job.say("Checkpoint present? Checking…")
            if not self._checkpoint_present(d, which):
                job.say("Checkpoint not found — you can run the recipe's download.sh from its directory, "
                        "or leave it; start.sh will tell you. (This button does not fetch the ~99 GiB weights by default.)")
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
            job.say("node not found. Installing Node.js 20 LTS via NodeSource…")
            rc, out = sh(["bash", "-c", "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -"], timeout=300)
            job.say(out[-300:])
            rc, out = sh(["apt-get", "install", "-y", "nodejs"], timeout=600)
            job.say(out[-300:])
        job.say("Installing openclaw globally (npm install -g openclaw)…")
        rc, out = sh(["bash", "-c", "npm install -g openclaw"], timeout=1800)
        job.say(out[-800:])
        if rc != 0:
            raise InstallFailed(f"openclaw install failed (exit {rc}).")
        job.say("Done. Run `openclaw init` / configure it, and the swapper will detect it on the next report.")

    def _do_ssl(self, job: Job):
        job.say("Setting up self-signed SSL…")
        import app as _app
        cert = Path(os.environ.get("SWAPPER_CONF", "/etc/spark-swapper")) / "tls.crt"
        if cert.exists():
            job.say(f"Swapper's own self-signed cert already exists at {cert}.")
            job.say("It is available for download at /cert.crt (see Keys & connection → Trust this certificate).")
        else:
            job.say("Generating a fresh self-signed cert (10 years)…")
            _app._gen_cert()
            job.say(f"Wrote cert to {cert}.")
        # nginx front
        ng = self.cfg.get("nginx_port", 11443)
        if port_open(ng):
            job.say(f"nginx TLS front on :{ng} is already up.")
        else:
            job.say(f"nginx TLS front on :{ng} is not running. To serve the model API over trusted TLS:")
            job.say("  1. install nginx:  sudo apt install nginx")
            job.say(f"  2. drop a server block that listens on {ng} with ssl, proxying to 127.0.0.1:{self.cfg['api_port']}")
            job.say("  3. use the same self-signed cert/key so clients trust one identity")
            job.say("     (sudo systemctl enable --now nginx)")
            job.say("     See README → 'Trusting the self-signed SSL (AWS / EC2)' for the full recipe.")
