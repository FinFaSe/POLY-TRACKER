import asyncio
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

from config import BotConfig
from src import notifier as notifier_module
from src.database import DatabaseManager
from src.listener import CopyBotMonitor
from src.notifier import Notifier
from src.polymarket_api import PolymarketClientWrapper

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_config(tmpdir, **overrides):
    values = dict(
        dry_run=True,
        db_path=os.path.join(tmpdir, "copybot.db"),
        traders_json_path=os.path.join(tmpdir, "no-existe.json"),
        telegram_token="",
        telegram_chat_id="",
        http_proxy_url="",
        private_key="",
    )
    values.update(overrides)
    return BotConfig(**values)


class TestOrderProxy(unittest.TestCase):
    """Punto 1: las órdenes reales deben salir por HTTP_PROXY_URL."""

    def test_clob_http_client_is_replaced_with_proxied_client(self):
        from py_clob_client.http_helpers import helpers

        original = helpers._http_client
        created = []

        class FakeClient:
            def __init__(self, **kwargs):
                created.append(kwargs)

            def close(self):
                pass

        try:
            with mock.patch("httpx.Client", FakeClient):
                PolymarketClientWrapper.configure_clob_http_proxy("http://user:pass@proxy.example:8080")
            self.assertIsInstance(helpers._http_client, FakeClient)
            self.assertEqual(created, [{"http2": True, "proxy": "http://user:pass@proxy.example:8080"}])
        finally:
            helpers._http_client = original

    def test_live_mode_on_github_actions_without_proxy_refuses_to_trade(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = make_config(tmpdir, dry_run=False, private_key="0x" + "1" * 64)
            with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
                api = PolymarketClientWrapper(cfg)
            self.assertIsNone(api.clob_client)
            result = asyncio.run(api.execute_order("token", "BUY", 0.5, usdc_amount=10.0))
            self.assertFalse(result.success)
            self.assertIn("HTTP_PROXY_URL", result.message)


class TestResolvedMarkets(unittest.TestCase):
    """Punto 3: las posiciones de mercados resueltos se liquidan."""

    def test_old_database_gets_condition_id_column(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "old.db")
            conn = sqlite3.connect(db_path)
            conn.execute("""
                CREATE TABLE sub_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trader_address TEXT NOT NULL, token_id TEXT NOT NULL,
                    market_title TEXT, outcome TEXT,
                    shares REAL NOT NULL DEFAULT 0.0, avg_price REAL NOT NULL DEFAULT 0.0,
                    UNIQUE(trader_address, token_id)
                )
            """)
            conn.commit()
            conn.close()

            DatabaseManager(db_path)
            conn = sqlite3.connect(db_path)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(sub_positions)")}
            conn.close()
            self.assertIn("condition_id", columns)

    def test_buy_stores_condition_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(os.path.join(tmpdir, "t.db"))
            db.add_trader("0xaaa", "A", bank=100.0)
            db.record_buy_execution("t1", "0xaaa", "tok", "M", "Yes", 10, 0.5, 10.0, 0.5, "SIMULATED", "", condition_id="0xcond")
            db.record_buy_execution("t2", "0xaaa", "tok", "M", "Yes", 10, 0.5, 10.0, 0.5, "SIMULATED", "")
            self.assertEqual(db.get_sub_position("0xaaa", "tok")["condition_id"], "0xcond")

    def test_settle_won_and_lost_positions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db = DatabaseManager(os.path.join(tmpdir, "t.db"))
            db.add_trader("0xaaa", "A", bank=100.0)
            # 20 USDC a 0.40 = 50 shares en 'win'; 10 USDC a 0.50 = 20 shares en 'lose'.
            db.record_buy_execution("t1", "0xaaa", "win", "M1", "Yes", 0, 0.4, 20.0, 0.4, "SIMULATED", "", condition_id="c1")
            db.record_buy_execution("t2", "0xaaa", "lose", "M2", "No", 0, 0.5, 10.0, 0.5, "SIMULATED", "", condition_id="c2")
            self.assertAlmostEqual(db.get_all_traders()[0]["available_cash"], 70.0)

            self.assertAlmostEqual(db.settle_resolved_position("0xaaa", "win", 1.0), 50.0)
            self.assertAlmostEqual(db.settle_resolved_position("0xaaa", "lose", 0.0), 0.0)
            # Una segunda liquidación no vuelve a abonar nada.
            self.assertEqual(db.settle_resolved_position("0xaaa", "win", 1.0), 0.0)

            self.assertAlmostEqual(db.get_all_traders()[0]["available_cash"], 120.0)
            self.assertEqual(db.get_trader_positions("0xaaa"), [])
            conn = sqlite3.connect(db.db_path)
            statuses = sorted(r[0] for r in conn.execute("SELECT status FROM processed_trades WHERE side = 'RESOLVE'"))
            conn.close()
            self.assertEqual(statuses, ["RESOLVED_LOST", "RESOLVED_WON"])

    def test_get_market_resolution_parsing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir))
            cases = [
                (None, None),
                ({"closed": False, "tokens": [{"token_id": "a", "winner": False}]}, None),
                ({"closed": True, "tokens": [{"token_id": "a", "winner": False}, {"token_id": "b", "winner": False}]}, None),
                ({"closed": True, "tokens": [{"token_id": "a", "winner": True}, {"token_id": "b", "winner": False}]},
                 {"a": 1.0, "b": 0.0}),
            ]
            for market, expected in cases:
                async def fake_get_json(session, url, params, market=market):
                    self.assertTrue(url.endswith("/markets/0xcond"))
                    return market
                api._get_json = fake_get_json
                self.assertEqual(asyncio.run(api.get_market_resolution(None, "0xcond")), expected)
            self.assertIsNone(asyncio.run(api.get_market_resolution(None, "")))

    def test_monitor_settles_resolved_positions_including_paused_traders(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = CopyBotMonitor(make_config(tmpdir))
            db = monitor.db
            db.add_trader("0xaaa", "A", bank=100.0)
            db.record_buy_execution("t1", "0xaaa", "win", "M1", "Yes", 0, 0.5, 10.0, 0.5, "SIMULATED", "", condition_id="c1")
            db.record_buy_execution("t2", "0xaaa", "open", "M2", "Yes", 0, 0.5, 10.0, 0.5, "SIMULATED", "", condition_id="c2")
            db.set_trader_active("A", False)

            async def fake_resolution(session, condition_id):
                return {"win": 1.0, "other": 0.0} if condition_id == "c1" else None
            monitor.api.get_market_resolution = fake_resolution
            messages = []

            async def fake_notify(message, level="INFO", send_telegram=True):
                messages.append((level, message))
            monitor.notifier.notify = fake_notify

            asyncio.run(monitor.settle_resolved_positions(None))

            self.assertEqual([p["token_id"] for p in db.get_trader_positions("0xaaa")], ["open"])
            self.assertAlmostEqual(db.get_all_traders()[0]["available_cash"], 100.0)  # 80 + 20 shares * 1.0
            self.assertEqual(messages[0][0], "SUCCESS")


class TestTelegramNotifications(unittest.TestCase):
    """Punto 11: sin avisos repetitivos y sin mensajes rechazados por Markdown."""

    def test_payload_is_plain_text_and_truncated(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            n = Notifier(make_config(tmpdir, telegram_chat_id="123"))
            payload = n.build_telegram_payload("Trader_1 compró en *Will_BTC*", "INFO")
            self.assertNotIn("parse_mode", payload)
            self.assertIn("Trader_1 compró en *Will_BTC*", payload["text"])
            long_payload = n.build_telegram_payload("x" * 10000, "INFO")
            self.assertLessEqual(len(long_payload["text"]), Notifier.TELEGRAM_MAX_LEN)

    def test_rejected_telegram_message_is_reported(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            n = Notifier(make_config(tmpdir, telegram_token="tok", telegram_chat_id="123"))
            n._sync_post_telegram = lambda url, payload: (400, "Bad Request: can't parse entities")
            with mock.patch.object(notifier_module, "aiohttp", None), \
                    mock.patch("builtins.print") as fake_print:
                asyncio.run(n.notify("hola", "INFO"))
            printed = " ".join(str(c.args[0]) for c in fake_print.call_args_list)
            self.assertIn("HTTP 400", printed)

    def test_send_telegram_false_skips_telegram(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            n = Notifier(make_config(tmpdir, telegram_token="tok", telegram_chat_id="123"))
            n._sync_post_telegram = mock.Mock(return_value=(200, "{}"))
            with mock.patch.object(notifier_module, "aiohttp", None):
                asyncio.run(n.notify("solo consola", "INFO", send_telegram=False))
            n._sync_post_telegram.assert_not_called()

    def test_single_run_does_not_send_startup_or_no_traders_to_telegram(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = CopyBotMonitor(make_config(tmpdir))
            calls = []

            async def fake_notify(message, level="INFO", send_telegram=True):
                calls.append((message, send_telegram))
            monitor.notifier.notify = fake_notify

            asyncio.run(monitor.run(once=True))

            self.assertEqual(len(calls), 2)
            self.assertTrue(all(send is False for _, send in calls), calls)


@unittest.skipUnless(shutil.which("git"), "git no está disponible")
class TestSaveStateScript(unittest.TestCase):
    """Punto 2: el estado se guarda aunque la rama haya avanzado mientras corría el bot."""

    SCRIPT = os.path.join(REPO_ROOT, "scripts", "save_state.sh")

    def git(self, cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=self.env).stdout

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = dict(
            os.environ,
            GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
            GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com",
            STATE_RETRY_DELAY="0", STATE_PUSH_ATTEMPTS="3",
            TELEGRAM_BOT_TOKEN="", TELEGRAM_CHAT_ID="",
        )
        self.remote = os.path.join(self.tmp, "remote.git")
        self.git(self.tmp, "init", "-q", "--bare", "-b", "main", self.remote)
        seed = os.path.join(self.tmp, "seed")
        self.git(self.tmp, "clone", "-q", self.remote, seed)
        os.makedirs(os.path.join(seed, "state"))
        with open(os.path.join(seed, "state", "copybot.db"), "wb") as f:
            f.write(b"v1")
        with open(os.path.join(seed, "traders.json"), "w") as f:
            f.write("{}")
        self.git(seed, "add", ".")
        self.git(seed, "commit", "-q", "-m", "init")
        self.git(seed, "push", "-q", "origin", "HEAD:main")

        self.bot = os.path.join(self.tmp, "bot")
        self.git(self.tmp, "clone", "-q", self.remote, self.bot)
        self.user = os.path.join(self.tmp, "user")
        self.git(self.tmp, "clone", "-q", self.remote, self.user)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self):
        return subprocess.run(["bash", self.SCRIPT], cwd=self.bot, capture_output=True, text=True, env=self.env)

    def test_push_succeeds_after_branch_moved(self):
        with open(os.path.join(self.user, "traders.json"), "w") as f:
            f.write('{"traders": []}')
        self.git(self.user, "commit", "-qam", "editar traders.json")
        self.git(self.user, "push", "-q", "origin", "HEAD:main")

        with open(os.path.join(self.bot, "state", "copybot.db"), "wb") as f:
            f.write(b"v2")
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.git(self.user, "pull", "-q", "origin", "main")
        with open(os.path.join(self.user, "state", "copybot.db"), "rb") as f:
            self.assertEqual(f.read(), b"v2")
        with open(os.path.join(self.user, "traders.json")) as f:
            self.assertEqual(f.read(), '{"traders": []}')

    def test_no_changes_is_a_no_op(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nada que guardar", result.stdout)

    def test_fails_loudly_when_push_is_impossible(self):
        with open(os.path.join(self.bot, "state", "copybot.db"), "wb") as f:
            f.write(b"v2")
        self.git(self.bot, "remote", "set-url", "origin", os.path.join(self.tmp, "no-existe.git"))
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("No se pudo guardar el estado", result.stdout)


if __name__ == "__main__":
    unittest.main()
