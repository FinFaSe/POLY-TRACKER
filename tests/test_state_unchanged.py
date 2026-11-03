import asyncio
import hashlib
import json
import os
import tempfile
import unittest

from config import BotConfig
from src.database import DatabaseManager
from src.listener import CopyBotMonitor

A = "0x00000000000000000000000000000000000000aa"
B = "0x00000000000000000000000000000000000000bb"


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class TestStateUnchangedWhenNothingHappens(unittest.TestCase):
    """Sin cambios reales, la base de datos no se reescribe (y GitHub Actions no hace un commit de estado)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "copybot.db")
        self.json_path = os.path.join(self.tmp.name, "traders.json")
        with open(self.json_path, "w") as f:
            json.dump({"traders": [
                {"alias": "A", "proxy_address": A, "bank": 100.0, "is_active": False},
                {"alias": "B", "proxy_address": B, "bank": 50.0, "is_active": True},
            ]}, f)
        db = DatabaseManager(self.db_path)
        db.sync_from_json(self.json_path)
        db.update_last_seen_timestamp(B, 1_800_000_000)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resync_and_old_timestamps_do_not_touch_the_file(self):
        before = digest(self.db_path)
        db = DatabaseManager(self.db_path)
        self.assertEqual(db.sync_from_json(self.json_path), [])
        db.update_last_seen_timestamp(B, 1_700_000_000)  # más antiguo que el guardado
        db.get_active_traders()
        self.assertEqual(digest(self.db_path), before)

    def test_bot_run_without_activity_does_not_touch_the_file(self):
        before = digest(self.db_path)
        cfg = BotConfig(dry_run=True, db_path=self.db_path, traders_json_path=self.json_path,
                        telegram_token="", telegram_chat_id="", private_key="", http_proxy_url="")
        monitor = CopyBotMonitor(cfg)

        async def no_trades(session, address, since):
            return [], False

        async def quiet(*args, **kwargs):
            return None
        monitor.api.get_leader_trades_since = no_trades
        monitor.notifier.notify = quiet
        asyncio.run(monitor.run(once=True))
        self.assertEqual(digest(self.db_path), before)

    def test_real_changes_are_still_saved(self):
        before = digest(self.db_path)
        db = DatabaseManager(self.db_path)
        db.update_last_seen_timestamp(B, 1_900_000_000)
        self.assertNotEqual(digest(self.db_path), before)
        self.assertEqual(db.get_active_traders()[0]["last_seen_timestamp"], 1_900_000_000)


if __name__ == "__main__":
    unittest.main()
