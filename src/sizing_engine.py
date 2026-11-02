from dataclasses import dataclass
from typing import Dict, Optional
from src.database import DatabaseManager

@dataclass
class CopyDecision:
    should_execute: bool
    side: str                  # 'BUY' o 'SELL'
    usdc_amount: float         # Monto en USDC a operar
    shares_to_sell: float      # Cantidad de shares a vender (si side == 'SELL')
    reason: str                # Explicación del cálculo o motivo de descarte

class ProportionalSizingEngine:
    """
    Motor de cálculo proporcional con aislamiento de sub-banca (bank).
    """
    def __init__(self, db: DatabaseManager, min_order_usdc: float = 2.0, max_position_weight_pct: float = 0.25):
        self.db = db
        self.min_order_usdc = min_order_usdc
        self.max_position_weight_pct = max_position_weight_pct

    def evaluate_buy(
        self,
        trader: Dict,
        leader_trade_usdc: float,
        leader_total_equity: float,
        current_subbank_positions_value: float
    ) -> CopyDecision:
        initial_bank = trader["initial_bank"]
        available_cash = trader["available_cash"]
        max_drawdown_pct = trader["max_drawdown_pct"]

        # 1. Verificar Stop-Loss de la sub-banca
        total_subbank_equity = available_cash + current_subbank_positions_value
        min_allowed_equity = initial_bank * (1.0 - max_drawdown_pct)
        if total_subbank_equity < min_allowed_equity:
            return CopyDecision(
                should_execute=False,
                side="BUY",
                usdc_amount=0.0,
                shares_to_sell=0.0,
                reason=f"Stop-loss de sub-banca activado: Capital actual (${total_subbank_equity:.2f}) menor al límite (${min_allowed_equity:.2f})"
            )

        # 2. Calcular proporción jugada por el líder
        effective_leader_equity = max(leader_total_equity, leader_trade_usdc)
        if effective_leader_equity <= 0:
            return CopyDecision(False, "BUY", 0.0, 0.0, "Portafolio del líder inválido o en cero")

        leader_weight = leader_trade_usdc / effective_leader_equity

        # Aplicar tope de riesgo por operación individual (ej. 25% máx del bank)
        capped_weight = min(leader_weight, self.max_position_weight_pct)

        # 3. Escalar a la sub-banca asignada a este trader
        raw_target_usdc = capped_weight * total_subbank_equity

        # 4. Limitar al efectivo disponible en esta sub-banca específica
        target_usdc = min(raw_target_usdc, available_cash)

        if target_usdc < self.min_order_usdc:
            return CopyDecision(
                should_execute=False,
                side="BUY",
                usdc_amount=0.0,
                shares_to_sell=0.0,
                reason=f"Orden proporcional (${target_usdc:.2f}, {leader_weight*100:.2f}% del bank) es menor al mínimo de ${self.min_order_usdc:.2f}"
            )

        return CopyDecision(
            should_execute=True,
            side="BUY",
            usdc_amount=round(target_usdc, 2),
            shares_to_sell=0.0,
            reason=f"Líder invirtió {leader_weight*100:.2f}% de su portafolio -> Copiando ${target_usdc:.2f} USDC del bank de '{trader['alias']}'"
        )

    def evaluate_sell(
        self,
        trader: Dict,
        token_id: str,
        leader_shares_sold: float,
        leader_remaining_shares: float,
        current_bid_price: float
    ) -> CopyDecision:
        sub_pos: Optional[Dict] = self.db.get_sub_position(trader["proxy_address"], token_id)
        if not sub_pos or sub_pos["shares"] <= 0.0001:
            return CopyDecision(
                should_execute=False,
                side="SELL",
                usdc_amount=0.0,
                shares_to_sell=0.0,
                reason=f"La sub-banca '{trader['alias']}' no posee acciones activas en este token"
            )

        our_shares = sub_pos["shares"]

        leader_shares_before = leader_shares_sold + max(0.0, leader_remaining_shares)
        if leader_shares_before <= 0:
            sell_ratio = 1.0
        else:
            sell_ratio = min(1.0, leader_shares_sold / leader_shares_before)

        if sell_ratio >= 0.95:
            sell_ratio = 1.0

        shares_to_sell = our_shares * sell_ratio
        estimated_usdc = shares_to_sell * current_bid_price

        if estimated_usdc < self.min_order_usdc and sell_ratio < 1.0:
            return CopyDecision(
                should_execute=False,
                side="SELL",
                usdc_amount=0.0,
                shares_to_sell=0.0,
                reason=f"Venta parcial estimada (${estimated_usdc:.2f}) menor al mínimo permitido (${self.min_order_usdc:.2f})"
            )

        return CopyDecision(
            should_execute=True,
            side="SELL",
            usdc_amount=round(estimated_usdc, 2),
            shares_to_sell=round(shares_to_sell, 4),
            reason=f"Líder vendió {sell_ratio*100:.1f}% de su posición -> Vendiendo {shares_to_sell:.2f} shares de la sub-banca '{trader['alias']}'"
        )
