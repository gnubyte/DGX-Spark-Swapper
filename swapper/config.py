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
    return {"USER": user, "HOME": home}


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
    site_models = site.get("models") or {}
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


def load(site_path: Path, catalog_path: Path, platform: str) -> dict:
    site = load_json(site_path)
    if not isinstance(site, dict):
        raise ValueError(f"{site_path} is not a JSON object")
    catalog = load_json(catalog_path, {}) or {}
    return merge(site, catalog, platform)
