import hashlib
import copy
import json
import os
import sqlite3
import threading
import time
import uuid
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


SYSTEM_RECORD_STATUSES = frozenset({"resume", "baseline"})


def record_category(record_or_status):
    if isinstance(record_or_status, dict):
        category = record_or_status.get("category")
        if category in ("system", "trade"):
            return category
        status = record_or_status.get("status")
    else:
        status = record_or_status
    return "system" if status in SYSTEM_RECORD_STATUSES else "trade"


SOURCE_HEALTH_ERRORS = (
    "爬虫尚未采集到熬鹰账户，请先在原项目后台配置该交易员",
    "未找到原爬虫数据库，请设置 SOURCE_DB 并启动 binance-copy-monitor",
    "源采集暂时中断或数据过期，已暂停跟单；恢复后将自动继续",
    "带单账户金额已过期，已暂停跟单；恢复后将自动继续",
    # Legacy wording kept so older sticky ledger errors still clear on recovery.
    "原爬虫未成功更新或数据过期，暂停跟单并检查原项目",
    "带单账户金额已过期，暂停跟单",
)


def fmt_dec(value, places=None):
    value = dec(value)
    if places is not None:
        value = value.quantize(dec(10) ** -places)
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def source_close_fields(event, source_before):
    if event.get("operation") != "CLOSE":
        return {}
    before = dec(source_before or 0)
    qty = dec(event.get("quantity") or 0)
    if before <= 0 or qty <= 0:
        return {}
    return {"source_quantity": str(qty), "source_before": str(before),
            "source_close_percent": fmt_dec(qty / before * 100, 2)}


def close_skip_note(reason, event, source_before, local_qty=0):
    fields = source_close_fields(event, source_before)
    if not fields:
        return reason
    note = (f"{reason}；源平仓 {fmt_dec(fields['source_quantity'])}，"
            f"占平仓前 {fields['source_close_percent']}%")
    if dec(local_qty) <= 0:
        note += "；本地无跟单仓位，未平仓"
    return note


