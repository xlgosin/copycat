"""Small USD-M REST client. Never automatically retry an order POST."""
import hashlib
import hmac
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from urllib.parse import urlencode

import requests


def dec(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("数值无效")
    return result


class Binance:
    def __init__(self, mode, key="", secret="", testnet="https://demo-fapi.binance.com", proxies=None):
        self.mode, self.key, self.secret = mode, key, secret
        self.base = testnet if mode == "testnet" else "https://fapi.binance.com"
        self.session = requests.Session()
        if proxies:
            self.session.proxies.update(proxies)
        self.rules = {}
        self.rules_time = 0
        self.cooldown = 0

    def request(self, method, path, params=None, signed=False):
        if time.time() < self.cooldown:
            raise RuntimeError("币安接口冷却中，请稍后重试")
        params = dict(params or {})
        headers = {}
        if signed:
            if not self.key or not self.secret:
                raise RuntimeError("请在 .env 配置 API Key 和 API Secret")
            params.update(timestamp=int(time.time() * 1000), recvWindow=5000)
            query = urlencode(params)
            params["signature"] = hmac.new(self.secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            headers["X-MBX-APIKEY"] = self.key
        try:
            response = self.session.request(method, self.base + path, params=params,
                                            headers=headers, timeout=(5, 12))
        except requests.RequestException:
            # requests exceptions may contain signed URLs: never expose them.
            raise RuntimeError("币安网络请求失败；订单是否成交须查询确认") from None
        if response.status_code in (403, 418, 429):
            try:
                wait = max(300, int(response.headers.get("Retry-After", 300)))
            except ValueError:
                wait = 300
            self.cooldown = time.time() + wait
            raise RuntimeError(f"币安限制请求 HTTP {response.status_code}，已暂停请求")
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError(f"币安响应无效 HTTP {response.status_code}") from None
        if not response.ok or (isinstance(body, dict) and int(body.get("code", 0)) < 0):
            code = body.get("code") if isinstance(body, dict) else "未知"
            raise RuntimeError(f"币安拒绝/未确认请求 HTTP {response.status_code}，代码 {code}")
        return body

    def market(self, symbol):
        if time.time() - self.rules_time > 3600:
            info = self.request("GET", "/fapi/v1/exchangeInfo")
            self.rules = {s["symbol"]: s for s in info["symbols"]}
            self.rules_time = time.time()
        rule = self.rules.get(symbol)
        if not rule or rule["status"] != "TRADING" or rule["quoteAsset"] != "USDT" or rule.get("contractType") != "PERPETUAL":
            raise ValueError("暂不支持此合约：仅支持交易中的 USDT 永续合约")
        price = dec(self.request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol})["markPrice"])
        if price <= 0:
            raise ValueError("币安价格无效")
        return rule, price

    def last_price(self, symbol):
        price = dec(self.request("GET", "/fapi/v1/ticker/price", {"symbol": symbol})["price"])
        if price <= 0:
            raise ValueError("币安价格无效")
        return price

    @staticmethod
    def limit_price(rule, source_price, side):
        rule = next((f for f in rule["filters"] if f["filterType"] == "PRICE_FILTER"), None)
        if not rule or dec(rule["tickSize"]) <= 0:
            raise ValueError("缺少有效限价价格精度，跳过开仓")
        tick = dec(rule["tickSize"])
        price = (source_price / tick).to_integral_value(rounding=ROUND_DOWN if side == "BUY" else ROUND_UP) * tick
        if price <= 0 or price < dec(rule["minPrice"]) or (dec(rule["maxPrice"]) > 0 and price > dec(rule["maxPrice"])):
            raise ValueError("限价不满足币安价格范围，跳过开仓")
        return price

    @staticmethod
    def quantity(rule, requested, price, opening):
        filters = {f["filterType"]: f for f in rule["filters"]}
        # Opens and closes both use market orders; apply market lot constraints too.
        lots = [filters[k] for k in ("LOT_SIZE", "MARKET_LOT_SIZE") if k in filters]
        steps = [dec(f["stepSize"]) for f in lots if dec(f["stepSize"]) > 0]
        step = max(steps)
        quantity = (requested / step).to_integral_value(rounding=ROUND_DOWN) * step
        if quantity <= 0 or any(quantity < dec(f["minQty"]) or quantity > dec(f["maxQty"]) for f in lots):
            raise ValueError("缩放数量不满足币安下单范围，已跳过")
        if any(quantity % s != 0 for s in steps):
            raise ValueError("数量精度不满足合约规则")
        minimum = dec(filters.get("MIN_NOTIONAL", {}).get("notional", 0))
        if opening and quantity * price < minimum:
            raise ValueError(f"缩放金额低于最小下单金额 {minimum} USDT，已跳过")
        return quantity

    def positions(self):
        return self.request("GET", "/fapi/v2/positionRisk", signed=True)

    def account_balances(self):
        account = self.request("GET", "/fapi/v2/account", signed=True)
        return {"available": account.get("availableBalance"),
                "margin_balance": account.get("totalMarginBalance"),
                "wallet_balance": account.get("totalWalletBalance")}

    def hedge_mode(self):
        return bool(self.request("GET", "/fapi/v1/positionSide/dual", signed=True)["dualSidePosition"])

    def set_one_way_mode(self):
        return self.request("POST", "/fapi/v1/positionSide/dual", {"dualSidePosition": "false"}, True)

    def validate_account(self):
        if self.request("GET", "/fapi/v1/multiAssetsMargin", signed=True)["multiAssetsMargin"]:
            raise ValueError("本版要求单资产保证金模式")
        account = self.request("GET", "/fapi/v2/account", signed=True)
        if not account.get("canTrade"):
            raise ValueError("账户未获得合约交易权限")
        return account

    def prepare(self, symbol, leverage):
        # All positions owned by this app must use isolated margin.
        rows = self.positions()
        row = next((p for p in rows if p["symbol"] == symbol and p["positionSide"] == "BOTH"), None)
        if row is None or row.get("marginType") != "isolated":
            self.request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"}, True)
        self.request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": leverage}, True)

    def order(self, pending):
        params = {"symbol": pending["symbol"], "side": pending["order_side"],
                  "type": pending.get("order_type") or "MARKET",
                  "quantity": pending["quantity"], "newClientOrderId": pending["client_id"],
                  "newOrderRespType": "RESULT", "positionSide": "BOTH"}
        if pending["operation"] == "CLOSE":
            params["reduceOnly"] = "true"
        elif params["type"] == "LIMIT":
            params.update(price=pending["limit_price"], timeInForce=pending.get("time_in_force") or "IOC")
        return self.request("POST", "/fapi/v1/order", params, True)

    def query(self, pending):
        return self.request("GET", "/fapi/v1/order", {"symbol": pending["symbol"],
                            "origClientOrderId": pending["client_id"]}, True)
