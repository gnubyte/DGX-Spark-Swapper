"""Tests for the 2.1 features: downloading from Hugging Face, adding a model from a link, the Storage
tab (sizes, shared weights, deleting) and the new docker-based engines.

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "swapper"))

import fetch  # noqa: E402
import hfmodels  # noqa: E402
import storage  # noqa: E402
from engine import Engine, Job  # noqa: E402
from installer import InstallFailed, Installer  # noqa: E402
from test_swapper import TempDir, free_port, site  # noqa: E402

FP4 = "nvidia/Qwen3.8-Flash-Next-FP4-Local"
CATALOG_PATH = ROOT / "swapper" / "catalog.json"


def a_job(target="x", steps=("scan", "remove", "refresh")) -> Job:
    return Job(target, None, list(steps))


def lots_of_disk(*a, **k):
    return types.SimpleNamespace(total=2 * 10**12, used=10**11, free=19 * 10**11)


# ─────────────────────────────── Hugging Face downloads ───────────────────────────────

class FakeHF(http.server.BaseHTTPRequestHandler):
    """A stand-in for huggingface.co: /api/models/<repo>[/revision/<rev>] and /<repo>/resolve/<rev>/<path>.
    `repos` maps a repo id to {"sha", "files": {path: bytes}, "lfs": {paths}, "wrong_sha": {paths}};
    `cdn` (a base URL) makes resolve answer 302 to that host, like the real thing."""
    repos: dict = {}
    cdn: str | None = None
    seen: list = []          # (path, Authorization header) of every request
    gated: set = set()       # repos that answer 401 until you bring a token

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", extra=None):
        self.send_response(code)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        self.seen.append((path, self.headers.get("Authorization")))
        m = re.match(r"^/api/models/([^/]+/[^/]+)(?:/revision/([^/]+))?$", path)
        if m:
            if m.group(1) in self.gated:
                return self._send(401, b"{}")
            r = self.repos.get(m.group(1))
            if not r:
                return self._send(404, b"{}")
            siblings = []
            for p, data in r["files"].items():
                s = {"rfilename": p, "size": len(data), "blobId": hashlib.sha1(data).hexdigest()}
                if p in r.get("lfs", ()):
                    sha = "0" * 64 if p in r.get("wrong_sha", ()) else hashlib.sha256(data).hexdigest()
                    s["lfs"] = {"sha256": sha, "size": len(data)}
                siblings.append(s)
            body = json.dumps({"id": m.group(1), "sha": r["sha"], "gated": False, "tags": r.get("tags", []),
                               "config": r.get("config", {}), "siblings": siblings}).encode()
            return self._send(200, body, {"Content-Type": "application/json"})
        m = re.match(r"^/([^/]+/[^/]+)/resolve/([^/]+)/(.+)$", path)
        if m and self.cdn:
            return self._send(302, b"", {"Location": f"{self.cdn}/blob/{m.group(1)}/{m.group(3)}"})
        m = m or re.match(r"^/blob/([^/]+/[^/]+)()/(.+)$", path)
        if m:
            data = self.repos[m.group(1)]["files"].get(m.group(3))
            if data is None:
                return self._send(404)
            if self.headers.get("Range"):
                start = int(self.headers["Range"].split("=")[1].split("-")[0])
                return self._send(206, data[start:], {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"})
            return self._send(200, data)
        self._send(404)


class HfDownloadBase(TempDir):
    def start(self, cdn=False):
        api = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
        threading.Thread(target=api.serve_forever, daemon=True).start()
        self.addCleanup(api.server_close)
        self.addCleanup(api.shutdown)
        FakeHF.cdn = None
        if cdn:
            # a second server on another port plays the CDN; same handler, so it logs requests too
            other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeHF)
            threading.Thread(target=other.serve_forever, daemon=True).start()
            self.addCleanup(other.server_close)
            self.addCleanup(other.shutdown)
            FakeHF.cdn = f"http://127.0.0.1:{other.server_address[1]}"
        old = fetch.HF
        fetch.HF = f"http://127.0.0.1:{api.server_address[1]}"
        self.addCleanup(setattr, fetch, "HF", old)

    def setUp(self):
        super().setUp()
        FakeHF.seen = []
        self.index = json.dumps({"weight_map": {"a": "model-00001-of-00002.safetensors",
                                                "b": "model-00002-of-00002.safetensors"}}).encode()
        self.files = {"config.json": b'{"architectures": ["X"]}', "model.safetensors.index.json": self.index,
                      "model-00001-of-00002.safetensors": os.urandom(300_000),
                      "model-00002-of-00002.safetensors": os.urandom(120_000), "images/chart.png": b"png"}
        self.sha = "a" * 40
        FakeHF.repos = {"owner/name": {"sha": self.sha, "files": self.files,
                                       "lfs": {"model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"}}}
        self.hub = self.tmp / "hub"



class HfDownloadTests(HfDownloadBase):
    def test_snapshot_is_in_the_layout_every_recipe_reads(self):
        self.start()
        said = []
        res = fetch.hf_snapshot("owner/name", self.hub, say=said.append)
        root = self.hub / "models--owner--name"
        self.assertEqual(res["commit"], self.sha)
        self.assertEqual((root / "refs" / "main").read_text(), self.sha)
        snap = root / "snapshots" / self.sha
        self.assertEqual((snap / "config.json").read_bytes(), self.files["config.json"])
        big = self.files["model-00001-of-00002.safetensors"]
        self.assertEqual((snap / "model-00001-of-00002.safetensors").read_bytes(), big)
        self.assertTrue((snap / "config.json").is_symlink())
        # blobs are named by their hash, as huggingface_hub does: sha256 for big files, the git id for the rest
        self.assertTrue((root / "blobs" / hashlib.sha256(big).hexdigest()).is_file())
        self.assertTrue((root / "blobs" / hashlib.sha1(self.files["config.json"]).hexdigest()).is_file())
        self.assertFalse((snap / "images").exists(), "charts aren't part of the model")
        st = fetch.hf_snapshot_status(self.hub, "owner/name")
        self.assertTrue(st["complete"], st["detail"])
        self.assertEqual(fetch.hf_repo_bytes(root), sum(len(v) for p, v in self.files.items() if p != "images/chart.png"))
        said.clear()
        fetch.hf_snapshot("owner/name", self.hub, say=said.append)
        self.assertIn("to download: 0 B", said[0], "a second run downloads nothing")

    def test_a_damaged_or_missing_file_is_noticed_and_repaired(self):
        self.start()
        fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None)
        sha = hashlib.sha256(self.files["model-00002-of-00002.safetensors"]).hexdigest()
        blob = self.hub / "models--owner--name" / "blobs" / sha
        blob.write_bytes(blob.read_bytes()[:-5])
        st = fetch.hf_snapshot_status(self.hub, "owner/name")
        self.assertFalse(st["complete"])
        self.assertIn("missing", st["detail"])
        fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None)
        self.assertTrue(fetch.hf_snapshot_status(self.hub, "owner/name")["complete"])

    def test_the_token_reaches_huggingface_but_not_the_cdn(self):
        self.start(cdn=True)
        fetch.hf_snapshot("owner/name", self.hub, token="hf_secrettoken", say=lambda s: None)
        api_calls = [a for p, a in FakeHF.seen if p.startswith("/api/") or "/resolve/" in p]
        blob_calls = [a for p, a in FakeHF.seen if p.startswith("/blob/")]
        self.assertTrue(api_calls and blob_calls)
        self.assertTrue(all(a == "Bearer hf_secrettoken" for a in api_calls))
        self.assertTrue(all(a is None for a in blob_calls), "the signed CDN URL must not see the token")

    def test_a_wrong_checksum_is_refused(self):
        self.start()
        FakeHF.repos["owner/name"]["wrong_sha"] = {"model-00002-of-00002.safetensors"}
        with self.assertRaises(fetch.FetchError):
            fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None)
        self.assertFalse(fetch.hf_snapshot_status(self.hub, "owner/name")["complete"])

    def test_file_names_that_leave_the_folder_are_refused(self):
        self.start()
        FakeHF.repos["owner/evil"] = {"sha": "b" * 40, "files": {"../../escape.txt": b"x"}, "lfs": set()}
        with self.assertRaises(fetch.FetchError):
            fetch.hf_snapshot("owner/evil", self.hub, say=lambda s: None)
        self.assertFalse((self.tmp / "escape.txt").exists())

    def test_gated_and_missing_repos_say_what_to_do(self):
        self.start()
        FakeHF.gated = {"owner/gated"}
        self.addCleanup(setattr, FakeHF, "gated", set())
        with self.assertRaisesRegex(fetch.FetchError, "token"):
            fetch.hf_info("owner/gated")
        with self.assertRaisesRegex(fetch.FetchError, "no model"):
            fetch.hf_info("nobody/nothing")

    def test_a_cache_made_by_hf_download_is_recognised(self):
        # no manifest: judged by the weights the index names (what the TensorFold recipes leave behind)
        root = self.hub / "models--owner--name"
        snap = root / "snapshots" / self.sha
        snap.mkdir(parents=True)
        (root / "refs").mkdir()
        (root / "refs" / "main").write_text(self.sha)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors.index.json").write_bytes(self.index)
        self.assertFalse(fetch.hf_snapshot_status(self.hub, "owner/name")["complete"], "shards are missing")
        (snap / "model-00001-of-00002.safetensors").write_bytes(b"x")
        (snap / "model-00002-of-00002.safetensors").write_bytes(b"x")
        self.assertTrue(fetch.hf_snapshot_status(self.hub, "owner/name")["complete"])
        (root / "blobs").mkdir()
        (root / "blobs" / "abc.incomplete").write_bytes(b"x")
        self.assertFalse(fetch.hf_snapshot_status(self.hub, "owner/name")["complete"], "a download is still running")

    def test_a_pinned_commit_only_counts_that_commit(self):
        root = self.hub / "models--owner--name"
        snap = root / "snapshots" / self.sha
        snap.mkdir(parents=True)
        (snap / "config.json").write_text("{}")
        (snap / "model.safetensors").write_bytes(b"x")
        self.assertTrue(fetch.hf_snapshot_status(self.hub, "owner/name", self.sha)["complete"])
        other = fetch.hf_snapshot_status(self.hub, "owner/name", "c" * 40)
        self.assertFalse(other["complete"])
        self.assertIn("not at revision", other["detail"])

    def test_links_and_ids_are_parsed_strictly(self):
        ok = {
            "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct": ("Qwen/Qwen2.5-0.5B-Instruct", None),
            "huggingface.co/Mia-AiLab/Ornith-1.5-122B-EXL3-EAQv1-4-bpw/tree/main": ("Mia-AiLab/Ornith-1.5-122B-EXL3-EAQv1-4-bpw", None),
            "https://hf.co/a/b/blob/abc123/config.json?download=true": ("a/b", "abc123"),
            "  owner/name  ": ("owner/name", None),
            "owner/name@v1": ("owner/name", "v1"),
            "hf.co/owner/name@v1": ("owner/name", "v1"),
        }
        for text, want in ok.items():
            self.assertEqual(fetch.parse_hf_ref(text), want, text)
        for bad in ("", "../etc/passwd", "a/b/c", "notamodel", "https://huggingface.co/datasets/x/y",
                    "https://huggingface.co/owner", "owner/na me", "owner/name;rm -rf /", "owner/$(id)"):
            with self.assertRaises(fetch.FetchError, msg=bad):
                fetch.parse_hf_ref(bad)


# ─────────────────────────────── storage: sizes, shared files, deleting ───────────────────────────────

IMAGES = [
    {"ref": "tensorfold-qwen38:zig-db28187", "repository": "tensorfold-qwen38", "tag": "zig-db28187", "id": "sha256:aaa1",
     "size": 40_000, "unique": 30_000, "containers": 0},
    {"ref": "nvcr.io/nvidia/pytorch:26.07-py3", "repository": "nvcr.io/nvidia/pytorch", "tag": "26.07-py3", "id": "sha256:bbb2",
     "size": 20_000, "unique": 20_000, "containers": 0},
    {"ref": "eugr/spark-vllm-b12x:nightly-20261015", "repository": "eugr/spark-vllm-b12x", "tag": "nightly-20261015",
     "id": "sha256:ccc3", "size": 9_000, "unique": 9_000, "containers": 0},
    {"ref": "someone/elses-app:1", "repository": "someone/elses-app", "tag": "1", "id": "sha256:ddd4", "size": 500,
     "unique": 500, "containers": 1},
]


class StorageBase(TempDir):
    def make(self, installed=("tfflash",), active=None):
        os.environ["SWAPPER_PLATFORM"] = "nvidia-gb10"
        self.addCleanup(os.environ.pop, "SWAPPER_PLATFORM", None)
        self.home = self.tmp / "home"
        self.home.mkdir(exist_ok=True)
        cfg = self.tmp / "models.json"
        cfg.write_text(json.dumps(site(openclaw={"user": "pat", "home": str(self.home)})))
        eng = Engine(cfg, self.tmp / "state", catalog_path=CATALOG_PATH, conf_dir=self.tmp)
        eng.install_state = lambda key, fresh=False: {"installed": key in installed, "detail": ""}
        eng.status = lambda fresh=False: {"active": active, "loading": None,
                                          "memory": {"total": 121.0, "available": 100.0, "free": 90.0}}
        eng.container_exists = lambda name: False
        self.ran = []
        self.eng = eng
        st = storage.Storage(eng)
        st.docker_images = lambda: [dict(i) for i in IMAGES]
        st.container_images = lambda: {"someone-app": "someone/elses-app:1"}
        st.docker_usage = lambda: {"Images": {"count": 4, "bytes": 69_500, "reclaimable": 40_000}}
        old = storage.sh
        storage.sh = lambda cmd, **k: (self.ran.append(cmd), (0, ""))[1]
        self.addCleanup(setattr, storage, "sh", old)
        return eng, st

    def lay_out_flash(self):
        """What the TensorFold Flash Next install leaves in the home of the box's user."""
        d = self.home / "Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold"
        d.mkdir(parents=True)
        (d / "start.sh").write_text("#!/bin/sh\n")
        (d / "README.md").write_bytes(b"r" * 1000)
        cache = self.home / ".cache/tensorfold-qwen38"
        cache.mkdir(parents=True)
        (cache / "kernel.bin").write_bytes(b"k" * 5000)
        state = self.home / ".local/state/qwen38-flash-next-tf"
        state.mkdir(parents=True)
        (state / "prepared").write_text("ok")
        repo = self.home / ".cache/huggingface/hub" / f"models--{FP4.replace('/', '--')}"
        (repo / "blobs").mkdir(parents=True)
        (repo / "blobs" / "abc").write_bytes(b"w" * 100_000)
        (repo / "snapshots" / ("a" * 40)).mkdir(parents=True)
        (repo / "snapshots" / ("a" * 40) / "model.safetensors").symlink_to("../../blobs/abc")
        return d, cache, repo

    @staticmethod
    def part(items, pid):
        return next((p for p in items if p["id"] == pid), None)



