import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import media


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "потрібен ffmpeg")
class TelegramPreviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.src = self.tmp / "clip.mp4"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=2",
                        "-c:v", "libx264", str(self.src)], check=True)
        self.cfg = {"ffmpeg": "ffmpeg", "ffprobe": "ffprobe", "paths": {"data": str(self.tmp / "d")},
                    "telegram": {"preview_height": 720}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_720_copy_for_telegram_original_untouched(self):
        before = self.src.read_bytes()
        out = media.telegram_copy(str(self.src), self.cfg)
        self.assertNotEqual(out, str(self.src))
        q = media.quality(out, "ffprobe")
        self.assertEqual((q["width"], q["height"]), (720, 1280))
        self.assertEqual(self.src.read_bytes(), before)  # оригінал не змінено

    def test_zero_means_original(self):
        self.cfg["telegram"]["preview_height"] = 0
        self.assertEqual(media.telegram_copy(str(self.src), self.cfg), str(self.src))


if __name__ == "__main__":
    unittest.main()
