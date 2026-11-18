import unittest

from app.platforms.meta_graph import Instagram


class KindsTest(unittest.TestCase):
    def test_all_kinds_tried(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from app.core import media

        class IG(Instagram):
            ig_id = "1"
        ig = IG.__new__(IG)
        ig.cfg = {}
        seen = []
        ig.test_upload = lambda clip, label, params=None, wait_min=20: (seen.append(params) or (True, label + " ok"))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(media, "run_hidden", lambda *a, **k: mock.Mock(returncode=0)):
            out = ig.test_kinds(Path(d), 59)
        self.assertEqual(len(out), len(Instagram.KINDS))
        self.assertEqual({p["media_type"] for p in seen}, {"REELS", "STORIES", "VIDEO"})
