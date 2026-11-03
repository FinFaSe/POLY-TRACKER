import asyncio
import os
import subprocess
import sys
import tempfile
import time
import unittest

from config import BotConfig
from src import follower_analysis
from src.polymarket_api import PolymarketClientWrapper

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEADER = "0x00000000000000000000000000000000000000aa"
FOLLOWER = "0x00000000000000000000000000000000000000f1"
RANDOM = "0x00000000000000000000000000000000000000r0"


def envelope(rows, next_cursor=None, has_more=False):
    return {"data": rows, "pagination": {"next_cursor": next_cursor, "has_more": has_more}}


def trade(wallet, token, cid, side, ts, price, usdc, title="Mercado"):
    return {"proxyWallet": wallet, "asset": token, "conditionId": cid, "side": side, "timestamp": ts,
            "price": price, "usdcSize": usdc, "title": title, "outcome": "Yes"}


class FakeApi:
    _timestamp_seconds = staticmethod(PolymarketClientWrapper._timestamp_seconds)

    def __init__(self, leader_trades, market_trades, truncated=(), high_frequency=()):
        self.leader_trades = leader_trades
        self.market_trades = market_trades
        self.truncated = set(truncated)
        self.high_frequency = set(high_frequency)

    async def count_recent_trades(self, session, address, days=7, max_pages=3):
        return (300, True, None) if address in self.high_frequency else (12, False, None)

    async def get_leader_trades_since(self, session, address, since):
        return [t for t in self.leader_trades if t["timestamp"] >= since][::-1], False

    async def get_market_trades(self, session, condition_id, since, max_pages=10, end_ts=None):
        rows = [t for t in self.market_trades if t["conditionId"] == condition_id and t["timestamp"] >= since]
        if condition_id in self.truncated:
            rows = [t for t in rows if t["timestamp"] > since + 3600]  # faltan los más antiguos
        return sorted(rows, key=lambda t: -t["timestamp"]), condition_id in self.truncated


def scenario_with_followers():
    """El líder compra en 3 mercados; la misma cartera compra 30 s después y el precio sube."""
    now = int(time.time())
    leader, market = [], []
    for i, (token, cid) in enumerate([("t1", "c1"), ("t2", "c2"), ("t3", "c3")]):
        t0 = now - 7200 + i * 600
        buy = trade(LEADER, token, cid, "BUY", t0, 0.50, 100.0)
        leader.append(buy)
        market += [
            buy,
            trade(RANDOM, token, cid, "BUY", t0 - 120, 0.50, 5.0),       # antes: precio estable
            trade(FOLLOWER, token, cid, "BUY", t0 + 30, 0.52, 80.0),     # le sigue
            trade(f"0x{i + 1:040x}", token, cid, "BUY", t0 + 60, 0.53, 10.0),  # otra cartera en cada mercado
        ]
    # Vende t1 más caro mientras el seguidor ya había comprado.
    leader.append(trade(LEADER, "t1", "c1", "SELL", now - 7200 + 300, 0.56, 112.0))
    return leader, market


