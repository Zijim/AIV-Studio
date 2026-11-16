import array
import math
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import av

from app.composition import CompositionError, compose_timeline, probe_clip


def make_clip(path, *, width=64, height=96, frames=36, frequency=440, audio=True):
    samples = frames * 32000 // 24
    with av.open(str(path), "w") as out:
        video = out.add_stream("libx264", rate=24)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        sound = out.add_stream("aac", rate=32000) if audio else None
        if sound is not None:
            sound.layout = "stereo"
        for index in range(frames):
            frame = av.VideoFrame(width, height, "yuv420p")
            for plane, value in zip(frame.planes, [100, 128, 128]):
                plane.update(bytes([value]) * plane.buffer_size)
            frame.pts, frame.time_base = index, Fraction(1, 24)
            for packet in video.encode(frame):
                out.mux(packet)
        for packet in video.encode():
            out.mux(packet)
        if sound is not None:
            for start in range(0, samples, 1024):
                size = min(1024, samples - start)
                frame = av.AudioFrame(format="fltp", layout="stereo", samples=size)
                frame.sample_rate, frame.pts, frame.time_base = 32000, start, Fraction(1, 32000)
                values = array.array(
                    "f", [0.1 * math.sin(2 * math.pi * frequency * i / 32000) for i in range(start, start + size)]
                ).tobytes()
                for plane in frame.planes:
                    plane.update(values + bytes(plane.buffer_size - len(values)))
                for packet in sound.encode(frame):
                    out.mux(packet)
            for packet in sound.encode():
                out.mux(packet)


class ProbeTests(unittest.TestCase):
    def test_probe_reports_geometry_frames_and_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "clip.mp4"
            make_clip(path)
            info = probe_clip(path)
            self.assertEqual((info["width"], info["height"]), (64, 96))
            self.assertEqual(info["fps"], 24.0)
            self.assertEqual(info["frames"], 36)
            self.assertTrue(info["has_audio"])

    def test_non_video_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "note.txt"
            path.write_text("not a movie", encoding="utf-8")
            with self.assertRaises(CompositionError) as raised:
                probe_clip(path)
            self.assertEqual(raised.exception.code, "not_video")


class ComposeTests(unittest.TestCase):
    def test_composes_exact_frames_and_keeps_native_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            first, second = root / "a.mp4", root / "b.mp4"
            make_clip(first)
            make_clip(second, frequency=880)
            output = root / "final.mp4"
            result = compose_timeline(
                [
                    {"path": first, "in_frame": 6, "frames": 24, "asset_id": "a"},
                    {"path": second, "in_frame": 0, "frames": 24, "asset_id": "b"},
                ],
                output,
                target_frames=48,
            )
            self.assertEqual(result["frames"], 48)
            self.assertEqual(result["duration"], 2.0)
            self.assertEqual((result["width"], result["height"]), (64, 96))
            self.assertTrue(output.is_file())
            self.assertEqual([clip["asset_id"] for clip in result["clips"]], ["a", "b"])
            with av.open(result["output"]) as source:
                self.assertEqual(sum(1 for _ in source.decode(video=0)), 48)
            with av.open(result["output"]) as source:
                self.assertTrue(any(True for _ in source.decode(audio=0)))

    def test_rejects_frame_total_mismatch_before_encoding(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clip = root / "a.mp4"
            make_clip(clip)
            output = root / "final.mp4"
            with self.assertRaises(CompositionError) as raised:
                compose_timeline([{"path": clip, "in_frame": 0, "frames": 24}], output, target_frames=30)
            self.assertEqual(raised.exception.code, "frame_mismatch")
            self.assertEqual(raised.exception.details["difference"], -6)
            self.assertFalse(output.exists())

    def test_rejects_size_mismatch(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            wide, tall = root / "wide.mp4", root / "tall.mp4"
            make_clip(wide, width=96, height=64)
            make_clip(tall)
            output = root / "final.mp4"
            with self.assertRaises(CompositionError) as raised:
                compose_timeline(
                    [{"path": wide, "in_frame": 0, "frames": 24}, {"path": tall, "in_frame": 0, "frames": 24}],
                    output,
                )
            self.assertEqual(raised.exception.code, "media_mismatch")
            reported = {(item["width"], item["height"]) for item in raised.exception.details["clips"]}
            self.assertEqual(reported, {(96, 64), (64, 96)})
            self.assertFalse(output.exists())

    def test_rejects_missing_native_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            silent = root / "silent.mp4"
            make_clip(silent, audio=False)
            output = root / "final.mp4"
            with self.assertRaises(CompositionError) as raised:
                compose_timeline([{"path": silent, "in_frame": 0, "frames": 24}], output)
            self.assertEqual(raised.exception.code, "audio_missing")
            self.assertFalse(output.exists())

    def test_rejects_range_beyond_decoded_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            clip = root / "a.mp4"
            make_clip(clip)
            output = root / "final.mp4"
            with self.assertRaises(CompositionError) as raised:
                compose_timeline([{"path": clip, "in_frame": 20, "frames": 24}], output)
            self.assertEqual(raised.exception.code, "range_invalid")
            self.assertEqual(raised.exception.details["available_frames"], 36)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
