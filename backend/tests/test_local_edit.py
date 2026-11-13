import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageOps

from app.config import ROOT
from app.errors import AppError
from app.local_edit import prepare_local_edit_source
from app.registry import Registry
from app.schemas import LOCAL_EDIT_TOOL


class LocalEditTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.registry = Registry(ROOT / "tool_registry", self.folder)
        self.assets = {"source_image": {"media_type": "image", "media_info": {"width": 1901, "height": 1277}}}
        self.values = {
            "instruction": "Restore the patch",
            "seed": 20,
            "region_x": 0.9,
            "region_y": 0.9,
            "region_width": 0.1,
            "region_height": 0.1,
        }

    def test_non_square_edge_crop_uses_original_coordinates_for_final_output(self):
        params, _, dimensions = self.registry.normalize(LOCAL_EDIT_TOOL, self.values, self.assets, SimpleNamespace())
        self.assertEqual(dimensions, {"width": 1901, "height": 1277})
        graph = self.registry.render(
            LOCAL_EDIT_TOOL, "1.0.0", params, {"source_image": "source.png"}, "test", source_size=(1901, 1277)
        )
        crop, mask, paste = (graph[node]["inputs"] for node in ("30", "35", "38"))
        self.assertEqual((crop["x"], crop["y"]), (paste["x"], paste["y"]))
        self.assertEqual(crop["x"] + mask["x"], round(1901 * 0.9))
        self.assertEqual(crop["y"] + mask["y"], round(1277 * 0.9))
        self.assertEqual(crop["x"] + crop["width"], 1901)
        self.assertEqual(crop["y"] + crop["height"], 1277)
        self.assertEqual(graph["33"]["inputs"]["width"], 1901 - round(1901 * 0.9))
        self.assertEqual(paste["destination"], ["7", 0])
        self.assertEqual(graph["16"]["inputs"]["images"], ["39", 0])

    def test_invalid_region_is_rejected_during_planning(self):
        for region in ({"region_x": 0.95}, {"region_width": 0}, {"region_y": float("nan")}, {"region_x": True}):
            with self.subTest(region=region), self.assertRaises(AppError):
                self.registry.normalize(LOCAL_EDIT_TOOL, {**self.values, **region}, self.assets, SimpleNamespace())

    def test_upload_uses_oriented_pixels_for_rotated_jpeg(self):
        source = self.folder / "rotated.jpg"
        upload = self.folder / "upload.png"
        image = Image.new("RGB", (80, 48), "red")
        image.paste("blue", (0, 0, 40, 48))
        exif = image.getexif()
        exif[274] = 6
        image.save(source, exif=exif)

        self.assertEqual(prepare_local_edit_source(source, upload), (48, 80))
        with Image.open(source) as raw, Image.open(upload) as oriented:
            self.assertEqual(oriented.tobytes(), ImageOps.exif_transpose(raw).convert("RGBA").tobytes())
