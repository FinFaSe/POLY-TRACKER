import asyncio
import itertools
import json
import os
import tempfile
import time
import unittest

from config import BotConfig
from src.listener import CopyBotMonitor

A = "0x00000000000000000000000000000000000000aa"
B = "0x00000000000000000000000000000000000000bb"


_writes = itertools.count(1)


def write_traders(path, traders):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"traders": traders}, f)
    # Fecha de modificación siempre distinta, para que el cambio se note aunque el test vaya muy rápido.
    stamp = time.time() + next(_writes)
    os.utime(path, (stamp, stamp))


def entry(alias, address, active=True):
    return {"alias": alias, "proxy_address": address, "bank": 100.0, "is_active": active}


class TestContinuousMode(unittest.TestCase):
    """Punto 5: el modo continuo aguanta errores, recarga traders.json y se detiene ordenadamente."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.json_path = os.path.join(self.tmp.name, "traders.json")
        write_traders(self.json_path, [entry("A", A)])
        self.cfg = BotConfig(
            dry_run=True, db_path=os.path.join(self.tmp.name, "c.db"), traders_json_path=self.json_path,
            telegram_token="", telegram_chat_id="", private_key="", http_proxy_url="",
            poll_interval=0, heartbeat_hours=0,
        )
        self.monitor = CopyBotMonitor(self.cfg)
        self.messages = []

        async def notify(message, level="INFO", send_telegram=True):
            self.messages.append((level, message, send_telegram))
        self.monitor.notifier.notify = notify

    def tearDown(self):
        self.tmp.cleanup()

    def run_loop(self, scan, max_cycles):
        """Ejecuta el bucle continuo con un scan_once falso y lo detiene tras max_cycles ciclos."""
        cycles = {"n": 0}

        async def fake_scan(session):
            cycles["n"] += 1
            try:
                await scan(cycles["n"])
            finally:
                if cycles["n"] >= max_cycles:
                    self.monitor.request_stop()
        self.monitor.scan_once = fake_scan

        async def no_wait(*args, **kwargs):
            return None
        self.monitor.check_real_balance = no_wait
        asyncio.run(self.monitor.run(once=False))
        return cycles["n"]

    def test_errors_do_not_stop_the_bot_and_telegram_is_not_flooded(self):
        self.monitor.cfg.poll_interval = 0
        original_wait_for = asyncio.wait_for

        async def instant_wait_for(aw, timeout):
            # Sin esperas reales en el test, aunque el bot aplique espera progresiva tras errores.
            return await original_wait_for(aw, timeout=0.001)

        async def failing_scan(n):
            if n <= 4:
                raise RuntimeError("Data API caída")

        asyncio.wait_for = instant_wait_for
        try:
            cycles = self.run_loop(failing_scan, max_cycles=6)
        finally:
            asyncio.wait_for = original_wait_for
        self.assertEqual(cycles, 6)
        errors = [m for m in self.messages if m[0] == "ERROR"]
        self.assertEqual(len(errors), 4)
        self.assertEqual([m[2] for m in errors], [True, False, False, False])  # solo el primero a Telegram
        self.assertIn(("INFO", "Bot detenido de forma ordenada.", True), self.messages)

    def test_traders_json_changes_are_applied_without_restart(self):
        async def scan(n):
            if n == 1:
                write_traders(self.json_path, [entry("B", B)])

        self.run_loop(scan, max_cycles=3)
        active = [t["alias"] for t in self.monitor.db.get_active_traders()]
        self.assertEqual(active, ["B"])
        texts = [m[1] for m in self.messages]
        self.assertTrue(any(t.startswith("traders.json recargado") for t in texts), texts)
        self.assertIn("Desactivados porque ya no están en traders.json: A", texts)

    def test_broken_traders_json_is_reported_and_previous_config_kept(self):
        async def scan(n):
            if n == 1:
                with open(self.json_path, "w") as f:
                    f.write("{ esto no es json")
                os.utime(self.json_path, (time.time() + 100, time.time() + 100))

        self.run_loop(scan, max_cycles=3)
        self.assertEqual([t["alias"] for t in self.monitor.db.get_active_traders()], ["A"])
        self.assertTrue(any(m[0] == "ERROR" and "traders.json" in m[1] for m in self.messages))

    def test_no_traders_warning_is_shown_once(self):
        write_traders(self.json_path, [])
        monitor = CopyBotMonitor(self.cfg)
        messages = []

        async def notify(message, level="INFO", send_telegram=True):
            messages.append(message)
        monitor.notifier.notify = notify
        for _ in range(5):
            asyncio.run(monitor.scan_once(None))
        self.assertEqual(sum("No hay traders activos" in m for m in messages), 1)

    def test_heartbeat_is_sent_when_due(self):
        self.monitor.cfg.heartbeat_hours = 1
        self.monitor._last_heartbeat = time.time() - 3601
        asyncio.run(self.monitor.send_heartbeat_if_due())
        asyncio.run(self.monitor.send_heartbeat_if_due())
        beats = [m for m in self.messages if m[1].startswith("Sigo funcionando")]
        self.assertEqual(len(beats), 1)
        self.assertIn("Traders activos: 1", beats[0][1])


if __name__ == "__main__":
    unittest.main()