class TestFollowerAnalysis(unittest.TestCase):
    def run_analysis(self, api, **kwargs):
        return asyncio.run(follower_analysis.analyze_followers(api, None, LEADER, **kwargs))

    def test_detects_recurring_follower_that_pushes_price(self):
        leader, market = scenario_with_followers()
        report = self.run_analysis(FakeApi(leader, market))
        self.assertEqual(report.verdict, "Hay indicios compatibles con que quienes le copian le empujan el precio")
        self.assertEqual(len(report.recurring), 1)
        self.assertTrue(report.recurring[0]["copier"])
        follower = report.recurring[0]
        self.assertEqual((follower["wallet"], follower["trades"], follower["markets"], follower["median_delay"]),
                         (FOLLOWER, 3, 3, 30))
        s = follower_analysis.summary(report.effects)
        self.assertEqual(s["trades"], 4)  # 3 compras + 1 venta
        self.assertGreaterEqual(s["extra_drift"], follower_analysis.PUSH_MIN_EXTRA_DRIFT)
        self.assertGreaterEqual(s["volume_ratio"], follower_analysis.PUSH_MIN_VOLUME_RATIO)
        buys = [e for e in report.effects if e.side == "BUY"]
        self.assertTrue(all(abs(e.drift_after - 0.06) < 1e-9 for e in buys))
        self.assertTrue(all(abs(e.drift_before) < 1e-9 for e in buys))
        self.assertEqual(s["profitable_exits_with_followers"], 1)
        self.assertTrue(any("lo hizo más caro" in d for d in report.details))
        md = follower_analysis.render_markdown(report)
        self.assertIn(f"`{FOLLOWER}`", md)
        self.assertIn("copiador probable", md)
        self.assertIn("**Veredicto: Hay indicios", md)

    def test_high_frequency_bot_is_not_a_copier(self):
        # Como RN1: quien opera tras él es otro bot de alta frecuencia que coincide en los mismos mercados.
        leader, market = scenario_with_followers()
        report = self.run_analysis(FakeApi(leader, market, high_frequency={FOLLOWER}))
        self.assertEqual(report.verdict, "Coincide a menudo con otras carteras, pero no hay copiadores claros")
        self.assertFalse(report.recurring[0]["copier"])
        self.assertIn("bot de alta frecuencia (coincide, no copia)", follower_analysis.render_markdown(report))

    def test_copier_without_extra_volume_does_not_count_as_push(self):
        # Hay copiador, pero antes de cada operación ya había más volumen que después.
        leader, market = scenario_with_followers()
        for cid, token in (("c1", "t1"), ("c2", "t2"), ("c3", "t3")):
            t0 = next(t["timestamp"] for t in leader if t["conditionId"] == cid and t["side"] == "BUY")
            market.append(trade(RANDOM, token, cid, "BUY", t0 - 60, 0.50, 5000.0))
        report = self.run_analysis(FakeApi(leader, market))
        self.assertEqual(report.verdict, "Tiene copiadores probables, pero no se aprecia que le muevan el precio")
        self.assertTrue(any("no hay claramente más volumen" in d for d in report.details))

    def test_no_followers_no_push(self):
        now = int(time.time())
        leader, market = [], []
        for i, (token, cid) in enumerate([("t1", "c1"), ("t2", "c2")]):
            t0 = now - 3600 + i * 600
            buy = trade(LEADER, token, cid, "BUY", t0, 0.50, 100.0)
            leader.append(buy)
            market += [buy, trade(RANDOM, token, cid, "SELL", t0 + 30, 0.50, 20.0)]  # nadie le sigue
        report = self.run_analysis(FakeApi(leader, market))
        self.assertEqual(report.verdict, "No hay indicios de seguidores que le empujen el precio")
        self.assertEqual(report.recurring, [])
        self.assertIn("No se encontraron seguidores recurrentes.", follower_analysis.render_markdown(report))

    def test_incomplete_market_data_is_skipped_not_misread(self):
        leader, market = scenario_with_followers()
        report = self.run_analysis(FakeApi(leader, market, truncated={"c2"}))
        self.assertEqual(report.skipped_incomplete, 1)
        self.assertNotIn("c2", {e.condition_id for e in report.effects})

    def test_only_most_recent_trades_and_period(self):
        leader, market = scenario_with_followers()
        old = trade(LEADER, "t9", "c9", "BUY", int(time.time()) - 30 * 86400, 0.5, 10.0)
        report = self.run_analysis(FakeApi(leader + [old], market), days=7, max_trades=2)
        self.assertEqual(len(report.effects), 2)
        self.assertNotIn("c9", {e.condition_id for e in report.effects})

    def test_no_data(self):
        report = self.run_analysis(FakeApi([], []))
        self.assertEqual(report.verdict, "Sin datos suficientes")

    def test_market_trades_api_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = PolymarketClientWrapper(BotConfig(dry_run=True, db_path=os.path.join(tmp, "x.db")))
            now = int(time.time())
            rows = [{"proxy_wallet": f"0x{i:040x}", "token_id": "t", "side": "BUY", "price": 0.5,
                     "timestamp": now - i * 60} for i in range(250)]
            calls = []

            async def fake_get(session, url, params):
                calls.append(dict(params))
                assert url.endswith("/v2/trades") and params["condition"] == "0xc"
                start = int(params.get("cursor", 0))
                more = start + 100 < len(rows)
                return envelope(rows[start:start + 100], str(start + 100) if more else None, more)
            api._get_json = fake_get

            trades, truncated = asyncio.run(api.get_market_trades(None, "0xc", now - 150 * 60))
            self.assertFalse(truncated)
            self.assertEqual(len(trades), 151)
            self.assertEqual(trades[0]["proxyWallet"], rows[0]["proxy_wallet"])  # campos traducidos
            calls.clear()
            trades, truncated = asyncio.run(api.get_market_trades(None, "0xc", 0, max_pages=2))
            self.assertTrue(truncated)
            self.assertEqual(len(calls), 2)

    def test_market_trades_end_parameter_supported_ignored_or_rejected(self):
        now = int(time.time())
        rows = [{"proxy_wallet": f"0x{i:040x}", "token_id": "t", "side": "BUY", "price": 0.5,
                 "timestamp": now - i * 60} for i in range(300)]

        def make_api(mode):
            api = PolymarketClientWrapper(BotConfig(dry_run=True, db_path=os.path.join(self.tmp, f"{mode}.db")))
            calls = []

            async def fake_get(session, url, params):
                calls.append(dict(params))
                if "end" in params and mode == "rejected":
                    return None  # HTTP 400
                data = [r for r in rows if "end" not in params or mode == "ignored" or r["timestamp"] <= int(params["end"])]
                start = int(params.get("cursor", 0))
                more = start + 100 < len(data)
                return envelope(data[start:start + 100], str(start + 100) if more else None, more)
            api._get_json = fake_get
            return api, calls

        since, end = now - 250 * 60, now - 200 * 60
        expected = [r["timestamp"] for r in rows if since <= r["timestamp"] <= end]
        for mode, pages in (("supported", 1), ("ignored", 3), ("rejected", 3)):
            api, calls = make_api(mode)
            trades, truncated = asyncio.run(api.get_market_trades(None, "0xc", since, max_pages=10, end_ts=end))
            self.assertEqual([t["timestamp"] for t in trades], expected, mode)
            self.assertFalse(truncated, mode)
            data_calls = [c for c in calls if not (mode == "rejected" and "end" in c)]
            self.assertEqual(len(data_calls), pages, mode)
            self.assertEqual(getattr(api, "_trades_end_supported", True), mode == "supported", mode)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_cli_rejects_bad_address(self):
        result = subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "cli.py"), "analizar-seguidores", "--address", "abc"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("formato 0x", result.stderr)


if __name__ == "__main__":
    unittest.main()
