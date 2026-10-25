"""A CLIP loader for Qwen-Image 2.1 with a GGUF text encoder, such as AtomicChat's uncensored Qwen3-VL-8B.

ComfyUI recognises Qwen-Image 2.1's encoder by its vision tower, which a GGUF text encoder leaves out. This loader
reads the GGUF's text weights through ComfyUI-GGUF and takes the vision tower (plain BF16) from the stock encoder.
"""
import sys

import comfy.sd
import folder_paths
import nodes
from safetensors import safe_open


class CLIPLoaderGGUFQwenImage21:
    @classmethod
    def INPUT_TYPES(cls):
        # ComfyUI-GGUF registers .gguf text encoders as "clip_gguf"; ComfyUI's own list leaves them out.
        return {"required": {
            "clip_name": (folder_paths.get_filename_list("clip_gguf"),),
            "vision_from": (folder_paths.get_filename_list("text_encoders"),),
        }}

    RETURN_TYPES = ("CLIP",)
    FUNCTION = "load_clip"
    CATEGORY = "advanced/loaders"
    TITLE = "CLIPLoader (GGUF) for Qwen-Image 2.1"

    def load_clip(self, clip_name, vision_from):
        base = nodes.NODE_CLASS_MAPPINGS["CLIPLoaderGGUF"]
        gguf_clip_loader = sys.modules[base.__module__].gguf_clip_loader
        path = folder_paths.get_full_path_or_raise("clip_gguf", clip_name)
        sd = gguf_clip_loader(path)
        with safe_open(folder_paths.get_full_path_or_raise("text_encoders", vision_from), "pt") as f:
            for key in f.keys():
                if key.startswith("model.visual."):
                    sd[key] = f.get_tensor(key)
        return (base().load_patcher([path], comfy.sd.CLIPType.QWEN_IMAGE, [sd]),)


NODE_CLASS_MAPPINGS = {"CLIPLoaderGGUFQwenImage21": CLIPLoaderGGUFQwenImage21}
NODE_DISPLAY_NAME_MAPPINGS = {"CLIPLoaderGGUFQwenImage21": CLIPLoaderGGUFQwenImage21.TITLE}
