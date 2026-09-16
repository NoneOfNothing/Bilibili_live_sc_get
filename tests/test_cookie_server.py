"""cookie_server 的单元与集成测试：Origin 校验、体校验、端口处理、等待超时。"""

from __future__ import annotations

import http.client
import json
import threading
import unittest

from blive_sc_get.cookie_server import (
    APP_SIGN,
    COOKIE_ENDPOINT,
    CookieReceiver,
    decode_cookie_payload,
    origin_allowed,
    wait_for_extension_cookie,
)

GOOD_ORIGIN = "chrome-extension://abcdefghijklmnop"
VALID_COOKIE = "SESSDATA=abc123; bili_jct=xyz789; buvid3=xxxx"


class OriginAllowedTests(unittest.TestCase):
    def test_chromium_prefix_allowed(self):
        self.assertTrue(origin_allowed("chrome-extension://a"))
        self.assertTrue(origin_allowed("chrome-extension://id — status"))

    def test_mozilla_prefix_allowed(self):
        self.assertTrue(origin_allowed("moz-extension://a"))

    def test_plain_site_rejected(self):
        self.assertFalse(origin_allowed("https://evil.com"))
        self.assertFalse(origin_allowed("https://bilibili.com"))

    def test_missing_or_empty_rejected(self):
        self.assertFalse(origin_allowed(None))
        self.assertFalse(origin_allowed(""))
        self.assertFalse(origin_allowed("   "))


class DecodePayloadTests(unittest.TestCase):
    def test_valid(self):
        data = {"app": APP_SIGN, "cookies": f"  {VALID_COOKIE}  "}
        self.assertEqual(decode_cookie_payload(data), VALID_COOKIE)

    def test_missing_app(self):
        with self.assertRaises(ValueError):
            decode_cookie_payload({"cookies": VALID_COOKIE})

    def test_wrong_app(self):
        with self.assertRaises(ValueError):
            decode_cookie_payload({"app": "other", "cookies": VALID_COOKIE})

    def test_cookies_missing(self):
        with self.assertRaises(ValueError):
            decode_cookie_payload({"app": APP_SIGN})

    def test_cookies_not_string(self):
        with self.assertRaises(ValueError):
            decode_cookie_payload({"app": APP_SIGN, "cookies": ["a=1"]})

    def test_cookies_no_equals(self):
        with self.assertRaises(ValueError):
            decode_cookie_payload({"app": APP_SIGN, "cookies": "session"})

    def test_not_a_dict(self):
        with self.assertRaises(ValueError):
            decode_cookie_payload("oops")


def _post(port: int, path: str, origin: str, body: bytes) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json", "Origin": origin})
        return conn.getresponse()
    finally:
        conn.close()


def _options(port: int) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("OPTIONS", COOKIE_ENDPOINT)
        return conn.getresponse()
    finally:
        conn.close()


class CookieReceiverIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.receiver = CookieReceiver(host="127.0.0.1", port=0)
        self.thread = threading.Thread(target=self.receiver.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.receiver.shutdown()
        self.receiver.server_close()
        self.thread.join(timeout=1.0)

    def test_good_origin_accepts_cookie(self):
        body = json.dumps({"app": APP_SIGN, "cookies": VALID_COOKIE}).encode("utf-8")
        resp = _post(self.receiver.port, COOKIE_ENDPOINT, GOOD_ORIGIN, body)
        self.assertEqual(resp.status, 200)
        self.assertTrue(self.receiver._done.is_set())
        self.assertEqual(self.receiver.received_cookie, VALID_COOKIE)

    def test_bad_origin_rejected_and_not_accepted(self):
        body = json.dumps({"app": APP_SIGN, "cookies": VALID_COOKIE}).encode("utf-8")
        resp = _post(self.receiver.port, COOKIE_ENDPOINT, "https://evil.com", body)
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.receiver._done.is_set())
        self.assertEqual(self.receiver.received_cookie, "")

    def test_missing_origin_rejected(self):
        body = json.dumps({"app": APP_SIGN, "cookies": VALID_COOKIE}).encode("utf-8")
        resp = _post(self.receiver.port, COOKIE_ENDPOINT, "", body)
        self.assertEqual(resp.status, 403)
        self.assertFalse(self.receiver._done.is_set())

    def test_wrong_path_404(self):
        body = json.dumps({"app": APP_SIGN, "cookies": VALID_COOKIE}).encode("utf-8")
        resp = _post(self.receiver.port, "/other", GOOD_ORIGIN, body)
        self.assertEqual(resp.status, 404)
        self.assertFalse(self.receiver._done.is_set())

    def test_bad_payload_400(self):
        body = json.dumps({"app": APP_SIGN}).encode("utf-8")
        resp = _post(self.receiver.port, COOKIE_ENDPOINT, GOOD_ORIGIN, body)
        self.assertEqual(resp.status, 400)
        self.assertFalse(self.receiver._done.is_set())

    def test_options_returns_cors(self):
        resp = _options(self.receiver.port)
        self.assertEqual(resp.status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), "*")

    def test_wrong_app_400(self):
        body = json.dumps({"app": "other", "cookies": VALID_COOKIE}).encode("utf-8")
        resp = _post(self.receiver.port, COOKIE_ENDPOINT, GOOD_ORIGIN, body)
        self.assertEqual(resp.status, 400)
        self.assertFalse(self.receiver._done.is_set())


class WaitForExtensionCookieTests(unittest.TestCase):
    def test_timeout_returns_none(self):
        # 用随机端口 + 短超时，验证超时返回 None 且不抛异常（服务器会自行关闭）
        result = wait_for_extension_cookie(timeout=0.2, port=0)
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()