"""
Diagnóstico de solo lectura de las APIs que usa el bot.

Comprueba contra Polymarket y Polygon reales lo que el bot da por supuesto:
paginación de la actividad, valor de posiciones, efectivo en la blockchain,
filtro de posiciones por token, detección de mercados resueltos y libro de órdenes.

No envía órdenes, no usa claves privadas y no toca la base de datos del bot.

Uso:
    python scripts/diagnose_api.py [--address 0x...]

Sin --address elige automáticamente un trader con mucha actividad reciente.
"""
import argparse
import asyncio
import os
import sys
from collections import Counter
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import BotConfig  # noqa: E402
from src.polymarket_api import PolymarketClientWrapper  # noqa: E402

try:
    import aiohttp
except ImportError:
    aiohttp = None

NATIVE_USDC = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
EXPECTED_TRADE_FIELDS = ["transactionHash", "asset", "side", "timestamp", "size", "price", "usdcSize", "conditionId", "title", "outcome"]


class Report:
    def __init__(self):
        self.rows: List[tuple] = []

    def add(self, name: str, status: str, detail: str):
        self.rows.append((name, status, detail))
        print(f"[{status}] {name}: {detail}", flush=True)

    @property
    def failed(self) -> bool:
        return any(status == "FAIL" for _, status, _ in self.rows)

    def markdown(self) -> str:
        icons = {"OK": "✅", "WARN": "⚠️", "FAIL": "❌", "INFO": "ℹ️"}
        lines = ["## Diagnóstico de APIs de Polymarket", "", "| | Comprobación | Resultado |", "|---|---|---|"]
        for name, status, detail in self.rows:
            lines.append(f"| {icons.get(status, status)} | {name} | {detail.replace('|', '/')} |")
        return "\n".join(lines) + "\n"


def trade_key(t: Dict) -> str:
    return f"{t.get('transactionHash')}_{t.get('asset')}_{t.get('side')}_{t.get('timestamp')}"


async def activity_page(api, session, address: str, offset: int, limit: int = 100, kind: str = "TRADE"):
    return await api._get_json(
        session, f"{api.cfg.data_api_host}/activity",
        {"user": address.lower(), "type": kind, "limit": str(limit), "offset": str(offset)},
    )


async def pick_trader(api, session, report: Report) -> Optional[str]:
    """Elige, entre los que más operan ahora mismo, uno con al menos 100 operaciones (para probar la paginación)."""
    trades = await api._get_json(session, f"{api.cfg.data_api_host}/trades", {"limit": "500"})
    if not isinstance(trades, list) or not trades:
        report.add("Elegir trader activo", "FAIL", "La Data API (/trades) no devolvió operaciones recientes")
        return None
    wallets = Counter(str(t.get("proxyWallet", "")).lower() for t in trades if t.get("proxyWallet"))
    for wallet, _ in wallets.most_common(15):
        page = await activity_page(api, session, wallet, 0)
        if isinstance(page, list) and len(page) >= 100:
            report.add("Elegir trader activo", "INFO", f"{wallet} (≥100 operaciones en su historial)")
            return wallet
    wallet = wallets.most_common(1)[0][0]
    report.add("Elegir trader activo", "WARN", f"{wallet}: ninguno de los más activos tiene 100 operaciones; la paginación no se podrá probar del todo")
    return wallet


