"""Downloading big files safely: resumable, checksummed, with progress in the job log.

Used for model weights from Hugging Face (tens of gigabytes — a dropped
connection resumes where it stopped), for llama.cpp builds from GitHub
releases, and for Spark Swapper's own updates. Every file is checked against a
SHA-256 published by its source before it is put in place, and a file is only
ever moved to its final name after that check passes.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import platform as _platform
import re
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
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _AuthRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects, but never carry credentials to another host: Hugging Face
    hands big files over to a CDN with a signed URL, which must not see the token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for name in ("Authorization", "Cookie"):
                new.headers.pop(name, None)
                new.unredirected_hdrs.pop(name, None)
        return new


_OPENER = urllib.request.build_opener(_AuthRedirect)


def _open(req: urllib.request.Request, timeout: float):
    return _OPENER.open(req, timeout=timeout)


def _request(url: str, headers: dict | None = None) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})


def get_json(url: str, timeout: float = 30, headers: dict | None = None):
    try:
        with _open(_request(url, {"Accept": "application/json", **(headers or {})}), timeout) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise FetchError(f"{url} answered {e.code} {e.reason}", status=e.code) from e
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
            with _open(_request(url, hdrs), 60) as r:
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
                raise FetchError(f"{label}: the server answered {e.code} {e.reason}.", status=e.code) from e
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

REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
# Charts and the like: not part of a model, and sometimes a good part of the repo's size.
DEFAULT_EXCLUDE = ("images/*", "assets/*", "*.png", "*.jpg", "*.jpeg", "*.gif")


def check_repo_id(repo: str) -> str:
    """The repo id is put into URLs, folder names and docker arguments: nothing odd gets through."""
    if not isinstance(repo, str) or not REPO_ID.match(repo) or ".." in repo:
        raise FetchError(f"{repo!r} isn't a Hugging Face model id (it looks like owner/name).")
    return repo


def parse_hf_ref(text: str) -> tuple[str, str | None]:
    """(repo, revision) from a Hugging Face link — huggingface.co/owner/name, .../tree/<rev>,
    .../blob/<rev>/file — or from "owner/name" or "owner/name@revision"."""
    s = (text or "").strip()
    rev = None
    m = re.match(r"^(?:https?://)?(?:www\.)?(?:huggingface\.co|hf\.co)/(.+)$", s, re.I)
    if m:
        path = m.group(1).split("?", 1)[0].split("#", 1)[0]
        parts = [p for p in path.split("/") if p]
        if parts and parts[0] in ("datasets", "spaces", "collections", "docs", "papers"):
            raise FetchError("That link isn't to a model. Paste a model's page, e.g. huggingface.co/owner/name.")
        if len(parts) < 2:
            raise FetchError("That link doesn't name a model (it should look like huggingface.co/owner/name).")
        repo = "/".join(parts[:2])
        if len(parts) >= 4 and parts[2] in ("tree", "blob", "resolve"):
            rev = urllib.parse.unquote(parts[3])
    else:
        repo = s
    if "@" in repo:
        repo, at = repo.split("@", 1)
        rev = rev or at
    check_repo_id(repo)
    if rev is not None and rev != "main" and not REVISION.match(rev):
        raise FetchError(f"{rev!r} isn't a branch, tag or commit name.")
    return repo, (None if rev in (None, "", "main") else rev)


def hf_headers(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def hf_info(repo: str, revision: str | None = None, token: str | None = None) -> dict:
    """What Hugging Face says about a model repo (at `revision`, default main): its commit,
    gating, tags, config and every file with its size and checksum."""
    check_repo_id(repo)
    path = f"/api/models/{repo}"
    if revision:
        path += f"/revision/{urllib.parse.quote(revision, safe='')}"
    try:
        d = get_json(f"{HF}{path}?blobs=true", timeout=60, headers=hf_headers(token)) or {}
    except FetchError as e:
        if e.status in (401, 403):
            raise FetchError(f"{repo} is private or needs you to accept its terms on huggingface.co first, and "
                             "then a Hugging Face token (Settings ▸ Hugging Face).", status=e.status) from e
        if e.status == 404:
            raise FetchError(f"Hugging Face has no model {repo}" + (f" at {revision}" if revision else "")
                             + ".", status=404) from e
        raise
    # Everything below is chosen by whoever owns the repo: take only values of the expected type.
    d = d if isinstance(d, dict) else {}
    as_dict = lambda x: x if isinstance(x, dict) else {}      # noqa: E731
    as_str = lambda x: x if isinstance(x, str) else None      # noqa: E731
    files = {}
    for s in d.get("siblings") or []:
        if not isinstance(s, dict) or not isinstance(s.get("rfilename"), str):
            continue
        lfs = as_dict(s.get("lfs"))
        try:
            size = int(s.get("size") or lfs.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        files[s["rfilename"]] = {"size": size, "sha256": as_str(lfs.get("sha256")), "blob_id": as_str(s.get("blobId"))}
    tags = [t for t in d.get("tags") or [] if isinstance(t, str)] if isinstance(d.get("tags"), list) else []
    lic = as_str(as_dict(d.get("cardData")).get("license")) or \
        next((t.split(":", 1)[1] for t in tags if t.startswith("license:")), None)
    config = as_dict(d.get("config"))
    if not isinstance(config.get("quantization_config"), dict):
        config = {k: v for k, v in config.items() if k != "quantization_config"}
    if not isinstance(config.get("architectures"), list):
        config = {k: v for k, v in config.items() if k != "architectures"}
    return {"id": as_str(d.get("id")) or repo, "sha": as_str(d.get("sha")), "gated": bool(d.get("gated")),
            "private": bool(d.get("private")), "tags": tags, "library": as_str(d.get("library_name")),
            "pipeline": as_str(d.get("pipeline_tag")), "license": lic, "config": config,
            "safetensors": as_dict(d.get("safetensors")), "modified": as_str(d.get("lastModified")), "files": files}


def hf_listing(repo: str, revision: str | None = None, token: str | None = None) -> dict[str, dict]:
    """{path: {"size": int, "sha256": str|None}} for every file in a model repo."""
    return {p: {"size": m["size"] or None, "sha256": m["sha256"]}
            for p, m in hf_info(repo, revision, token)["files"].items()}


def hf_url(repo: str, path: str, revision: str = "main") -> str:
    return f"{HF}/{repo}/resolve/{urllib.parse.quote(revision, safe='')}/{urllib.parse.quote(path)}"


def hf_repo_dir(hub: Path, repo: str) -> Path:
    """Where huggingface_hub (and every recipe that uses it) keeps a repo: <hub>/models--owner--name."""
    return Path(hub) / f"models--{check_repo_id(repo).replace('/', '--')}"


def _read_text(p: Path) -> str:
    try:
        return p.read_text().strip()
    except OSError:
        return ""


def hf_snapshot_status(hub: Path, repo: str, revision: str | None = None) -> dict:
    """See _hf_snapshot_status; a cache that can't be read (a folder of another user) counts as not there."""
    try:
        return _hf_snapshot_status(hub, repo, revision)
    except OSError as e:
        return {"present": False, "complete": False, "commit": None, "root": str(hf_repo_dir(hub, repo)),
                "detail": f"{repo}: can't read {hub} ({e.strerror or e})"}


