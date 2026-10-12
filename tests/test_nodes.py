"""Offline tests: run with ComfyUI's Python (it has torch, av and Pillow), from the repo root:

    python -m unittest discover -s tests -v

No request leaves the machine: requests.request is replaced in every test that would call out.
"""

import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np
import torch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from genmagic import catalog, client, media, nodes  # noqa: E402


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None, content=b""):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.content = content

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class KeyTests(unittest.TestCase):
    def test_missing_key_explains_where_to_put_one_and_sends_nothing(self):
        with mock.patch.dict(os.environ, {"GENMAGIC_API_KEY": ""}), mock.patch.object(client, "settings_key", return_value=""), mock.patch(
            "requests.request"
        ) as request:
            with self.assertRaises(client.GenMagicError) as ctx:
                client.GenMagicClient().text({"prompt": "hi"})
        self.assertIn("Settings > GenMagic > API key", str(ctx.exception))
        self.assertIn("genmagic.co/get-api-key", str(ctx.exception))
        request.assert_not_called()

    def test_key_from_comfyui_settings_and_env_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "default"))
            with open(os.path.join(root, "default", "comfy.settings.json"), "w") as f:
                json.dump({"GenMagic.APIKey": "  gm_from_settings  "}, f)
            folder_paths = types.SimpleNamespace(get_user_directory=lambda: root)
            with mock.patch.dict(sys.modules, {"folder_paths": folder_paths}):
                with mock.patch.dict(os.environ, {"GENMAGIC_API_KEY": ""}):
                    self.assertEqual(client.resolve_api_key(), "gm_from_settings")
                with mock.patch.dict(os.environ, {"GENMAGIC_API_KEY": "gm_from_env"}):
                    self.assertEqual(client.resolve_api_key(), "gm_from_env")

    def test_requests_name_this_pack_and_send_the_key(self):
        res = FakeResponse(body={"text": "ok", "model": "m"}, headers={"X-Cost-Cents": "0.5", "X-Credits-Remaining": "120"})
        with mock.patch("requests.request", return_value=res) as request:
            body, metering = client.GenMagicClient(api_key="gm_test").text({"prompt": "hi"})
        headers = request.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer gm_test")
        self.assertRegex(headers["X-GenMagic-Client"], r"^comfyui/\d+\.\d+\.\d+$")
        self.assertEqual(metering.describe("m"), "model: m, cost: $0.0050, balance: $1.20")


class ErrorTests(unittest.TestCase):
    def call(self, status, error):
        with mock.patch("requests.request", return_value=FakeResponse(status, {"error": error})):
            with self.assertRaises(client.GenMagicError) as ctx:
                client.GenMagicClient(api_key="gm_test").image({"prompt": "x"})
        return str(ctx.exception)

    def test_low_balance_points_to_pricing(self):
        message = self.call(402, {"code": "insufficient_credits", "message": "Insufficient credits."})
        self.assertIn("balance is too low", message)
        self.assertIn("genmagic.co/pricing", message)

    def test_api_message_is_passed_through(self):
        message = self.call(400, {"code": "model_not_found", "message": "No image model 'x' on GenMagic."})
        self.assertIn("model_not_found", message)
        self.assertIn("No image model 'x'", message)

    def test_media_is_only_downloaded_from_genmagic(self):
        with mock.patch("requests.get") as get:
            with self.assertRaises(client.GenMagicError):
                client.GenMagicClient(api_key="gm_test").fetch_media("https://evil.example/clip.mp4")
        get.assert_not_called()


