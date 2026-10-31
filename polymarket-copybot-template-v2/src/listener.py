import asyncio
import time
from contextlib import AsyncExitStack
from typing import Dict, Optional
from config import BotConfig
from src.database import DatabaseManager
from src.polymarket_api import PolymarketClientWrapper
from src.sizing_engine import ProportionalSizingEngine
from src.notifier import Notifier

try:
    import aiohttp
except ImportError:
    aiohttp = None

class CopyBotMonitor:
    """
    Escucha los movimientos de cada trader activo y coordina
    el cálculo proporcional por sub-banca y la ejecución en el CLOB.
    Soporta tanto ejecución por ciclo único (--once en GitHub Actions)
    como bucle continuo (Codespaces / VPS / Docker).
    """
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.db = DatabaseManager(cfg.db_path)
        self.db.sync_from_json(cfg.traders_json_path)
        self.api = PolymarketClientWrapper(cfg)
        self.sizer = ProportionalSizingEngine(
            self.db,
            min_order_usdc=cfg.min_order_usdc,
            max_position_weight_pct=cfg.max_position_weight_pct
        )
        self.notifier = Notifier(cfg)

    @staticmethod
    def normalize_timestamp(raw_ts) -> int:
        ts = int(float(raw_ts or 0))
        if ts > 1_000_000_000_000:
            ts = ts // 1000
        return ts

    async def estimate_subbank_positions_value(self, session, trader_address: str) -> float:
        positions = self.db.get_trader_positions(trader_address)
        total_val = 0.0
        for pos in positions:
            price = await self.api.get_best_orderbook_price(session, pos["token_id"], "SELL")
            if price is None:
                price = pos["avg_price"]
            total_val += pos["shares"] * price
        return total_val

    async def process_leader_trade(self, session, trader: Dict, trade_event: Dict):
        tx_hash = trade_event.get("transactionHash", "")
        token_id = str(trade_event.get("asset", ""))
        side = str(trade_event.get("side", "")).upper()
        trade_timestamp = self.normalize_timestamp(trade_event.get("timestamp", 0))

        last_seen = int(trader.get("last_seen_timestamp", 0))
        if trade_timestamp < last_seen:
            return

        trade_id = f"{tx_hash}_{token_id}_{side}_{trade_timestamp}"
        if not token_id or side not in ("BUY", "SELL") or self.db.is_trade_processed(trade_id):
            return

        leader_shares = float(trade_event.get("size", 0.0))
        leader_price = float(trade_event.get("price", 0.0))
        leader_usdc = float(trade_event.get("usdcSize", leader_shares * leader_price))
        market_title = trade_event.get("title", "Mercado Polymarket")
        outcome = trade_event.get("outcome", "")

        await self.notifier.notify(
            f"Movimiento detectado de *{trader['alias']}*: {side} ${leader_usdc:.2f} USDC en '{market_title}' ({outcome}) a ${leader_price:.3f}",
            "INFO"
        )

        market_price = await self.api.get_best_orderbook_price(session, token_id, side)
        if market_price is None:
            self.db.record_buy_execution(
                trade_id, trader["proxy_address"], token_id, market_title, outcome,
                leader_usdc, leader_price, 0.0, 0.0, "SKIPPED_NO_LIQUIDITY", "Libro de órdenes vacío"
            )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            await self.notifier.notify(f"Operación descartada ({trader['alias']}): Sin liquidez en el libro de órdenes.", "WARNING")
            return

        slippage_ok, slippage_pct = self.api.check_slippage(leader_price, market_price, side)
        if not slippage_ok:
            status = "SKIPPED_SLIPPAGE"
            reason = f"Slippage de {slippage_pct*100:.2f}% supera el máximo de {self.cfg.max_slippage_pct*100:.2f}%"
            if side == "BUY":
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, market_price, status, reason
                )
            else:
                self.db.record_sell_execution(
                    trade_id, trader["proxy_address"], token_id,
                    leader_usdc, leader_price, 0.0, market_price, status, reason
                )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            await self.notifier.notify(f"Operación descartada ({trader['alias']}): {reason}", "WARNING")
            return

        if side == "BUY":
            leader_equity = await self.api.get_leader_portfolio_value(session, trader["proxy_address"])
            subbank_pos_val = await self.estimate_subbank_positions_value(session, trader["proxy_address"])
            decision = self.sizer.evaluate_buy(trader, leader_usdc, leader_equity, subbank_pos_val)

            if not decision.should_execute:
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, market_price, "SKIPPED_SIZING", decision.reason
                )
                self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                await self.notifier.notify(f"Compra omitida ({trader['alias']}): {decision.reason}", "WARNING")
                return

            success, exec_msg = await self.api.execute_order(
                token_id=token_id, side="BUY", price=market_price, usdc_amount=decision.usdc_amount
            )
            status = ("SIMULATED" if self.cfg.dry_run else "EXECUTED") if success else "FAILED"
            self.db.record_buy_execution(
                trade_id, trader["proxy_address"], token_id, market_title, outcome,
                leader_usdc, leader_price, decision.usdc_amount, market_price, status, exec_msg
            )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            await self.notifier.notify(f"{decision.reason} | {exec_msg}", "SUCCESS" if success else "ERROR")

        elif side == "SELL":
            leader_remaining = await self.api.get_leader_token_balance(session, trader["proxy_address"], token_id)
            decision = self.sizer.evaluate_sell(trader, token_id, leader_shares, leader_remaining, market_price)

            if not decision.should_execute:
                self.db.record_sell_execution(
                    trade_id, trader["proxy_address"], token_id,
                    leader_usdc, leader_price, 0.0, market_price, "SKIPPED_SIZING", decision.reason
                )
                self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                await self.notifier.notify(f"Venta omitida ({trader['alias']}): {decision.reason}", "WARNING")
                return

            success, exec_msg = await self.api.execute_order(
                token_id=token_id, side="SELL", price=market_price, shares=decision.shares_to_sell
            )
            status = ("SIMULATED" if self.cfg.dry_run else "EXECUTED") if success else "FAILED"
            self.db.record_sell_execution(
                trade_id, trader["proxy_address"], token_id,
                leader_usdc, leader_price, decision.shares_to_sell, market_price, status, exec_msg
            )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            await self.notifier.notify(f"{decision.reason} | {exec_msg}", "SUCCESS" if success else "ERROR")

    async def scan_once(self, session):
        active_traders = self.db.get_active_traders()
        if not active_traders:
            await self.notifier.notify("No hay traders activos configurados (revisa traders.json o usa cli.py).", "WARNING")
            return

        for trader in active_traders:
            try:
                recent_trades = await self.api.get_recent_leader_trades(session, trader["proxy_address"], limit=10)
                for trade_event in reversed(recent_trades):
                    await self.process_leader_trade(session, trader, trade_event)
            except Exception as e:
                await self.notifier.notify(f"Error monitoreando a {trader['alias']}: {e}", "ERROR")

    async def run(self, once: bool = False, max_runtime_minutes: Optional[int] = None):
        mode_str = "SIMULACIÓN (Paper Trading)" if self.cfg.dry_run else "PRODUCCIÓN (Dinero Real)"
        exec_type = "Ciclo Único (--once)" if once else "Monitoreo Continuo"
        await self.notifier.notify(f"Iniciando Polymarket CopyBot | Modo: {mode_str} | Ejecución: {exec_type}", "INFO")

        start_time = time.time()
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(aiohttp.ClientSession()) if aiohttp is not None else None
            if once:
                await self.scan_once(session)
                return

            while True:
                await self.scan_once(session)
                if max_runtime_minutes and (time.time() - start_time) >= (max_runtime_minutes * 60):
                    await self.notifier.notify(f"Tiempo límite alcanzado ({max_runtime_minutes} min). Finalizando ciclo limpiamente.", "INFO")
                    break
                await asyncio.sleep(self.cfg.poll_interval)