class StorageTests(StorageBase):
    def test_what_a_model_leaves_on_the_disk(self):
        eng, st = self.make()
        fp = st.footprint(eng.models["tfflash"])
        self.assertIn(str(self.home / "Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold"), fp["dirs"])
        self.assertIn(str(self.home / ".cache/tensorfold-qwen38"), fp["dirs"])
        self.assertIn(FP4, fp["hf"])
        self.assertIn("tensorfold-qwen38", fp["images"])
        self.assertIn("swapper/tabbyapi-exl3:1.6.3-a270c55", st.footprint(eng.models["ornith"])["images"],
                      "the image the install builds")
        laguna = st.footprint(eng.models["laguna"])
        self.assertEqual(laguna["hf"], ["poolside/Laguna-S-2.1-NVFP4", "poolside/Laguna-S-2.1-DFlash-NVFP4"])
        self.assertIn("vllm/vllm-openai:v0.25.1", laguna["images"])

    def test_weights_two_models_share_are_kept_while_one_is_installed(self):
        eng, st = self.make(installed=("tfflash", "vllmfp4"))
        self.lay_out_flash()
        plan = st.plan_delete("tfflash")
        keep = self.part(plan["keep"], f"hf:{FP4}")
        self.assertIsNotNone(keep, "the checkpoint stays for the vLLM model")
        self.assertEqual(keep["kept_for"], [eng.models["vllmfp4"]["title"]])
        self.assertIsNotNone(self.part(plan["remove"], f"dir:{self.home}/Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold"))
        self.assertIsNotNone(self.part(plan["remove"], "image:tensorfold-qwen38"))
        self.assertEqual(plan["blockers"], [])
        # once the vLLM model isn't installed, the checkpoint is the biggest thing a delete gives back
        eng, st = self.make(installed=("tfflash",))
        plan = st.plan_delete("tfflash")
        self.assertEqual(self.part(plan["remove"], f"hf:{FP4}")["bytes"], 100_000)

    def test_an_image_another_container_uses_stays(self):
        eng, st = self.make(installed=("tfflash",))
        st.container_images = lambda: {"mine": "tensorfold-qwen38:zig-db28187",
                                       "qwen38-flash-next-tf": "tensorfold-qwen38:zig-db28187"}
        plan = st.plan_delete("tfflash")
        self.assertIsNone(self.part(plan["remove"], "image:tensorfold-qwen38"))
        self.assertIsNotNone(self.part(plan["keep"], "image:tensorfold-qwen38"))

    def test_deleting_removes_everything_it_listed_and_nothing_else(self):
        eng, st = self.make()
        d, cache, repo = self.lay_out_flash()
        mine = self.home / "notes.txt"
        mine.write_text("keep me")
        eng.save_state(measured={"tfflash": {"decode": 50}, "bolt": {"decode": 70}}, desired="tfflash")
        freed = st.delete_model("tfflash", a_job("delete:tfflash"))
        self.assertGreater(freed, 100_000)
        for gone in (d, cache, repo, self.home / ".local/state/qwen38-flash-next-tf"):
            self.assertFalse(gone.exists(), f"{gone} should be gone")
        self.assertEqual(mine.read_text(), "keep me")
        self.assertTrue((self.home / ".cache/huggingface/hub").is_dir(), "the cache folder itself stays")
        self.assertIn(["docker", "rmi", "tensorfold-qwen38:zig-db28187"], self.ran)
        self.assertNotIn(["docker", "rmi", "someone/elses-app:1"], self.ran)
        state = eng.load_state()
        self.assertNotIn("tfflash", state["measured"])
        self.assertIn("bolt", state["measured"])
        self.assertIsNone(state["desired"])

    def test_the_serving_model_cannot_be_deleted(self):
        eng, st = self.make(active="tfflash")
        d, cache, repo = self.lay_out_flash()
        self.assertTrue(st.plan_delete("tfflash")["blockers"])
        with self.assertRaises(storage.StorageError):
            st.delete_model("tfflash", a_job("delete:tfflash"))
        self.assertTrue(d.exists() and repo.exists())

    def test_nothing_outside_the_model_folders_can_be_removed(self):
        eng, st = self.make()
        job = a_job()
        outside = self.tmp / "elsewhere"
        outside.mkdir()
        (outside / "precious").write_text("x")
        (self.home / ".cache/huggingface/hub").mkdir(parents=True)
        for bad in (Path("/etc"), Path("/"), self.home, self.home / ".cache", self.home / ".cache/huggingface",
                    self.home / ".cache/huggingface/hub", outside, Path("relative/path")):
            with self.assertRaises(storage.StorageError, msg=str(bad)):
                st.remove_path(bad, job)
        self.assertTrue((outside / "precious").exists())
        # a link is only unlinked, never followed out of the folder
        (self.home / "model").symlink_to(outside)
        self.assertTrue(st.remove_path(self.home / "model", job))
        self.assertFalse((self.home / "model").exists())
        self.assertTrue((outside / "precious").exists())

    def test_old_downloads_nobody_accounts_for_are_listed_and_removable(self):
        eng, st = self.make()
        hub = self.home / ".cache/huggingface/hub"
        old = hub / "models--someone--old-checkpoint"
        (old / "blobs").mkdir(parents=True)
        (old / "blobs" / "x").write_bytes(b"o" * 70_000)
        (hub / f"models--{FP4.replace('/', '--')}" / "blobs").mkdir(parents=True)
        other = {o["name"]: o for o in st._scan()["other"]}
        self.assertEqual(other["someone/old-checkpoint"]["bytes"], 70_000)
        self.assertTrue(other["someone/old-checkpoint"]["removable"])
        self.assertNotIn(FP4, other, "a model's own checkpoint isn't 'other'")
        self.assertFalse(other["someone/elses-app:1"]["removable"], "an image a container uses isn't offered")
        self.assertNotIn("eugr/spark-vllm-b12x:nightly-20261015", other, "claimed by a model's pull step")
        job = a_job()
        st.delete_other("hf", "someone/old-checkpoint", job)
        self.assertFalse(old.exists())
        with self.assertRaises(storage.StorageError):
            st.delete_other("hf", FP4, job)                                   # belongs to a model in the list
        with self.assertRaises(storage.StorageError):
            st.delete_other("image", "tensorfold-qwen38:zig-db28187", job)

    def test_sizes_and_image_names(self):
        self.assertEqual(storage.parse_size("329.7MB"), 329_700_000)
        self.assertEqual(storage.parse_size("1.1GB"), 1_100_000_000)
        self.assertEqual(storage.parse_size("24.35kB"), 24_350)
        self.assertEqual(storage.parse_size("0B"), 0)
        self.assertEqual(storage.parse_size("N/A"), 0)
        self.assertEqual(storage.split_image_ref("host:5000/repo:tag"), ("host:5000/repo", "tag"))
        self.assertEqual(storage.split_image_ref("host:5000/repo"), ("host:5000/repo", ""))
        self.assertTrue(storage.image_matches("tensorfold-qwen38", "tensorfold-qwen38", "any"))
        self.assertTrue(storage.image_matches("a/b:1", "a/b", "1"))
        self.assertFalse(storage.image_matches("a/b:1", "a/b", "2"))
        self.assertTrue(storage.ref_used("a/b", {"a/b:latest"}))
        self.assertFalse(storage.ref_used("a/b:1", {"a/b:2"}))

    def test_scan_measures_each_model(self):
        eng, st = self.make()
        self.lay_out_flash()
        scan = st._scan()
        flash = next(m for m in scan["models"] if m["key"] == "tfflash")
        self.assertEqual({p["kind"] for p in flash["parts"]}, {"recipe", "cache", "weights", "image"})
        self.assertEqual(flash["bytes"], sum(p["bytes"] for p in flash["parts"]))
        self.assertEqual(eng.disk_sizes["tfflash"], flash["bytes"])
        self.assertTrue(scan["disks"] and scan["disks"][0]["free"] > 0)


