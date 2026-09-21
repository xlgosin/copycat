"""Optional standalone public-API collector; run separately from app.py."""
import hashlib
import argparse
import json
import math
import os
import re
import sqlite3
import threading
import time
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
import requests

from notifications import DingTalk, message, notification_config

ROOT = Path(__file__).resolve().parent
BASE = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/"
SHANGHAI = timezone(timedelta(hours=8))
SOURCE_NOTIFICATION_FRESH_SECONDS = 10 * 60


def poll_watchdog(timeout):
    """Kill a wedged collector so the service supervisor can restart it."""
    def expired():
        print(f"单轮采集超过{timeout}秒，强制退出并交由 systemd 重启容器", flush=True)
        os._exit(2)
    timer = threading.Timer(timeout, expired)
    timer.daemon = True
    timer.start()
    return timer


def restart_due(last_success_at, started_at, threshold, now=None):
    """Return true after the source has had no successful poll for threshold seconds."""
    now = time.time() if now is None else now
    since = started_at
    if last_success_at:
        try:
            since = datetime.fromisoformat(last_success_at.replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError):
            pass
    return now - since >= threshold


def parse_trades(items, portfolio):
    actions = {("LONG", "BUY"): "OPEN", ("LONG", "SELL"): "CLOSE",
               ("SHORT", "SELL"): "OPEN", ("SHORT", "BUY"): "CLOSE"}
    events = []
    duplicate_counts = {}
    for item in items:
        side = item.get("positionSide")
        operation = actions.get((side, item.get("side")))
        if not operation:
            continue
        qty, price = float(item.get("qty") or 0), float(item.get("price") or 0)
        if not math.isfinite(qty) or not math.isfinite(price) or qty <= 0 or price <= 0:
            raise ValueError("源成交数量/价格无效")
        timestamp = int(item.get("time") or 0)
        if timestamp <= 0:
            raise ValueError("源成交时间无效")
        when = datetime.fromtimestamp(timestamp / 1000, timezone.utc).isoformat()
        symbol = str(item.get("symbol") or "")
        if not re.fullmatch(r"[A-Z0-9_]+USDT", symbol):
            raise ValueError("源合约格式无法识别")
        try:
            realized_profit = float(item.get("realizedProfit") or 0)
        except (TypeError, ValueError):
            raise ValueError("源成交已实现盈亏无效") from None
        if not math.isfinite(realized_profit):
            raise ValueError("源成交已实现盈亏无效")
        # The endpoint normally exposes an immutable trade id. Keep a stable
        # content identity as a fallback for response variants without it.
        native = item.get("tradeId") or item.get("id") or item.get("orderId")
        if native is not None:
            identity = f"{portfolio}|{native}"
        else:
            base_identity = (f"{portfolio}|{when}|{symbol}|{side}|{operation}|"
                             f"{qty:.8f}|{price:.8f}|{realized_profit:.8f}")
            # The public response currently has no trade ID and can contain
            # genuinely repeated fills with identical values. Number repeats
            # so none are silently collapsed by the event primary key.
            occurrence = duplicate_counts.get(base_identity, 0)
            duplicate_counts[base_identity] = occurrence + 1
            identity = f"{base_identity}|{occurrence}"
        events.append({"event_id": hashlib.sha256(identity.encode()).hexdigest(), "portfolio_id": portfolio,
                       "occurred_at": when, "symbol": symbol, "side": side, "operation": operation,
                       "quantity": qty, "price": price, "realized_profit": realized_profit})
    return events


def aggregate_trades(items):
    """Rebuild order-sized signals from the endpoint's individual fills."""
    groups = {}
    order = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("源成交列表格式异常")
        key = (item.get("time"), item.get("symbol"), item.get("positionSide"), item.get("side"))
        try:
            qty = float(item.get("qty") or 0)
            price = float(item.get("price") or 0)
            profit = float(item.get("realizedProfit") or 0)
        except (TypeError, ValueError):
            raise ValueError("源成交数值格式异常") from None
        if key not in groups:
            groups[key] = {"row": dict(item), "qty": 0.0, "quote": 0.0, "profit": 0.0}
            order.append(key)
        group = groups[key]
        group["qty"] += qty
        group["quote"] += qty * price
        group["profit"] += profit
    result = []
    for key in order:
        group = groups[key]
        row = group["row"]
        row["qty"] = group["qty"]
        row["price"] = group["quote"] / group["qty"] if group["qty"] else 0
        row["realizedProfit"] = group["profit"]
        result.append(row)
    return result


