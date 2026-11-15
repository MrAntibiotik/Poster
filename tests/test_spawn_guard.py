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


if __name__ == "__main__":
    unittest.main()
