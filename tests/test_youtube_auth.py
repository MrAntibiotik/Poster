"""Вхід у YouTube з панелі: посилання видно навіть без браузера; без вічного очікування."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import google_auth_oauthlib.flow as gflow  # noqa: E402

from app.platforms.youtube import YouTube  # noqa: E402

URL = "https://accounts.google.com/o/oauth2/auth?x=1"


class FakeFlow:
    def __init__(self, fail=None):
        self.fail = fail

    def run_local_server(self, **kw):
        self.kw = kw
        gflow.webbrowser.get(None).open(URL, new=1, autoraise=True)   # як робить бібліотека
        if self.fail:
            raise self.fail
        return mock.Mock(to_json=lambda: "{}")


class YouTubeAuthTest(unittest.TestCase):
    def yt(self, d):
        y = YouTube.__new__(YouTube)
        y.pcfg = {"client_secrets_file": str(Path(d) / "client_secret.json")}
        y.tokens_dir = Path(d) / "tokens"
        return y

    def test_link_shown_even_if_browser_fails(self):
        seen = []
        with tempfile.TemporaryDirectory() as d:
            flow = FakeFlow()
            with mock.patch.object(gflow.InstalledAppFlow, "from_client_secrets_file", return_value=flow), \
                    mock.patch("webbrowser.open", side_effect=RuntimeError("no browser")):
                self.yt(d).authorize(on_url=seen.append, timeout_s=300)
            self.assertEqual(seen, [URL])
            self.assertEqual(flow.kw["timeout_seconds"], 300)
            self.assertTrue((Path(d) / "tokens" / "youtube_token.json").exists())
        self.assertIsNot(gflow.webbrowser, None)
        import webbrowser
        self.assertIs(gflow.webbrowser, webbrowser)                    # бібліотеку повернуто як було

    def test_timeout_message(self):
        with tempfile.TemporaryDirectory() as d:
            err = gflow.WSGITimeoutError("Timed out waiting for response from authorization server")
            with mock.patch.object(gflow.InstalledAppFlow, "from_client_secrets_file", return_value=FakeFlow(err)), \
                    mock.patch("webbrowser.open", return_value=True):
                with self.assertRaises(RuntimeError) as cm:
                    self.yt(d).authorize(timeout_s=1)
        self.assertIn("Час вийшов", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