# ─────────────────────────────── models added from Hugging Face ───────────────────────────────

class CustomModelBase(TempDir):
    def make(self, platform="nvidia-gb10"):
        os.environ["SWAPPER_PLATFORM"] = platform
        self.addCleanup(os.environ.pop, "SWAPPER_PLATFORM", None)
        cfg = self.tmp / "models.json"
        cfg.write_text(json.dumps(site(api_key="k-123", api_port=8899, openclaw={"user": "pat", "home": "/home/pat"})))
        eng = Engine(cfg, self.tmp / "state", catalog_path=CATALOG_PATH, conf_dir=self.tmp)
        eng.status = lambda fresh=False: {"active": None, "loading": None,
                                          "memory": {"total": 121.0, "available": 100.0, "free": 90.0}}
        eng.install_state = lambda key, fresh=False: {"installed": False, "detail": "x"}
        old = hfmodels.shutil.disk_usage
        hfmodels.shutil.disk_usage = lots_of_disk
        self.addCleanup(setattr, hfmodels.shutil, "disk_usage", old)
        return eng

    def fake_hf(self, files, tags=(), config=None, pipeline="text-generation", ctx=131072):
        info = {"id": "acme/model", "sha": "d" * 40, "gated": False, "private": False, "tags": list(tags),
                "library": None, "pipeline": pipeline, "license": "apache-2.0",
                "config": {"architectures": ["LlamaForCausalLM"]} if config is None else config,
                "safetensors": {}, "modified": None,
                "files": {p: {"size": n, "sha256": "f" * 64, "blob_id": "e" * 40} for p, n in files.items()}}
        old_info, old_ctx = fetch.hf_info, hfmodels.native_context
        fetch.hf_info = lambda repo, revision=None, token=None: info
        hfmodels.native_context = lambda repo, commit, token: ctx
        self.addCleanup(setattr, fetch, "hf_info", old_info)
        self.addCleanup(setattr, hfmodels, "native_context", old_ctx)



