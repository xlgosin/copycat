import hashlib
import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from exchange import Binance, dec
from notifications import DingTalk, message


ROOT = Path(__file__).resolve().parent


def stamp():
    return datetime.now(timezone.utc).isoformat()


def age(value):
    return time.time() - datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class Engine:
    def __init__(self, config, exchange=None):
        self.c = config
        self.notifier = DingTalk(config.get("dingtalk", {}))
        self.notification_lock = threading.Lock()
        self.exchange = exchange or Binance(config["mode"], config["key"], config["secret"], config["testnet"])
        self.lock = threading.RLock()
        self.db = Path(config["db"])
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            row = con.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.s = json.loads(row[0]) if row else {"initialized": False, "seen": [], "source_positions": {},
            "positions": {}, "records": [], "pending": None, "realized": 0, "fees": 0,
            "blocked_cycles": [], "last_event_time": ""}
        identity = hashlib.sha256(json.dumps({k: str(config[k]) for k in
            ("mode", "source_db", "portfolio", "capital", "multiplier", "leverage", "key")}, sort_keys=True).encode()).hexdigest()
        if self.s.get("identity") and self.s["identity"] != identity:
            raise ValueError("配置/账户身份与现有账本不同。先核对并平完旧仓位，归档 data 中对应模式数据库后再变更")
        self.s["identity"] = identity
        if self.s.get("processing") and not self.s.get("pending"):
            self.s["review_required"] = "上次进程在处理信号时中断，请人工核对后归档账本重新初始化"
        self.s.update(running=False, error=None)
        if self.s.get("pending"):
            self.s["error"] = "重启发现待确认订单，请核对订单后继续"
        if self.s.get("review_required"):
            self.s["error"] = self.s["review_required"]
        self.source = {}
        self.save()

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.db, timeout=10)
        try:
            with con:
                yield con
        finally:
            con.close()

    def save(self):
        error = self.s.get("error")
        if error and error != self.s.get("last_notified_error"):
            self.enqueue_notification("异常暂停", {"time": stamp()}, error)
        self.s["last_notified_error"] = error
        pending = self.s.get("pending")
        if pending and error and pending["client_id"] != self.s.get("last_notified_pending"):
            event = {**pending["event"], "time": stamp(), "client_id": pending["client_id"]}
            # Pending quantity is requested, not confirmed filled.
            event.pop("quantity", None)
            event.pop("price", None)
            self.enqueue_notification("待确认订单", event, f"{error}；请求数量 {pending['quantity']}，尚未确认成交，请点击核对订单")
            self.s["last_notified_pending"] = pending["client_id"]
        with self.connection() as con:
            con.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(self.s),))

    def enqueue_notification(self, kind, event, note):
        if not self.notifier.enabled:
            return
        text = message(self.c["mode"], kind, event, note, self.c["capital"])
        identifier = hashlib.sha256(text.encode()).hexdigest()[:20]
        queue = self.s.setdefault("notifications", [])
        if not any(n["id"] == identifier for n in queue):
            queue.append({"id": identifier, "text": text + f"\n通知编号：{identifier}", "attempts": 0, "next_try": 0})

    def report_error(self, error):
        with self.lock:
            self.s.update(running=False, error=error)
            self.save()

    def deliver_notification(self):
        if not self.notifier.enabled or not self.notification_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                queue = self.s.get("notifications", [])
                if not queue or queue[0]["next_try"] > time.time():
                    return
                item = dict(queue[0])
            error = None
            try:
                self.notifier.send(item)  # No trading lock held during HTTP I/O.
            except Exception as exc:
                # Never leak a webhook/signature or propagate a notification error into trading.
                error = str(exc) if isinstance(exc, ValueError) else "钉钉发送失败，等待重试"
            with self.lock:
                queue = self.s.get("notifications", [])
                if not queue or queue[0]["id"] != item["id"]:
                    return
                if error:
                    queue[0]["attempts"] += 1
                    queue[0]["next_try"] = time.time() + min(900, 15 * 2 ** min(queue[0]["attempts"], 6))
                else:
                    queue.pop(0)
                    self.s["notification_last_sent"] = stamp()
                self.s["notification_error"] = error
                self.save()
        finally:
            self.notification_lock.release()

    def read_source(self):
        path = Path(self.c["source_db"]).resolve()
        if not path.exists():
            raise ValueError("未找到原爬虫数据库，请设置 SOURCE_DB 并启动 binance-copy-monitor")
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN")
            row = con.execute("SELECT data_json,updated_at FROM trader_state WHERE portfolio_id=?",
                              (self.c["portfolio"],)).fetchone()
            if not row:
                raise ValueError("爬虫尚未采集到熬鹰账户，请先在原项目后台配置该交易员")
            profile = json.loads(row["data_json"])
            events = [dict(r) for r in con.execute("SELECT * FROM trade_events WHERE portfolio_id=? ORDER BY occurred_at,event_id",
                                                   (self.c["portfolio"],))]
            status = con.execute("SELECT value_json FROM runtime_state WHERE key=?",
                                 ("collector_status:" + self.c["portfolio"],)).fetchone()
            status = json.loads(status[0]) if status else {}
            return profile, events, status
        finally:
            con.close()

    def record(self, event, status, note, **extra):
        self.s["records"].insert(0, {"time": stamp(), "event_id": event.get("event_id"),
             "symbol": event.get("symbol"), "side": event.get("side"),
             "operation": event.get("operation"), "status": status, "note": note, **extra})
        self.s["records"] = self.s["records"][:1000]
        kind = ("开仓成交" if event.get("operation") == "OPEN" else "平仓成交") if status == "filled" else "跳过订单"
        if status in ("filled", "skipped", "rejected"):
            self.enqueue_notification(kind, self.s["records"][0], note)

    def consume(self, e):
        self.s["seen"].append(e["event_id"])
        self.s["last_event_time"] = max(self.s["last_event_time"], e["occurred_at"])

    def source_step(self, e):
        if e.get("side") not in ("LONG", "SHORT") or e.get("operation") not in ("OPEN", "CLOSE"):
            raise ValueError("信号方向或操作无效")
        key = e["symbol"] + ":" + e["side"]
        quantity = dec(e["quantity"])
        if quantity <= 0:
            raise ValueError("信号数量无效")
        before = dec(self.s["source_positions"].get(key, 0))
        after = before + quantity if e["operation"] == "OPEN" else max(dec(0), before - quantity)
        self.s["source_positions"][key] = str(after)
        return key, before, after

    def check_source(self, profile, status):
        updated = status.get("last_success_at")
        if status.get("last_error") or not updated or not -10 <= age(updated) <= self.c["source_age"]:
            raise ValueError("原爬虫未成功更新或数据过期，暂停跟单并检查原项目")
        if not profile.get("captured_at") or not -10 <= age(profile["captured_at"]) <= self.c["source_age"]:
            raise ValueError("带单账户金额已过期，暂停跟单")
        equity = dec(profile.get("margin_balance") or 0)
        if equity <= 0:
            raise ValueError("无法获取有效的带单保证金余额，不能计算跟单比例")
        return equity

    def reconcile(self):
        if self.c["mode"] == "paper":
            return
        actual = {}
        for p in self.exchange.positions():
            q = dec(p["positionAmt"])
            if q:
                if p["positionSide"] != "BOTH" or p.get("marginType") != "isolated":
                    raise ValueError("检测到双向/非逐仓持仓，停止跟单")
                actual[p["symbol"] + (":LONG" if q > 0 else ":SHORT")] = abs(q)
        owned = {k: dec(v["quantity"]) for k, v in self.s["positions"].items() if dec(v["quantity"]) > 0}
        if actual != owned:
            raise ValueError("交易所持仓与 CopyCat 账本不一致，需人工核对（外部交易、强平或部分成交）")
        if self.exchange.request("GET", "/fapi/v1/openOrders", signed=True):
            raise ValueError("账户有未完成委托，停止跟单")
        if self.exchange.request("GET", "/fapi/v1/openAlgoOrders", signed=True):
            raise ValueError("账户有条件委托，停止跟单")

    def start(self):
        with self.lock:
            if self.s.get("review_required"):
                raise ValueError(self.s["review_required"])
            if self.s["pending"]:
                raise ValueError("有结果待确认的订单，请先点击核对订单")
            if not self.s["initialized"]:
                self.tick()
                if not self.s["initialized"]:
                    raise ValueError("信号初始化失败，请检查源数据库")
            profile, _, status = self.read_source()
            self.check_source(profile, status)
            if self.c["mode"] != "paper":
                if self.c["mode"] == "live" and not self.c["live_enabled"]:
                    raise ValueError("实盘未启用，请在 .env 设置 LIVE_TRADING_ENABLED=true 并重启")
                account = self.exchange.validate_account()
                if dec(account["totalMarginBalance"]) > self.c["capital"] + dec(5):
                    raise ValueError("请使用约100 USDT的独立合约账户；当前账户余额超出预算范围")
                self.reconcile()
            self.s.update(running=True, error=None)
            self.save()

    def stop(self):
        with self.lock:
            self.s["running"] = False
            self.save()

    def tick(self):
        with self.lock:
            try:
                profile, events, status = self.read_source()
                self.source = {"name": profile.get("name", "熬鹰资本"), "equity": profile.get("margin_balance"),
                               "aum": profile.get("aum"), "updated": status.get("last_success_at"), "count": len(events)}
                if not self.s["initialized"]:
                    for e in events:
                        try:
                            self.source_step(e)
                        except (ValueError, TypeError, KeyError):
                            pass
                        self.consume(e)
                    self.s["blocked_cycles"] = [k for k,v in self.s["source_positions"].items() if dec(v)>0]
                    self.s["initialized"] = True
                    self.record({}, "baseline", "历史订单仅建立基线，不追单；已有仓位等待新周期")
                    self.save()
                    return
                if self.s["pending"]:
                    return
                if self.s.get("review_required"):
                    self.s.update(running=False, error=self.s["review_required"])
                    return
                if self.s["running"]:
                    equity = self.check_source(profile, status)
                    self.reconcile()
                else:
                    equity = dec(profile.get("margin_balance") or 0)
                seen = set(self.s["seen"])
                for e in events:
                    if e["event_id"] in seen:
                        continue
                    if e["occurred_at"] < self.s["last_event_time"]:
                        self.consume(e)
                        self.s.update(running=False, error="发现补录/乱序历史成交，请核查源仓位，停止自动跟单")
                        self.s["review_required"] = self.s["error"]
                        self.record(e, "blocked", self.s["error"])
                        self.save()
                        break
                    key, before, after = self.source_step(e)
                    self.consume(e)
                    # If interrupted before intent/fill is recorded, startup requires review.
                    self.s["processing"] = e["event_id"]
                    self.save()
                    try:
                        if not self.s["running"]:
                            if key in self.s["blocked_cycles"] and after == 0:
                                self.s["blocked_cycles"].remove(key)
                            raise ValueError("已暂停，此信号不追单；已有跟单仓位须自行管理")
                        if not -10 <= age(e["occurred_at"]) <= self.c["signal_age"]:
                            raise ValueError("信号已过期，停止跟单以免漏平仓/追历史订单")
                        if key in self.s["blocked_cycles"]:
                            if after == 0:
                                self.s["blocked_cycles"].remove(key)
                            raise ValueError("启动前已有源仓位，此周期未参与")
                        self.execute(e, key, before, equity)
                    except ValueError as exc:
                        self.record(e, "skipped", str(exc))
                        if dec(self.s["positions"].get(key, {}).get("quantity", 0)) > 0 and e["operation"] == "CLOSE":
                            self.s["review_required"] = "未能执行源平仓信号，请在币安人工核对持仓并归档账本后重新初始化"
                            self.s.update(running=False, error=self.s["review_required"])
                        if "过期" in str(exc):
                            self.s.update(running=False, error=str(exc))
                    self.s.pop("processing", None)
                    self.save()
                    if self.s["pending"] or self.s.get("error"):
                        break
                self.s["last_poll"] = stamp()
                self.save()
            except Exception as exc:
                self.s.update(running=False, error=str(exc))
                if self.s.get("processing") and not self.s.get("pending"):
                    self.s["review_required"] = "信号执行中断，请人工核对源记录和本系统持仓后归档账本重新初始化"
                self.save()

    def execute(self, e, key, source_before, equity):
        opening = e["operation"] == "OPEN"
        own = self.s["positions"].get(key, {"quantity": "0", "entry": "0"})
        if not opening and dec(own["quantity"]) == 0:
            raise ValueError("无本系统跟单仓位，无需平仓")
        if opening and any(k.split(":")[0] == e["symbol"] and k != key and dec(v["quantity"]) > 0
                           for k,v in self.s["positions"].items()):
            raise ValueError("同币种已有反向持仓，单向模式不跟此开仓")
        rule, price = self.exchange.market(e["symbol"])
        order_side = "BUY" if (e["side"] == "LONG") == opening else "SELL"
        limit_price = None
        if opening:
            source_price = dec(e["price"])
            if source_price <= 0 or abs(price / source_price - 1) * 100 > self.c["deviation"]:
                raise ValueError("当前价格偏离源成交价格过大，跳过开仓")
            quantity = dec(e["quantity"]) * self.c["capital"] / equity * self.c["multiplier"]
            limit_price = Binance.limit_price(rule, source_price, order_side)
        else:
            if source_before <= 0 or dec(e["quantity"]) > source_before:
                raise ValueError("源仓位数量不完整，停止自动平仓并人工核对")
            quantity = dec(own["quantity"]) * dec(e["quantity"]) / source_before
        quantity = self.exchange.quantity(rule, quantity, limit_price if opening else price, opening)
        if opening:
            risk_price = max(price, limit_price)
            gross = dec(0)
            for k,v in self.s["positions"].items():
                if dec(v["quantity"]) > 0:
                    _, mark = self.exchange.market(k.split(":")[0])
                    gross += mark * dec(v["quantity"])
            if gross + quantity * risk_price > self.c["max_gross"]:
                raise ValueError("开仓将超过总名义敞口上限，跳过")
            if self.c["mode"] != "paper":
                account = self.exchange.validate_account()
                available = min(dec(account["availableBalance"]), self.c["capital"])
                if quantity * risk_price / self.c["leverage"] + quantity * risk_price * dec("0.002") > available:
                    raise ValueError("可用保证金不足（已预留费用）")
                self.exchange.prepare(e["symbol"], self.c["leverage"])
        pending = {"event": e, "key": key, "symbol": e["symbol"], "operation": e["operation"],
            "quantity": str(quantity), "price": str(price),
            "order_side": order_side, "order_type": "LIMIT" if opening else "MARKET",
            "limit_price": str(limit_price) if opening else None, "time_in_force": "IOC" if opening else None,
            "client_id": "cc_" + hashlib.sha256((self.c["mode"] + e["event_id"]).encode()).hexdigest()[:28]}
        self.s["pending"] = pending
        self.save()  # durable intent before network submission; never blindly resend
        if self.c["mode"] == "paper":
            result = self.paper_result(pending)
        else:
            result = self.exchange.order(pending)
        self.settle(result)

    @staticmethod
    def paper_result(pending):
        price = dec(pending["price"])
        if pending.get("order_type") == "LIMIT":
            limit = dec(pending["limit_price"])
            crosses = price <= limit if pending["order_side"] == "BUY" else price >= limit
            if not crosses:
                return {"status": "EXPIRED", "executedQty": "0", "avgPrice": "0"}
        return {"status": "FILLED", "executedQty": pending["quantity"], "avgPrice": str(price)}

    def settle(self, result):
        p = self.s["pending"]
        status = result.get("status")
        if status not in ("FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
            raise RuntimeError("订单结果尚未确定，已暂停；点击核对订单，不会重复下单")
        quantity = dec(result.get("executedQty", 0))
        price = dec(result.get("avgPrice", 0))
        if quantity < 0 or quantity > dec(p["quantity"]) or (quantity > 0 and price <= 0):
            raise RuntimeError("成交回报无效，请核对订单")
        own = self.s["positions"].setdefault(p["key"], {"quantity": "0", "entry": "0"})
        old_q, old_p = dec(own["quantity"]), dec(own["entry"])
        if p["operation"] == "OPEN":
            total = old_q + quantity
            own.update(quantity=str(total), entry=str((old_q * old_p + quantity * price) / total if total else 0))
        else:
            if quantity > old_q:
                raise RuntimeError("平仓成交数量超出本系统账本，需人工核对")
            pnl = (price - old_p) * quantity * (1 if p["event"]["side"] == "LONG" else -1)
            self.s["realized"] += float(pnl)
            own["quantity"] = str(old_q - quantity)
        normal_ioc = p.get("order_type") == "LIMIT" and p.get("time_in_force") == "IOC" and status in ("EXPIRED", "CANCELED")
        note = "模拟成交" if self.c["mode"] == "paper" else "交易所确认结果"
        if normal_ioc:
            note = ("模拟：" if self.c["mode"] == "paper" else "") + ("限价部分成交，剩余自动取消" if quantity else "限价未成交，剩余自动取消；不转市价")
        self.record(p["event"], "filled" if quantity else "rejected", note,
                    quantity=str(quantity), price=str(price), client_id=p["client_id"], exchange_status=status,
                    order_type=p.get("order_type", "MARKET"), limit_price=p.get("limit_price"),
                    **({"realized_pnl": str(pnl)} if p["operation"] == "CLOSE" else {}))
        self.s["pending"] = None
        self.s.pop("processing", None)
        if status != "FILLED" and not normal_ioc:
            self.s.update(running=False, error="订单未完全成交，请检查记录后再启动")
        self.save()

    def resolve(self):
        with self.lock:
            if not self.s["pending"]:
                raise ValueError("当前无待确认订单")
            if self.c["mode"] == "paper":
                p = self.s["pending"]
                self.settle(self.paper_result(p))
            else:
                self.settle(self.exchange.query(self.s["pending"]))
            self.s.update(running=False, error=None)
            self.save()

    def view(self):
        with self.lock:
            try:
                source_stale = not self.source.get("updated") or not -10 <= age(self.source["updated"]) <= self.c["source_age"]
            except (ValueError, TypeError):
                source_stale = True
            return {"mode": self.c["mode"], "capital": str(self.c["capital"]), "multiplier": str(self.c["multiplier"]),
                "opening_order": "LIMIT IOC · 熬鹰成交均价", "closing_order": "MARKET reduceOnly",
                "max_gross": str(self.c["max_gross"]), "leverage": self.c["leverage"], "source": self.source,
                "running": self.s["running"], "error": self.s["error"], "positions": self.s["positions"],
                "records": self.s["records"][:100], "pending": bool(self.s["pending"]),
                "last_poll": self.s.get("last_poll"), "realized": self.s["realized"],
                "source_stale": source_stale,
                "dingtalk": {"enabled": self.notifier.enabled, "pending": len(self.s.get("notifications", [])),
                    "last_sent": self.s.get("notification_last_sent"), "error": self.s.get("notification_error")},
                "credentials_configured": bool(self.c["key"] and self.c["secret"])}
