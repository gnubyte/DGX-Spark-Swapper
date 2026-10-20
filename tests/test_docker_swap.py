"""Model swaps through the real Engine with a fake `docker` and a fake model server (no GPU, no Docker needed).

Covers the `docker` kind end to end: preflight, the container made from the catalog's arguments (API key,
127.0.0.1), waiting for health, the speed check, stopping the previous model, reusing a stopped container, and a
model added from Hugging Face going through the same path. Linux only (a fake `docker` executable on PATH).
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "swapper"))

import fetch  # noqa: E402
import hfmodels  # noqa: E402
from engine import Engine  # noqa: E402
from test_swapper import TempDir, free_port  # noqa: E402

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, signal, subprocess, sys
st, port = os.environ["FAKE_STATE"], os.environ["FAKE_PORT"]
a = sys.argv[1:]
path = lambda n: os.path.join(st, n)
open(path("_calls.log"), "a").write(" ".join(a) + "\n")
cmd = a[0] if a else ""

def launch(name, args, label):
    served = key = None
    for i, x in enumerate(args):
        if x == "--served-model-name": served = args[i + 1]
        if x.startswith("TABBY_NAME="): served = x.split("=", 1)[1]
        if x == "--api-key": key = args[i + 1]
        if x.startswith("TABBY_API_KEY="): key = x.split("=", 1)[1]
    open(path(name), "w").write(label + "\n" + json.dumps(args))
    p = subprocess.Popen([sys.executable, os.environ["FAKE_SERVER"], port, served or "?", key or ""],
                         start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(path(name + ".pid"), "w").write(str(p.pid))

if cmd == "inspect":
    if a[1:2] == ["-f"]:
        name = a[-1]
        sys.exit(1) if not os.path.exists(path(name)) else (print(open(path(name)).read().split("\n")[0]), sys.exit(0))
    sys.exit(0 if os.path.exists(path(a[1])) else 1)
if cmd == "image":
    sys.exit(0)
if cmd == "ps":
    for n in sorted(os.listdir(st)):
        if not n.startswith("_") and os.path.exists(path(n + ".pid")):
            try: os.kill(int(open(path(n + ".pid")).read()), 0); print(n)
            except OSError: pass
    sys.exit(0)
if cmd == "run":
    name = a[a.index("--name") + 1]
    launch(name, a, next((x.split("=", 1)[1] for x in a if x.startswith("swapper.args=")), ""))
    print("fakecontainerid"); sys.exit(0)
if cmd == "start":
    name = a[-1]
    if not os.path.exists(path(name)): sys.exit(1)
    label, args = open(path(name)).read().split("\n", 1)
    launch(name, json.loads(args), label); print(name); sys.exit(0)
if cmd in ("stop", "kill") or (cmd == "rm" and "-f" in a):
    name = a[-1]
    if os.path.exists(path(name + ".pid")):
        try: os.killpg(int(open(path(name + ".pid")).read()), signal.SIGTERM)
        except OSError: pass
        os.remove(path(name + ".pid"))
    if cmd == "rm" and os.path.exists(path(name)): os.remove(path(name))
    print(name)
sys.exit(0)
'''

FAKE_SERVER = r'''
import json, sys, time, http.server
port, served, key = int(sys.argv[1]), sys.argv[2], sys.argv[3]
class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def ok(self):
        if key and self.headers.get("Authorization") != "Bearer " + key:
            self.send_response(401); self.end_headers(); return False
        return True
    def do_GET(self):
        if not self.ok(): return
        b = json.dumps({"data": [{"id": served, "max_model_len": 131072}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        if not self.ok(): return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if body.get("stream"):
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for _ in range(40):
                self.wfile.write(("data: " + json.dumps({"choices": [{"delta": {"content": "word "}}]}) + "\n\n").encode())
                self.wfile.flush(); time.sleep(0.01)
            self.wfile.write(("data: " + json.dumps({"choices": [], "usage": {"completion_tokens": 40}}) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n"); return
        b = json.dumps({"choices": [{"message": {"content": "ready"}}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
'''


@unittest.skipUnless(sys.platform.startswith("linux"), "uses a fake docker executable")
class DockerSwapTests(TempDir):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.state = self.tmp / "dockerstate"
        self.state.mkdir()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "docker").write_text(FAKE_DOCKER)
        (bindir / "docker").chmod(0o755)
        (self.tmp / "server.py").write_text(FAKE_SERVER)
        saved = {k: os.environ.get(k) for k in ("PATH", "FAKE_STATE", "FAKE_PORT", "FAKE_SERVER", "SWAPPER_PLATFORM")}
        os.environ.update({"PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_STATE": str(self.state),
                           "FAKE_PORT": str(self.port), "FAKE_SERVER": str(self.tmp / "server.py"),
                           "SWAPPER_PLATFORM": "nvidia-gb10"})
        self.addCleanup(self.restore, saved)
        self.addCleanup(self.kill_servers)
        self.home = self.tmp / "home"
        self.home.mkdir()
        cfg = self.tmp / "models.json"
        cfg.write_text(json.dumps({
            "api_port": self.port, "api_key": "k1", "nginx_port": free_port(), "public_host": "x",
            "openclaw": {"user": "pat", "home": str(self.home), "bin": str(self.tmp / "no-openclaw"), "unit": "u",
                         "gateway_port": 1},
            "models": {k: {"min_free_gib": 1} for k in ("laguna", "ornith", "vllmfp4")}}))
        self.eng = Engine(cfg, self.tmp / "state", catalog_path=ROOT / "swapper" / "catalog.json", conf_dir=self.tmp)
        for repo in ("poolside/Laguna-S-2.1-NVFP4", "poolside/Laguna-S-2.1-DFlash-NVFP4",
                     "Mia-AiLab/Ornith-1.5-122B-EXL3-EAQv1-4-bpw"):
            self.snapshot(repo)

    @staticmethod
    def restore(saved):
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def kill_servers(self):
        import signal
        for pidfile in self.state.glob("*.pid"):
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass

    def snapshot(self, repo, commit="a" * 40):
        d = self.home / ".cache/huggingface/hub" / f"models--{repo.replace('/', '--')}" / "snapshots" / commit
        d.mkdir(parents=True)
        (d / "config.json").write_text("{}")
        (d / "model.safetensors").write_text("x")

    def swap(self, key):
        job = self.eng.begin_swap(key)
        deadline = time.time() + 90
        while job.state == "running" and time.time() < deadline:
            time.sleep(0.2)
        log = "\n".join(m for _, m in job.log)
        self.assertEqual(job.state, "done", log)
        return log

    def calls(self) -> str:
        return (self.state / "_calls.log").read_text()

    def test_swapping_between_docker_models(self):
        eng = self.eng
        self.assertTrue(eng.install_state("laguna", fresh=True)["installed"])
        self.assertTrue(eng.install_state("ornith", fresh=True)["installed"])
        self.assertFalse(eng.install_state("vllmfp4", fresh=True)["installed"], "its checkpoint isn't downloaded")

        log = self.swap("laguna")
        self.assertIn("Speed here:", log)
        self.assertEqual(eng.status(fresh=True)["active"], "laguna")
        args = json.loads((self.state / "laguna-s-2.1-nvfp4").read_text().split("\n", 1)[1])
        self.assertEqual(args[args.index("--api-key") + 1], "k1")
        self.assertEqual(args[args.index("--host") + 1], "127.0.0.1")
        self.assertGreater(eng.load_state()["measured"]["laguna"]["decode"], 0)

        # to the TabbyAPI model: the first container is stopped, the second made with the key and port
        self.swap("ornith")
        self.assertIn("stop -t 60 laguna-s-2.1-nvfp4", self.calls())
        self.assertEqual(eng.status(fresh=True)["active"], "ornith")
        self.assertFalse((self.state / "laguna-s-2.1-nvfp4.pid").exists())
        tabby = json.loads((self.state / "swapper-ornith-122b").read_text().split("\n", 1)[1])
        self.assertIn("TABBY_API_KEY=k1", tabby)
        self.assertIn(f"TABBY_PORT={self.port}", tabby)

        # and back: the stopped container has the same settings, so it is started again, not recreated
        before = len(self.calls())
        self.swap("laguna")
        again = self.calls()[before:].splitlines()
        self.assertTrue(any(c.startswith("start laguna-s-2.1-nvfp4") for c in again), again)
        self.assertFalse(any(c.startswith("run ") and "laguna-s-2.1-nvfp4" in c for c in again), again)

    def test_a_model_added_from_hugging_face_swaps_like_any_other(self):
        info = {"id": "acme/tiny", "sha": "d" * 40, "gated": False, "private": False, "tags": [], "library": None,
                "pipeline": "text-generation", "license": "mit", "config": {"architectures": ["LlamaForCausalLM"]},
                "safetensors": {}, "modified": None,
                "files": {"config.json": {"size": 10, "sha256": None, "blob_id": "e" * 40},
                          "model.safetensors": {"size": 200_000_000, "sha256": "f" * 64, "blob_id": None}}}
        old_info, old_ctx = fetch.hf_info, hfmodels.native_context
        fetch.hf_info = lambda repo, revision=None, token=None: info
        hfmodels.native_context = lambda *a: 32768
        self.addCleanup(setattr, fetch, "hf_info", old_info)
        self.addCleanup(setattr, hfmodels, "native_context", old_ctx)
        key, entry = hfmodels.build(self.eng, "acme/tiny", {"context": 8192})
        entry["min_free_gib"] = 1
        self.eng.add_custom_model(key, entry)
        self.assertFalse(self.eng.install_state(key, fresh=True)["installed"])
        self.snapshot("acme/tiny", "d" * 40)
        self.assertTrue(self.eng.install_state(key, fresh=True)["installed"])
        self.swap(key)
        self.assertEqual(self.eng.status(fresh=True)["active"], key)
        run = json.loads((self.state / f"swapper-{key}").read_text().split("\n", 1)[1])
        self.assertEqual(run[run.index("--served-model-name") + 1], "tiny")
        self.assertEqual(run[run.index("--revision") + 1], "d" * 40)
        self.assertEqual(run[run.index("--max-model-len") + 1], "8192")


if __name__ == "__main__":
    unittest.main()