class CustomModelTests(CustomModelBase):
    def test_custom_models_are_saved_merged_and_removed(self):
        eng = self.make()
        entry = {"title": "Mine", "platforms": ["nvidia-gb10"], "kind": "docker", "container": "swapper-mine",
                 "served_id": "mine", "context": 4096, "openclaw_provider": "mine", "perf": {},
                 "docker_run": ["-e", "K={{API_KEY}}", "-v", "{{HOME}}/x:/x", "img", "--port", "{{API_PORT}}"],
                 "install": {"steps": []}}
        eng.add_custom_model("hf-mine", entry)
        m = eng.models["hf-mine"]
        self.assertEqual(m["source"], "custom")
        self.assertEqual(m["docker_run"], ["-e", "K=k-123", "-v", "/home/pat/x:/x", "img", "--port", "8899"])
        self.assertIn("model:hf-mine", Installer(eng).ACTIONS)
        again = Engine(self.tmp / "models.json", self.tmp / "state", catalog_path=CATALOG_PATH, conf_dir=self.tmp)
        self.assertIn("hf-mine", again.models, "a new process finds it again")
        eng.remove_custom_model("hf-mine")
        self.assertNotIn("hf-mine", eng.models)
        self.assertEqual(eng.load_custom(), {})

    def test_a_custom_model_can_never_take_a_catalog_models_place(self):
        eng = self.make()
        for bad in ("tfflash", "Bad Key!"):
            with self.assertRaises(ValueError, msg=bad):
                eng.add_custom_model(bad, {"title": "x"})
        eng.state_dir.joinpath("custom_models.json").write_text(json.dumps({"tfflash": {"title": "Hijack", "kind": "docker"}}))
        eng.reload_models()
        self.assertEqual(eng.models["tfflash"]["title"], "Qwen3.8 Flash Next")

    def test_the_references_other_modules_hold_follow_a_reload(self):
        eng = self.make()
        installer = Installer(eng)
        eng.add_custom_model("hf-a", {"title": "A", "platforms": ["nvidia-gb10"], "kind": "docker", "container": "c",
                                      "served_id": "a", "context": 1, "openclaw_provider": "a", "perf": {},
                                      "docker_run": ["x"], "install": {"steps": []}})
        self.assertIs(installer.cfg, eng.cfg)
        self.assertIn("hf-a", installer.cfg["models"])

    def test_a_vllm_repo_becomes_a_card_that_installs(self):
        eng = self.make()
        self.fake_hf({"config.json": 100, "model-00001-of-00002.safetensors": 8 * 1024**3,
                      "model-00002-of-00002.safetensors": 8 * 1024**3, "images/x.png": 50},
                     config={"architectures": ["LlamaForCausalLM"], "quantization_config": {"quant_method": "awq", "bits": 4}})
        info = hfmodels.inspect(eng, "https://huggingface.co/acme/model")
        self.assertEqual((info["format"], info["profile"], info["supported"]), ("safetensors", "vllm-spark", True))
        self.assertEqual(info["commit"], "d" * 40)
        key, entry = hfmodels.build(eng, "acme/model", {"context": 32768, "title": "  Acme  ",
                                                         "extra_args": "--tool-call-parser hermes --enable-auto-tool-choice",
                                                         "trust_remote_code": True})
        self.assertEqual(key, "hf-acme-model")
        eng.add_custom_model(key, entry)
        m = eng.models[key]
        a = m["docker_run"]
        self.assertEqual(m["title"], "Acme")
        self.assertEqual(a[a.index("--served-model-name") + 1], "model")
        self.assertEqual(a[a.index("--max-model-len") + 1], "32768")
        self.assertEqual(a[a.index("--revision") + 1], "d" * 40, "pinned to the commit that was looked at")
        self.assertEqual(a[a.index("--host") + 1], "127.0.0.1")
        self.assertEqual(a[a.index("--api-key") + 1], "k-123")
        self.assertEqual(a[a.index("--port") + 1], "8899")
        self.assertIn("acme/model", a)
        self.assertTrue({"--tool-call-parser", "hermes", "--trust-remote-code"} <= set(a))
        self.assertGreaterEqual(float(a[a.index("--gpu-memory-utilization") + 1]), 0.4)
        self.assertEqual(m["container"], "swapper-hf-acme-model")
        self.assertEqual(Engine.model_hf_repos(m), ["acme/model"])
        self.assertEqual([s["do"] for s in m["install"]["steps"]], ["mkdirs", "docker_pull", "hf_snapshot"])
        self.assertEqual(m["install"]["steps"][-1]["revision"], "d" * 40)
        self.assertTrue(m["experimental"])
        self.assertEqual(m["install"]["size_gb"], 18 + 12, "16 GiB of weights plus the engine image")
        with self.assertRaisesRegex(hfmodels.ModelError, "already"):
            hfmodels.build(eng, "acme/model")

    def test_extra_arguments_are_data_never_shell(self):
        self.assertEqual(hfmodels.clean_extra_args("--a 1 '--b c;d' $(x)"), ["--a", "1", "--b c;d", "$(x)"])
        self.assertEqual(hfmodels.clean_extra_args("  "), [])
        for bad in ("--a \x00", "'unterminated", " ".join(["x"] * 40)):
            with self.assertRaises(hfmodels.ModelError, msg=bad):
                hfmodels.clean_extra_args(bad)

    def test_an_exl3_repo_runs_on_tabbyapi(self):
        eng = self.make()
        self.fake_hf({"config.json": 100, "model-00001-of-00001.safetensors": 20 * 1024**3, "quantization_config.json": 10},
                     tags=["exl3"], config={"architectures": ["Qwen3_5MoeForConditionalGeneration"],
                                            "quantization_config": {"quant_method": "exl3", "bits": 4}})
        info = hfmodels.inspect(eng, "acme/Big-EXL3")
        self.assertEqual((info["format"], info["profile"]), ("exl3", "exllamav3"))
        key, entry = hfmodels.build(eng, "acme/Big-EXL3", {"context": 8192})
        eng.add_custom_model(key, entry)
        m = eng.models[key]
        a = m["docker_run"]
        self.assertIn("TABBY_CTX=8192", a)
        self.assertIn("TABBY_API_KEY=k-123", a)
        self.assertIn("/home/pat/.cache/huggingface/hub/models--acme--Big-EXL3:/model:ro", a)
        self.assertIn("set -e", a[a.index("-c") + 1], "the entrypoint script was filled in from the catalog")
        self.assertIn({"do": "docker_build", "build": "tabby-exl3"}, m["install"]["steps"])
        self.assertFalse(m["template_kwargs"])
        self.assertFalse(m["vision"])

    def test_gguf_on_a_strix_halo_uses_llamacpp(self):
        eng = self.make("amd-strix-halo")
        self.fake_hf({"m-Q4_K_M.gguf": 5 * 1024**3, "m-Q8_0-00001-of-00002.gguf": 6 * 1024**3,
                      "m-Q8_0-00002-of-00002.gguf": 4 * 1024**3, "mmproj-m.gguf": 100}, config={})
        info = hfmodels.inspect(eng, "acme/m-GGUF")
        self.assertEqual((info["format"], info["profile"]), ("gguf", "llamacpp-gguf"))
        self.assertEqual([g["label"] for g in info["gguf"]], ["m-Q4_K_M", "m-Q8_0"],
                         "shards form one choice; mmproj isn't one")
        with self.assertRaises(hfmodels.ModelError):
            hfmodels.build(eng, "acme/m-GGUF")           # a file has to be chosen
        key, entry = hfmodels.build(eng, "acme/m-GGUF", {"gguf": "m-Q8_0.gguf", "context": 16384})
        eng.add_custom_model(key, entry)
        m = eng.models[key]
        self.assertEqual([f["path"] for f in m["files"]], ["m-Q8_0-00001-of-00002.gguf", "m-Q8_0-00002-of-00002.gguf"])
        self.assertEqual(m["model_file"], "acme/m-GGUF/m-Q8_0-00001-of-00002.gguf")
        self.assertEqual(m["args"][m["args"].index("--ctx-size") + 1], "16384")
        self.assertTrue(m["unit"].startswith("swapper-llama-hf-"))

    def test_what_cannot_run_here_is_said_plainly(self):
        eng = self.make()
        self.fake_hf({"model.gguf": 1024**3}, config={})
        info = hfmodels.inspect(eng, "acme/m-GGUF")
        self.assertFalse(info["supported"])
        self.assertIn("GGUF", " ".join(info["problems"]))
        self.fake_hf({"model.safetensors": 300 * 1024**3})        # more than the box has
        info = hfmodels.inspect(eng, "acme/huge")
        self.assertFalse(info["supported"])
        self.assertIn("memory", " ".join(info["problems"]))
        with self.assertRaises(hfmodels.ModelError):
            hfmodels.build(eng, "acme/huge")
        halo = self.make("amd-strix-halo")
        self.fake_hf({"model.safetensors": 1024**3})
        self.assertIn("GGUF", " ".join(hfmodels.inspect(halo, "acme/x")["problems"]))

    def test_a_model_the_catalog_already_covers_is_recognised(self):
        eng = self.make()
        self.fake_hf({"config.json": 10, "model.safetensors": 1024**3})
        info = hfmodels.inspect(eng, "https://huggingface.co/Mia-AiLab/Ornith-1.5-122B-EXL3-EAQv1-4-bpw")
        self.assertEqual(info["catalog_key"], "ornith")
        self.assertEqual(info["catalog_title"], "Ornith 1.5 122B")
        self.assertIn(hfmodels.inspect(eng, FP4)["catalog_key"], ("tfflash", "vllmfp4"))
        with self.assertRaisesRegex(hfmodels.ModelError, "already"):
            hfmodels.build(eng, "Mia-AiLab/Ornith-1.5-122B-EXL3-EAQv1-4-bpw")

    def test_keys_are_stable_and_short(self):
        self.assertEqual(hfmodels.model_key("Mia-AiLab/Ornith-1.5-122B"), "hf-mia-ailab-ornith-1-5-122b")
        long_key = hfmodels.model_key("o" * 90 + "/" + "n" * 90)
        self.assertLessEqual(len(long_key), 60)
        self.assertRegex(long_key, r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


# ─────────────────────────────── installer steps for the new kinds ───────────────────────────────

class InstallStepTests(TempDir):
    def make(self, mem_available=100.0):
        import installer as installer_mod
        os.environ["SWAPPER_PLATFORM"] = "nvidia-gb10"
        self.addCleanup(os.environ.pop, "SWAPPER_PLATFORM", None)
        cfg = self.tmp / "models.json"
        cfg.write_text(json.dumps(site(openclaw={"user": "pat", "home": str(self.tmp / "home")})))
        eng = Engine(cfg, self.tmp / "state", catalog_path=CATALOG_PATH, conf_dir=self.tmp)
        ins = Installer(eng)
        self.calls = []
        ins._stream = lambda job, argv, **k: (self.calls.append(argv), 0)[1]
        self.label = (1, "")
        old_sh, old_mem, old_disk = installer_mod.sh, installer_mod.meminfo, shutil.disk_usage
        installer_mod.sh = lambda cmd, **k: self.label
        installer_mod.meminfo = lambda: {"available": mem_available, "total": 121.0, "free": 4.0}
        shutil.disk_usage = lots_of_disk
        self.addCleanup(setattr, installer_mod, "sh", old_sh)
        self.addCleanup(setattr, installer_mod, "meminfo", old_mem)
        self.addCleanup(setattr, shutil, "disk_usage", old_disk)
        return eng, ins

    def test_the_image_is_built_once_and_again_when_its_recipe_changes(self):
        eng, ins = self.make()
        step = {"do": "docker_build", "build": "tabby-exl3"}
        ins._step_docker_build(a_job("x", ["run"]), eng.models["ornith"], step)
        self.assertEqual(len(self.calls), 1)
        argv = self.calls[0]
        self.assertEqual(argv[:4], ["docker", "build", "--tag", "swapper/tabbyapi-exl3:1.6.3-a270c55"])
        label = next(a for a in argv if a.startswith("swapper.build="))
        self.assertIn("EXLLAMAV3_REF=v1.6.3", argv)
        self.label = (0, label.split("=", 1)[1])            # the same recipe is already built → nothing runs
        ins._step_docker_build(a_job("x", ["run"]), eng.models["ornith"], step)
        self.assertEqual(len(self.calls), 1)
        self.label = (0, "0123456789ab")                    # a changed Dockerfile hashes differently → rebuilt
        ins._step_docker_build(a_job("x", ["run"]), eng.models["ornith"], step)
        self.assertEqual(len(self.calls), 2)

    def test_building_needs_free_memory_because_a_running_model_shares_it(self):
        eng, ins = self.make(mem_available=8.0)
        with self.assertRaisesRegex(InstallFailed, "free memory"):
            ins._step_docker_build(a_job("x", ["run"]), eng.models["ornith"], {"do": "docker_build", "build": "tabby-exl3"})
        self.assertEqual(self.calls, [])

    def test_hf_snapshot_step_skips_what_is_already_there(self):
        eng, ins = self.make()
        eng.hf_status = lambda repo, revision=None, home=None: {"complete": True, "present": True, "hub": "/x", "detail": ""}
        said = []
        job = a_job("x", ["run"])
        job.say = said.append
        called = []
        old = fetch.hf_snapshot
        fetch.hf_snapshot = lambda *a, **k: called.append(a)
        self.addCleanup(setattr, fetch, "hf_snapshot", old)
        ins._step_hf_snapshot(job, eng.models["vllmfp4"], {"do": "hf_snapshot", "repo": FP4})
        self.assertEqual(called, [])
        self.assertIn("already downloaded", said[0])
        eng.hf_status = lambda repo, revision=None, home=None: {"complete": False, "present": False, "detail": "not downloaded"}
        ins._step_hf_snapshot(job, eng.models["vllmfp4"], {"do": "hf_snapshot", "repo": FP4, "revision": "b" * 40})
        self.assertEqual(len(called), 1)
        self.assertEqual(called[0][0], FP4)

    def test_cache_folders_are_made_for_the_user_not_for_docker(self):
        eng, ins = self.make()
        target = self.tmp / "home" / ".cache" / "vllm"
        ins._step_mkdirs(a_job("x", ["run"]), eng.models["vllmfp4"], {"do": "mkdirs", "paths": [str(target)]})
        self.assertTrue(target.is_dir())

    def test_docker_models_check_the_key_themselves_and_stay_off_the_network(self):
        import engine as engine_mod
        self.assertEqual(engine_mod.DOCKER_KINDS, ("docker", "sglang-docker"))
        eng, _ = self.make()
        for key in ("laguna", "ornith", "vllmfp4", "standard"):
            self.assertIn(eng.models[key]["kind"], engine_mod.DOCKER_KINDS)
            self.assertTrue(eng.own_key(eng.models[key]), f"{key}'s server checks the API key itself")
            args = eng.models[key]["docker_run"]
            self.assertEqual(args.count("--api-key") + sum(a.startswith("TABBY_API_KEY=") for a in args), 1, key)
        self.assertEqual(Engine._chat_extras(eng.models["ornith"]), {})
        self.assertEqual(Engine._chat_extras(eng.models["laguna"]), {"chat_template_kwargs": {"enable_thinking": False}})


# ─────────────────────────────── review findings: things that must stay fixed ───────────────────────────────

class StorageSafetyTests(StorageBase):
    def test_a_cache_another_model_mounts_is_never_deleted_from_under_it(self):
        eng, st = self.make(installed=("vllmfp4", "laguna", "hf-x"))
        eng.add_custom_model("hf-x", {
            "title": "X", "platforms": ["nvidia-gb10"], "kind": "docker", "container": "swapper-hf-x", "served_id": "x",
            "context": 1, "openclaw_provider": "x", "perf": {}, "install": {"steps": []},
            "docker_run": ["-v", "{{HOME}}/.cache/vllm:/root/.cache/vllm", "-v", "{{HOME}}/.cache/flashinfer:/f", "img"]})
        for d in (".cache/vllm", ".cache/b12x", ".cache/flashinfer"):
            (self.home / d).mkdir(parents=True)
            (self.home / d / "kernels.bin").write_bytes(b"k" * 100)
        holders = st.holders()
        self.assertEqual(holders[f"dir:{self.home}/.cache/vllm"], {"vllmfp4", "hf-x"})
        self.assertEqual(holders[f"dir:{self.home}/.cache/flashinfer"], {"laguna", "vllmfp4", "hf-x"},
                         "mounted by two more models although only Laguna declares it")
        plan = st.plan_delete("vllmfp4")
        self.assertIsNotNone(self.part(plan["keep"], f"dir:{self.home}/.cache/vllm"), "the custom model still mounts it")
        self.assertIsNone(self.part(plan["remove"], f"dir:{self.home}/.cache/vllm"))
        self.assertIsNotNone(self.part(plan["remove"], f"dir:{self.home}/.cache/b12x"), "nobody else mounts this one")
        plan = st.plan_delete("laguna")
        self.assertIsNotNone(self.part(plan["keep"], f"dir:{self.home}/.cache/flashinfer"))

    def test_a_delete_that_cannot_be_carried_out_is_refused_before_anything_is_touched(self):
        eng, st = self.make()
        elsewhere = self.tmp / "other-disk"
        (elsewhere / "tensorfold-qwen38").mkdir(parents=True)
        (elsewhere / "tensorfold-qwen38" / "k").write_text("x")
        recipe = self.home / "Qwen3.8-Flash-Next-Single-DGX-Spark-TensorFold"
        recipe.mkdir(parents=True)
        (recipe / "start.sh").write_text("x")
        (self.home / ".cache").symlink_to(elsewhere)             # ~/.cache lives on another disk
        plan = st.plan_delete("tfflash")
        self.assertTrue(any("tensorfold-qwen38" in b for b in plan["blockers"]), plan["blockers"])
        with self.assertRaises(storage.StorageError):
            st.delete_model("tfflash", a_job("delete:tfflash"))
        self.assertTrue(recipe.exists(), "nothing was removed before the refusal")
        self.assertEqual(self.ran, [], "and no docker command ran")
        self.assertTrue((elsewhere / "tensorfold-qwen38" / "k").exists())

    def test_a_repo_that_is_a_link_to_another_disk_frees_nothing_and_says_so(self):
        eng, st = self.make()
        disk2 = self.tmp / "disk2"
        data = disk2 / f"models--{FP4.replace('/', '--')}"
        (data / "blobs").mkdir(parents=True)
        (data / "blobs" / "abc").write_bytes(b"w" * 50_000)
        hub = self.home / ".cache/huggingface/hub"
        hub.mkdir(parents=True)
        (hub / data.name).symlink_to(data)
        parts = st._model_parts("tfflash", eng.models["tfflash"], [], st.holders())
        weights = self.part(parts, f"hf:{FP4}")
        self.assertEqual(weights["bytes"], 0)
        self.assertIn("link", weights["label"])
        self.assertEqual(storage.dir_bytes(hub / data.name), 0)
        st.delete_model("tfflash", a_job("delete:tfflash"))
        self.assertFalse((hub / data.name).exists() or (hub / data.name).is_symlink())
        self.assertEqual((data / "blobs" / "abc").read_bytes(), b"w" * 50_000, "only the link was removed")

    def test_a_link_outside_the_model_folders_is_not_removed_either(self):
        eng, st = self.make()
        out = self.tmp / "elsewhere"
        out.mkdir()
        (out / "lnk").symlink_to(self.tmp)
        with self.assertRaises(storage.StorageError):
            st.remove_path(out / "lnk", a_job())
        self.assertTrue((out / "lnk").is_symlink())

    def test_an_image_known_by_another_name_or_its_id_is_still_a_models(self):
        eng, st = self.make()
        long_id = "sha256:" + "ab" * 32
        mine = dict(IMAGES[0], id=long_id)
        alias = dict(IMAGES[0], id=long_id, ref="myalias:1", repository="myalias", tag="1")
        st.docker_images = lambda: [mine, alias] + [dict(i) for i in IMAGES[1:]]
        job = a_job()
        for name in ("myalias:1", long_id, "abababababab", "sha256:abababababab"):
            with self.assertRaises(storage.StorageError, msg=name):
                st.delete_other("image", name, job)
        self.assertEqual([c for c in self.ran if c[:2] == ["docker", "rmi"]], [])

    def test_the_openclaw_provider_stays_while_any_of_its_defaults_uses_it(self):
        eng, st = self.make(installed=("tfflash", "tf27b"))
        eng.cfg["openclaw"]["bin"] = "/usr/bin/openclaw"
        eng.openclaw_installed = lambda: True
        eng.openclaw_config = lambda: {"models": {"providers": {"tfflash": {}}}}
        eng._as_oc_user = lambda cmd: cmd
        eng.openclaw_refs = lambda: ["tf27b/Qwen3.8-27B", "tfflash/Qwen3.8-Flash-Next"]       # e.g. imageModel
        job = a_job()
        st._forget_openclaw_provider(eng.models["tfflash"], job)
        self.assertEqual(self.ran, [], "still in use as the image model")
        eng.openclaw_refs = lambda: ["tf27b/Qwen3.8-27B"]
        st._forget_openclaw_provider(eng.models["tfflash"], job)
        self.assertEqual(self.ran, [["/usr/bin/openclaw", "config", "unset", "models.providers.tfflash"]])


class DownloadSafetyTests(HfDownloadBase):
    """HfDownloadTests' fake Hugging Face, with the cases the review found. (Subclassing re-runs the parent's
    tests once more; they are quick.)"""

    def test_a_pinned_older_commit_does_not_move_main_back(self):
        self.start()
        root = self.hub / "models--owner--name"
        fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None)
        self.assertEqual((root / "refs" / "main").read_text(), self.sha)
        newer = "b" * 40
        FakeHF.repos["owner/name"]["sha"] = newer
        fetch.hf_snapshot("owner/name", self.hub, revision=newer, say=lambda s: None)      # a pin of another commit
        self.assertEqual((root / "refs" / "main").read_text(), self.sha, "a pinned download leaves main alone")
        fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None)                      # an unpinned one is "latest"
        self.assertEqual((root / "refs" / "main").read_text(), newer)
        FakeHF.repos["owner/name"]["sha"] = self.sha
        fetch.hf_snapshot("owner/name", self.hub, revision=self.sha, say=lambda s: None)   # pin the OLDER one again
        self.assertEqual((root / "refs" / "main").read_text(), newer, "…so a pinned older commit can't rewind it")
        self.assertTrue(fetch.hf_snapshot_status(self.hub, "owner/name", self.sha)["complete"])
        self.assertTrue(fetch.hf_snapshot_status(self.hub, "owner/name", newer)["complete"])
        (root / "refs" / "main").unlink()
        fetch.hf_snapshot("owner/name", self.hub, revision=self.sha, say=lambda s: None)
        self.assertEqual((root / "refs" / "main").read_text(), self.sha, "a missing main is filled in")

    def test_nothing_is_written_through_links_planted_in_the_cache(self):
        self.start()
        outside = self.tmp / "outside"
        outside.mkdir()
        precious = outside / "precious.txt"
        precious.write_text("keep")
        root = self.hub / "models--owner--name"
        (root / "refs").mkdir(parents=True)
        (root / "refs" / "main").symlink_to(precious)                       # main -> a file elsewhere
        (root / f"swapper-manifest.{self.sha}.json").symlink_to(precious)   # the manifest too
        fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None)
        self.assertEqual(precious.read_text(), "keep")
        self.assertFalse((root / "refs" / "main").is_symlink())
        self.assertEqual((root / "refs" / "main").read_text(), self.sha)
        # a directory the download needs, replaced by a link, stops it
        other = self.tmp / "other"
        root2 = self.hub / "models--owner--second"
        FakeHF.repos["owner/second"] = FakeHF.repos["owner/name"]
        (root2).mkdir(parents=True)
        other.mkdir()
        (root2 / "refs").symlink_to(other)
        with self.assertRaisesRegex(fetch.FetchError, "link"):
            fetch.hf_snapshot("owner/second", self.hub, say=lambda s: None)
        self.assertEqual(list(other.iterdir()), [])

    def test_a_repo_folder_that_is_a_link_must_be_the_box_users(self):
        self.start()
        disk2 = self.tmp / "disk2"
        disk2.mkdir()
        self.hub.mkdir()
        (self.hub / "models--owner--name").symlink_to(disk2)
        with self.assertRaisesRegex(fetch.FetchError, "isn't the box user's"):
            fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None, owner=(os.getuid() + 1, os.getgid()))
        self.assertEqual(list(disk2.iterdir()), [])
        fetch.hf_snapshot("owner/name", self.hub, say=lambda s: None, owner=(os.getuid(), os.getgid()))   # their own: fine
        self.assertTrue((disk2 / "snapshots" / self.sha / "config.json").exists())

    def test_the_same_weights_in_other_formats_are_not_downloaded(self):
        self.start()
        FakeHF.repos["owner/multi"] = {"sha": "c" * 40, "lfs": {"model.safetensors", "pytorch_model.bin", "original/consolidated.00.pth"},
                                       "files": {"config.json": b"{}", "model.safetensors": os.urandom(5000),
                                                 "pytorch_model.bin": os.urandom(5000),
                                                 "original/consolidated.00.pth": os.urandom(5000), "tf_model.h5": b"h"}}
        fetch.hf_snapshot("owner/multi", self.hub, say=lambda s: None)
        snap = self.hub / "models--owner--multi" / "snapshots" / ("c" * 40)
        self.assertEqual(sorted(p.name for p in snap.rglob("*") if p.is_symlink()), ["config.json", "model.safetensors"])
        # a repo with only .bin keeps it
        FakeHF.repos["owner/binonly"] = {"sha": "d" * 40, "lfs": {"pytorch_model.bin"},
                                         "files": {"config.json": b"{}", "pytorch_model.bin": os.urandom(5000)}}
        fetch.hf_snapshot("owner/binonly", self.hub, say=lambda s: None)
        self.assertTrue((self.hub / "models--owner--binonly" / "snapshots" / ("d" * 40) / "pytorch_model.bin").exists())

    def test_whatever_json_a_repo_returns_the_lookup_takes_only_expected_types(self):
        self.start()
        FakeHF.repos["owner/odd"] = {"sha": "e" * 40, "files": {"model.safetensors": b"x" * 10}, "lfs": set(),
                                     "config": {"quantization_config": ["not", "a", "dict"], "architectures": "NotAList"},
                                     "tags": ["ok", 5, None]}
        info = fetch.hf_info("owner/odd")
        self.assertNotIn("quantization_config", info["config"])
        self.assertNotIn("architectures", info["config"])
        self.assertEqual(info["tags"], ["ok"], "tags that aren't strings are dropped")