# Kept as an import-compatible name for callers of older collector releases.
parse_orders = parse_trades


def request(session, endpoint, payload=None, query=None, retries=8, timeout_seconds=45):
    last = None
    for attempt in range(retries):
        url = BASE + endpoint
        if query:
            url += "?" + urlencode(query)
        try:
            response = (session.post(url, json=payload, timeout=timeout_seconds) if payload is not None
                        else session.get(url, timeout=timeout_seconds))
        except requests.RequestException as exc:
            last = exc
            if attempt + 1 < retries:
                time.sleep(2 + attempt)
                continue
            raise ValueError("公开接口连接失败") from None
        last = response.text
        if response.status_code != 200:
            raise ValueError(f"公开接口 HTTP {response.status_code}，不绕过限制")
        try:
            body = response.json()
        except requests.exceptions.JSONDecodeError:
            raise ValueError("公开接口返回非 JSON 内容") from None
        code = body.get("code")
        if code == "000000":
            return body.get("data")
        # Binance intermittently returns busy on the public order-history API.
        if code == "11012005" and attempt + 1 < retries:
            time.sleep(2 + attempt)
            continue
        raise ValueError(f"公开接口未成功返回，代码 {code}；{str(body.get('message') or '')[:180]}")
    raise ValueError(f"公开接口未成功返回；{str(last)[:180]}")


