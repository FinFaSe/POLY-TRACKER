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
        rpc_fallback_urls=(),
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


def v2_trade(ts, side="BUY", token="tok", usdc=100.0, price=0.5):
    """Fila de /v2/activity, con los nombres en snake_case de la Data API v2."""
    return {
        "transaction_hash": f"0x{ts}{token}{side}", "token_id": token, "side": side, "type": "TRADE",
        "timestamp": ts, "size": usdc / price, "price": price, "usdc_size": usdc,
        "title": "Mercado", "outcome": "Yes", "condition_id": "0xcond", "proxy_wallet": LEADER,
    }


def envelope(rows, next_cursor=None, has_more=False):
    return {"data": rows, "pagination": {"next_cursor": next_cursor, "has_more": has_more}}


class TestLeaderEquityApi(unittest.TestCase):
    """Punto 4: el patrimonio del líder incluye su efectivo y los fallos no cuentan como cero."""

    def test_cash_balance_is_read_on_chain(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir))
            calls = []

            balances = {api.cfg.collateral_token_address: 9000, api.cfg.legacy_collateral_address: 500}

            async def fake_post(session, url, payload):
                calls.append((url, payload))
                token = payload["params"][0]["to"]
                return {"jsonrpc": "2.0", "id": 1, "result": hex(balances[token] * 10 ** 6)}
            api._post_json = fake_post

            # Líder: pUSD + USDC.e sin convertir. Cartera propia (para comprar): solo pUSD.
            self.assertEqual(asyncio.run(api.get_leader_cash_balance(None, LEADER)), 9500.0)
            self.assertEqual(asyncio.run(api.get_cash_balance(None, LEADER)), 9000.0)
            url, payload = calls[0]
            self.assertEqual(url, "https://rpc.example")
            call = payload["params"][0]
            self.assertEqual(call["to"], "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB")  # pUSD
            self.assertEqual(call["data"], "0x70a08231" + LEADER[2:].rjust(64, "0"))

    def test_rpc_falls_back_to_next_url_and_remembers_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir, rpc_fallback_urls=("https://b.example", "https://c.example")))
            calls = []

            async def fake_post(session, url, payload):
                calls.append(url)
                if url == "https://c.example":
                    return {"result": hex(7 * 10 ** 6)}
                raise OSError("caído")
            api._post_json = fake_post

            self.assertEqual(asyncio.run(api.get_cash_balance(None, LEADER)), 7.0)
            self.assertEqual(calls, ["https://rpc.example", "https://b.example", "https://c.example"])
            calls.clear()
            self.assertEqual(asyncio.run(api.get_cash_balance(None, LEADER)), 7.0)
            self.assertEqual(calls, ["https://c.example"])

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
            pages = {
                None: envelope([{"token_id": "a", "current_value": 600.0}], next_cursor="c2", has_more=True),
                "c2": envelope([{"token_id": "b", "current_value": 400.0}]),
            }
            cases = [
                (lambda params: None, None),                                   # la API falla
                (lambda params: {"error": "Data API v1 has been retired"}, None),  # formato inesperado
                (lambda params: envelope([]), 0.0),                            # sin posiciones
                (lambda params: pages[params.get("cursor")], 1000.0),          # dos páginas
            ]
            for responder, expected in cases:
                async def fake_get(session, url, params, responder=responder):
                    self.assertTrue(url.endswith("/v2/positions"))
                    return responder(params)
                api._get_json = fake_get
                self.assertEqual(asyncio.run(api.get_leader_portfolio_value(None, LEADER)), expected)

    def test_leader_token_balance_onchain_then_api_then_unknown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir))
            state = {"onchain": 12.0, "positions": envelope([{"token_id": "777", "current_size": 3.5}])}

            async def onchain(session, address, token_id):
                return state["onchain"]

            async def fake_get(session, url, params):
                return state["positions"]
            api.get_onchain_shares = onchain
            api._get_json = fake_get

            self.assertEqual(asyncio.run(api.get_leader_token_balance(None, LEADER, "777")), 12.0)
            state["onchain"] = None
            self.assertEqual(asyncio.run(api.get_leader_token_balance(None, LEADER, "777")), 3.5)
            self.assertEqual(asyncio.run(api.get_leader_token_balance(None, LEADER, "888")), 0.0)
            state["positions"] = None
            self.assertIsNone(asyncio.run(api.get_leader_token_balance(None, LEADER, "777")))

    def test_v2_activity_rows_are_normalized_and_iso_timestamps_parsed(self):
        row = PolymarketClientWrapper.normalize_activity(v2_trade(1_800_000_000))
        self.assertEqual((row["transactionHash"], row["asset"], row["conditionId"], row["usdcSize"]),
                         ("0x1800000000tokBUY", "tok", "0xcond", 100.0))
        self.assertEqual(PolymarketClientWrapper._timestamp_seconds("2027-01-15T08:00:00Z"), 1_800_000_000)
        self.assertEqual(PolymarketClientWrapper._timestamp_seconds("1970-01-01T00:01:40Z"), 100)
        self.assertEqual(PolymarketClientWrapper._timestamp_seconds(1_800_000_000_000), 1_800_000_000)

    def test_empty_rpc_secret_falls_back_to_default(self):
        # GitHub Actions pasa los secrets no configurados como cadena vacía.
        import importlib
        import config as config_module
        from unittest import mock
        with mock.patch.dict(os.environ, {"POLYGON_RPC_URL": ""}):
            reloaded = importlib.reload(config_module)
            cfg = reloaded.BotConfig()
            self.assertEqual(cfg.polygon_rpc_url, "")
            self.assertTrue(all(u.startswith("https://") for u in cfg.rpc_fallback_urls) and cfg.rpc_fallback_urls)
        importlib.reload(config_module)


