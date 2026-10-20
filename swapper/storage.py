"""Disk space: what is on the box, what each model takes, and taking models off it.

A model leaves several things behind: the recipe's git checkout, its weights (in the
Hugging Face cache, or the Swapper's own download folder for GGUF files), the docker
images it runs from, and the kernel caches its engine builds. `Storage` knows where
each model's things are from its catalog entry (`footprint`, plus what its install
steps made), measures them, and removes them when a model is deleted.

Weights and images are often shared: Qwen3.8 Flash Next on TensorFold and on vLLM read
the same checkpoint, and several recipes build on the same NVIDIA PyTorch image. A thing
is only removed when no *other installed* model uses it, and the plan says so before
anything is touched.

Measuring walks big folders and asks docker, so it runs in the background and the page
polls for the result; a scan is reused for a couple of minutes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import fetch
from engine import DEMO, Engine, Job, sh

GB = 1000 ** 3
_UNITS = {"B": 1, "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}
_IMAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,255}$")


class StorageError(Exception):
    pass


def parse_size(text) -> int:
    """Docker's human sizes ("329.7MB", "1.1GB", "24.35kB", "0B") as bytes."""
    m = re.match(r"^\s*([\d.]+)\s*([kKmMgGtT]?[bB])\s*$", str(text or ""))
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNITS[m.group(2).upper()])
    except (ValueError, KeyError):
        return 0


def dir_bytes(path: Path) -> int:
    """Bytes under `path`, without following links (a link itself takes no room: deleting it frees nothing)."""
    total = 0
    if os.path.islink(path):
        return 0
    stack = [str(path)]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def nearest_existing(path: Path) -> Path:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def mount_point(path: Path) -> Path:
    p = nearest_existing(path).resolve()
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return p


def split_image_ref(ref: str) -> tuple[str, str]:
    """"host:5000/repo:tag" → ("host:5000/repo", "tag"); no tag → ("repo", "")."""
    if ":" in ref.rsplit("/", 1)[-1]:
        head, tag = ref.rsplit(":", 1)
        return head, tag
    return ref, ""


def image_matches(ref: str, repository: str, tag: str) -> bool:
    """A footprint image is "repo:tag" (exactly that one) or "repo" (every tag of it)."""
    last = ref.rsplit("/", 1)[-1]
    if ":" in last or "@" in last:
        return ref == f"{repository}:{tag}"
    return ref == repository


def ref_used(ref: str, used: set[str]) -> bool:
    """Whether `used` (image names as `docker ps` prints them) contains this reference — a
    container made from "repo" shows "repo", one made from "repo:latest" shows "repo:latest"."""
    repo, tag = split_image_ref(ref)
    return ref in used or (tag in ("", "latest") and (repo in used or f"{repo}:latest" in used))


