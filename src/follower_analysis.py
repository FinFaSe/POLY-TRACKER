"""
¿Le "empujan" el precio sus seguidores a un trader?

Para cada operación reciente del trader se mira qué hicieron otras carteras con el mismo
resultado (token) justo después y justo antes:

- Seguidores recurrentes: carteras que operan en el mismo sentido poco después que él,
  una y otra vez y en mercados distintos. Es la huella del copy-trading.
- Volumen de esos seguidores frente al del trader: si es pequeño, no pueden mover el precio.
- Cómo se mueve el precio después de su operación, frente a cómo venía moviéndose antes
  (si ya subía, la subida posterior no se debe a los seguidores).
- Si el trader deshace la posición (vende lo comprado) con ganancia mientras sus seguidores
  aún compran: el patrón de usar a los seguidores como compradores.

Es una correlación, no una prueba: el precio también se mueve por noticias o por la
propia orden del trader, que se lleva las mejores ofertas del libro.
"""
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

# Umbrales de los veredictos (documentados en el informe).
RECURRING_MIN_TRADES = 3       # un seguidor recurrente sigue al menos 3 operaciones…
RECURRING_MIN_MARKETS = 2      # …en al menos 2 mercados distintos
PUSH_MIN_EXTRA_DRIFT = 0.01    # el precio se mueve al menos 1 punto % más que la tendencia previa
PUSH_MIN_VOLUME_RATIO = 0.5    # los copiadores mueven al menos la mitad del volumen del trader
PUSH_MIN_AFTER_VS_BEFORE = 1.5  # y después de operar hay claramente más volumen que antes (1,5 veces)
# Un seguidor recurrente es "copiador probable" si opera rápido tras el trader o lo hace con
# mucha constancia, y no es él mismo un bot de alta frecuencia (que coincide en los mismos
# mercados por operar sin parar, no por copiar).
COPIER_MAX_MEDIAN_DELAY = 120  # segundos
COPIER_MIN_SHARE = 0.25        # o sigue al menos el 25 % de sus operaciones
HIGH_FREQUENCY_TRADES = 300    # más de esto en 7 días = bot de alta frecuencia


@dataclass
class TradeEffect:
    market: str
    outcome: str
    token: str
    condition_id: str
    side: str
    ts: int
    price: float
    usdc: float
    complete: bool = True
    followers: Dict[str, int] = field(default_factory=dict)   # cartera -> segundos de retraso
    follower_usdc: float = 0.0
    follower_usdc_by_wallet: Dict[str, float] = field(default_factory=dict)
    before_wallets: int = 0
    before_usdc: float = 0.0
    drift_after: Optional[float] = None    # a favor del trader: + si compró y subió / vendió y bajó
    drift_before: Optional[float] = None   # tendencia previa, mismo signo
    exit_price: Optional[float] = None
    exit_ts: Optional[int] = None
    followers_before_exit: int = 0


@dataclass
class FollowerReport:
    address: str
    days: int
    window_minutes: int
    effects: List[TradeEffect]
    skipped_incomplete: int
    recurring: List[dict]
    verdict: str
    details: List[str]


def _ts(api, trade) -> int:
    return api._timestamp_seconds(trade.get("timestamp", 0))


