import unittest
from unittest.mock import patch

from gitcp.oauth import DeviceCode, OAuthError, oauth_base, poll_for_access_token, request_device_code


class OAuthTests(unittest.TestCase):
    def test_oauth_base_dotcom(self):
        self.assertEqual(oauth_base("github.com"), "https://github.com")

    def test_oauth_base_enterprise(self):
        self.assertEqual(oauth_base("ghe.example.com"), "https://ghe.example.com")

    def test_request_device_code(self):
        with patch("gitcp.oauth.post_oauth_json") as post:
            post.return_value = {
                "device_code": "device",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            }
            code = request_device_code("github.com")

        self.assertEqual(code.device_code, "device")
        self.assertEqual(code.user_code, "ABCD-EFGH")
        self.assertEqual(code.interval, 5)

    def test_poll_handles_authorization_pending_then_success(self):
        device = DeviceCode("device", "ABCD", "https://github.com/login/device", 900, 1)
        with patch("gitcp.oauth.time.sleep"), patch("gitcp.oauth.post_oauth_json") as post:
            post.side_effect = [{"error": "authorization_pending"}, {"access_token": "token"}]
            token = poll_for_access_token("github.com", device, timeout=30)
        self.assertEqual(token, "token")

    def test_poll_expired_token(self):
        device = DeviceCode("device", "ABCD", "https://github.com/login/device", 900, 1)
        with patch("gitcp.oauth.time.sleep"), patch("gitcp.oauth.post_oauth_json") as post:
            post.return_value = {"error": "expired_token"}
            with self.assertRaises(OAuthError):
                poll_for_access_token("github.com", device, timeout=30)


if __name__ == "__main__":
    unittest.main()
