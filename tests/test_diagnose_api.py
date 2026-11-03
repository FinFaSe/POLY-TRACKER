import asyncio
import contextlib
import importlib.util
import os
import unittest
from unittest import mock

from src.polymarket_api import PolymarketClientWrapper

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location("diagnose_api", os.path.join(REPO_ROOT, "scripts", "diagnose_api.py"))
diagnose_api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diagnose_api)

WALLET = "0x00000000000000000000000000000000000000aa"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"


def envelope(rows, next_cursor=None, has_more=False):
    return {"data": rows, "pagination": {"next_cursor": next_cursor, "has_more": has_more}}


class FakePolymarket:
    """Simula la Data API v2, el CLOB y el RPC de Polygon."""

    def __init__(self, ignore_cursor=False, onchain_mismatch=False, clob_lagging=False):
        self.ignore_cursor = ignore_cursor
        self.onchain_mismatch = onchain_mismatch
        # El CLOB aún no marca el mercado resuelto como cerrado (pasa en Polymarket real).
        self.clob_lagging = clob_lagging
        now = 1_800_000_000
        self.trades = [
            {"transaction_hash": f"0x{i}", "token_id": "111", "side": "BUY" if i % 2 == 0 else "SELL",
             "timestamp": now - i * 60, "size": 20, "price": 0.5, "usdc_size": 10.0, "type": "TRADE",
             "condition_id": "0xcond", "title": "Mercado", "outcome": "Yes", "proxy_wallet": WALLET}
            for i in range(150)
        ]
        self.positions = [
            {"token_id": "111", "current_size": 20.0, "current_value": 10.0, "condition_id": "0xcond", "redeemable": False},
            {"token_id": "222", "current_size": 7.5, "current_value": 7.5, "condition_id": "0xres", "redeemable": True},
        ]

    async def get_json(self, wrapper, session, url, params):
        if url.endswith("/v2/trades"):
            return envelope(self.trades[:50])
        if url.endswith("/v2/activity"):
            if params.get("type") == "REDEEM":
                return envelope([{"condition_id": "0xres", "type": "REDEEM"}])
            start = 0 if self.ignore_cursor else int(params.get("cursor", 0))
            limit = int(params["limit"])
            more = start + limit < len(self.trades)
            return envelope(self.trades[start:start + limit], str(start + limit) if more else None, more)
        if url.endswith("/v2/positions"):
            return envelope(list(self.positions))
        if url.endswith("/v2/leaderboard"):
            return envelope([{"rank": 1, "user_id": WALLET, "user_name": "Ballena", "pnl": 1000.0, "volume": 5000.0}])
        if url.endswith("/markets/0xcond"):
            return {"closed": False, "tokens": [{"token_id": "111", "winner": False}, {"token_id": "112", "winner": False}]}
        if url.endswith("/markets/0xres"):
            if self.clob_lagging:
                return {"closed": False, "tokens": [{"token_id": "222", "price": 0.9995, "winner": False},
                                                    {"token_id": "223", "price": 0.0005, "winner": False}]}
            return {"closed": True, "tokens": [{"token_id": "222", "winner": True}, {"token_id": "223", "winner": False}]}
        if url.endswith("/book"):
            return {"asks": [{"price": "0.55"}], "bids": [{"price": "0.53"}]}
        return None

    async def post_json(self, wrapper, session, url, payload):
        if payload["method"] == "eth_blockNumber":
            return {"result": hex(80_000_000)}
        call = payload["params"][0]
        data = call["data"]
        if data.startswith("0x70a08231"):  # ERC20 balanceOf
            amount = 9000 if call["to"].lower() == PUSD.lower() else 500
            return {"result": hex(amount * 10 ** 6)}
        if data.startswith("0xdd34de67"):  # CTF payoutDenominator(conditionId)
            # Los condition_id falsos ("0xres", "0xcond") llegan sin traducir al final de 'data'.
            return {"result": hex(1 if data.endswith("res") else 0)}
        if data.startswith("0x0504c814"):  # CTF payoutNumerators(conditionId, index)
            return {"result": hex(1 if int(data[-64:], 16) == 0 else 0)}
        if data.startswith("0x00fdd58e"):  # CTF balanceOf(owner, tokenId)
            token = str(int(data[-64:], 16))
            size = {p["token_id"]: p["current_size"] for p in self.positions}.get(token, 0)
            if self.onchain_mismatch:
                size = size * 2
            return {"result": hex(int(size * 10 ** 6))}
        return None

    async def raw_get(self, session, url, params):
        data = await self.get_json(None, session, url, params)
        return (200, data) if data is not None else (404, None)

    def patch(self):
        fake = self

        async def get_json(wrapper, session, url, params):
            return await fake.get_json(wrapper, session, url, params)

        async def post_json(wrapper, session, url, payload):
            return await fake.post_json(wrapper, session, url, payload)

        async def raw_get(session, url, params):
            return await fake.raw_get(session, url, params)

        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.multiple(PolymarketClientWrapper, _get_json=get_json, _post_json=post_json))
        stack.enter_context(mock.patch.object(diagnose_api, "raw_get", raw_get))
        return stack


