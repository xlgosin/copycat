"""Read-only AlphaFox latency probe.

The probe never logs the session cookie and never talks to CopyCat's trading
engine.  Successful API snapshots are stored in a separate SQLite database so
their first-observed time can later be compared with the source event time.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import requests
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parent
BASE = "https://www.alphafox.app"
DEFAULT_TRADER_ID = "01a00aa6-d208-71ee-8e88-0c78972b1886"
ENDPOINTS = ("activity?limit=100", "orders?limit=100", "positions", "signal-source-positions")


def stamp():
    return datetime.now(timezone.utc).isoformat()


class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.description = ""
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag == "meta" and values.get("name") == "description":
            self.description = values.get("content", "")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def initialize(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with sqlite3.connect(path) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS probe_runs(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          observed_at TEXT NOT NULL,
          endpoint TEXT NOT NULL,
          http_status INTEGER,
          elapsed_ms INTEGER NOT NULL,
          error TEXT,
          payload_sha256 TEXT
        );
        CREATE TABLE IF NOT EXISTS snapshots(
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          trader_id TEXT NOT NULL,
          endpoint TEXT NOT NULL,
          observed_at TEXT NOT NULL,
          payload_sha256 TEXT NOT NULL,
          body TEXT NOT NULL,
          UNIQUE(trader_id, endpoint, payload_sha256)
        );
        """)
    os.chmod(path, 0o600)


def save_result(path, trader_id, endpoint, observed_at, status, elapsed_ms, body=None, error=None):
    serialized = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if body is not None else None
    digest = hashlib.sha256(serialized.encode()).hexdigest() if serialized is not None else None
    with sqlite3.connect(path) as con:
        con.execute("INSERT INTO probe_runs(observed_at,endpoint,http_status,elapsed_ms,error,payload_sha256) VALUES(?,?,?,?,?,?)",
                    (observed_at, endpoint, status, elapsed_ms, error, digest))
        if serialized is not None:
            con.execute("INSERT OR IGNORE INTO snapshots(trader_id,endpoint,observed_at,payload_sha256,body) VALUES(?,?,?,?,?)",
                        (trader_id, endpoint, observed_at, digest, serialized))
            changed = con.execute("SELECT changes()").fetchone()[0] == 1
        else:
            changed = False
    return changed


def request_json(session, url, timeout=20):
    started = time.monotonic()
    try:
        response = session.get(url, timeout=timeout, allow_redirects=False)
        elapsed = round((time.monotonic() - started) * 1000)
        try:
            body = response.json()
        except ValueError:
            body = None
        error = None if response.ok else ((body or {}).get("message") if isinstance(body, dict) else f"HTTP {response.status_code}")
        return response.status_code, elapsed, body, error
    except requests.RequestException:
        return None, round((time.monotonic() - started) * 1000), None, "network request failed"


def probe_once(session, trader_id, db):
    page_url = f"{BASE}/zh/dashboard/leaderboard/{trader_id}"
    started = time.monotonic()
    try:
        page = session.get(page_url, timeout=20, allow_redirects=False)
        elapsed = round((time.monotonic() - started) * 1000)
        parser = MetadataParser()
        parser.feed(page.text if page.ok else "")
        print(f"public  HTTP {page.status_code}  {elapsed}ms  {parser.title or 'no title'}")
        if parser.description:
            print(f"        {parser.description}")
    except requests.RequestException:
        print("public  network request failed")

    authenticated = False
    for endpoint in ENDPOINTS:
        observed_at = stamp()
        url = f"{BASE}/api/trading/traders/{trader_id}/{endpoint}"
        status, elapsed, body, error = request_json(session, url)
        changed = save_result(db, trader_id, endpoint, observed_at, status, elapsed, body if status == 200 else None, error)
        label = str(status) if status is not None else "network"
        suffix = " NEW SNAPSHOT" if changed else ""
        print(f"{endpoint:<32} HTTP {label:<7} {elapsed:>5}ms{suffix}{'  ' + error if error else ''}")
        authenticated = authenticated or status == 200
    return authenticated


def main():
    parser = argparse.ArgumentParser(description="Probe AlphaFox public/private read-only trader data latency")
    parser.add_argument("--trader-id", default=DEFAULT_TRADER_ID)
    parser.add_argument("--db", default=str(ROOT / "data" / "alphafox-probe.db"))
    parser.add_argument("--interval", type=int, default=5)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if not args.trader_id or "/" in args.trader_id:
        raise SystemExit("invalid trader id")
    if args.interval < 2:
        raise SystemExit("interval must be at least 2 seconds")

    load_dotenv(ROOT / ".env")
    db = Path(args.db).resolve()
    initialize(db)
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "CopyCat-AlphaFox-Probe/1.0"})
    cookie = os.getenv("ALPHAFOX_COOKIE", "").strip()
    if cookie:
        session.headers["Cookie"] = cookie
    else:
        print("ALPHAFOX_COOKIE is not set; private endpoints are expected to return HTTP 401.")

    while True:
        authenticated = probe_once(session, args.trader_id, db)
        if args.once:
            break
        if not authenticated:
            raise SystemExit("Private data requires ALPHAFOX_COOKIE; stopping instead of repeatedly polling HTTP 401.")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
