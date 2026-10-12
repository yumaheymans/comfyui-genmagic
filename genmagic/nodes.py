"""GenMagic's ComfyUI nodes: images, video, speech, music and text from 450+ models.

Every node calls GenMagic's REST API with the user's own API key and draws on their
GenMagic balance. A node whose inputs did not change is not run again by ComfyUI, so it is
not billed again: change the seed (or set it to randomize) for a new take.
"""

from __future__ import annotations

import time
from typing import Any

from . import catalog
from .client import GenMagicClient, GenMagicError
from .media import audio_from_bytes, bytes_to_tensor, image_data_url, stack_images, video_from_bytes

CATEGORY = "GenMagic"
VIDEO_WAIT_SECONDS = 15 * 60
VIDEO_POLL_SECONDS = 5


def _model_input(kind: str, tooltip: str) -> tuple:
    return (catalog.choices(kind), {"default": catalog.AUTO, "tooltip": tooltip})


def _seed_input() -> tuple:
    return (
        "INT",
        {
            "default": 0,
            "min": 0,
            "max": 2**31 - 1,
            "control_after_generate": "fixed",
            "tooltip": "Not sent to GenMagic. ComfyUI reruns a node only when an input changes, so change this "
            "(or set it to randomize) for a new take; an unchanged node is not billed twice.",
        },
    )


def _prompt_input(tooltip: str, default: str = "") -> tuple:
    return ("STRING", {"multiline": True, "default": default, "tooltip": tooltip})


