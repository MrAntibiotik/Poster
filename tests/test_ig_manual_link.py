import unittest
from types import SimpleNamespace

from app.insights import reports
from app.platforms.meta_graph import Instagram


class FakeG:
    def __init__(self, data):
        self.data = data

    def get(self, path, **kw):
        return {"data": self.data}


class FakeDB:
    def __init__(self, items):
        self.kv, self.saved, self._items = {}, {}, items

    def get(self, k):
        return self.kv.get(k)

    def set(self, k, v):
        self.kv[k] = v

    def items(self, where, args=()):
        return self._items

    def update_item(self, i, **f):
        self.saved[i] = f


class ManualLinkTest(unittest.TestCase):
    def ig(self, data):
        class IG(Instagram):
            ig_id = "1"

            def _g(self):
                return FakeG(data)
        return IG.__new__(IG)

    def test_no_id_no_stats(self):
        self.assertEqual(self.ig([]).get_stats({"status": "ok", "url": ""}), {})

    def test_find_earliest_after(self):
        ig = self.ig([
            {"id": "c", "timestamp": "2026-11-17T00:50:00+0000", "permalink": "u/c"},
            {"id": "b", "timestamp": "2026-11-17T00:40:00+0000", "permalink": "u/b"},
            {"id": "a", "timestamp": "2026-11-16T20:00:00+0000", "permalink": "u/a"},
        ])
        after = 1794873600  # 2026-11-17T00:00:00Z
        self.assertEqual(ig.find_manual(after, set()), {"id": "b", "url": "u/b"})
        self.assertEqual(ig.find_manual(after, {"b"})["id"], "c")

    def test_link_manual_updates_item(self):
        ig = self.ig([{"id": "b", "timestamp": "2026-11-17T00:40:00+0000", "permalink": "u/b"}])
        item = {"id": 49, "posted_at": 1794873600,
                "results": {"instagram": {"status": "ok", "url": "", "sent_at": 1794873600}}}
        db = FakeDB([item])
        app = SimpleNamespace(db=db, platforms={"instagram": ig})
        out = reports.link_manual(app, item)
        self.assertEqual(out["results"]["instagram"]["id"], "b")
        self.assertEqual(db.saved[49]["results"]["instagram"]["url"], "u/b")


if __name__ == "__main__":
    unittest.main()


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