def statuses(report):
    return {name: status for name, status, _ in report.rows}


class TestDiagnoseApi(unittest.TestCase):
    def run_diagnosis(self, fake, address=""):
        with fake.patch(), mock.patch("builtins.print"):
            return asyncio.run(diagnose_api.run(address))

    def test_all_checks_pass_against_expected_api(self):
        report = self.run_diagnosis(FakePolymarket())
        self.assertFalse(report.failed, report.rows)
        s = statuses(report)
        for check in [
            "Actividad (/v2/activity)", "Campos de cada operación (v2)", "Orden y formato de timestamp",
            "Paginación con cursor", "get_leader_trades_since (código del bot)",
            "Valor de posiciones (/v2/positions)", "Efectivo en Polygon (pUSD + USDC.e)",
            "Campos de cada posición (v2)", "Shares en la blockchain (CTF)",
            "Mercado (CLOB /markets/{condition_id})", "Libro de órdenes (CLOB /book)",
            "Detección de mercado resuelto (código del bot)", "Resolución en la blockchain (CTF)",
            "Simulación de extremo a extremo", "Ranking (/v2/leaderboard)",
            "Operaciones de un mercado (/v2/trades)",
        ]:
            self.assertEqual(s.get(check), "OK", (check, report.rows))
        self.assertTrue(any(name.startswith("RPC ") and status == "OK" for name, status, _ in report.rows))
        cash = [d for n, _, d in report.rows if n == "Efectivo en Polygon (pUSD + USDC.e)"][0]
        self.assertIn("$9,500.00", cash)
        self.assertIn("|", report.markdown())

    def test_falls_back_to_market_holders_when_trades_endpoint_fails(self):
        fake = FakePolymarket()

        async def raw_get(session, url, params):
            if url.endswith("/v2/trades"):
                return 400, {"error": "bad request"}
            if url.endswith("/markets"):
                return 200, [{"conditionId": "0xcond"}]
            if url.endswith("/v2/holders"):
                assert params.get("condition") == "0xcond"
                return 200, envelope([{"token_id": "111", "holders": [{"proxy_wallet": WALLET, "amount": 10}]}])
            return 404, None

        fake.raw_get = raw_get
        report = self.run_diagnosis(fake)
        s = statuses(report)
        self.assertEqual(s["Operaciones recientes (/v2/trades)"], "WARN")
        self.assertEqual(s["Elegir trader activo"], "INFO")
        self.assertFalse(report.failed, report.rows)

    def test_resolution_found_on_chain_when_clob_lags(self):
        report = self.run_diagnosis(FakePolymarket(clob_lagging=True), address=WALLET)
        row = [r for r in report.rows if r[0] == "Detección de mercado resuelto (código del bot)"][0]
        self.assertEqual(row[1], "OK", row)
        self.assertIn("blockchain", row[2])

    def test_detects_api_ignoring_cursor(self):
        report = self.run_diagnosis(FakePolymarket(ignore_cursor=True), address=WALLET)
        self.assertEqual(statuses(report)["Paginación con cursor"], "FAIL")
        self.assertTrue(report.failed)

    def test_detects_onchain_shares_mismatch(self):
        report = self.run_diagnosis(FakePolymarket(onchain_mismatch=True), address=WALLET)
        self.assertEqual(statuses(report)["Shares en la blockchain (CTF)"], "FAIL")

    def test_reports_when_no_rpc_answers(self):
        fake = FakePolymarket()

        async def dead_rpc(wrapper, session, url, payload):
            raise OSError("sin conexión")
        fake.post_json = dead_rpc
        report = self.run_diagnosis(fake, address=WALLET)
        s = statuses(report)
        self.assertEqual(s["RPCs de Polygon"], "FAIL")
        self.assertEqual(s["Efectivo en Polygon (pUSD + USDC.e)"], "FAIL")


if __name__ == "__main__":
    unittest.main()