def _hf_snapshot_status(hub: Path, repo: str, revision: str | None = None) -> dict:
    """Is `repo` fully in the Hugging Face cache at `hub`? Works for what this module downloaded
    (it leaves a manifest) and for what `hf download` or a recipe's script left. Pinning a commit
    means only that commit counts; otherwise whatever `main` points at (or the newest snapshot)."""
    root = hf_repo_dir(hub, repo)
    out = {"present": root.is_dir(), "complete": False, "commit": None, "root": str(root),
           "detail": f"{repo} isn't downloaded"}
    if not root.is_dir():
        return out
    snaps = root / "snapshots"
    commit = revision if revision and COMMIT.match(revision) else _read_text(root / "refs" / (revision or "main"))
    if not commit or not (snaps / commit).is_dir():
        if revision and COMMIT.match(revision):
            out["detail"] = f"{repo} is downloaded, but not at revision {revision[:8]}"
            return out
        dirs = sorted((p for p in snaps.glob("*") if p.is_dir()), key=lambda p: p.stat().st_mtime) if snaps.is_dir() else []
        commit = dirs[-1].name if dirs else None
    if not commit:
        out["detail"] = f"{repo} has no snapshot yet"
        return out
    snap = snaps / commit
    out["commit"] = commit
    manifest = {}
    try:
        manifest = json.loads((root / f"swapper-manifest.{commit}.json").read_text())
    except (OSError, ValueError):
        pass
    if manifest.get("commit") == commit and isinstance(manifest.get("files"), dict):
        bad = []
        for rel, size in manifest["files"].items():
            try:
                if (snap / rel).stat().st_size != size:
                    bad.append(rel)
            except OSError:
                bad.append(rel)
        out["complete"] = not bad
        out["detail"] = "" if not bad else f"{len(bad)} of {len(manifest['files'])} files of {repo} are missing"
        return out
    # Left by `hf download` or a recipe's own script: no manifest, so judge by the weights.
    if any((root / "blobs").glob("*.incomplete")):
        out["detail"] = f"{repo} is only partly downloaded"
        return out
    if not (snap / "config.json").exists():
        out["detail"] = f"{repo} has no config.json yet"
        return out
    shards = None
    idx = snap / "model.safetensors.index.json"
    if idx.exists():
        try:
            shards = set(json.loads(idx.read_text())["weight_map"].values())
        except (OSError, ValueError, KeyError):
            shards = None
        if shards is not None and not all((snap / s).exists() for s in shards):
            out["detail"] = f"{repo} is missing some of its weight files"
            return out
    has_weights = shards or any(snap.rglob("*.safetensors")) or any(snap.rglob("*.gguf")) or any(snap.rglob("*.bin"))
    out["complete"] = bool(has_weights)
    out["detail"] = "" if has_weights else f"{repo} has no weights yet"
    return out