def _require(text: str, label: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ValueError(f"{label} is empty.")
    return text


def _interrupted() -> None:
    try:
        import comfy.model_management

        comfy.model_management.throw_exception_if_processing_interrupted()
    except ImportError:
        pass


class _GenMagicNode:
    CATEGORY = CATEGORY

    @classmethod
    def VALIDATE_INPUTS(cls, model):  # noqa: N802 (ComfyUI's hook name)
        # Naming `model` here skips ComfyUI's list check for it, so a workflow saved with a
        # model added or retired since still runs, and GenMagic answers for that id itself.
        return True


class GenMagicImage(_GenMagicNode):
    DESCRIPTION = (
        "Generate images with any of GenMagic's image models (Nano Banana, GPT Image, FLUX, Seedream, Recraft "
        "and more), or transform a reference image. Billed per image to your GenMagic balance."
    )
    RETURN_TYPES = ("IMAGE", "STRING")
    RETURN_NAMES = ("images", "info")
    OUTPUT_TOOLTIPS = ("The generated images as one batch.", "The model that ran, the cost and the balance left.")
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802
        return {
            "required": {
                "prompt": _prompt_input("What to create, or the edit to make when a reference image is connected."),
                "model": _model_input("image", "An image model by id, newest first. auto runs GenMagic's recommended default."),
                "size": (["auto", "1024x1024", "1792x1024", "1024x1792"], {"default": "auto", "tooltip": "Square, wide or tall."}),
                "batch_size": ("INT", {"default": 1, "min": 1, "max": 4, "tooltip": "How many images to make (each one is billed)."}),
                "seed": _seed_input(),
            },
            "optional": {
                "reference": ("IMAGE", {"tooltip": "Optional image to transform (image-to-image); the first of a batch is used."}),
            },
        }

    def generate(self, prompt, model, size, batch_size, seed, reference=None):
        body: dict[str, Any] = {"prompt": _require(prompt, "The prompt"), "n": int(batch_size)}
        if chosen := catalog.chosen(model):
            body["model"] = chosen
        if size != "auto":
            body["size"] = size
        if reference is not None:
            body["image"] = image_data_url(reference)
        client = GenMagicClient()
        result, metering = client.image(body)
        images = []
        for item in result.get("data") or []:
            source = item.get("url") or (f"data:image/png;base64,{item['b64_json']}" if item.get("b64_json") else "")
            if source:
                images.append(bytes_to_tensor(client.fetch_media(source)))
        if not images:
            raise GenMagicError(502, "no_image", "GenMagic returned no image")
        return (stack_images(images), metering.describe(result.get("model") or chosen or catalog.AUTO))


class GenMagicVideo(_GenMagicNode):
    DESCRIPTION = (
        "Generate a video clip with any of GenMagic's video models (Veo, Kling, Seedance, Hailuo, Wan, Runway and "
        "more) from a prompt, or animate an image by connecting a first frame. Billed once, when the clip is ready."
    )
    RETURN_TYPES = ("VIDEO", "STRING")
    RETURN_NAMES = ("video", "info")
    OUTPUT_TOOLTIPS = ("The finished clip.", "The model that ran, the cost and the balance left.")
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802
        return {
            "required": {
                "prompt": _prompt_input("What happens in the clip. With a first frame, describe the motion."),
                "model": _model_input("video", "A video model by id, newest first. auto runs GenMagic's default."),
                "aspect_ratio": (["16:9", "9:16", "1:1"], {"default": "16:9"}),
                "duration": (
                    "INT",
                    {"default": 0, "min": 0, "max": 30, "tooltip": "Clip length in seconds, one the model supports. 0 = the model's default. The price scales with it."},
                ),
                "resolution": (["auto", "480p", "720p", "1080p"], {"default": "auto", "tooltip": "auto = the model's default. The price scales with it."}),
                "sound": (["model default", "on", "off"], {"default": "model default", "tooltip": "Sound, on models that make it (it can roughly double the price)."}),
                "seed": _seed_input(),
            },
            "optional": {
                "first_frame": ("IMAGE", {"tooltip": "Optional image the clip opens on (image-to-video)."}),
                "last_frame": ("IMAGE", {"tooltip": "Optional image the clip ends on, for models that take one."}),
            },
        }

    def generate(self, prompt, model, aspect_ratio, duration, resolution, sound, seed, first_frame=None, last_frame=None):
        body: dict[str, Any] = {"prompt": _require(prompt, "The prompt"), "aspect_ratio": aspect_ratio}
        chosen = catalog.chosen(model)
        if chosen:
            body["model"] = chosen
        if int(duration) > 0:
            body["duration"] = int(duration)
        if resolution != "auto":
            body["resolution"] = resolution
        if sound != "model default":
            body["generate_audio"] = sound == "on"
        frames = {"first_frame": first_frame, "last_frame": last_frame}
        if chosen:
            takes = ((catalog.entry(chosen) or {}).get("capabilities") or {}).get("frame_images")
            for frame, image in frames.items():
                if image is not None and isinstance(takes, list) and frame not in takes:
                    raise ValueError(f"{chosen} cannot use a {frame.replace('_', ' ')}. Pick a model that takes one, or disconnect it.")
        if first_frame is not None:
            body["image"] = image_data_url(first_frame)
        if last_frame is not None:
            body["last_frame"] = image_data_url(last_frame)

        client = GenMagicClient()
        job = client.create_video(body)
        job_id = str(job.get("id") or "")
        if not job_id:
            raise GenMagicError(502, "no_job", "GenMagic did not return a video job id")
        try:
            import comfy.utils

            progress = comfy.utils.ProgressBar(VIDEO_WAIT_SECONDS)
        except ImportError:
            progress = None
        started = time.monotonic()
        metering = None
        while job.get("status") not in ("completed", "failed"):
            elapsed = time.monotonic() - started
            if elapsed > VIDEO_WAIT_SECONDS:
                raise GenMagicError(
                    504,
                    "timeout",
                    f"The clip was not ready after {VIDEO_WAIT_SECONDS // 60} minutes (job {job_id}). It is billed only if it completes.",
                )
            for _ in range(VIDEO_POLL_SECONDS):
                _interrupted()
                time.sleep(1)
            if progress:
                progress.update_absolute(min(int(time.monotonic() - started), VIDEO_WAIT_SECONDS - 1), VIDEO_WAIT_SECONDS)
            job, metering = client.get_video(job_id)
        if job.get("status") == "failed":
            raise GenMagicError(502, "video_failed", f"{job.get('error') or 'The render failed'}. Nothing was billed for it.")
        if progress:
            progress.update_absolute(VIDEO_WAIT_SECONDS, VIDEO_WAIT_SECONDS)
        url = str(job.get("url") or "")
        if not url:
            raise GenMagicError(502, "no_video", "GenMagic finished the job but returned no clip")
        info = (metering.describe(job.get("model")) if metering else f"model: {job.get('model')}") + f", job: {job_id}"
        return (video_from_bytes(client.fetch_media(url)), info)


class GenMagicSpeech(_GenMagicNode):
    DESCRIPTION = "Turn text into spoken audio with GenMagic's speech models. The text is read verbatim."
    RETURN_TYPES = ("AUDIO", "STRING")
    RETURN_NAMES = ("audio", "info")
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802
        return {
            "required": {
                "text": _prompt_input("The words to speak."),
                "model": _model_input("speech", "A speech model by id. auto runs GenMagic's default."),
                "voice": ("STRING", {"default": "", "tooltip": "Optional voice name (for example alloy). Empty = a neutral default voice."}),
                "seed": _seed_input(),
            }
        }

    def generate(self, text, model, voice, seed):
        body: dict[str, Any] = {"input": _require(text, "The text")}
        if chosen := catalog.chosen(model):
            body["model"] = chosen
        if voice.strip():
            body["voice"] = voice.strip()
        data, metering = GenMagicClient().speech(body)
        return (audio_from_bytes(data), metering.describe(chosen))


class GenMagicMusic(_GenMagicNode):
    DESCRIPTION = "Generate an original music track from a description (genre, mood, instruments, tempo) with GenMagic's music models."
    RETURN_TYPES = ("AUDIO", "STRING")
    RETURN_NAMES = ("audio", "info")
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802
        return {
            "required": {
                "prompt": _prompt_input("Genre, mood, instruments and tempo."),
                "model": _model_input("music", "A music model by id. auto runs GenMagic's default."),
                "seed": _seed_input(),
            }
        }

    def generate(self, prompt, model, seed):
        body: dict[str, Any] = {"prompt": _require(prompt, "The prompt")}
        if chosen := catalog.chosen(model):
            body["model"] = chosen
        data, metering = GenMagicClient().music(body)
        return (audio_from_bytes(data), metering.describe(chosen))


class GenMagicText(_GenMagicNode):
    DESCRIPTION = (
        "Write text with any of GenMagic's 360+ language models (Claude, GPT, Gemini, Grok, DeepSeek, Qwen and more): "
        "a prompt for another node, a caption, a script. Connect an image for the model to look at."
    )
    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("text", "info")
    FUNCTION = "generate"

    @classmethod
    def INPUT_TYPES(cls):  # noqa: N802
        return {
            "required": {
                "prompt": _prompt_input("What to write."),
                "model": _model_input("text", "A language model by id, newest first. auto runs GenMagic's default."),
                "system": ("STRING", {"multiline": True, "default": "", "tooltip": "Optional instructions, for example 'Answer with one image prompt only.'"}),
                "seed": _seed_input(),
            },
            "optional": {
                "image": ("IMAGE", {"tooltip": "Optional image for the model to read (use a model that sees images)."}),
            },
        }

    def generate(self, prompt, model, system, seed, image=None):
        body: dict[str, Any] = {"prompt": _require(prompt, "The prompt")}
        if chosen := catalog.chosen(model):
            body["model"] = chosen
        if system.strip():
            body["system"] = system.strip()
        if image is not None:
            data_url = image_data_url(image)
            body["attachments"] = [{"name": "image.png" if data_url.startswith("data:image/png") else "image.jpg", "mime": data_url[5:].split(";")[0], "dataUrl": data_url}]
        result, metering = GenMagicClient().text(body)
        text = str(result.get("text") or "").strip()
        if not text:
            raise GenMagicError(502, "no_text", "GenMagic returned no text")
        return (text, metering.describe(result.get("model") or chosen))


NODE_CLASS_MAPPINGS = {
    "GenMagicImage": GenMagicImage,
    "GenMagicVideo": GenMagicVideo,
    "GenMagicSpeech": GenMagicSpeech,
    "GenMagicMusic": GenMagicMusic,
    "GenMagicText": GenMagicText,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "GenMagicImage": "GenMagic Image",
    "GenMagicVideo": "GenMagic Video",
    "GenMagicSpeech": "GenMagic Speech",
    "GenMagicMusic": "GenMagic Music",
    "GenMagicText": "GenMagic Text",
}