class NodeTests(unittest.TestCase):
    def test_saved_workflows_with_any_model_id_pass_comfyui_validation(self):
        for cls in nodes.NODE_CLASS_MAPPINGS.values():
            self.assertIs(cls.VALIDATE_INPUTS(model="vendor/model-added-later"), True)

    def test_a_frame_the_model_cannot_take_stops_before_any_request(self):
        entry = {"id": "lab/clip", "capabilities": {"text_to_video": True, "frame_images": ["first_frame"]}}
        frame = torch.rand(1, 64, 64, 3)
        with mock.patch.object(catalog, "entry", return_value=entry), mock.patch("requests.request") as request:
            with self.assertRaises(ValueError) as ctx:
                nodes.GenMagicVideo().generate("a boat", "lab/clip", "16:9", 0, "auto", "model default", 0, first_frame=frame, last_frame=frame)
        self.assertIn("cannot use a last frame", str(ctx.exception))
        request.assert_not_called()

    def test_video_sends_frames_and_options_then_polls_to_the_clip(self):
        entry = {"id": "lab/clip", "capabilities": {"text_to_video": True, "frame_images": ["first_frame", "last_frame"]}}
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs.get("json")))
            if method == "POST":
                return FakeResponse(202, {"id": "gen-vid-1", "status": "queued", "model": "lab/clip"})
            return FakeResponse(200, {"id": "gen-vid-1", "status": "completed", "model": "lab/clip", "url": "https://genmagic.co/clip.mp4"}, {"X-Cost-Cents": "40"})

        with mock.patch.object(catalog, "entry", return_value=entry), mock.patch("requests.request", side_effect=fake_request), mock.patch.object(
            client.GenMagicClient, "fetch_media", return_value=b"mp4"
        ), mock.patch.object(nodes, "video_from_bytes", side_effect=lambda data: ("VIDEO", data)), mock.patch.object(nodes, "VIDEO_POLL_SECONDS", 0), mock.patch.dict(
            os.environ, {"GENMAGIC_API_KEY": "gm_test"}
        ):
            video, info = nodes.GenMagicVideo().generate(
                "the boat drifts", "lab/clip", "9:16", 5, "720p", "off", 0, first_frame=torch.rand(1, 32, 32, 3), last_frame=torch.rand(1, 32, 32, 3)
            )
        body = calls[0][2]
        self.assertEqual(body["model"], "lab/clip")
        self.assertEqual((body["aspect_ratio"], body["duration"], body["resolution"], body["generate_audio"]), ("9:16", 5, "720p", False))
        self.assertTrue(body["image"].startswith("data:image/png;base64,"))
        self.assertTrue(body["last_frame"].startswith("data:image/png;base64,"))
        self.assertEqual(video, ("VIDEO", b"mp4"))
        self.assertIn("cost: $0.4000", info)

    def test_auto_sends_no_model(self):
        with mock.patch("requests.request", return_value=FakeResponse(200, {"text": "hello", "model": "default/model"})) as request, mock.patch.dict(
            os.environ, {"GENMAGIC_API_KEY": "gm_test"}
        ):
            text, info = nodes.GenMagicText().generate("hi", "auto", "", 0)
        self.assertNotIn("model", request.call_args.kwargs["json"])
        self.assertEqual(text, "hello")
        self.assertIn("default/model", info)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        catalog._state.update(models=None, at=0.0, failed_at=0.0)

    def tearDown(self):
        catalog._state.update(models=None, at=0.0, failed_at=0.0)

    def test_dropdowns_split_the_live_catalog_by_node(self):
        live = [
            {"id": "v/gen", "category": "video", "capabilities": {"text_to_video": True}},
            {"id": "v/upscaler", "category": "video", "capabilities": {"text_to_video": False}},
            {"id": "a/voice", "category": "audio", "audio_kind": "speech"},
            {"id": "a/song", "category": "audio", "audio_kind": "music"},
            {"id": "i/pic", "category": "image"},
        ]
        with mock.patch("requests.request", return_value=FakeResponse(200, {"data": live})):
            self.assertEqual(catalog.choices("video"), ["auto", "v/gen"])
            self.assertEqual(catalog.choices("speech"), ["auto", "a/voice"])
            self.assertEqual(catalog.choices("music"), ["auto", "a/song"])
            self.assertEqual(catalog.choices("image"), ["auto", "i/pic"])

    def test_unreachable_catalog_falls_back_without_retrying_every_call(self):
        with mock.patch("requests.request", side_effect=client.requests.ConnectionError("offline")) as request:
            self.assertEqual(catalog.choices("video")[0], "auto")
            self.assertIn("google/veo-3.1", catalog.choices("video"))
            catalog.choices("image")
        self.assertEqual(request.call_count, 1)


class MediaTests(unittest.TestCase):
    def test_small_images_go_as_png(self):
        self.assertTrue(media.image_data_url(torch.rand(1, 64, 64, 3)).startswith("data:image/png;base64,"))

    def test_large_images_are_shrunk_under_the_request_limit(self):
        noise = torch.rand(1, 2048, 2048, 3)
        url = media.image_data_url(noise, limit=600_000)
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(len(url.split(",", 1)[1]) * 3 // 4, 600_000)

    def test_returned_images_become_one_batch(self):
        from PIL import Image

        def png(w, h):
            buf = io.BytesIO()
            Image.new("RGB", (w, h), "red").save(buf, format="PNG")
            return buf.getvalue()

        batch = media.stack_images([media.bytes_to_tensor(png(64, 48)), media.bytes_to_tensor(png(32, 32))])
        self.assertEqual(tuple(batch.shape), (2, 48, 64, 3))
        self.assertAlmostEqual(float(batch[1, 0, 0, 0]), 1.0, places=3)

    def test_audio_files_decode_to_comfyui_audio(self):
        import av

        buf = io.BytesIO()
        with av.open(buf, mode="w", format="wav") as out:
            stream = out.add_stream("pcm_s16le", rate=24000, layout="mono")
            samples = (np.sin(np.linspace(0, 440 * 2 * np.pi, 24000)) * 12000).astype(np.int16)[None, :]
            frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
            frame.sample_rate = 24000
            for packet in stream.encode(frame):
                out.mux(packet)
            for packet in stream.encode(None):
                out.mux(packet)
        audio = media.audio_from_bytes(buf.getvalue())
        self.assertEqual(audio["sample_rate"], 24000)
        self.assertEqual(audio["waveform"].shape[0:2], (1, 1))
        self.assertGreater(audio["waveform"].shape[2], 23000)
        self.assertLessEqual(float(audio["waveform"].abs().max()), 1.0)


if __name__ == "__main__":
    unittest.main()
