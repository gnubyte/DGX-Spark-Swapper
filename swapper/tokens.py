"""App connections: long-lived tokens that let DSH (or a script) use the API
without a password.

A token is `sst_<id>_<secret>`: the id names it, the secret proves it. Only a
SHA-256 of the secret is stored (tokens.json, 0600, next to users.json), so the
file can't be turned back into working tokens. A token acts as the user who
made it, except that it can't manage users, tokens, passwords or updates.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

TOKEN_RE = re.compile(r"^sst_([0-9a-f]{8})_([A-Za-z0-9_\-]{20,128})$")
CODE_PREFIX = "SSC1:"


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class TokenStore:
    def __init__(self, path: Path, persist: bool = True):
        self.path = Path(path)
        self.persist = persist
        self._lock = threading.RLock()
        self._tokens: dict[str, dict] | None = None

    def _load(self) -> dict[str, dict]:
        with self._lock:
            if self._tokens is None:
                try:
                    self._tokens = json.loads(self.path.read_text()) if self.persist else {}
                except (OSError, ValueError):
                    self._tokens = {}
            return self._tokens

    def _save(self):
        if not self.persist:
            return
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._tokens, indent=1))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def create(self, user: str, label: str) -> tuple[str, dict]:
        with self._lock:
            tokens = self._load()
            tid = secrets.token_hex(4)
            while tid in tokens:
                tid = secrets.token_hex(4)
            secret = secrets.token_urlsafe(32)
            info = {"user": user, "label": (label or "App").strip()[:80], "created": time.time(),
                    "last_used": None, "hash": _digest(secret)}
            tokens[tid] = info
            self._save()
            return f"sst_{tid}_{secret}", {"id": tid, **{k: v for k, v in info.items() if k != "hash"}}

    def verify(self, token: str | None) -> dict | None:
        """{"id", "user", "label"} for a valid token, else None."""
        m = TOKEN_RE.match((token or "").strip())
        if not m:
            return None
        tid, secret = m.groups()
        with self._lock:
            info = self._load().get(tid)
            if not info or not hmac.compare_digest(info["hash"], _digest(secret)):
                return None
            now = time.time()
            if not info.get("last_used") or now - info["last_used"] > 60:
                info["last_used"] = now
                self._save()
            return {"id": tid, "user": info["user"], "label": info.get("label")}

    def list(self, user: str | None = None) -> list[dict]:
        with self._lock:
            out = [{"id": tid, **{k: v for k, v in info.items() if k != "hash"}}
                   for tid, info in self._load().items() if user is None or info["user"] == user]
        out.sort(key=lambda t: t.get("created") or 0, reverse=True)
        return out

    def revoke(self, tid: str, user: str | None = None) -> bool:
        """Revoke a token; with `user`, only one of that user's own."""
        with self._lock:
            tokens = self._load()
            info = tokens.get(tid)
            if not info or (user is not None and info["user"] != user):
                return False
            del tokens[tid]
            self._save()
            return True

    def revoke_user(self, user: str):
        with self._lock:
            tokens = self._load()
            for tid in [t for t, i in tokens.items() if i["user"] == user]:
                del tokens[tid]
            self._save()


def connection_code(info: dict, token: str, user: str) -> str:
    """The text DSH's "Connect my Spark Swapper" accepts: SSC1: + base64url(JSON)."""
    payload = {"v": 1, **{k: info.get(k) for k in ("name", "hostname", "platform", "version", "swapper_url",
                                                  "api_base", "api_key", "model", "context", "fingerprint")},
               "token": token, "user": user}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_code(code: str) -> dict:
    """The inverse of connection_code (used by the tests; DSH has its own)."""
    text = "".join((code or "").split())
    if text.upper().startswith(CODE_PREFIX):
        text = text[len(CODE_PREFIX):]
    if text.startswith("{"):
        return json.loads(text)
    text = text.replace("+", "-").replace("/", "_")
    return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
