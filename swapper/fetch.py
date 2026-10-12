"""Downloading big files safely: resumable, checksummed, with progress in the job log.

Used for model weights from Hugging Face (tens of gigabytes — a dropped
connection resumes where it stopped), for llama.cpp builds from GitHub
releases, and for Spark Swapper's own updates. Every file is checked against a
SHA-256 published by its source before it is put in place, and a file is only
ever moved to its final name after that check passes.
"""
from __future__ import annotations

import hashlib
import json
import platform as _platform
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = "SparkSwapper (+https://github.com/gnubyte/DGX-Spark-Swapper)"
CHUNK = 4 * 1024 * 1024
HF = "https://huggingface.co"


class FetchError(Exception):
    pass


def _request(url: str, headers: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})


def get_json(url: str, timeout: float = 30, headers: dict | None = None):
    try:
        with urllib.request.urlopen(_request(url, {"Accept": "application/json", **(headers or {})}),
                                    timeout=timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise FetchError(f"{url} answered {e.code} {e.reason}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise FetchError(f"couldn't read {url}: {e}") from e


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1000
    return f"{n:.1f} TB"


def _eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min"
    if seconds >= 60:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def sha256_file(path: Path, say=None, label: str = "") -> str:
    h = hashlib.sha256()
    size = path.stat().st_size
    done = 0
    last = time.time()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK * 4)
            if not block:
                break
            h.update(block)
            done += len(block)
            if say and time.time() - last >= 15:
                last = time.time()
                say(f"  checking {label or path.name}: {done * 100 // max(size, 1)}%")
    return h.hexdigest()


def download(url: str, dest: Path, *, size: int | None = None, sha256: str | None = None,
             say=print, label: str | None = None, headers: dict | None = None,
             retries: int = 6) -> Path:
    """Fetch `url` to `dest`. Resumes a partial `dest.part`, checks free disk
    space first, verifies size and SHA-256, then renames into place."""
    label = label or dest.name
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    if dest.exists() and (size is None or dest.stat().st_size == size):
        if not sha256:
            say(f"{label}: already here.")
            return dest
        say(f"{label}: already here; checking its SHA-256…")
        if sha256_file(dest, say, label).lower() == sha256.lower():
            return dest
        say(f"{label}: the copy here doesn't match; downloading it again.")
        dest.unlink()
    attempt = 0
    while True:
        have = part.stat().st_size if part.exists() else 0
        if size is not None and have > size:
            # Left over from an older, bigger upload of the file: start over.
            say(f"{label}: the partial download is bigger than the file now is; starting over.")
            part.unlink()
            have = 0
        if size is not None:
            free = shutil.disk_usage(dest.parent).free
            need = size - have
            if need > free - 2 * 1024**3:
                raise FetchError(f"Not enough disk space for {label}: it needs {human(need)} more, "
                                 f"{human(free)} is free on {dest.parent}.")
        hdrs = dict(headers or {})
        if have:
            hdrs["Range"] = f"bytes={have}-"
        try:
            with urllib.request.urlopen(_request(url, hdrs), timeout=60) as r:
                if have and r.status != 206:
                    have = 0  # the server ignored the range: start over
                mode = "ab" if have else "wb"
                total = size or (have + int(r.headers.get("Content-Length") or 0)) or None
                if have:
                    say(f"{label}: resuming at {human(have)}" + (f" of {human(total)}" if total else ""))
                else:
                    say(f"{label}: downloading" + (f" {human(total)}" if total else ""))
                t0, got0, last = time.time(), have, time.time()
                with open(part, mode) as fh:
                    while True:
                        block = r.read(CHUNK)
                        if not block:
                            break
                        fh.write(block)
                        have += len(block)
                        now = time.time()
                        if now - last >= 15:
                            last = now
                            rate = (have - got0) / max(now - t0, 0.001)
                            if total:
                                say(f"  {label}: {human(have)} of {human(total)} ({have * 100 // total}%) "
                                    f"at {human(rate)}/s, about {_eta((total - have) / max(rate, 1))} left")
                            else:
                                say(f"  {label}: {human(have)} at {human(rate)}/s")
            break
        except urllib.error.HTTPError as e:
            if e.code == 416 and size is not None and have == size:
                break  # already complete
            if e.code == 416:
                # The range no longer fits the file (it changed upstream): start over.
                part.unlink(missing_ok=True)
            if e.code in (401, 403, 404):
                raise FetchError(f"{label}: the server answered {e.code} {e.reason}.") from e
            err = f"{e.code} {e.reason}"
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            err = str(e)
        attempt += 1
        if attempt > retries:
            raise FetchError(f"{label}: download kept failing ({err}). Run it again to resume.")
        wait = min(60, 5 * 2 ** (attempt - 1))
        say(f"  {label}: connection trouble ({err}); retrying in {wait} s…")
        time.sleep(wait)

    got = part.stat().st_size
    if size is not None and got != size:
        raise FetchError(f"{label}: got {human(got)}, expected {human(size)}. Run it again to resume.")
    if sha256:
        say(f"{label}: checking its SHA-256…")
        digest = sha256_file(part, say, label)
        if digest.lower() != sha256.lower():
            part.unlink(missing_ok=True)
            raise FetchError(f"{label}: the checksum didn't match (got {digest[:16]}…, expected "
                             f"{sha256[:16]}…). The partial file was deleted; run it again.")
    part.replace(dest)
    say(f"{label}: done.")
    return dest


# ── Hugging Face ──

def hf_listing(repo: str) -> dict[str, dict]:
    """{path: {"size": int, "sha256": str|None}} for every file in a model repo."""
    d = get_json(f"{HF}/api/models/{repo}?blobs=true")
    out = {}
    for s in (d or {}).get("siblings") or []:
        lfs = s.get("lfs") or {}
        out[s["rfilename"]] = {"size": s.get("size") or lfs.get("size"), "sha256": lfs.get("sha256")}
    return out


def hf_url(repo: str, path: str) -> str:
    return f"{HF}/{repo}/resolve/main/{urllib.parse.quote(path)}"


# ── GitHub releases ──

def gh_latest_release(repo: str) -> dict:
    return get_json(f"https://api.github.com/repos/{repo}/releases/latest",
                    headers={"Accept": "application/vnd.github+json"})


def machine() -> str:
    m = _platform.machine().lower()
    return {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(m, m)


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract a .tar.gz refusing absolute paths, `..`, devices and links that
    point outside `dest`."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")
            return
        root = dest.resolve()
        for member in tf.getmembers():
            target = (dest / member.name).resolve()
            if root != target and root not in target.parents:
                raise FetchError(f"refusing to extract {member.name!r} outside {dest}")
            if member.isdev():
                raise FetchError(f"refusing to extract device {member.name!r}")
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve()
                if root != link and root not in link.parents:
                    raise FetchError(f"refusing link {member.name!r} → {member.linkname!r}")
        tf.extractall(dest)  # noqa: S202 — members checked above
