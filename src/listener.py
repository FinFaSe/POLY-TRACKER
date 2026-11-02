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
        self._last_resolution_check = 0.0
        self._warned_missing_condition = set()

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

    async def get_leader_total_equity(self, session, proxy_address: str) -> Optional[float]:
        """
        Patrimonio total del líder: valor de sus posiciones + su efectivo en USDC.
        La Data API (/value) solo da las posiciones; sin el efectivo, un líder con mucha
        liquidez parecería apostar una parte mucho mayor de su capital.
        Devuelve None si falta cualquiera de los dos datos.
        """
        positions_value = await self.api.get_leader_portfolio_value(session, proxy_address)
        if positions_value is None:
            return None
        cash = await self.api.get_leader_cash_balance(session, proxy_address)
        if cash is None:
            return None
        return positions_value + cash

    async def process_leader_trade(self, session, trader: Dict, trade_event: Dict) -> str:
        """
        Procesa una operación del líder. Devuelve "stale" si se descartó por antigua,
        "ignored" si ya estaba vista o no es válida, y "processed" en otro caso.
        """
        tx_hash = trade_event.get("transactionHash", "")
        token_id = str(trade_event.get("asset", ""))
        side = str(trade_event.get("side", "")).upper()
        trade_timestamp = self.normalize_timestamp(trade_event.get("timestamp", 0))

        last_seen = int(trader.get("last_seen_timestamp", 0))
        if trade_timestamp < last_seen:
            return "ignored"

        trade_id = f"{tx_hash}_{token_id}_{side}_{trade_timestamp}"
        if not token_id or side not in ("BUY", "SELL") or self.db.is_trade_processed(trade_id):
            return "ignored"

        leader_shares = float(trade_event.get("size", 0.0))
        leader_price = float(trade_event.get("price", 0.0))
        leader_usdc = float(trade_event.get("usdcSize", leader_shares * leader_price))
        market_title = trade_event.get("title", "Mercado Polymarket")
        outcome = trade_event.get("outcome", "")
        condition_id = str(trade_event.get("conditionId", "") or "")

        # Comprar al precio de ahora lo que el líder compró hace horas no es copiarle. Pasa al
        # reactivar un trader o el workflow tras una pausa, o si GitHub Actions se retrasa.
        # Una venta antigua sí se ejecuta si tenemos la posición: el líder ya salió del mercado.
        max_age_seconds = self.cfg.max_trade_age_minutes * 60
        is_stale = max_age_seconds > 0 and trade_timestamp < time.time() - max_age_seconds
        holds_position = side == "SELL" and bool(
            (self.db.get_sub_position(trader["proxy_address"], token_id) or {}).get("shares", 0) > 0.0001
        )
        if is_stale and not holds_position:
            reason = f"Operación con más de {self.cfg.max_trade_age_minutes} min de antigüedad"
            if side == "BUY":
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, 0.0, "SKIPPED_STALE", reason
                )
            else:
                self.db.record_sell_execution(
                    trade_id, trader["proxy_address"], token_id,
                    leader_usdc, leader_price, 0.0, 0.0, "SKIPPED_STALE", reason
                )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            return "stale"

        await self.notifier.notify(
            f"Movimiento detectado de {trader['alias']}: {side} ${leader_usdc:.2f} USDC en '{market_title}' ({outcome}) a ${leader_price:.3f}",
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
            return "processed"

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
            return "processed"

        if side == "BUY":
            leader_equity = await self.get_leader_total_equity(session, trader["proxy_address"])
            if leader_equity is None:
                reason = "No se pudo leer el patrimonio del líder (posiciones + efectivo); revisa POLYGON_RPC_URL"
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, market_price, "SKIPPED_NO_EQUITY", reason
                )
                self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                await self.notifier.notify(f"Compra omitida ({trader['alias']}): {reason}", "WARNING")
                return "processed"
            subbank_pos_val = await self.estimate_subbank_positions_value(session, trader["proxy_address"])
            decision = self.sizer.evaluate_buy(trader, leader_usdc, leader_equity, subbank_pos_val)

            if not decision.should_execute:
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, market_price, "SKIPPED_SIZING", decision.reason
                )
                self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                await self.notifier.notify(f"Compra omitida ({trader['alias']}): {decision.reason}", "WARNING")
                return "processed"

            success, exec_msg = await self.api.execute_order(
                token_id=token_id, side="BUY", price=market_price, usdc_amount=decision.usdc_amount
            )
            status = ("SIMULATED" if self.cfg.dry_run else "EXECUTED") if success else "FAILED"
            self.db.record_buy_execution(
                trade_id, trader["proxy_address"], token_id, market_title, outcome,
                leader_usdc, leader_price, decision.usdc_amount, market_price, status, exec_msg,
                condition_id=condition_id
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
                return "processed"

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

        return "processed"

    async def settle_resolved_positions(self, session):
        """
        Cierra las posiciones de mercados ya resueltos. El trader líder cobra al
        resolverse el mercado sin vender, así que el bot nunca vería una venta que copiar.
        Incluye traders pausados: sus posiciones también se resuelven.
        """
        for trader in self.db.get_all_traders():
            for pos in self.db.get_trader_positions(trader["proxy_address"]):
                token_id = pos["token_id"]
                condition_id = pos.get("condition_id") or ""
                if not condition_id:
                    if token_id not in self._warned_missing_condition:
                        self._warned_missing_condition.add(token_id)
                        await self.notifier.notify(
                            f"Posición de {trader['alias']} en '{pos.get('market_title')}' sin condition_id: "
                            "no se puede comprobar si el mercado se resolvió.",
                            "WARNING", send_telegram=False
                        )
                    continue
                try:
                    payouts = await self.api.get_market_resolution(session, condition_id)
                except Exception as e:
                    await self.notifier.notify(
                        f"No se pudo comprobar la resolución de '{pos.get('market_title')}': {e}",
                        "WARNING", send_telegram=False
                    )
                    continue
                if payouts is None or token_id not in payouts:
                    continue

                payout = payouts[token_id]
                credited = self.db.settle_resolved_position(trader["proxy_address"], token_id, payout)
                market = f"'{pos.get('market_title') or 'Mercado Polymarket'}' ({pos.get('outcome') or ''})"
                if payout > 0:
                    msg = f"Mercado resuelto a favor ({trader['alias']}): {market}. Se abonan ${credited:.2f} USDC a la sub-banca."
                    if not self.cfg.dry_run:
                        msg += " El bot no cobra en la blockchain: reclama las ganancias en polymarket.com (botón Claim)."
                    await self.notifier.notify(msg, "SUCCESS")
                else:
                    await self.notifier.notify(
                        f"Mercado resuelto en contra ({trader['alias']}): {market}. La posición se cierra en $0.",
                        "WARNING"
                    )

    async def scan_once(self, session):
        now = time.time()
        if now - self._last_resolution_check >= self.cfg.resolution_check_interval:
            self._last_resolution_check = now
            await self.settle_resolved_positions(session)

        active_traders = self.db.get_active_traders()
        if not active_traders:
            await self.notifier.notify(
                "No hay traders activos configurados (revisa traders.json o usa cli.py).",
                "WARNING", send_telegram=False
            )
            return

        for trader in active_traders:
            try:
                # Todas las operaciones desde la última vista, no solo las 10 últimas.
                since = int(trader.get("last_seen_timestamp", 0))
                trades, truncated = await self.api.get_leader_trades_since(session, trader["proxy_address"], since)
                if truncated:
                    await self.notifier.notify(
                        f"{trader['alias']} hizo más de {len(trades)} operaciones desde la última revisión: "
                        "solo se procesan las más recientes (sube MAX_ACTIVITY_PAGES si pasa a menudo).",
                        "WARNING"
                    )

                trades.sort(key=lambda t: self.normalize_timestamp(t.get("timestamp", 0)))
                stale = 0
                for trade_event in trades:
                    if await self.process_leader_trade(session, trader, trade_event) == "stale":
                        stale += 1
                if stale:
                    await self.notifier.notify(
                        f"Se omitieron {stale} operaciones de {trader['alias']} con más de "
                        f"{self.cfg.max_trade_age_minutes} min de antigüedad.",
                        "WARNING"
                    )
            except Exception as e:
                await self.notifier.notify(f"Error monitoreando a {trader['alias']}: {e}", "ERROR")

    async def run(self, once: bool = False, max_runtime_minutes: Optional[int] = None):
        mode_str = "SIMULACIÓN (Paper Trading)" if self.cfg.dry_run else "PRODUCCIÓN (Dinero Real)"
        exec_type = "Ciclo Único (--once)" if once else "Monitoreo Continuo"
        # En GitHub Actions (--once) el bot arranca cada pocos minutos: el aviso de arranque
        # solo va a la consola para no llenar Telegram.
        await self.notifier.notify(
            f"Iniciando Polymarket CopyBot | Modo: {mode_str} | Ejecución: {exec_type}",
            "INFO", send_telegram=not once
        )

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
