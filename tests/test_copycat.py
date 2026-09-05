import tempfile
import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import create_app
from core import Engine, stamp
from exchange import Binance, dec
from collect import parse_orders


RULE = {"filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "1000000"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100000"},
                    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "100000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}


class Market:
    quantity = staticmethod(Binance.quantity)
    def market(self, symbol):
        return RULE, dec(100)


class CopyCatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.c = {"mode": "paper", "db": str(Path(self.tmp.name) / "paper.db"), "source_db": "unused",
            "portfolio": "5075281354358777856", "key": "", "secret": "", "testnet": "https://demo-fapi.binance.com",
            "capital": dec(100), "multiplier": dec(3), "leverage": 3, "max_gross": dec(300),
            "signal_age": 180, "source_age": 180, "deviation": dec(1), "live_enabled": False}
        self.engine = Engine(self.c, Market())
        self.events = []
        self.profile = {"margin_balance": 300000, "aum": 6300000, "captured_at": stamp()}
        self.status = {"last_success_at": stamp()}
        self.engine.read_source = lambda: (self.profile, self.events, self.status)
        self.engine.tick()
        self.engine.start()
        self.when = datetime.now(timezone.utc) - timedelta(seconds=30)

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, identifier, operation="OPEN", quantity=300, side="LONG"):
        self.when += timedelta(seconds=1)
        return {"event_id": str(identifier), "occurred_at": self.when.isoformat(), "symbol": "ETHUSDT",
                "operation": operation, "side": side, "quantity": quantity, "price": 100}

    def add(self, *args, **kwargs):
        self.events.append(self.event(*args, **kwargs))
        self.engine.tick()

    def test_open_partial_and_full_close(self):
        self.add(1)
        p = self.engine.s["positions"]["ETHUSDT:LONG"]
        self.assertEqual(dec(p["quantity"]), dec("0.3"))
        self.add(2, "CLOSE", 150)
        self.assertEqual(dec(p["quantity"]), dec("0.15"))
        self.add(3, "CLOSE", 150)
        self.assertEqual(dec(p["quantity"]), 0)
        self.assertTrue(self.engine.s["running"])

    def test_short_copy_and_no_reverse_open(self):
        self.add(1, side="SHORT")
        self.add(2, side="LONG")
        self.assertNotIn("ETHUSDT:LONG", self.engine.s["positions"])
        self.assertIn("反向", self.engine.s["records"][0]["note"])
        self.add(3, "CLOSE", 300, "SHORT")
        self.assertEqual(dec(self.engine.s["positions"]["ETHUSDT:SHORT"]["quantity"]), 0)

    def test_duplicates_and_restart_dont_trade_again(self):
        self.add(1)
        self.engine.tick()
        self.assertEqual(len(self.engine.s["records"]), 2)
        other = Engine(self.c, Market())
        self.assertFalse(other.s["running"])
        other.read_source = self.engine.read_source
        other.tick()
        self.assertEqual(dec(other.s["positions"]["ETHUSDT:LONG"]["quantity"]), dec("0.3"))

    def test_baseline_is_not_copied(self):
        self.engine.s["initialized"] = False
        self.events.append(self.event(1))
        self.engine.tick()
        self.assertFalse(self.engine.s["positions"])
        self.add(2, "CLOSE", 300)
        self.assertFalse(self.engine.s["positions"])
        self.add(3)
        self.assertEqual(dec(self.engine.s["positions"]["ETHUSDT:LONG"]["quantity"]), dec("0.3"))

    def test_small_orders_skip_and_cap_blocks(self):
        self.add(1, quantity=1)
        self.assertFalse(self.engine.s["positions"])
        self.add(2, quantity=4000)
        self.assertFalse(self.engine.s["positions"])
        self.assertIn("上限", self.engine.s["records"][0]["note"])

    def test_missing_equity_and_stale_source_stop(self):
        self.profile["margin_balance"] = None
        self.add(1)
        self.assertFalse(self.engine.s["running"])
        self.assertFalse(self.engine.s["positions"])
        self.profile["margin_balance"] = 300000
        self.status["last_success_at"] = "2020-01-01T00:00:00+00:00"
        with self.assertRaises(ValueError):
            self.engine.start()

    def test_paused_missed_close_requires_review(self):
        self.add(1)
        self.engine.stop()
        self.add(2, "CLOSE", 300)
        self.assertIn("review_required", self.engine.s)
        with self.assertRaises(ValueError):
            self.engine.start()

    def test_unknown_submission_is_not_resent(self):
        self.engine.c["mode"] = "testnet"
        self.engine.reconcile = lambda: None
        exchange = self.engine.exchange
        exchange.validate_account = lambda: {"availableBalance": "100"}
        exchange.prepare = lambda *args: None
        calls = []
        def order(p):
            calls.append(p)
            raise RuntimeError("网络超时")
        exchange.order = order
        exchange.query = lambda p: {"status": "FILLED", "executedQty": p["quantity"], "avgPrice": "100"}
        self.add(1)
        self.assertTrue(self.engine.s["pending"])
        self.engine.tick()
        self.assertEqual(len(calls), 1)
        self.engine.resolve()
        self.assertFalse(self.engine.s["pending"])
        self.assertFalse(self.engine.s["running"])
        self.assertEqual(dec(self.engine.s["positions"]["ETHUSDT:LONG"]["quantity"]), dec("0.3"))

    def test_partial_fill_applies_executed_only(self):
        e = self.event(1)
        self.engine.s["pending"] = {"event": e, "key": "ETHUSDT:LONG", "operation": "OPEN",
                                    "quantity": "0.3", "client_id": "test"}
        self.engine.settle({"status": "EXPIRED", "executedQty": "0.1", "avgPrice": "100"})
        self.assertEqual(dec(self.engine.s["positions"]["ETHUSDT:LONG"]["quantity"]), dec("0.1"))
        self.assertFalse(self.engine.s["running"])

    def test_unfinished_intent_survives_restart(self):
        self.engine.s["processing"] = "event1"
        self.engine.save()
        other = Engine(self.c, Market())
        self.assertIn("review_required", other.s)

    def test_account_change_rejected(self):
        with self.assertRaises(ValueError):
            Engine({**self.c, "key": "different-account"}, Market())

    def test_api_auth_secrets_and_live_confirmation(self):
        client = create_app(self.engine, "control-token").test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/api/status").status_code, 401)
        headers = {"Authorization": "Bearer control-token"}
        response = client.get("/api/status", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("secret", response.get_data(as_text=True))
        self.engine.c["mode"] = "live"
        self.assertEqual(client.post("/api/start", headers=headers, json={}).status_code, 400)

    def test_rounding_and_min_notional(self):
        self.assertEqual(Binance.quantity(RULE, dec("0.12345"), dec(100), True), dec("0.123"))
        with self.assertRaises(ValueError):
            Binance.quantity(RULE, dec("0.001"), dec(100), True)
        self.assertEqual(Binance.quantity(RULE, dec("0.001"), dec(100), False), dec("0.001"))

    def test_standalone_parser_and_immutable_order_identity(self):
        item = {"positionSide":"LONG", "side":"SELL", "status":"FILLED", "orderId":123,
                "executedQty":"20", "avgPrice":"2.7", "orderTime":1770000000000, "symbol":"TRUMPUSDT"}
        first = parse_orders([item], "5075281354358777856")[0]
        second = parse_orders([{**item, "orderUpdateTime":1770000001000}], "5075281354358777856")[0]
        self.assertEqual(first["operation"], "CLOSE")
        self.assertEqual(first["event_id"], second["event_id"])
        with self.assertRaises(ValueError):
            parse_orders([{**item,"status":"PARTIALLY_FILLED"}],"5075281354358777856")
        with self.assertRaises(ValueError):
            parse_orders([{**item,"executedQty":"NaN"}],"5075281354358777856")

    def test_limit_price_rounding(self):
        self.assertEqual(Binance.limit_price(RULE, dec("100.005"), "BUY"), dec("100.00"))
        self.assertEqual(Binance.limit_price(RULE, dec("100.005"), "SELL"), dec("100.01"))

    def test_limit_ioc_payload_and_market_reduce_only_close(self):
        client = Binance("testnet")
        client.request = Mock(return_value={})
        p = {"symbol":"ETHUSDT", "order_side":"BUY", "operation":"OPEN", "quantity":"0.3",
             "client_id":"cc_limit", "limit_price":"100.00"}
        client.order(p)
        params = client.request.call_args.args[2]
        self.assertEqual((params["type"],params["timeInForce"],params["price"]), ("LIMIT","IOC","100.00"))
        self.assertNotIn("reduceOnly", params)
        client.order({**p,"operation":"CLOSE","order_side":"SELL"})
        params = client.request.call_args.args[2]
        self.assertEqual((params["type"],params["reduceOnly"]), ("MARKET","true"))
        self.assertNotIn("price",params)

    def test_paper_non_crossing_limit_does_not_fake_fill(self):
        e = self.event(1)
        e["price"] = 99.5
        self.events.append(e)
        self.engine.tick()
        self.assertEqual(dec(self.engine.s["positions"]["ETHUSDT:LONG"]["quantity"]), 0)
        self.assertTrue(self.engine.s["running"])
        self.assertFalse(self.engine.s["pending"])
        self.assertEqual(self.engine.s["records"][0]["exchange_status"], "EXPIRED")

    def test_limit_partial_then_proportional_close(self):
        e = self.event(1)
        self.engine.s["pending"] = {"event":e,"key":"ETHUSDT:LONG","operation":"OPEN","quantity":"0.3",
            "client_id":"cc_partial","order_type":"LIMIT","time_in_force":"IOC","limit_price":"100"}
        self.engine.s["source_positions"]["ETHUSDT:LONG"] = "300"
        self.engine.settle({"status":"EXPIRED","executedQty":"0.1","avgPrice":"99.9"})
        self.assertTrue(self.engine.s["running"])
        self.assertEqual(dec(self.engine.s["positions"]["ETHUSDT:LONG"]["quantity"]), dec("0.1"))
        self.add(2,"CLOSE",150)
        self.assertEqual(dec(self.engine.s["positions"]["ETHUSDT:LONG"]["quantity"]), dec("0.05"))

    def test_limit_uses_lot_size_not_market_lot_size(self):
        rule = {"filters":[dict(f) for f in RULE["filters"]]}
        next(f for f in rule["filters"] if f["filterType"]=="MARKET_LOT_SIZE")["maxQty"]="0.1"
        self.assertEqual(Binance.quantity(rule,dec("0.3"),dec(100),True),dec("0.3"))
        with self.assertRaises(ValueError):
            Binance.quantity(rule,dec("0.3"),dec(100),False)


if __name__ == "__main__":
    unittest.main()
