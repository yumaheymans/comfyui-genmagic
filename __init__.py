"""GenMagic for ComfyUI: images, video, speech, music and text from 450+ models with one API key.

https://genmagic.co/developers#comfyui
"""

from .genmagic.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS

WEB_DIRECTORY = "./web"

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
