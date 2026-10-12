"""Swap engine: knows how to stop, start and verify each model on the box,
and how to repoint OpenClaw at whichever one is live.

Model kinds:
  sglang-docker   one docker container kept between runs (docker start/stop)
  flash-vllm      the original Flash Next recipe's start.sh / stop.sh, run as root
  recipe-script   a recipe whose start.sh / stop.sh run one docker container, run
                  as the box's user (TensorFold 27B and Flash Next, Nemotron)
  llamacpp        llama.cpp's llama-server as a transient systemd unit (Strix Halo)

Everything here runs as root (docker, systemd and the root-owned Flash recipe
need it). One swap at a time; every step is logged into the Job the web UI polls.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import socket
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

import config as config_mod
import hardware

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
DEMO = os.environ.get("SWAPPER_DEMO", "") == "1"
ROOT = Path(__file__).resolve().parent


def app_version() -> str:
    try:
        return (ROOT / "VERSION").read_text().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


# ─────────────────────────────── helpers ────────────────────────────────

def sh(cmd: list[str], timeout: float = 60, env: dict | None = None,
       input_text: str | None = None, cwd: str | None = None) -> tuple[int, str]:
    """Run a command, return (rc, combined output). Never raises on failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, input=input_text, cwd=cwd)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s: {' '.join(cmd[:3])}"
    except (FileNotFoundError, NotADirectoryError, PermissionError) as e:
        return 127, str(e)


def meminfo() -> dict:
    out = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            out[k] = int(v.split()[0]) / 1048576  # kB -> GiB
    except OSError:
        pass
    return {"total": round(out.get("MemTotal", 0), 1),
            "available": round(out.get("MemAvailable", 0), 1),
            "free": round(out.get("MemFree", 0), 1)}


def _tls_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # our own self-signed nginx front
    return ctx


def http_json(url: str, key: str | None = None, timeout: float = 4,
              body: dict | None = None) -> dict | None:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_tls_context()) as r:
            return json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def http_status(url: str, key: str | None = None, timeout: float = 5) -> int | None:
    """Just the HTTP status of a GET (None when nothing answered)."""
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout,
                                    context=_tls_context()) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, TimeoutError):
        return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def cert_fingerprint(cert_path: Path) -> str | None:
    """SHA-256 of the certificate's DER, as AA:BB:… (what DSH pins)."""
    try:
        pem = Path(cert_path).read_text()
    except OSError:
        return None
    m = re.search(r"-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----", pem, re.S)
    if not m:
        return None
    try:
        der = base64.b64decode("".join(m.group(1).split()))
    except ValueError:
        return None
    return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())


# ─────────────────────────────── the job ────────────────────────────────

STEP_LABELS = {
    "prepare": "Getting ready",
    "stop": "Stopping the current model",
    "memory": "Waiting for memory to free up",
    "start": "Loading the new model",
    "verify": "Checking it answers (and how fast)",
    "openclaw": "Pointing OpenClaw at it",
    "rollback": "Restoring the previous model",
    "run": "Installing",
    "requires": "Checking what it needs",
    "fetch": "Downloading and setting up",
    "check": "Checking it's ready",
}


class SwapFailed(Exception):
    pass


class NotInstalled(ValueError):
    pass


class Job:
    def __init__(self, target: str, source: str | None, steps: list[str]):
        self.id = uuid.uuid4().hex[:10]
        self.target = target
        self.source = source
        self.state = "running"          # running | done | failed
        self.error: str | None = None
        self.note: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.log: deque[tuple[float, str]] = deque(maxlen=1500)
        self.steps = [{"key": k, "label": STEP_LABELS.get(k, k), "state": "pending",
                       "started": None, "finished": None, "detail": ""} for k in steps]
        self._lock = threading.Lock()

    def say(self, msg: str):
        msg = ANSI.sub("", msg or "").rstrip()
        if not msg:
            return
        with self._lock:
            for line in msg.splitlines():
                self.log.append((time.time(), line))

    def step(self, key: str, state: str, detail: str | None = None):
        with self._lock:
            for s in self.steps:
                if s["key"] == key:
                    s["state"] = state
                    if state == "running" and not s["started"]:
                        s["started"] = time.time()
                    if state in ("done", "failed", "skipped"):
                        s["finished"] = time.time()
                    if detail is not None:
                        s["detail"] = detail
                    return
            # steps can be added late (rollback)
            self.steps.append({"key": key, "label": STEP_LABELS.get(key, key), "state": state,
                               "started": time.time(), "finished": None, "detail": detail or ""})

    def to_dict(self, log_since: int = 0) -> dict:
        with self._lock:
            log = list(self.log)
        return {
            "id": self.id, "target": self.target, "source": self.source,
            "state": self.state, "error": self.error, "note": self.note,
            "started": self.started, "finished": self.finished,
            "steps": self.steps,
            "log_total": len(log),
            "log": [{"t": t, "m": m} for t, m in log[-400:]],
        }


# ─────────────────────────────── the engine ─────────────────────────────

