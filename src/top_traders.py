"""
Búsqueda de traders para copiar en el ranking de Polymarket (Data API v2).

Además de ganancias y volumen, cuenta cuántas operaciones hizo cada uno en los
últimos 7 días: los bots de alta frecuencia (cientos de operaciones al día) no
se pueden copiar bien, porque sus operaciones son diminutas respecto a su
capital y el precio se mueve antes de que el bot llegue.
"""
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional

PERIODS = {"dia": "DAY", "semana": "WEEK", "mes": "MONTH", "todo": "ALL"}
PERIOD_LABELS = {"dia": "último día", "semana": "última semana", "mes": "último mes", "todo": "todo el historial"}
ORDERS = {"ganancias": "PNL", "volumen": "VOL"}
ACTIVITY_DAYS = 7
# Más de esto en 7 días (unas 40 al día) suele ser un bot de alta frecuencia.
HIGH_FREQUENCY_TRADES = 300
# Con --solo-copiables, cuántos puestos del ranking se revisan como máximo.
COPYABLE_SCAN = 100
# Por debajo de este margen (ganancias / volumen), la diferencia entre el precio de compra
# y el de venta que paga quien copia suele comerse toda la ganancia.
LOW_MARGIN = 0.03


@dataclass
class TraderSummary:
    rank: int
    name: str
    address: str
    pnl: float
    volume: float
    trades_7d: Optional[int] = None
    trades_capped: bool = False
    last_trade_ts: Optional[int] = None
    followed: bool = False

    @property
    def margin(self) -> Optional[float]:
        """Ganancias por cada dólar movido en el periodo."""
        return self.pnl / self.volume if self.volume > 0 else None

    @property
    def active(self) -> bool:
        return self.trades_7d is not None and self.trades_7d > 0

    def margin_label(self) -> str:
        m = self.margin
        if m is None:
            return "?"
        text = "más de 100%" if m > 1 else f"{m * 100:.1f}%"
        return text + (" ↓" if 0 <= m < LOW_MARGIN else "")

    @property
    def high_frequency(self) -> bool:
        return self.trades_7d is not None and (self.trades_capped or self.trades_7d > HIGH_FREQUENCY_TRADES)

    def activity_label(self) -> str:
        if self.trades_7d is None:
            return "?"
        text = f"más de {self.trades_7d}" if self.trades_capped else str(self.trades_7d)
        return text + (" ⚠️" if self.high_frequency else "")

    def last_trade_label(self, now: Optional[float] = None) -> str:
        if not self.last_trade_ts:
            return "?"
        hours = ((now or time.time()) - self.last_trade_ts) / 3600
        if hours < 1:
            return "hace menos de 1 h"
        if hours < 48:
            return f"hace {hours:.0f} h"
        return f"hace {hours / 24:.0f} días"


def _money(value: float) -> str:
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


def parse_leaderboard_row(row: dict, position: int) -> Optional[TraderSummary]:
    address = str(row.get("user_id") or row.get("proxy_wallet") or row.get("proxyWallet") or "").lower()
    if not address.startswith("0x"):
        return None
    name = str(row.get("user_name") or row.get("userName") or row.get("name") or row.get("pseudonym") or address[:10])
    return TraderSummary(
        rank=int(row.get("rank") or position),
        name=name,
        address=address,
        pnl=float(row.get("pnl") or 0.0),
        volume=float(row.get("volume") or row.get("vol") or 0.0),
    )


async def fetch_top_traders(api, session, period: str = "mes", order: str = "ganancias", limit: int = 15,
                            with_activity: bool = True, followed: Iterable[str] = (),
                            only_copyable: bool = False, scan: int = COPYABLE_SCAN) -> List[TraderSummary]:
    """
    Con only_copyable, recorre hasta 'scan' puestos del ranking y devuelve solo traders con
    ganancias y con alguna operación en los últimos 7 días (los bots se incluyen, marcados).
    """
    rows = await api.get_leaderboard(session, PERIODS[period], ORDERS[order], max(limit, scan) if only_copyable else limit)
    followed = {a.lower() for a in followed}
    traders = []
    for position, row in enumerate(rows, start=1):
        trader = parse_leaderboard_row(row, position)
        if trader is None:
            continue
        if only_copyable and trader.pnl <= 0:
            continue
        trader.followed = trader.address in followed
        if with_activity or only_copyable:
            try:
                trader.trades_7d, trader.trades_capped, trader.last_trade_ts = await api.count_recent_trades(
                    session, trader.address, days=ACTIVITY_DAYS
                )
            except Exception:
                pass  # se muestra "?" en lugar de detener la búsqueda
        if only_copyable and not trader.active:
            continue
        traders.append(trader)
        if len(traders) >= limit:
            break
    return traders[:limit]


NOTES = [
    "Ganancias y volumen corresponden al periodo elegido; las operaciones, a los últimos 7 días.",
    f"Margen = ganancias / volumen. ↓ = menos de un {LOW_MARGIN * 100:.0f}%: suele ser creación de mercado o arbitraje, "
    "donde la diferencia entre el precio de compra y el de venta que pagaría quien copia se come la ganancia.",
    f"⚠️ = más de {HIGH_FREQUENCY_TRADES} operaciones en 7 días: suele ser un bot de alta frecuencia. "
    "Copiarlo da órdenes demasiado pequeñas o tardías, y la mayoría se descartan.",
    "Mejor candidatos con ganancias en varios periodos (prueba también 'semana' y 'todo') y pocas operaciones grandes.",
    "Para seguir a uno: python cli.py add-trader --address <dirección> --alias <nombre> --bank <USDC>, "
    "o añádelo a traders.json desde la web de GitHub.",
]


def render_markdown(traders: List[TraderSummary], period: str, order: str, only_copyable: bool = False) -> str:
    title = f"## Mejores traders de Polymarket ({PERIOD_LABELS[period]}, por {order}"
    title += f", solo activos y con ganancias entre los {COPYABLE_SCAN} primeros)" if only_copyable else ")"
    lines = [
        title,
        "",
        "| # | Trader | Dirección | Ganancias | Volumen | Margen | Operaciones (7 días) | Última operación | |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    now = time.time()
    for t in traders:
        lines.append(
            f"| {t.rank} | {t.name.replace('|', '/')} | `{t.address}` | {_money(t.pnl)} | {_money(t.volume)} | "
            f"{t.margin_label()} | {t.activity_label()} | {t.last_trade_label(now)} | {'✔ ya lo sigues' if t.followed else ''} |"
        )
    lines.append("")
    lines.extend(f"- {note}" for note in NOTES)
    return "\n".join(lines) + "\n"


def render_rows(traders: List[TraderSummary]) -> List[List[str]]:
    now = time.time()
    return [
        [str(t.rank), t.name + (" ✔" if t.followed else ""), t.address, _money(t.pnl), _money(t.volume),
         t.margin_label(), t.activity_label(), t.last_trade_label(now)]
        for t in traders
    ]
