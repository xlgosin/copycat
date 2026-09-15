"""DingTalk transport; delivery is separate from the trading thread."""
import base64
import hashlib
import hmac
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests


UTC8 = timezone(timedelta(hours=8))


def format_time(value):
    """Render source and local timestamps consistently in China Standard Time."""
    if value in (None, ""):
        return "—"
    try:
        if isinstance(value, (int, float)) or str(value).strip().isdigit():
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            parsed = datetime.fromtimestamp(stamp, timezone.utc)
        else:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(UTC8).strftime("%Y-%m-%d %H:%M:%S") + " (UTC+8)"
    except (OSError, OverflowError, TypeError, ValueError):
        return str(value)


def trade_style(event):
    side = event.get("side")
    operation = event.get("operation")
    direction = {"LONG": "多单", "SHORT": "空单"}.get(side, side or "—")
    action = {"OPEN": "开仓", "CLOSE": "平仓"}.get(operation, operation or "—")
    marker = {"LONG": "🟢", "SHORT": "🔴"}.get(side, "🔵")
    return marker, direction, action


def notification_config():
    # app.settings loads CopyCat/.env before calling this function.
    return {"enabled": os.getenv("DINGTALK_ENABLED", "true").lower() == "true",
            "webhook": os.getenv("DINGTALK_WEBHOOK") or "",
            "secret": os.getenv("DINGTALK_SECRET") or ""}


class DingTalk:
    def __init__(self, config):
        self.config = config
        self.webhook = config.get("webhook", "").strip()
        self.secret = config.get("secret", "").strip()

    @property
    def enabled(self):
        return bool(self.config.get("enabled") and self.webhook)

    def signed_url(self):
        parts = urlsplit(self.webhook)
        if parts.scheme != "https" or parts.netloc != "oapi.dingtalk.com" or parts.path != "/robot/send":
            raise ValueError("钉钉Webhook格式无效，仅支持官方群机器人地址")
        query = dict(parse_qsl(parts.query))
        if not query.get("access_token"):
            raise ValueError("钉钉Webhook缺少access_token")
        if self.secret:
            timestamp = str(int(time.time() * 1000))
            sign = base64.b64encode(hmac.new(self.secret.encode(),
                f"{timestamp}\n{self.secret}".encode(), hashlib.sha256).digest()).decode()
            query.update(timestamp=timestamp, sign=sign)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    def send(self, item):
        try:
            r = requests.post(self.signed_url(), json={"msgtype": "markdown", "markdown": {
                              "title": item.get("title") or "CopyCat 交易通知", "text": item["text"]},
                              "at": {"isAtAll": False}}, timeout=(5, 10), allow_redirects=False)
            if r.status_code != 200:
                raise ValueError(f"钉钉响应HTTP {r.status_code}")
            body = r.json()
            if body.get("errcode") != 0:
                raise ValueError(f"钉钉拒绝通知，错误码 {body.get('errcode')}")
        except requests.RequestException:
            raise ValueError("钉钉网络请求失败，通知等待重试") from None


def message(mode, kind, event, note, mode_capital, portfolio=None):
    labels = {"paper":"模拟", "testnet":"测试网", "live":"实盘", "collector":"采集器"}
    marker, direction, action = trade_style(event)
    symbol = event.get("symbol")
    heading = f"{marker} **{symbol}** · {direction} · {kind}" if symbol else f"{marker} CopyCat · {kind}"
    lines = [f"### {heading}",
             f"> **CopyCat · 熬鹰跟单**　|　{labels.get(mode, mode)}",
             "",
             f"- **通知时间**　{format_time(event.get('time') or event.get('occurred_at'))}"]
    if event.get("symbol"):
        lines.append(f"- **交易方向**　{marker} **{direction}**")
    if event.get("operation"):
        lines.append(f"- **交易操作**　{action}")
        if mode != "collector":
            capital = event.get("calculation_capital") or mode_capital
            lines.append(f"- **计算本金**　`{capital} USDT`")
    for key, label in (("quantity","本次成交数量"),("price","成交价格"),("client_id","订单编号"),
                       ("order_type","订单类型"),("limit_price","委托限价"),
                       ("source_time","源成交时间"),("source_price","源成交价格"),
                       ("source_quantity","源成交数量"),("source_close_percent","源平仓占比%"),
                       ("local_close_percent","本地平仓占比%"),
                       ("local_leverage","本地杠杆"),
                       ("exchange_status","交易所状态"),("realized_pnl","本次平仓毛盈亏USDT")):
        if event.get(key) is not None:
            value = format_time(event[key]) if key == "source_time" else event[key]
            lines.append(f"- **{label}**　`{value}`")
    lines.extend(("", f"> **说明**　{note}"))
    body = "\n".join(lines)
    if event.get("operation") == "OPEN" and portfolio:
        portfolio_id = quote(str(portfolio), safe="")
        body += f"\n\n[查看币安交易员页面](https://www.binance.com/en/copy-trading/lead-details/{portfolio_id})"
    return body
