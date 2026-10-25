#!/usr/bin/env bash
# Qwen-Image on this box, for the box user (no sudo): the qwen-image command, its units and the ComfyUI node.
#   bash deploy/pictures/install.sh
# Needs ComfyUI in ~/ComfyUI with Qwen-Image 2.1's files in its models folders (README: "Pictures and the context
# window"). Spark Swapper's Models tab then turns pictures on and off.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
[[ -d ~/ComfyUI ]] || { echo "ComfyUI isn't in ~/ComfyUI." >&2; exit 1; }
install -Dm755 "$here/qwen-image" ~/.local/bin/qwen-image
install -Dm644 -t ~/.config/systemd/user "$here/qwen-image.service" "$here/qwen-image-idle.service" "$here/qwen-image-idle.timer"
install -Dm644 "$here/comfyui/qwen_image_uncensored/__init__.py" ~/ComfyUI/custom_nodes/qwen_image_uncensored/__init__.py
mkdir -p ~/qwen-image/output ~/qwen-image/comfy-output
systemctl --user daemon-reload
systemctl --user enable --now qwen-image-idle.timer
echo "Installed. qwen-image --status:"
~/.local/bin/qwen-image --status
