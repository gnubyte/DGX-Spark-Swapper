"""Swap engine: knows how to stop, start and verify each model on the Spark,
and how to repoint OpenClaw at whichever one is live.

Everything here runs as root (docker and the root-owned Flash recipe need it).
One swap at a time; every step is logged into the Job the web UI polls.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import signal
import ssl
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
DEMO = os.environ.get("SWAPPER_DEMO", "") == "1"


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
    except FileNotFoundError as e:
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


def http_json(url: str, key: str | None = None, timeout: float = 4,
              body: dict | None = None) -> dict | None:
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # our own self-signed nginx front
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode() or "{}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def port_open(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


# ─────────────────────────────── the job ────────────────────────────────

STEP_LABELS = {
    "prepare": "Getting ready",
    "stop": "Stopping the current model",
    "memory": "Waiting for GPU memory to free up",
    "start": "Loading the new model",
    "verify": "Checking it answers",
    "openclaw": "Pointing OpenClaw at it",
    "rollback": "Restoring the previous model",
}


class SwapFailed(Exception):
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
        msg = ANSI.sub("", msg).rstrip()
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
    def __init__(self, config_path: Path, state_dir: Path):
        self.config_path = config_path
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg = json.loads(config_path.read_text())
        self.job: Job | None = None
        self._swap_lock = threading.Lock()
        self._status_cache: tuple[float, dict] | None = None

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
        st = self.load_state()
        st.update(kv)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2))
        tmp.replace(self._state_file)

    @property
    def models(self) -> dict:
        return self.cfg["models"]

    @property
    def key(self) -> str:
        return self.cfg.get("api_key", "vllm-local")

    # ── observation ──
    def running_containers(self) -> set[str]:
        rc, out = sh(["docker", "ps", "--format", "{{.Names}}"], timeout=15)
        return set(out.split()) if rc == 0 else set()

    def container_exists(self, name: str) -> bool:
        return sh(["docker", "inspect", name], timeout=15)[0] == 0

    def served(self) -> dict | None:
        d = http_json(f"http://127.0.0.1:{self.cfg['api_port']}/v1/models", self.key, timeout=3)
        if not d or not d.get("data"):
            return None
        m = d["data"][0]
        return {"id": m.get("id"), "context": m.get("max_model_len")}

    def status(self, fresh: bool = False) -> dict:
        now = time.time()
        if not fresh and self._status_cache and now - self._status_cache[0] < 2.5:
            return self._status_cache[1]
        if DEMO:
            running, served = set(), {"id": "qwen3.8-27b-sglang", "context": 1000000}
        else:
            running = self.running_containers()
            served = self.served()
        models = {}
        active = None
        for key, m in self.models.items():
            is_running = m["container"] in running
            healthy = bool(served and served["id"] == m["served_id"])
            if healthy:
                active = key
            models[key] = {
                "key": key, "title": m["title"], "tagline": m.get("tagline", ""),
                "engine": m.get("engine", ""), "context": m["context"],
                "served_context": served["context"] if healthy else None,
                "served_id": m["served_id"], "vision": m.get("vision", False),
                "running": is_running, "healthy": healthy,
                "order": m.get("order", 9),
            }
        loading = [k for k, v in models.items() if v["running"] and not v["healthy"]]
        st = self.load_state()
        if DEMO:
            mem = {"total": 128.0, "available": 97.4, "free": 41.2}
            out = {
                "active": "standard",
                "loading": None,
                "desired": "standard",
                "last_swap": {"to": "standard", "from": "flash", "at": time.time() - 86400, "ok": True},
                "models": models,
                "memory": mem,
                "openclaw_primary": "sglang/qwen3.8-27b-sglang",
                "nginx": True,
                "gateway": True,
                "busy": bool(self.job and self.job.state == "running"),
            }
            self._status_cache = (now, out)
            return out
        out = {
            "active": active,
            "loading": loading[0] if loading and not active else None,
            "desired": st.get("desired"),
            "last_swap": st.get("last_swap"),
            "models": models,
            "memory": meminfo(),
            "openclaw_primary": self.openclaw_primary(),
            "nginx": port_open(self.cfg.get("nginx_port", 11443)),
            "gateway": port_open(self.cfg["openclaw"].get("gateway_port", 18789)),
            "busy": bool(self.job and self.job.state == "running"),
        }
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

    def openclaw_point_to(self, key: str, job: Job):
        m = self.models[key]
        ref = self.openclaw_ref(key)
        port = self.cfg["api_port"]
        provider = {
            "baseUrl": f"http://127.0.0.1:{port}/v1",
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

    # ── per-backend stop / start ──
    def stop_model(self, key: str, job: Job):
        m = self.models[key]
        if m["kind"] == "sglang-docker":
            # Keep the container (fast restart) but stop docker from reviving it at boot.
            sh(["docker", "update", "--restart", "no", m["container"]], timeout=20)
            job.say(f"docker stop {m['container']} (up to 60s for a clean shutdown)")
            rc, out = sh(["docker", "stop", "-t", "60", m["container"]], timeout=90)
            job.say(out)
        elif m["kind"] == "flash-vllm":
            job.say("Running the Flash recipe's stop.sh (graceful, frees shared memory)")
            rc, out = sh(["bash", "./stop.sh"], cwd=m["dir"], timeout=120,
                         env={**os.environ, "HOME": "/root"})
            job.say(out)
            if m["container"] in self.running_containers():
                sh(["docker", "rm", "-f", m["container"]], timeout=60)
        for _ in range(60):
            if m["container"] not in self.running_containers():
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
        if m["kind"] == "flash-vllm":
            d = Path(m["dir"])
            if not (d / "start.sh").is_file() or not (d / ".env").is_file():
                raise SwapFailed(f"Flash recipe not found at {d} (need start.sh and .env).")
            if not self._checkpoint_home(m):
                raise SwapFailed(f"Flash checkpoint {m['checkpoint']} is not in any Hugging Face cache. Run download.sh first.")
        elif m["kind"] == "sglang-docker":
            if not self.container_exists(m["container"]):
                job.say(f"Container {m['container']} is missing — it will be recreated from the saved recipe.")
        mem = meminfo()
        job.say(f"Host memory: {mem['available']:.0f} GiB available of {mem['total']:.0f} GiB.")

    def start_model(self, key: str, job: Job):
        m = self.models[key]
        timeout = m.get("start_timeout_s", 1800)
        if m["kind"] == "sglang-docker":
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
        elif m["kind"] == "flash-vllm":
            home = self._checkpoint_home(m) or "/root"
            env = {**os.environ, "HOME": home,
                   "PORT": str(self.cfg["api_port"]), "BIND": "127.0.0.1",
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

    def _wait_healthy(self, key: str, job: Job, timeout: float, proc, logfile: Path | None):
        m = self.models[key]
        t0 = time.time()
        pos = 0
        last_beat = t0
        seen_container = False
        while True:
            # stream the launcher's output (Flash)
            if logfile and logfile.exists():
                with open(logfile, errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                if chunk:
                    job.say(chunk)
            s = self.served()
            if s and s["id"] == m["served_id"]:
                job.say(f"{m['title']} is serving (context {s['context']:,} tokens) after {int(time.time()-t0)}s.")
                if proc is not None and proc.poll() is None:
                    # start.sh prints its KV summary right after /health; give it a moment.
                    try:
                        proc.wait(timeout=20)
                    except subprocess.TimeoutExpired:
                        pass
                    if logfile:
                        with open(logfile, errors="replace") as fh:
                            fh.seek(pos)
                            job.say(fh.read())
                return
            running = m["container"] in self.running_containers()
            seen_container |= running
            if proc is not None and proc.poll() is not None and proc.returncode != 0:
                raise SwapFailed(f"{m['title']} launcher exited with code {proc.returncode}. See the log above.")
            if proc is None and seen_container and not running:
                _, logs = sh(["docker", "logs", "--tail", "25", m["container"]], timeout=20)
                job.say(logs)
                raise SwapFailed(f"{m['title']} container exited while loading.")
            if proc is None and not running and time.time() - t0 > 60:
                raise SwapFailed(f"{m['title']} container is not running.")
            if time.time() - t0 > timeout:
                if proc is not None and proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGTERM)
                raise SwapFailed(f"{m['title']} did not become ready within {int(timeout/60)} minutes.")
            if proc is None and time.time() - last_beat >= 30:
                last_beat = time.time()
                _, tail = sh(["docker", "logs", "--tail", "1", m["container"]], timeout=10)
                mem = meminfo()
                job.say(f"…still loading ({int(time.time()-t0)}s, {mem['available']:.0f} GiB free)"
                        + (f"  {tail[-160:]}" if tail else ""))
            time.sleep(3)

    def verify(self, key: str, job: Job):
        m = self.models[key]
        ng = self.cfg.get("nginx_port", 11443)
        d = http_json(f"https://127.0.0.1:{ng}/v1/models", self.key, timeout=8)
        if d and d.get("data") and d["data"][0].get("id") == m["served_id"]:
            job.say(f"nginx https://…:{ng}/v1 → {m['served_id']} ✓")
        else:
            job.say(f"Warning: nginx on :{ng} did not return {m['served_id']} (got {d}).")
        body = {"model": m["served_id"], "max_tokens": 24, "temperature": 0,
                "messages": [{"role": "user", "content": "Reply with exactly: ready"}],
                "chat_template_kwargs": {"enable_thinking": False}}
        t0 = time.time()
        r = http_json(f"http://127.0.0.1:{self.cfg['api_port']}/v1/chat/completions",
                      self.key, timeout=180, body=body)
        if r and r.get("choices"):
            txt = (r["choices"][0].get("message") or {}).get("content") or ""
            job.say(f"Test reply in {time.time()-t0:.1f}s: {txt.strip()[:80]!r}")
        else:
            raise SwapFailed("The model is up but did not answer a test prompt.")

    # ── the swap ──
    def begin_swap(self, target: str) -> Job:
        if target not in self.models:
            raise ValueError("unknown model")
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
            running = self.running_containers()
            others = [k for k, m in self.models.items() if k != target and m["container"] in running]
            if not others:
                job.say("Nothing else is running.")
            for k in others:
                self.stop_model(k, job)
            job.step(cur, "done" if others else "skipped")

            if already:
                job.say(f"{self.models[target]['title']} is already serving — skipping the load.")
                job.step("memory", "skipped"); job.step("start", "skipped")
            else:
                if self.models[target]["container"] in self.running_containers():
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
            if self.openclaw_primary() == self.openclaw_ref(target) and already:
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
            tm = self.models[job.target]
            if tm["container"] in self.running_containers():
                self.stop_model(job.target, job)
            self.wait_memory(source, job)
            self.start_model(source, job)
            if self.openclaw_primary() != self.openclaw_ref(source):
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
        own (restart=unless-stopped), so in practice this only restarts Flash."""
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
        if st["active"] or st["loading"] or port_open(self.cfg["api_port"]):
            return
        try:
            self.begin_swap(desired)
        except RuntimeError:
            pass

    # ── credentials ──
    def credentials(self) -> dict:
        host = self.cfg.get("public_host") or "127.0.0.1"
        ng = self.cfg.get("nginx_port", 11443)
        if DEMO:
            return {
                "openclaw": {"token": None, "password": None, "auth_mode": None,
                             "dashboard_url": f"https://{host}:{ng}/"},
                "api": {"key": self.key, "base_url": f"https://{host}:{ng}/v1",
                        "model": "qwen3.8-27b-sglang", "found_in": [], "other_keys": {},
                        "openclaw_provider_keys": []},
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
        host = self.cfg.get("public_host") or "127.0.0.1"
        ng = self.cfg.get("nginx_port", 11443)
        return {
            "openclaw": {
                "token": token,
                "password": password,
                "auth_mode": auth.get("mode"),
                "dashboard_url": f"https://{host}:{ng}/" + (f"#token={token}" if token else ""),
            },
            "api": {
                "key": api_key,
                "base_url": f"https://{host}:{ng}/v1",
                "model": self.status()["models"].get(self.status()["active"] or "", {}).get("served_id"),
                "found_in": sorted(set(found_keys.get(api_key, []))),
                "other_keys": {k: sorted(set(v)) for k, v in found_keys.items() if k != api_key},
                "openclaw_provider_keys": sorted({k for k in provider_keys.values()}),
            },
        }
