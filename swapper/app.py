#!/usr/bin/env python3
"""Spark Swapper — a calm, one-click model switcher for the DGX Spark.

HTTPS on :8999 with a self-signed cert. Multi-user: the first visitor creates
the admin login (one time only); admin can add/remove other users and reset
their passwords. Every user can see the running model, swap it, and grab
connection credentials. Standard library only, so it runs on the stock
Ubuntu python3 with nothing to install.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine  # noqa: E402
from engine import Engine  # noqa: E402
from installer import Installer  # noqa: E402

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
CONF_DIR = Path(os.environ.get("SWAPPER_CONF", "/etc/spark-swapper"))
STATE_DIR = Path(os.environ.get("SWAPPER_STATE", "/var/lib/spark-swapper"))
PORT = int(os.environ.get("SWAPPER_PORT", "8999"))
BIND = os.environ.get("SWAPPER_BIND", "0.0.0.0")
DEMO = os.environ.get("SWAPPER_DEMO", "") == "1"
SESSION_DAYS = 30

CERT = CONF_DIR / "tls.crt"
KEY = CONF_DIR / "tls.key"

# In-memory (demo) or on-disk auth database: {username: {salt, hash, role, created}}
_users: dict[str, dict] | None = None
_users_lock = threading.RLock()   # reentrant: mutators hold it, then _load_users() re-acquires
_auth_lock = threading.Lock()


# ─────────────────────────────── auth (multi-user) ────────────────────────

def _secret() -> bytes:
    if not SECRET_FILE.exists():
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
        SECRET_FILE.chmod(0o600)
    return SECRET_FILE.read_bytes()


SECRET_FILE = CONF_DIR / "secret.key"
AUTH_FILE = CONF_DIR / "users.json"


def _hash(password: str, salt: bytes) -> str:
    try:
        return hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1,
                              maxmem=64 * 1024 * 1024, dklen=32).hex()
    except (AttributeError, ValueError):
        # Some libressl/OpenSSL builds lack scrypt; PBKDF2-HMAC-SHA256 is a
        # solid fallback. (The two are never mixed for one password.)
        return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000, dklen=32).hex()


def _load_users() -> dict:
    global _users
    with _users_lock:
        if _users is not None:
            return _users
        if DEMO:
            salt = secrets.token_bytes(16)
            _users = {"admin": {"salt": salt.hex(), "hash": _hash("admin", salt),
                                "role": "admin", "created": time.time()}}
            return _users
        try:
            _users = json.loads(AUTH_FILE.read_text())
        except (OSError, ValueError):
            _users = {}
        return _users


def _save_users():
    if DEMO:
        return
    tmp = AUTH_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(_users))
    tmp.chmod(0o600)
    tmp.replace(AUTH_FILE)


def any_user() -> bool:
    return bool(_load_users())


def get_user(username: str) -> dict | None:
    return _load_users().get(username)


def user_role(username: str) -> str:
    u = get_user(username)
    return u["role"] if u else ""


def add_user(username: str, password: str, role: str = "user") -> None:
    salt = secrets.token_bytes(16)
    with _users_lock:
        _load_users()[username] = {"salt": salt.hex(), "hash": _hash(password, salt),
                                   "role": role, "created": time.time()}
        _save_users()


def remove_user(username: str) -> None:
    with _users_lock:
        _load_users().pop(username, None)
        _save_users()


def set_password(username: str, password: str) -> None:
    salt = secrets.token_bytes(16)
    with _users_lock:
        u = _load_users().get(username)
        if not u:
            return
        u["salt"] = salt.hex()
        u["hash"] = _hash(password, salt)
        u["epoch"] = u.get("epoch", 1) + 1
        _save_users()


def check_password(username: str, password: str) -> bool:
    u = _load_users().get(username)
    if not u:
        # constant-ish time: still hash to prevent user-enumeration timing
        _hash(password, b"0" * 16)
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"])


def list_users() -> list[dict]:
    out = []
    for name, u in _load_users().items():
        out.append({"username": name, "role": u.get("role", "user"),
                    "created": u.get("created", 0)})
    out.sort(key=lambda x: (x["role"] != "admin", x["username"]))
    return out


def make_session(username: str) -> str:
    u = _load_users().get(username) or {}
    exp = int(time.time()) + SESSION_DAYS * 86400
    payload = f"{username}|{u.get('role', 'user')}|{u.get('epoch', 1)}|{exp}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()


def read_session(cookie: str | None) -> dict | None:
    """Return {username, role} or None."""
    if not cookie:
        return None
    try:
        raw = base64.urlsafe_b64decode(cookie.encode()).decode()
        username, role, epoch, exp, sig = raw.rsplit("|", 4)
    except (ValueError, UnicodeDecodeError):
        return None
    payload = f"{username}|{role}|{epoch}|{exp}"
    good = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return None
    if int(exp) < time.time():
        return None
    u = get_user(username)
    if not u:
        return None
    if str(u.get("epoch", 1)) != epoch:
        return None
    return {"username": username, "role": u.get("role", "user")}


class Throttle:
    """Slow down password guessing: after 5 misses from an address, lock it
    out for a doubling interval (30s, 60s, 120s … capped at 1h)."""

    def __init__(self):
        self.fails: dict[str, tuple[int, float]] = {}
        self.lock = threading.Lock()

    def wait_for(self, ip: str) -> int:
        with self.lock:
            n, until = self.fails.get(ip, (0, 0))
            return max(0, int(until - time.time()))

    def fail(self, ip: str):
        with self.lock:
            n, _ = self.fails.get(ip, (0, 0))
            n += 1
            until = time.time() + min(3600, 30 * 2 ** (n - 5)) if n >= 5 else 0
            self.fails[ip] = (n, until)

    def ok(self, ip: str):
        with self.lock:
            self.fails.pop(ip, None)


throttle = Throttle()
setup_lock = threading.Lock()


# ─────────────────────────────── http ────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "SparkSwapper/1.1"
    timeout = 30
    sys_version = ""
    engine: Engine
    installer: Installer

    def log_message(self, fmt, *args):  # quieter journal
        if not self.path.startswith("/api/status"):
            sys.stderr.write(f"{self.client_address[0]} {fmt % args}\n")

    # ── plumbing ──
    def _headers(self, code: int, ctype: str, length: int, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _json(self, code: int, obj, extra: dict | None = None):
        body = json.dumps(obj).encode()
        self._headers(code, "application/json", len(body), extra)
        self.wfile.write(body)

    def _cookie(self) -> str | None:
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "swapper_session":
                return v
        return None

    def _user(self) -> dict | None:
        return read_session(self._cookie())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return {}

    def _same_origin(self) -> bool:
        # Every state change is a JSON POST carrying our custom header, which a
        # cross-site form cannot send; the cookie is also SameSite=Strict.
        if self.headers.get("X-Swapper") != "1":
            return False
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != (self.headers.get("Host") or ""):
            return False
        return True

    def _set_session(self, username: str) -> dict:
        tok = make_session(username)
        return {"Set-Cookie": f"swapper_session={tok}; Path=/; HttpOnly; Secure; SameSite=Strict; "
                              f"Max-Age={SESSION_DAYS * 86400}"}

    def _logout_cookie(self) -> dict:
        return {"Set-Cookie": "swapper_session=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0"}

    # ── routes ──
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/cert.crt":
            # Self-signed certificate download, so users can add it to their
            # trust store (browser, macOS Keychain, Windows, or AWS EC2).
            try:
                data = CERT.read_bytes()
            except OSError:
                return self._json(404, {"error": "No certificate yet."})
            self._headers(200, "application/x-pem-file", len(data),
                          {"Content-Disposition": 'attachment; filename="spark-swapper.crt"'})
            self.wfile.write(data)
            return
        if path == "/api/session":
            u = self._user()
            return self._json(200, {
                "setup_needed": not any_user(),
                "user": u["username"] if u else None,
                "role": u["role"] if u else None,
                "demo": DEMO,
            })
        if not self._user():
            return self._json(401, {"error": "Please sign in."})
        if path == "/api/status":
            st = self.engine.status()
            job = self.engine.job
            return self._json(200, {**st, "job": job.to_dict() if job else None})
        if path == "/api/credentials":
            return self._json(200, self.engine.credentials())
        if path == "/api/provisioning":
            return self._json(200, self.installer.report())
        if path == "/api/provisioning/job":
            job = self.installer.job
            return self._json(200, {"job": job.to_dict() if job else None})
        if path == "/api/users":
            if user_role(self._user()["username"]) == "admin":
                return self._json(200, {"users": list_users()})
            return self._json(403, {"error": "Admin only."})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if not self._same_origin():
            return self._json(403, {"error": "Cross-site request refused."})
        ip = self.client_address[0]
        body = self._body()

        if path == "/api/setup":
            with setup_lock:
                if any_user():
                    return self._json(409, {"error": "The admin login already exists."})
                u = str(body.get("username", "")).strip()
                p = str(body.get("password", ""))
                if not (1 <= len(u) <= 64) or any(c in u for c in "|\n\r"):
                    return self._json(400, {"error": "Choose a username (letters, numbers, up to 64 characters)."})
                if len(p) < 8:
                    return self._json(400, {"error": "Use a password of at least 8 characters."})
                add_user(u, p, role="admin")
            return self._json(200, {"ok": True, "user": u}, self._set_session(u))

        if path == "/api/login":
            wait = throttle.wait_for(ip)
            if wait:
                return self._json(429, {"error": f"Too many attempts. Try again in {wait}s."})
            u = str(body.get("username", "")).strip()
            p = str(body.get("password", ""))
            if u and check_password(u, p):
                throttle.ok(ip)
                return self._json(200, {"ok": True, "user": u, "role": user_role(u)}, self._set_session(u))
            throttle.fail(ip)
            time.sleep(0.6)
            return self._json(401, {"error": "That username and password don't match."})

        if path == "/api/logout":
            return self._json(200, {"ok": True}, self._logout_cookie())

        user = self._user()
        if not user:
            return self._json(401, {"error": "Please sign in."})
        me, role = user["username"], user["role"]

        if path == "/api/swap":
            target = str(body.get("target", ""))
            try:
                job = self.engine.begin_swap(target)
            except ValueError:
                return self._json(400, {"error": "Unknown model."})
            except RuntimeError as e:
                return self._json(409, {"error": str(e)})
            return self._json(202, {"ok": True, "job": job.to_dict()})

        if path == "/api/install":
            action = str(body.get("action", ""))
            if action not in self.installer.ACTIONS:
                return self._json(400, {"error": "Unknown action."})
            try:
                job = self.installer.begin(action)
            except RuntimeError as e:
                return self._json(409, {"error": str(e)})
            return self._json(202, {"ok": True, "job": job.to_dict()})

        if path == "/api/password":
            if not check_password(me, str(body.get("current", ""))):
                return self._json(401, {"error": "Current password is wrong."})
            new = str(body.get("new", ""))
            if len(new) < 8:
                return self._json(400, {"error": "Use a password of at least 8 characters."})
            set_password(me, new)
            return self._json(200, {"ok": True}, self._set_session(me))

        # ── admin-only ──
        if role != "admin":
            return self._json(403, {"error": "Admin only."})

        if path == "/api/users/add":
            with _auth_lock:
                u = str(body.get("username", "")).strip()
                p = str(body.get("password", ""))
                r = "admin" if str(body.get("role", "")) == "admin" else "user"
                if not (1 <= len(u) <= 64) or any(c in u for c in "|\n\r"):
                    return self._json(400, {"error": "Choose a username (letters, numbers, up to 64 characters)."})
                if get_user(u):
                    return self._json(409, {"error": f"{u!r} already exists."})
                if len(p) < 8:
                    return self._json(400, {"error": "Use a password of at least 8 characters."})
                add_user(u, p, r)
            return self._json(201, {"ok": True, "user": u}, self._set_session(u))

        if path == "/api/users/reset":
            with _auth_lock:
                u = str(body.get("username", "")).strip()
                p = str(body.get("password", ""))
                if not get_user(u):
                    return self._json(404, {"error": f"{u!r} not found."})
                if len(p) < 8:
                    return self._json(400, {"error": "Use a password of at least 8 characters."})
                set_password(u, p)
            return self._json(200, {"ok": True, "user": u})

        if path == "/api/users/remove":
            with _auth_lock:
                u = str(body.get("username", "")).strip()
                if u == me:
                    return self._json(400, {"error": "You cannot remove yourself."})
                target = get_user(u)
                admins = [x for x in list_users() if x["role"] == "admin"]
                if not target:
                    return self._json(404, {"error": f"{u!r} not found."})
                if target["role"] == "admin" and len(admins) <= 1:
                    return self._json(400, {"error": "Cannot remove the last admin."})
                remove_user(u)
            return self._json(200, {"ok": True, "user": u})

        return self._json(404, {"error": "not found"})

    def _static(self, name: str):
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            return self._json(404, {"error": "not found"})
        data = f.read_bytes()
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._headers(200, ctype, len(data))
        self.wfile.write(data)


class TLSServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        # Browsers probing a self-signed cert drop connections mid-handshake; not worth a traceback.
        exc = sys.exc_info()[1]
        if isinstance(exc, (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def _make_httpd():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        try:
            os.chmod(CONF_DIR, 0o700)
        except OSError:
            pass
    if not (CERT.exists() and KEY.exists()):
        _gen_cert()
    cfg = CONF_DIR / "models.json"
    if not cfg.exists():
        cfg = ROOT / "models.json"
    eng = Engine(cfg, STATE_DIR)
    ins = Installer(eng)
    Handler.engine = eng
    Handler.installer = ins
    threading.Thread(target=eng.reconcile_at_boot, daemon=True).start()

    httpd = TLSServer((BIND, PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    # Handshake lazily in the worker thread so a slow client can't stall accept().
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd, eng


def _gen_cert():
    """Generate a self-signed cert if none exists (10 years, with SANs)."""
    import subprocess
    host = os.uname().nodename
    san = [f"DNS:localhost", f"DNS:{host}", "IP:127.0.0.1"]
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        for ip in out.stdout.split():
            if ":" not in ip:
                san.append(f"IP:{ip}")
    except (OSError, subprocess.SubprocessError):
        pass
    cmd = ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650",
           "-keyout", str(KEY), "-out", str(CERT),
           "-subj", f"/CN=Spark Swapper ({host})",
           "-addext", f"subjectAltName={','.join(san)}"]
    subprocess.run(cmd, check=False, capture_output=True)
    try:
        KEY.chmod(0o600)
    except OSError:
        pass
    print(f"Generated self-signed cert at {CERT}", flush=True)


def _demo_config(base: Path) -> Path:
    """Demo mode runs off the template with placeholders filled, so the UI
    shows a coherent (if synthetic) host."""
    import tempfile
    raw = base.read_text()
    ip = "127.0.0.1"
    tmp = Path(tempfile.mkdtemp(prefix="swapper-demo-"))
    cfg = tmp / "models.json"
    cfg.write_text(raw.replace("{{USER}}", "demo")
                   .replace("{{PUBLIC_HOST}}", ip)
                   .replace("{{OPENCLAW_BIN}}", "/usr/local/bin/openclaw"))
    return cfg


def _make_httpd():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    if os.name == "posix" and not DEMO:
        try:
            os.chmod(CONF_DIR, 0o700)
        except OSError:
            pass
    if not (CERT.exists() and KEY.exists()):
        _gen_cert()
    cfg = CONF_DIR / "models.json"
    if not cfg.exists():
        cfg = ROOT / "models.json"
    if DEMO:
        cfg = _demo_config(cfg)
    eng = Engine(cfg, STATE_DIR)
    ins = Installer(eng)
    Handler.engine = eng
    Handler.installer = ins
    if not DEMO:
        threading.Thread(target=eng.reconcile_at_boot, daemon=True).start()

    httpd = TLSServer((BIND, PORT), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(CERT, KEY)
    # Handshake lazily in the worker thread so a slow client can't stall accept().
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
    return httpd, eng


def main():
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    httpd, eng = _make_httpd()
    print(f"Spark Swapper listening on https://{BIND}:{PORT}  (config {eng.config_path}, demo={DEMO})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
