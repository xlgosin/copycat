import fcntl
import hmac
import logging
import os
import secrets
import threading
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from core import Engine, ROOT
from exchange import dec
from notifications import notification_config


def run_worker(engine, action, interval, trading=False):
    while True:
        try:
            action()
        except Exception:
            logging.exception("CopyCat 后台任务异常退出")
            if trading:
                engine.worker_error = "交易工作线程异常退出，请检查服务日志并重启"
                engine.stop()
            return
        threading.Event().wait(interval)


def settings():
    load_dotenv(ROOT / ".env")
    mode = os.getenv("COPYCAT_MODE", "paper")
    if mode not in ("paper", "testnet", "live"):
        raise ValueError("COPYCAT_MODE 必须为 paper/testnet/live")
    source = Path(os.getenv("SOURCE_DB", "../binance-copy-monitor/data/copy_monitor.db"))
    c = {"mode": mode, "source_db": str(source if source.is_absolute() else ROOT / source),
         "db": str(ROOT / "data" / (mode + ".db")), "portfolio": os.getenv("PORTFOLIO_ID", "5075281354358777856"),
         "key": os.getenv("BINANCE_API_KEY", ""), "secret": os.getenv("BINANCE_API_SECRET", ""),
         "testnet": os.getenv("BINANCE_TESTNET_URL", "https://demo-fapi.binance.com"),
         "capital": dec(os.getenv("COPY_CAPITAL", "100")), "multiplier": dec(os.getenv("COPY_MULTIPLIER", "3")),
         "leverage": int(os.getenv("FUTURES_LEVERAGE", "3")), "max_gross": dec(os.getenv("MAX_GROSS_NOTIONAL", "300")),
         "signal_age": int(os.getenv("MAX_SIGNAL_AGE_SECONDS", "180")),
         "source_age": int(os.getenv("MAX_SOURCE_AGE_SECONDS", "300")),
         "deviation": dec(os.getenv("MAX_PRICE_DEVIATION_PERCENT", "1")),
         "poll": max(5, int(os.getenv("POLL_SECONDS", "5"))),
         "live_enabled": os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true",
         "auto_resume": os.getenv("AUTO_RESUME_ON_SOURCE_RECOVERY", "true").lower() == "true",
         "proxies": {k: v for k, v in {
             "http": os.getenv("HTTP_PROXY", "").strip(),
             "https": os.getenv("HTTPS_PROXY", "").strip(),
         }.items() if v}}
    if c["testnet"] != "https://demo-fapi.binance.com":
        raise ValueError("测试网地址必须为 https://demo-fapi.binance.com，防止密钥发往其他主机")
    if not (0 < c["capital"] <= 100 and 0 < c["multiplier"] <= 3 and 1 <= c["leverage"] <= 3
            and 0 < c["max_gross"] <= c["capital"] * 3 and 0 < c["deviation"] <= 5
            and c["source_age"] > 0 and c["signal_age"] > 0):
        raise ValueError("本版预算最多100U，倍率/杠杆最多3，总敞口最多本金3倍；时效参数须为正")
    c["dingtalk"] = notification_config()
    return c


def create_app(engine, token):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4096

    @app.before_request
    def protect():
        if request.path.startswith("/api/"):
            provided = request.headers.get("Authorization", "")
            if not hmac.compare_digest(provided, "Bearer " + token):
                return jsonify(error="请输入正确的控制口令"), 401
            if request.method == "POST" and not request.is_json:
                return jsonify(error="仅接受 JSON 请求"), 415

    @app.after_request
    def headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/status")
    def status():
        return jsonify(engine.view())

    @app.get("/api/records")
    def records():
        try:
            before = int(request.args["before"]) if "before" in request.args else None
            return jsonify(engine.history(before, int(request.args.get("limit", 100))))
        except (ValueError, OverflowError):
            return jsonify(error="分页参数无效"), 400

    @app.get("/api/position-mode")
    def get_position_mode():
        try:
            hedge = engine.check_position_mode()
            return jsonify(ok=True, hedge_mode=hedge, account=engine.account)
        except Exception as exc:
            return jsonify(error=str(exc)), 400

    @app.post("/api/position-mode")
    def position_mode():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="请求内容必须为 JSON 对象"), 400
        if body.get("mode") != "one_way":
            return jsonify(error="仅支持将持仓模式设为单向 mode=one_way"), 400
        try:
            engine.enable_one_way_mode()
            return jsonify(ok=True, account=engine.account)
        except Exception as exc:
            engine.report_error(str(exc))
            return jsonify(error=str(exc)), 400

    @app.post("/api/start")
    def start():
        body = request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return jsonify(error="请求内容必须为 JSON 对象"), 400
        try:
            engine.start()
            return jsonify(ok=True)
        except Exception as exc:
            engine.report_error(str(exc))
            return jsonify(error=str(exc)), 400

    @app.post("/api/stop")
    def stop():
        engine.stop()
        return jsonify(ok=True)

    @app.post("/api/reconcile")
    def reconcile():
        try:
            engine.resolve()
            return jsonify(ok=True)
        except Exception as exc:
            engine.report_error(str(exc))
            return jsonify(error=str(exc)), 400

    @app.post("/api/close-position")
    def close_position():
        try:
            body = request.get_json(silent=True) or {}
            key = body.get("key")
            if not isinstance(key, str) or ":" not in key:
                raise ValueError("持仓参数无效")
            return jsonify(ok=True, closed=engine.manual_close(key))
        except Exception as exc:
            engine.report_error(str(exc))
            return jsonify(error=str(exc)), 400

    @app.post("/api/close-all")
    def close_all():
        try:
            return jsonify(ok=True, closed=engine.manual_close())
        except Exception as exc:
            engine.report_error(str(exc))
            return jsonify(error=str(exc)), 400

    return app


if __name__ == "__main__":
    c = settings()
    (ROOT / "data").mkdir(exist_ok=True)
    os.chmod(ROOT / "data", 0o700)
    # One executor across all modes: no accidental double orders from two servers.
    lock_file = open(ROOT / "data" / "executor.lock", "a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("CopyCat 已运行，请勿重复启动")
    token = os.getenv("ADMIN_TOKEN", "").strip()
    if not token:
        token_path = ROOT / "data" / "admin-token"
        if not token_path.exists():
            fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as out:
                out.write(secrets.token_hex(24))
        token = token_path.read_text().strip()
    if len(token) < 24:
        raise SystemExit("ADMIN_TOKEN 至少24位")
    engine = Engine(c)
    threading.Thread(target=run_worker, args=(engine, engine.tick, c["poll"], True), daemon=True).start()
    threading.Thread(target=run_worker, args=(engine, engine.deliver_notification, 4), daemon=True).start()
    host, port = os.getenv("HOST", "127.0.0.1"), int(os.getenv("PORT", "8010"))
    print(f"CopyCat: http://{host}:{port} | 模式: {c['mode']} | 默认暂停", flush=True)
    print(f"控制口令: {token}", flush=True)
    create_app(engine, token).run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
