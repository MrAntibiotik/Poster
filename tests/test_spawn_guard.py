"""Запобіжник від лавини процесів: той самий запуск — не частіше ніж раз на N секунд."""
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.system import control  # noqa: E402
from app.channels import manager  # noqa: E402


class SpawnGuardTest(unittest.TestCase):
    def test_once_per_window(self):
        name = "test-" + uuid.uuid4().hex
        self.assertTrue(control.spawn_allowed(name, 60))
        self.assertFalse(control.spawn_allowed(name, 60))
        self.assertTrue(control.spawn_allowed(name, 0))

    def test_silent_channel_panel_is_not_restarted(self):
        with mock.patch.object(manager, "keys", return_value=["en"]), \
                mock.patch.object(manager, "status", return_value={"panel_running": True, "panel_port": 8771}), \
                mock.patch.object(manager, "panel_version", return_value=""), \
                mock.patch.object(control, "spawn_panel") as sp, mock.patch.object(control, "kill") as kill:
            self.assertEqual(manager.refresh_panels({}), [])
        sp.assert_not_called()
        kill.assert_not_called()


class PanelLockTest(unittest.TestCase):
    def test_second_copy_exits_immediately(self):
        import time as T
        from app.web.panel.server import single_panel_lock
        first = single_panel_lock(18999)
        self.assertIsNotNone(first)
        t0 = T.time()
        self.assertIsNone(single_panel_lock(18999))          # друга копія — одразу «не потрібна»
        self.assertLess(T.time() - t0, 1)
        first.close()
        again = single_panel_lock(18999, wait=2)
        self.assertIsNotNone(again)
        again.close()


if __name__ == "__main__":
    unittest.main()
