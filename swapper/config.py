"""The site config (/etc/spark-swapper/models.json) merged with the model catalog.

The site config holds what belongs to this box: ports, the API key, the
OpenClaw user, and any per-model overrides. The catalog (catalog.json, shipped
with every release) holds the models. Keeping them apart is what lets an update
add new models to a box whose config was written years ago: a catalog model is
merged *under* a site entry with the same key, field by field, so local edits
win and new fields (speeds, install steps) still arrive.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


def render(obj, values: dict):
    """Fill {{NAME}} placeholders in every string of a JSON-like structure.
    Unknown names are left as they are."""
    if isinstance(obj, str):
        return _PLACEHOLDER.sub(lambda m: str(values.get(m.group(1), m.group(0))), obj)
    if isinstance(obj, list):
        return [render(v, values) for v in obj]
    if isinstance(obj, dict):
        return {k: render(v, values) for k, v in obj.items()}
    return obj


def site_values(site: dict) -> dict:
    oc = site.get("openclaw") or {}
    user = oc.get("user") or "root"
    home = oc.get("home") or ("/root" if user == "root" else f"/home/{user}")
    return {"USER": user, "HOME": home, "API_KEY": str(site.get("api_key", "vllm-local")),
            "API_PORT": str(site.get("api_port", 8888))}


LEGACY_V1 = Path(__file__).resolve().parent / "legacy_v1.json"


def strip_legacy(site_models: dict, values: dict, legacy: dict | None) -> dict:
    """Drop the fields of site entries that still hold the values the 1.x
    template shipped. A box installed with 1.x has complete "standard" and
    "flash" entries in its site config; left alone they would shadow the
    catalog's (titles, order, speeds) and show up as duplicates of the new
    TensorFold models. Anything the admin actually changed is kept."""
    old = render((legacy or {}).get("models") or {}, {**values, "USER": values["USER"]})
    out = {}
    for key, entry in site_models.items():
        shipped = old.get(key)
        if not shipped or not isinstance(entry, dict):
            out[key] = entry
            continue
        kept = {f: v for f, v in entry.items() if f not in shipped or shipped[f] != v}
        if kept:
            out[key] = kept
    return out


def load_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def merge(site: dict, catalog: dict, platform: str) -> dict:
    """The effective config: the site config with "models" and "recipes"
    replaced by catalog ⊕ site for this platform. Models carry a "source" field
    ("catalog", "site" or "catalog+site") so the UI can say where they came from."""
    values = site_values(site)
    cat_models = render((catalog or {}).get("models") or {}, values)
    cat_recipes = render((catalog or {}).get("recipes") or {}, values)
    site_models = strip_legacy(site.get("models") or {}, values, load_json(LEGACY_V1, {}))
    hidden = set(site.get("hidden_models") or [])

    models: dict[str, dict] = {}
    for key, m in cat_models.items():
        if key in hidden:
            continue
        local = site_models.get(key)
        merged = {**m, **(local or {})}
        merged["source"] = "catalog+site" if local else "catalog"
        models[key] = merged
    for key, m in site_models.items():
        if key in hidden or key in cat_models:
            continue
        models[key] = {**m, "source": "site"}

    # A model without "platforms" (an old site entry) runs anywhere.
    models = {k: m for k, m in models.items() if platform in (m.get("platforms") or [platform])}
    for key, m in models.items():
        m["key"] = key

    out = {k: v for k, v in site.items() if k not in ("models", "recipes")}
    out["models"] = models
    out["recipes"] = {**cat_recipes, **(site.get("recipes") or {})}
    out["catalog_version"] = (catalog or {}).get("catalog_version", 1)
    return out


class ConfigError(Exception):
    pass


def load_site(site_path: Path) -> dict:
    """The site config, or a ConfigError that says what's wrong — never silent
    defaults (they would mean the wrong API key and user)."""
    try:
        text = Path(site_path).read_text()
    except OSError as e:
        raise ConfigError(f"Can't read {site_path}: {e}") from e
    try:
        site = json.loads(text)
    except ValueError as e:
        raise ConfigError(f"{site_path} isn't valid JSON ({e}). Fix it and restart: "
                          "sudo systemctl restart spark-swapper") from e
    if not isinstance(site, dict):
        raise ConfigError(f"{site_path} must be a JSON object.")
    if not isinstance(site.get("openclaw"), dict):
        raise ConfigError(f"{site_path} has no \"openclaw\" section (user, home, bin, unit).")
    return site


def load(site_path: Path, catalog_path: Path, platform: str) -> dict:
    catalog = load_json(catalog_path, {}) or {}
    return merge(load_site(site_path), catalog, platform)
