"""Optional standalone public-page collector; run separately from app.py."""
import hashlib
import argparse
import json
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
BASE = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/"
SHANGHAI = timezone(timedelta(hours=8))


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


def parse_orders(items, portfolio):
    actions = {("LONG", "BUY"): "OPEN", ("LONG", "SELL"): "CLOSE",
               ("SHORT", "SELL"): "OPEN", ("SHORT", "BUY"): "CLOSE"}
    events = []
    for item in items:
        side = item.get("positionSide")
        operation = actions.get((side, item.get("side")))
        if not operation:
            continue
        if item.get("status") and item["status"] != "FILLED":
            # Keep collecting; publish this immutable order ID only after its
            # final FILLED quantity is available.
            continue
        qty, price = float(item.get("executedQty") or 0), float(item.get("avgPrice") or 0)
        if not math.isfinite(qty) or not math.isfinite(price) or qty <= 0 or price <= 0:
            raise ValueError("源订单成交数量/价格无效")
        when = datetime.fromtimestamp(int(item.get("orderUpdateTime") or item.get("orderTime"))/1000, timezone.utc).isoformat()
        symbol = str(item.get("symbol") or "")
        if not re.fullmatch(r"[A-Z0-9_]+USDT", symbol):
            raise ValueError("源合约格式无法识别")
        # Prefer immutable order ID when exposed; otherwise content identity.
        native = item.get("orderId")
        identity = f"{portfolio}|{native}" if native else f"{portfolio}|{when}|{symbol}|{side}|{operation}|{qty:.8f}|{price:.8f}"
        events.append({"event_id": hashlib.sha256(identity.encode()).hexdigest(), "portfolio_id": portfolio,
                       "occurred_at": when, "symbol": symbol, "side": side, "operation": operation,
                       "quantity": qty, "price": price})
    return events


def request(page, endpoint, payload=None, retries=8):
    last = None
    for attempt in range(retries):
        result = page.evaluate("""async ({url,payload}) => {
          const r = await fetch(url,{method:payload?'POST':'GET',credentials:'same-origin',
            headers:{'Content-Type':'application/json'},...(payload?{body:JSON.stringify(payload)}:{})});
          return {status:r.status, body:await r.text()};
        }""", {"url": BASE + endpoint, "payload": payload})
        last = result
        if result["status"] != 200:
            raise ValueError(f"网页接口 HTTP {result['status']}，暂停5分钟，不绕过限制")
        body = json.loads(result["body"])
        code = body.get("code")
        if code == "000000":
            return body.get("data")
        # Binance intermittently returns busy on the public order-history API.
        if code == "11012005" and attempt + 1 < retries:
            time.sleep(2 + attempt)
            continue
        raise ValueError(f"网页接口未成功返回，代码 {code}；{str(body.get('message') or '')[:180]}")
    raise ValueError(f"网页接口未成功返回；{str(last)[:180]}")


