"""Add a model from Hugging Face: paste a link, get a card that installs and runs.

`inspect` asks Hugging Face what the repo is (its format, size, architecture), says whether
this box can run it and with which engine, and notes when the catalog already has it.
`build` turns that into a model entry from one of the catalog's *profiles* — fixed templates
for an engine (vLLM on the Spark, ExLlamaV3 on the Spark, llama.cpp on a Strix Halo). The only
things that vary are the repo, the commit, the context window and the extra arguments the admin
typed; they are checked here, never put through a shell, and only an admin can add a model.

The entry is saved (engine.add_custom_model) and installs like any catalog model: download the
weights into the Hugging Face cache, pull or build the engine image, then "Switch to this".
"""
from __future__ import annotations

import copy
import hashlib
import math
import re
import shlex
import shutil
from pathlib import Path

import config as config_mod
import fetch
import hardware


GIB = 1024 ** 3
WEIGHT_SUFFIXES = (".safetensors", ".gguf", ".bin", ".pt", ".pth")
SPLIT_GGUF = re.compile(r"^(?P<stem>.+)-(?P<n>\d{5})-of-(?P<total>\d{5})\.gguf$")
DEFAULT_CONTEXT = 65536
MAX_EXTRA_ARGS = 24
VISION_PIPELINES = ("image-text-to-text", "video-text-to-text")


class ModelError(Exception):
    pass


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "model"


def model_key(repo: str) -> str:
    """hf-<owner>-<name>: short enough to be a container and provider name, stable for a repo."""
    owner, name = repo.split("/", 1)
    return f"hf-{slug(owner, 14)}-{slug(name, 36)}"[:60].rstrip("-")


def quant_config(info: dict) -> dict:
    """The repo's quantization_config (whatever else the repo's JSON says, it is only ever used as a dict)."""
    q = (info.get("config") or {}).get("quantization_config") if isinstance(info.get("config"), dict) else None
    return q if isinstance(q, dict) else {}


def detect_format(info: dict) -> str:
    quant = quant_config(info)
    tags = {t for t in info.get("tags") or [] if isinstance(t, str)}
    names = list(info["files"])
    for fmt in ("exl3", "exl2"):
        if fmt in tags or quant.get("quant_method") == fmt:
            return fmt
    if any(n.endswith(".gguf") for n in names):
        return "gguf"
    if any(n.endswith(".safetensors") for n in names):
        return "safetensors"
    return "unknown"


def weight_bytes(info: dict) -> int:
    """What the engine will load: the safetensors if there are any (a repo often also carries the same weights
    as .bin / .pth / .msgpack, which are not downloaded), else whatever weight files there are."""
    return sum(m["size"] for p, m in fetch.dedupe_weights(info["files"]).items() if p.endswith(WEIGHT_SUFFIXES))


def gguf_groups(info: dict) -> list[dict]:
    """The quantizations a GGUF repo offers: one per file, or per set of numbered shards."""
    groups: dict[str, dict] = {}
    for path, meta in info["files"].items():
        base = Path(path).name.lower()
        if not path.endswith(".gguf") or "mmproj" in base or base.startswith("mtp-"):
            continue
        m = SPLIT_GGUF.match(path)
        gid = f"{m['stem']}.gguf" if m else path
        g = groups.setdefault(gid, {"id": gid, "files": [], "bytes": 0})
        g["files"].append(path)
        g["bytes"] += meta["size"]
    for g in groups.values():
        g["files"].sort()
        g["label"] = Path(g["id"]).name.removesuffix(".gguf")
    return sorted(groups.values(), key=lambda g: g["bytes"])


def quant_label(info: dict, fmt: str) -> str:
    q = quant_config(info)
    bits = f" {q['bits']}-bit" if isinstance(q.get("bits"), (int, float)) and q.get("bits") else ""
    if fmt == "exl3":
        return f"EXL3{bits}"
    if isinstance(q.get("quant_method"), str):
        return f"{q['quant_method'][:40]}{bits}"
    return {"gguf": "GGUF", "safetensors": "unquantized weights"}.get(fmt, "")


def native_context(repo: str, commit: str, token: str | None) -> int | None:
    """The model's own context window, from its config.json (text_config for multimodal ones)."""
    try:
        cfg = fetch.get_json(fetch.hf_url(repo, "config.json", commit), timeout=30, headers=fetch.hf_headers(token))
    except fetch.FetchError:
        return None
    for node in ((cfg or {}).get("text_config") or {}, cfg or {}):
        n = node.get("max_position_embeddings") if isinstance(node, dict) else None
        if isinstance(n, int) and n > 0:
            return n
    return None


def pick_profile(platform: str, fmt: str, profiles: dict) -> tuple[str | None, list[str]]:
    """(profile name, reasons it can't run) for a repo format on this platform."""
    for name, p in profiles.items():
        if platform in p.get("platforms", []) and fmt in p.get("formats", []):
            return name, []
    if platform == hardware.AMD_STRIX_HALO:
        return None, ["Only GGUF models run on a Strix Halo (they use llama.cpp), and this repo isn't GGUF."]
    if fmt == "gguf":
        return None, ["This is a GGUF repo, which runs on llama.cpp. Spark Swapper doesn't set llama.cpp up on a "
                      "DGX Spark; look for a safetensors or EXL3 version of the model."]
    if fmt == "exl2":
        return None, ["EXL2 is the older ExLlama format. Look for the EXL3 version of this model."]
    return None, ["Spark Swapper can't tell how to run this repo (it has no safetensors weights, EXL3 files "
                  "or GGUF files)."]


