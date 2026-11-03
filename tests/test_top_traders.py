import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from src import top_traders
from src.polymarket_api import PolymarketClientWrapper
from config import BotConfig

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = "0x00000000000000000000000000000000000000aa"
B = "0x00000000000000000000000000000000000000bb"
C = "0x00000000000000000000000000000000000000cc"


def envelope(rows, next_cursor=None, has_more=False):
    return {"data": rows, "pagination": {"next_cursor": next_cursor, "has_more": has_more}}


class FakeApi:
    """Ranking con un trader normal, un bot de alta frecuencia y uno cuya actividad falla."""

    def __init__(self):
        self.leaderboard_params = None

    async def get_leaderboard(self, session, period, order_by, limit):
        self.leaderboard_params = (period, order_by, limit)
        return [
            {"rank": 1, "user_id": A.upper().replace("0X", "0x"), "user_name": "Ballena", "pnl": 125000.5, "volume": 2e6},
            {"rank": 2, "user_id": B, "user_name": "BotRapido", "pnl": 90000, "volume": 9e6},
            {"rank": 3, "user_id": C, "pnl": -500, "volume": 1000},
            {"rank": 4, "user_id": "no-es-una-direccion"},
        ]

    async def count_recent_trades(self, session, address, days=7, max_pages=3):
        if address == A:
            return 12, False, int(time.time()) - 3 * 3600
        if address == B:
            return 300, True, int(time.time()) - 60
        raise RuntimeError("Data API caída")


class TestTopTraders(unittest.TestCase):
    def test_fetch_parses_ranking_marks_followed_and_flags_bots(self):
        api = FakeApi()
        traders = asyncio.run(top_traders.fetch_top_traders(api, None, "semana", "volumen", 10, followed=[A]))
        self.assertEqual(api.leaderboard_params, ("WEEK", "VOL", 10))
        self.assertEqual([t.address for t in traders], [A, B, C])
        normal, bot, unknown = traders
        self.assertTrue(normal.followed and not bot.followed)
        self.assertEqual((normal.trades_7d, normal.high_frequency), (12, False))
        self.assertEqual(bot.activity_label(), "más de 300 ⚠️")
        self.assertEqual(unknown.activity_label(), "?")
        self.assertEqual(unknown.name, C[:10])

    def test_markdown_output(self):
        traders = asyncio.run(top_traders.fetch_top_traders(FakeApi(), None, followed=[A]))
        md = top_traders.render_markdown(traders, "mes", "ganancias")
        self.assertIn("## Mejores traders de Polymarket (último mes, por ganancias)", md)
        self.assertIn(f"| 1 | Ballena | `{A}` | $125,000 | $2,000,000 | 6.3% | 12 | hace 3 h | ✔ ya lo sigues |", md)
        self.assertIn("| 3 |", md)
        self.assertIn("-$500", md)
        self.assertIn("python cli.py add-trader", md)

    def test_margin_labels(self):
        t = top_traders.TraderSummary
        self.assertEqual(t(1, "x", A, pnl=13_410_733, volume=1_470_236_280).margin_label(), "0.9% ↓")  # RN1
        self.assertEqual(t(1, "x", A, pnl=981_366, volume=2_757_365).margin_label(), "35.6%")
        self.assertEqual(t(1, "x", A, pnl=2_335_115, volume=13_209).margin_label(), "más de 100%")
        self.assertEqual(t(1, "x", A, pnl=10, volume=0).margin_label(), "?")

    def test_only_copyable_skips_inactive_and_losing_but_keeps_bots(self):
        class DeepApi:
            def __init__(self):
                self.requested = None

            async def get_leaderboard(self, session, period, order_by, limit):
                self.requested = limit
                # 1 inactivo, 2 bot activo, 3 pierde dinero, 4 activo normal, 5 activo normal
                return [{"rank": i, "user_id": f"0x{i:040x}", "user_name": f"T{i}",
                         "pnl": -10 if i == 3 else 1000, "volume": 5000} for i in range(1, 6)]

            async def count_recent_trades(self, session, address, days=7, max_pages=3):
                n = int(address, 16)
                return {1: (0, False, None), 2: (300, True, int(time.time()))}.get(n, (5, False, int(time.time())))

        api = DeepApi()
        traders = asyncio.run(top_traders.fetch_top_traders(api, None, limit=2, only_copyable=True))
        self.assertEqual(api.requested, top_traders.COPYABLE_SCAN)
        self.assertEqual([t.name for t in traders], ["T2", "T4"])
        self.assertTrue(traders[0].high_frequency)  # el bot se incluye, marcado
        md = top_traders.render_markdown(traders, "mes", "ganancias", only_copyable=True)
        self.assertIn("solo activos y con ganancias", md)

    def test_leaderboard_follows_cursor_until_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = PolymarketClientWrapper(BotConfig(dry_run=True, db_path=os.path.join(tmp, "x.db")))
            calls = []

            async def fake_get(session, url, params):
                calls.append(dict(params))
                start = int(params.get("cursor", 0))
                rows = [{"rank": i, "user_id": f"0x{i:040x}"} for i in range(start, start + 50)]
                return envelope(rows, str(start + 50), True)
            api._get_json = fake_get
            rows = asyncio.run(api.get_leaderboard(None, "MONTH", "PNL", 100))
            self.assertEqual(len(rows), 100)
            self.assertEqual([c.get("cursor") for c in calls], [None, "50"])
            self.assertEqual(calls[0]["limit"], "50")

    def test_skip_activity(self):
        traders = asyncio.run(top_traders.fetch_top_traders(FakeApi(), None, with_activity=False))
        self.assertTrue(all(t.trades_7d is None for t in traders))

    def test_count_recent_trades_pages_until_window_and_reports_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = PolymarketClientWrapper(BotConfig(dry_run=True, db_path=os.path.join(tmp, "x.db")))
            now = int(time.time())

            def pages_for(stamps):
                async def fake_get(session, url, params):
                    start = int(params.get("cursor", 0))
                    rows = [{"timestamp": ts} for ts in stamps[start:start + 100]]
                    more = start + 100 < len(stamps)
                    return envelope(rows, str(start + 100) if more else None, more)
                return fake_get

            # 150 operaciones en los últimos 7 días y 50 más antiguas.
            api._get_json = pages_for([now - i * 60 for i in range(150)] + [now - 8 * 86400 - i for i in range(50)])
            self.assertEqual(asyncio.run(api.count_recent_trades(None, A)), (150, False, now))
            # 400 operaciones recientes: se para en 3 páginas e indica que hay más.
            api._get_json = pages_for([now - i for i in range(400)])
            self.assertEqual(asyncio.run(api.count_recent_trades(None, A)), (300, True, now))

    def test_cli_reports_api_failure_without_touching_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "nunca.db")
            env = dict(os.environ, DB_PATH=db_path, TRADERS_JSON_PATH=os.path.join(tmp, "t.json"),
                       HTTP_PROXY_URL="http://127.0.0.1:9")  # proxy inexistente: la consulta falla rápido
            result = subprocess.run(
                [sys.executable, os.path.join(REPO_ROOT, "cli.py"), "top-traders", "--limit", "3", "--sin-actividad"],
                cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn("No se pudo consultar el ranking", result.stderr)
            self.assertFalse(os.path.exists(db_path))


if __name__ == "__main__":
    unittest.main()