def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS trader_state(portfolio_id TEXT PRIMARY KEY,data_json TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS runtime_state(key TEXT PRIMARY KEY,value_json TEXT,updated_at TEXT);
        CREATE TABLE IF NOT EXISTS trade_events(event_id TEXT PRIMARY KEY,portfolio_id TEXT,occurred_at TEXT,
          symbol TEXT,side TEXT,operation TEXT,quantity REAL,price REAL);
        CREATE INDEX IF NOT EXISTS trade_events_portfolio_time ON trade_events(portfolio_id,occurred_at,event_id);
        """)


def open_lead_page(page, portfolio):
    url = f"https://www.binance.com/zh-TC/copy-trading/lead-details/{portfolio}"
    last = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.locator("h1").first.wait_for(timeout=40000)
            page.wait_for_timeout(1500)
            return
        except Exception as exc:
            last = exc
            if attempt == 0:
                page.wait_for_timeout(2000)
                continue
            raise last


def poll(page, path, portfolio):
    with sqlite3.connect(path) as con:
        previous = con.execute("SELECT value_json FROM runtime_state WHERE key=?", ("collector_status:" + portfolio,)).fetchone()
    previous = json.loads(previous[0]) if previous else {}
    open_lead_page(page, portfolio)
    body = " ".join(page.locator("body").inner_text().split())
    def amount(pattern):
        match = re.search(pattern + r"\s*([\d,]+(?:\.\d+)?)\s*USDT", body)
        return float(match[1].replace(",", "")) if match else None
    equity = amount(r"(?:帶單保證金餘額|带单保证金余额|帶單餘額|带单余额|Leading Margin Balance)")
    if not equity or equity <= 0:
        raise ValueError("页面未读取到带单余额，请检查地区/登录要求/网页变化")
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
    payload = {"portfolioId": portfolio, "startTime": int(api_start.timestamp()*1000),
               "endTime": int(end.timestamp()*1000), "pageSize": 50}
    items = []
    for _ in range(40):
        data = request(page, "lead-portfolio/order-history", payload)
        rows = data.get("list") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("源交易列表格式异常")
        items.extend(rows)
        cursor = data.get("indexValue")
        if not cursor or len(rows) < payload["pageSize"]:
            break
        payload["indexValue"] = str(cursor)
        page.wait_for_timeout(200)
    else:
        raise ValueError("历史超过单次拉取上限，当前窗口不完整；改用原爬虫数据或缩小历史窗口后人工检查")
    events = parse_orders(items, portfolio)
    completed = datetime.now(timezone.utc).isoformat()
    profile = {"name": page.locator("h1").first.inner_text(), "margin_balance": equity,
               "aum": amount(r"(?:資產管理規模|资产管理规模|AUM)"), "captured_at": completed}
    if baseline:
        profile["position_baseline"] = baseline
    window_start = previous.get("history_window_start") or coverage_start.isoformat()
    with sqlite3.connect(path) as con:
        for e in events:
            old = con.execute("SELECT quantity,price FROM trade_events WHERE event_id=?", (e["event_id"],)).fetchone()
            if old and (old[0] != e["quantity"] or old[1] != e["price"]):
                raise ValueError("已记录订单的累计成交发生变化，请人工核查")
            con.execute("INSERT OR IGNORE INTO trade_events VALUES(:event_id,:portfolio_id,:occurred_at,:symbol,:side,:operation,:quantity,:price)", e)
        con.execute("INSERT OR REPLACE INTO trader_state VALUES(?,?,?)", (portfolio,json.dumps(profile),completed))
        con.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?,?)",
                    ("collector_status:"+portfolio,json.dumps({"last_success_at": completed, "last_error":None,
                     "history_complete": False,
                     "history_window_start": window_start,
                     "history_window_end": end.isoformat()}),completed))
    print(f"{completed} 熬鹰余额 {equity} USDT，读取 {len(events)} 条历史", flush=True)


if __name__ == "__main__":
    from playwright.sync_api import sync_playwright
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
    collector_started = time.time()
    restart_after = max(60, int(os.getenv("SOURCE_RESTART_AFTER_SECONDS", "60")))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        while True:
            # Chromium renderers retain a sizeable working set after navigation.
            # Use a fresh context for every poll so cookies, page resources and the
            # renderer process are released instead of accumulating indefinitely.
            context = browser.new_context(locale="zh-TW")
            page = context.new_page()
            try:
                poll(page,path,portfolio)
                if args.once:
                    break
                time.sleep(max(5, int(os.getenv("SOURCE_POLL_SECONDS", "60"))))
            except Exception as exc:
                updated = datetime.now(timezone.utc).isoformat()
                wait = max(15, int(os.getenv("SOURCE_FAILURE_WAIT_SECONDS", "30")))
                print(f"{updated} 采集失败: {type(exc).__name__}，等待{wait}秒；查看原页是否可访问", flush=True)
                with sqlite3.connect(path) as con:
                    previous = con.execute("SELECT value_json FROM runtime_state WHERE key=?", ("collector_status:"+portfolio,)).fetchone()
                    status = json.loads(previous[0]) if previous else {}
                    status["last_error"] = "独立网页采集失败"
                    con.execute("INSERT OR REPLACE INTO runtime_state VALUES(?,?,?)",
                        ("collector_status:"+portfolio,json.dumps(status),updated))
                if args.once:
                    print(str(exc)[:300], flush=True)
                    raise SystemExit(1)
                if restart_due(status.get("last_success_at"), collector_started, restart_after):
                    print(f"连续{restart_after}秒未成功采集，退出并交由 systemd 重启容器", flush=True)
                    # A dead Playwright transport can hang forever in the
                    # context.close() finally block. Exit without running Python
                    # cleanup so Docker terminates and systemd can restart it.
                    os._exit(2)
                time.sleep(wait)
            finally:
                context.close()
