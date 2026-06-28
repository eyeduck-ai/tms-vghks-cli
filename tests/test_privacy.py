import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tms_vghks.privacy import redact_sensitive_url


class PrivacyTests(unittest.TestCase):
    def test_redacts_user_identifier_query_keys_in_nested_urls(self):
        url = (
            "https://tms.vghks.gov.tw/ajax/path?"
            "userId=11078&ajaxAuth=secret&"
            "redir=%2Fkexam%2F1%2Ftake%3FuserID%3D11078%26key%3Dsecret%26recordID%3D938871"
        )

        redacted = redact_sensitive_url(url)

        self.assertIn("userId=REDACTED", redacted)
        self.assertIn("ajaxAuth=REDACTED", redacted)
        self.assertIn("userID%3DREDACTED", redacted)
        self.assertIn("key%3DREDACTED", redacted)
        self.assertIn("recordID%3D938871", redacted)
        self.assertNotIn("11078", redacted)
        self.assertNotIn("secret", redacted)


if __name__ == "__main__":
    unittest.main()