class ReloadAndStatusTests(TempDir):
    def make(self):
        os.environ["SWAPPER_PLATFORM"] = "nvidia-gb10"
        self.addCleanup(os.environ.pop, "SWAPPER_PLATFORM", None)
        self.cfg = self.tmp / "models.json"
        self.cfg.write_text(json.dumps(site(api_key="k-123", openclaw={"user": "pat", "home": str(self.tmp / "home")})))
        return Engine(self.cfg, self.tmp / "state", catalog_path=CATALOG_PATH, conf_dir=self.tmp)

    ENTRY = {"title": "A", "platforms": ["nvidia-gb10"], "kind": "docker", "container": "c", "served_id": "a",
             "context": 1, "openclaw_provider": "a", "perf": {}, "docker_run": ["--api-key", "{{API_KEY}}"],
             "install": {"steps": []}}

    def test_an_add_that_fails_persists_nothing(self):
        import config as config_mod
        eng = self.make()
        old = config_mod.merge
        config_mod.merge = lambda *a, **k: (_ for _ in ()).throw(config_mod.ConfigError("boom"))
        self.addCleanup(setattr, config_mod, "merge", old)
        with self.assertRaises(config_mod.ConfigError):
            eng.add_custom_model("hf-a", dict(self.ENTRY))
        self.assertEqual(eng.load_custom(), {}, "the entry was not saved")
        self.assertNotIn("hf-a", eng.models)

    def test_edits_to_the_site_file_wait_for_a_restart(self):
        eng = self.make()
        self.cfg.write_text(json.dumps(site(api_key="changed", openclaw={"user": "pat", "home": "/elsewhere"})))
        eng.add_custom_model("hf-a", dict(self.ENTRY))
        self.assertEqual(eng.key, "k-123")
        self.assertEqual(eng.models["hf-a"]["docker_run"], ["--api-key", "k-123"])
        self.cfg.write_text("{ half-edited")
        eng.remove_custom_model("hf-a")           # a broken file doesn't get in the way either
        self.assertNotIn("hf-a", eng.models)

    def test_a_model_removed_during_a_status_poll_does_not_fail_the_poll(self):
        eng = self.make()
        real = eng.model_summary

        def flaky(key, *a, **k):
            if key == "ornith":
                raise KeyError(key)               # the model vanished between listing and looking it up
            return real(key, *a, **k)
        eng.model_summary = flaky
        st = eng.status(fresh=True)
        self.assertNotIn("ornith", st["models"])
        self.assertIn("tfflash", st["models"])

    def test_only_the_box_users_hugging_face_cache_counts_as_downloaded(self):
        eng = self.make()
        other = self.tmp / "hf-home"
        os.environ["HF_HOME"] = str(other)
        self.addCleanup(os.environ.pop, "HF_HOME", None)
        for repo in ("poolside/Laguna-S-2.1-NVFP4",):
            d = other / "hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / ("a" * 40)
            d.mkdir(parents=True)
            (d / "config.json").write_text("{}")
            (d / "model.safetensors").write_text("x")
        self.assertFalse(eng.hf_status("poolside/Laguna-S-2.1-NVFP4")["complete"], "the containers don't mount it")
        self.assertTrue(any(h == other / "hub" for h in eng.hf_hubs()), "but Storage still lists what's there")