class TestLeaderActivityPagination(unittest.TestCase):
    """Punto 6: se leen todas las operaciones nuevas, no solo las 10 últimas."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1_800_000_000
        # 250 operaciones, de la más reciente a la más antigua (como la Data API).
        self.all_trades = [v2_trade(self.now - i) for i in range(250)]

    def tearDown(self):
        self.tmp.cleanup()

    def api(self, **overrides):
        api = PolymarketClientWrapper(make_config(self.tmp.name, **overrides))
        self.requests = []

        async def fake_get(session, url, params):
            # Simula /v2/activity: el cursor es la posición de la siguiente página.
            assert url.endswith("/v2/activity") and "offset" not in params
            self.requests.append(dict(params))
            start, limit = int(params.get("cursor", 0)), int(params["limit"])
            rows = self.all_trades[start:start + limit]
            more = start + limit < len(self.all_trades)
            return envelope(rows, next_cursor=str(start + limit) if more else None, has_more=more)
        api._get_json = fake_get
        return api

    def test_stops_at_page_that_reaches_last_seen(self):
        trades, truncated = asyncio.run(self.api().get_leader_trades_since(None, LEADER, self.now - 150))
        self.assertFalse(truncated)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(len(trades), 200)
        self.assertEqual([r.get("cursor") for r in self.requests], [None, "100"])
        self.assertEqual(trades[0]["asset"], "tok")  # filas traducidas a los nombres del bot

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
            if "cursor" not in params:
                return envelope(self.all_trades[:100], next_cursor="100", has_more=True)
            return None
        api._get_json = flaky_get
        with self.assertRaises(RuntimeError):
            asyncio.run(api.get_leader_trades_since(None, LEADER, 0))

    def test_repeated_cursor_raises_instead_of_looping(self):
        api = self.api()

        async def stuck_get(session, url, params):
            return envelope(self.all_trades[:100], next_cursor="mismo", has_more=True)
        api._get_json = stuck_get
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

        self.telegram = []

        async def notify(message, level="INFO", send_telegram=True):
            self.messages.append((level, message))
            if send_telegram:
                self.telegram.append((level, message))
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

    def test_leader_equity_is_reused_within_cache_window(self):
        calls = []
        original = self.monitor.api.get_leader_portfolio_value

        async def counting_value(session, address):
            calls.append(address)
            return await original(session, address)
        self.monitor.api.get_leader_portfolio_value = counting_value
        now = int(time.time())
        for i in range(5):
            asyncio.run(self.monitor.process_leader_trade(None, self.trader(), trade(now + i, token=f"t{i}", usdc=50.0)))
        self.assertEqual(len(calls), 1)
        self.monitor._equity_cache[LEADER] = (time.time() - 3600, 1.0)  # caducado
        asyncio.run(self.monitor.process_leader_trade(None, self.trader(), trade(now + 10, token="t9", usdc=50.0)))
        self.assertEqual(len(calls), 2)

    def test_buy_skipped_when_equity_unknown(self):
        self.cash = None
        now = int(time.time())
        asyncio.run(self.monitor.process_leader_trade(None, self.trader(), trade(now, usdc=1000.0)))
        self.assertEqual(self.processed(), [("tok", "BUY", "SKIPPED_NO_EQUITY", 0.0)])
        self.assertEqual(self.db.get_trader_positions(LEADER), [])

    def test_skipped_trades_send_one_telegram_summary(self):
        # 30 compras de $1 de un líder con $10.000: cada copia sería de $0,01, por debajo del mínimo.
        now = int(time.time())
        self.trades = [trade(now - i, token=f"t{i}", usdc=1.0) for i in range(30)]
        asyncio.run(self.monitor.scan_once(None))
        self.assertEqual(len([m for m in self.messages if m[1].startswith("Compra omitida")]), 30)  # en consola
        self.assertEqual(len(self.telegram), 1)
        level, text = self.telegram[0]
        self.assertEqual(level, "WARNING")
        self.assertEqual(text, "Lider: 30 operaciones nuevas, 0 copiadas. "
                               "No copiadas: 30 por tamaño (menor que el mínimo o sin posición que vender).")

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
        summaries = [m for lvl, m in self.messages if m.startswith("Lider: 3 operaciones nuevas")]
        self.assertEqual(len(summaries), 1)
        self.assertIn("1 copiadas", summaries[0])
        self.assertIn("2 por antigüedad (más de 60 min)", summaries[0])


if __name__ == "__main__":
    unittest.main()
