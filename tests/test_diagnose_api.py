import asyncio
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


class FakePolymarket:
    """Simula las respuestas de la Data API, el CLOB y el RPC de Polygon."""

    def __init__(self, ignore_offset=False, ignore_asset_filter=False):
        self.ignore_offset = ignore_offset
        self.ignore_asset_filter = ignore_asset_filter
        now = 1_800_000_000
        self.trades = [
            {"transactionHash": f"0x{i}", "asset": "tok", "side": "BUY" if i % 2 == 0 else "SELL",
             "timestamp": now - i * 60, "size": 20, "price": 0.5, "usdcSize": 10.0,
             "conditionId": "0xcond", "title": "Mercado", "outcome": "Yes", "proxyWallet": WALLET}
            for i in range(150)
        ]
        self.positions = [
            {"asset": "tok", "size": 20.0, "conditionId": "0xcond"},
            {"asset": "tok2", "size": 7.5, "conditionId": "0xother"},
        ]

    async def get_json(self, wrapper, session, url, params):
        if url.endswith("/trades"):
            return self.trades[:50]
        if url.endswith("/activity"):
            if params.get("type") == "REDEEM":
                return [{"conditionId": "0xres"}]
            offset = 0 if self.ignore_offset else int(params["offset"])
            return self.trades[offset:offset + int(params["limit"])]
        if url.endswith("/value"):
            return [{"user": WALLET, "value": 1000.0}]
        if url.endswith("/positions"):
            if "asset" in params and not self.ignore_asset_filter:
                return [p for p in self.positions if p["asset"] == params["asset"]]
            return list(self.positions)
        if url.endswith("/markets/0xcond"):
            return {"closed": False, "tokens": [{"token_id": "tok", "winner": False}, {"token_id": "tok-no", "winner": False}]}
        if url.endswith("/markets/0xres"):
            return {"closed": True, "tokens": [{"token_id": "a", "winner": True}, {"token_id": "b", "winner": False}]}
        if url.endswith("/book"):
            return {"asks": [{"price": "0.55"}], "bids": [{"price": "0.53"}]}
        return None

    async def post_json(self, wrapper, session, url, payload):
        token = payload["params"][0]["to"]
        amount = 9000 if token == wrapper.cfg.collateral_token_address else 0
        return {"result": hex(amount * 10 ** 6)}

    def patch(self):
        fake = self

        async def get_json(wrapper, session, url, params):
            return await fake.get_json(wrapper, session, url, params)

        async def post_json(wrapper, session, url, payload):
            return await fake.post_json(wrapper, session, url, payload)

        return mock.patch.multiple(PolymarketClientWrapper, _get_json=get_json, _post_json=post_json)


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
            "Campos de cada operación", "Paginación con offset", "get_leader_trades_since (código del bot)",
            "Valor de posiciones (/value)", "Efectivo USDC.e en Polygon",
            "Filtro de posiciones por token (ventas proporcionales)", "Mercado (/markets/{condition_id})",
            "Libro de órdenes (/book)", "Detección de mercado resuelto (código del bot)",
        ]:
            self.assertEqual(s.get(check), "OK", (check, report.rows))
        self.assertIn("|", report.markdown())

    def test_detects_api_ignoring_offset(self):
        report = self.run_diagnosis(FakePolymarket(ignore_offset=True), address=WALLET)
        self.assertEqual(statuses(report)["Paginación con offset"], "FAIL")
        self.assertTrue(report.failed)

    def test_detects_positions_asset_filter_ignored(self):
        report = self.run_diagnosis(FakePolymarket(ignore_asset_filter=True), address=WALLET)
        self.assertEqual(statuses(report)["Filtro de posiciones por token (ventas proporcionales)"], "FAIL")


if __name__ == "__main__":
    unittest.main()
