"""Keeping Spark Swapper current.

Once a week — on Sunday, at a minute picked once per box after 03:00 local
time (so boxes don't all knock on GitHub at once) — the Swapper asks GitHub
for its latest release. When there's a newer one it installs it, unless an
admin turned that off on the Settings tab; then it only says an update is
available. "Check now" and "Update now" do the same on demand.

How an update runs:
  1. Download the release's spark-swapper-<version>.tar.gz and its SHA256SUMS,
     and check the checksum. Unpack it under /var/lib/spark-swapper/updates/.
     If this installed copy ships a release-signers file, the package must also
     carry an SSH signature (spark-swapper-<version>.tar.gz.sig) from one of the
     keys listed there, checked with `ssh-keygen -Y verify` — see
     deploy/sign-release.sh. Without that file the checksum is the only check.
  2. Compile-check the new Python files before anything is replaced.
  3. Hand over to deploy/update.sh *from the new release*, started with
     systemd-run so it lives outside this service (it restarts the service).
     It copies /opt/spark-swapper aside, runs the new deploy/install.sh, waits
     for the page to answer, and puts the copy back if it doesn't.
  4. The restarted service reads update-result.json and shows how it went.
Models keep serving throughout: only the web service restarts.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import fetch
import hardware
from engine import DEMO, ROOT, sh

DEFAULT_REPO = "gnubyte/DGX-Spark-Swapper"
SETTINGS = {"check": True, "auto_install": True, "hour": 3}
SIGNERS = ROOT / "release-signers"
SIG_NAMESPACE = "spark-swapper-release"
STALE_AFTER = 1800   # a "running" update that hasn't reported back after 30 min failed


def verify_signature(archive: Path, signature: Path, signers: Path = SIGNERS) -> tuple[bool, str]:
    """ssh-keygen -Y verify against the allowed-signers file shipped with this copy."""
    if not shutil.which("ssh-keygen"):
        return False, "ssh-keygen isn't installed, so the release's signature can't be checked."
    with open(archive, "rb") as fh:
        p = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(signers), "-I", SIG_NAMESPACE,
                            "-n", SIG_NAMESPACE, "-s", str(signature)], stdin=fh, capture_output=True, text=True)
    return p.returncode == 0, (p.stdout + p.stderr).strip()


def parse_version(v: str | None) -> tuple[int, int, int]:
    nums = re.findall(r"\d+", (v or "").lstrip("vV").split("-")[0].split("+")[0])[:3]
    nums += ["0"] * (3 - len(nums))
    return tuple(int(n) for n in nums)  # type: ignore[return-value]


def newer(candidate: str | None, current: str | None) -> bool:
    return bool(candidate) and parse_version(candidate) > parse_version(current)


def next_slot(now: datetime, hour: int, minute: int) -> datetime:
    """The next Sunday hour:minute at or after `now`."""
    days = (6 - now.weekday()) % 7
    slot = (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot < now:
        slot += timedelta(days=7)
    return slot


def due(now: datetime, hour: int, minute: int, last_auto_check: float | None) -> bool:
    """Sunday, past the box's slot, and not yet checked since that slot."""
    if now.weekday() != 6:
        return False
    slot = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < slot:
        return False
    return not last_auto_check or datetime.fromtimestamp(last_auto_check) < slot