class Engine:
    def __init__(self, config_path: Path, state_dir: Path, catalog_path: Path | None = None,
                 conf_dir: Path | None = None):
        self.config_path = Path(config_path)
        self.catalog_path = Path(catalog_path) if catalog_path else ROOT / "catalog.json"
        self.conf_dir = Path(conf_dir) if conf_dir else self.config_path.parent
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.version = app_version()
        site = config_mod.load_json(self.config_path, {}) or {}
        if DEMO:
            self.platform = os.environ.get("SWAPPER_DEMO_PLATFORM", hardware.NVIDIA_GB10)
            self.platform_evidence = "demo"
        else:
            self.platform, self.platform_evidence = hardware.detect(sh, site.get("platform"))
        self.cfg = config_mod.merge(site, config_mod.load_json(self.catalog_path, {}) or {}, self.platform)
        self.job: Job | None = None
        self._swap_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._status_cache: tuple[float, dict] | None = None
        self._install_cache: dict[str, tuple[float, dict]] = {}
        self._mtp_cache: dict[str, bool] = {}

    # ── persistent state ──
    @property
    def _state_file(self) -> Path:
        return self.state_dir / "state.json"

    def load_state(self) -> dict:
        try:
            return json.loads(self._state_file.read_text())
        except (OSError, ValueError):
            return {}

    def save_state(self, **kv):
        with self._state_lock:
            st = self.load_state()
            st.update(kv)
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=2))
            tmp.replace(self._state_file)

    def update_state(self, key: str, fn):
        """Read-modify-write one top-level state entry under the lock."""
        with self._state_lock:
            st = self.load_state()
            st[key] = fn(st.get(key))
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=2))
            tmp.replace(self._state_file)
            return st[key]

    @property
    def models(self) -> dict:
        return self.cfg["models"]

    @property
    def key(self) -> str:
        return self.cfg.get("api_key", "vllm-local")

    @property
    def api_port(self) -> int:
        return int(self.cfg.get("api_port", 8888))

    @property
    def user(self) -> str:
        return (self.cfg.get("openclaw") or {}).get("user") or "root"

    @property
    def home(self) -> str:
        return config_mod.site_values(self.cfg)["HOME"]

    @property
    def models_dir(self) -> Path:
        return self.state_dir / "models"

    def hostname(self) -> str:
        return os.uname().nodename if hasattr(os, "uname") else socket.gethostname()

    # ── observation ──
    def running_containers(self) -> set[str]:
        rc, out = sh(["docker", "ps", "--format", "{{.Names}}"], timeout=15)
        return set(out.split()) if rc == 0 else set()

    def active_units(self) -> set[str]:
        """Our llama.cpp units that systemd says are running (or starting)."""
        if not any(m["kind"] == "llamacpp" for m in self.models.values()):
            return set()
        rc, out = sh(["systemctl", "list-units", "--type=service", "--all", "--no-legend", "--plain",
                      "swapper-llama-*"], timeout=10)
        units = set()
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[2] in ("active", "activating", "reloading"):
                    units.add(parts[0].removesuffix(".service"))
        return units

    def container_exists(self, name: str) -> bool:
        return sh(["docker", "inspect", name], timeout=15)[0] == 0

    def image_exists(self, name: str) -> bool:
        return sh(["docker", "image", "inspect", name], timeout=15)[0] == 0

    def model_running(self, key: str, containers: set[str], units: set[str]) -> bool:
        m = self.models[key]
        if m["kind"] == "llamacpp":
            return m.get("unit") in units
        return bool(m.get("container")) and m["container"] in containers

    def served(self) -> dict | None:
        d = http_json(f"http://127.0.0.1:{self.api_port}/v1/models", self.key, timeout=3)
        if not d or not d.get("data"):
            return None
        m = d["data"][0]
        # vLLM and SGLang report the window they serve; llama.cpp only the model's
        # training length, which says nothing about --ctx-size, so it is left out.
        return {"id": m.get("id"), "context": m.get("max_model_len")}

    @staticmethod
    def serves(m: dict, served: dict | None) -> bool:
        if not served or not served.get("id"):
            return False
        sid = str(served["id"])
        return sid == m["served_id"] or bool(m.get("served_match") and m["served_match"] in sid)

    # ── installs ──
    def local_file(self, repo: str, path: str) -> Path:
        return self.models_dir / repo / path

    @staticmethod
    def split_ref(ref: str) -> tuple[str, str]:
        """"owner/repo/dir/file.gguf" → ("owner/repo", "dir/file.gguf")."""
        owner, _, rest = ref.partition("/")
        repo, _, path = rest.partition("/")
        return f"{owner}/{repo}", path

    def file_verified(self, repo: str, path: str) -> bool:
        f = self.local_file(repo, path)
        marker = f.with_name(f.name + ".ok")
        try:
            info = json.loads(marker.read_text())
            return f.is_file() and f.stat().st_size == info.get("size")
        except (OSError, ValueError):
            return False

    def llama_server(self) -> Path | None:
        info = self.load_state().get("llamacpp") or {}
        p = Path(info.get("bin") or "/nonexistent")
        return p if p.is_file() else None

    def mtp_supported(self, exe: Path) -> bool:
        k = str(exe)
        if k not in self._mtp_cache:
            rc, out = sh([str(exe), "--help"], timeout=30,
                         env={**os.environ, "LD_LIBRARY_PATH": str(exe.parent)})
            self._mtp_cache[k] = "draft-mtp" in out
        return self._mtp_cache[k]

    def _ready(self, m: dict, check: dict) -> tuple[bool, str]:
        if "file" in check:
            ok = Path(check["file"]).is_file()
            return ok, "" if ok else f"missing {check['file']}"
        if "hf_cached" in check:
            repo = check["hf_cached"]
            home = check.get("home") or self.home
            snaps = Path(home) / ".cache/huggingface/hub" / f"models--{repo.replace('/', '--')}" / "snapshots"
            ok = any(p.is_dir() for p in snaps.glob("*")) if snaps.is_dir() else False
            return ok, "" if ok else f"{repo} not downloaded"
        if "docker_image_or_container" in check:
            ok = self.image_exists(check["docker_image_or_container"]) or \
                 (bool(check.get("container")) and self.container_exists(check["container"]))
            return ok, "" if ok else f"image {check['docker_image_or_container']} not pulled"
        if "hf_files" in check:
            missing = [f["path"] for f in m.get("files", []) if not self.file_verified(f["repo"], f["path"])]
            if missing:
                return False, f"{len(missing)} file(s) not downloaded"
            if not self.llama_server():
                return False, "llama.cpp isn't installed"
            return True, ""
        return True, ""

    def install_state(self, key: str, fresh: bool = False) -> dict:
        """{"installed": bool, "detail": str}, cached for 20 s (some checks ask docker)."""
        now = time.time()
        hit = self._install_cache.get(key)
        if hit and not fresh and now - hit[0] < 20:
            return hit[1]
        m = self.models[key]
        if DEMO:
            demo_missing = {"nemotron", "halo27b-q8", "haloflash", "flash"}
            st = {"installed": key not in demo_missing, "detail": "" if key not in demo_missing else "not downloaded"}
        elif m["kind"] == "flash-vllm":
            d = Path(m.get("dir", ""))
            ok = (d / "start.sh").is_file() and (d / ".env").is_file()
            st = {"installed": ok, "detail": "" if ok else f"recipe not at {d}"}
        elif m["kind"] == "sglang-docker" and not (m.get("install") or {}).get("ready"):
            st = {"installed": True, "detail": ""}
        else:
            results = [self._ready(m, c) for c in (m.get("install") or {}).get("ready", [])]
            bad = [why for ok, why in results if not ok]
            st = {"installed": not bad, "detail": "; ".join(bad)}
        self._install_cache[key] = (now, st)
        return st

    def forget_install_cache(self):
        self._install_cache.clear()
        self._status_cache = None

    # ── status ──
    def front_state(self) -> dict:
        """Whether the :11443 HTTPS front is ours (it then checks the API key)."""
        if DEMO:
            return {"up": True, "enforces_key": True}
        conf = Path("/etc/nginx/conf.d/spark-model-front.conf")
        try:
            ours = "Written by Spark Swapper" in conf.read_text(errors="replace")
        except OSError:
            ours = False
        return {"up": port_open(self.cfg.get("nginx_port", 11443)), "enforces_key": ours or DEMO}

    def model_summary(self, key: str, m: dict, running: bool, healthy: bool, served: dict | None,
                      measured: dict) -> dict:
        inst = self.install_state(key)
        install = m.get("install") or {}
        return {
            "key": key, "title": m["title"], "tagline": m.get("tagline", ""),
            "engine": m.get("engine", ""), "context": m["context"],
            "served_context": (served or {}).get("context") if healthy else None,
            "served_id": m["served_id"], "vision": m.get("vision", False),
            "running": running, "healthy": healthy,
            "order": m.get("order", 9),
            "platform": self.platform,
            "family": m.get("family"),
            "kind": m["kind"],
            "installed": inst["installed"],
            "install_detail": inst["detail"],
            "install": {"size_gb": install.get("size_gb"), "time": install.get("time"),
                        "requires": install.get("requires", [])} if install else None,
            "perf": m.get("perf"),
            "measured": measured.get(key),
            "recommended": bool(m.get("recommended")),
            "superseded_by": m.get("superseded_by"),
            "eta": m.get("eta"),
            "repo": m.get("repo"),
            "anthropic_api": bool(m.get("anthropic_api")),
        }

    def status(self, fresh: bool = False) -> dict:
        now = time.time()
        if not fresh and self._status_cache and now - self._status_cache[0] < 2.5:
            return self._status_cache[1]
        st = self.load_state()
        measured = st.get("measured") or {}
        if DEMO:
            demo_active = next((k for k, m in self.models.items() if m.get("recommended")), None) \
                or next(iter(self.models), None)
            containers, units = set(), set()
            served = {"id": self.models[demo_active]["served_id"], "context": self.models[demo_active]["context"]} \
                if demo_active else None
        else:
            containers = self.running_containers()
            units = self.active_units()
            served = self.served()
        models = {}
        active = None
        for key, m in self.models.items():
            is_running = self.model_running(key, containers, units)
            healthy = self.serves(m, served)
            if healthy:
                active = key
                is_running = True
            models[key] = self.model_summary(key, m, is_running, healthy, served, measured)
        loading = [k for k, v in models.items() if v["running"] and not v["healthy"]]
        out = {
            "active": active,
            "loading": loading[0] if loading and not active else None,
            "desired": st.get("desired"),
            "last_swap": st.get("last_swap"),
            "models": models,
            "memory": {"total": 128.0, "available": 97.4, "free": 41.2} if DEMO else meminfo(),
            "openclaw_primary": self.openclaw_primary() if not DEMO else
                (f"{self.models[active]['openclaw_provider']}/{self.models[active]['served_id']}" if active else None),
            "nginx": True if DEMO else port_open(self.cfg.get("nginx_port", 11443)),
            "front": self.front_state(),
            "gateway": True if DEMO else port_open(self.cfg["openclaw"].get("gateway_port", 18789)),
            "busy": bool(self.job and self.job.state == "running"),
            "version": self.version,
            "platform": self.platform,
            "platform_label": hardware.label(self.platform),
            "hostname": self.hostname(),
        }
        if self.platform == hardware.AMD_STRIX_HALO:
            out["gpu"] = {"vram_gib": 0.5, "gtt_gib": 108.0, "total_gib": 108.5} if DEMO else hardware.amd_gpu_memory()
        self._status_cache = (now, out)
        return out

    # ── OpenClaw ──
    def _oc_env(self) -> dict:
        oc = self.cfg["openclaw"]
        uid = self._uid(oc["user"])
        return {
            "HOME": oc["home"], "USER": oc["user"], "LOGNAME": oc["user"],
            "PATH": f"{Path(oc['bin']).parent}:/usr/local/bin:/usr/bin:/bin",
            "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
        }

    @staticmethod
    def _uid(user: str) -> int:
        import pwd
        return pwd.getpwnam(user).pw_uid

    def _as_oc_user(self, cmd: list[str]) -> list[str]:
        oc = self.cfg["openclaw"]
        if os.geteuid() == 0:
            envs = [f"{k}={v}" for k, v in self._oc_env().items()]
            return ["runuser", "-u", oc["user"], "--", "env", *envs, *cmd]
        return cmd

    def openclaw_config(self) -> dict:
        try:
            return json.loads((Path(self.cfg["openclaw"]["home"]) / ".openclaw/openclaw.json").read_text())
        except (OSError, ValueError):
            return {}

    def openclaw_primary(self) -> str | None:
        return (((self.openclaw_config().get("agents") or {}).get("defaults") or {})
                .get("model") or {}).get("primary")

    def openclaw_ref(self, key: str) -> str:
        m = self.models[key]
        return f"{m['openclaw_provider']}/{m['served_id']}"

    def openclaw_installed(self) -> bool:
        oc = self.cfg.get("openclaw") or {}
        return bool(oc.get("bin")) and Path(oc["bin"]).exists()

    def openclaw_point_to(self, key: str, job: Job):
        m = self.models[key]
        ref = self.openclaw_ref(key)
        provider = {
            "baseUrl": f"http://127.0.0.1:{self.api_port}/v1",
            "apiKey": self.key,
            "api": "openai-completions",
            "timeoutSeconds": 10800,
            "models": [{
                "id": m["served_id"],
                "name": m.get("openclaw_model_name", m["title"]),
                "api": "openai-completions",
                "reasoning": True,
                "input": ["text", "image"] if m.get("vision") else ["text"],
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                "contextWindow": m.get("openclaw_context", m["context"]),
                "contextTokens": m.get("openclaw_context", m["context"]),
                "maxTokens": 32768,
                "compat": {"supportsTools": True, "supportsUsageInStreaming": True},
            }],
        }
        patch = {
            "models": {"providers": {m["openclaw_provider"]: provider}},
            "agents": {"defaults": {
                "model": {"primary": ref},
                "imageModel": ref,
                "compaction": {"model": ref, "memoryFlush": {"model": ref}},
            }},
        }
        oc_bin = self.cfg["openclaw"]["bin"]
        replace = f"models.providers.{m['openclaw_provider']}.models"
        cmd = self._as_oc_user([oc_bin, "config", "patch", "--stdin", "--replace-path", replace])
        rc, out = sh(cmd, timeout=120, input_text=json.dumps(patch))
        job.say(out)
        if rc != 0:
            raise SwapFailed(f"OpenClaw rejected the config change (exit {rc}).")
        job.say(f"OpenClaw primary model → {ref}")

        unit = self.cfg["openclaw"]["unit"]
        rc, out = sh(self._as_oc_user(["systemctl", "--user", "restart", unit]), timeout=60)
        if rc != 0:
            job.say(out)
            job.say("Could not restart the OpenClaw gateway via systemd; it will pick the change up on its next restart.")
        else:
            job.say("OpenClaw gateway restarted.")
            gp = self.cfg["openclaw"].get("gateway_port", 18789)
            for _ in range(60):
                if port_open(gp):
                    job.say(f"OpenClaw gateway is listening on :{gp}.")
                    break
                time.sleep(1)
            else:
                job.say("OpenClaw gateway did not come back within 60s — check `systemctl --user status openclaw-gateway`.")
        got = self.openclaw_primary()
        if got != ref:
            raise SwapFailed(f"OpenClaw read-back shows {got!r}, expected {ref!r}.")

    # ── running things as the box's user ──
    def user_env(self, extra: dict | None = None) -> dict:
        home, user = self.home, self.user
        env = {"HOME": home, "USER": user, "LOGNAME": user, "LANG": "C.UTF-8",
               "PATH": f"{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
        env.update({k: str(v) for k, v in (extra or {}).items()})
        return env

    def as_user(self, cmd: list[str], env: dict | None = None) -> list[str]:
        """`cmd` as the box's user with a clean environment (HOME etc. set)."""
        envs = [f"{k}={v}" for k, v in self.user_env(env).items()]
        if os.geteuid() == 0 and self.user != "root":
            return ["runuser", "-u", self.user, "--", "env", "-i", *envs, *cmd]
        return ["env", "-i", *envs, *cmd]

    def run_cmd(self, m_or_step: dict, cmd: list[str], env: dict | None = None) -> tuple[list[str], dict | None]:
        """(argv, env) for a recipe command honouring "as" / "run_as" (user | root)."""
        who = m_or_step.get("as") or m_or_step.get("run_as") or "root"
        if who == "user":
            return self.as_user(cmd, env), None
        return cmd, {**os.environ, "HOME": "/root", **{k: str(v) for k, v in (env or {}).items()}}

    def ensure_docker_group(self, job: Job):
        """Recipes run their containers as the box's user, who must be in the docker group."""
        if DEMO or self.user == "root" or os.geteuid() != 0:
            return
        rc, out = sh(["id", "-nG", self.user], timeout=10)
        if rc == 0 and "docker" not in out.split():
            rc2, out2 = sh(["usermod", "-aG", "docker", self.user], timeout=20)
            job.say(f"Added {self.user} to the docker group so recipes can start their container."
                    if rc2 == 0 else f"Couldn't add {self.user} to the docker group: {out2}")

    # ── per-backend stop / start ──
    def stop_model(self, key: str, job: Job):
        m = self.models[key]
        kind = m["kind"]
        if kind == "sglang-docker":
            # Keep the container (fast restart) but stop docker from reviving it at boot.
            sh(["docker", "update", "--restart", "no", m["container"]], timeout=20)
            job.say(f"docker stop {m['container']} (up to 60s for a clean shutdown)")
            rc, out = sh(["docker", "stop", "-t", "60", m["container"]], timeout=90)
            job.say(out)
        elif kind == "flash-vllm":
            job.say("Running the Flash recipe's stop.sh (graceful, frees shared memory)")
            rc, out = sh(["bash", "./stop.sh"], cwd=m["dir"], timeout=120,
                         env={**os.environ, "HOME": "/root"})
            job.say(out)
            if m["container"] in self.running_containers():
                sh(["docker", "rm", "-f", m["container"]], timeout=60)
        elif kind == "recipe-script":
            job.say(f"Running {m['title']}'s stop script")
            argv, env = self.run_cmd(m, m.get("stop") or ["bash", "./stop.sh"],
                                     {"PORT": self.api_port, **(m.get("env") or {})})
            rc, out = sh(argv, cwd=m["dir"], timeout=180, env=env)
            job.say(out)
            if m["container"] in self.running_containers():
                sh(["docker", "rm", "-f", m["container"]], timeout=60)
        elif kind == "llamacpp":
            job.say(f"systemctl stop {m['unit']}")
            rc, out = sh(["systemctl", "stop", m["unit"]], timeout=60)
            job.say(out)
        for _ in range(60):
            if not self.model_running(key, self.running_containers(), self.active_units()):
                job.say(f"{m['title']} is stopped.")
                return
            time.sleep(1)
        raise SwapFailed(f"{m['title']} would not stop.")

    def gpu_tenants(self) -> list[str]:
        rc, out = sh(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                      "--format=csv,noheader"], timeout=20)
        return [l.strip() for l in out.splitlines() if l.strip()] if rc == 0 else []

    def wait_memory(self, key: str, job: Job):
        # The Flash launcher refuses to start while any process holds the GPU;
        # give a stopped server's workers time to exit first.
        for i in range(60):
            tenants = self.gpu_tenants()
            if not tenants:
                if i:
                    job.say("GPU is idle.")
                break
            if i == 0:
                job.say("Waiting for GPU processes to exit: " + "; ".join(tenants))
            time.sleep(2)
        else:
            job.say("Still on the GPU after 2 minutes: " + "; ".join(self.gpu_tenants()))
        need = self.models[key].get("min_free_gib", 95)
        deadline = time.time() + 180
        last = -1.0
        while True:
            mem = meminfo()
            if mem["available"] >= need:
                job.say(f"Memory available: {mem['available']:.0f} GiB of {mem['total']:.0f} GiB. Good to go.")
                return
            if abs(mem["available"] - last) >= 2:
                job.say(f"Memory available: {mem['available']:.0f} GiB (waiting for ~{need} GiB)")
                last = mem["available"]
            if time.time() > deadline:
                job.say(f"Only {mem['available']:.0f} GiB free after 3 minutes — something else is using memory. Trying anyway.")
                return
            time.sleep(3)

    def _checkpoint_home(self, m: dict) -> str | None:
        name = m.get("checkpoint")
        if not name:
            return "/root"
        import pwd
        homes = ["/root", self.cfg.get("openclaw", {}).get("home", ""),
                 pwd.getpwuid(os.getuid()).pw_dir]
        for home in homes:
            if home and (Path(home) / ".cache/huggingface/hub" / name).is_dir():
                return home
        return None

    def preflight(self, key: str, job: Job):
        m = self.models[key]
        kind = m["kind"]
        if kind == "flash-vllm":
            d = Path(m["dir"])
            if not (d / "start.sh").is_file() or not (d / ".env").is_file():
                raise SwapFailed(f"Flash recipe not found at {d} (need start.sh and .env).")
            if not self._checkpoint_home(m):
                raise SwapFailed(f"Flash checkpoint {m['checkpoint']} is not in any Hugging Face cache. Run download.sh first.")
        elif kind == "sglang-docker":
            if not self.container_exists(m["container"]):
                job.say(f"Container {m['container']} is missing — it will be recreated from the saved recipe.")
        elif kind == "recipe-script":
            if not (Path(m["dir"]) / "start.sh").is_file():
                raise SwapFailed(f"{m['title']} isn't installed (no recipe at {m['dir']}). Install it from the Models tab.")
            if m.get("run_as") == "user":
                self.ensure_docker_group(job)
        elif kind == "llamacpp":
            if not self.llama_server():
                raise SwapFailed("llama.cpp isn't installed. Install it from the Provisioning tab.")
            missing = [f["path"] for f in m.get("files", []) if not self.file_verified(f["repo"], f["path"])]
            if missing:
                raise SwapFailed(f"{m['title']} isn't downloaded yet ({len(missing)} file(s) missing). Install it from the Models tab.")
            gpu = hardware.amd_gpu_memory()
            need = m.get("gpu_gib")
            if gpu and need and gpu["total_gib"] < need:
                raise SwapFailed(
                    f"{m['title']} needs about {need} GiB of GPU memory, but the GPU may only use "
                    f"{gpu['total_gib']:.0f} GiB right now. Raise the limit on the Provisioning tab "
                    f"(\"GPU memory limit\"), reboot, then try again.")
        mem = meminfo()
        job.say(f"Host memory: {mem['available']:.0f} GiB available of {mem['total']:.0f} GiB.")

    def llama_argv(self, key: str) -> list[str]:
        m = self.models[key]
        exe = self.llama_server()
        if not exe:
            raise SwapFailed("llama.cpp isn't installed.")
        argv = [str(exe), "--model", str(self.local_file(*self.split_ref(m["model_file"]))),
                "--alias", m["served_id"],
                "--host", "127.0.0.1", "--port", str(self.api_port)]
        if self.key:
            argv += ["--api-key", self.key]
        argv += list(m.get("args") or [])
        if m.get("spec_args") and self.mtp_supported(exe):
            if m.get("draft_file"):
                argv += ["-md", str(self.local_file(*self.split_ref(m["draft_file"])))]
            argv += list(m["spec_args"])
        return argv

    def start_model(self, key: str, job: Job):
        m = self.models[key]
        timeout = m.get("start_timeout_s", 1800)
        kind = m["kind"]
        if kind == "sglang-docker":
            if self.container_exists(m["container"]):
                sh(["docker", "update", "--restart", "unless-stopped", m["container"]], timeout=20)
                rc, out = sh(["docker", "start", m["container"]], timeout=60)
                job.say(f"docker start {m['container']}: {out}")
                if rc != 0:
                    raise SwapFailed(f"docker start failed: {out}")
            else:
                rc, out = sh(["docker", "run", "-d", "--name", m["container"], *m["docker_run"]], timeout=300)
                job.say(out)
                if rc != 0:
                    raise SwapFailed(f"docker run failed: {out[-400:]}")
            self._wait_healthy(key, job, timeout, proc=None, logfile=None)
        elif kind == "flash-vllm":
            home = self._checkpoint_home(m) or "/root"
            env = {**os.environ, "HOME": home,
                   "PORT": str(self.api_port), "BIND": "127.0.0.1",
                   "API_KEY": self.key, "READY_TIMEOUT_S": str(timeout)}
            env.update({k: str(v) for k, v in (m.get("env") or {}).items()})
            logfile = self.state_dir / f"flash-start-{job.id}.log"
            job.say(f"./start.sh  (PORT={env['PORT']} BIND=127.0.0.1, "
                    + " ".join(f"{k}={v}" for k, v in (m.get('env') or {}).items())
                    + ") — a cold start takes about 10–12 minutes")
            with open(logfile, "w") as fh:
                proc = subprocess.Popen(["bash", "./start.sh"], cwd=m["dir"], env=env,
                                        stdout=fh, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
            self._wait_healthy(key, job, timeout + 120, proc=proc, logfile=logfile)
        elif kind == "recipe-script":
            env = {"PORT": self.api_port, **(m.get("env") or {})}
            argv, penv = self.run_cmd(m, m.get("start") or ["bash", "./start.sh"], env)
            logfile = self.state_dir / f"start-{key}-{job.id}.log"
            job.say(f"{' '.join(m.get('start') or ['./start.sh'])} in {m['dir']}  ("
                    + " ".join(f"{k}={v}" for k, v in env.items()) + ")")
            with open(logfile, "w") as fh:
                proc = subprocess.Popen(argv, cwd=m["dir"], env=penv, stdout=fh, stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL, start_new_session=True)
            self._wait_healthy(key, job, timeout + 120, proc=proc, logfile=logfile)
        elif kind == "llamacpp":
            argv = self.llama_argv(key)
            exe = Path(argv[0])
            if not self.mtp_supported(exe) and m.get("spec_args"):
                job.say("This llama.cpp build has no MTP drafts (--spec-type draft-mtp), so the model runs "
                        "without them — about half the speed. Update llama.cpp on the Provisioning tab.")
            cache = self.state_dir / "llama-cache"
            cache.mkdir(parents=True, exist_ok=True)
            unit = m["unit"]
            sh(["systemctl", "stop", unit], timeout=30)
            sh(["systemctl", "reset-failed", unit], timeout=10)
            cmd = ["systemd-run", f"--unit={unit}", "--collect", "--quiet",
                   "--description", f"Spark Swapper: {m['title']}",
                   "-p", "Restart=no", "-p", "LimitMEMLOCK=infinity",
                   f"--setenv=LD_LIBRARY_PATH={exe.parent}",
                   f"--setenv=HOME={cache}", f"--setenv=XDG_CACHE_HOME={cache}",
                   "--", *argv]
            shown = ["•••" if a == self.key else a for a in argv[1:]]
            job.say("llama-server " + " ".join(shown))
            rc, out = sh(cmd, timeout=30)
            if rc != 0:
                raise SwapFailed(f"systemd-run failed: {out[-400:]}")
            self._wait_healthy(key, job, timeout, proc=None, logfile=None, unit=unit)

    def _journal(self, unit: str, since: float) -> list[str]:
        rc, out = sh(["journalctl", "-u", unit, "--since", f"@{int(since)}", "-o", "cat", "--no-pager"], timeout=15)
        return out.splitlines() if rc == 0 else []

    def _wait_healthy(self, key: str, job: Job, timeout: float, proc, logfile: Path | None,
                      unit: str | None = None):
        m = self.models[key]
        t0 = time.time()
        pos = 0
        journal_seen = 0
        last_beat = t0
        seen_running = False
        while True:
            # stream the launcher's output (recipe scripts)
            if logfile and logfile.exists():
                with open(logfile, errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                if chunk:
                    job.say(chunk)
            if unit:
                lines = self._journal(unit, t0 - 5)
                if len(lines) > journal_seen:
                    job.say("\n".join(lines[journal_seen:][-60:]))
                    journal_seen = len(lines)
            s = self.served()
            if self.serves(m, s):
                ctx = s.get("context")
                job.say(f"{m['title']} is serving" + (f" (context {ctx:,} tokens)" if isinstance(ctx, int) else "")
                        + f" after {int(time.time()-t0)}s.")
                if proc is not None and proc.poll() is None:
                    # start.sh prints a summary right after the server answers; give it a moment.
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        pass
                    if logfile:
                        with open(logfile, errors="replace") as fh:
                            fh.seek(pos)
                            job.say(fh.read())
                return
            running = self.model_running(key, self.running_containers(), self.active_units())
            seen_running |= running
            if proc is not None and proc.poll() is not None and proc.returncode != 0:
                raise SwapFailed(f"{m['title']} launcher exited with code {proc.returncode}. See the log above.")
            if proc is None and seen_running and not running:
                if unit:
                    job.say("\n".join(self._journal(unit, t0 - 5)[-25:]))
                else:
                    _, logs = sh(["docker", "logs", "--tail", "25", m["container"]], timeout=20)
                    job.say(logs)
                raise SwapFailed(f"{m['title']} exited while loading.")
            if proc is None and not running and time.time() - t0 > 60:
                raise SwapFailed(f"{m['title']} is not running.")
            if proc is not None and proc.poll() == 0 and not running and time.time() - t0 > 90:
                raise SwapFailed(f"{m['title']}'s start script finished but nothing is serving.")
            if time.time() - t0 > timeout:
                if proc is not None and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                raise SwapFailed(f"{m['title']} did not become ready within {int(timeout/60)} minutes.")
            if proc is None and not unit and time.time() - last_beat >= 30:
                last_beat = time.time()
                _, tail = sh(["docker", "logs", "--tail", "1", m["container"]], timeout=10)
                mem = meminfo()
                job.say(f"…still loading ({int(time.time()-t0)}s, {mem['available']:.0f} GiB free)"
                        + (f"  {tail[-160:]}" if tail else ""))
            time.sleep(3)

    def measure(self, key: str, job: Job | None = None, usage: bool = True) -> dict | None:
        """Stream one ~256-token reply and time it: decode tokens per second after
        the first token. Returns {"decode", "ttft", "tokens", "reply"} or None when
        the request failed. Without `usage` the server isn't asked for token counts
        (for servers that refuse stream_options) and streamed chunks are counted."""
        m = self.models[key]
        body = {"model": m["served_id"], "max_tokens": 256, "temperature": 0, "stream": True,
                "messages": [{"role": "user", "content":
                              "In one paragraph of about 150 words, explain why the sky is blue."}],
                "chat_template_kwargs": {"enable_thinking": False}}
        if usage:
            body["stream_options"] = {"include_usage": True}
        req = urllib.request.Request(f"http://127.0.0.1:{self.api_port}/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": f"Bearer {self.key}"})
        t0 = time.time()
        first = last = None
        chunks = 0
        usage = None
        text = []
        try:
            with urllib.request.urlopen(req, timeout=240) as r:
                for raw in r:
                    line = raw.decode(errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        ev = json.loads(data)
                    except ValueError:
                        continue
                    if ev.get("usage"):
                        usage = ev["usage"]
                    for ch in ev.get("choices") or []:
                        delta = ch.get("delta") or {}
                        piece = delta.get("content") or delta.get("reasoning_content") or ""
                        if piece:
                            now = time.time()
                            first = first or now
                            last = now
                            chunks += 1
                            text.append(piece)
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            if job:
                job.say(f"Speed check failed: {e}")
            return None
        tokens = int((usage or {}).get("completion_tokens") or chunks)
        if not first or not last or last <= first or tokens < 8:
            return {"decode": None, "ttft": round(first - t0, 2) if first else None, "tokens": tokens,
                    "reply": "".join(text)[:80]}
        return {"decode": round((tokens - 1) / (last - first), 1), "ttft": round(first - t0, 2),
                "tokens": tokens, "reply": "".join(text)[:80]}

    def verify(self, key: str, job: Job):
        m = self.models[key]
        ng = self.cfg.get("nginx_port", 11443)
        d = http_json(f"https://127.0.0.1:{ng}/v1/models", self.key, timeout=8)
        if d and d.get("data") and self.serves(m, {"id": d["data"][0].get("id")}):
            job.say(f"nginx https://…:{ng}/v1 → {d['data'][0].get('id')} ✓")
        else:
            job.say(f"Warning: nginx on :{ng} did not return {m['served_id']} (got {d}).")
        front = self.front_state()
        if front["up"] and not front["enforces_key"] and m["kind"] in ("recipe-script",):
            job.say(f"Note: this model's server has no API key of its own, and the HTTPS front on :{ng} "
                    "wasn't set up by Spark Swapper, so it doesn't check the key either. Run "
                    f"Provisioning ▸ HTTPS front once to make :{ng} require the API key.")
        result = self.measure(key, job) or self.measure(key, job, usage=False)
        if not result or not result.get("tokens"):
            # Some servers don't stream the way the speed check expects; a plain
            # request still proves the model answers.
            body = {"model": m["served_id"], "max_tokens": 24, "temperature": 0,
                    "messages": [{"role": "user", "content": "Reply with exactly: ready"}],
                    "chat_template_kwargs": {"enable_thinking": False}}
            r = http_json(f"http://127.0.0.1:{self.api_port}/v1/chat/completions", self.key, timeout=180, body=body)
            if not (r and r.get("choices")):
                raise SwapFailed("The model is up but did not answer a test prompt.")
            txt = (r["choices"][0].get("message") or {}).get("content") or ""
            job.say(f"Test reply: {txt.strip()[:80]!r} (speed not measured)")
            return
        job.say(f"Test reply: {result.get('reply', '').strip()!r}")
        if result.get("decode"):
            job.say(f"Speed here: {result['decode']} tokens/s decode, first token after {result['ttft']} s "
                    f"({result['tokens']} tokens, thinking off).")

            def put(cur):
                cur = dict(cur or {})
                cur[key] = {"decode": result["decode"], "ttft": result["ttft"], "at": time.time()}
                return cur
            self.update_state("measured", put)

    # ── the swap ──
    def begin_swap(self, target: str) -> Job:
        if target not in self.models:
            raise ValueError("unknown model")
        if not self.install_state(target, fresh=True)["installed"]:
            raise NotInstalled(f"{self.models[target]['title']} isn't installed yet. Install it first.")
        if not self._swap_lock.acquire(blocking=False):
            raise RuntimeError("A swap is already in progress.")
        try:
            st = self.status(fresh=True)
            source = st["active"] or st["loading"]
            steps = ["prepare", "stop", "memory", "start", "verify", "openclaw"]
            job = Job(target, source, steps)
            self.job = job
            threading.Thread(target=self._run_swap, args=(job,), daemon=True).start()
            return job
        except Exception:
            self._swap_lock.release()
            raise

    def _run_swap(self, job: Job):
        target, source = job.target, job.source
        cur = None
        try:
            cur = "prepare"; job.step(cur, "running")
            self.preflight(target, job)
            already = self.status(fresh=True)["active"] == target
            job.step(cur, "done")

            cur = "stop"; job.step(cur, "running")
            containers, units = self.running_containers(), self.active_units()
            others = [k for k in self.models if k != target and self.model_running(k, containers, units)]
            if not others:
                job.say("Nothing else is running.")
            for k in others:
                self.stop_model(k, job)
            job.step(cur, "done" if others else "skipped")

            if already:
                job.say(f"{self.models[target]['title']} is already serving — skipping the load.")
                job.step("memory", "skipped"); job.step("start", "skipped")
            else:
                if self.model_running(target, self.running_containers(), self.active_units()):
                    job.say("Target is half-loaded; restarting it cleanly.")
                    self.stop_model(target, job)
                cur = "memory"; job.step(cur, "running")
                self.wait_memory(target, job)
                job.step(cur, "done")
                cur = "start"; job.step(cur, "running")
                self.start_model(target, job)
                job.step(cur, "done")

            cur = "verify"; job.step(cur, "running")
            self.verify(target, job)
            job.step(cur, "done")

            cur = "openclaw"; job.step(cur, "running")
            if not self.openclaw_installed():
                job.say("OpenClaw isn't installed on this box — skipping. (Install it on the Provisioning tab.)")
                job.step(cur, "skipped")
            elif self.openclaw_primary() == self.openclaw_ref(target) and already:
                job.say("OpenClaw already points here.")
                job.step(cur, "skipped")
            else:
                self.openclaw_point_to(target, job)
                job.step(cur, "done")

            self.save_state(desired=target, last_swap={"to": target, "from": source, "at": time.time(), "ok": True})
            job.state = "done"
            job.note = "Existing OpenClaw chats may still remember the old model — start a new chat if one errors."
        except Exception as e:  # noqa: BLE001 — every failure must reach the UI
            msg = str(e) if isinstance(e, SwapFailed) else f"{type(e).__name__}: {e}"
            job.say(f"✗ {msg}")
            if cur:
                job.step(cur, "failed", msg)
            job.error = msg
            job.state = "failed"
            self.save_state(last_swap={"to": target, "from": source, "at": time.time(), "ok": False, "error": msg})
            if source and source != target and cur in ("start", "verify", "memory"):
                self._rollback(job, source)
        finally:
            job.finished = time.time()
            self._status_cache = None
            self._swap_lock.release()

    def _rollback(self, job: Job, source: str):
        job.step("rollback", "running")
        job.say(f"Bringing {self.models[source]['title']} back so you are not left without a model…")
        try:
            if self.model_running(job.target, self.running_containers(), self.active_units()):
                self.stop_model(job.target, job)
            self.wait_memory(source, job)
            self.start_model(source, job)
            if self.openclaw_installed() and self.openclaw_primary() != self.openclaw_ref(source):
                self.openclaw_point_to(source, job)
            job.step("rollback", "done")
            job.note = f"{self.models[source]['title']} was restored and is serving again."
            self.save_state(desired=source)
        except Exception as e:  # noqa: BLE001
            job.step("rollback", "failed", str(e))
            job.say(f"✗ Rollback failed too: {e}")

    # ── boot reconcile ──
    def reconcile_at_boot(self):
        """If nothing is serving a few minutes after the service starts, bring
        up the model that was last chosen. Docker already revives SGLang on its
        own (restart=unless-stopped), so in practice this restarts the recipe
        scripts and llama.cpp, whose servers don't survive a reboot."""
        st0 = self.load_state()
        if "desired" not in st0:
            # First run: adopt whatever is serving now as the baseline.
            active = self.status(fresh=True)["active"]
            if active:
                self.save_state(desired=active)
        if not self.cfg.get("boot_autostart", True):
            return
        desired = self.load_state().get("desired")
        if desired not in self.models:
            return
        time.sleep(120)
        if self.job and self.job.state == "running":
            return
        st = self.status(fresh=True)
        if st["active"] or st["loading"] or port_open(self.api_port):
            return
        try:
            self.begin_swap(desired)
        except (RuntimeError, ValueError):
            pass

    # ── credentials / connecting ──
    def public_host(self, request_host: str | None = None) -> str:
        """The address other machines should use: the one the browser used to
        reach this page (unless that was localhost), else the configured one."""
        if request_host:
            h = request_host.strip()
            if h.startswith("["):
                h = h[1:h.index("]")] if "]" in h else h
            elif h.count(":") == 1:
                h = h.split(":", 1)[0]
            if h and h not in ("localhost", "127.0.0.1", "::1"):
                return h
        return self.cfg.get("public_host") or "127.0.0.1"

    def connect_info(self, cert_path: Path, port: int, request_host: str | None = None) -> dict:
        host = self.public_host(request_host)
        hp = f"[{host}]" if ":" in host else host
        ng = self.cfg.get("nginx_port", 11443)
        st = self.status()
        active = st["models"].get(st["active"] or "") or {}
        kind = hardware.short(self.platform)
        return {
            "name": f"{kind} ({self.hostname()})",
            "hostname": self.hostname(),
            "platform": self.platform,
            "version": self.version,
            "swapper_url": f"https://{hp}:{port}",
            "api_base": f"https://{hp}:{ng}/v1",
            "api_key": self.key,
            "model": active.get("served_id"),
            "context": active.get("served_context") or active.get("context"),
            "fingerprint": cert_fingerprint(cert_path) or (":".join(["00"] * 32) if DEMO else None),
            "anthropic_api": bool(active.get("anthropic_api")),
        }

    def credentials(self, request_host: str | None = None) -> dict:
        host = self.public_host(request_host)
        hp = f"[{host}]" if ":" in host else host
        ng = self.cfg.get("nginx_port", 11443)
        if DEMO:
            return {
                "openclaw": {"token": None, "password": None, "auth_mode": None,
                             "dashboard_url": f"https://{hp}:{ng}/"},
                "api": {"key": self.key, "base_url": f"https://{hp}:{ng}/v1",
                        "model": self.status()["models"].get(self.status()["active"] or "", {}).get("served_id"),
                        "found_in": [], "other_keys": {}, "openclaw_provider_keys": []},
                "demo": True,
            }
        oc = self.openclaw_config()
        gw = (oc.get("gateway") or {})
        auth = gw.get("auth") or {}
        token = auth.get("token")
        if not token:
            p = Path(self.cfg["openclaw"]["home"]) / ".openclaw/gateway_token"
            try:
                token = p.read_text().strip() or None
            except OSError:
                pass
        password = auth.get("password")

        found_keys: dict[str, list[str]] = {}
        for f in self.cfg.get("credential_sources", []):
            try:
                txt = Path(f).read_text(errors="replace")
            except OSError:
                continue
            for k in re.findall(r"--api-key\s+['\"]?([A-Za-z0-9._\-]+)", txt) + \
                     re.findall(r"API_KEY=['\"]?([A-Za-z0-9._\-]+)", txt) + \
                     re.findall(r"Bearer ([A-Za-z0-9._\-]+)", txt) + \
                     re.findall(r"API key:\s*([A-Za-z0-9._\-]+)", txt):
                if k.startswith("$"):
                    continue
                found_keys.setdefault(k, []).append(Path(f).name)
        provider_keys = {name: p.get("apiKey") for name, p in
                         ((oc.get("models") or {}).get("providers") or {}).items() if p.get("apiKey")}
        api_key = self.key
        st = self.status()
        return {
            "openclaw": {
                "token": token,
                "password": password,
                "auth_mode": auth.get("mode"),
                "dashboard_url": f"https://{hp}:{ng}/" + (f"#token={token}" if token else ""),
            },
            "api": {
                "key": api_key,
                "base_url": f"https://{hp}:{ng}/v1",
                "model": st["models"].get(st["active"] or "", {}).get("served_id"),
                "found_in": sorted(set(found_keys.get(api_key, []))),
                "other_keys": {k: sorted(set(v)) for k, v in found_keys.items() if k != api_key},
                "openclaw_provider_keys": sorted({k for k in provider_keys.values()}),
            },
        }
