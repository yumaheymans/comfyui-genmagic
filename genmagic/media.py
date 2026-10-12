"""Conversions between ComfyUI's types and the files GenMagic sends and returns.

IMAGE is a float tensor [batch, height, width, channels] in 0..1; AUDIO is
{"waveform": float tensor [1, channels, samples], "sample_rate": int}; VIDEO is ComfyUI's
own VideoFromFile, so the clip flows into Save Video, Get Video Components and the rest.
"""

from __future__ import annotations

import base64
import io

import numpy as np
import torch
from PIL import Image, ImageOps

# GenMagic takes a request body of about 4.5 MB, so an image sent inline stays well under it.
MAX_INLINE_IMAGE_BYTES = 3_000_000


def tensor_to_pil(image: torch.Tensor) -> Image.Image:
    """The first image of a ComfyUI IMAGE batch as an RGB(A) picture."""
    if image.ndim == 4:
        image = image[0]
    array = (image.detach().cpu().clamp(0, 1).numpy() * 255).round().astype(np.uint8)
    if array.shape[-1] == 1:
        array = array[..., 0]
    return Image.fromarray(array)


def image_data_url(image: torch.Tensor, limit: int = MAX_INLINE_IMAGE_BYTES) -> str:
    """A data URL for the first image of a batch: PNG when it fits, else JPEG, scaled down
    only as far as the request size requires."""
    picture = tensor_to_pil(image)
    png = io.BytesIO()
    picture.save(png, format="PNG", optimize=True)
    if png.tell() <= limit:
        return "data:image/png;base64," + base64.b64encode(png.getvalue()).decode("ascii")
    picture = picture.convert("RGB")
    while True:
        jpeg = io.BytesIO()
        picture.save(jpeg, format="JPEG", quality=92)
        if jpeg.tell() <= limit or min(picture.size) <= 256:
            return "data:image/jpeg;base64," + base64.b64encode(jpeg.getvalue()).decode("ascii")
        scale = max(0.5, (limit / jpeg.tell()) ** 0.5 * 0.95)
        picture = picture.resize((max(1, int(picture.width * scale)), max(1, int(picture.height * scale))), Image.LANCZOS)


def bytes_to_tensor(data: bytes) -> torch.Tensor:
    """An image file as a one-image IMAGE batch [1, H, W, 3]."""
    picture = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    picture = picture.convert("RGB")
    array = np.asarray(picture, dtype=np.float32) / 255.0
    return torch.from_numpy(array)[None, ...]


def stack_images(images: list[torch.Tensor]) -> torch.Tensor:
    """One IMAGE batch from several single images; later ones are resized to the first's
    size, since a batch must share one size."""
    first = images[0]
    height, width = first.shape[1], first.shape[2]
    out = [first]
    for image in images[1:]:
        if image.shape[1] != height or image.shape[2] != width:
            resized = tensor_to_pil(image).convert("RGB").resize((width, height), Image.LANCZOS)
            image = torch.from_numpy(np.asarray(resized, dtype=np.float32) / 255.0)[None, ...]
        out.append(image)
    return torch.cat(out, dim=0)


def audio_from_bytes(data: bytes) -> dict:
    """Any audio file GenMagic returns (mp3, wav, ...) as a ComfyUI AUDIO."""
    import av  # bundled with ComfyUI

    with av.open(io.BytesIO(data)) as container:
        if not container.streams.audio:
            raise ValueError("GenMagic returned a file with no audio in it.")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="fltp", layout=stream.layout.name if stream.layout else "stereo", rate=stream.rate)
        chunks: list[np.ndarray] = []
        for frame in container.decode(stream):
            for planar in resampler.resample(frame):
                chunks.append(planar.to_ndarray())
        for planar in resampler.resample(None):
            chunks.append(planar.to_ndarray())
        rate = stream.rate
    if not chunks:
        raise ValueError("GenMagic returned an empty audio file.")
    waveform = torch.from_numpy(np.concatenate(chunks, axis=1).astype(np.float32))
    return {"waveform": waveform.unsqueeze(0).contiguous(), "sample_rate": int(rate)}


def video_from_bytes(data: bytes):
    """A finished clip as ComfyUI's VIDEO."""
    from comfy_api.latest import InputImpl  # ComfyUI 0.3.40 and newer

    return InputImpl.VideoFromFile(io.BytesIO(data))