def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS trader_state(portfolio_id TEXT PRIMARY KEY,data_json TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY,value_json TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS trade_events(event_id TEXT PRIMARY KEY,portfolio_id TEXT,occurred_at TEXT,
          symbol TEXT,side TEXT,operation TEXT,quantity REAL,price REAL,realized_profit REAL);
        CREATE TABLE IF NOT EXISTS collector_notification_queue(
          event_id TEXT PRIMARY KEY,body TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
          next_try REAL NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS trade_events_portfolio_time ON trade_events(portfolio_id,occurred_at,event_id);
        """)
        columns = {row[1] for row in con.execute("PRAGMA table_info(trade_events)")}
        if "realized_profit" not in columns:
            con.execute("ALTER TABLE trade_events ADD COLUMN realized_profit REAL")


def enable_source_notifications(path, portfolio):
    """Enable notifications without replaying a pre-existing trade history."""
    key = "collector_notifications_initialized:" + portfolio
    with sqlite3.connect(path) as con:
        initialized = con.execute("SELECT 1 FROM runtime_state WHERE key=?", (key,)).fetchone()
        existing = con.execute("SELECT 1 FROM trade_events WHERE portfolio_id=? LIMIT 1", (portfolio,)).fetchone()
        if not initialized and existing:
            now = datetime.now(timezone.utc).isoformat()
            con.execute("INSERT INTO runtime_state VALUES(?,?,?)", (key, json.dumps(True), now))


def source_notification_item(event, portfolio):
    kind = "开仓成交" if event["operation"] == "OPEN" else "平仓成交"
    details = {"symbol": event["symbol"], "side": event["side"], "operation": event["operation"],
               "time": datetime.now(timezone.utc).isoformat(), "source_time": event["occurred_at"],
               "source_quantity": event["quantity"], "source_price": event["price"]}
    text = message("collector", kind, details, "采集器发现熬鹰成交", "—", portfolio)
    return {"title": f"CopyCat · 熬鹰{kind}",
            "text": text + f"\n\n---\n> 通知编号　`{event['event_id'][:20]}`",
            # DingTalk ignores this private field. Keeping the source facts in
            # the persistent queue lets delivery merge notifications that
            # arrive late or become stale during webhook retries.
            "source_event": {key: event[key] for key in
                             ("event_id", "occurred_at", "symbol", "side", "operation",
                              "quantity", "price")}}


def _source_event_time(item):
    try:
        value = item["source_event"]["occurred_at"]
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (KeyError, TypeError, ValueError):
        return None


def merged_source_notification(rows, now=None):
    """Build one compact notice for source trades discovered over 10 minutes late."""
    now = time.time() if now is None else now
    events = [item["source_event"] for _, item, _ in rows]
    groups = {}
    for event in events:
        key = (event["symbol"], event["side"], event["operation"])
        group = groups.setdefault(key, {"quantity": 0.0, "quote": 0.0, "count": 0})
        quantity = float(event["quantity"])
        group["quantity"] += quantity
        group["quote"] += quantity * float(event["price"])
        group["count"] += 1
    source_times = sorted(datetime.fromtimestamp(_source_event_time(item), timezone.utc)
                          for _, item, _ in rows)
    notified_at = datetime.fromtimestamp(now, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")
    lines = ["### 🟠 **熬鹰历史成交合并通知**",
             "> **CopyCat · 熬鹰跟单**　|　采集器", "",
             f"- **通知时间**　{notified_at} (UTC+8)",
             f"- **合并成交数**　`{len(events)}`",
             f"- **源成交时间范围**　`{source_times[0].astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')} 至 "
             f"{source_times[-1].astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S')} (UTC+8)`", ""]
    for (symbol, side, operation), group in groups.items():
        marker, direction = {"LONG": ("🟢", "多单"), "SHORT": ("🔴", "空单")}.get(
            side, ("🔵", side))
        action = {"OPEN": "开仓", "CLOSE": "平仓"}.get(operation, operation)
        average = group["quote"] / group["quantity"]
        lines.append(f"- {marker} **{symbol} · {direction} · {action}**："
                     f"`{group['count']} 笔`，合计数量 `{group['quantity']:.8g}`，加权均价 `{average:.8g}`")
    digest = hashlib.sha256("|".join(row[0] for row in rows).encode()).hexdigest()[:20]
    lines.extend(("", "> **说明**　源成交距通知已超过10分钟，采集器已合并通知，避免迟到成交逐条刷屏。",
                  "", "---", f"> 通知编号　`{digest}`"))
    return {"title": f"CopyCat · {len(events)}笔历史成交合并通知", "text": "\n".join(lines)}


def deliver_source_notification(path, notifier, now=None):
    """Send a fresh trade immediately, or merge all due trades older than 10 minutes."""
    if not notifier.enabled:
        return
    now = time.time() if now is None else now
    with sqlite3.connect(path) as con:
        queued = con.execute("SELECT event_id,body,attempts FROM collector_notification_queue "
                             "WHERE next_try<=? ORDER BY rowid", (now,)).fetchall()
    if not queued:
        return
    decoded = [(event_id, json.loads(body), attempts) for event_id, body, attempts in queued]
    fresh = []
    for row in decoded:
        occurred_at = _source_event_time(row[1])
        if occurred_at is None or now - occurred_at <= SOURCE_NOTIFICATION_FRESH_SECONDS:
            fresh.append(row)
    if fresh:
        rows = fresh[:1]
        outbound = rows[0][1]
    else:
        rows = decoded
        outbound = merged_source_notification(rows, now)
    try:
        notifier.send(outbound)
    except Exception:
        delays = []
        with sqlite3.connect(path) as con:
            for event_id, _, attempts in rows:
                attempts += 1
                delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
                delays.append(delay)
                con.execute("UPDATE collector_notification_queue SET attempts=?,next_try=? WHERE event_id=?",
                            (attempts, time.time() + delay, event_id))
        print(f"熬鹰成交通知发送失败，{max(delays)}秒后重试", flush=True)
    else:
        with sqlite3.connect(path) as con:
            con.executemany("DELETE FROM collector_notification_queue WHERE event_id=?",
                            [(event_id,) for event_id, _, _ in rows])


def fetch_detail(session, portfolio):
    detail = request(session, "lead-portfolio/detail", query={"portfolioId": portfolio})
    if not isinstance(detail, dict):
        raise ValueError("带单账户详情格式异常")
    try:
        equity = float(detail.get("marginBalance") or 0)
        aum = float(detail["aumAmount"]) if detail.get("aumAmount") is not None else None
    except (TypeError, ValueError):
        raise ValueError("带单账户金额格式异常") from None
    if not equity or equity <= 0:
        raise ValueError("公开接口未读取到带单余额，请检查地区限制或接口变化")
    return {"name": str(detail.get("nickname") or portfolio), "margin_balance": equity,
            "aum": aum, "captured_at": datetime.now(timezone.utc).isoformat()}


def poll(session, path, portfolio, detail=None, notifier=None):
    with sqlite3.connect(path) as con:
        previous = con.execute("SELECT value_json FROM runtime_state WHERE key=?", ("collector_status:" + portfolio,)).fetchone()
    previous = json.loads(previous[0]) if previous else {}
    detail = detail or fetch_detail(session, portfolio)
    equity = detail["margin_balance"]
    now = datetime.now(SHANGHAI)
    coverage_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    baseline = None
    baseline_path = os.getenv("SOURCE_BASELINE_FILE", "").strip()
    if baseline_path:
        baseline_file = Path(baseline_path)
        if not baseline_file.is_absolute():
            baseline_file = ROOT / baseline_file
        baseline = json.loads(baseline_file.read_text())
        if baseline.get("portfolio_id") != portfolio:
            raise ValueError("源仓位基线的交易员ID不匹配")
        baseline_time = datetime.fromisoformat(baseline["as_of"].replace("Z", "+00:00"))
        if baseline_time.tzinfo is None or baseline_time > now:
            raise ValueError("源仓位基线时间无效")
        if not previous.get("last_success_at"):
            coverage_start = min(coverage_start, baseline_time)
    # After bootstrap, only pull a short overlap window — full-week re-fetch made each cycle too slow.
    # Keep 15 minutes of overlap so delayed public history can still land.
    if previous.get("last_success_at"):
        api_start = datetime.fromisoformat(previous["last_success_at"]) - timedelta(minutes=15)
    else:
        api_start = coverage_start
    end = now
    # This endpoint returns a misleading 11012005 "busy" response when the
    # JSON portfolioId is a string; it must be encoded as a number.
    payload = {"portfolioId": int(portfolio), "pageNumber": 1, "pageSize": 50}
    items = []
    for _ in range(40):
        data = request(session, "lead-portfolio/trade-history", payload)
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("源交易列表格式异常")
        # Trade history is newest first and has no time-range arguments. Retain
        # the overlap window locally and stop as soon as a page crosses it.
        recent = []
        crossed_start = False
        for row in rows:
            try:
                row_time = int(row.get("time") or 0)
            except (AttributeError, TypeError, ValueError):
                row_time = 0
            if row_time >= int(api_start.timestamp() * 1000):
                recent.append(row)
            else:
                crossed_start = True
        items.extend(recent)
        if crossed_start or len(rows) < payload["pageSize"]:
            break
        payload["pageNumber"] += 1
        time.sleep(0.2)
    else:
        raise ValueError("历史超过单次拉取上限，当前窗口不完整；改用原爬虫数据或缩小历史窗口后人工检查")
    events = parse_trades(aggregate_trades(items), portfolio)
    completed = datetime.now(timezone.utc).isoformat()
    profile = dict(detail)
    if baseline:
        profile["position_baseline"] = baseline
    window_start = previous.get("history_window_start") or coverage_start.isoformat()
    with sqlite3.connect(path) as con:
        notification_key = "collector_notifications_initialized:" + portfolio
        notification_ready = con.execute("SELECT 1 FROM runtime_state WHERE key=?",
                                         (notification_key,)).fetchone() is not None
        for e in events:
            old = con.execute("SELECT quantity,price FROM trade_events WHERE event_id=?", (e["event_id"],)).fetchone()
            if old and (old[0] != e["quantity"] or old[1] != e["price"]):
                raise ValueError("已记录订单的累计成交发生变化，请人工核查")
            inserted = con.execute("INSERT OR IGNORE INTO trade_events "
                        "(event_id,portfolio_id,occurred_at,symbol,side,operation,quantity,price,realized_profit) "
                        "VALUES(:event_id,:portfolio_id,:occurred_at,:symbol,:side,:operation,:quantity,:price,:realized_profit)", e)
            if inserted.rowcount and notification_ready and notifier and notifier.enabled:
                con.execute("INSERT OR IGNORE INTO collector_notification_queue(event_id,body) VALUES(?,?)",
                            (e["event_id"], json.dumps(source_notification_item(e, portfolio))))
        con.execute("INSERT OR REPLACE INTO trader_state VALUES(?,?,?)", (portfolio,json.dumps(profile),completed))
        con.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?,?)",
                    ("collector_status:"+portfolio,json.dumps({"last_success_at": completed, "last_error":None,
                     "history_complete": False,
                     "history_window_start": window_start,
                     "history_window_end": end.isoformat()}),completed))
        if not notification_ready:
            con.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?,?)",
                        (notification_key, json.dumps(True), completed))
    print(f"{completed} 熬鹰余额 {equity} USDT，读取 {len(events)} 条历史", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CopyCat public signal collector")
    parser.add_argument("--once", action="store_true", help="只采集一次，便于部署检查")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    path = Path(os.getenv("STANDALONE_SOURCE_DB", str(ROOT / "data" / "source.db")))
    if not path.is_absolute():
        path = ROOT / path
    initialize(path)
    portfolio = os.getenv("PORTFOLIO_ID", "5075281354358777856")
    if not portfolio.isdigit():
        raise SystemExit("交易员ID无效")
    notifier = DingTalk(notification_config())
    enable_source_notifications(path, portfolio)
    collector_started = time.time()
    restart_after = max(60, int(os.getenv("SOURCE_RESTART_AFTER_SECONDS", "60")))
    poll_timeout = max(60, int(os.getenv("SOURCE_POLL_HARD_TIMEOUT_SECONDS", "120")))
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "Content-Type": "application/json",
                            "User-Agent": "CopyCat/1.0"})
    detail = None
    detail_next_at = 0.0
    detail_interval = max(30, int(os.getenv("SOURCE_DETAIL_POLL_SECONDS", "60")))
    while True:
        watchdog = poll_watchdog(poll_timeout)
        next_delay = max(5, int(os.getenv("SOURCE_POLL_SECONDS", "60")))
        try:
            if detail is None or time.monotonic() >= detail_next_at:
                detail = fetch_detail(session, portfolio)
                detail_next_at = time.monotonic() + detail_interval
            poll(session,path,portfolio,detail,notifier)
            deliver_source_notification(path, notifier)
        except Exception as exc:
            updated = datetime.now(timezone.utc).isoformat()
            wait = max(15, int(os.getenv("SOURCE_FAILURE_WAIT_SECONDS", "30")))
            print(f"{updated} 采集失败: {type(exc).__name__}，等待{wait}秒；检查公开接口是否可访问", flush=True)
            with sqlite3.connect(path) as con:
                previous = con.execute("SELECT value_json FROM runtime_state WHERE key=?", ("collector_status:"+portfolio,)).fetchone()
                status = json.loads(previous[0]) if previous else {}
                status["last_error"] = "独立公开接口采集失败"
                con.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?,?)",
                    ("collector_status:"+portfolio,json.dumps(status),updated))
            if args.once:
                print(str(exc)[:300], flush=True)
                raise SystemExit(1)
            if restart_due(status.get("last_success_at"), collector_started, restart_after):
                print(f"连续{restart_after}秒未成功采集，退出并交由 systemd 重启", flush=True)
                os._exit(2)
            next_delay = wait
        finally:
            watchdog.cancel()
        if args.once:
            break
        time.sleep(next_delay)
