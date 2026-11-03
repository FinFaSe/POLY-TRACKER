import asyncio
import os
import sqlite3
import tempfile
import time
import unittest

from config import BotConfig
from src.listener import CopyBotMonitor
from src.polymarket_api import OrderResult, PolymarketClientWrapper

LEADER = "0x00000000000000000000000000000000000000aa"
ME = "0x00000000000000000000000000000000000000ee"


def make_config(tmpdir, **overrides):
    values = dict(
        dry_run=False, db_path=os.path.join(tmpdir, "copybot.db"),
        traders_json_path=os.path.join(tmpdir, "no-existe.json"),
        telegram_token="", telegram_chat_id="", http_proxy_url="", private_key="",
        proxy_address=ME, polygon_rpc_url="https://rpc.example", max_slippage_pct=0.025,
        max_trade_age_minutes=60,
    )
    values.update(overrides)
    return BotConfig(**values)


class FakeClob:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.created = []
        self.posted = []
        self.cancelled = []

    def create_market_order(self, args):
        self.created.append(args)
        return {"signed": args}

    def post_order(self, order, order_type):
        self.posted.append(order_type)
        if self.error:
            raise self.error
        return self.response

    def cancel(self, order_id):
        self.cancelled.append(order_id)


class TestExecuteOrder(unittest.TestCase):
    """Punto 7: solo cuenta como ejecutada una orden que Polymarket confirma como llenada."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = PolymarketClientWrapper(make_config(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def run_order(self, clob, side="BUY", **kwargs):
        self.api.clob_client = clob
        params = dict(token_id="123", side=side, price=0.50, limit_price=0.5125)
        params.update(kwargs)
        return asyncio.run(self.api.execute_order(**params))

    def test_matched_buy_uses_market_order_with_limit_and_real_fill(self):
        from py_clob_client.clob_types import OrderType
        from py_clob_client.order_builder.constants import BUY

        clob = FakeClob({"success": True, "errorMsg": "", "orderID": "0xabc", "status": "matched",
                         "makingAmount": "9.9", "takingAmount": "19.5"})
        result = self.run_order(clob, usdc_amount=10.0)
        self.assertTrue(result.success)
        self.assertAlmostEqual(result.filled_usdc, 9.9)
        self.assertAlmostEqual(result.filled_shares, 19.5)
        self.assertAlmostEqual(result.avg_price, 9.9 / 19.5)
        args = clob.created[0]
        self.assertEqual((args.token_id, args.amount, args.side, args.price), ("123", 10.0, BUY, 0.5125))
        self.assertEqual(args.order_type, OrderType.FOK)
        self.assertEqual(clob.posted, [OrderType.FOK])

    def test_matched_sell_amounts_in_base_units(self):
        clob = FakeClob({"success": True, "status": "matched", "orderID": "1",
                         "makingAmount": str(20 * 10 ** 6), "takingAmount": str(int(9.6 * 10 ** 6))})
        result = self.run_order(clob, side="SELL", shares=20.0, limit_price=0.4875)
        self.assertTrue(result.success)
        self.assertAlmostEqual(result.filled_shares, 20.0)
        self.assertAlmostEqual(result.filled_usdc, 9.6)

    def test_matched_without_amounts_falls_back_to_requested(self):
        result = self.run_order(FakeClob({"success": True, "status": "matched", "orderID": "1"}), usdc_amount=10.0)
        self.assertTrue(result.success)
        self.assertAlmostEqual(result.filled_usdc, 10.0)
        self.assertAlmostEqual(result.filled_shares, 20.0)

    def test_rejected_or_unconfirmed_orders_are_failures(self):
        cases = [
            {"success": False, "errorMsg": "not enough balance / allowance"},
            {"success": True, "errorMsg": "order couldn't be fully filled", "status": ""},
            {"success": True, "status": "unmatched", "orderID": "1"},
            "texto inesperado",
        ]
        for response in cases:
            result = self.run_order(FakeClob(response), usdc_amount=10.0)
            self.assertFalse(result.success, response)
            self.assertEqual(result.filled_usdc, 0.0)

    def test_delayed_order_is_cancelled_and_reported(self):
        clob = FakeClob({"success": True, "status": "delayed", "orderID": "0xdef"})
        result = self.run_order(clob, usdc_amount=10.0)
        self.assertFalse(result.success)
        self.assertIn("delayed", result.message)
        self.assertEqual(clob.cancelled, ["0xdef"])

    def test_exception_is_a_failure(self):
        result = self.run_order(FakeClob(error=RuntimeError("PolyApiException 400")), usdc_amount=10.0)
        self.assertFalse(result.success)
        self.assertIn("400", result.message)

    def test_dry_run_reports_simulated_fill(self):
        api = PolymarketClientWrapper(make_config(self.tmp.name, dry_run=True))
        buy = asyncio.run(api.execute_order("123", "BUY", 0.5, usdc_amount=10.0))
        sell = asyncio.run(api.execute_order("123", "SELL", 0.4, shares=5.0))
        self.assertEqual((buy.success, buy.filled_usdc, buy.filled_shares), (True, 10.0, 20.0))
        self.assertEqual((sell.success, sell.filled_shares), (True, 5.0))
        self.assertAlmostEqual(sell.filled_usdc, 2.0)

    def test_limit_price_follows_slippage_and_is_clamped(self):
        self.assertAlmostEqual(self.api.limit_price(0.50, "BUY"), 0.5125)
        self.assertAlmostEqual(self.api.limit_price(0.50, "SELL"), 0.4875)
        self.assertEqual(self.api.limit_price(0.98, "BUY"), 0.99)
        self.assertEqual(self.api.limit_price(0.01, "SELL"), 0.01)


class TestOnchainBalances(unittest.TestCase):
    """Punto 8: el saldo real se lee en la blockchain."""

    def test_onchain_shares_call_and_parsing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            api = PolymarketClientWrapper(make_config(tmpdir))
            calls = []

            async def fake_post(session, url, payload):
                calls.append(payload["params"][0])
                return {"result": hex(int(12.5 * 10 ** 6))}
            api._post_json = fake_post

            token_id = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
            self.assertEqual(asyncio.run(api.get_onchain_shares(None, ME, token_id)), 12.5)
            call = calls[0]
            self.assertEqual(call["to"], api.cfg.ctf_contract_address)
            self.assertEqual(call["data"], "0x00fdd58e" + ME[2:].rjust(64, "0") + format(int(token_id), "x").rjust(64, "0"))
            self.assertIsNone(asyncio.run(api.get_onchain_shares(None, ME, "no-es-un-numero")))


class TestLiveModeGuards(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.build()

    def build(self, **overrides):
        self.monitor = CopyBotMonitor(make_config(self.tmp.name, **overrides))
        self.db = self.monitor.db
        if not self.db.get_all_traders():
            self.db.add_trader(LEADER, "Lider", bank=100.0)
        self.own_cash = 1000.0
        self.onchain_shares = None
        self.orders = []
        self.messages = []
        self.fill = None
        api = self.monitor.api

        async def book(session, token_id, side):
            return 0.5

        async def leader_value(session, address):
            return 0.0

        async def cash(session, address):
            return self.own_cash if address == ME else 0.0  # el líder no tiene efectivo

        async def onchain(session, address, token_id):
            return self.onchain_shares

        async def token_balance(session, address, token_id):
            return 0.0

        async def execute(token_id, side, price, usdc_amount=0.0, shares=0.0, limit_price=None):
            self.orders.append((side, usdc_amount, shares, limit_price))
            if self.fill is not None:
                return self.fill
            if side == "BUY":
                return OrderResult(True, "ok", usdc_amount, usdc_amount / price, price)
            return OrderResult(True, "ok", shares * price, shares, price)

        api.get_best_orderbook_price = book
        api.get_leader_portfolio_value = leader_value
        api.get_cash_balance = cash
        api.get_leader_cash_balance = cash
        api.get_onchain_shares = onchain
        api.get_leader_token_balance = token_balance
        api.execute_order = execute

        async def notify(message, level="INFO", send_telegram=True):
            self.messages.append((level, message))
        self.monitor.notifier.notify = notify

    def tearDown(self):
        self.tmp.cleanup()

    def trader(self):
        return self.db.get_active_traders()[0]

    def buy(self, usdc=100.0):
        event = {"transactionHash": f"0x{len(self.orders)}{time.time()}", "asset": "123", "side": "BUY",
                 "timestamp": int(time.time()) + 5, "size": usdc / 0.5, "price": 0.5, "usdcSize": usdc,
                 "title": "M", "outcome": "Yes", "conditionId": "0xc"}
        asyncio.run(self.monitor.process_leader_trade(None, self.trader(), event))

    def sell(self, shares=1000.0):
        event = {"transactionHash": f"0xs{time.time()}", "asset": "123", "side": "SELL",
                 "timestamp": int(time.time()) + 5, "size": shares, "price": 0.5, "usdcSize": shares * 0.5,
                 "title": "M", "outcome": "Yes", "conditionId": "0xc"}
        asyncio.run(self.monitor.process_leader_trade(None, self.trader(), event))

    def last_status(self):
        conn = sqlite3.connect(self.db.db_path)
        row = conn.execute("SELECT status, copied_usdc FROM processed_trades ORDER BY rowid DESC LIMIT 1").fetchone()
        conn.close()
        return row

    def test_buy_skipped_when_wallet_has_less_than_order(self):
        # Líder sin efectivo y sin posiciones: compra el 100% → tope 25% de 100 = $25.
        self.own_cash = 10.0
        self.buy()
        self.assertEqual(self.orders, [])
        self.assertEqual(self.last_status()[0], "SKIPPED_NO_FUNDS")
        self.assertEqual(self.messages[-1][0], "ERROR")
        self.assertAlmostEqual(self.trader()["available_cash"], 100.0)

    def test_buy_skipped_without_proxy_address(self):
        self.build(proxy_address="")
        self.buy()
        self.assertEqual(self.orders, [])
        self.assertEqual(self.last_status()[0], "SKIPPED_NO_FUNDS")
        self.assertIn("POLYMARKET_PROXY_ADDRESS", self.messages[-1][1])

    def test_buy_records_actual_fill_and_limit_price(self):
        self.fill = OrderResult(True, "ok", 24.0, 46.0, 24.0 / 46.0)
        self.buy()
        self.assertEqual(self.orders, [("BUY", 25.0, 0.0, 0.5125)])
        self.assertEqual(self.last_status(), ("EXECUTED", 24.0))
        self.assertAlmostEqual(self.trader()["available_cash"], 76.0)
        self.assertAlmostEqual(self.db.get_sub_position(LEADER, "123")["shares"], 46.0)

    def test_failed_buy_does_not_move_balances(self):
        self.fill = OrderResult(False, "Polymarket rechazó la orden")
        self.buy()
        self.assertEqual(self.last_status()[0], "FAILED")
        self.assertAlmostEqual(self.trader()["available_cash"], 100.0)
        self.assertIsNone(self.db.get_sub_position(LEADER, "123"))

    def test_sell_is_capped_to_onchain_shares(self):
        self.db.record_buy_execution("t0", LEADER, "123", "M", "Yes", 0, 0.5, 10.0, 0.5, "EXECUTED", "")  # 20 shares
        self.onchain_shares = 12.345
        self.sell()
        side, _, shares, limit = self.orders[-1]
        self.assertEqual((side, shares, limit), ("SELL", 12.34, 0.4875))
        self.assertIn("limitada", self.messages[-1][1])
        self.assertAlmostEqual(self.db.get_sub_position(LEADER, "123")["shares"], 20.0 - 12.34)

    def test_sell_skipped_when_wallet_has_no_shares(self):
        self.db.record_buy_execution("t0", LEADER, "123", "M", "Yes", 0, 0.5, 10.0, 0.5, "EXECUTED", "")
        self.onchain_shares = 0.0
        self.sell()
        self.assertEqual(self.orders, [])
        self.assertEqual(self.last_status()[0], "SKIPPED_NO_SHARES")

    def test_startup_warns_when_banks_exceed_real_balance(self):
        self.own_cash = 40.0
        asyncio.run(self.monitor.check_real_balance(None, once=True))
        level, message = self.messages[-1]
        self.assertEqual(level, "WARNING")
        self.assertIn("$100.00", message)
        self.assertIn("$40.00", message)

    def test_no_balance_checks_in_dry_run(self):
        self.build(dry_run=True)
        self.own_cash = 0.0
        self.buy()
        self.assertEqual(self.last_status()[0], "SIMULATED")
        asyncio.run(self.monitor.check_real_balance(None, once=True))
        self.assertFalse(any("cartera" in m for _, m in self.messages))


if __name__ == "__main__":
    unittest.main()