class GgufRepoTests(CustomModelBase):
    def test_a_repo_with_many_quantizations_is_judged_by_the_one_you_pick(self):
        eng = self.make("amd-strix-halo")
        files = {f"m-Q{i}.gguf": n * 1024**3 for i, n in enumerate((13, 20, 34, 50, 75, 110, 150, 200), 2)}
        self.fake_hf(files, config={})
        hfmodels.shutil.disk_usage = lambda p: types.SimpleNamespace(total=10**12, used=0, free=60 * 10**9)
        info = hfmodels.inspect(eng, "acme/m-GGUF")
        self.assertTrue(info["supported"], info["problems"])
        self.assertEqual(info["size_bytes"], 13 * 1024**3, "the smallest quantization")
        self.assertEqual(info["size_max_bytes"], 200 * 1024**3)
        with self.assertRaisesRegex(hfmodels.ModelError, "memory"):
            hfmodels.build(eng, "acme/m-GGUF", {"gguf": "m-Q9.gguf"})          # 200 GiB: more than the box has
        with self.assertRaisesRegex(hfmodels.ModelError, "free"):
            hfmodels.build(eng, "acme/m-GGUF", {"gguf": "m-Q6.gguf"})          # 75 GiB fits in memory, not on the disk
        key, entry = hfmodels.build(eng, "acme/m-GGUF", {"gguf": "m-Q2.gguf"})
        self.assertEqual(entry["files"], [{"repo": "acme/m-GGUF", "path": "m-Q2.gguf", "revision": "d" * 40}],
                         "pinned to the commit that was looked at")

    def test_duplicate_weight_formats_do_not_inflate_the_size_or_memory(self):
        eng = self.make()
        self.fake_hf({"model.safetensors": 8 * 1024**3, "pytorch_model.bin": 8 * 1024**3,
                      "original/consolidated.00.pth": 8 * 1024**3, "config.json": 100})
        info = hfmodels.inspect(eng, "acme/model")
        self.assertEqual(info["size_bytes"], 8 * 1024**3)

    def test_odd_quantization_config_values_cannot_crash_the_lookup(self):
        eng = self.make()
        for odd in (["a"], "gptq", 5, {"quant_method": ["x"], "bits": "four"}):
            self.fake_hf({"model.safetensors": 1024**3}, config={"architectures": ["LlamaForCausalLM"],
                                                                 "quantization_config": odd})
            info = hfmodels.inspect(eng, "acme/model")
            self.assertEqual(info["format"], "safetensors", odd)

    def test_the_gguf_install_uses_the_saved_token_and_the_pinned_commit(self):
        eng = self.make("amd-strix-halo")
        ins = Installer(eng)
        eng.hf_token = lambda: "hf_tok"
        seen = {}
        old_list, old_dl = fetch.hf_listing, fetch.download
        fetch.hf_listing = lambda repo, revision=None, token=None: (seen.update(listing=(repo, revision, token)),
                                                                    {"m.gguf": {"size": 10, "sha256": None}})[1]

        def fake_download(url, dest, **k):
            seen["url"], seen["headers"] = url, k.get("headers")
            Path(dest).parent.mkdir(parents=True, exist_ok=True)
            Path(dest).write_bytes(b"x" * 10)
        fetch.download = fake_download
        self.addCleanup(setattr, fetch, "hf_listing", old_list)
        self.addCleanup(setattr, fetch, "download", old_dl)
        model = {"files": [{"repo": "a/b", "path": "m.gguf", "revision": "c" * 40}]}
        eng.file_verified = lambda repo, path: False
        eng.models_dir.mkdir(parents=True, exist_ok=True)
        ins._step_hf_files(a_job("x", ["run"]), model, {"do": "hf_files"})
        self.assertEqual(seen["listing"], ("a/b", "c" * 40, "hf_tok"))
        self.assertIn("/a/b/resolve/" + "c" * 40 + "/m.gguf", seen["url"])
        self.assertEqual(seen["headers"], {"Authorization": "Bearer hf_tok"})