class Updater:
    def __init__(self, engine, installer=None, app_dir: Path = Path("/opt/spark-swapper")):
        self.engine = engine
        self.installer = installer
        self.repo = os.environ.get("SWAPPER_UPDATE_REPO") or engine.cfg.get("update_repo") or DEFAULT_REPO
        self.app_dir = app_dir
        self.busy = False
        self._lock = threading.Lock()
        self.log: deque[tuple[float, str]] = deque(maxlen=400)

    @property
    def result_file(self) -> Path:
        return self.engine.state_dir / "update-result.json"

    @property
    def log_file(self) -> Path:
        return self.engine.state_dir / "update.log"

    def say(self, msg: str):
        for line in (msg or "").rstrip().splitlines():
            self.log.append((time.time(), line))

    # ── settings ──
    def _state(self) -> dict:
        return self.engine.load_state().get("updates") or {}

    def settings(self) -> dict:
        st = self._state()
        out = {k: st.get(k, v) for k, v in SETTINGS.items()}
        if "minute" not in st:
            minute = random.randrange(60)
            self.engine.update_state("updates", lambda cur: {**(cur or {}), "minute": minute})
            st["minute"] = minute
        out["minute"] = st["minute"]
        return out

    def set_settings(self, check: bool | None = None, auto_install: bool | None = None):
        def put(cur):
            cur = dict(cur or {})
            if check is not None:
                cur["check"] = bool(check)
            if auto_install is not None:
                cur["auto_install"] = bool(auto_install)
            return cur
        self.engine.update_state("updates", put)

    def can_install(self) -> tuple[bool, str]:
        if DEMO:
            return True, ""
        if hardware.in_container():
            return False, "Spark Swapper runs in a container here: update it by pulling a newer image (pull-docker.sh)."
        if ROOT != self.app_dir.resolve():
            return False, f"This copy runs from {ROOT}, not the installed service in {self.app_dir}."
        if os.geteuid() != 0:
            return False, "Updating needs the service to run as root."
        if not shutil.which("systemd-run"):
            return False, "systemd-run isn't available."
        return True, ""

    def read_result(self) -> dict | None:
        try:
            r = json.loads(self.result_file.read_text())
        except (OSError, ValueError):
            return None
        if r.get("state") == "running" and time.time() - (r.get("started") or 0) > STALE_AFTER:
            r = {**r, "state": "failed", "message": f"The installer didn't report back. See {self.log_file}."}
        return r

    def info(self) -> dict:
        st = self._state()
        s = self.settings()
        ok, why = self.can_install()
        nxt = next_slot(datetime.now(), s["hour"], s["minute"])
        try:
            log_tail = self.log_file.read_text(errors="replace").splitlines()[-120:]
        except OSError:
            log_tail = []
        return {
            "current": self.engine.version,
            "repo": self.repo,
            "latest": st.get("latest"),
            "latest_url": st.get("latest_url"),
            "notes": st.get("notes"),
            "published": st.get("published"),
            "available": newer(st.get("latest"), self.engine.version),
            "last_check": st.get("last_check"),
            "last_error": st.get("last_error"),
            "check": s["check"],
            "auto_install": s["auto_install"],
            "next_check": nxt.timestamp() if s["check"] else None,
            "can_install": ok,
            "why_not": why,
            "busy": self.busy,
            "log": [{"t": t, "m": m} for t, m in list(self.log)[-200:]],
            "last_result": self.read_result(),
            "last_log": log_tail,
            "signed_only": SIGNERS.exists(),
        }

    # ── the weekly schedule ──
    def loop(self):
        time.sleep(90)
        while True:
            try:
                self.tick(datetime.now())
            except Exception as e:  # noqa: BLE001 — the loop must survive anything
                self.say(f"Update check failed: {e}")
            time.sleep(300)

    def tick(self, now: datetime):
        s = self.settings()
        st = self._state()
        if not s["check"] or time.time() < (st.get("retry_after") or 0):
            return
        if not due(now, s["hour"], s["minute"], st.get("last_auto_check")):
            return
        info = self.check(auto=True)
        if info.get("available") and s["auto_install"]:
            ok, why = self.can_install()
            if not ok:
                self.say(f"Version {info['latest']} is out, but it can't be installed automatically: {why}")
                return
            if self._busy_elsewhere():
                self.say("An update is waiting for the current swap or install to finish.")
                self.engine.update_state("updates", lambda cur: {**(cur or {}), "last_auto_check": None})
                return
            self.install()

    def _busy_elsewhere(self) -> bool:
        jobs = [self.engine.job, getattr(self.installer, "job", None)]
        return any(j is not None and j.state == "running" for j in jobs)

    # ── checking ──
    def check(self, auto: bool = False) -> dict:
        now = time.time()
        if DEMO:
            cur = self.engine.version
            major, minor, _ = parse_version(cur)
            latest = f"{major}.{minor + 1}.0"
            self.engine.update_state("updates", lambda c: {**(c or {}), "latest": latest,
                                                           "latest_url": f"https://github.com/{self.repo}/releases",
                                                           "notes": "Demo: no real release was fetched.",
                                                           "published": datetime.now().isoformat(),
                                                           "last_check": now, "last_error": None,
                                                           **({"last_auto_check": now} if auto else {})})
            return self.info()
        self.say(f"Asking GitHub for the latest release of {self.repo}…")
        try:
            rel = fetch.gh_latest_release(self.repo)
        except fetch.FetchError as e:
            msg = str(e)
            if "404" in msg:
                msg = "No releases are published yet."
            self.engine.update_state("updates", lambda c: {**(c or {}), "last_check": now, "last_error": msg,
                                                           "retry_after": now + 3600})
            self.say(msg)
            return self.info()
        tag = rel.get("tag_name") or ""
        ver = tag.lstrip("vV")
        assets = {a["name"]: a for a in rel.get("assets") or []}
        tarball = assets.get(f"spark-swapper-{ver}.tar.gz")
        sums = assets.get("SHA256SUMS")
        sig = assets.get(f"spark-swapper-{ver}.tar.gz.sig")

        def put(c):
            c = dict(c or {})
            c.update({"latest": ver, "latest_tag": tag, "latest_url": rel.get("html_url"),
                      "notes": (rel.get("body") or "")[:6000], "published": rel.get("published_at"),
                      "last_check": now, "last_error": None, "retry_after": None,
                      "asset": {"url": tarball["browser_download_url"], "size": tarball.get("size"),
                                "sums": sums["browser_download_url"] if sums else None,
                                "sig": sig["browser_download_url"] if sig else None} if tarball else None})
            if auto:
                c["last_auto_check"] = now
            return c
        self.engine.update_state("updates", put)
        if newer(ver, self.engine.version):
            self.say(f"Version {ver} is available (this is {self.engine.version}).")
        else:
            self.say(f"Up to date ({self.engine.version}; the latest release is {ver or 'unknown'}).")
        return self.info()

    # ── installing ──
    def install(self):
        with self._lock:
            if self.busy or self.engine.updating():
                raise RuntimeError("An update is already running.")
            ok, why = self.can_install()
            if not ok:
                raise RuntimeError(why)
            if self._busy_elsewhere():
                raise RuntimeError("Wait for the current swap or install to finish, then try again.")
            st = self._state()
            if not newer(st.get("latest"), self.engine.version):
                raise RuntimeError("Spark Swapper is already up to date.")
            if not DEMO and not st.get("asset"):
                raise RuntimeError(f"Release {st.get('latest')} has no installable package "
                                   f"(spark-swapper-{st.get('latest')}.tar.gz).")
            self.busy = True
        threading.Thread(target=self._install, args=(st,), daemon=True).start()

    def _install(self, st: dict):
        ver = st["latest"]
        try:
            if DEMO:
                for line in (f"Downloading spark-swapper-{ver}.tar.gz…", "Checksum OK.",
                             "Demo mode: nothing is installed."):
                    self.say(line)
                    time.sleep(1)
                return
            asset = st["asset"]
            name = f"spark-swapper-{ver}.tar.gz"
            sha = None
            if asset.get("sums"):
                with urllib.request.urlopen(fetch._request(asset["sums"]), timeout=30) as r:
                    for line in r.read().decode(errors="replace").splitlines():
                        parts = line.split()
                        if len(parts) == 2 and parts[1].lstrip("*") == name:
                            sha = parts[0]
            if not sha:
                raise fetch.FetchError("The release publishes no checksum for its package, so it won't be installed.")
            downloads = self.engine.state_dir / "downloads"
            archive = fetch.download(asset["url"], downloads / name, size=asset.get("size"), sha256=sha,
                                     say=self.say, label=name)
            if SIGNERS.exists() and not asset.get("sig"):
                # The signature may have been uploaded after the last check.
                self.check()
                asset = self._state().get("asset") or asset
            if SIGNERS.exists():
                if not asset.get("sig"):
                    raise fetch.FetchError(f"This box only installs signed releases, and {ver} isn't signed.")
                sig = fetch.download(asset["sig"], downloads / f"{name}.sig", say=self.say, label=f"{name}.sig")
                ok, detail = verify_signature(archive, sig)
                sig.unlink(missing_ok=True)
                if not ok:
                    raise fetch.FetchError(f"The release's signature didn't check out: {detail[-300:]}")
                self.say("Signature OK.")
            # A fresh directory per attempt: an earlier installer may still be reading the last one.
            updates = self.engine.state_dir / "updates"
            staging = updates / f"{ver}-{int(time.time())}"
            for old in sorted(updates.glob("*"), key=lambda q: q.stat().st_mtime)[:-2] if updates.exists() else []:
                shutil.rmtree(old, ignore_errors=True)
            fetch.safe_extract(archive, staging)
            roots = [p for p in staging.iterdir() if p.is_dir()]
            new_root = roots[0] if len(roots) == 1 else staging
            if not (new_root / "swapper" / "app.py").is_file() or not (new_root / "deploy" / "update.sh").is_file():
                raise fetch.FetchError("The package doesn't look like a Spark Swapper release.")
            shipped = (new_root / "swapper" / "VERSION").read_text().strip() if (new_root / "swapper" / "VERSION").exists() else ""
            if shipped != ver:
                raise fetch.FetchError(f"The package says it is version {shipped or '?'}, not {ver}.")
            rc = subprocess.run([sys.executable, "-m", "py_compile", *map(str, (new_root / "swapper").glob("*.py"))],
                                capture_output=True, text=True)
            if rc.returncode != 0:
                raise fetch.FetchError(f"The new version doesn't compile: {rc.stderr[-300:]}")
            self.result_file.write_text(json.dumps({"state": "running", "from": self.engine.version, "to": ver,
                                                    "started": time.time()}))
            self.say("Handing over to the installer. The page restarts in a moment; models keep serving.")
            unit = f"spark-swapper-update-{int(time.time())}"
            env_args = [f"--setenv=SWAPPER_STATE={self.engine.state_dir}",
                        f"--setenv=SWAPPER_PORT={os.environ.get('SWAPPER_PORT', '8999')}"]
            rc2, out = sh(["systemd-run", f"--unit={unit}", "--collect", "--quiet",
                           "--description", f"Spark Swapper update to {ver}", *env_args,
                           "/bin/bash", str(new_root / "deploy" / "update.sh"), str(new_root)], timeout=30)
            if rc2 != 0:
                raise fetch.FetchError(f"Couldn't start the installer: {out[-300:]}")
            archive.unlink(missing_ok=True)
        except Exception as e:  # noqa: BLE001
            self.say(f"✗ Update failed: {e}")
            self.result_file.write_text(json.dumps({"state": "failed", "from": self.engine.version, "to": ver,
                                                    "finished": time.time(), "message": str(e)}))
        finally:
            self.busy = False
