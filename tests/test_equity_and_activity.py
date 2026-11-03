import asyncio
import os
import sqlite3
import tempfile
import time
import unittest

from config import BotConfig
from src.listener import CopyBotMonitor
from src.polymarket_api import PolymarketClientWrapper

LEADER = "0x00000000000000000000000000000000000000aa"


def make_config(tmpdir, **overrides):
    values = dict(
        dry_run=True,
        db_path=os.path.join(tmpdir, "copybot.db"),
        traders_json_path=os.path.join(tmpdir, "no-existe.json"),
        telegram_token="",
        telegram_chat_id="",
        http_proxy_url="",
        private_key="",
        polygon_rpc_url="https://rpc.example",
        max_trade_age_minutes=60,
        activity_page_size=100,
        max_activity_pages=10,
    )
    values.update(overrides)
    return BotConfig(**values)


def trade(ts, side="BUY", token="tok", usdc=100.0, price=0.5, tx=None):
    return {
        "transactionHash": tx or f"0x{ts}{token}{side}", "asset": token, "side": side,
        "timestamp": ts, "size": usdc / price, "price": price, "usdcSize": usdc,
        "title": "Mercado", "outcome": "Yes", "conditionId": "0xcond",
    }


class TestLeaderEquityApi(unittest.TestCase):
    """Punto 4: el patrimonio del líder incluye su efectivo y los fallos no cuentan como cero."""

    def test_cash_balance_is_read_on_chain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir))
            calls = []

            async def fake_post(session, url, payload):
                calls.append((url, payload))
                return {"jsonrpc": "2.0", "id": 1, "result": hex(9000 * 10 ** 6)}
            api._post_json = fake_post

            self.assertEqual(asyncio.run(api.get_leader_cash_balance(None, LEADER)), 9000.0)
            url, payload = calls[0]
            self.assertEqual(url, "https://rpc.example")
            call = payload["params"][0]
            self.assertEqual(call["to"], api.cfg.collateral_token_address)
            self.assertEqual(call["data"], "0x70a08231" + LEADER[2:].rjust(64, "0"))

    def test_cash_balance_failure_returns_none_after_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir))
            calls = []

            async def failing_post(session, url, payload):
                calls.append(1)
                if len(calls) == 1:
                    raise OSError("timeout")
                return {"error": {"code": -32000, "message": "rate limited"}}
            api._post_json = failing_post

            self.assertIsNone(asyncio.run(api.get_leader_cash_balance(None, LEADER)))
            self.assertEqual(len(calls), 2)

    def test_portfolio_value_distinguishes_error_from_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir))
            for response, expected in [(None, None), ([], 0.0), ([{"value": 1000}], 1000.0), ({"value": 5}, 5.0)]:
                async def fake_get(session, url, params, response=response):
                    return response
                api._get_json = fake_get
                self.assertEqual(asyncio.run(api.get_leader_portfolio_value(None, LEADER)), expected)

    def test_empty_rpc_secret_falls_back_to_default(self):
        # GitHub Actions pasa los secrets no configurados como cadena vacía.
        import importlib
        import config as config_module
        from unittest import mock
        with mock.patch.dict(os.environ, {"POLYGON_RPC_URL": ""}):
            reloaded = importlib.reload(config_module)
            self.assertTrue(reloaded.BotConfig().polygon_rpc_url.startswith("https://"))
        importlib.reload(config_module)


