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

    def test_slow_collector_poll_does_not_pause_when_equity_is_fresh(self):
        self.e.s.update(running=True, auto_resume=True, error=None)
        self.f.status["last_success_at"] = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        self.f.profile["captured_at"] = stamp()
        self.e.tick()
        self.assertTrue(self.e.s["running"])
        self.assertIsNone(self.e.s["error"])

    def test_stale_source_pauses_when_equity_and_success_are_old(self):
        self.e.s.update(running=True, auto_resume=True, error=None)
        old = "2020-01-01T00:00:00+00:00"
        self.f.status["last_success_at"] = old
        self.f.profile["captured_at"] = old
        self.e.tick()
        self.assertFalse(self.e.s["running"])
        self.assertIn("源采集暂时中断", self.e.s["error"])

    def test_auto_resume_after_source_recovers(self):
        self.e.c["auto_resume"] = True
        self.assertTrue(self.e.s.get("auto_resume"))
        self.e.s.update(running=False, error="源采集暂时中断或数据过期，已暂停跟单；恢复后将自动继续")
        self.f.status["last_success_at"] = stamp()
        self.f.profile["captured_at"] = stamp()
        self.e.tick()
        self.assertTrue(self.e.s["running"])
        self.assertIsNone(self.e.s.get("error"))
        self.assertTrue(any(r.get("status") == "resume" for r in self.e.s["records"]))

    def test_manual_stop_does_not_auto_resume(self):
        self.e.c["auto_resume"] = True
        self.e.stop()
        self.assertFalse(self.e.s.get("auto_resume"))
        self.f.status["last_success_at"] = stamp()
        self.f.profile["captured_at"] = stamp()
        self.e.tick()
        self.assertFalse(self.e.s["running"])

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

    def test_old_fill_reads_source_time_price_from_collector(self):
        path = Path(self.f.tmp.name) / "source.db"
        initialize(path)
        when = "2026-09-07T02:09:30+08:00"
        with sqlite3.connect(path) as con:
            con.execute("INSERT INTO trade_events VALUES(?,?,?,?,?,?,?,?)",
                        ("lead-open-1", self.f.c["portfolio"], when, "ETHUSDT", "SHORT", "OPEN", 300, 2479.1))
        self.e.c["source_db"] = str(path)
        self.e.record({"event_id": "lead-open-1", "symbol": "ETHUSDT", "side": "SHORT", "operation": "OPEN"},
                      "filled", "交易所确认结果", quantity="0.067", price="2478.5")
        rec = next(r for r in self.e.view()["records"] if r.get("event_id") == "lead-open-1")
        self.assertEqual(rec["source_time"], when)
        self.assertEqual(dec(rec["source_price"]), dec("2479.1"))
        self.assertEqual(dec(rec["source_quantity"]), dec("300"))
        self.assertEqual(rec["price"], "2478.5")
        self.e.save()
        page = self.e.history(kind="trade")
        hydrated = next(r for r in page["records"] if r.get("event_id") == "lead-open-1")
        self.assertEqual(hydrated["source_time"], when)

    def test_history_can_hide_noisy_resume_records(self):
        for i in range(3):
            self.e.record({}, "resume", f"resume-{i}")
        self.e.record({"symbol": "ETHUSDT", "side": "LONG", "operation": "OPEN"}, "filled", "open")
        self.e.save()
        trade = self.e.history(kind="trade")
        self.assertEqual({r["status"] for r in trade["records"]}, {"filled"})
        self.assertEqual({r["category"] for r in trade["records"]}, {"trade"})
        self.assertIsNone(trade["next_before"])
        system = self.e.history(kind="system")
        self.assertEqual({r["status"] for r in system["records"]}, {"resume", "baseline"})
        self.assertEqual({r["category"] for r in system["records"]}, {"system"})
        mixed = self.e.history(kind="all")
        self.assertEqual(len(mixed["records"]), 5)
        page = self.e.history(kind="system", limit=2)
        self.assertEqual(len(page["records"]), 2)
        self.assertIsNotNone(page["next_before"])
        rest = self.e.history(before=page["next_before"], kind="system", limit=2)
        self.assertEqual(len(rest["records"]), 2)
        self.assertIsNone(rest["next_before"])

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
            profile = json.loads(con.execute("SELECT data_json FROM trader_state").fetchone()[0])
        self.assertFalse(status["history_complete"])
        self.assertEqual(status["history_window_start"], old)
        self.assertEqual(status["last_success_at"], profile["captured_at"])
        success = datetime.fromisoformat(status["last_success_at"].replace("Z", "+00:00"))
        self.assertEqual(success.utcoffset(), timedelta(0))
        self.assertLess(abs(datetime.now(timezone.utc).timestamp() - success.timestamp()), 5)

    def test_records_api_auth_and_bad_start_body(self):
        client = create_app(self.e, "token").test_client()
        headers = {"Authorization": "Bearer token"}
        self.assertEqual(client.get("/api/records").status_code, 401)
        self.assertEqual(client.get("/api/records?before=bad", headers=headers).status_code, 400)
        self.assertEqual(client.get("/api/records?kind=nope", headers=headers).status_code, 400)
        self.assertEqual(client.get("/api/records", headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/start", json=[1], headers=headers).status_code, 400)

    def test_live_positions_enrich_mark_and_pnl(self):
        self.f.add(1)
        self.e.exchange.market = lambda symbol: (RULE, dec("110"))
        view = self.e.view()
        live = view["live_positions"]["ETHUSDT:LONG"]
        self.assertEqual(live["status"], "持有中")
        self.assertEqual(dec(live["mark_price"]), dec("110"))
        self.assertEqual(dec(live["unrealized_pnl"]), dec("3"))  # (110-100)*0.3
        self.assertEqual(dec(live["notional"]), dec("33"))
        self.assertEqual(live["leverage"], 3)
        self.assertIsNotNone(live["margin"])
        # Throttle: second call within 15s keeps cache
        self.e.exchange.market = lambda symbol: (_ for _ in ()).throw(AssertionError("should throttle"))
        again = self.e.view()
        self.assertEqual(again["live_positions"]["ETHUSDT:LONG"]["mark_price"], "110")

    def test_live_positions_use_exchange_row_when_keys_present(self):
        self.e.c["key"], self.e.c["secret"] = "k", "s"
        self.f.add(1)
        self.e.exchange.positions = lambda: [{
            "symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0.3",
            "entryPrice": "100", "markPrice": "95", "unRealizedProfit": "-1.5",
            "liquidationPrice": "80", "isolatedMargin": "10", "leverage": "5",
            "notional": "28.5",
        }]
        live = self.e.view()["live_positions"]["ETHUSDT:LONG"]
        self.assertEqual(dec(live["mark_price"]), dec("95"))
        self.assertEqual(dec(live["unrealized_pnl"]), dec("-1.5"))
        self.assertEqual(dec(live["liquidation_price"]), dec("80"))
        self.assertEqual(dec(live["margin"]), dec("10"))
        self.assertEqual(live["leverage"], 5)
        self.assertEqual(dec(live["roe_percent"]), dec("-15"))


if __name__ == "__main__":
    unittest.main()