def _usdc(trade) -> float:
    try:
        value = trade.get("usdcSize")
        if value is not None:
            return float(value)
        return float(trade.get("size") or 0) * float(trade.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _signed_drift(side: str, start: float, end: float) -> Optional[float]:
    if not start:
        return None
    move = (end - start) / start
    return move if side == "BUY" else -move


def _last_price(trades: List[dict], api, lo: int, hi: int) -> Optional[float]:
    """Precio de la última operación con ts en (lo, hi]."""
    inside = [t for t in trades if lo < _ts(api, t) <= hi and t.get("price") is not None]
    if not inside:
        return None
    return float(max(inside, key=lambda t: _ts(api, t))["price"])


async def analyze_followers(api, session, address: str, days: int = 7, window_minutes: int = 10,
                            max_trades: int = 60, max_pages_per_market: int = 30) -> FollowerReport:
    address = address.lower()
    window = window_minutes * 60
    since = int(time.time()) - days * 86400
    leader_trades, _ = await api.get_leader_trades_since(session, address, since)
    leader_trades = [t for t in leader_trades if _ts(api, t) >= since and str(t.get("side", "")).upper() in ("BUY", "SELL")]
    leader_trades.sort(key=lambda t: _ts(api, t))
    analyzed = leader_trades[-max_trades:]

    by_market: Dict[str, List[dict]] = defaultdict(list)
    for t in analyzed:
        by_market[str(t.get("conditionId") or "")].append(t)

    effects: List[TradeEffect] = []
    skipped = 0
    for condition_id, trades in by_market.items():
        if not condition_id:
            continue
        start = min(_ts(api, t) for t in trades) - window
        end = max(_ts(api, t) for t in trades) + window
        market_trades, truncated = await api.get_market_trades(session, condition_id, start, max_pages_per_market, end_ts=end)
        others = [m for m in market_trades if str(m.get("proxyWallet") or m.get("proxy_wallet") or "").lower() != address]
        oldest_loaded = min((_ts(api, m) for m in market_trades), default=None)

        for t in trades:
            ts, token, side = _ts(api, t), str(t.get("asset") or ""), str(t.get("side")).upper()
            effect = TradeEffect(
                market=str(t.get("title") or "Mercado"), outcome=str(t.get("outcome") or ""), token=token,
                condition_id=condition_id, side=side, ts=ts, price=float(t.get("price") or 0), usdc=_usdc(t),
            )
            # Con datos truncados, las operaciones más antiguas del mercado pueden no estar cargadas.
            if truncated and (oldest_loaded is None or oldest_loaded > ts - window):
                effect.complete = False
                skipped += 1
                continue
            same_token = [m for m in others if str(m.get("asset") or "") == token]
            after = [m for m in same_token if ts < _ts(api, m) <= ts + window and str(m.get("side")).upper() == side]
            before = [m for m in same_token if ts - window <= _ts(api, m) < ts and str(m.get("side")).upper() == side]
            for m in after:
                wallet = str(m.get("proxyWallet") or m.get("proxy_wallet") or "").lower()
                if wallet and wallet not in effect.followers:
                    effect.followers[wallet] = _ts(api, m) - ts
                if wallet:
                    effect.follower_usdc_by_wallet[wallet] = effect.follower_usdc_by_wallet.get(wallet, 0.0) + _usdc(m)
            effect.follower_usdc = sum(_usdc(m) for m in after)
            effect.before_wallets = len({str(m.get("proxyWallet") or "").lower() for m in before})
            effect.before_usdc = sum(_usdc(m) for m in before)

            price_after = _last_price(same_token, api, ts, ts + window)
            price_before = _last_price(same_token, api, ts - window - 1, ts - 1)
            if price_after is not None:
                effect.drift_after = _signed_drift(side, effect.price, price_after)
            if price_before is not None:
                effect.drift_before = _signed_drift(side, price_before, effect.price)

            # ¿Deshizo la posición después (vender lo comprado o recomprar lo vendido)?
            opposite = "SELL" if side == "BUY" else "BUY"
            exits = [x for x in leader_trades if str(x.get("asset") or "") == token
                     and str(x.get("side")).upper() == opposite and _ts(api, x) > ts]
            if exits:
                exit_trade = min(exits, key=lambda x: _ts(api, x))
                effect.exit_ts = _ts(api, exit_trade)
                effect.exit_price = float(exit_trade.get("price") or 0)
                effect.followers_before_exit = len({
                    str(m.get("proxyWallet") or "").lower() for m in same_token
                    if ts < _ts(api, m) < effect.exit_ts and str(m.get("side")).upper() == side
                })
            effects.append(effect)

    recurring = _recurring_followers(effects)
    await _classify_followers(api, session, recurring)
    verdict, details = _verdict(effects, recurring)
    return FollowerReport(address, days, window_minutes, effects, skipped, recurring, verdict, details)


def _recurring_followers(effects: List[TradeEffect]) -> List[dict]:
    trades, markets, delays = Counter(), defaultdict(set), defaultdict(list)
    for e in effects:
        for wallet, delay in e.followers.items():
            trades[wallet] += 1
            markets[wallet].add(e.condition_id)
            delays[wallet].append(delay)
    recurring = [
        {"wallet": w, "trades": n, "markets": len(markets[w]),
         "share": n / len(effects) if effects else 0.0, "median_delay": statistics.median(delays[w])}
        for w, n in trades.items()
        if n >= RECURRING_MIN_TRADES and len(markets[w]) >= RECURRING_MIN_MARKETS
    ]
    return sorted(recurring, key=lambda r: (-r["trades"], r["median_delay"]))


async def _classify_followers(api, session, recurring: List[dict], limit: int = 15):
    """Marca qué seguidores recurrentes son bots de alta frecuencia y cuáles, copiadores probables."""
    for r in recurring:
        r["high_frequency"] = None
        if recurring.index(r) < limit:
            try:
                count, capped, _ = await api.count_recent_trades(session, r["wallet"], days=7)
                r["high_frequency"] = capped or count > HIGH_FREQUENCY_TRADES
            except Exception:
                pass
        r["copier"] = (
            r["high_frequency"] is not True
            and (r["median_delay"] <= COPIER_MAX_MEDIAN_DELAY or r["share"] >= COPIER_MIN_SHARE)
        )


def _follower_kind(r: dict) -> str:
    if r.get("high_frequency"):
        return "bot de alta frecuencia (coincide, no copia)"
    if r.get("copier"):
        return "copiador probable"
    return "coincide a veces"


def _mean(values: List[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def summary(effects: List[TradeEffect], copiers: Iterable[str] = ()) -> dict:
    copiers = set(copiers)
    with_price = [e for e in effects if e.drift_after is not None]
    paired = [e for e in effects if e.drift_after is not None and e.drift_before is not None]
    exits = [e for e in effects if e.side == "BUY" and e.exit_price is not None]
    leader_usdc = sum(e.usdc for e in effects)
    follower_usdc = sum(e.follower_usdc for e in effects)
    before_usdc = sum(e.before_usdc for e in effects)
    copier_usdc = sum(v for e in effects for w, v in e.follower_usdc_by_wallet.items() if w in copiers)
    return {
        "trades": len(effects),
        "with_followers": sum(1 for e in effects if e.followers),
        "median_followers": statistics.median([len(e.followers) for e in effects]) if effects else 0,
        "leader_usdc": leader_usdc,
        "follower_usdc": follower_usdc,
        "before_usdc": before_usdc,
        "volume_ratio": follower_usdc / leader_usdc if leader_usdc else None,
        "after_vs_before": follower_usdc / before_usdc if before_usdc else (None if not follower_usdc else float("inf")),
        "copier_usdc": copier_usdc,
        "copier_ratio": copier_usdc / leader_usdc if leader_usdc else None,
        "drift_after": _mean([e.drift_after for e in with_price]),
        "drift_before": _mean([e.drift_before for e in paired]),
        "extra_drift": _mean([e.drift_after - e.drift_before for e in paired]),
        "paired": len(paired),
        "exits": len(exits),
        "profitable_exits_with_followers": sum(
            1 for e in exits if e.exit_price > e.price and e.followers_before_exit > 0
        ),
    }


def _verdict(effects: List[TradeEffect], recurring: List[dict]):
    if not effects:
        return "Sin datos suficientes", ["No hay operaciones del trader con datos completos en el periodo elegido."]
    copiers = [r for r in recurring if r.get("copier")]
    s = summary(effects, [r["wallet"] for r in copiers])
    price_moves = s["extra_drift"] is not None and s["extra_drift"] >= PUSH_MIN_EXTRA_DRIFT
    more_volume_after = s["after_vs_before"] is not None and s["after_vs_before"] >= PUSH_MIN_AFTER_VS_BEFORE
    copiers_big = s["copier_ratio"] is not None and s["copier_ratio"] >= PUSH_MIN_VOLUME_RATIO
    details = []
    if copiers and price_moves and more_volume_after and copiers_big:
        verdict = "Hay indicios compatibles con que quienes le copian le empujan el precio"
        if s["profitable_exits_with_followers"]:
            details.append(
                f"En {s['profitable_exits_with_followers']} de {s['exits']} compras que luego vendió, lo hizo más caro "
                "mientras otras carteras seguían comprando."
            )
    elif copiers:
        verdict = "Tiene copiadores probables, pero no se aprecia que le muevan el precio"
        reasons = []
        if not copiers_big:
            reasons.append(f"mueven {s['copier_ratio'] or 0:.1f} veces su volumen (hace falta {PUSH_MIN_VOLUME_RATIO})")
        if not more_volume_after:
            reasons.append("después de operar no hay claramente más volumen que antes")
        if not price_moves:
            reasons.append("el precio no se mueve a su favor más que la tendencia previa")
        details.append("Motivo: " + "; ".join(reasons) + ".")
    elif recurring:
        verdict = "Coincide a menudo con otras carteras, pero no hay copiadores claros"
        details.append("Quienes aparecen tras sus operaciones son bots de alta frecuencia o coinciden de forma esporádica.")
    elif price_moves:
        verdict = "El precio se mueve a su favor después de operar, sin seguidores que lo expliquen"
        details.append("Más compatible con información, tendencia o el efecto de su propia orden que con seguidores.")
    else:
        verdict = "No hay indicios de seguidores que le empujen el precio"
    if price_moves and not more_volume_after:
        details.append("El precio se movió a su favor sin un aumento de volumen tras sus operaciones: "
                       "más compatible con un rebote o con el impacto de su propia orden.")
    details.append("Es una correlación, no una prueba: el precio también se mueve por noticias o por la propia orden del trader.")
    return verdict, details


def _pct(value: Optional[float]) -> str:
    return "?" if value is None else f"{value * 100:+.2f}%"


def _money(value: float) -> str:
    return f"${value:,.0f}"


def render_markdown(report: FollowerReport) -> str:
    copiers = [r["wallet"] for r in report.recurring if r.get("copier")]
    s = summary(report.effects, copiers)
    ratio = "?" if s["volume_ratio"] is None else f"{s['volume_ratio']:.1f} veces el del trader"
    after_before = ("?" if s["after_vs_before"] is None else
                    "sin volumen antes" if s["after_vs_before"] == float("inf") else f"{s['after_vs_before']:.1f}×")
    copier_ratio = "?" if s["copier_ratio"] is None else f"{s['copier_ratio']:.1f} veces el del trader"
    lines = [
        f"## ¿Le empujan el precio sus seguidores? `{report.address}`",
        "",
        f"**Veredicto: {report.verdict}.**",
        "",
    ]
    lines += [f"- {d}" for d in report.details]
    lines += [
        "",
        f"Operaciones analizadas: {s['trades']} de los últimos {report.days} días "
        f"(ventana de {report.window_minutes} min antes y después de cada una)"
        + (f"; {report.skipped_incomplete} sin datos completos del mercado." if report.skipped_incomplete else "."),
        "",
        "| Indicador | Valor |",
        "|---|---|",
        f"| Operaciones con al menos un seguidor | {s['with_followers']} de {s['trades']} |",
        f"| Seguidores por operación (mediana) | {s['median_followers']} |",
        f"| Volumen del trader | {_money(s['leader_usdc'])} |",
        f"| Volumen de otras carteras en el mismo sentido, después | {_money(s['follower_usdc'])} ({ratio}) |",
        f"| Mismo volumen en la ventana anterior (referencia) | {_money(s['before_usdc'])} |",
        f"| Volumen después / antes | {after_before} |",
        f"| Volumen de los copiadores probables, después | {_money(s['copier_usdc'])} ({copier_ratio}) |",
        f"| Movimiento del precio después, a su favor (media) | {_pct(s['drift_after'])} |",
        f"| Tendencia que ya traía antes (media) | {_pct(s['drift_before'])} |",
        f"| Movimiento extra tras operar (después − antes) | {_pct(s['extra_drift'])} |",
        f"| Compras deshechas con ganancia mientras otros compraban | {s['profitable_exits_with_followers']} de {s['exits']} |",
        "",
    ]
    if report.recurring:
        lines += [
            f"### Seguidores recurrentes (al menos {RECURRING_MIN_TRADES} operaciones en {RECURRING_MIN_MARKETS} mercados)",
            "",
            "| Cartera | Operaciones seguidas | Mercados | Retraso mediano | Tipo |",
            "|---|---|---|---|---|",
        ]
        for r in report.recurring[:15]:
            lines.append(f"| `{r['wallet']}` | {r['trades']} ({r['share'] * 100:.0f}%) | {r['markets']} | "
                         f"{r['median_delay']:.0f} s | {_follower_kind(r)} |")
        lines.append("")
    else:
        lines += ["No se encontraron seguidores recurrentes.", ""]
    lines += [
        "Cómo se decide: un *seguidor recurrente* opera en el mismo sentido poco después que el trader en "
        f"al menos {RECURRING_MIN_TRADES} operaciones y {RECURRING_MIN_MARKETS} mercados; es *copiador probable* si no es "
        f"un bot de alta frecuencia (más de {HIGH_FREQUENCY_TRADES} operaciones en 7 días) y su retraso mediano es de "
        f"{COPIER_MAX_MEDIAN_DELAY} s o menos o sigue al menos el {COPIER_MIN_SHARE * 100:.0f} % de sus operaciones. "
        f"Hay *empuje* si, además, el precio se mueve a su favor al menos {PUSH_MIN_EXTRA_DRIFT * 100:.0f} punto % más "
        f"que la tendencia previa, después de operar hay al menos {PUSH_MIN_AFTER_VS_BEFORE} veces el volumen de antes "
        f"y los copiadores mueven al menos {PUSH_MIN_VOLUME_RATIO} veces el volumen del trader.",
    ]
    return "\n".join(lines) + "\n"
