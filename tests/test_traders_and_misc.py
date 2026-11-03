import asyncio
import gc
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import warnings

from config import BotConfig
from src.database import DatabaseManager
from src.listener import CopyBotMonitor

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A = "0x00000000000000000000000000000000000000aa"
B = "0x00000000000000000000000000000000000000bb"


def write_traders(path, traders):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"traders": traders}, f)


def entry(alias, address, bank=100.0, active=True):
    return {"alias": alias, "proxy_address": address, "bank": bank, "max_drawdown_pct": 0.3, "is_active": active}


class TestTradersJsonIsSourceOfTruth(unittest.TestCase):
    """traders.json manda sobre la base de datos."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.json_path = os.path.join(self.tmp.name, "traders.json")
        self.db_path = os.path.join(self.tmp.name, "copybot.db")

    def tearDown(self):
        self.tmp.cleanup()

    def active_aliases(self, db):
        return sorted(t["alias"] for t in db.get_active_traders())

    def test_trader_removed_from_json_is_deactivated_and_keeps_history(self):
        db = DatabaseManager(self.db_path)
        write_traders(self.json_path, [entry("A", A), entry("B", B)])
        self.assertEqual(db.sync_from_json(self.json_path), [])
        db.record_buy_execution("t1", B, "tok", "M", "Yes", 0, 0.5, 10.0, 0.5, "SIMULATED", "")

        write_traders(self.json_path, [entry("A", A)])
        self.assertEqual(db.sync_from_json(self.json_path), ["B"])
        self.assertEqual(self.active_aliases(db), ["A"])
        self.assertEqual(len(db.get_trader_positions(B)), 1)
        # Una segunda sincronización no lo vuelve a anunciar.
        self.assertEqual(db.sync_from_json(self.json_path), [])

    def test_empty_traders_list_deactivates_everyone(self):
        db = DatabaseManager(self.db_path)
        write_traders(self.json_path, [entry("A", A)])
        db.sync_from_json(self.json_path)
        write_traders(self.json_path, [])
        self.assertEqual(db.sync_from_json(self.json_path), ["A"])
        self.assertEqual(self.active_aliases(db), [])

    def test_missing_json_changes_nothing(self):
        db = DatabaseManager(self.db_path)
        db.add_trader(A, "A", bank=100.0)
        self.assertEqual(db.sync_from_json(os.path.join(self.tmp.name, "no-existe.json")), [])
        self.assertEqual(self.active_aliases(db), ["A"])

    def cli(self, *args):
        env = dict(os.environ, DB_PATH=self.db_path, TRADERS_JSON_PATH=self.json_path)
        return subprocess.run(
            [sys.executable, os.path.join(REPO_ROOT, "cli.py"), *args],
            cwd=REPO_ROOT, env=env, capture_output=True, text=True,
        )

    def test_cli_changes_are_written_to_json_and_survive_sync(self):
        write_traders(self.json_path, [entry("A", A)])
        self.assertEqual(self.cli("add-trader", "--address", B, "--alias", "B", "--bank", "50").returncode, 0)
        self.assertEqual(self.cli("update-bank", "--alias", "A", "--bank", "300").returncode, 0)
        self.assertEqual(self.cli("pause", "--alias", "A").returncode, 0)

        with open(self.json_path, encoding="utf-8") as f:
            traders = {t["alias"]: t for t in json.load(f)["traders"]}
        self.assertEqual(traders["A"]["bank"], 300.0)
        self.assertFalse(traders["A"]["is_active"])
        self.assertTrue(traders["B"]["is_active"])

        # Lo que hace el bot al arrancar: sincronizar. Los cambios del CLI no se pierden.
        db = DatabaseManager(self.db_path)
        db.sync_from_json(self.json_path)
        state = {t["alias"]: t for t in db.get_all_traders()}
        self.assertEqual(state["A"]["initial_bank"], 300.0)
        self.assertEqual(state["A"]["is_active"], 0)
        self.assertEqual(state["B"]["is_active"], 1)

        self.assertEqual(self.cli("resume", "--alias", "A").returncode, 0)
        self.assertEqual(self.active_aliases(DatabaseManager(self.db_path)), ["A", "B"])

    def test_cli_rejects_duplicates_and_unknown_alias(self):
        write_traders(self.json_path, [entry("A", A)])
        dup = self.cli("add-trader", "--address", B, "--alias", "A", "--bank", "50")
        self.assertEqual(dup.returncode, 1)
        self.assertIn("Ya existe", dup.stdout)
        unknown = self.cli("pause", "--alias", "Nadie")
        self.assertEqual(unknown.returncode, 1)
        with open(self.json_path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["traders"]), 1)

    def test_bot_warns_about_removed_traders_on_start(self):
        db = DatabaseManager(self.db_path)
        write_traders(self.json_path, [entry("A", A), entry("B", B, active=False)])
        db.sync_from_json(self.json_path)
        write_traders(self.json_path, [entry("B", B, active=False)])

        cfg = BotConfig(dry_run=True, db_path=self.db_path, traders_json_path=self.json_path,
                        telegram_token="", telegram_chat_id="", private_key="", http_proxy_url="")
        monitor = CopyBotMonitor(cfg)
        messages = []

        async def notify(message, level="INFO", send_telegram=True):
            messages.append((level, message))
        monitor.notifier.notify = notify
        asyncio.run(monitor.run(once=True))
        self.assertIn(("WARNING", "Desactivados porque ya no están en traders.json: A"), messages)


class TestMinorFixes(unittest.TestCase):
    def test_sell_without_liquidity_is_recorded_as_sell(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = BotConfig(dry_run=True, db_path=os.path.join(tmpdir, "c.db"),
                            traders_json_path=os.path.join(tmpdir, "no.json"),
                            telegram_token="", telegram_chat_id="", private_key="", http_proxy_url="")
            monitor = CopyBotMonitor(cfg)
            monitor.db.add_trader(A, "A", bank=100.0)
            monitor.db.record_buy_execution("t0", A, "tok", "M", "Yes", 0, 0.5, 10.0, 0.5, "SIMULATED", "")

            async def empty_book(session, token_id, side):
                return None

            async def notify(message, level="INFO", send_telegram=True):
                pass
            monitor.api.get_best_orderbook_price = empty_book
            monitor.notifier.notify = notify

            trader = monitor.db.get_active_traders()[0]
            event = {"transactionHash": "0x1", "asset": "tok", "side": "SELL", "timestamp": int(time.time()) + 5,
                     "size": 10, "price": 0.5, "usdcSize": 5.0, "title": "M", "outcome": "Yes"}
            asyncio.run(monitor.process_leader_trade(None, trader, event))

            conn = sqlite3.connect(cfg.db_path)
            rows = conn.execute("SELECT side, status FROM processed_trades WHERE trade_id != 't0'").fetchall()
            conn.close()
            self.assertEqual(rows, [("SELL", "SKIPPED_NO_LIQUIDITY")])
            self.assertEqual(monitor.db.get_sub_position(A, "tok")["shares"], 20.0)

    def test_database_connections_are_closed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                db = DatabaseManager(os.path.join(tmpdir, "c.db"))
                db.add_trader(A, "A", bank=100.0)
                db.get_active_traders()
                db.record_buy_execution("t1", A, "tok", "M", "Yes", 0, 0.5, 10.0, 0.5, "SIMULATED", "")
                del db
                gc.collect()
            leaks = [w for w in caught if issubclass(w.category, ResourceWarning) and "sqlite3" in str(w.message)]
            self.assertEqual(leaks, [])

    def test_requirements_are_pinned_and_unused_packages_removed(self):
        with open(os.path.join(REPO_ROOT, "requirements.txt"), encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.startswith("#")]
        for line in lines:
            self.assertRegex(line, r"^[A-Za-z0-9_.\-\[\]]+==[0-9][0-9A-Za-z.]*$")
        names = {re.split(r"[\[=]", l)[0].lower() for l in lines}
        self.assertFalse(names & {"web3", "pydantic"})


if __name__ == "__main__":
    unittest.main()