class Engine:
    def __init__(self, config, exchange=None):
        self.c = config
        self.notifier = DingTalk(config.get("dingtalk", {}))
        self.notification_lock = threading.Lock()
        self.exchange = exchange or Binance(config["mode"], config["key"], config["secret"], config["testnet"],
                                           config.get("proxies") or None)
        self.lock = threading.RLock()
        self.control_lock = threading.Lock()
        self.stop_requested = threading.Event()
        self.stop_requested.set()
        self.stop_generation = 0
        self.submitting = False
        self.worker_error = None
        self.storage_error = None
        self.snapshot = None
        self.new_records = []
        self.new_seen = set()
        self.notification_cache = {}
        self.db = Path(config["db"])
        self.db.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as con:
            con.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS processed_events (event_id TEXT PRIMARY KEY)")
            con.execute("CREATE TABLE IF NOT EXISTS records (seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT UNIQUE NOT NULL, body TEXT NOT NULL)")
            con.execute("CREATE TABLE IF NOT EXISTS notification_queue (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, body TEXT NOT NULL)")
            row = con.execute("SELECT body FROM state WHERE id=1").fetchone()
        self.s = json.loads(row[0]) if row else {"initialized": False, "seen": [], "source_positions": {},
            "positions": {}, "records": [], "pending": None, "realized": 0, "fees": 0,
            "blocked_cycles": [], "last_event_time": ""}
        identity = hashlib.sha256(json.dumps({k: str(config[k]) for k in
            ("mode", "source_db", "portfolio", "capital", "multiplier", "key")}, sort_keys=True).encode()).hexdigest()
        stored = self.s.get("identity")
        if stored and stored != identity:
            def hashed(cfg, keys):
                return hashlib.sha256(json.dumps({k: str(cfg[k]) for k in keys}, sort_keys=True).encode()).hexdigest()
            keys = ("mode", "source_db", "portfolio", "capital", "multiplier", "leverage", "key")
            leverage_only = any(stored == hashed({**config, "leverage": lev}, keys) for lev in range(1, 21))
            if stored != hashed(config, keys) and not leverage_only:
                raise ValueError("配置/账户身份与现有账本不同。先核对并平完旧仓位，归档 data 中对应模式数据库后再变更")
        self.s["identity"] = identity
        # Migrate the old bounded history once, in the same transaction as state.
        self.new_seen.update(self.s.pop("seen", []))
        legacy_records = self.s.pop("records", [])
        for item in reversed(legacy_records):
            self.new_records.append({**item, "record_id": uuid.uuid4().hex})
        with self.connection() as con:
            records = [json.loads(r[0]) for r in con.execute("SELECT body FROM records ORDER BY seq DESC LIMIT 100")]
            queue = [json.loads(r[0]) for r in con.execute("SELECT body FROM notification_queue ORDER BY seq")]
        if "notifications" not in self.s:
            self.s["notifications"] = queue
        self.notification_cache = {item["id"]: json.dumps(item) for item in queue}
        self.s["records"] = legacy_records[:100] if legacy_records else records
        self.s["realized"] = str(dec(self.s["realized"]))
        if self.s.get("processing") and not self.s.get("pending"):
            self.s["review_required"] = "上次进程在处理信号时中断，请人工核对后归档账本重新初始化"
        self.s.update(running=False, error=None)
        if self.s.get("pending"):
            self.s["error"] = "重启发现待确认订单，系统将自动查询交易所结果"
        if self.s.get("review_required"):
            self.s["error"] = self.s["review_required"]
        if self.s["initialized"] and not self.s.get("baseline_verified"):
            self.s["review_required"] = "旧账本缺少已验证源仓位基线，请核对并归档旧账本后重新初始化"
            self.s["error"] = self.s["review_required"]
        self.source = {}
        self.account = {"available": None, "margin_balance": None, "wallet_balance": None,
                        "updated": None, "error": None, "mismatch": False, "label": "U本位", "hedge_mode": None}
        self._account_refreshed_at = 0
        self.live_positions = {}
        self._live_positions_refreshed_at = 0
        self.refresh_account(force=True)
        self.save()

    def refresh_account(self, force=False):
        now = time.time()
        if not force and self._account_refreshed_at and now - self._account_refreshed_at < 60:
            return
        hedge = self.account.get("hedge_mode")
        try:
            if not (self.c["key"] and self.c["secret"]):
                if self.c["mode"] == "paper":
                    capital = str(self.c["capital"])
                    self.account = {"available": capital, "margin_balance": capital, "wallet_balance": capital,
                                    "updated": stamp(), "error": None, "mismatch": False, "label": "模拟",
                                    "hedge_mode": False}
                else:
                    self.account = {"available": None, "margin_balance": None, "wallet_balance": None,
                                    "updated": stamp(), "error": "未配置API凭据", "mismatch": False, "label": "U本位",
                                    "hedge_mode": hedge}
                self._account_refreshed_at = now
                return
            # Even in paper mode, show real USD-M equity when keys exist (query only; no live orders).
            # /fapi/v2/account at most once per minute; position mode is checked only on Start.
            balances = self.exchange.account_balances()
            margin = dec(balances.get("margin_balance") or 0)
            label = "U本位" if self.c["mode"] != "paper" else "U本位·仅查询"
            self.account = {"available": str(dec(balances.get("available") or 0)),
                            "margin_balance": str(margin),
                            "wallet_balance": str(dec(balances.get("wallet_balance") or 0)),
                            "updated": stamp(), "error": None, "mismatch": False, "label": label,
                            "hedge_mode": hedge}
            self._account_refreshed_at = now
        except Exception as exc:
            self._account_refreshed_at = now
            self.account = {"available": None, "margin_balance": None, "wallet_balance": None,
                            "updated": stamp(), "error": str(exc)[:160], "mismatch": False, "label": "U本位",
                            "hedge_mode": hedge}

    def refresh_live_positions(self, owned, force=False):
        """Enrich open CopyCat positions with mark/PnL. Throttled to ~15s when holding."""
        owned = {k: v for k, v in (owned or {}).items() if dec(v.get("quantity", 0)) > 0}
        if not owned:
            self.live_positions = {}
            self._live_positions_refreshed_at = 0
            return
        now = time.time()
        if not force and self._live_positions_refreshed_at and now - self._live_positions_refreshed_at < 15:
            return
        leverage = self.c["leverage"]
        live = {}
        rows_by_symbol = {}
        try:
            if self.c["key"] and self.c["secret"]:
                for row in self.exchange.positions():
                    if row.get("positionSide") not in (None, "BOTH"):
                        continue
                    amt = dec(row.get("positionAmt") or 0)
                    if amt == 0:
                        continue
                    side = "LONG" if amt > 0 else "SHORT"
                    rows_by_symbol[f"{row['symbol']}:{side}"] = row
        except Exception as exc:
            self._live_positions_refreshed_at = now
            self.live_positions = {k: {**(self.live_positions.get(k) or {}), "error": str(exc)[:120]}
                                  for k in owned}
            return
        for key, pos in owned.items():
            symbol, side = key.split(":", 1)
            qty = dec(pos.get("quantity") or 0)
            entry = dec(pos.get("entry") or 0)
            row = rows_by_symbol.get(key)
            try:
                if row:
                    mark = dec(row.get("markPrice") or 0)
                    pnl = dec(row.get("unRealizedProfit") or 0)
                    liq = dec(row.get("liquidationPrice") or 0)
                    margin = dec(row.get("isolatedMargin") or row.get("positionInitialMargin") or 0)
                    lev = int(dec(row.get("leverage") or leverage))
                    notional = abs(dec(row.get("notional") or mark * qty))
                else:
                    _, mark = self.exchange.market(symbol)
                    pnl = (mark - entry) * qty if side == "LONG" else (entry - mark) * qty
                    liq = dec(0)
                    margin = (entry * qty) / dec(leverage) if leverage else dec(0)
                    lev = leverage
                    notional = mark * qty
                if mark <= 0 or entry <= 0 or qty <= 0:
                    raise ValueError("仓位价格无效")
                roe = (pnl / margin * dec(100)) if margin > 0 else dec(0)
                live[key] = {
                    "status": "持有中",
                    "mark_price": str(mark),
                    "entry_price": str(entry),
                    "quantity": str(qty),
                    "notional": str(notional),
                    "unrealized_pnl": str(pnl),
                    "roe_percent": str(roe),
                    "liquidation_price": str(liq) if liq > 0 else None,
                    "margin": str(margin) if margin > 0 else None,
                    "leverage": lev,
                    "updated": stamp(),
                    "error": None,
                }
            except Exception as exc:
                live[key] = {**(self.live_positions.get(key) or {}), "status": "持有中",
                             "error": str(exc)[:120], "updated": stamp()}
        self.live_positions = live
        self._live_positions_refreshed_at = now

    def check_position_mode(self):
        """Fetch dual/one-way once until known one-way; not polled with account balance."""
        if not (self.c["key"] and self.c["secret"]):
            if self.c["mode"] == "paper":
                self.account["hedge_mode"] = False
                return False
            raise ValueError("请先配置币安 API Key / Secret")
        if self.account.get("hedge_mode") is False:
            return False
        hedge = self.exchange.hedge_mode()
        self.account["hedge_mode"] = hedge
        return hedge

    def enable_one_way_mode(self):
        if not (self.c["key"] and self.c["secret"]):
            raise ValueError("请先配置币安 API Key / Secret")
        self.exchange.set_one_way_mode()
        if self.exchange.hedge_mode():
            raise ValueError("持仓模式仍为双向，请确认账户无持仓/挂单后再试")
        self.account["hedge_mode"] = False
        self.refresh_account(force=True)
    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.db, timeout=10)
        try:
            with con:
                yield con
        finally:
            con.close()

    def save(self):
        if self.storage_error:
            raise RuntimeError(self.storage_error)
        if self.stop_requested.is_set():
            self.s["running"] = False
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
            self.enqueue_notification("订单自动确认中", event, f"{error}；请求数量 {pending['quantity']}，系统将自动查询交易所结果，无需手动处理")
            self.s["last_notified_pending"] = pending["client_id"]
        try:
            with self.connection() as con:
                con.executemany("INSERT OR IGNORE INTO processed_events VALUES(?)", [(e,) for e in self.new_seen])
                con.executemany("INSERT OR IGNORE INTO records(record_id,body) VALUES(?,?)",
                                [(r["record_id"], json.dumps(r)) for r in self.new_records])
                notifications = {item["id"]: json.dumps(item) for item in self.s.get("notifications", [])}
                con.executemany("INSERT INTO notification_queue(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                                [(key, value) for key, value in notifications.items() if self.notification_cache.get(key) != value])
                con.executemany("DELETE FROM notification_queue WHERE id=?",
                                [(key,) for key in self.notification_cache.keys() - notifications.keys()])
                body = {k: v for k, v in self.s.items() if k not in ("records", "notifications")}
                con.execute("INSERT OR REPLACE INTO state VALUES(1,?)", (json.dumps(body),))
        except Exception:
            self.storage_error = "账本写入失败，执行已锁定；请恢复存储后重启并核对待确认订单"
            self.stop_requested.set()
            raise
        self.new_records.clear()
        self.new_seen.clear()
        self.notification_cache = notifications
        self.snapshot = copy.deepcopy(self._view())

    def has_seen(self, event_id):
        if event_id in self.new_seen:
            return True
        with self.connection() as con:
            return con.execute("SELECT 1 FROM processed_events WHERE event_id=?", (event_id,)).fetchone() is not None

    def history(self, before=None, limit=100, kind="all"):
        limit = max(1, min(int(limit), 100))
        if kind not in ("all", "trade", "system"):
            raise ValueError("分页参数无效")
        start = before if before is not None else 9223372036854775807
        if kind == "all":
            with self.connection() as con:
                rows = con.execute("SELECT seq,body FROM records WHERE seq < ? ORDER BY seq DESC LIMIT ?",
                                   (start, limit + 1)).fetchall()
            return {"records": self.enrich_source_fields([json.loads(r[1]) for r in rows[:limit]]),
                    "next_before": rows[limit - 1][0] if len(rows) > limit else None}
        collected = []
        next_before = None
        cursor = start
        with self.connection() as con:
            while next_before is None:
                rows = con.execute("SELECT seq,body FROM records WHERE seq < ? ORDER BY seq DESC LIMIT ?",
                                   (cursor, 200)).fetchall()
                if not rows:
                    break
                for seq, body in rows:
                    cursor = seq
                    rec = json.loads(body)
                    if record_category(rec) != kind:
                        continue
                    collected.append((seq, rec))
                    if len(collected) > limit:
                        next_before = collected[limit - 1][0]
                        break
                if len(rows) < 200:
                    break
        return {"records": self.enrich_source_fields([item[1] for item in collected[:limit]]), "next_before": next_before}

    def lookup_source_events(self, event_ids):
        ids = [i for i in dict.fromkeys(event_ids) if i and not str(i).startswith("manual_")]
        if not ids:
            return {}
        path = Path(self.c["source_db"]).resolve()
        if not path.exists():
            return {}
        found = {}
        try:
            con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                for i in range(0, len(ids), 200):
                    chunk = ids[i:i + 200]
                    marks = ",".join("?" * len(chunk))
                    for row in con.execute(
                        f"SELECT event_id,occurred_at,quantity,price FROM trade_events WHERE event_id IN ({marks})",
                        chunk,
                    ):
                        found[row[0]] = {"occurred_at": row[1], "quantity": row[2], "price": row[3]}
            finally:
                con.close()
        except sqlite3.Error:
            return {}
        return found

    def enrich_source_fields(self, records):
        need = [r.get("event_id") for r in records
                if r.get("operation") in ("OPEN", "CLOSE")
                and (r.get("source_time") in (None, "")
                     or r.get("source_price") in (None, "")
                     or r.get("source_quantity") in (None, ""))]
        sources = self.lookup_source_events(need)
        if not sources:
            return records
        for rec in records:
            src = sources.get(rec.get("event_id"))
            if not src:
                continue
            if rec.get("source_time") in (None, "") and src.get("occurred_at"):
                rec["source_time"] = src["occurred_at"]
            if rec.get("source_price") in (None, "") and src.get("price") not in (None, ""):
                rec["source_price"] = str(src["price"])
            if rec.get("source_quantity") in (None, "") and src.get("quantity") not in (None, ""):
                rec["source_quantity"] = str(src["quantity"])
        return records

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
            if self.storage_error:
                return
            self.s.update(running=False, error=error)
            self.save()

    def deliver_notification(self):
        if self.storage_error or not self.notifier.enabled or not self.notification_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                if self.storage_error:
                    return
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
                if self.storage_error:
                    return
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
            # Legacy collectors need no schema changes. The indexed anti-join also
            # finds late insertions with old timestamps instead of losing them.
            con.execute("ATTACH DATABASE ? AS ledger", (self.db.resolve().as_uri() + "?mode=ro",))
            con.execute("BEGIN")
            row = con.execute("SELECT data_json,updated_at FROM trader_state WHERE portfolio_id=?",
                              (self.c["portfolio"],)).fetchone()
            if not row:
                raise ValueError("爬虫尚未采集到熬鹰账户，请先在原项目后台配置该交易员")
            profile = json.loads(row["data_json"])
            events = [dict(r) for r in con.execute("SELECT e.* FROM trade_events e WHERE portfolio_id=? AND NOT EXISTS "
                                                   "(SELECT 1 FROM ledger.processed_events p WHERE p.event_id=e.event_id) ORDER BY occurred_at,event_id",
                                                   (self.c["portfolio"],))]
            status = con.execute("SELECT value_json FROM runtime_state WHERE key=?",
                                 ("collector_status:" + self.c["portfolio"],)).fetchone()
            status = json.loads(status[0]) if status else {}
            return profile, events, status
        finally:
            con.close()

    def record(self, event, status, note, **extra):
        body = {"record_id": uuid.uuid4().hex, "time": stamp(), "event_id": event.get("event_id"),
             "symbol": event.get("symbol"), "side": event.get("side"),
             "operation": event.get("operation"), "status": status,
             "category": record_category(status), "note": note}
        if event and not event.get("manual"):
            if event.get("occurred_at"):
                body["source_time"] = event["occurred_at"]
            if event.get("price") not in (None, ""):
                body["source_price"] = str(event["price"])
            if extra.get("source_quantity") is None and event.get("quantity") not in (None, ""):
                body["source_quantity"] = str(event["quantity"])
        body.update(extra)
        self.s["records"].insert(0, body)
        self.new_records.append(self.s["records"][0])
        self.s["records"] = self.s["records"][:100]
        kind = ("开仓成交" if event.get("operation") == "OPEN" else "平仓成交") if status == "filled" else "跳过订单"
        if status in ("filled", "skipped", "rejected"):
            self.enqueue_notification(kind, self.s["records"][0], note)

    def consume(self, e):
        self.new_seen.add(e["event_id"])
        self.s["last_event_time"] = max(self.s["last_event_time"], e["occurred_at"])

    def source_step(self, e):
        if e.get("side") not in ("LONG", "SHORT") or e.get("operation") not in ("OPEN", "CLOSE"):
            raise ValueError("信号方向或操作无效")
        key = e["symbol"] + ":" + e["side"]
        quantity = dec(e["quantity"])
        if quantity <= 0:
            raise ValueError("信号数量无效")
        before = dec(self.s["source_positions"].get(key, 0))
        if e["operation"] == "CLOSE" and quantity > before:
            self.s["review_required"] = "源平仓数量超过已知仓位，历史不完整；请核对源仓位基线"
            raise ValueError(self.s["review_required"])
        after = before + quantity if e["operation"] == "OPEN" else before - quantity
        self.s["source_positions"][key] = str(after)
        return key, before, after

    def check_source(self, profile, status):
        updated = status.get("last_success_at")
        captured = profile.get("captured_at")
        def fresh(value):
            try:
                return bool(value) and -10 <= age(value) <= self.c["source_age"]
            except (ValueError, TypeError):
                return False
        # last_success_at used to be the query-window start. A slow Playwright poll
        # could finish successfully yet look expired. captured_at is written at completion.
        if not fresh(updated) and not fresh(captured):
            raise ValueError("源采集暂时中断或数据过期，已暂停跟单；恢复后将自动继续")
        if not fresh(captured):
            raise ValueError("带单账户金额已过期，已暂停跟单；恢复后将自动继续")
        equity = dec(profile.get("margin_balance") or 0)
        if equity <= 0:
            raise ValueError("无法获取有效的带单保证金余额，不能计算跟单比例")
        return equity

    def establish_baseline(self, profile, events, status):
        self.check_source(profile, status)
        baseline = profile.get("position_baseline")
        positions, cutoff = {}, None
        if baseline:
            if baseline.get("portfolio_id") != self.c["portfolio"]:
                raise ValueError("源仓位基线的交易员ID不匹配")
            cutoff = datetime.fromisoformat(baseline["as_of"].replace("Z", "+00:00"))
            if cutoff.tzinfo is None or cutoff.timestamp() > time.time():
                raise ValueError("源仓位基线时间无效")
            start = status.get("history_window_start")
            if not start or datetime.fromisoformat(start) > cutoff:
                raise ValueError("采集窗口未覆盖源仓位基线时间")
            positions = {k: str(dec(v)) for k, v in baseline["positions"].items()}
            if any(k.rsplit(":", 1)[-1] not in ("LONG", "SHORT") or dec(v) < 0 for k, v in positions.items()):
                raise ValueError("源仓位基线数量/方向无效")
        elif status.get("history_complete") is not True:
            raise ValueError("源历史完整性未知：需要完整历史声明或经核实的源仓位基线，暂不允许跟单")
        previous = self.s["source_positions"]
        self.s["source_positions"] = positions
        try:
            for e in events:
                if cutoff is None or datetime.fromisoformat(e["occurred_at"]) > cutoff:
                    self.source_step(e)
        except Exception:
            self.s["source_positions"] = previous
            raise
        for e in events:
            self.consume(e)
        if cutoff is not None:
            self.s["baseline_as_of"] = cutoff.astimezone(timezone.utc).isoformat()
            self.s["last_event_time"] = max(self.s["last_event_time"], self.s["baseline_as_of"])
        self.s["blocked_cycles"] = [k for k, v in positions.items() if dec(v) > 0]
        self.s.update(initialized=True, baseline_verified=True)
        self.record({}, "baseline", "已验证源仓位基线；历史订单不追单，已有仓位等待新周期")

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
        with self.control_lock:
            generation = self.stop_generation
        with self.lock:
            if self.storage_error:
                raise ValueError(self.storage_error)
            if self.worker_error:
                raise ValueError(self.worker_error)
            if self.s.get("review_required"):
                raise ValueError(self.s["review_required"])
            if self.s["pending"] and not self.auto_resolve_pending():
                raise ValueError("订单结果正在自动确认，确认完成后将自动恢复跟单")
            if self.s["running"] and not self.stop_requested.is_set():
                return
            self.s.update(running=False, error=None)
            # Drain the snapshot while paused, including closes missed since the
            # last poll. Only signals arriving after this snapshot can be copied.
            self.tick()
            if self.s.get("review_required") or self.s.get("error"):
                raise ValueError(self.s.get("review_required") or self.s["error"])
            if not self.s["initialized"]:
                raise ValueError("信号初始化失败，请检查源数据库")
            profile, _, status = self.read_source()
            self.check_source(profile, status)
            if self.c["mode"] != "paper":
                if self.c["mode"] == "live" and not self.c["live_enabled"]:
                    raise ValueError("实盘未启用，请在 .env 设置 LIVE_TRADING_ENABLED=true 并重启")
                if self.check_position_mode():
                    raise ValueError("本版要求单向持仓模式；可在页面确认后自动切换")
                self.exchange.validate_account()
                self.reconcile()
            with self.control_lock:
                if generation != self.stop_generation:
                    raise ValueError("启动期间收到暂停请求，保持暂停")
                self.stop_requested.clear()
                self.s.update(running=True, error=None, resume_at=stamp(), auto_resume=True)
            self.save()

    def stop(self):
        with self.control_lock:
            self.stop_generation += 1
            self.stop_requested.set()
        # Do not wait for HTTP I/O. The executor persists this flag on its next
        # save; a process restart is always paused even if it exits beforehand.
        if self.lock.acquire(blocking=False):
            try:
                self.s["running"] = False
                self.s["auto_resume"] = False
                if not self.storage_error:
                    self.save()
            finally:
                self.lock.release()

    def check_stopped(self):
        if self.stop_requested.is_set():
            self.s["running"] = False
            raise ValueError("已暂停，未提交此订单")

    def try_auto_resume(self, profile, status):
        """Resume after a transient source outage without requiring another UI Start."""
        if not self.c.get("auto_resume", True):
            return False
        if not self.s.get("auto_resume"):
            return False
        if self.s["running"]:
            return False
        if self.s.get("review_required") or self.s.get("pending"):
            return False
        if self.storage_error or self.worker_error:
            return False
        self.check_source(profile, status)
        if self.c["mode"] != "paper":
            if self.c["mode"] == "live" and not self.c["live_enabled"]:
                return False
            if self.check_position_mode():
                return False
            self.exchange.validate_account()
            self.reconcile()
        # auto_resume=true is persisted only for an operator-enabled session.
        # It is therefore also the restart token: a process restart must not leave
        # copying paused merely because the in-memory stop Event starts as set.
        with self.control_lock:
            self.stop_requested.clear()
            self.s.update(running=True, error=None, resume_at=stamp())
        self.record({}, "resume", "源采集已恢复，自动恢复跟单")
        return True

    def tick(self):
        with self.lock:
            if self.storage_error:
                return
            try:
                # A timed-out submission may already have reached Binance. Query
                # by the unique client order ID until Binance reports a terminal
                # result; never submit the same trading intent again.
                if self.s.get("pending") and not self.auto_resolve_pending():
                    return
                profile, events, status = self.read_source()
                heartbeat = profile.get("captured_at") or status.get("last_success_at")
                self.source = {"name": profile.get("name", "熬鹰资本"), "equity": profile.get("margin_balance"),
                               "aum": profile.get("aum"), "updated": heartbeat, "count": len(events)}
                # Recover before draining newly published events. This matters when
                # a CLOSE happened during a collector outage but reached us only
                # after the source became healthy again.
                if not self.s["running"] and self.s.get("auto_resume") and not self.s.get("review_required"):
                    try:
                        self.try_auto_resume(profile, status)
                    except ValueError:
                        pass
                # Clear sticky source-health errors after collector recovers.
                if self.s.get("error") in SOURCE_HEALTH_ERRORS:
                    try:
                        self.check_source(profile, status)
                    except ValueError:
                        pass
                    else:
                        self.s["error"] = None
                if not self.s["initialized"]:
                    self.establish_baseline(profile, events, status)
                    self.s["coverage_end"] = status.get("history_window_end")
                    self.save()
                    return
                if self.s.get("review_required"):
                    self.s.update(running=False, error=self.s["review_required"])
                    return
                if self.stop_requested.is_set():
                    self.s["running"] = False
                if (status.get("history_window_start") and self.s.get("coverage_end") and
                        datetime.fromisoformat(status["history_window_start"]) > datetime.fromisoformat(self.s["coverage_end"])):
                    self.s["review_required"] = "源采集窗口存在缺口，请核对遗漏成交与源仓位"
                    raise ValueError(self.s["review_required"])
                if self.s["running"]:
                    equity = self.check_source(profile, status)
                    self.reconcile()
                else:
                    equity = dec(profile.get("margin_balance") or 0)
                for e in events:
                    if self.has_seen(e["event_id"]):
                        continue
                    if self.s.get("baseline_as_of") and datetime.fromisoformat(e["occurred_at"]) <= datetime.fromisoformat(self.s["baseline_as_of"]):
                        self.consume(e)
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
                        if self.stop_requested.is_set():
                            self.s["running"] = False
                        pre_resume = self.s.get("resume_at") and datetime.fromisoformat(e["occurred_at"]) <= datetime.fromisoformat(self.s["resume_at"])
                        local_qty = dec(self.s["positions"].get(key, {}).get("quantity", 0))
                        # 恢复前发生、恢复后才被采集到的开仓不追；但已有本地仓位的
                        # 平仓必须继续按比例执行以降低风险。否则采集短暂中断后，延迟
                        # 到达的 CLOSE 会被跳过并把账户永久锁进 review_required。
                        delayed_risk_reduce = (bool(pre_resume) and self.s["running"] and
                                               e["operation"] == "CLOSE" and local_qty > 0)
                        if not self.s["running"] or (pre_resume and not delayed_risk_reduce):
                            if after > 0 and local_qty == 0 and key not in self.s["blocked_cycles"]:
                                self.s["blocked_cycles"].append(key)
                            if key in self.s["blocked_cycles"] and after == 0:
                                self.s["blocked_cycles"].remove(key)
                            raise ValueError(close_skip_note(
                                "暂停期间/恢复前的信号不追单；已有跟单仓位须自行管理", e, before, local_qty))
                        # Never chase a stale OPEN. A stale CLOSE for a position we
                        # actually own is risk reduction and remains actionable.
                        if (not -10 <= age(e["occurred_at"]) <= self.c["signal_age"] and
                                not (e["operation"] == "CLOSE" and local_qty > 0)):
                            raise ValueError("信号已过期，停止跟单以免漏平仓/追历史订单")
                        if key in self.s["blocked_cycles"]:
                            # Still reduce risk: copy CLOSE proportionally if we already hold.
                            if e["operation"] == "CLOSE" and local_qty > 0:
                                self.execute(e, key, before, equity)
                                if after == 0:
                                    self.s["blocked_cycles"].remove(key)
                            else:
                                if after == 0:
                                    self.s["blocked_cycles"].remove(key)
                                raise ValueError(close_skip_note(
                                    "启动前已有源仓位，此周期未参与开仓", e, before, local_qty))
                        else:
                            self.execute(e, key, before, equity)
                    except ValueError as exc:
                        self.record(e, "skipped", str(exc), **source_close_fields(e, before))
                        if self.stop_requested.is_set() and after > 0 and dec(self.s["positions"].get(key, {}).get("quantity", 0)) == 0 and key not in self.s["blocked_cycles"]:
                            self.s["blocked_cycles"].append(key)
                        # 可确定结果的 CLOSE 跳过不再制造永久人工锁。保留账本仓位，
                        # 后续源减仓继续按剩余比例执行；源完全平仓时本地也会完全平仓。
                        if "过期" in str(exc):
                            self.s.update(running=False, error=str(exc))
                    self.s.pop("processing", None)
                    self.save()
                    if self.s["pending"] or self.s.get("error"):
                        break
                self.s["last_poll"] = stamp()
                self.s["coverage_end"] = status.get("history_window_end", self.s.get("coverage_end"))
                # After draining while paused, resume if Start had armed auto_resume and source is healthy.
                if not self.s["running"]:
                    try:
                        self.try_auto_resume(profile, status)
                    except ValueError:
                        pass
                self.refresh_account()
                self.save()
            except Exception as exc:
                if self.storage_error:
                    return
                self.s.update(running=False, error=str(exc))
                if self.s.get("processing") and not self.s.get("pending"):
                    self.s["review_required"] = "信号执行中断，请人工核对源记录和本系统持仓后归档账本重新初始化"
                self.save()

    def execute(self, e, key, source_before, equity):
        self.check_stopped()
        opening = e["operation"] == "OPEN"
        own = self.s["positions"].get(key, {"quantity": "0", "entry": "0"})
        if not opening and dec(own["quantity"]) == 0:
            raise ValueError("无本系统跟单仓位，无需平仓")
        if opening and any(k.split(":")[0] == e["symbol"] and k != key and dec(v["quantity"]) > 0
                           for k,v in self.s["positions"].items()):
            raise ValueError("同币种已有反向持仓，单向模式不跟此开仓")
        rule, price = self.exchange.market(e["symbol"])
        order_side = "BUY" if (e["side"] == "LONG") == opening else "SELL"
        if opening:
            quantity = dec(e["quantity"]) * self.c["capital"] / equity * self.c["multiplier"]
        else:
            if source_before <= 0 or dec(e["quantity"]) > source_before:
                raise ValueError("源仓位数量不完整，停止自动平仓并人工核对")
            quantity = dec(own["quantity"]) * dec(e["quantity"]) / source_before
        quantity = self.exchange.quantity(rule, quantity, price, opening)
        if opening:
            risk_price = price
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
                self.check_stopped()
                self.exchange.prepare(e["symbol"], self.c["leverage"])
        pending = {"event": e, "key": key, "symbol": e["symbol"], "operation": e["operation"],
            "quantity": str(quantity), "price": str(price),
            "order_side": order_side, "order_type": "MARKET",
            "limit_price": None, "time_in_force": None,
            "source_before": str(source_before) if not opening else None,
            "client_id": "cc_" + hashlib.sha256((self.c["mode"] + e["event_id"]).encode()).hexdigest()[:28]}
        self.check_stopped()
        self.s["pending"] = pending
        self.save()  # durable intent before network submission; never blindly resend
        # Serialize the decision to submit with pause acknowledgement. Once this
        # reservation is made, this one order is in flight and must be settled.
        with self.control_lock:
            submit = not self.stop_requested.is_set()
            self.submitting = submit
        if not submit:
            self.s["pending"] = None
            self.save()
            self.check_stopped()
        try:
            if self.c["mode"] == "paper":
                result = self.paper_result(pending)
            else:
                result = self.exchange.order(pending)
        finally:
            with self.control_lock:
                self.submitting = False
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
            raise RuntimeError("订单结果尚未确定，系统将自动查询，不会重复下单")
        quantity = dec(result.get("executedQty", 0))
        price = dec(result.get("avgPrice", 0))
        if quantity < 0 or quantity > dec(p["quantity"]) or (quantity > 0 and price <= 0):
            raise RuntimeError("成交回报无效，系统将继续查询交易所结果")
        own = self.s["positions"].setdefault(p["key"], {"quantity": "0", "entry": "0"})
        old_q, old_p = dec(own["quantity"]), dec(own["entry"])
        if p["operation"] == "OPEN":
            total = old_q + quantity
            own.update(quantity=str(total), entry=str((old_q * old_p + quantity * price) / total if total else 0))
        else:
            if quantity > old_q:
                raise RuntimeError("平仓成交数量超出本系统账本，需人工核对")
            pnl = (price - old_p) * quantity * (1 if p["event"]["side"] == "LONG" else -1)
            self.s["realized"] = str(dec(self.s["realized"]) + pnl)
            own["quantity"] = str(old_q - quantity)
        normal_ioc = p.get("order_type") == "LIMIT" and p.get("time_in_force") == "IOC" and status in ("EXPIRED", "CANCELED")
        note = "模拟成交" if self.c["mode"] == "paper" else "交易所确认结果"
        if p["event"].get("manual"):
            note = ("模拟" if self.c["mode"] == "paper" else "交易所确认结果") + "；手动平仓"
        if normal_ioc:
            note = ("模拟：" if self.c["mode"] == "paper" else "") + ("限价部分成交，剩余自动取消" if quantity else "限价未成交，剩余自动取消；不转市价")
        extras = source_close_fields(p["event"], p.get("source_before"))
        if p["operation"] == "CLOSE" and old_q > 0 and quantity > 0:
            extras["local_close_percent"] = fmt_dec(quantity / old_q * 100, 2)
            note = (f"{note}；按源平仓比例跟平 {extras.get('source_close_percent', '?')}%"
                    f"（本地平 {extras['local_close_percent']}%）")
        if p["operation"] == "CLOSE":
            extras["realized_pnl"] = str(pnl)
        self.record(p["event"], "filled" if quantity else "rejected", note,
                    quantity=str(quantity), price=str(price), client_id=p["client_id"], exchange_status=status,
                    order_type=p.get("order_type", "MARKET"), limit_price=p.get("limit_price"),
                    **extras)
        self.s["pending"] = None
        self.s.pop("processing", None)
        if status != "FILLED" and not normal_ioc:
            # 已取得交易所终态且按实际成交量入账，账实一致；短暂停止后由
            # auto_resume 自动继续，不再要求人工清锁。
            self.s.update(running=False, error="订单未完全成交，已按实际成交量入账并等待自动恢复")
        self.save()

    def auto_resolve_pending(self):
        """Safely reconcile an uncertain submission without operator action."""
        if not self.s.get("pending"):
            return True
        try:
            if self.c["mode"] == "paper":
                result = self.paper_result(self.s["pending"])
            else:
                result = self.exchange.query(self.s["pending"])
            self.s["error"] = None
            self.settle(result)
            return True
        except Exception:
            self.s.update(running=False,
                error="订单结果尚未确定，系统正在自动查询；不会重复下单")
            self.save()
            return False

    def manual_close(self, key=None):
        """Pause copying and close one or all CopyCat-owned positions."""
        self.stop()
        with self.lock:
            if self.s.get("pending"):
                raise ValueError("仍有订单结果正在自动确认，请稍后再平仓")
            if self.c["mode"] != "paper":
                self.reconcile()
            targets = [key] if key else [
                name for name, position in self.s["positions"].items()
                if dec(position.get("quantity", 0)) > 0
            ]
            if key and (key not in self.s["positions"] or dec(self.s["positions"][key].get("quantity", 0)) <= 0):
                raise ValueError("该持仓不存在或已经平仓")
            if not targets:
                raise ValueError("当前没有可平的 CopyCat 持仓")
            closed = 0
            for name in targets:
                symbol, side = name.rsplit(":", 1)
                quantity = dec(self.s["positions"][name]["quantity"])
                _, price = self.exchange.market(symbol)
                event = {"event_id": "manual_" + uuid.uuid4().hex, "occurred_at": stamp(),
                         "symbol": symbol, "side": side, "operation": "CLOSE",
                         "quantity": str(quantity), "price": str(price), "manual": True}
                pending = {"event": event, "key": name, "symbol": symbol, "operation": "CLOSE",
                           "quantity": str(quantity), "price": str(price),
                           "order_side": "SELL" if side == "LONG" else "BUY", "order_type": "MARKET",
                           "limit_price": None, "time_in_force": None, "source_before": None,
                           "client_id": "cc_manual_" + uuid.uuid4().hex[:24]}
                self.s["pending"] = pending
                self.save()
                result = self.paper_result(pending) if self.c["mode"] == "paper" else self.exchange.order(pending)
                self.settle(result)
                if dec(self.s["source_positions"].get(name, 0)) > 0 and name not in self.s["blocked_cycles"]:
                    self.s["blocked_cycles"].append(name)
                    self.save()
                closed += 1
            return closed

    def resolve(self):
        with self.lock:
            if self.storage_error:
                raise ValueError(self.storage_error)
            if not self.s["pending"]:
                raise ValueError("当前无待确认订单")
            if self.c["mode"] == "paper":
                p = self.s["pending"]
                self.settle(self.paper_result(p))
            else:
                self.settle(self.exchange.query(self.s["pending"]))
            self.s.update(running=False, error=self.s.get("review_required"))
            self.save()

    def view(self):
        # Dashboard refresh should always re-check equity; keep Binance I/O off the trading lock.
        self.refresh_account()
        if self.storage_error:
            result = copy.deepcopy(self.snapshot)
        elif self.lock.acquire(blocking=False):
            try:
                result = copy.deepcopy(self._view())
            finally:
                self.lock.release()
        else:
            result = copy.deepcopy(self.snapshot)
        owned = {k: v for k, v in (result.get("positions") or {}).items() if dec(v.get("quantity", 0)) > 0}
        self.refresh_live_positions(owned)
        result["live_positions"] = copy.deepcopy(self.live_positions)
        result["account"] = copy.deepcopy(self.account)
        if self.snapshot is not None:
            self.snapshot["account"] = copy.deepcopy(self.account)
            self.snapshot["live_positions"] = copy.deepcopy(self.live_positions)
        result["stop_requested"] = self.stop_requested.is_set()
        result["order_in_flight"] = self.submitting
        if self.stop_requested.is_set():
            result["running"] = False
        if self.storage_error:
            result["error"] = self.storage_error
        elif self.worker_error:
            result["error"] = self.worker_error
        try:
            result["source_stale"] = not result["source"].get("updated") or not -10 <= age(result["source"]["updated"]) <= self.c["source_age"]
        except (ValueError, TypeError):
            result["source_stale"] = True
        result["executor_busy"] = not self.lock.acquire(blocking=False)
        if not result["executor_busy"]:
            self.lock.release()
        return result

    def _view(self):
        try:
            source_stale = not self.source.get("updated") or not -10 <= age(self.source["updated"]) <= self.c["source_age"]
        except (ValueError, TypeError):
            source_stale = True
        return {"mode": self.c["mode"], "capital": str(self.c["capital"]), "multiplier": str(self.c["multiplier"]),
            "opening_order": "MARKET", "closing_order": "MARKET reduceOnly",
            "max_gross": str(self.c["max_gross"]), "leverage": self.c["leverage"], "source": self.source,
            "account": self.account, "live_positions": self.live_positions,
            "running": self.s["running"], "error": self.s["error"], "positions": self.s["positions"],
            "records": self.enrich_source_fields([dict(r) for r in self.s["records"][:100]]), "pending": bool(self.s["pending"]),
            "last_poll": self.s.get("last_poll"), "realized": self.s["realized"],
            "source_stale": source_stale,
            "dingtalk": {"enabled": self.notifier.enabled, "pending": len(self.s.get("notifications", [])),
                "last_sent": self.s.get("notification_last_sent"), "error": self.s.get("notification_error")},
            "credentials_configured": bool(self.c["key"] and self.c["secret"])}
