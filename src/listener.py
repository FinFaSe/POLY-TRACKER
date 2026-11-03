import asyncio
import math
import os
import signal
import time
from collections import Counter
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
        self._removed_traders = self.db.sync_from_json(cfg.traders_json_path)
        self._traders_mtime = self._file_mtime(cfg.traders_json_path)
        self._warned_no_traders = False
        self._stop = None  # asyncio.Event, creado en run()
        self.api = PolymarketClientWrapper(cfg)
        self.sizer = ProportionalSizingEngine(
            self.db,
            min_order_usdc=cfg.min_order_usdc,
            max_position_weight_pct=cfg.max_position_weight_pct
        )
        self.notifier = Notifier(cfg)
        self._last_resolution_check = 0.0
        self._last_heartbeat = time.time()
        self._equity_cache = {}
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

    async def get_own_cash(self, session) -> Optional[float]:
        """USDC real de tu cartera de Polymarket (POLYMARKET_PROXY_ADDRESS). None si no se puede leer."""
        if not self.cfg.proxy_address:
            return None
        return await self.api.get_cash_balance(session, self.cfg.proxy_address)

    async def check_real_balance(self, session, once: bool):
        """En modo real, avisa si las sub-bancas suman más efectivo del que hay en la cartera."""
        if self.cfg.dry_run:
            return
        own_cash = await self.get_own_cash(session)
        assigned = sum(t["available_cash"] for t in self.db.get_active_traders())
        if own_cash is None:
            await self.notifier.notify(
                "No se pudo leer el saldo real de tu cartera: las compras se omitirán hasta que se pueda "
                "(revisa POLYMARKET_PROXY_ADDRESS y POLYGON_RPC_URL).", "ERROR", send_telegram=not once
            )
        elif assigned > own_cash + 0.01:
            await self.notifier.notify(
                f"Las sub-bancas activas tienen ${assigned:.2f} USDC disponibles, pero tu cartera solo tiene "
                f"${own_cash:.2f}. Las compras que no quepan en el saldo real se omitirán.",
                "WARNING", send_telegram=not once
            )

    async def get_leader_total_equity(self, session, proxy_address: str) -> Optional[float]:
        """
        Patrimonio total del líder: valor de sus posiciones + su efectivo (pUSD y USDC.e).
        Sin el efectivo, un líder con mucha liquidez parecería apostar una parte mucho mayor
        de su capital. Devuelve None si falta cualquiera de los dos datos.

        Se reutiliza durante EQUITY_CACHE_SECONDS: calcularlo recorre todas las posiciones
        del líder (varios segundos en carteras grandes), y un trader muy activo puede hacer
        decenas de compras seguidas, con un patrimonio que apenas cambia entre ellas.
        """
        cached = self._equity_cache.get(proxy_address)
        if cached and time.time() - cached[0] < self.cfg.equity_cache_seconds:
            return cached[1]
        positions_value = await self.api.get_leader_portfolio_value(session, proxy_address)
        if positions_value is None:
            return None
        cash = await self.api.get_leader_cash_balance(session, proxy_address)
        if cash is None:
            return None
        self._equity_cache[proxy_address] = (time.time(), positions_value + cash)
        return positions_value + cash

    async def process_leader_trade(self, session, trader: Dict, trade_event: Dict) -> str:
        """
        Procesa una operación del líder. Devuelve el estado con que quedó registrada
        (SIMULATED, EXECUTED, FAILED o SKIPPED_*), o "ignored" si ya estaba vista o no es válida.
        Los descartes no se envían uno a uno a Telegram: scan_once los resume por trader.
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
            return "SKIPPED_STALE"

        await self.notifier.notify(
            f"Movimiento detectado de {trader['alias']}: {side} ${leader_usdc:.2f} USDC en '{market_title}' ({outcome}) a ${leader_price:.3f}",
            "INFO", send_telegram=False
        )

        market_price = await self.api.get_best_orderbook_price(session, token_id, side)
        if market_price is None:
            if side == "BUY":
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, 0.0, "SKIPPED_NO_LIQUIDITY", "Libro de órdenes vacío"
                )
            else:
                self.db.record_sell_execution(
                    trade_id, trader["proxy_address"], token_id,
                    leader_usdc, leader_price, 0.0, 0.0, "SKIPPED_NO_LIQUIDITY", "Libro de órdenes vacío"
                )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            await self.notifier.notify(f"Operación descartada ({trader['alias']}): Sin liquidez en el libro de órdenes.", "WARNING", send_telegram=False)
            return "SKIPPED_NO_LIQUIDITY"

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
            await self.notifier.notify(f"Operación descartada ({trader['alias']}): {reason}", "WARNING", send_telegram=False)
            return "SKIPPED_SLIPPAGE"

        if side == "BUY":
            leader_equity = await self.get_leader_total_equity(session, trader["proxy_address"])
            if leader_equity is None:
                reason = "No se pudo leer el patrimonio del líder (posiciones + efectivo); revisa POLYGON_RPC_URL"
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, market_price, "SKIPPED_NO_EQUITY", reason
                )
                self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                await self.notifier.notify(f"Compra omitida ({trader['alias']}): {reason}", "WARNING", send_telegram=False)
                return "SKIPPED_NO_EQUITY"
            subbank_pos_val = await self.estimate_subbank_positions_value(session, trader["proxy_address"])
            decision = self.sizer.evaluate_buy(trader, leader_usdc, leader_equity, subbank_pos_val)

            if not decision.should_execute:
                self.db.record_buy_execution(
                    trade_id, trader["proxy_address"], token_id, market_title, outcome,
                    leader_usdc, leader_price, 0.0, market_price, "SKIPPED_SIZING", decision.reason
                )
                self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                await self.notifier.notify(f"Compra omitida ({trader['alias']}): {decision.reason}", "WARNING", send_telegram=False)
                return "SKIPPED_SIZING"

            # Las sub-bancas son cifras en la base de datos: en modo real, comprobar que la
            # cartera tiene de verdad el USDC antes de comprar.
            if not self.cfg.dry_run:
                own_cash = await self.get_own_cash(session)
                if own_cash is None or own_cash < decision.usdc_amount:
                    reason = (
                        "No se pudo leer el saldo real de tu cartera (revisa POLYMARKET_PROXY_ADDRESS y POLYGON_RPC_URL)"
                        if own_cash is None else
                        f"Saldo real insuficiente: tu cartera tiene ${own_cash:.2f} USDC y la orden necesita "
                        f"${decision.usdc_amount:.2f}. Revisa los bank de traders.json"
                    )
                    self.db.record_buy_execution(
                        trade_id, trader["proxy_address"], token_id, market_title, outcome,
                        leader_usdc, leader_price, 0.0, market_price, "SKIPPED_NO_FUNDS", reason
                    )
                    self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                    await self.notifier.notify(f"Compra omitida ({trader['alias']}): {reason}", "ERROR", send_telegram=False)
                    return "SKIPPED_NO_FUNDS"

            result = await self.api.execute_order(
                token_id=token_id, side="BUY", price=market_price, usdc_amount=decision.usdc_amount,
                limit_price=self.api.limit_price(leader_price, "BUY"),
            )
            status = ("SIMULATED" if self.cfg.dry_run else "EXECUTED") if result.success else "FAILED"
            # Se registra lo realmente ejecutado; si falló, lo solicitado (no mueve saldos).
            self.db.record_buy_execution(
                trade_id, trader["proxy_address"], token_id, market_title, outcome,
                leader_usdc, leader_price,
                result.filled_usdc if result.success else decision.usdc_amount,
                result.avg_price if result.success else market_price,
                status, result.message, condition_id=condition_id
            )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            await self.notifier.notify(f"{decision.reason} | {result.message}", "SUCCESS" if result.success else "ERROR")
            return status

        elif side == "SELL":
            leader_remaining = await self.api.get_leader_token_balance(session, trader["proxy_address"], token_id)
            if leader_remaining is None:
                # Sin este dato no se puede calcular qué parte vendió; antes contaba como 0 y se
                # vendía todo. La excepción deja la operación sin procesar para el próximo ciclo.
                raise RuntimeError(f"No se pudo leer cuántas shares le quedan a {trader['alias']} en '{market_title}'")
            decision = self.sizer.evaluate_sell(trader, token_id, leader_shares, leader_remaining, market_price)

            if not decision.should_execute:
                self.db.record_sell_execution(
                    trade_id, trader["proxy_address"], token_id,
                    leader_usdc, leader_price, 0.0, market_price, "SKIPPED_SIZING", decision.reason
                )
                self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                await self.notifier.notify(f"Venta omitida ({trader['alias']}): {decision.reason}", "WARNING", send_telegram=False)
                return "SKIPPED_SIZING"

            shares_to_sell = decision.shares_to_sell
            note = ""
            # En modo real, no intentar vender más shares de las que la cartera tiene de verdad
            # (por ejemplo, si vendiste a mano): la orden FOK fallaría entera.
            if not self.cfg.dry_run and self.cfg.proxy_address:
                onchain = await self.api.get_onchain_shares(session, self.cfg.proxy_address, token_id)
                if onchain is not None and onchain + 1e-6 < shares_to_sell:
                    if onchain < 0.01:
                        reason = (f"La base de datos indica {shares_to_sell:.2f} shares de la sub-banca, "
                                  "pero tu cartera no tiene ninguna de este resultado")
                        self.db.record_sell_execution(
                            trade_id, trader["proxy_address"], token_id,
                            leader_usdc, leader_price, 0.0, market_price, "SKIPPED_NO_SHARES", reason
                        )
                        self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
                        await self.notifier.notify(f"Venta omitida ({trader['alias']}): {reason}", "ERROR", send_telegram=False)
                        return "SKIPPED_NO_SHARES"
                    note = f" (limitada a las {onchain:.2f} shares que hay en tu cartera)"
                    shares_to_sell = math.floor(onchain * 100) / 100

            result = await self.api.execute_order(
                token_id=token_id, side="SELL", price=market_price, shares=shares_to_sell,
                limit_price=self.api.limit_price(leader_price, "SELL"),
            )
            status = ("SIMULATED" if self.cfg.dry_run else "EXECUTED") if result.success else "FAILED"
            self.db.record_sell_execution(
                trade_id, trader["proxy_address"], token_id, leader_usdc, leader_price,
                result.filled_shares if result.success else shares_to_sell,
                result.avg_price if result.success else market_price,
                status, result.message + note
            )
            self.db.update_last_seen_timestamp(trader["proxy_address"], trade_timestamp)
            await self.notifier.notify(f"{decision.reason}{note} | {result.message}", "SUCCESS" if result.success else "ERROR")
            return status

        return "ignored"

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
            # En modo continuo esto se comprueba cada pocos segundos: avisar solo una vez.
            if not self._warned_no_traders:
                self._warned_no_traders = True
                await self.notifier.notify(
                    "No hay traders activos configurados (revisa traders.json o usa cli.py).",
                    "WARNING", send_telegram=False
                )
            return
        self._warned_no_traders = False

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
                outcomes = Counter()
                for trade_event in trades:
                    outcomes[await self.process_leader_trade(session, trader, trade_event)] += 1
                await self.notify_skipped_summary(trader, outcomes)
            except Exception as e:
                await self.notifier.notify(f"Error monitoreando a {trader['alias']}: {e}", "ERROR")

    SKIP_REASONS = {
        "SKIPPED_SIZING": "por tamaño (menor que el mínimo o sin posición que vender)",
        "SKIPPED_SLIPPAGE": "porque el precio se movió más que el slippage permitido",
        "SKIPPED_NO_LIQUIDITY": "por falta de liquidez",
        "SKIPPED_STALE": "por antigüedad",
        "SKIPPED_NO_EQUITY": "por no poder leer el patrimonio del líder",
        "SKIPPED_NO_FUNDS": "por saldo insuficiente en tu cartera",
        "SKIPPED_NO_SHARES": "porque tu cartera no tiene esas shares",
    }
    ACTIONABLE_SKIPS = {"SKIPPED_NO_EQUITY", "SKIPPED_NO_FUNDS", "SKIPPED_NO_SHARES"}

    async def notify_skipped_summary(self, trader: Dict, outcomes: Counter):
        """
        Un solo aviso a Telegram por trader y ciclo con las operaciones no copiadas. Un trader
        muy activo puede hacer cientos de operaciones en media hora: avisar de cada descarte
        llenaría Telegram (y superaría sus límites de envío).
        """
        skipped = {k: v for k, v in outcomes.items() if k.startswith("SKIPPED_")}
        if not skipped:
            return
        considered = sum(v for k, v in outcomes.items() if k != "ignored")
        copied = outcomes.get("SIMULATED", 0) + outcomes.get("EXECUTED", 0)
        details = ", ".join(
            f"{n} {self.SKIP_REASONS.get(k, k)}"
            + (f" (más de {self.cfg.max_trade_age_minutes} min)" if k == "SKIPPED_STALE" else "")
            for k, n in sorted(skipped.items(), key=lambda kv: -kv[1])
        )
        level = "ERROR" if self.ACTIONABLE_SKIPS & set(skipped) else "WARNING"
        await self.notifier.notify(
            f"{trader['alias']}: {considered} operaciones nuevas, {copied} copiadas. No copiadas: {details}.",
            level
        )

    @staticmethod
    def _file_mtime(path: str) -> Optional[float]:
        try:
            return os.stat(path).st_mtime
        except OSError:
            return None

    async def reload_traders_if_changed(self):
        """En modo continuo, aplica los cambios de traders.json sin reiniciar el bot."""
        mtime = self._file_mtime(self.cfg.traders_json_path)
        if mtime is None or mtime == self._traders_mtime:
            return
        self._traders_mtime = mtime
        try:
            removed = self.db.sync_from_json(self.cfg.traders_json_path)
        except Exception as e:
            await self.notifier.notify(f"traders.json tiene un error y no se ha aplicado: {e}", "ERROR")
            return
        active = ", ".join(t["alias"] for t in self.db.get_active_traders()) or "ninguno"
        await self.notifier.notify(f"traders.json recargado. Traders activos: {active}", "INFO")
        if removed:
            await self.notifier.notify(
                f"Desactivados porque ya no están en traders.json: {', '.join(removed)}", "WARNING"
            )

    async def send_heartbeat_if_due(self):
        """Aviso periódico de que el bot sigue vivo (HEARTBEAT_HOURS; 0 lo desactiva)."""
        hours = self.cfg.heartbeat_hours
        if hours <= 0 or time.time() - self._last_heartbeat < hours * 3600:
            return
        self._last_heartbeat = time.time()
        traders = self.db.get_active_traders()
        cash = sum(t["available_cash"] for t in traders)
        await self.notifier.notify(
            f"Sigo funcionando. Traders activos: {len(traders)}; efectivo disponible en sus sub-bancas: ${cash:.2f}.",
            "INFO"
        )

    def request_stop(self):
        """Pide al bucle continuo que termine al acabar el ciclo en curso (SIGTERM, SIGINT)."""
        if self._stop is not None:
            self._stop.set()

    def _install_signal_handlers(self):
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows o hilo secundario: queda Ctrl+C como antes.

    async def run(self, once: bool = False, max_runtime_minutes: Optional[int] = None):
        mode_str = "SIMULACIÓN (Paper Trading)" if self.cfg.dry_run else "PRODUCCIÓN (Dinero Real)"
        exec_type = "Ciclo Único (--once)" if once else "Monitoreo Continuo"
        # En GitHub Actions (--once) el bot arranca cada pocos minutos: el aviso de arranque
        # solo va a la consola para no llenar Telegram.
        await self.notifier.notify(
            f"Iniciando Polymarket CopyBot | Modo: {mode_str} | Ejecución: {exec_type}",
            "INFO", send_telegram=not once
        )
        if self._removed_traders:
            await self.notifier.notify(
                f"Desactivados porque ya no están en traders.json: {', '.join(self._removed_traders)}",
                "WARNING"
            )

        start_time = time.time()
        async with AsyncExitStack() as stack:
            session = await stack.enter_async_context(aiohttp.ClientSession()) if aiohttp is not None else None
            await self.check_real_balance(session, once)
            if once:
                await self.scan_once(session)
                return

            self._stop = asyncio.Event()
            self._install_signal_handlers()
            consecutive_errors = 0
            while not self._stop.is_set():
                await self.reload_traders_if_changed()
                try:
                    await self.scan_once(session)
                    consecutive_errors = 0
                except Exception as e:
                    # Un fallo puntual (red, API) no debe tumbar el bot: se avisa y se reintenta.
                    consecutive_errors += 1
                    await self.notifier.notify(
                        f"Error en el ciclo de monitoreo ({consecutive_errors} seguidos): {e}", "ERROR",
                        send_telegram=consecutive_errors == 1 or consecutive_errors % 50 == 0
                    )
                await self.send_heartbeat_if_due()
                if max_runtime_minutes and (time.time() - start_time) >= (max_runtime_minutes * 60):
                    await self.notifier.notify(f"Tiempo límite alcanzado ({max_runtime_minutes} min). Finalizando ciclo limpiamente.", "INFO")
                    break
                # Tras errores seguidos, esperar más (hasta 5 min) para no saturar la API.
                delay = self.cfg.poll_interval if not consecutive_errors else min(
                    300, self.cfg.poll_interval * 2 ** min(consecutive_errors, 8)
                )
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            if self._stop.is_set():
                await self.notifier.notify("Bot detenido de forma ordenada.", "INFO")