def catalog_match(engine, repo: str) -> str | None:
    for key, m in engine.models.items():
        if repo in engine.model_hf_repos(m) or any(f.get("repo") == repo for f in m.get("files") or []):
            return key
        if (m.get("repo") or "").removeprefix("https://huggingface.co/") == repo:
            return key
    return None


def _existing(p: Path) -> Path:
    p = Path(p)
    while not p.exists() and p != p.parent:
        p = p.parent
    return p


def gather(engine, text: str) -> tuple[dict, dict]:
    """(raw Hugging Face info, summary) for a link: what the repo is and whether the box can run it."""
    try:
        repo, revision = fetch.parse_hf_ref(text)
        token = engine.hf_token()
        info = fetch.hf_info(repo, revision, token)
    except fetch.FetchError as e:
        raise ModelError(str(e)) from e
    fmt = detect_format(info)
    profiles = engine.cfg.get("profiles") or {}
    profile, problems = pick_profile(engine.platform, fmt, profiles)
    groups = gguf_groups(info) if fmt == "gguf" else []
    # A GGUF repo holds a dozen quantizations of one model and you pick one: judge it by the smallest.
    size = groups[0]["bytes"] if groups else weight_bytes(info)
    mem = engine.status()["memory"]
    archs = (info.get("config") or {}).get("architectures")
    arch = archs[0] if isinstance(archs, list) and archs and isinstance(archs[0], str) else None
    ctx = native_context(repo, info["sha"], token)
    out = {
        "repo": repo, "commit": info["sha"], "title": repo.split("/", 1)[1],
        "license": info["license"], "gated": bool(info["gated"]), "private": info["private"],
        "format": fmt, "quantization": quant_label(info, fmt), "architecture": arch,
        "pipeline": info["pipeline"], "tags": [t for t in info["tags"] if ":" not in t][:12],
        "size_bytes": size, "files": len(info["files"]), "context_native": ctx,
        "context_default": min(ctx or DEFAULT_CONTEXT, DEFAULT_CONTEXT),
        "profile": profile, "profile_label": profiles.get(profile, {}).get("label"),
        "catalog_key": catalog_match(engine, repo),
    }
    if out["catalog_key"]:
        out["catalog_title"] = engine.models[out["catalog_key"]]["title"]
        out["catalog_installed"] = engine.install_state(out["catalog_key"])["installed"]
    if groups:
        out["gguf"] = [{"id": g["id"], "label": g["label"], "bytes": g["bytes"], "files": len(g["files"])}
                       for g in groups]
        out["size_max_bytes"] = groups[-1]["bytes"]
    need_gib = math.ceil(size / GIB * (1.15 if groups else 1.1) + (6 if groups else 8))
    out["memory_gib"], out["memory_total_gib"] = need_gib, mem.get("total") or None
    if mem.get("total") and need_gib > mem["total"] - 6:
        problems.append(f"It needs about {need_gib} GiB of memory{' even at its smallest quantization' if groups else ''} "
                        f"and this box has {mem['total']:.0f} GiB.")
    # GGUF files go to the Swapper's own download folder, everything else to the Hugging Face cache
    free = shutil.disk_usage(_existing(engine.models_dir if groups else engine.hf_hub())).free
    out["disk_free"] = free
    if size > free - 5 * GIB and not out["catalog_key"]:
        problems.append(f"It is {fetch.human(size)}{' at its smallest quantization' if groups else ''} and only "
                        f"{fetch.human(free)} is free. Delete a model you no longer use on the Storage tab first.")
    warnings = []
    if info["gated"]:
        warnings.append("This model is gated: accept its terms on huggingface.co and save a Hugging Face token "
                        "on the Settings tab, or the download is refused.")
    if profile == "vllm-spark":
        warnings.append("Runs on vLLM from a community image built for the Spark. A brand-new architecture can "
                        "need a newer image than the one pinned in the catalog.")
    if fmt == "exl3" and arch and not arch.startswith(("Qwen", "Llama", "Mistral", "Gemma", "Glm", "Phi", "Cohere")):
        warnings.append(f"{arch} may not be supported by the ExLlamaV3 build in this catalog.")
    out["problems"], out["warnings"] = problems, warnings
    out["supported"] = not problems and profile is not None
    return info, out


def inspect(engine, text: str) -> dict:
    return gather(engine, text)[1]


def clean_extra_args(text: str) -> list[str]:
    """Arguments typed by the admin: split the way a shell would, but never run through one."""
    if not text or not text.strip():
        return []
    if re.search(r"[\x00-\x08\x0b-\x1f]", text):
        raise ModelError("The extra arguments contain control characters.")
    try:
        toks = shlex.split(text)
    except ValueError as e:
        raise ModelError(f"The extra arguments don't parse: {e}") from e
    if len(toks) > MAX_EXTRA_ARGS or any(len(t) > 400 for t in toks):
        raise ModelError("Too many or too long extra arguments.")
    return toks