class Storage:
    TTL = 120   # seconds a scan is reused

    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.Lock()
        self._data: dict | None = None
        self._at = 0.0
        self._scanning = False

    # ── what a model leaves on the box ──
    def footprint(self, m: dict) -> dict:
        """{"dirs", "hf", "images", "files"}: everything this catalog entry puts on the disk.
        Derived from its install steps, plus the entry's own "footprint" (kernel caches,
        extra images …)."""
        fp = m.get("footprint") or {}
        inst = m.get("install") or {}
        builds = self.engine.cfg.get("builds") or {}
        dirs = list(fp.get("dirs") or [])
        images = list(fp.get("images") or []) + list(fp.get("shared_images") or [])
        if m.get("dir"):
            dirs.insert(0, m["dir"])
        for step in inst.get("steps", []):
            do = step.get("do")
            if do == "git" and step.get("dir"):
                dirs.append(step["dir"])
            elif do == "docker_pull" and step.get("image"):
                images.append(step["image"])
            elif do == "docker_build":
                image = step.get("image") or (builds.get(step.get("build")) or {}).get("image")
                if image:
                    images.append(image)
        files = [(f["repo"], f["path"]) for f in m.get("files") or []]
        return {"dirs": list(dict.fromkeys(dirs)), "hf": Engine.model_hf_repos(m),
                "images": list(dict.fromkeys(images)), "files": files}

    def _installed(self, key: str) -> bool:
        return bool(self.engine.install_state(key)["installed"])

    @staticmethod
    def mounts(m: dict) -> list[str]:
        """Host paths a docker model bind-mounts (`-v host:container`)."""
        args = m.get("docker_run") or []
        return [a.split(":", 1)[0] for flag, a in zip(args, args[1:])
                if flag == "-v" and isinstance(a, str) and a.startswith("/")]

    def holders(self) -> dict[str, set[str]]:
        """part id → the models whose entries use it. A folder another model mounts into its container
        (the kernel caches several vLLM models share) counts as used by that model too, whether or not it
        is in its own footprint."""
        out: dict[str, set[str]] = {}
        models = list(self.engine.models.items())
        for key, m in models:
            fp = self.footprint(m)
            for d in fp["dirs"]:
                out.setdefault(f"dir:{d}", set()).add(key)
            for repo in fp["hf"]:
                out.setdefault(f"hf:{repo}", set()).add(key)
            for ref in fp["images"]:
                out.setdefault(f"image:{ref}", set()).add(key)
            for repo, path in fp["files"]:
                out.setdefault(f"file:{repo}/{path}", set()).add(key)
        for key, m in models:
            for host in self.mounts(m):
                for pid, users in out.items():
                    if pid.startswith("dir:"):
                        d = pid[4:]
                        if host == d or host.startswith(d.rstrip("/") + "/") or d.startswith(host.rstrip("/") + "/"):
                            users.add(key)
        return out

    # ── docker ──
    def docker_images(self) -> list[dict]:
        """Images with their sizes. `unique` is what removing the image alone would free
        (layers shared with other images stay)."""
        rc, out = sh(["docker", "system", "df", "-v", "--format", "json"], timeout=90)
        rows = None
        if rc == 0:
            try:
                rows = json.loads(out).get("Images")
            except (ValueError, AttributeError):
                rows = None
        if rows is None:
            rc, out = sh(["docker", "image", "ls", "--format", "{{json .}}"], timeout=60)
            rows = []
            if rc == 0:
                for line in out.splitlines():
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        continue
        images = []
        for r in rows or []:
            repo, tag = r.get("Repository") or "", r.get("Tag") or ""
            if repo in ("", "<none>"):
                continue
            size = parse_size(r.get("Size"))
            unique = parse_size(r.get("UniqueSize")) if r.get("UniqueSize") not in (None, "N/A") else size
            images.append({"ref": f"{repo}:{tag}" if tag and tag != "<none>" else repo, "repository": repo,
                           "tag": tag, "id": r.get("ID") or "", "size": size, "unique": unique or size,
                           "containers": int(r.get("Containers") or 0) if str(r.get("Containers") or "0").isdigit() else 0})
        return images

    def docker_usage(self) -> dict:
        rc, out = sh(["docker", "system", "df", "--format", "{{.Type}}|{{.TotalCount}}|{{.Size}}|{{.Reclaimable}}"],
                     timeout=60)
        use = {}
        if rc == 0:
            for line in out.splitlines():
                parts = line.split("|")
                if len(parts) == 4:
                    use[parts[0]] = {"count": int(parts[1] or 0) if parts[1].isdigit() else 0,
                                     "bytes": parse_size(parts[2]), "reclaimable": parse_size(parts[3].split(" ")[0])}
        return use

    def container_images(self) -> dict[str, str]:
        """{container name: image reference it was made from}, running or not."""
        rc, out = sh(["docker", "ps", "-a", "--format", "{{.Names}}|{{.Image}}"], timeout=30)
        res = {}
        if rc == 0:
            for line in out.splitlines():
                n, _, img = line.partition("|")
                if n:
                    res[n] = img
        return res

    def docker_root(self) -> Path | None:
        rc, out = sh(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=20)
        return Path(out.strip()) if rc == 0 and out.strip().startswith("/") else None

    # ── disks ──
    def disks(self) -> list[dict]:
        eng = self.engine
        roles = [("Models, recipes and caches (home)", Path(eng.home)),
                 ("Hugging Face downloads", eng.hf_hub()),
                 ("Docker images", self.docker_root() or Path("/var/lib/docker")),
                 ("Swapper downloads (GGUF files, llama.cpp)", eng.runtime_dir),
                 ("Swapper's own data", eng.state_dir)]
        groups: dict[int, dict] = {}
        for label, path in roles:
            real = nearest_existing(path)
            try:
                dev = real.stat().st_dev
                usage = shutil.disk_usage(real)
            except OSError:
                continue
            g = groups.setdefault(dev, {"mount": str(mount_point(real)), "total": usage.total, "free": usage.free,
                                        "used": usage.total - usage.free, "roles": []})
            g["roles"].append({"label": label, "path": str(path)})
        return list(groups.values())

    # ── measuring ──
    def _model_parts(self, key: str, m: dict, images: list[dict], holders: dict[str, set[str]]) -> list[dict]:
        eng = self.engine
        fp = self.footprint(m)
        parts = []

        def part(pid, kind, label, bytes_, where):
            others = sorted(k for k in holders.get(pid, ()) if k != key)
            installed_others = [k for k in others if self._installed(k)]
            parts.append({"id": pid, "kind": kind, "label": label, "bytes": bytes_, "where": where,
                          "shared_with": [eng.models[k]["title"] for k in others],
                          "kept_for": [eng.models[k]["title"] for k in installed_others],
                          "exclusive": not installed_others})

        link_note = " (a link: only the link is removed, the files it points to stay)"
        for d in fp["dirs"]:
            p = Path(d)
            if fetch.exists(p) or os.path.islink(p):
                is_recipe = bool(m.get("dir") and d == m["dir"]) or fetch.exists(p / ".git")
                part(f"dir:{d}", "recipe" if is_recipe else "cache", (p.name or d) + (link_note if os.path.islink(p) else ""),
                     dir_bytes(p), [str(p)])
        for repo in fp["hf"]:
            roots = [fetch.hf_repo_dir(h, repo) for h in eng.hf_hubs()]
            roots = [r for r in roots if fetch.is_dir(r) or os.path.islink(r)]
            if roots:
                linked = any(os.path.islink(r) for r in roots)
                part(f"hf:{repo}", "weights", repo + (link_note if linked else ""),
                     sum(fetch.hf_repo_bytes(r) for r in roots), [str(r) for r in roots])
        for repo, path in fp["files"]:
            f = eng.local_file(repo, path)
            if fetch.is_file(f):
                linked = os.path.islink(f)
                part(f"file:{repo}/{path}", "weights", Path(path).name + (link_note if linked else ""),
                     0 if linked else f.stat().st_size, [str(f)])
        for ref in fp["images"]:
            hits = [i for i in images if image_matches(ref, i["repository"], i["tag"])]
            if hits:
                part(f"image:{ref}", "image", ref, sum(i["unique"] for i in hits), [i["ref"] for i in hits])
        return parts

    def _scan(self) -> dict:
        eng = self.engine
        images = self.docker_images()
        holders = self.holders()
        containers = self.container_images()
        usage = self.docker_usage()
        models, claimed_hf, claimed_images = [], set(), []
        for key, m in list(eng.models.items()):
            fp = self.footprint(m)
            claimed_hf.update(fp["hf"])
            claimed_images.extend(fp["images"])
            try:
                parts = self._model_parts(key, m, images, holders)
                inst = self._installed(key)
            except KeyError:          # a model added from Hugging Face was removed while this scan ran
                continue
            models.append({"key": key, "title": m["title"], "installed": inst, "custom": m.get("source") == "custom",
                           "bytes": sum(p["bytes"] for p in parts),
                           "bytes_freed": sum(p["bytes"] for p in parts if p["exclusive"]),
                           "parts": parts})
        eng.disk_sizes = {x["key"]: x["bytes"] for x in models}
        # Things on the disk that no catalog entry accounts for: shown so they can be cleaned up.
        other = []
        for hub in eng.hf_hubs():
            for d in sorted(hub.glob("models--*")) if fetch.is_dir(hub) else []:
                repo = d.name[len("models--"):].replace("--", "/", 1)
                if repo in claimed_hf:
                    continue
                other.append({"kind": "hf", "name": repo, "label": repo, "bytes": fetch.hf_repo_bytes(d),
                              "where": str(d), "removable": bool(fetch.REPO_ID.match(repo))})
        in_use = set(containers.values())
        for i in images:
            if any(image_matches(ref, i["repository"], i["tag"]) for ref in claimed_images):
                continue
            used = i["containers"] > 0 or ref_used(i["ref"], in_use) or i["id"][:12] in {c[:12] for c in in_use}
            other.append({"kind": "image", "name": i["ref"], "label": i["ref"], "bytes": i["unique"],
                          "where": "docker image", "removable": not used and bool(_IMAGE_REF.match(i["ref"])),
                          "note": "in use by a container" if used else ""})
        build = usage.get("Build Cache") or {}
        if build.get("bytes"):
            other.append({"kind": "builder", "name": "build-cache", "label": "Docker build cache",
                          "bytes": build.get("reclaimable") or build["bytes"], "where": "docker", "removable": True,
                          "note": "Layers left over from building images; safe to clear, a rebuild just takes longer."})
        other.sort(key=lambda x: -x["bytes"])
        return {"at": time.time(), "disks": self.disks(), "models": models, "other": other,
                "docker": {k: v for k, v in usage.items()},
                "models_total": sum(x["bytes"] for x in models)}

    def _demo(self) -> dict:
        eng = self.engine
        total, used = 3840 * GB, 1960 * GB
        sizes = {"tf27b": 61, "tfflash": 138, "nemotron": 49, "standard": 41, "bolt": 49}
        models = []
        for key, m in eng.models.items():
            if not self._installed(key):
                continue
            gb = sizes.get(key, 40)
            models.append({"key": key, "title": m["title"], "installed": True, "custom": False, "bytes": gb * GB,
                           "bytes_freed": int(gb * 0.9 * GB),
                           "parts": [{"id": f"hf:{key}", "kind": "weights", "label": "weights", "bytes": int(gb * 0.7 * GB),
                                      "where": [], "shared_with": [], "kept_for": [], "exclusive": True},
                                     {"id": f"image:{key}", "kind": "image", "label": "engine image",
                                      "bytes": int(gb * 0.2 * GB), "where": [], "shared_with": [], "kept_for": [],
                                      "exclusive": True},
                                     {"id": f"dir:{key}", "kind": "recipe", "label": "recipe", "bytes": int(gb * 0.1 * GB),
                                      "where": [], "shared_with": ["Another model"], "kept_for": ["Another model"],
                                      "exclusive": False}]})
        eng.disk_sizes = {x["key"]: x["bytes"] for x in models}
        return {"at": time.time(), "demo": True,
                "disks": [{"mount": "/", "total": total, "used": used, "free": total - used,
                           "roles": [{"label": "Models, recipes and caches (home)", "path": "/home/demo"},
                                     {"label": "Hugging Face downloads", "path": "/home/demo/.cache/huggingface/hub"},
                                     {"label": "Docker images", "path": "/var/lib/docker"}]}],
                "models": models,
                "other": [{"kind": "hf", "name": "azampatti/Qwen3.8-Flash-Next-125B-A5B-INT4-AutoRound",
                           "label": "azampatti/Qwen3.8-Flash-Next-125B-A5B-INT4-AutoRound", "bytes": 131 * GB,
                           "where": "/home/demo/.cache/huggingface/hub", "removable": True},
                          {"kind": "builder", "name": "build-cache", "label": "Docker build cache", "bytes": 22 * GB,
                           "where": "docker", "removable": True, "note": "Layers left over from building images."}],
                "docker": {"Images": {"count": 9, "bytes": 96 * GB, "reclaimable": 31 * GB}},
                "models_total": sum(x["bytes"] for x in models)}

    # ── the scan, in the background ──
    def snapshot(self, fresh: bool = False) -> dict:
        """The last scan (with `scanning` true while a newer one runs). The first call has no
        scan yet and returns just the disks."""
        with self._lock:
            stale = self._data is None or time.time() - self._at > self.TTL
            if (fresh or stale) and not self._scanning:
                self._scanning = True
                threading.Thread(target=self._scan_thread, daemon=True).start()
            data = self._data
            scanning = self._scanning
        if data is None:
            data = {"at": None, "disks": [] if DEMO else self.disks(), "models": [], "other": [], "docker": {},
                    "models_total": 0}
        return {**data, "scanning": scanning}

    def _scan_thread(self):
        try:
            data = self._demo() if DEMO else self._scan()
        except Exception as e:  # noqa: BLE001 — a failed scan must not leave the page waiting
            data = {"at": time.time(), "disks": self.disks(), "models": [], "other": [], "docker": {},
                    "models_total": 0, "error": f"{type(e).__name__}: {e}"}
        with self._lock:
            self._data, self._at, self._scanning = data, time.time(), False

    def touch(self):
        """Start a scan if there has never been one or the last is old: keeps the per-model sizes on the
        Models tab roughly current without a scan on every poll."""
        with self._lock:
            if self._scanning or (self._data is not None and time.time() - self._at < 600):
                return
            self._scanning = True
        threading.Thread(target=self._scan_thread, daemon=True).start()

    def invalidate(self):
        with self._lock:
            self._at = 0.0

    # ── removing things ──
    def _roots(self) -> list[Path]:
        eng = self.engine
        roots = [Path(eng.home), Path("/root/.cache"), eng.runtime_dir, *eng.hf_hubs()]
        return [r.resolve() for r in roots if str(r) not in ("", "/")]

    def _protected(self) -> set[Path]:
        eng = self.engine
        home = Path(eng.home)
        out = {Path("/"), home, home / ".cache", home / ".cache/huggingface", home / ".local", home / ".local/state",
               home / ".config", Path("/root"), Path("/root/.cache"), eng.runtime_dir, eng.runtime_dir / "models",
               eng.state_dir, *eng.hf_hubs()}
        return {p.resolve() for p in out}

    def path_problem(self, path: Path) -> str | None:
        """Why `path` may not be removed (None if it may). The same check guards the removal and is run
        for every path in the plan first, so a delete never gets half-way and then refuses."""
        p = Path(path)
        if not p.is_absolute():
            return "isn't an absolute path"
        roots = self._roots()
        # where the path itself sits (a link is judged by its own place, not by what it points at)
        place = Path(os.path.abspath(p))
        if place.parent.resolve() / place.name in self._protected():
            return "is one of the folders that hold everything and is never removed"
        if p.is_symlink():
            # only the link goes; it must itself sit inside a model folder
            parent = Path(os.path.abspath(p.parent)).resolve()
            return None if any(r == parent or r in parent.parents for r in roots) else \
                "is a link outside the model folders"
        if not fetch.exists(p):
            return None
        real = p.resolve()
        if real in self._protected() or not any(r in real.parents for r in roots):
            return ("isn't a model folder inside the box user's home, the Hugging Face cache or the Swapper's "
                    "download folder (is something along its path a link to another disk? move or remove it yourself)")
        return None

    def remove_path(self, path: Path, job: Job) -> bool:
        """Delete a file or folder the Swapper itself put (or a recipe put) under the box user's
        home, the HF cache or the Swapper's download folder — and nothing else. A link is
        unlinked, never followed."""
        p = Path(path)
        problem = self.path_problem(p)
        if problem:
            raise StorageError(f"won't remove {p}: it {problem}.")
        if p.is_symlink():
            p.unlink()
            job.say(f"removed link {p}")
            return True
        if not fetch.exists(p):
            return False
        job.say(f"removing {p} …")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return True

    def plan_delete(self, key: str) -> dict:
        """What deleting a model would remove and what it would keep (shared with another
        installed model), and what stops it."""
        eng = self.engine
        m = eng.models.get(key)
        if not m:
            raise StorageError("unknown model")
        if DEMO:
            parts = next((x["parts"] for x in self._demo()["models"] if x["key"] == key), [])
        else:
            parts = self._model_parts(key, m, self.docker_images(), self.holders())
        blockers = []
        st = eng.status()
        if st["active"] == key or st["loading"] == key or (eng.job and eng.job.state == "running"
                                                             and eng.job.target == key):
            blockers.append(f"{m['title']} is serving or loading right now. Switch to another model first.")
        if eng.job and eng.job.state == "running":
            blockers.append("A model switch is in progress. Wait for it to finish.")
        containers = self.container_images() if not DEMO else {}
        in_use = {i for n, i in containers.items() if n != m.get("container")}
        remove, keep = [], []
        for p in parts:
            item = {k: p[k] for k in ("id", "kind", "label", "bytes", "where", "kept_for")}
            if not p["exclusive"]:
                keep.append(item)
                continue
            if p["kind"] == "image" and any(ref_used(w, in_use) for w in p["where"]):
                item["kept_for"] = ["a container that isn't this model's"]
                keep.append(item)
                continue
            remove.append(item)
        for item in remove:                          # every path is checked now, not half-way through the delete
            if item["kind"] == "image":
                continue
            for w in item["where"]:
                problem = self.path_problem(Path(w))
                if problem:
                    blockers.append(f"{w} {problem}.")
        return {"key": key, "title": m["title"], "installed": self._installed(key), "custom": m.get("source") == "custom",
                "container": m.get("container"), "remove": remove, "keep": keep,
                "bytes": sum(p["bytes"] for p in remove), "blockers": blockers}

    def delete_model(self, key: str, job: Job) -> int:
        """Remove a model: its container, the weights, images and caches nobody else installed
        uses, its measured speed and — for one added from Hugging Face — its entry. Returns the
        bytes the plan said would be freed."""
        eng = self.engine
        m = eng.models[key]
        job.step("scan", "running")
        plan = self.plan_delete(key)
        if plan["blockers"]:
            raise StorageError(" ".join(plan["blockers"]))
        job.say(f"Deleting {m['title']}: freeing about {fetch.human(plan['bytes'])}.")
        for p in plan["keep"]:
            job.say(f"keeping {p['label']} ({fetch.human(p['bytes'])}): also used by {', '.join(p['kept_for'])}")
        job.step("scan", "done")

        job.step("remove", "running")
        if m["kind"] == "llamacpp" and m.get("unit"):
            sh(["systemctl", "stop", m["unit"]], timeout=60)
            sh(["systemctl", "reset-failed", m["unit"]], timeout=10)
        if m.get("container") and eng.container_exists(m["container"]):
            job.say(f"docker rm -f {m['container']}")
            rc, out = sh(["docker", "rm", "-f", m["container"]], timeout=120)
            if rc != 0:
                job.say(out)
        for p in plan["remove"]:
            if p["kind"] == "image":
                for ref in p["where"]:
                    if not _IMAGE_REF.match(ref):
                        continue
                    job.say(f"docker rmi {ref}")
                    rc, out = sh(["docker", "rmi", ref], timeout=300)
                    if rc != 0:
                        job.say(f"  couldn't remove it ({out.splitlines()[-1] if out else rc}); leaving it")
            else:
                for w in p["where"]:
                    self.remove_path(Path(w), job)
                    if p["id"].startswith("file:"):
                        marker = Path(w).with_name(Path(w).name + ".ok")
                        marker.unlink(missing_ok=True)
                        self._prune_empty(Path(w).parent)
        job.step("remove", "done")

        job.step("refresh", "running")

        def forget(st):
            st = dict(st or {})
            st.pop(key, None)
            return st
        eng.update_state("measured", forget)
        if eng.load_state().get("desired") == key:
            eng.save_state(desired=None)
        if m.get("source") == "custom":
            eng.remove_custom_model(key)
            job.say("removed it from the list of models")
        if DEMO:
            eng._demo_removed.add(key)
        eng.forget_install_cache()
        eng.disk_sizes.pop(key, None)
        self.invalidate()
        self._forget_openclaw_provider(m, job)
        job.step("refresh", "done")
        job.note = f"Freed about {fetch.human(plan['bytes'])}."
        job.say(job.note)
        return plan["bytes"]

    def _prune_empty(self, d: Path):
        stop = self.engine.models_dir.resolve() if self.engine.models_dir.exists() else None
        while stop and d.resolve() != stop and stop in d.resolve().parents:
            try:
                d.rmdir()
            except OSError:
                return
            d = d.parent

    def _forget_openclaw_provider(self, m: dict, job: Job):
        """Take the model's provider entry out of OpenClaw's config, unless OpenClaw still uses it."""
        eng = self.engine
        provider = m.get("openclaw_provider")
        if DEMO or not provider or not eng.openclaw_installed():
            return
        in_use = [r for r in eng.openclaw_refs() if r.startswith(f"{provider}/")]
        if in_use:
            job.say(f"OpenClaw still uses {in_use[0]} (as its main, image or compaction model); leaving the "
                    f"{provider} entry until you switch to another model.")
            return
        cfg = eng.openclaw_config()
        if provider not in ((cfg.get("models") or {}).get("providers") or {}):
            return
        cmd = eng._as_oc_user([eng.cfg["openclaw"]["bin"], "config", "unset", f"models.providers.{provider}"])
        rc, out = sh(cmd, timeout=60)
        job.say(f"OpenClaw: removed the {provider} provider." if rc == 0
                else f"OpenClaw: couldn't remove the {provider} provider ({out.splitlines()[-1] if out else rc}); "
                     "it is harmless and stays.")

    def delete_other(self, kind: str, name: str, job: Job) -> int:
        """Remove something no catalog entry accounts for: a Hugging Face repo, a docker image
        nothing uses, or docker's build cache."""
        eng = self.engine
        job.step("scan", "running")
        if (eng.job and eng.job.state == "running") or eng.status()["loading"]:
            raise StorageError("A model switch is in progress. Wait for it to finish.")
        job.step("scan", "done")
        job.step("remove", "running")
        freed = 0
        if kind == "hf":
            fetch.check_repo_id(name)
            if any(name in self.footprint(m)["hf"] for m in eng.models.values()):
                raise StorageError(f"{name} belongs to a model in the list; delete that model instead.")
            roots = [fetch.hf_repo_dir(h, name) for h in eng.hf_hubs()]
            roots = [r for r in roots if fetch.is_dir(r) or os.path.islink(r)]
            for root in roots:                       # check everything first: no half-done removal
                problem = self.path_problem(root)
                if problem:
                    raise StorageError(f"won't remove {root}: it {problem}.")
            for root in roots:
                freed += fetch.hf_repo_bytes(root)
                self.remove_path(root, job)
        elif kind == "image":
            if not _IMAGE_REF.match(name):
                raise StorageError("that isn't an image name")
            images = self.docker_images()
            # the same image can be known by several names or by its id: a model's claim on any of them counts
            bare = name.replace("sha256:", "")
            same = [i for i in images if i["ref"] == name or
                    (len(bare) >= 6 and i["id"].replace("sha256:", "").startswith(bare))]
            ids = {i["id"] for i in same if i["id"]}
            related = [i for i in images if i["id"] in ids] or same
            claimed = [(i["repository"], i["tag"]) for i in related] + [split_image_ref(name)]
            if any(image_matches(ref, repo, tag) for m in eng.models.values() for ref in self.footprint(m)["images"]
                   for repo, tag in claimed):
                raise StorageError(f"{name} belongs to a model in the list; delete that model instead.")
            used = set(self.container_images().values())
            if ref_used(name, used) or any(ref_used(i["ref"], used) for i in related):
                raise StorageError(f"{name} is used by a container; remove the container first.")
            job.say(f"docker rmi {name}")
            rc, out = sh(["docker", "rmi", name], timeout=300)
            if rc != 0:
                raise StorageError(out.splitlines()[-1] if out else f"docker rmi failed ({rc})")
            freed = next((i["unique"] for i in self.docker_images() if i["ref"] == name), 0)
        elif kind == "builder":
            job.say("docker builder prune -af")
            rc, out = sh(["docker", "builder", "prune", "-af"], timeout=900)
            job.say(out[-300:])
            if rc != 0:
                raise StorageError("docker builder prune failed")
        else:
            raise StorageError("unknown kind of thing to remove")
        job.step("remove", "done")
        job.step("refresh", "running")
        eng.forget_install_cache()
        self.invalidate()
        job.step("refresh", "done")
        job.note = "Removed." if not freed else f"Freed about {fetch.human(freed)}."
        job.say(job.note)
        return freed
