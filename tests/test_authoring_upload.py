from __future__ import annotations

import base64
import io
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from vlmbench.data_model import MetadataRepository
from vlmbench.web.authoring_app import server


def image_data_url(*, color: str = "navy", image_format: str = "PNG") -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, format=image_format)
    mime = "jpeg" if image_format == "JPEG" else image_format.lower()
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/{mime};base64,{encoded}"


class AuthoringUploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.previous_input = server.AUTHORING_INPUT
        self.previous_output = server.AUTHORING_OUTPUT

    def tearDown(self) -> None:
        server.configure_authoring(self.previous_input, self.previous_output)
        self.temporary.cleanup()

    def test_directory_upload_validates_sanitizes_and_never_overwrites(self) -> None:
        input_root = self.root / "real_images"
        output_root = self.root / "authoring"
        server.configure_authoring(input_root, output_root)

        first = server.save_authoring_upload("../My real image.png", image_data_url())
        second = server.save_authoring_upload("My real image.png", image_data_url(color="red"))

        self.assertEqual(first["storage"], "directory")
        self.assertEqual(first["original_filename"], "My_real_image.png")
        self.assertEqual(second["original_filename"], "My_real_image__2.png")
        self.assertTrue((input_root / first["original_filename"]).exists())
        self.assertTrue((input_root / second["original_filename"]).exists())
        with Image.open(input_root / second["original_filename"]) as image:
            self.assertEqual(image.size, (32, 24))

    def test_metadata_upload_enters_canonical_authoring_queue(self) -> None:
        input_root = self.root / "real_images"
        output_root = self.root / "authoring"
        repository = MetadataRepository(output_root)
        repository.write("assets", [])
        repository.write("samples", [])
        server.configure_authoring(input_root, output_root)

        result = server.save_authoring_upload("camera photo.webp", image_data_url())

        self.assertEqual(result["storage"], "metadata")
        self.assertEqual(result["item_id"], "real_001")
        self.assertTrue((input_root / "camera_photo.png").exists())
        assets = repository.load("assets")
        samples = repository.load("samples")
        edits = repository.load("edits")
        self.assertEqual([row["asset_id"] for row in assets], ["real_001_source"])
        self.assertEqual([row["sample_id"] for row in samples], ["real_001"])
        self.assertEqual([row["edit_id"] for row in edits], ["real_001_edit_001"])
        self.assertTrue((output_root / assets[0]["path"]).exists())
        queue = server.new_authoring_items()
        self.assertEqual([row["id"] for row in queue["items"]], ["real_001"])
        self.assertEqual(queue["items"][0]["original_filename"], "camera_photo.png")

    def test_invalid_authoring_upload_is_rejected_without_creating_files(self) -> None:
        input_root = self.root / "real_images"
        server.configure_authoring(input_root, self.root / "authoring")

        invalid = "data:image/png;base64," + base64.b64encode(b"not an image").decode("ascii")
        with self.assertRaisesRegex(ValueError, "not a valid"):
            server.save_authoring_upload("broken.png", invalid)
        self.assertFalse(input_root.exists())


if __name__ == "__main__":
    unittest.main()