def build(engine, text: str, options: dict | None = None) -> tuple[str, dict]:
    """(key, entry) for a repo, ready for engine.add_custom_model. Like a catalog entry it keeps
    {{HOME}}, {{API_KEY}} and {{API_PORT}} unexpanded, so it follows the site config."""
    options = options or {}
    info, s = gather(engine, text)
    if s["catalog_key"]:
        raise ModelError(f"{s['catalog_title']} in the catalog already runs this model; install that one.")
    if not s["supported"]:
        raise ModelError(" ".join(s["problems"]) or "This model can't be run on this box.")
    repo, commit, fmt, profile_name = s["repo"], s["commit"], s["format"], s["profile"]
    profile = engine.cfg["profiles"][profile_name]
    key = model_key(repo)
    existing = engine.models.get(key)
    if existing:
        if existing.get("hf_repo") == repo:
            raise ModelError("That model is already in the list.")
        key = f"{key[:53]}-{hashlib.sha1(repo.encode()).hexdigest()[:6]}"
    served = slug(repo.split("/", 1)[1], 40)
    if served in {m.get("served_id") for m in engine.models.values()}:
        served = f"{served[:36]}-hf"
    title = (str(options.get("title") or "").strip() or s["title"])[:80]
    ctx = int(options.get("context") or s["context_default"])
    ctx = max(2048, min(ctx, s["context_native"] or 1_048_576))
    size_gib = s["size_bytes"] / GIB
    total = engine.status()["memory"].get("total") or 121.0
    util = round(min(0.88, max(0.4, (size_gib + 20) / total)), 2)

    entry = config_mod.render(copy.deepcopy(profile["entry"]), {
        "REPO": repo, "REPO_DIR": repo.replace("/", "--"), "SLUG": served, "KEY": key, "COMMIT": commit,
        "CTX": ctx, "UTIL": util})
    entry.update({
        "title": title, "served_id": served, "hf_repo": repo, "hf_commit": commit,
        "tagline": f"Added from huggingface.co/{repo} · {s['quantization'] or s['format']} · {fetch.human(s['size_bytes'])}",
        "order": 100 + len(engine.load_custom()), "context": ctx, "openclaw_context": ctx,
        "openclaw_provider": key, "openclaw_model_name": f"{title} ({profile['engine']}, {max(ctx // 1024, 1)}K ctx)",
        "vision": bool(s["pipeline"] in VISION_PIPELINES and profile.get("vision", False)),
        "repo": f"https://huggingface.co/{repo}", "engine": profile["engine"], "experimental": True,
        "perf": {"source": "not published for this model — measured here after the first load", "estimate": False},
        "min_free_gib": math.ceil(util * total) if profile_name == "vllm-spark" else max(24, math.ceil(size_gib * 1.1 + 10)),
    })
    entry["install"]["size_gb"] = math.ceil(s["size_bytes"] / 1e9) + int(profile.get("image_gb", 0))
    minutes = math.ceil(s["size_bytes"] / 1e9 / 6 / 60) + 5
    entry["install"]["time"] = (f"{minutes}–{minutes * 2 + 10} minutes depending on your connection"
                                + ("; the first install also builds the engine image" if profile.get("build") else ""))
    if fmt == "gguf":
        groups = {g["id"]: g for g in gguf_groups(info)}
        pick = groups.get(str(options.get("gguf") or ""))
        if not pick:
            raise ModelError("Choose which GGUF file to use.")
        pick_gib = math.ceil(pick["bytes"] / GIB * 1.15 + 6)
        if pick_gib > total - 6:
            raise ModelError(f"{pick['label']} needs about {pick_gib} GiB of memory and this box has {total:.0f} GiB; "
                             "choose a smaller quantization.")
        free = shutil.disk_usage(_existing(engine.models_dir)).free
        if pick["bytes"] > free - 5 * GIB:
            raise ModelError(f"{pick['label']} is {fetch.human(pick['bytes'])} and only {fetch.human(free)} is free. "
                             "Delete a model you no longer use on the Storage tab, or choose a smaller quantization.")
        entry["files"] = [{"repo": repo, "path": f, "revision": commit} for f in pick["files"]]
        entry["model_file"] = f"{repo}/{pick['files'][0]}"
        entry["min_free_gib"] = max(16, math.ceil(pick["bytes"] / GIB * 1.15 + 6))
        entry["gpu_gib"] = math.ceil(pick["bytes"] / GIB + 4)
        entry["install"]["size_gb"] = math.ceil(pick["bytes"] / 1e9)
    elif profile_name == "vllm-spark":
        extra = clean_extra_args(str(options.get("extra_args") or ""))
        if options.get("trust_remote_code"):
            extra.append("--trust-remote-code")
        entry["docker_run"] = [*entry["docker_run"], *extra]
    return key, {k: v for k, v in entry.items() if v is not None}