# ─────────────────────────────── HTTP API (demo mode) ───────────────────────────────

class StorageApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(__import__("tempfile").mkdtemp(prefix="swapper-api2-"))
        port = free_port()
        env = {**os.environ, "SWAPPER_DEMO": "1", "SWAPPER_DEMO_HTTP": "1", "SWAPPER_BIND": "127.0.0.1",
               "SWAPPER_PORT": str(port), "SWAPPER_CONF": str(cls.tmp / "conf"), "SWAPPER_STATE": str(cls.tmp / "state")}
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "swapper" / "app.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{port}"
        for _ in range(100):
            try:
                urllib.request.urlopen(cls.base + "/api/session", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, path, body=None, headers=None, cookie=None):
        h = {"X-Swapper": "1", "Content-Type": "application/json", **(headers or {})}
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}")

    def login(self, user="admin", password="admin") -> str:
        req = urllib.request.Request(self.base + "/api/login", data=json.dumps({"username": user, "password": password}).encode(),
                                     headers={"X-Swapper": "1", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.headers["Set-Cookie"].split(";", 1)[0]

    def wait_job(self, cookie, timeout=20):
        for _ in range(timeout * 10):
            code, body = self.call("/api/provisioning/job", cookie=cookie)
            if body["job"] and body["job"]["state"] != "running":
                return body["job"]
            time.sleep(0.1)
        self.fail("the job never finished")

    def test_storage_is_measured_in_the_background(self):
        cookie = self.login()
        code, data = self.call("/api/storage", cookie=cookie)
        self.assertEqual(code, 200)
        for _ in range(100):
            if not data["scanning"]:
                break
            time.sleep(0.1)
            code, data = self.call("/api/storage", cookie=cookie)
        self.assertFalse(data["scanning"])
        self.assertTrue(data["disks"] and data["models"])
        self.assertGreater(data["models_total"], 0)
        self.assertTrue(all(m["parts"] for m in data["models"]))
        code, st = self.call("/api/status", cookie=cookie)
        self.assertTrue(st["disk"]["total"] > st["disk"]["free"] > 0)

    def test_an_app_connection_cannot_touch_storage_models_or_the_token(self):
        cookie = self.login()
        code, made = self.call("/api/tokens/create", {"label": "t"}, cookie=cookie)
        bearer = {"Authorization": f"Bearer {made['token']}"}
        self.assertEqual(self.call("/api/storage", headers=bearer)[0], 403)
        for path, body in (("/api/models/delete", {"key": "standard"}), ("/api/storage/remove", {"kind": "builder", "name": "x"}),
                           ("/api/hf/inspect", {"url": "a/b"}), ("/api/hf/add", {"url": "a/b"}), ("/api/hf/token", {"token": ""})):
            self.assertEqual(self.call(path, body, headers=bearer)[0], 403, path)
        self.assertEqual(self.call("/api/storage/plan?key=standard", headers=bearer)[0], 403)

    def test_only_an_admin_deletes_or_adds(self):
        cookie = self.login()
        name = f"u{time.time_ns()}"
        self.assertEqual(self.call("/api/users/add", {"username": name, "password": "password1"}, cookie=cookie)[0], 201)
        user = self.login(name, "password1")
        self.assertEqual(self.call("/api/storage", cookie=user)[0], 200)
        for path, body in (("/api/models/delete", {"key": "standard"}), ("/api/storage/remove", {"kind": "builder", "name": "x"}),
                           ("/api/hf/inspect", {"url": "a/b"}), ("/api/hf/add", {"url": "a/b"}), ("/api/hf/token", {"token": ""})):
            self.assertEqual(self.call(path, body, cookie=user)[0], 403, path)
        self.assertEqual(self.call("/api/storage/plan?key=standard", cookie=user)[0], 403)
        self.assertEqual(self.call("/api/hf/token", cookie=user)[0], 403)

    def test_the_serving_model_cannot_be_deleted(self):
        cookie = self.login()
        active = self.call("/api/status", cookie=cookie)[1]["active"]
        code, plan = self.call(f"/api/storage/plan?key={active}", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(plan["blockers"])
        code, body = self.call("/api/models/delete", {"key": active}, cookie=cookie)
        self.assertEqual(code, 409)
        self.assertIn("serving", body["error"])
        self.assertEqual(self.call("/api/models/delete", {"key": "nope"}, cookie=cookie)[0], 400)
        self.assertEqual(self.call("/api/storage/plan?key=nope", cookie=cookie)[0], 404)

    def test_a_model_is_deleted_end_to_end(self):
        cookie = self.login()
        st = self.call("/api/status", cookie=cookie)[1]
        victim = next(k for k, m in st["models"].items() if m["installed"] and k != st["active"] and k != "tf27b")
        code, body = self.call("/api/models/delete", {"key": victim}, cookie=cookie)
        self.assertEqual(code, 202, body)
        self.assertEqual(body["job"]["target"], f"delete:{victim}")
        job = self.wait_job(cookie)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual([s["state"] for s in job["steps"]], ["done", "done", "done"])
        self.assertFalse(self.call("/api/status", cookie=cookie)[1]["models"][victim]["installed"])

    def test_links_are_checked_before_anything_is_fetched(self):
        cookie = self.login()
        for bad in ("", "../etc/passwd", "https://huggingface.co/datasets/a/b", "a/b/c"):
            self.assertEqual(self.call("/api/hf/inspect", {"url": bad}, cookie=cookie)[0], 400, bad)
            self.assertEqual(self.call("/api/hf/add", {"url": bad}, cookie=cookie)[0], 400, bad)

    def test_the_hugging_face_token_is_validated_and_never_sent_back(self):
        cookie = self.login()
        self.assertEqual(self.call("/api/hf/token", {"token": "not a token"}, cookie=cookie)[0], 400)
        tok = "hf_" + "a1B2c3D4e5" * 3
        code, body = self.call("/api/hf/token", {"token": tok}, cookie=cookie)
        self.assertEqual((code, body["set"], body["source"]), (200, True, "settings"))
        code, body = self.call("/api/hf/token", cookie=cookie)
        self.assertTrue(body["set"])
        self.assertNotIn(tok, json.dumps(body))
        code, body = self.call("/api/hf/token", {"token": ""}, cookie=cookie)
        self.assertFalse(body["set"])


if __name__ == "__main__":
    unittest.main()
