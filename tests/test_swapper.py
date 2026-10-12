"""Unit tests for Spark Swapper (standard library only).

    python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "swapper"))

import config  # noqa: E402
import fetch  # noqa: E402
import hardware  # noqa: E402
import tokens  # noqa: E402
import updater  # noqa: E402
from engine import Engine, cert_fingerprint  # noqa: E402
from installer import InstallFailed, grub_with_gpu_limit, nginx_front_conf  # noqa: E402

CATALOG = json.loads((ROOT / "swapper" / "catalog.json").read_text())


def site(**extra) -> dict:
    base = {"api_port": 8888, "api_key": "vllm-local", "nginx_port": 11443, "public_host": "10.0.0.5",
            "openclaw": {"user": "pat", "home": "/home/pat", "bin": "/usr/bin/openclaw",
                         "unit": "openclaw-gateway.service", "gateway_port": 18789},
            "models": {}}
    base.update(extra)
    return base


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="swapper-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


# ─────────────────────────────── config ───────────────────────────────

class ConfigTests(unittest.TestCase):
    def test_catalog_json_is_complete(self):
        for key, m in CATALOG["models"].items():
            for field in ("title", "platforms", "kind", "served_id", "context", "openclaw_provider", "perf"):
                self.assertIn(field, m, f"{key} lacks {field}")
            self.assertIn(m["kind"], ("sglang-docker", "flash-vllm", "recipe-script", "llamacpp"))
            if m["kind"] == "llamacpp":
                self.assertTrue(m.get("unit", "").startswith("swapper-llama-"))
                self.assertTrue(m["files"])
            if m["kind"] == "recipe-script":
                self.assertTrue(m.get("container") and m.get("dir"))
        providers = [m["openclaw_provider"] for m in CATALOG["models"].values()]
        self.assertEqual(len(providers), len(set(providers)), "each model needs its own OpenClaw provider")

    def test_render_fills_known_placeholders_only(self):
        out = config.render({"a": "{{HOME}}/x", "b": ["{{USER}}", "{{NOPE}}"], "c": 3}, {"HOME": "/home/pat", "USER": "pat"})
        self.assertEqual(out, {"a": "/home/pat/x", "b": ["pat", "{{NOPE}}"], "c": 3})

    def test_platform_filtering(self):
        spark = config.merge(site(), CATALOG, hardware.NVIDIA_GB10)["models"]
        halo = config.merge(site(), CATALOG, hardware.AMD_STRIX_HALO)["models"]
        self.assertIn("tf27b", spark)
        self.assertIn("nemotron", spark)
        self.assertNotIn("halo27b", spark)
        self.assertIn("halo27b", halo)
        self.assertIn("haloflash", halo)
        self.assertNotIn("tf27b", halo)
        self.assertEqual(spark["tf27b"]["dir"], "/home/pat/Qwen3.8-27B-DGX-Spark-TensorFold")

    def test_site_entry_merges_over_catalog_field_by_field(self):
        # An old box's config: its own "standard" entry with no platforms/perf/install.
        old = {"title": "My 27B", "kind": "sglang-docker", "container": "qwen3.8-27b-sglang",
               "served_id": "qwen3.8-27b-sglang", "context": 1000000, "docker_run": ["custom"]}
        merged = config.merge(site(models={"standard": old}), CATALOG, hardware.NVIDIA_GB10)["models"]["standard"]
        self.assertEqual(merged["title"], "My 27B")
        self.assertEqual(merged["docker_run"], ["custom"])
        self.assertIn("perf", merged)          # new field arrives from the catalog
        self.assertEqual(merged["source"], "catalog+site")

    def test_upgraded_1x_box_gets_catalog_titles_without_duplicates(self):
        legacy = json.loads((ROOT / "swapper" / "legacy_v1.json").read_text())["models"]
        rendered = config.render(legacy, {"USER": "pat"})
        merged = config.merge(site(models=rendered), CATALOG, hardware.NVIDIA_GB10)["models"]
        titles = [m["title"] for m in merged.values()]
        self.assertEqual(len(titles), len(set(titles)), titles)
        self.assertEqual(merged["standard"]["title"], "Qwen3.8 27B (SGLang)")
        self.assertEqual(merged["flash"]["title"], "Qwen3.8 Flash Next (vLLM)")
        self.assertEqual(merged["standard"]["source"], "catalog")

    def test_upgraded_box_keeps_its_own_edits(self):
        legacy = config.render(json.loads((ROOT / "swapper" / "legacy_v1.json").read_text())["models"], {"USER": "pat"})
        legacy["standard"]["min_free_gib"] = 80
        merged = config.merge(site(models=legacy), CATALOG, hardware.NVIDIA_GB10)["models"]["standard"]
        self.assertEqual(merged["min_free_gib"], 80)
        self.assertEqual(merged["title"], "Qwen3.8 27B (SGLang)")

    def test_api_key_and_port_reach_the_model_args(self):
        merged = config.merge(site(api_key="sk-spark-abc", api_port=9999), CATALOG, hardware.NVIDIA_GB10)["models"]
        args = merged["standard"]["docker_run"]
        self.assertEqual(args[args.index("--api-key") + 1], "sk-spark-abc")
        self.assertEqual(args[args.index("--port") + 1], "9999")
        self.assertEqual(merged["tfflash"]["env"]["TENSORFOLD_API_KEY"], "sk-spark-abc")

    def test_broken_site_config_fails_loudly(self):
        with tempfile.TemporaryDirectory() as d:
            bad = Path(d) / "models.json"
            bad.write_text('{"api_key": "x",}')
            with self.assertRaises(config.ConfigError):
                config.load_site(bad)
            bad.write_text('{"api_key": "x"}')
            with self.assertRaises(config.ConfigError):
                config.load_site(bad)

    def test_hidden_and_site_only_models(self):
        extra = {"title": "Mine", "kind": "sglang-docker", "container": "mine", "served_id": "mine", "context": 1000}
        merged = config.merge(site(hidden_models=["nemotron"], models={"mine": extra}), CATALOG, hardware.NVIDIA_GB10)
        self.assertNotIn("nemotron", merged["models"])
        self.assertEqual(merged["models"]["mine"]["source"], "site")  # no platforms: runs anywhere

    def test_root_user_home(self):
        self.assertEqual(config.site_values({"openclaw": {"user": "root"}})["HOME"], "/root")


# ─────────────────────────────── hardware ───────────────────────────────

class HardwareTests(unittest.TestCase):
    def test_forced(self):
        self.assertEqual(hardware.detect(lambda *a, **k: (127, ""), "amd-strix-halo")[0], hardware.AMD_STRIX_HALO)

    def test_nvidia_smi(self):
        sh = lambda cmd, **k: (0, "GPU 0: NVIDIA GB10 (UUID: GPU-123)") if cmd[0] == "nvidia-smi" else (127, "")
        self.assertEqual(hardware.detect(sh, "auto"), (hardware.NVIDIA_GB10, "GPU 0: NVIDIA GB10 (UUID: GPU-123)"))

    def test_fallback_is_spark(self):
        old = os.environ.pop("SWAPPER_PLATFORM", None)
        try:
            if not Path("/proc/cpuinfo").exists():
                self.assertEqual(hardware.detect(lambda *a, **k: (127, ""))[0], hardware.NVIDIA_GB10)
        finally:
            if old is not None:
                os.environ["SWAPPER_PLATFORM"] = old


# ─────────────────────────────── tokens ───────────────────────────────

class TokenTests(TempDir):
    def test_create_verify_revoke(self):
        store = tokens.TokenStore(self.tmp / "tokens.json")
        tok, info = store.create("pat", "DSH on laptop")
        self.assertRegex(tok, tokens.TOKEN_RE)
        self.assertEqual(store.verify(tok)["user"], "pat")
        self.assertIsNone(store.verify(tok[:-1] + ("A" if tok[-1] != "A" else "B")))
        self.assertIsNone(store.verify("sst_zzzzzzzz_" + "a" * 43))
        self.assertIsNone(store.verify(None))
        stored = json.loads((self.tmp / "tokens.json").read_text())
        self.assertNotIn(tok.split("_", 2)[2], json.dumps(stored), "the secret itself is never stored")
        self.assertEqual(oct((self.tmp / "tokens.json").stat().st_mode & 0o777), "0o600")
        self.assertFalse(store.revoke(info["id"], user="someone-else"))
        self.assertTrue(store.revoke(info["id"], user="pat"))
        self.assertIsNone(store.verify(tok))

    def test_revoke_user(self):
        store = tokens.TokenStore(self.tmp / "t.json")
        a, _ = store.create("a", "x")
        b, _ = store.create("b", "y")
        store.revoke_user("a")
        self.assertIsNone(store.verify(a))
        self.assertIsNotNone(store.verify(b))

    def test_connection_code_round_trip(self):
        info = {"name": "DGX Spark (spark-1)", "hostname": "spark-1", "platform": "nvidia-gb10", "version": "2.0.0",
                "swapper_url": "https://10.0.0.5:8999", "api_base": "https://10.0.0.5:11443/v1",
                "api_key": "vllm-local", "model": "Qwen3.8-27B", "context": 262144, "fingerprint": "AA:" * 31 + "AA"}
        code = tokens.connection_code(info, "sst_1a2b3c4d_" + "x" * 43, "pat")
        self.assertTrue(code.startswith("SSC1:"))
        self.assertNotIn("=", code)
        decoded = tokens.decode_code("  " + code[:30] + "\n" + code[30:] + "\n")
        self.assertEqual(decoded["v"], 1)
        self.assertEqual(decoded["token"], "sst_1a2b3c4d_" + "x" * 43)
        self.assertEqual(decoded["api_base"], info["api_base"])
        self.assertEqual(decoded["user"], "pat")
        self.assertEqual(tokens.decode_code(json.dumps(decoded))["model"], "Qwen3.8-27B")


# ─────────────────────────────── updater ───────────────────────────────

class UpdaterTests(unittest.TestCase):
    def test_versions(self):
        self.assertEqual(updater.parse_version("v2.10.3"), (2, 10, 3))
        self.assertEqual(updater.parse_version("2.1"), (2, 1, 0))
        self.assertEqual(updater.parse_version("2.0.0-rc1"), (2, 0, 0))
        self.assertTrue(updater.newer("2.0.1", "2.0.0"))
        self.assertTrue(updater.newer("2.10.0", "2.9.9"))
        self.assertFalse(updater.newer("2.0.0", "2.0.0"))
        self.assertFalse(updater.newer(None, "2.0.0"))
        self.assertFalse(updater.newer("1.9.9", "2.0.0"))

    def test_due_only_on_sunday_after_the_slot(self):
        sunday_early = datetime(2026, 10, 18, 2, 59)      # 2026-10-18 is a Sunday
        sunday_late = datetime(2026, 10, 18, 3, 20)
        monday = datetime(2026, 10, 19, 4, 0)
        self.assertEqual(sunday_late.weekday(), 6)
        self.assertFalse(updater.due(sunday_early, 3, 10, None))
        self.assertTrue(updater.due(sunday_late, 3, 10, None))
        self.assertFalse(updater.due(monday, 3, 10, None))
        checked = datetime(2026, 10, 18, 3, 11).timestamp()
        self.assertFalse(updater.due(sunday_late, 3, 10, checked))
        last_week = datetime(2026, 10, 11, 3, 15).timestamp()
        self.assertTrue(updater.due(sunday_late, 3, 10, last_week))

    def test_next_slot(self):
        wed = datetime(2026, 10, 14, 12, 0)
        self.assertEqual(updater.next_slot(wed, 3, 7), datetime(2026, 10, 18, 3, 7))
        sunday_after = datetime(2026, 10, 18, 9, 0)
        self.assertEqual(updater.next_slot(sunday_after, 3, 7), datetime(2026, 10, 25, 3, 7))


# ─────────────────────────────── installer ───────────────────────────────

class InstallerTextTests(unittest.TestCase):
    def test_nginx_front_requires_the_key(self):
        conf = nginx_front_conf(site(), "/etc/spark-swapper/tls.crt", "/etc/spark-swapper/tls.key")
        self.assertTrue(conf.startswith("# Written by Spark Swapper"))
        self.assertIn('"~^(?i:bearer)[ \\t]+vllm\\-local$" 1;', conf)
        self.assertIn('"~^vllm\\-local$" 1;', conf)
        self.assertIn(f"# api-key-sha256: {hashlib.sha256(b'vllm-local').hexdigest()}", conf)
        self.assertIn('if ($swapper_auth = "00")', conf)
        self.assertIn("return 401", conf)
        self.assertIn("proxy_pass http://127.0.0.1:8888;", conf)
        self.assertIn("proxy_pass http://127.0.0.1:18789;", conf)
        self.assertIn("listen 11443 ssl;", conf)

    def test_nginx_map_keywords_cannot_collide(self):
        conf = nginx_front_conf(site(api_key="default"), "c", "k")
        self.assertIn('"~^default$" 1;', conf)
        self.assertNotIn('"default" 1;', conf)

    def test_nginx_front_without_key_and_bad_key(self):
        conf = nginx_front_conf(site(api_key=""), "c", "k")
        self.assertNotIn("swapper_auth", conf)
        with self.assertRaises(InstallFailed):
            nginx_front_conf(site(api_key='bad"key'), "c", "k")

    def test_grub_edit(self):
        before = 'GRUB_DEFAULT=0\nGRUB_CMDLINE_LINUX_DEFAULT="quiet splash amdgpu.gttsize=126976 ttm.pages_limit=5"\n'
        after = grub_with_gpu_limit(before, 28311552)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="quiet splash ttm.pages_limit=28311552 ttm.page_pool_size=28311552"', after)
        self.assertNotIn("gttsize", after)
        self.assertEqual(grub_with_gpu_limit(after, 28311552), after, "idempotent")
        appended = grub_with_gpu_limit("GRUB_DEFAULT=0\n", 100)
        self.assertIn('GRUB_CMDLINE_LINUX_DEFAULT="ttm.pages_limit=100 ttm.page_pool_size=100"', appended)


# ─────────────────────────────── engine ───────────────────────────────

class EngineTests(TempDir):
    def make(self, platform="amd-strix-halo", **site_extra):
        os.environ["SWAPPER_PLATFORM"] = platform
        cfg = self.tmp / "models.json"
        cfg.write_text(json.dumps(site(**site_extra)))
        return Engine(cfg, self.tmp / "state", catalog_path=ROOT / "swapper" / "catalog.json", conf_dir=self.tmp)

    def tearDown(self):
        os.environ.pop("SWAPPER_PLATFORM", None)
        super().tearDown()

    def test_serves(self):
        self.assertTrue(Engine.serves({"served_id": "a"}, {"id": "a"}))
        self.assertFalse(Engine.serves({"served_id": "a"}, {"id": "b"}))
        nem = CATALOG["models"]["nemotron"]
        path_id = "/root/.cache/huggingface/hub/models--nvidia--NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4/snapshots/abc"
        self.assertTrue(Engine.serves(nem, {"id": path_id}))
        self.assertTrue(Engine.serves(nem, {"id": "nemotron-3.5-lightning"}))
        self.assertFalse(Engine.serves(nem, None))

    def test_split_ref(self):
        self.assertEqual(Engine.split_ref("unsloth/Qwen3.8-Flash-Next-GGUF/UD-Q3_K_XL/a.gguf"),
                         ("unsloth/Qwen3.8-Flash-Next-GGUF", "UD-Q3_K_XL/a.gguf"))

    def fake_llama(self, mtp: bool) -> Path:
        exe = self.tmp / "bin" / "llama-server"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\necho 'usage: llama-server [options]'\n"
                       + ("echo '--spec-type TYPE  (none, draft, draft-mtp)'\n" if mtp else ""))
        exe.chmod(0o755)
        return exe

    def test_llama_argv_with_and_without_mtp(self):
        eng = self.make()
        for mtp in (True, False):
            exe = self.fake_llama(mtp)
            eng._mtp_cache.clear()
            eng.save_state(llamacpp={"bin": str(exe), "tag": "t"})
            argv = eng.llama_argv("haloflash")
            self.assertEqual(argv[0], str(exe))
            model = argv[argv.index("--model") + 1]
            self.assertTrue(model.endswith("unsloth/Qwen3.8-Flash-Next-GGUF/UD-Q3_K_XL/Qwen3.8-Flash-Next-UD-Q3_K_XL-00001-of-00003.gguf"))
            self.assertEqual(argv[argv.index("--alias") + 1], "qwen3.8-flash-next")
            self.assertEqual(argv[argv.index("--host") + 1], "127.0.0.1")
            self.assertEqual(argv[argv.index("--api-key") + 1], "vllm-local")
            if mtp:
                self.assertIn("draft-mtp", argv)
                self.assertTrue(argv[argv.index("-md") + 1].endswith("MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf"))
            else:
                self.assertNotIn("draft-mtp", argv)
                self.assertNotIn("-md", argv)

    def test_hf_install_state(self):
        eng = self.make()
        self.assertFalse(eng.install_state("halo27b", fresh=True)["installed"])
        exe = self.fake_llama(True)
        eng.save_state(llamacpp={"bin": str(exe), "tag": "t"})
        f = eng.local_file("lmstudio-community/Qwen3.8-27B-GGUF", "Qwen3.8-27B-Q4_K_M.gguf")
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x" * 10)
        self.assertFalse(eng.install_state("halo27b", fresh=True)["installed"], "no verified marker yet")
        f.with_name(f.name + ".ok").write_text(json.dumps({"size": 10}))
        self.assertTrue(eng.install_state("halo27b", fresh=True)["installed"])
        f.write_bytes(b"x" * 11)
        self.assertFalse(eng.install_state("halo27b", fresh=True)["installed"], "size changed")

    def test_recipe_install_state(self):
        eng = self.make(platform="nvidia-gb10", openclaw={"user": "pat", "home": str(self.tmp / "home")})
        st = eng.install_state("tf27b", fresh=True)
        self.assertFalse(st["installed"])
        d = self.tmp / "home" / "Qwen3.8-27B-DGX-Spark-TensorFold"
        d.mkdir(parents=True)
        (d / "start.sh").write_text("#!/bin/sh\n")
        marker = self.tmp / "home" / ".cache/tensorfold-qwen38-27b/.prepared"
        marker.parent.mkdir(parents=True)
        marker.write_text("ok")
        self.assertTrue(eng.install_state("tf27b", fresh=True)["installed"])

    def test_cert_fingerprint_matches_openssl(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl not available")
        crt, key = self.tmp / "c.crt", self.tmp / "c.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(crt), "-subj", "/CN=test"], check=True, capture_output=True)
        out = subprocess.run(["openssl", "x509", "-in", str(crt), "-noout", "-fingerprint", "-sha256"],
                             check=True, capture_output=True, text=True).stdout
        self.assertEqual(cert_fingerprint(crt), out.strip().split("=", 1)[1])

    def test_keyless_model_refused_behind_an_open_front(self):
        import engine as engine_mod
        eng = self.make(platform="nvidia-gb10", openclaw={"user": "pat", "home": str(self.tmp / "home")})
        d = self.tmp / "home" / "Qwen3.8-27B-DGX-Spark-TensorFold"
        d.mkdir(parents=True)
        (d / "start.sh").write_text("#!/bin/sh\n")
        conf = self.tmp / "front.conf"
        old_conf, old_port = engine_mod.FRONT_CONF, engine_mod.port_open
        engine_mod.FRONT_CONF = conf
        engine_mod.port_open = lambda *a, **k: True
        eng.ensure_docker_group = lambda job: None
        try:
            job = engine_mod.Job("tf27b", None, ["prepare"])
            conf.write_text("server { listen 11443 ssl; }")            # someone else's front
            with self.assertRaises(engine_mod.SwapFailed):
                eng.preflight("tf27b", job)
            conf.write_text(nginx_front_conf(site(), "c", "k"))          # ours, same key
            eng.preflight("tf27b", job)
            conf.write_text(nginx_front_conf(site(api_key="old"), "c", "k"))   # ours, stale key
            with self.assertRaises(engine_mod.SwapFailed):
                eng.preflight("tf27b", job)
            self.assertTrue(Engine.own_key(eng.models["tfflash"]))
            self.assertFalse(Engine.own_key(eng.models["nemotron"]))
            self.assertTrue(Engine.own_key(eng.models["standard"]))
        finally:
            engine_mod.FRONT_CONF, engine_mod.port_open = old_conf, old_port

    def test_container_reused_only_with_current_settings(self):
        import engine as engine_mod
        eng = self.make(platform="nvidia-gb10")
        m = eng.models["standard"]
        replies = {}
        old = engine_mod.sh
        engine_mod.sh = lambda cmd, **k: replies["inspect"]
        try:
            replies["inspect"] = (0, f"{Engine.args_hash(m)}|[]")
            self.assertTrue(eng.container_current(m))
            replies["inspect"] = (0, "0123456789abcdef|[]")
            self.assertFalse(eng.container_current(m))
            replies["inspect"] = (0, '<no value>|["--port", "8888", "--api-key", "vllm-local"]')
            self.assertTrue(eng.container_current(m), "a 1.x container with today's key is kept")
            replies["inspect"] = (0, '<no value>|["--port", "8888", "--api-key", "something-else"]')
            self.assertFalse(eng.container_current(m))
            replies["inspect"] = (1, "")
            self.assertFalse(eng.container_current(m))
        finally:
            engine_mod.sh = old

    def test_user_commands_get_a_clean_environment(self):
        eng = self.make()
        argv, env = eng.run_cmd({"as": "user"}, ["bash", "x.sh"], {"PORT": 8888, "TENSORFOLD_API_KEY": "k"})
        self.assertEqual(argv[-2:], ["bash", "x.sh"])
        self.assertEqual(env["HOME"], "/home/pat")
        self.assertEqual(env["TENSORFOLD_API_KEY"], "k")
        self.assertNotIn("k", " ".join(argv), "secrets go in the environment, not the command line")
        if os.geteuid() == 0:
            self.assertEqual(argv[0], "setpriv")

    def test_updating_blocks_swaps(self):
        eng = self.make()
        (self.tmp / "state" / "update-result.json").write_text(json.dumps({"state": "running", "started": time.time()}))
        self.assertTrue(eng.updating())
        with self.assertRaisesRegex(RuntimeError, "updating"):
            eng.begin_swap("halo27b")
        (self.tmp / "state" / "update-result.json").write_text(json.dumps({"state": "running", "started": time.time() - 4000}))
        self.assertFalse(eng.updating(), "a stale handoff doesn't block forever")

    def test_public_host(self):
        eng = self.make()
        self.assertEqual(eng.public_host("192.168.1.9:8999"), "192.168.1.9")
        self.assertEqual(eng.public_host("spark.local"), "spark.local")
        self.assertEqual(eng.public_host("localhost:8999"), "10.0.0.5")
        self.assertEqual(eng.public_host("[fe80::1]:8999"), "fe80::1")
        self.assertEqual(eng.public_host(None), "10.0.0.5")


# ─────────────────────────────── fetch ───────────────────────────────

class RangeHandler(http.server.BaseHTTPRequestHandler):
    payload = b""
    fail_once = False

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = self.payload
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data) - start))
        self.end_headers()
        self.wfile.write(data[start:])


class FetchTests(TempDir):
    def serve(self, payload: bytes) -> str:
        RangeHandler.payload = payload
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f"http://127.0.0.1:{srv.server_address[1]}/file"

    def test_resume_and_verify(self):
        payload = os.urandom(3 * 1024 * 1024 + 17)
        url = self.serve(payload)
        dest = self.tmp / "out" / "model.gguf"
        dest.parent.mkdir()
        dest.with_name("model.gguf.part").write_bytes(payload[:1000000])
        said = []
        fetch.download(url, dest, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(), say=said.append)
        self.assertEqual(dest.read_bytes(), payload)
        self.assertFalse(dest.with_name("model.gguf.part").exists())
        self.assertTrue(any("resuming" in s for s in said))

    def test_oversized_partial_file_starts_over(self):
        payload = os.urandom(200000)
        url = self.serve(payload)
        dest = self.tmp / "m.gguf"
        dest.with_name("m.gguf.part").write_bytes(os.urandom(300000))
        fetch.download(url, dest, size=len(payload), sha256=hashlib.sha256(payload).hexdigest(), say=lambda s: None)
        self.assertEqual(dest.read_bytes(), payload)

    def test_bad_checksum_is_refused(self):
        url = self.serve(b"hello world")
        dest = self.tmp / "x.bin"
        with self.assertRaises(fetch.FetchError):
            fetch.download(url, dest, size=11, sha256="0" * 64, say=lambda s: None)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name("x.bin.part").exists())

    def test_safe_extract_refuses_escapes(self):
        archive = self.tmp / "evil.tar.gz"
        with tarfile.open(archive, "w:gz") as tf:
            info = tarfile.TarInfo("../escaped.txt")
            data = b"nope"
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        with self.assertRaises(Exception):
            fetch.safe_extract(archive, self.tmp / "dest")
        self.assertFalse((self.tmp / "escaped.txt").exists())


# ─────────────────────────────── HTTP API (demo) ───────────────────────────────

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="swapper-api-"))
        cls.port = free_port()
        env = {**os.environ, "SWAPPER_DEMO": "1", "SWAPPER_DEMO_HTTP": "1", "SWAPPER_BIND": "127.0.0.1",
               "SWAPPER_PORT": str(cls.port), "SWAPPER_CONF": str(cls.tmp / "conf"),
               "SWAPPER_STATE": str(cls.tmp / "state")}
        cls.proc = subprocess.Popen([sys.executable, str(ROOT / "swapper" / "app.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.base = f"http://127.0.0.1:{cls.port}"
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
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}"), r.headers
        except urllib.error.HTTPError as e:
            with e:
                return e.code, json.loads(e.read() or b"{}"), e.headers

    def login(self) -> str:
        code, body, headers = self.call("/api/login", {"username": "admin", "password": "admin"})
        self.assertEqual(code, 200)
        return headers["Set-Cookie"].split(";", 1)[0]

    def test_session_reports_version_and_platform(self):
        code, s, _ = self.call("/api/session")
        self.assertEqual(code, 200)
        self.assertEqual(s["version"], (ROOT / "swapper" / "VERSION").read_text().strip())
        self.assertEqual(s["platform"], "nvidia-gb10")

    def test_tokens_end_to_end(self):
        cookie = self.login()
        code, made, _ = self.call("/api/tokens/create", {"label": "test"}, cookie=cookie)
        self.assertEqual(code, 201)
        bearer = {"Authorization": f"Bearer {made['token']}"}
        decoded = tokens.decode_code(made["code"])
        self.assertEqual(decoded["token"], made["token"])
        self.assertRegex(decoded["fingerprint"], r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")

        code, st, _ = self.call("/api/status", headers=bearer)
        self.assertEqual(code, 200)
        self.assertIn("models", st)
        self.assertTrue(all("installed" in m and "perf" in m for m in st["models"].values()))
        self.assertEqual(self.call("/api/users", headers=bearer)[0], 403)
        self.assertEqual(self.call("/api/tokens/create", {"label": "x"}, headers=bearer)[0], 403)
        self.assertEqual(self.call("/api/updates", headers=bearer)[0], 403)
        self.assertEqual(self.call("/api/connect", headers=bearer)[0], 200)
        code, sess, _ = self.call("/api/session", headers=bearer)
        self.assertEqual((sess["user"], sess["via"]), ("admin", "token"))

        not_installed = next(k for k, m in st["models"].items() if not m["installed"])
        code, body, _ = self.call("/api/swap", {"target": not_installed}, headers=bearer)
        self.assertEqual(code, 400)
        self.assertTrue(body.get("not_installed"))

        self.assertEqual(self.call("/api/tokens/revoke", {"id": made["id"]}, cookie=cookie)[0], 200)
        code, body, _ = self.call("/api/status", headers=bearer)
        self.assertEqual(code, 401)
        self.assertIn("connection", body["error"])

    def test_posts_need_the_custom_header(self):
        req = urllib.request.Request(self.base + "/api/login", data=b"{}", headers={"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        with ctx.exception:
            self.assertEqual(ctx.exception.code, 403)

    def test_updates_admin_flow(self):
        cookie = self.login()
        code, info, _ = self.call("/api/updates", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(info["auto_install"])
        code, info, _ = self.call("/api/updates/settings", {"auto_install": False}, cookie=cookie)
        self.assertFalse(info["auto_install"])
        code, info, _ = self.call("/api/updates/check", {}, cookie=cookie)
        self.assertTrue(info["available"])

    def test_adding_a_user_keeps_the_admin_signed_in(self):
        cookie = self.login()
        code, body, headers = self.call("/api/users/add", {"username": f"u{time.time_ns()}", "password": "password1"},
                                        cookie=cookie)
        self.assertEqual(code, 201)
        self.assertIsNone(headers.get("Set-Cookie"))

    def test_non_object_bodies_are_ignored(self):
        cookie = self.login()
        req = urllib.request.Request(self.base + "/api/swap", data=b"[]",
                                     headers={"X-Swapper": "1", "Content-Type": "application/json", "Cookie": cookie})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        with ctx.exception:
            self.assertEqual(ctx.exception.code, 400)

    def test_tokens_cannot_provision_and_resets_revoke_them(self):
        cookie = self.login()
        name = f"t{time.time_ns()}"
        self.assertEqual(self.call("/api/users/add", {"username": name, "password": "password1"}, cookie=cookie)[0], 201)
        code, body, headers = self.call("/api/login", {"username": name, "password": "password1"})
        user_cookie = headers["Set-Cookie"].split(";", 1)[0]
        code, made, _ = self.call("/api/tokens/create", {"label": "x"}, cookie=user_cookie)
        bearer = {"Authorization": f"Bearer {made['token']}"}
        self.assertEqual(self.call("/api/install", {"action": "ssl"}, headers=bearer)[0], 403)
        self.assertEqual(self.call("/api/install", {"action": "ssl"}, cookie=user_cookie)[0], 403, "non-admins can't provision")
        self.assertEqual(self.call("/api/status", headers=bearer)[0], 200)
        self.assertEqual(self.call("/api/users/reset", {"username": name, "password": "password2"}, cookie=cookie)[0], 200)
        self.assertEqual(self.call("/api/status", headers=bearer)[0], 401)

    def test_install_action_validation(self):
        cookie = self.login()
        self.assertEqual(self.call("/api/install", {"action": "rm -rf"}, cookie=cookie)[0], 400)
        self.assertEqual(self.call("/api/models/install", {"key": "nope"}, cookie=cookie)[0], 400)


if __name__ == "__main__":
    unittest.main()
