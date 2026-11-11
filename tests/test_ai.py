"""Тести ШІ-текстів (Gemini підмінено підставними відповідями; кадри — справжнім ffmpeg, якщо є)."""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ai  # noqa: E402
from meta import load_meta  # noqa: E402
from tests.test_flow import FlowBase  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code, self.text = data, status, json.dumps(data)
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._d


def gemini_ok(payload: dict) -> Resp:
    return Resp({"candidates": [{"content": {"parts": [{"text": json.dumps(payload, ensure_ascii=False)}]}}]})


ANSWER = {"title": "«Кранвагн тягне бій сам» 🔥", "description": "Неймовірний камбек на Прохорівці.",
          "hashtags": ["#wot", "танки", "#worldoftanks"],
          "tiktok": "Камбек року #wot", "instagram": "Коли команда злилась, а ти ні"}


class AITest(FlowBase):
    def setUp(self):
        super().setUp()
        self.cfg["secrets"]["GEMINI_API_KEY"] = "test-key"
        self.calls = []
        self._orig = ai.requests
        self.answer = gemini_ok(ANSWER)

        def post(url, json=None, headers=None, timeout=None):
            self.calls.append({"url": url, "json": json, "headers": headers})
            return self.answer
        ai.requests = types.SimpleNamespace(post=post, RequestException=Exception)

    def tearDown(self):
        ai.requests = self._orig
        super().tearDown()

    def real_video(self, name="бій.mp4", game="WoT"):
        d = self.cfg["paths"]["ready"] / game
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=360x640:rate=10",
                        "-t", "3", "-pix_fmt", "yuv420p", str(p)], check=True)
        old = os.path.getmtime(p) - 120
        os.utime(p, (old, old))
        return p

    def test_normalize(self):
        r = ai.normalize(ANSWER, self.cfg, "WoT", 5)
        self.assertEqual(r["title"], "Кранвагн тягне бій сам» 🔥")  # зовнішні лапки прибрано

        self.assertEqual(len(r["hashtags"]), 5)
        self.assertEqual(r["hashtags"][:3], ["#wot", "#танки", "#worldoftanks"])
        self.assertIn("#shorts", r["hashtags"])
        self.assertEqual(r["tiktok"].count("#wot"), 1)  # хештеги з тексту ШІ не дублюються
        self.assertTrue(r["instagram"].endswith(" ".join(r["hashtags"])))

    @unittest.skipUnless(HAS_FFMPEG, "потрібен ffmpeg")
    def test_generate_sends_frames(self):
        mp4 = self.real_video()
        r = ai.generate(self.cfg, mp4, "WoT")
        body = self.calls[0]["json"]
        images = [p for p in body["contents"][0]["parts"] if "inline_data" in p]
        self.assertEqual(len(images), 4)
        self.assertTrue(base64.b64decode(images[0]["inline_data"]["data"]).startswith(b"\xff\xd8"))  # JPEG
        prompt = body["contents"][0]["parts"][-1]["text"]
        self.assertIn("рівно 5 хештегів", prompt)
        self.assertIn("«WoT»", prompt)
        self.assertEqual(self.calls[0]["headers"]["x-goog-api-key"], "test-key")
        self.assertIn("gemini-3.5-flash:generateContent", self.calls[0]["url"])
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(len(r["hashtags"]), 5)

    def test_errors(self):
        mp4 = self.cfg["paths"]["ready"] / "x.mp4"
        mp4.parent.mkdir(parents=True, exist_ok=True)
        mp4.write_bytes(b"0")
        for status, data, text in [(429, {}, "ліміт"), (403, {"error": {"message": "API key not valid"}}, "Ключ"),
                                   (404, {}, "Модель"), (200, {"candidates": []}, "розібрати")]:
            self.answer = Resp(data, status)
            with self.assertRaisesRegex(ai.AIError, text):
                ai.generate(self.cfg, mp4, "")
        self.cfg["secrets"]["GEMINI_API_KEY"] = ""
        self.assertFalse(ai.enabled(self.cfg))

    @unittest.skipUnless(HAS_FFMPEG, "потрібен ffmpeg")
    def test_queue_gets_ai_texts_and_regen(self):
        mp4 = self.real_video("2026-11-10 21-33-05.mp4")
        self.app.scheduler.tick(self.ts(11, "11:00"))  # скан + ШІ у фоні (тут — синхронно)
        m = load_meta(mp4.with_suffix(".json"))
        self.assertTrue(m.ai)
        self.assertFalse(m.auto)
        self.assertIn("Кранвагн", m.yt_title)
        self.assertEqual(len(m.hashtags), 5)
        n_calls = len(self.calls)
        self.app.scheduler.tick(self.ts(11, "11:01"))
        self.assertEqual(len(self.calls), n_calls)  # повторно не генерує

        self.app.scheduler.tick(self.ts(11, "12:00"))
        cap = [s for s in self.app.bot.tg.sent if s[0] == "video"][0][1]
        self.assertIn("Кранвагн", cap)
        self.assertIn("Тексти від ШІ", cap)
        # «Перегенерувати тексти» -> новий варіант від ШІ
        self.answer = gemini_ok({**ANSWER, "title": "Інший варіант"})
        item = self.app.db.items("status='notified'")[0]
        self.app.bot._regen_ai(item["id"])
        self.assertEqual(load_meta(mp4.with_suffix(".json")).yt_title, "Інший варіант")
        self.assertTrue(any("Інший варіант" in e[1] for e in self.app.bot.tg.edits))

    def test_ai_failure_keeps_filename_texts(self):
        self.answer = Resp({}, 429)
        d = self.cfg["paths"]["ready"] / "WoT"
        d.mkdir(parents=True, exist_ok=True)
        p = d / "супер_бій.mp4"
        p.write_bytes(b"0")
        os.utime(p, (os.path.getmtime(p) - 120,) * 2)
        self.app.scheduler.tick(self.ts(11, "11:00"))
        m = load_meta(p.with_suffix(".json"))
        self.assertEqual(m.yt_title, "Супер бій")
        self.assertTrue(m.auto)
        self.assertEqual(len(self.app.db.queue()), 1)


if __name__ == "__main__":
    unittest.main()