async def check_activity(api, session, address: str, report: Report) -> List[Dict]:
    page1 = await activity_page(api, session, address, 0)
    if not isinstance(page1, list):
        report.add("Actividad (/activity)", "FAIL", f"Respuesta inesperada: {type(page1).__name__}")
        return []
    if not page1:
        report.add("Actividad (/activity)", "WARN", "El trader no tiene operaciones")
        return []

    missing = [f for f in EXPECTED_TRADE_FIELDS if f not in page1[0]]
    report.add(
        "Campos de cada operación", "OK" if not missing else "FAIL",
        "todos presentes" if not missing else f"faltan: {', '.join(missing)}",
    )

    raw_ts = [float(t.get("timestamp") or 0) for t in page1]
    unit = "milisegundos" if max(raw_ts) > 1_000_000_000_000 else "segundos"
    ordered = all(raw_ts[i] >= raw_ts[i + 1] for i in range(len(raw_ts) - 1))
    report.add("Orden y unidad de timestamp", "OK" if ordered else "WARN",
               f"{'de más reciente a más antigua' if ordered else 'NO ordenada'}, en {unit}")

    if len(page1) < 100:
        report.add("Paginación con offset", "WARN", f"solo {len(page1)} operaciones: no hay segunda página que probar")
        return page1

    page2 = await activity_page(api, session, address, 100)
    if not isinstance(page2, list):
        report.add("Paginación con offset", "FAIL", f"la segunda página devolvió {type(page2).__name__}")
        return page1
    keys1 = {trade_key(t) for t in page1}
    overlap = sum(1 for t in page2 if trade_key(t) in keys1)
    newer_in_page2 = page2 and max(float(t.get("timestamp") or 0) for t in page2) > min(raw_ts)
    if not page2:
        report.add("Paginación con offset", "WARN", "la segunda página está vacía")
    elif overlap == len(page2):
        report.add("Paginación con offset", "FAIL", "la segunda página repite la primera: la API ignora 'offset'")
    elif newer_in_page2:
        report.add("Paginación con offset", "FAIL", "la segunda página tiene operaciones más recientes que la primera")
    else:
        report.add("Paginación con offset", "OK", f"página 2 con {len(page2)} operaciones más antiguas ({overlap} repetidas)")

    # La función del bot, de principio a fin.
    if page2:
        since = api._timestamp_seconds(page2[len(page2) // 2].get("timestamp"))
        trades, truncated = await api.get_leader_trades_since(session, address, since)
        covered = sum(1 for t in trades if api._timestamp_seconds(t.get("timestamp")) >= since)
        expected = sum(1 for t in page1 + page2 if api._timestamp_seconds(t.get("timestamp")) >= since)
        report.add("get_leader_trades_since (código del bot)", "OK" if covered >= expected and not truncated else "FAIL",
                   f"{covered} de {expected} operaciones desde el corte, truncado={truncated}")
    return page1


async def check_equity(api, session, address: str, report: Report, last_trades: List[Dict]):
    value = await api.get_leader_portfolio_value(session, address)
    report.add("Valor de posiciones (/value)", "OK" if value is not None else "FAIL",
               f"${value:,.2f}" if value is not None else "la API falló")

    rpc = api.cfg.polygon_rpc_url
    rpc_label = "secret POLYGON_RPC_URL" if os.getenv("POLYGON_RPC_URL") else f"RPC por defecto ({rpc})"
    cash = await api.get_leader_cash_balance(session, address)
    report.add("Efectivo USDC.e en Polygon", "OK" if cash is not None else "FAIL",
               f"${cash:,.2f} vía {rpc_label}" if cash is not None else f"no se pudo leer vía {rpc_label}")

    original = api.cfg.collateral_token_address
    api.cfg.collateral_token_address = NATIVE_USDC
    try:
        native = await api.get_leader_cash_balance(session, address)
    finally:
        api.cfg.collateral_token_address = original
    if native is not None:
        status = "WARN" if (cash == 0 and native > 0) else "INFO"
        extra = " (¿Polymarket usa otro token como colateral?)" if status == "WARN" else ""
        report.add("Efectivo USDC nativo (referencia)", status, f"${native:,.2f}{extra}")

    if value is not None and cash is not None:
        equity = value + cash
        buys = [t for t in last_trades if str(t.get("side", "")).upper() == "BUY"]
        if buys and equity > 0:
            usdc = float(buys[0].get("usdcSize") or 0)
            report.add("Ejemplo de cálculo", "INFO",
                       f"patrimonio ${equity:,.2f}; última compra ${usdc:,.2f} = {usdc / equity * 100:.2f}% del capital "
                       f"(sin el efectivo saldría {min(usdc / max(value, usdc), 1) * 100:.2f}%)")


async def check_positions_filter(api, session, address: str, report: Report):
    positions = await api._get_json(session, f"{api.cfg.data_api_host}/positions", {"user": address.lower()})
    if not isinstance(positions, list):
        report.add("Posiciones (/positions)", "FAIL", f"Respuesta inesperada: {type(positions).__name__}")
        return
    if len(positions) < 2:
        report.add("Filtro de posiciones por token", "WARN", f"el trader tiene {len(positions)} posiciones: no se puede probar el filtro")
        return
    target = positions[-1]
    token = str(target.get("asset"))
    balance = await api.get_leader_token_balance(session, address, token)
    expected = float(target.get("size") or 0)
    ok = abs(balance - expected) < 1e-6
    report.add("Filtro de posiciones por token (ventas proporcionales)", "OK" if ok else "FAIL",
               f"esperado {expected:.4f} shares, el bot leyó {balance:.4f}"
               + ("" if ok else " → la API ignora el parámetro 'asset' y el bot calcularía mal las ventas"))


async def find_resolved_condition(api, session, address: str) -> Optional[str]:
    redeems = await activity_page(api, session, address, 0, limit=20, kind="REDEEM")
    if isinstance(redeems, list):
        for r in redeems:
            if r.get("conditionId"):
                return str(r["conditionId"])
    positions = await api._get_json(session, f"{api.cfg.data_api_host}/positions", {"user": address.lower(), "redeemable": "true"})
    if isinstance(positions, list):
        for p in positions:
            if p.get("redeemable") and p.get("conditionId"):
                return str(p["conditionId"])
    return None


async def check_markets(api, session, address: str, report: Report, last_trades: List[Dict]):
    if last_trades:
        latest = last_trades[0]
        cid, token = str(latest.get("conditionId") or ""), str(latest.get("asset") or "")
        market = await api._get_json(session, f"{api.cfg.clob_host}/markets/{cid}", {})
        if isinstance(market, dict) and isinstance(market.get("tokens"), list):
            token_ids = [str(t.get("token_id")) for t in market["tokens"]]
            has_fields = all("winner" in t for t in market["tokens"]) and "closed" in market
            report.add("Mercado (/markets/{condition_id})", "OK" if has_fields and token in token_ids else "FAIL",
                       f"closed={market.get('closed')}, token de la operación {'presente' if token in token_ids else 'AUSENTE'}, "
                       f"campos winner/closed {'presentes' if has_fields else 'AUSENTES'}")
        else:
            report.add("Mercado (/markets/{condition_id})", "FAIL", "respuesta sin 'tokens'")

        buy = await api.get_best_orderbook_price(session, token, "BUY")
        sell = await api.get_best_orderbook_price(session, token, "SELL")
        valid = all(p is None or 0 < p < 1 for p in (buy, sell))
        report.add("Libro de órdenes (/book)", "OK" if valid else "FAIL",
                   f"mejor ask {buy}, mejor bid {sell}" + ("" if buy is not None or sell is not None else " (libro vacío o mercado cerrado)"))

    resolved_cid = await find_resolved_condition(api, session, address)
    if not resolved_cid:
        report.add("Detección de mercado resuelto", "WARN", "no se encontró ningún mercado resuelto de este trader para probar")
        return
    payouts = await api.get_market_resolution(session, resolved_cid)
    if payouts and max(payouts.values()) == 1.0 and min(payouts.values()) == 0.0:
        report.add("Detección de mercado resuelto (código del bot)", "OK", f"{resolved_cid[:12]}…: pagos {sorted(payouts.values())}")
    else:
        market = await api._get_json(session, f"{api.cfg.clob_host}/markets/{resolved_cid}", {})
        detail = f"closed={market.get('closed')}, tokens={market.get('tokens')}" if isinstance(market, dict) else "sin respuesta"
        report.add("Detección de mercado resuelto (código del bot)", "FAIL",
                   f"{resolved_cid[:12]}… está resuelto pero el bot no lo detecta ({detail[:300]})")


async def run(address: str) -> Report:
    report = Report()
    cfg = BotConfig(dry_run=True)  # Nunca inicializa el cliente de órdenes.
    api = PolymarketClientWrapper(cfg)
    session_cm = aiohttp.ClientSession() if aiohttp is not None else None
    session = await session_cm.__aenter__() if session_cm else None
    try:
        address = (address or "").strip().lower()
        if not address:
            try:
                address = await pick_trader(api, session, report)
            except Exception as e:
                report.add("Elegir trader activo", "FAIL", f"error: {e}")
        if not address:
            return report
        last_trades: List[Dict] = []
        try:
            last_trades = await check_activity(api, session, address, report)
        except Exception as e:
            report.add("Comprobación de actividad", "FAIL", f"error: {e}")
        for name, check in [
            ("patrimonio", lambda: check_equity(api, session, address, report, last_trades)),
            ("posiciones", lambda: check_positions_filter(api, session, address, report)),
            ("mercados", lambda: check_markets(api, session, address, report, last_trades)),
        ]:
            try:
                await check()
            except Exception as e:
                report.add(f"Comprobación de {name}", "FAIL", f"error: {e}")
    finally:
        if session_cm:
            await session_cm.__aexit__(None, None, None)
    return report


def main():
    parser = argparse.ArgumentParser(description="Diagnóstico de solo lectura de las APIs de Polymarket")
    parser.add_argument("--address", default="", help="Dirección proxy de un trader (vacío = elegir uno activo)")
    args = parser.parse_args()

    report = asyncio.run(run(args.address))
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write(report.markdown())
    sys.exit(1 if report.failed or not report.rows else 0)


if __name__ == "__main__":
    main()
