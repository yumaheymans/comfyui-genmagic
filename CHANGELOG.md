# Changelog

## 1.0.0 (2026-10-12)

First release.

- GenMagic Image: any of GenMagic's image models, 1 to 4 images, optional reference image (image-to-image).
- GenMagic Video: any text-to-video model, clip length, resolution and sound; optional first and last frame (image-to-video).
- GenMagic Speech and GenMagic Music: text to speech and original music as AUDIO.
- GenMagic Text: any of 360+ language models, with an optional image to read; handy for writing prompts for the other nodes.
- Model dropdowns read GenMagic's live catalog (newest first); a saved model id that is no longer listed still runs.
- The API key lives in Settings > GenMagic > API key or the GENMAGIC_API_KEY environment variable, never in a workflow.
