import os
import tempfile
import unittest
from src.database import DatabaseManager
from src.sizing_engine import ProportionalSizingEngine

class TestMultiBankCopyBot(unittest.TestCase):
    def test_multi_bank_isolation_and_proportional_sizing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_copybot.db")
            db = DatabaseManager(db_path)
            sizer = ProportionalSizingEngine(db, min_order_usdc=2.0, max_position_weight_pct=0.50)

            db.add_trader("0xaaa111", "TraderA", bank=500.0, max_drawdown_pct=0.30)
            db.add_trader("0xbbb222", "TraderB", bank=200.0, max_drawdown_pct=0.20)

            traders = {t["alias"]: t for t in db.get_active_traders()}
            self.assertEqual(len(traders), 2)

            decision_buy = sizer.evaluate_buy(
                trader=traders["TraderA"],
                leader_trade_usdc=1000.0,
                leader_total_equity=10000.0,
                current_subbank_positions_value=0.0
            )
            self.assertTrue(decision_buy.should_execute)
            self.assertEqual(decision_buy.usdc_amount, 50.0)

            db.record_buy_execution(
                trade_id="tx1_tokenX_BUY_100",
                trader_address="0xaaa111",
                token_id="tokenX",
                market_title="Will BTC hit 100k?",
                outcome="Yes",
                leader_usdc=1000.0,
                leader_price=0.50,
                copied_usdc=50.0,
                exec_price=0.50,
                status="SIMULATED"
            )

            traders_after = {t["alias"]: t for t in db.get_active_traders()}
            self.assertEqual(traders_after["TraderA"]["available_cash"], 450.0)
            self.assertEqual(traders_after["TraderB"]["available_cash"], 200.0)

            decision_sell = sizer.evaluate_sell(
                trader=traders_after["TraderA"],
                token_id="tokenX",
                leader_shares_sold=1000.0,
                leader_remaining_shares=1000.0,
                current_bid_price=0.60
            )
            self.assertTrue(decision_sell.should_execute)
            self.assertEqual(decision_sell.shares_to_sell, 50.0)

            db.record_sell_execution(
                trade_id="tx2_tokenX_SELL_200",
                trader_address="0xaaa111",
                token_id="tokenX",
                leader_usdc=600.0,
                leader_price=0.60,
                shares_sold=50.0,
                exec_price=0.60,
                status="SIMULATED"
            )

            traders_final = {t["alias"]: t for t in db.get_active_traders()}
            self.assertEqual(traders_final["TraderA"]["available_cash"], 480.0)
            pos_a = db.get_sub_position("0xaaa111", "tokenX")
            self.assertEqual(pos_a["shares"], 50.0)

if __name__ == "__main__":
    unittest.main()