def hf_repo_bytes(root: Path) -> int:
    """Bytes a repo takes in the cache: its blobs (the snapshot folders only hold links to them). A repo
    folder that is itself a link (moved to another disk) takes nothing here."""
    total = 0
    if Path(root).is_symlink():
        return 0
    try:
        blobs = list((Path(root) / "blobs").glob("*"))
    except OSError:
        return 0
    for p in blobs:
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def is_dir(p: Path) -> bool:
    """Path.is_dir() that says no — rather than raising — for a folder this user may not look into."""
    try:
        return Path(p).is_dir()
    except OSError:
        return False


def exists(p: Path) -> bool:
    try:
        return Path(p).exists()
    except OSError:
        return False


def is_file(p: Path) -> bool:
    try:
        return Path(p).is_file()
    except OSError:
        return False


def mkdirs(path: Path, owner: tuple[int, int] | None = None):
    """mkdir -p that hands every folder it makes to `owner`, so a cache the Swapper (root)
    creates inside the box user's home stays writable by that user."""
    missing = []
    p = Path(path)
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(exist_ok=True)
        if owner and hasattr(os, "lchown"):
            try:
                os.lchown(d, *owner)
            except OSError:
                pass


def _chown_tree(path: Path, owner: tuple[int, int] | None):
    if not owner or not hasattr(os, "lchown"):
        return
    uid, gid = owner
    for dirpath, dirnames, filenames in os.walk(path):
        for name in [dirpath, *(os.path.join(dirpath, n) for n in dirnames + filenames)]:
            try:
                os.lchown(name, uid, gid)
            except OSError:
                pass


def _atomic_write(path: Path, text: str):
    """Write a small file by replacing it: a link someone left in its place is replaced, not written through."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    with open(tmp, "x") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _check_root(root: Path, owner: tuple[int, int] | None):
    """A repo folder may be a link to another disk (models moved off the system drive), but then it must be
    the box user's own folder: the Swapper changes ownership of what it finds inside."""
    if root.is_symlink() and owner is not None:
        try:
            uid = os.stat(root).st_uid
        except OSError as e:
            raise FetchError(f"{root} is a broken link ({e.strerror}).") from e
        if uid != owner[0]:
            raise FetchError(f"{root} is a link to a folder that isn't the box user's; refusing to write there.")


WEIGHT_SUFFIXES = (".safetensors", ".bin", ".pt", ".pth", ".ckpt", ".h5", ".msgpack", ".onnx", ".ot", ".gguf")


def dedupe_weights(files: dict) -> dict:
    """A repo that ships safetensors often also ships the same weights as .bin, .pth, .msgpack …; the engines read
    the safetensors, so the rest is skipped (it can double or triple the download)."""
    if not any(p.endswith(".safetensors") for p in files):
        return dict(files)
    return {p: m for p, m in files.items() if not p.endswith(WEIGHT_SUFFIXES) or p.endswith(".safetensors")}


