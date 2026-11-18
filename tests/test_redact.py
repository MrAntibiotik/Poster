"""Токени й ключі не потрапляють у журнал."""
import logging
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import config  # noqa: E402


class RedactTest(unittest.TestCase):
    def test_patterns(self):
        r = config.redact
        self.assertNotIn("ya29.SECRET", r("{'access_token': 'ya29.SECRET123'}"))
        self.assertNotIn("1//0cNIinraQji7nCgYIARA", r("'refresh_token': '1//0cNIinraQji7nCgYIARA'"))
        self.assertNotIn("MzU1NDEx", r("'Authorization': 'Basic MzU1NDExMDg3NTcxLTF1aHBk'"))
        self.assertNotIn("4%2F0AUn", r("&code=4%2F0AUn6Hd8Od&redirect_uri=x"))
        self.assertNotIn("EAAQabcdefghijklmnopqrstu", r("EAAQabcdefghijklmnopqrstuvwxyz"))
        self.assertIn("status_code: FINISHED", r("status_code: FINISHED"))   # діагностика лишається

    def test_log_file_and_old_logs(self):
        d = Path(tempfile.mkdtemp())
        log = d / "log.txt"
        (d / "log.txt.1").write_text("old 'refresh_token': '1//0abcdefghijklmnop'\n", encoding="utf-8")
        config.setup_logging({"paths": {"log": str(log)}})
        logging.getLogger("requests_oauthlib.oauth2_session").debug("Obtained token ya29.LEAK12345")
        logging.getLogger("x").warning("token ya29.LEAK67890")
        for h in logging.getLogger().handlers:
            h.flush()
        text = log.read_text(encoding="utf-8")
        self.assertNotIn("LEAK", text)
        self.assertIn("ya29.***", text)
        self.assertNotIn("1//0abcdefghijklmnop", (d / "log.txt.1").read_text(encoding="utf-8"))
        for h in list(logging.getLogger().handlers):
            h.close()
            logging.getLogger().removeHandler(h)


if __name__ == "__main__":
    unittest.main()
