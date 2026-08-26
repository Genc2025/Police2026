from __future__ import annotations

import argparse
import json
import mimetypes
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
DASHBOARD_DIR = ROOT / "dashboard"
CONFIG_PATH = ROOT / "config.example.json"


def load_config() -> dict:
    user_config = ROOT / "config.json"
    selected = user_config if user_config.exists() else CONFIG_PATH
    return json.loads(selected.read_text(encoding="utf-8"))


def db_path() -> Path:
    config = load_config()
    return ROOT / str(config.get("sqlite_path", "paper_arbitrage_usdt.db"))


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    ).fetchone() is not None


def rows_as_dicts(connection: sqlite3.Connection, sql: str, params=()) -> list[dict]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def scalar(connection: sqlite3.Connection, sql: str, params=(), default=0):
    row = connection.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return default
    return row[0]


def api_summary() -> dict:
    config = load_config()
    asset = str(config.get("capital_asset", "USDT")).upper()
    starting = float(config.get("virtual_capital", 100.0))
    path = db_path()
    result = {
        "mode": "PAPER",
        "capital_asset": asset,
        "starting_capital": starting,
        "scanner_db": path.name,
        "database_ready": path.exists(),
        "total_scans": 0,
        "total_candidates": 0,
        "paper_trades": 0,
        "wins": 0,
        "losses": 0,
        "net_pnl": 0.0,
        "paper_balance": starting,
        "best_trade": 0.0,
        "worst_trade": 0.0,
        "latest_observed_at": None,
    }
    if not path.exists():
        return result

    connection = sqlite3.connect(path)
    try:
        if table_exists(connection, "quotes"):
            result["total_scans"] = int(scalar(connection, "SELECT COUNT(DISTINCT scan_id) FROM quotes"))
        if table_exists(connection, "opportunities"):
            result["total_candidates"] = int(scalar(connection, "SELECT COUNT(*) FROM opportunities"))
            result["paper_trades"] = int(scalar(connection, "SELECT COUNT(*) FROM opportunities WHERE qualifies=1"))
            result["wins"] = int(scalar(connection, "SELECT COUNT(*) FROM opportunities WHERE qualifies=1 AND net_profit_quote > 0"))
            result["losses"] = int(scalar(connection, "SELECT COUNT(*) FROM opportunities WHERE qualifies=1 AND net_profit_quote < 0"))
            result["net_pnl"] = float(scalar(connection, "SELECT COALESCE(SUM(net_profit_quote),0) FROM opportunities WHERE qualifies=1", default=0.0))
            result["best_trade"] = float(scalar(connection, "SELECT COALESCE(MAX(net_profit_quote),0) FROM opportunities WHERE qualifies=1", default=0.0))
            result["worst_trade"] = float(scalar(connection, "SELECT COALESCE(MIN(net_profit_quote),0) FROM opportunities WHERE qualifies=1", default=0.0))
            result["latest_observed_at"] = scalar(connection, "SELECT MAX(observed_at) FROM opportunities", default=None)
            result["paper_balance"] = starting + result["net_pnl"]
    finally:
        connection.close()
    return result


def api_opportunities(limit: int) -> list[dict]:
    path = db_path()
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    try:
        if not table_exists(connection, "opportunities"):
            return []
        return rows_as_dicts(
            connection,
            """
            SELECT id, scan_id, pair, quote_asset, buy_exchange, buy_ask,
                   sell_exchange, sell_bid, capital_quote, base_qty,
                   gross_spread_pct, estimated_cost_pct, net_profit_pct,
                   net_profit_quote, observed_at, qualifies,
                   max_leg_latency_ms, snapshot_skew_ms
            FROM opportunities
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
    finally:
        connection.close()


def api_trades(limit: int) -> list[dict]:
    rows = api_opportunities(max(limit * 4, 200))
    trades = []
    for row in rows:
        if int(row.get("qualifies", 0)) != 1:
            continue
        trades.append({
            **row,
            "mode": "PAPER",
            "status": "PAPER_FILLED",
        })
        if len(trades) >= limit:
            break
    return trades


def api_market(limit: int) -> list[dict]:
    path = db_path()
    if not path.exists():
        return []
    connection = sqlite3.connect(path)
    try:
        if not table_exists(connection, "quotes"):
            return []
        return rows_as_dicts(
            connection,
            """
            SELECT q.exchange, q.pair, q.bid, q.bid_qty, q.ask, q.ask_qty,
                   q.observed_at, q.latency_ms
            FROM quotes q
            JOIN (
                SELECT exchange, pair, MAX(id) AS max_id
                FROM quotes
                GROUP BY exchange, pair
            ) latest ON latest.max_id = q.id
            ORDER BY q.pair, q.exchange
            LIMIT ?
            """,
            (limit,),
        )
    finally:
        connection.close()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "ArbitrageDashboard/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"dashboard: {fmt % args}")

    def send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_static(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self.send_error(404)
            return
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if content_type.startswith("text/") else content_type)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        try:
            limit = max(1, min(int(params.get("limit", [100])[0]), 500))
        except ValueError:
            limit = 100

        if parsed.path == "/api/summary":
            self.send_json(api_summary())
            return
        if parsed.path == "/api/opportunities":
            self.send_json(api_opportunities(limit))
            return
        if parsed.path == "/api/trades":
            self.send_json(api_trades(limit))
            return
        if parsed.path == "/api/market":
            self.send_json(api_market(limit))
            return
        if parsed.path in {"/", "/index.html"}:
            self.send_static(DASHBOARD_DIR / "index.html")
            return

        relative = parsed.path.lstrip("/")
        candidate = (DASHBOARD_DIR / relative).resolve()
        if DASHBOARD_DIR.resolve() not in candidate.parents:
            self.send_error(403)
            return
        self.send_static(candidate)


def main() -> int:
    parser = argparse.ArgumentParser(description="Serve the crypto arbitrage paper dashboard")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    print(f"Crypto Arbitrage Dashboard: http://{args.host}:{args.port}")
    print(f"Database: {db_path()}")
    print("Mode: PAPER (no real orders)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