def hf_snapshot(repo: str, hub: Path, *, revision: str | None = None, token: str | None = None, say=print,
                exclude=DEFAULT_EXCLUDE, owner: tuple[int, int] | None = None, info: dict | None = None) -> dict:
    """Download a whole model repo into the Hugging Face cache at `hub`, in the layout
    huggingface_hub uses (blobs/<hash>, snapshots/<commit>/<file> links, refs/main), so
    vLLM, SGLang, TensorFold and `hf download` all find it there and share the copy.
    Resumable; large files are checked against the SHA-256 Hugging Face publishes."""
    info = info or hf_info(repo, revision, token)
    commit = info.get("sha")
    if not commit or not COMMIT.match(commit):
        raise FetchError(f"Hugging Face didn't say which commit of {repo} this is.")
    files = dedupe_weights({p: m for p, m in info["files"].items()
                            if not any(fnmatch.fnmatch(p, pat) for pat in exclude)})
    if not files:
        raise FetchError(f"{repo} has no files to download.")
    for p in files:
        if p.startswith("/") or ".." in Path(p).parts:
            raise FetchError(f"{repo} lists a file path that leaves its folder ({p!r}); refusing.")
    hub = Path(hub)
    root = hf_repo_dir(hub, repo)
    _check_root(root, owner)
    blobs, snaps = root / "blobs", root / "snapshots"
    snap, refs = snaps / commit, root / "refs"
    mkdirs(blobs, owner)
    for d in (snaps, snap, refs):
        d.mkdir(parents=True, exist_ok=True)
    # The Swapper runs as root inside a folder the box user owns: nothing it writes may go through a link they made.
    for d in (blobs, snaps, snap, refs):
        if d.is_symlink():
            raise FetchError(f"{d} is a link; refusing to download through it.")
    total = sum(m["size"] for m in files.values())
    todo, need = [], 0
    for p, m in files.items():
        etag = m["sha256"] or m["blob_id"]
        if not etag or not re.fullmatch(r"[0-9a-fA-F]{40,64}", etag):
            raise FetchError(f"{repo}: Hugging Face gave no usable checksum for {p}.")
        m["etag"] = etag
        blob = blobs / etag
        if blob.is_file() and not blob.is_symlink() and blob.stat().st_size == m["size"]:
            continue
        part = blob.with_name(blob.name + ".part")
        need += m["size"] - (part.stat().st_size if part.exists() else 0)
        todo.append((p, m, blob))
    free = shutil.disk_usage(blobs).free
    say(f"{repo} @ {commit[:8]}: {len(files)} files, {human(total)}; to download: {human(need)} "
        f"({len(todo)} files); free here: {human(free)}.")
    if need > free - 5 * 1024**3:
        raise FetchError(f"Not enough disk space for {repo}: it needs {human(need)} more, {human(free)} is free "
                         f"on {blobs}. Delete a model you no longer use from the Storage tab, then try again.")
    for i, (p, m, blob) in enumerate(todo, 1):
        if blob.is_symlink():
            blob.unlink()
        big = m["size"] >= 64 * 1024 * 1024
        if big:
            say(f"[{i}/{len(todo)}] {p}")
        download(hf_url(repo, p, commit), blob, size=m["size"], sha256=m["sha256"],
                 say=say if big else (lambda _s: None), label=p, headers=hf_headers(token))
    for p, m in files.items():
        link = snap / p
        link.parent.mkdir(parents=True, exist_ok=True)
        target = os.path.relpath(blobs / m["etag"], link.parent)
        if link.is_symlink():
            if os.readlink(link) == target:
                continue
            link.unlink()
        elif link.exists():
            link.unlink()
        link.symlink_to(target)
    # What an unpinned `vllm serve owner/name` (offline) resolves "main" through. A download of an older, pinned
    # commit must not move it back: only set it when it is missing or points at nothing that is there.
    main = refs / "main"
    current = _read_text(main)
    if revision in (None, "main") or not current or not (snaps / current).is_dir():
        _atomic_write(main, commit)
    _atomic_write(root / f"swapper-manifest.{commit}.json", json.dumps(
        {"repo": repo, "commit": commit, "files": {p: m["size"] for p, m in files.items()},
         "bytes": total, "at": time.time()}))
    _chown_tree(root, owner)
    say(f"{repo}: all {len(files)} files are in place.")
    return {"commit": commit, "files": len(files), "bytes": total, "root": str(root)}


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
