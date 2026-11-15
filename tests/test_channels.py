"""Додатковий (англомовний) канал: створення, завантаження налаштувань, англійські тексти, підпис каналу."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core import ai  # noqa: E402
from app.channels import manager as channels  # noqa: E402
from app.system import control  # noqa: E402
from app.core.config import load_config  # noqa: E402
from app.core.meta import PostMeta, apply_always_hashtags, auto_meta_json, title_from_filename  # noqa: E402


class ChannelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        main = self.base / "main"
        main.mkdir()
        (main / "config.json").write_text(json.dumps({"paths": {"ready": "ready"}}), encoding="utf-8")
        (main / ".env").write_text("TELEGRAM_BOT_TOKEN=111:main\nTELEGRAM_CHAT_ID=42\nGEMINI_API_KEY=g-key\n"
                                   "META_PAGE_TOKEN=secret-page\n", encoding="utf-8")
        self.main_cfg = load_config(main / "config.json")
        self.patch = mock.patch.object(channels, "CHANNELS_DIR", self.base / "channels")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def test_create_and_load(self):
        d = channels.create(self.main_cfg, "en", ready=str(self.base / "en_ready"))
        self.assertTrue((d / "config.json").exists())
        cfg = load_config(d / "config.json")
        self.assertEqual(cfg["timezone"], "America/New_York")
        self.assertEqual(cfg["ai"]["language"], "en")
        self.assertEqual(cfg["platforms"]["youtube"]["language"], "en")
        self.assertEqual(Path(cfg["paths"]["ready"]), (self.base / "en_ready").resolve())
        self.assertEqual(Path(cfg["paths"]["data"]), (d / "data").resolve())  # своя база й черга
        self.assertNotEqual(cfg["lock_port"], self.main_cfg["lock_port"])
        self.assertFalse(cfg["updates"]["auto"])
        self.assertEqual(cfg["platforms"]["youtube"]["client_secrets_file"],
                         self.main_cfg["platforms"]["youtube"]["client_secrets_file"])
        env = (d / ".env").read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_CHAT_ID=42", env)
        self.assertIn("GEMINI_API_KEY=g-key", env)
        self.assertNotIn("111:main", env)       # бот — окремий
        self.assertNotIn("secret-page", env)    # акаунти платформ — окремі
        with self.assertRaises(ValueError):
            channels.create(self.main_cfg, "en")

    def test_status(self):
        self.assertFalse(channels.status("en")["exists"])
        channels.create(self.main_cfg, "en")
        with mock.patch.object(control, "is_running", return_value=False):
            st = channels.status("en")
        self.assertTrue(st["exists"])
        self.assertFalse(st["running"])
        self.assertFalse(st["telegram_ready"])
        self.assertEqual(st["platforms"], ["youtube"])
        with self.assertRaises(ValueError):
            channels.start("en")  # без свого бота не запускається
        channels.set_autostart("en", False)
        self.assertFalse(channels.read_raw("en")["channel"]["autostart"])

    def test_start_passes_config(self):
        channels.create(self.main_cfg, "en")
        env = channels.channel_dir("en") / ".env"
        env.write_text(env.read_text(encoding="utf-8").replace("TELEGRAM_BOT_TOKEN=", "TELEGRAM_BOT_TOKEN=222:en"),
                       encoding="utf-8")
        with mock.patch.object(control, "is_running", return_value=False), \
                mock.patch.object(control, "start_bot", return_value=True) as sb:
            self.assertTrue(channels.start("en"))
        self.assertEqual(sb.call_args.kwargs["config"], str(channels.config_path("en")))

    def test_channel_panel_runs_program_from_code_folder(self):
        """Панель EN-каналу: poster.py — з папки програми, а не з channels/en (там лише налаштування)."""
        from app.web import panel
        d = channels.create(self.main_cfg, "en")
        pnl = panel.Panel(str(d / "config.json"))
        self.assertEqual(pnl.root, d.resolve())
        with mock.patch("subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            pnl.run_poster("check")
        args, kw = run.call_args
        self.assertEqual(args[0][1], str(ROOT / "poster.py"))
        self.assertTrue((ROOT / "poster.py").exists())
        self.assertEqual(kw["cwd"], ROOT)
        self.assertIn(str(d / "config.json"), args[0])

    def test_channel_client_secret_never_overwrites_main(self):
        import json as J
        from app.web import panel
        main_secret = Path(self.main_cfg["platforms"]["youtube"]["client_secrets_file"])
        main_secret.write_text(J.dumps({"installed": {"client_id": "MAIN"}}), encoding="utf-8")
        d = channels.create(self.main_cfg, "en")
        (d / "tokens").mkdir(exist_ok=True)
        (d / "tokens" / "youtube_token.json").write_text("{}", encoding="utf-8")   # токен від старого ключа
        pnl = panel.Panel(str(d / "config.json"))
        msg = pnl.youtube_secret(J.dumps({"installed": {"client_id": "POSTER-EU"}}))
        self.assertIn("Авторизувати", msg)
        self.assertEqual(J.loads(main_secret.read_text())["installed"]["client_id"], "MAIN")   # основний цілий
        self.assertEqual(J.loads((d / "client_secret.json").read_text())["installed"]["client_id"], "POSTER-EU")
        self.assertFalse((d / "tokens" / "youtube_token.json").exists())                     # старий токен прибрано
        from app.core.config import load_config
        self.assertEqual(Path(load_config(d / "config.json")["platforms"]["youtube"]["client_secrets_file"]),
                         (d / "client_secret.json").resolve())

    def test_autostart_skipped_inside_channel(self):
        with mock.patch.object(channels, "keys") as k:
            channels.autostart_all({"channel": {"key": "en"}})
        k.assert_not_called()


class EnglishTextsTest(unittest.TestCase):
    def test_prompt_language(self):
        cfg = {"ai": {"language": "en", "channel_description": "WoT shorts"}}
        p = ai.build_prompt(cfg, "WoT", "clip", 5, True)
        self.assertIn("US English", p)
        self.assertNotIn("українською", p)
        p_uk = ai.build_prompt({"ai": {}}, "WoT", "clip", 5, True)
        self.assertIn("українською", p_uk)

    def test_normalize_pool_en(self):
        cfg = {"ai": {"language": "en"}, "auto_meta": {"hashtags": {"*": []}}}
        out = ai.normalize({"title": "Clutch", "hashtags": []}, cfg, "", 3)
        self.assertEqual(out["hashtags"], ["#shorts", "#gaming", "#gameplay"])

    def test_default_title(self):
        self.assertEqual(title_from_filename("2026-11-10 21-33-05", "WoT", "{game} — insane moment 🔥"),
                         "WoT — insane moment 🔥")
        self.assertEqual(title_from_filename("2026-11-10 21-33-05", "WoT"), "WoT — епічний момент 🔥")
        js = auto_meta_json(Path("rec_battle_01_dmg_3837.mp4"), "WoT", {"auto_meta": {"default_title": "{game} clutch"}})
        self.assertEqual(js["youtube"]["title"], "WoT clutch")
        self.assertEqual(title_from_filename("Камбек на прохорівці", "WoT", "x"), "Камбек на прохорівці")

    def test_footer(self):
        footer = "🇺🇦 Ukrainian veteran — subscribe 🛡️♥️"
        cfg = {"footer": {"text": footer}, "always_hashtags": {"tags": ["#wot"]}}
        m = PostMeta(yt_title="Clutch", yt_description="1 vs 4", tiktok_text="Clutch #worldoftanks",
                     instagram_text="Clutch\n\n#worldoftanks #shorts", facebook_text="#tanks")
        m = apply_always_hashtags(m, cfg)
        self.assertEqual(m.yt_description, f"1 vs 4\n\n{footer}")
        self.assertEqual(m.tiktok_text, f"Clutch\n\n{footer}\n\n#worldoftanks #wot")
        self.assertEqual(m.instagram_text, f"Clutch\n\n{footer}\n\n#worldoftanks #shorts #wot")
        self.assertEqual(m.facebook_text, f"{footer}\n\n#tanks #wot")
        again = apply_always_hashtags(m, cfg)  # без дублів
        self.assertEqual(again.yt_description.count(footer), 1)

    def test_no_footer_unchanged(self):
        m = PostMeta(yt_title="T", yt_description="d", tiktok_text="t")
        apply_always_hashtags(m, {"always_hashtags": {"tags": []}})
        self.assertEqual((m.yt_description, m.tiktok_text), ("d", "t"))


if __name__ == "__main__":
    unittest.main()


class DiscoveryTest(unittest.TestCase):
    def test_publish_paths(self):
        import json as _json
        import os as _os
        import tempfile as _tf

        from app.channels import manager as ch
        tmp = Path(_tf.mkdtemp())
        old_env, old_dir = _os.environ.get("LOCALAPPDATA"), ch.CHANNELS_DIR
        _os.environ["LOCALAPPDATA"] = str(tmp)
        ch.CHANNELS_DIR = tmp / "channels"
        try:
            (ch.CHANNELS_DIR / "en").mkdir(parents=True)
            (ch.CHANNELS_DIR / "en" / "config.json").write_text(_json.dumps({"channel": {"name": "EN"}}), encoding="utf-8")
            path = ch.publish_paths({"paths": {"ready": str(tmp / "ready")}})
            data = _json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(path, tmp / "MrAntibiotik" / "poster.json")
            self.assertEqual(data["channels"]["en"]["ready"], str(ch.CHANNELS_DIR / "en" / "ready"))
            self.assertTrue((ch.CHANNELS_DIR / "en" / "ready").is_dir())
            self.assertIsNone(ch.publish_paths({"channel": {"key": "en"}, "paths": {}}))  # лише основний бот
        finally:
            ch.CHANNELS_DIR = old_dir
            if old_env is None:
                _os.environ.pop("LOCALAPPDATA", None)
            else:
                _os.environ["LOCALAPPDATA"] = old_env


class RestartPanelsTest(unittest.TestCase):
    def test_update_restarts_channel_panel_too(self):
        from app.channels import manager as ch
        st = {"running": False, "panel_running": True, "panel_port": 8771}
        with mock.patch.object(ch, "keys", return_value=["en"]), \
                mock.patch.object(ch, "status", return_value=st), \
                mock.patch.object(ch.control, "spawn_panel") as sp:
            ch.restart_running({"paths": {}})
        sp.assert_called_once()
        self.assertEqual(sp.call_args[0][1], 8771)


class RefreshPanelsTest(unittest.TestCase):
    def test_old_panel_is_replaced(self):
        from app.channels import manager as ch
        st = {"panel_running": True, "panel_port": 8771}
        with mock.patch.object(ch, "keys", return_value=["en"]), \
                mock.patch.object(ch, "status", return_value=st), \
                mock.patch.object(ch, "panel_version", return_value="2026.11.14-65"), \
                mock.patch.object(ch.control, "port_owner", return_value={"pid": 4242, "cmd": "pythonw panel.py --port 8771"}), \
                mock.patch.object(ch.control, "kill") as kill, \
                mock.patch.object(ch.control, "spawn_panel") as sp:
            self.assertEqual(ch.refresh_panels({"paths": {}}), ["en"])
        kill.assert_called_once_with(4242)
        sp.assert_called_once()

    def test_current_panel_untouched(self):
        from app.channels import manager as ch
        from app.core.config import VERSION
        with mock.patch.object(ch, "keys", return_value=["en"]), \
                mock.patch.object(ch, "status", return_value={"panel_running": True, "panel_port": 8771}), \
                mock.patch.object(ch, "panel_version", return_value=VERSION), \
                mock.patch.object(ch.control, "spawn_panel") as sp:
            self.assertEqual(ch.refresh_panels({"paths": {}}), [])
        sp.assert_not_called()
