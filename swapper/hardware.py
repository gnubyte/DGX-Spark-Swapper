"""What this box is: an NVIDIA DGX Spark (GB10) or an AMD Strix Halo (Ryzen AI Max+ 395).

The platform decides which models the catalog offers and which provisioning
steps make sense (Docker with NVIDIA passthrough on a Spark; Vulkan drivers,
llama.cpp and the GPU memory limit on a Strix Halo). Detection never raises;
`SWAPPER_PLATFORM` or the site config's "platform" can force an answer.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

NVIDIA_GB10 = "nvidia-gb10"
AMD_STRIX_HALO = "amd-strix-halo"

LABELS = {
    NVIDIA_GB10: "DGX Spark (NVIDIA GB10)",
    AMD_STRIX_HALO: "Strix Halo (AMD Ryzen AI Max+ 395)",
}

SHORT = {NVIDIA_GB10: "DGX Spark", AMD_STRIX_HALO: "Strix Halo"}

# Strix Halo's integrated GPU: gfx1151 (KFD reports 110501), PCI device 0x1586.
_STRIX_GFX = "110501"
_STRIX_PCI = {"0x1586"}


def label(platform: str) -> str:
    return LABELS.get(platform, platform or "unknown")


def short(platform: str) -> str:
    return SHORT.get(platform, platform or "this box")


def _read(path: str | Path) -> str:
    try:
        return Path(path).read_text(errors="replace").replace("\x00", "").strip()
    except OSError:
        return ""


def cpu_model() -> str:
    for line in _read("/proc/cpuinfo").splitlines():
        if line.lower().startswith("model name"):
            return line.split(":", 1)[1].strip()
    return ""


def _strix_gpu() -> bool:
    for props in Path("/sys/class/kfd/kfd/topology/nodes").glob("*/properties"):
        if re.search(rf"^gfx_target_version\s+{_STRIX_GFX}\s*$", _read(props), re.M):
            return True
    for dev in Path("/sys/class/drm").glob("card[0-9]*/device"):
        if _read(dev / "vendor") == "0x1002" and _read(dev / "device") in _STRIX_PCI:
            return True
    return False


def detect(sh, forced: str | None = None) -> tuple[str, str]:
    """Return (platform, how we know). `sh` is engine.sh (rc, output)."""
    forced = (os.environ.get("SWAPPER_PLATFORM") or forced or "").strip()
    if forced and forced != "auto":
        return forced, "set in the configuration"
    rc, out = sh(["nvidia-smi", "-L"], timeout=10)
    if rc == 0 and "GPU" in out:
        first = out.splitlines()[0].strip()
        return NVIDIA_GB10, first
    model = _read("/proc/device-tree/model") or _read("/sys/firmware/devicetree/base/model")
    if "DGX Spark" in model or "GB10" in model:
        return NVIDIA_GB10, model
    cpu = cpu_model()
    if re.search(r"ryzen\s+ai\s+max", cpu, re.I):
        return AMD_STRIX_HALO, cpu
    if _strix_gpu():
        return AMD_STRIX_HALO, "AMD Radeon 8060S (gfx1151)"
    # The Swapper started life on the Spark; a Spark in a container without
    # nvidia-smi lands here too.
    return NVIDIA_GB10, "no GPU detected — assuming a DGX Spark"


def amd_gpu_memory() -> dict | None:
    """How much memory the AMD GPU may use: the carved-out VRAM plus GTT (system
    memory the GPU can map). On a Strix Halo GTT is the part that matters; the
    kernel's ttm.pages_limit caps it."""
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]*")):
        dev = card / "device"
        if _read(dev / "vendor") != "0x1002":
            continue
        try:
            vram = int(_read(dev / "mem_info_vram_total") or 0)
            gtt = int(_read(dev / "mem_info_gtt_total") or 0)
        except ValueError:
            continue
        if not (vram or gtt):
            continue
        return {"card": card.name, "vram_gib": round(vram / 2**30, 1), "gtt_gib": round(gtt / 2**30, 1),
                "total_gib": round((vram + gtt) / 2**30, 1)}
    return None


def kernel_cmdline() -> str:
    return _read("/proc/cmdline")


def ttm_pages_limit() -> int | None:
    m = re.search(r"\bttm\.pages_limit=(\d+)", kernel_cmdline())
    return int(m.group(1)) if m else None


def in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("SWAPPER_IN_DOCKER") == "1"
