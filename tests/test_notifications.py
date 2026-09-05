import base64
import hashlib
import hmac
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import requests

from core import Engine
from notifications import DingTalk, notification_config
import test_copycat as fixtures


CONFIG = {"enabled": True, "webhook": "https://oapi.dingtalk.com/robot/send?access_token=test-only", "secret": "SEC-test-only"}


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CopyCatTests()
        self.fixture.setUp()
        self.e = self.fixture.engine
        self.e.c["dingtalk"] = CONFIG
        self.e.notifier = DingTalk(CONFIG)

    def tearDown(self):
        self.fixture.tearDown()

    def test_all_five_types_and_error_dedup(self):
        self.fixture.add(1)
        self.fixture.add(2, "CLOSE", 150)
        self.fixture.add(3, quantity=1)
        self.e.report_error("接口异常")
        self.e.report_error("接口异常")
        queue = self.e.s["notifications"]
        self.assertEqual(len(queue), 4)
        self.assertIn("开仓成交", queue[0]["text"])
        self.assertIn("平仓成交", queue[1]["text"])
        self.assertIn("本次平仓毛盈亏", queue[1]["text"])
        self.assertIn("跳过订单", queue[2]["text"])
        self.assertIn("异常暂停", queue[3]["text"])
        self.assertTrue(all("模拟" in item["text"] for item in queue))
        event = self.fixture.event(4)
        self.e.s["pending"] = {"client_id": "cc_example", "event": event, "quantity": "0.3"}
        self.e.report_error("接口异常")
        self.e.save()
        self.assertEqual(len(queue), 5)
        self.assertIn("待确认订单", queue[-1]["text"])
        self.assertNotIn("本次成交数量", queue[-1]["text"])
        self.assertIn("cc_example", queue[-1]["text"])

    def test_persistent_queue_and_no_replay_of_fills(self):
        self.fixture.add(1)
        e2 = Engine(self.e.c, fixtures.Market())
        self.assertEqual(len(e2.s["notifications"]), 1)
        self.assertFalse(e2.s["running"])
        e2.read_source = self.e.read_source
        e2.tick()
        self.assertEqual(len(e2.s["notifications"]), 1)

    def test_delivery_failure_does_not_pause_trading(self):
        self.fixture.add(1)
        self.e.notifier.send = Mock(side_effect=ValueError("钉钉网络请求失败，通知等待重试"))
        self.e.deliver_notification()
        self.assertTrue(self.e.s["running"])
        self.assertIsNone(self.e.s["error"])
        self.assertEqual(self.e.s["notifications"][0]["attempts"], 1)
        self.e.deliver_notification()
        self.assertEqual(self.e.notifier.send.call_count, 1)
        self.e.s["notifications"][0]["next_try"] = 0
        self.e.notifier.send = Mock()
        self.e.deliver_notification()
        self.assertEqual(self.e.s["notifications"], [])
        self.assertIsNone(self.e.s["notification_error"])
        self.assertIsNotNone(self.e.s["notification_last_sent"])

    def test_sender_does_not_hold_trading_lock(self):
        self.fixture.add(1)
        def send(item):
            with ThreadPoolExecutor(max_workers=1) as pool:
                self.assertTrue(pool.submit(self.e.view).result(timeout=2)["running"])
        self.e.notifier.send = send
        self.e.deliver_notification()
        self.assertEqual(self.e.s["notifications"], [])

    def test_reuses_only_previous_dingtalk_keys(self):
        with patch.dict(os.environ, {}, clear=True), patch("notifications.dotenv_values", return_value={
            "DINGTALK_WEBHOOK": CONFIG["webhook"], "DINGTALK_SECRET": CONFIG["secret"], "LIVE_TRADING_ENABLED": "true"
        }), patch.object(Path, "is_file", return_value=True):
            config = notification_config(Path("/tmp/CopyCat"))
            self.assertEqual(config, CONFIG)
            self.assertNotIn("LIVE_TRADING_ENABLED", os.environ)

    def test_signed_payload_and_redaction(self):
        notifier = DingTalk(CONFIG)
        with patch("notifications.time.time", return_value=1234):
            query = parse_qs(urlsplit(notifier.signed_url()).query)
        expected = base64.b64encode(hmac.new(CONFIG["secret"].encode(),
            ("1234000\n" + CONFIG["secret"]).encode(), hashlib.sha256).digest()).decode()
        self.assertEqual(query["sign"], [expected])
        with patch("notifications.requests.post", return_value=Mock(status_code=200, json=lambda: {"errcode": 0})) as post:
            notifier.send({"text": "CopyCat · 模拟 · 开仓成交"})
            self.assertEqual(post.call_args.kwargs["json"]["text"]["content"], "CopyCat · 模拟 · 开仓成交")
            self.assertFalse(post.call_args.kwargs["allow_redirects"])
        with patch("notifications.requests.post", side_effect=requests.ConnectionError(CONFIG["webhook"])):
            with self.assertRaises(ValueError) as caught:
                notifier.send({"text": "test"})
            self.assertNotIn("test-only", str(caught.exception))

    def test_disabled_does_not_enqueue_or_send(self):
        self.e.notifier = DingTalk({**CONFIG, "enabled": False})
        self.fixture.add(1)
        self.assertFalse(self.e.s.get("notifications"))
        with patch("notifications.requests.post") as post:
            self.e.deliver_notification()
            post.assert_not_called()