class TestLeaderActivityPagination(unittest.TestCase):
    """Punto 6: se leen todas las operaciones nuevas, no solo las 10 últimas."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000
        # 250 operaciones, de la más reciente a la más antigua (como la Data API).
        self.all_trades = [trade(self.now - i) for i in range(250)]

    def tearDown(self):
        self.tmp.cleanup()

    def api(self, **overrides):
        api = PolymarketClientWrapper(make_config(self.tmp.name, **overrides))
        self.requests = []

        async def fake_get(session, url, params):
            self.requests.append(dict(params))
            offset, limit = int(params["offset"]), int(params["limit"])
            return self.all_trades[offset:offset + limit]
        api._get_json = fake_get
        return api

    def test_stops_at_page_that_reaches_last_seen(self):
        trades, truncated = asyncio.run(self.api().get_leader_trades_since(None, LEADER, self.now - 150))
        self.assertFalse(truncated)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(trades), 200)
        self.assertEqual([r["offset"] for r in self.requests], ["0", "100"])

    def test_stops_on_short_page(self):
        trades, truncated = asyncio.run(self.api().get_leader_trades_since(None, LEADER, 0))
        self.assertFalse(truncated)
        self.assertEqual(len(trades), 250)
        self.assertEqual(len(self.requests), 3)

    def test_reports_truncation_at_page_cap(self):
        trades, truncated = asyncio.run(self.api(max_activity_pages=2).get_leader_trades_since(None, LEADER, 0))
        self.assertTrue(truncated)
        self.assertEqual(len(trades), 200)

    def test_missing_page_raises_instead_of_skipping_older_trades(self):
        api = self.api()

        async def flaky_get(session, url, params):
            return self.all_trades[:100] if params["offset"] == "0" else None
        api._get_json = flaky_get
        with self.assertRaises(RuntimeError):
            asyncio.run(api.get_leader_trades_since(None, LEADER, 0))


class TestMonitorWithFakeApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.monitor = CopyBotMonitor(make_config(self.tmp.name))
        self.db = self.monitor.db
        self.db.add_trader(LEADER, "Lider", bank=100.0)
        conn = sqlite3.connect(self.db.db_path)
        conn.execute("UPDATE tracked_traders SET last_seen_timestamp = 0")
        conn.commit()
        conn.close()

        self.book_calls = []
        self.messages = []
        self.positions_value = 1000.0
        self.cash = 9000.0
        self.trades = []
        api = self.monitor.api

        async def book(session, token_id, side):
            self.book_calls.append((token_id, side))
            return 0.5

        async def value(session, address):
            return self.positions_value

        async def cash(session, address):
            return self.cash

        async def trades_since(session, address, since):
            return list(self.trades), False

        async def token_balance(session, address, token_id):
            return 0.0

        api.get_best_orderbook_price = book
        api.get_leader_portfolio_value = value
        api.get_leader_cash_balance = cash
        api.get_leader_trades_since = trades_since
        api.get_leader_token_balance = token_balance

        async def notify(message, level="INFO", send_telegram=True):
            self.messages.append((level, message))
        self.monitor.notifier.notify = notify

    def tearDown(self):
        self.tmp.cleanup()

    def trader(self):
        return self.db.get_active_traders()[0]

    def processed(self):
        conn = sqlite3.connect(self.db.db_path)
        rows = conn.execute("SELECT token_id, side, status, copied_usdc FROM processed_trades ORDER BY rowid").fetchall()
        conn.close()
        return rows

    def test_buy_size_uses_positions_plus_cash(self):
        # El líder tiene 1000 en posiciones y 9000 en efectivo: una compra de 1000 es el 10%
        # de su capital, no el 100% (que el tope dejaría en un 25%).
        now = int(time.time())
        asyncio.run(self.monitor.process_leader_trade(None, self.trader(), trade(now, usdc=1000.0)))
        self.assertEqual(self.processed(), [("tok", "BUY", "SIMULATED", 10.0)])

    def test_buy_skipped_when_equity_unknown(self):
        self.cash = None
        now = int(time.time())
        asyncio.run(self.monitor.process_leader_trade(None, self.trader(), trade(now, usdc=1000.0)))
        self.assertEqual(self.processed(), [("tok", "BUY", "SKIPPED_NO_EQUITY", 0.0)])
        self.assertEqual(self.db.get_trader_positions(LEADER), [])

    def test_scan_processes_more_than_ten_trades_oldest_first(self):
        now = int(time.time())
        self.trades = [trade(now - i, token=f"t{i:02d}", usdc=50.0) for i in range(15)]  # más reciente primero
        asyncio.run(self.monitor.scan_once(None))
        self.assertEqual(len(self.processed()), 15)
        self.assertEqual([t for t, _ in self.book_calls], [f"t{i:02d}" for i in reversed(range(15))])
        self.assertEqual(self.trader()["last_seen_timestamp"], now)

    def test_stale_buys_are_skipped_but_stale_sell_of_held_position_runs(self):
        now = int(time.time())
        self.db.record_buy_execution("prev", LEADER, "held", "M", "Yes", 0, 0.5, 10.0, 0.5, "SIMULATED", "")
        old = now - 3 * 3600
        self.trades = [
            trade(old + 2, token="held", side="SELL"),
            trade(old + 1, token="other", side="SELL"),
            trade(old, token="new"),
        ]
        asyncio.run(self.monitor.scan_once(None))

        statuses = {(t, s): st for t, s, st, _ in self.processed()}
        self.assertEqual(statuses[("new", "BUY")], "SKIPPED_STALE")
        self.assertEqual(statuses[("other", "SELL")], "SKIPPED_STALE")
        self.assertEqual(statuses[("held", "SELL")], "SIMULATED")
        self.assertEqual(self.book_calls, [("held", "SELL")])
        summaries = [m for lvl, m in self.messages if "Se omitieron 2 operaciones" in m]
        self.assertEqual(len(summaries), 1)


if __name__ == "__main__":
    unittest.main()
