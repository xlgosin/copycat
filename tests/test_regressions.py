import json
import os
import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from app import create_app, run_worker
from collect import initialize, poll
from core import Engine, stamp
from exchange import dec
import test_copycat as fixtures
from test_copycat import Market, RULE


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.CopyCatTests()
        self.f.setUp()
        self.e = self.f.engine

    def tearDown(self):
        self.f.tearDown()

    def test_pause_and_status_do_not_wait_for_market_request(self):
        entered, release = threading.Event(), threading.Event()
        def market(symbol):
            entered.set()
            self.assertTrue(release.wait(5))
            return RULE, dec(100)
        self.e.exchange.market = market
        self.f.events.extend([self.f.event(1), self.f.event(2)])
        client = create_app(self.e, "token").test_client()
        headers = {"Authorization": "Bearer token"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(self.e.tick)
            try:
                self.assertTrue(entered.wait(2))
                response = pool.submit(client.post, "/api/stop", headers=headers, json={}).result(timeout=1)
                self.assertEqual(response.status_code, 200)
                view = pool.submit(self.e.view).result(timeout=1)
                self.assertFalse(view["running"])
                self.assertTrue(view["executor_busy"])
            finally:
                release.set()
            worker.result(timeout=2)
        self.assertFalse(self.e.s["pending"])
        self.assertFalse(self.e.s["positions"])
        self.assertIn("ETHUSDT:LONG", self.e.s["blocked_cycles"])

    def test_pause_during_submission_settles_only_inflight_order(self):
        self.e.c["mode"] = "testnet"
        self.e.reconcile = lambda: None
        self.e.exchange.validate_account = lambda: {"availableBalance": "100"}
        self.e.exchange.prepare = lambda *args: None
        entered, release = threading.Event(), threading.Event()
        def order(p):
            entered.set()
            self.assertTrue(release.wait(5))
            return {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": "100"}
        self.e.exchange.order = Mock(side_effect=order)
        self.f.events.extend([self.f.event(1), self.f.event(2)])
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker = pool.submit(self.e.tick)
            try:
                self.assertTrue(entered.wait(2))
                pool.submit(self.e.stop).result(timeout=1)
                self.assertTrue(self.e.view()["order_in_flight"])
            finally:
                release.set()
            worker.result(timeout=2)
        self.assertEqual(self.e.exchange.order.call_count, 1)
        self.assertEqual(self.e.s["positions"]["ETHUSDT:LONG"]["quantity"], "0.3")
        self.assertFalse(self.e.s["running"])

    def test_resume_drains_backlog_and_blocks_partial_source_cycle(self):
        self.e.stop()
        self.f.events.append(self.f.event(1))
        self.e.start()
        self.assertFalse(self.e.s["positions"])
        self.f.add(2)
        self.assertFalse(self.e.s["positions"])
        self.f.add(3, "CLOSE", 600)
        self.f.add(4)
        self.assertEqual(self.e.s["positions"]["ETHUSDT:LONG"]["quantity"], "0.3")

    def test_resume_finds_unpolled_close(self):
        self.f.add(1)
        self.e.stop()
        self.f.events.append(self.f.event(2, "CLOSE"))
        with self.assertRaisesRegex(ValueError, "平仓"):
            self.e.start()
        self.assertFalse(self.e.s["running"])
        self.assertEqual(self.e.s["positions"]["ETHUSDT:LONG"]["quantity"], "0.3")

    def test_pause_wins_over_inflight_start(self):
        self.e.stop()
        original = self.e.read_source
        entered, release = threading.Event(), threading.Event()
        def read():
            entered.set()
            self.assertTrue(release.wait(5))
            return original()
        self.e.read_source = read
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(self.e.start)
            try:
                self.assertTrue(entered.wait(2))
                pool.submit(self.e.stop).result(timeout=1)
            finally:
                release.set()
            with self.assertRaisesRegex(ValueError, "暂停"):
                start.result(timeout=2)
        self.assertFalse(self.e.view()["running"])

    def test_unknown_history_cannot_initialize(self):
        self.e.s["initialized"] = False
        self.f.status.pop("history_complete")
        self.e.tick()
        self.assertFalse(self.e.s["initialized"])
        with self.assertRaisesRegex(ValueError, "完整性"):
            self.e.start()

    def test_delayed_signal_from_before_resume_is_not_copied(self):
        self.e.stop()
        delayed = self.f.event(1)
        delayed["occurred_at"] = stamp()
        self.e.start()
        self.f.events.append(delayed)
        self.e.tick()
        self.assertFalse(self.e.s["positions"])
        self.assertIn("ETHUSDT:LONG", self.e.s["blocked_cycles"])

    def test_worker_failure_is_visible_and_prevents_restart(self):
        with self.assertLogs(level="ERROR"):
            run_worker(self.e, Mock(side_effect=RuntimeError("worker failed")), 5, trading=True)
        self.assertFalse(self.e.view()["running"])
        self.assertIn("工作线程", self.e.view()["error"])
        with self.assertRaisesRegex(ValueError, "工作线程"):
            self.e.start()

    def test_source_window_gap_requires_review(self):
        self.e.s["coverage_end"] = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        self.f.status["history_window_start"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        self.e.tick()
        self.assertFalse(self.e.s["running"])
        self.assertIn("缺口", self.e.s["review_required"])

    def test_uncovered_baseline_is_rejected(self):
        self.e.s["initialized"] = False
        self.f.status["history_window_start"] = stamp()
        self.f.profile["position_baseline"] = {"portfolio_id": self.f.c["portfolio"],
            "as_of": (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(), "positions": {}}
        self.e.tick()
        self.assertFalse(self.e.s["initialized"])
        self.assertIn("未覆盖", self.e.s["error"])

    def test_verified_cross_week_position_baseline(self):
        self.e.s["initialized"] = False
        before = (self.f.when - timedelta(days=7)).isoformat()
        self.f.status.update(history_complete=False, history_window_start=before)
        self.f.profile["position_baseline"] = {"portfolio_id": self.f.c["portfolio"], "as_of": before, "positions": {"ETHUSDT:LONG": "600"}}
        self.f.events.append(self.f.event(1, "CLOSE", 300))
        self.e.tick()
        self.assertEqual(self.e.s["source_positions"]["ETHUSDT:LONG"], "300")
        self.f.add(2, "CLOSE", 300)
        self.f.add(3)
        self.assertEqual(self.e.s["positions"]["ETHUSDT:LONG"]["quantity"], "0.3")

    def test_excess_close_does_not_invent_flat_source_position(self):
        self.f.add(1)
        self.f.add(2, "CLOSE", 600)
        self.assertEqual(self.e.s["source_positions"]["ETHUSDT:LONG"], "300")
        self.assertIn("review_required", self.e.s)
        self.assertFalse(self.e.s["running"])

    def test_partial_close_requires_review_even_after_resolve(self):
        self.f.add(1)
        self.e.s["pending"] = {"event": self.f.event(2, "CLOSE"), "key": "ETHUSDT:LONG",
            "operation": "CLOSE", "quantity": "0.3", "client_id": "partial-close", "price": "100"}
        with patch.object(self.e, "paper_result", return_value={"status": "EXPIRED", "executedQty": "0.1", "avgPrice": "100"}):
            self.e.resolve()
        self.assertIn("剩余仓位", self.e.s["error"])
        with self.assertRaises(ValueError):
            self.e.start()

    def test_audit_history_and_pagination_survive_restart(self):
        for i in range(1100):
            self.e.record({"event_id": str(i)}, "baseline", str(i))
        self.e.save()
        other = Engine(self.f.c, Market())
        before, ids = None, []
        while True:
            page = other.history(before, 100)
            ids.extend(r["record_id"] for r in page["records"])
            before = page["next_before"]
            if before is None:
                break
        self.assertEqual(len(ids), 1101)
        self.assertEqual(len(set(ids)), 1101)
        self.assertEqual(len(other.s["records"]), 100)
        with other.connection() as con:
            body = json.loads(con.execute("SELECT body FROM state").fetchone()[0])
        self.assertNotIn("records", body)
        self.assertNotIn("seen", body)
        self.assertNotIn("notifications", body)

    def test_legacy_migration_is_idempotent(self):
        with self.e.connection() as con:
            body = json.loads(con.execute("SELECT body FROM state").fetchone()[0])
            body.pop("baseline_verified")
            body["seen"] = ["old-event"]
            body["records"] = [{"time": stamp(), "note": "legacy", "status": "filled"}]
            con.execute("UPDATE state SET body=?", (json.dumps(body),))
        first = Engine(self.f.c, Market())
        second = Engine(self.f.c, Market())
        self.assertTrue(second.has_seen("old-event"))
        self.assertEqual(first.history(), second.history())
        self.assertIn("旧账本", second.s["review_required"])

    def test_disk_failure_after_fill_cannot_apply_fill_twice(self):
        original = self.e.connection
        @contextmanager
        def failing_connection():
            if not self.e.s["pending"] and self.e.s["positions"]:
                raise sqlite3.OperationalError("disk full")
            with original() as con:
                yield con
        with patch.object(self.e, "connection", failing_connection):
            self.f.add(1)
        self.assertIsNotNone(self.e.storage_error)
        with self.assertRaises(ValueError):
            self.e.resolve()
        other = Engine(self.f.c, Market())
        self.assertIsNotNone(other.s["pending"])
        self.assertFalse(other.s["positions"])
        other.resolve()
        self.assertEqual(other.s["positions"]["ETHUSDT:LONG"]["quantity"], "0.3")
        with self.assertRaises(ValueError):
            other.resolve()

    def test_intent_write_failure_never_submits_order(self):
        original = self.e.connection
        @contextmanager
        def failing_connection():
            if self.e.s["pending"]:
                raise sqlite3.OperationalError("disk full")
            with original() as con:
                yield con
        with patch.object(self.e, "connection", failing_connection), patch.object(self.e, "paper_result") as submit:
            self.f.add(1)
        submit.assert_not_called()
        self.assertIsNotNone(self.e.storage_error)
        client = create_app(self.e, "token").test_client()
        response = client.post("/api/start", json={}, headers={"Authorization": "Bearer token"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("账本写入失败", response.json["error"])

    def test_real_source_read_excludes_consumed_but_detects_late_event(self):
        path = Path(self.f.tmp.name) / "source.db"
        initialize(path)
        event = self.f.event(1)
        with sqlite3.connect(path) as con:
            con.execute("INSERT INTO trader_state VALUES(?,?,?)", (self.f.c["portfolio"], json.dumps(self.f.profile), stamp()))
            con.execute("INSERT INTO runtime_state VALUES(?,?,?)", ("collector_status:" + self.f.c["portfolio"], json.dumps(self.f.status), stamp()))
            for event_id in ("1", "late"):
                con.execute("INSERT INTO trade_events VALUES(?,?,?,?,?,?,?,?)", (event_id, self.f.c["portfolio"], event["occurred_at"], "ETHUSDT", "LONG", "OPEN", 300, 100))
        self.e.consume(event)
        self.e.save()
        self.e.c["source_db"] = str(path)
        profile, events, status = Engine.read_source(self.e)
        self.assertEqual([e["event_id"] for e in events], ["late"])

    def test_collector_window_covers_downtime_across_week(self):
        path = Path(self.f.tmp.name) / "source.db"
        initialize(path)
        old = (datetime.now(timezone.utc) - timedelta(days=9)).isoformat()
        with sqlite3.connect(path) as con:
            con.execute("INSERT INTO runtime_state VALUES(?,?,?)", ("collector_status:" + self.f.c["portfolio"], json.dumps({"last_success_at": old, "history_window_start": old}), old))
        page = Mock()
        page.locator.return_value.inner_text.return_value = "帶單餘額 300,000 USDT"
        page.locator.return_value.first.inner_text.return_value = "测试"
        with patch.dict(os.environ, {"SOURCE_BASELINE_FILE": ""}), patch("collect.request", return_value={"list": []}) as request:
            poll(page, path, self.f.c["portfolio"])
        payload = request.call_args.args[2]
        self.assertLess(payload["startTime"], int(datetime.fromisoformat(old).timestamp() * 1000))
        with sqlite3.connect(path) as con:
            status = json.loads(con.execute("SELECT value_json FROM runtime_state").fetchone()[0])
        self.assertFalse(status["history_complete"])
        self.assertEqual(status["history_window_start"], old)

    def test_records_api_auth_and_bad_start_body(self):
        client = create_app(self.e, "token").test_client()
        headers = {"Authorization": "Bearer token"}
        self.assertEqual(client.get("/api/records").status_code, 401)
        self.assertEqual(client.get("/api/records?before=bad", headers=headers).status_code, 400)
        self.assertEqual(client.get("/api/records", headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/start", json=[1], headers=headers).status_code, 400)


if __name__ == "__main__":
    unittest.main()
