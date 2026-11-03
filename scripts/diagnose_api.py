"""
Diagnóstico de solo lectura de las APIs que usa el bot.

Comprueba contra Polymarket y Polygon reales lo que el bot da por supuesto:
actividad de la Data API v2 y su paginación por cursor, valor de posiciones,
efectivo (pUSD y USDC.e) y shares leídos en la blockchain, detección de mercados
resueltos y libro de órdenes.

No envía órdenes, no usa claves privadas y no toca la base de datos del bot.

Uso:
    python scripts/diagnose_api.py [--address 0x...]

Sin --address elige automáticamente un trader con mucha actividad reciente.
"""
import argparse
import asyncio
import json
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

GAMMA_HOST = "https://gamma-api.polymarket.com"
EXPECTED_ACTIVITY_FIELDS = ["transaction_hash", "token_id", "side", "timestamp", "size", "price",
                            "usdc_size", "condition_id", "title", "outcome"]
EXPECTED_POSITION_FIELDS = ["token_id", "condition_id", "current_size", "current_value"]


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


async def raw_get(session, url: str, params: Dict[str, str]):
    """GET que devuelve (código HTTP, JSON o texto) para poder explicar los fallos."""
    if session is None:
        return None, "sin aiohttp"
    async with session.get(url, params=params, timeout=15) as resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except ValueError:
            return resp.status, text[:300]


def collect_wallets(data, found: Counter):
    """Recoge todas las direcciones de cartera de una respuesta JSON, estén donde estén."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key in ("proxy_wallet", "proxyWallet") and isinstance(value, str) and value.startswith("0x"):
                found[value.lower()] += 1
            else:
                collect_wallets(value, found)
    elif isinstance(data, list):
        for item in data:
            collect_wallets(item, found)


async def activity_page(api, session, address: str, cursor: Optional[str] = None, limit: int = 100, kind: str = "TRADE"):
    """Una página de /v2/activity con el código del bot. Devuelve (filas normalizadas, cursor, has_more)."""
    params = {"user": address.lower(), "type": kind, "limit": str(limit),
              "sort_by": "TIMESTAMP", "sort_direction": "DESC"}
    if cursor:
        params["cursor"] = cursor
    rows, next_cursor, has_more = await api._data_api_v2(session, "/v2/activity", params)
    return rows, [api.normalize_activity(r) for r in rows], next_cursor, has_more


async def candidate_wallets(api, session, report: Report) -> Counter:
    wallets: Counter = Counter()
    status, data = await raw_get(session, f"{api.cfg.data_api_host}/v2/trades", {"limit": "100"})
    collect_wallets(data, wallets)
    if wallets:
        return wallets
    report.add("Operaciones recientes (/v2/trades)", "WARN", f"HTTP {status}: {str(data)[:200]}")

    # Plan B: mayores tenedores de los mercados con más volumen en las últimas 24 h.
    status, markets = await raw_get(session, f"{GAMMA_HOST}/markets",
                                    {"active": "true", "closed": "false", "order": "volume24hr",
                                     "ascending": "false", "limit": "5"})
    if not isinstance(markets, list):
        report.add("Mercados con más volumen (Gamma API)", "WARN", f"HTTP {status}: {str(markets)[:200]}")
        return wallets
    for market in markets:
        cid = market.get("conditionId") or market.get("condition_id")
        if not cid:
            continue
        status, holders = await raw_get(session, f"{api.cfg.data_api_host}/v2/holders", {"condition": cid, "limit": "20"})
        collect_wallets(holders, wallets)
        if not wallets:
            report.add("Mayores tenedores (/v2/holders)", "WARN", f"HTTP {status}: {str(holders)[:200]}")
    return wallets


async def pick_trader(api, session, report: Report) -> Optional[str]:
    """Elige, entre los que más operan ahora mismo, uno con más de una página de actividad (para probar la paginación)."""
    wallets = await candidate_wallets(api, session, report)
    if not wallets:
        report.add("Elegir trader activo", "FAIL", "no se encontró ningún trader; pasa una dirección con el campo 'address'")
        return None
    for wallet, _ in wallets.most_common(15):
        try:
            _, rows, _, has_more = await activity_page(api, session, wallet)
        except Exception:
            continue
        if has_more and len(rows) >= 100:
            report.add("Elegir trader activo", "INFO", f"{wallet} (más de 100 operaciones en su historial)")
            return wallet
    wallet = wallets.most_common(1)[0][0]
    report.add("Elegir trader activo", "WARN", f"{wallet}: ninguno de los más activos tiene más de una página; la paginación no se podrá probar del todo")
    return wallet


async def check_activity(api, session, address: str, report: Report) -> List[Dict]:
    try:
        raw1, page1, cursor, has_more = await activity_page(api, session, address)
    except Exception as e:
        status, body = await raw_get(session, f"{api.cfg.data_api_host}/v2/activity",
                                     {"user": address, "type": "TRADE", "limit": "100"})
        report.add("Actividad (/v2/activity)", "FAIL", f"{e} — HTTP {status}: {str(body)[:200]}")
        return []
    if not page1:
        report.add("Actividad (/v2/activity)", "WARN", "el trader no tiene operaciones")
        return []
    report.add("Actividad (/v2/activity)", "OK", f"{len(page1)} operaciones en la primera página, has_more={has_more}")

    missing = [f for f in EXPECTED_ACTIVITY_FIELDS if f not in raw1[0]]
    report.add("Campos de cada operación (v2)", "OK" if not missing else "FAIL",
               "todos presentes" if not missing else f"faltan: {', '.join(missing)} (campos recibidos: {', '.join(sorted(raw1[0]))[:300]})")

    sample_ts = raw1[0].get("timestamp")
    try:
        stamps = [api._timestamp_seconds(t.get("timestamp")) for t in page1]
        ordered = all(stamps[i] >= stamps[i + 1] for i in range(len(stamps) - 1))
        report.add("Orden y formato de timestamp", "OK" if ordered else "WARN",
                   f"{'de más reciente a más antigua' if ordered else 'NO ordenada'}; ejemplo {sample_ts!r} "
                   f"({type(sample_ts).__name__}) → {stamps[0]} s")
    except Exception as e:
        report.add("Orden y formato de timestamp", "FAIL", f"el bot no entiende el timestamp {sample_ts!r}: {e}")
        return page1

    if not has_more or not cursor:
        report.add("Paginación con cursor", "WARN", "solo hay una página: no se puede probar")
        return page1
    _, page2, _, _ = await activity_page(api, session, address, cursor=cursor)
    keys1 = {trade_key(t) for t in page1}
    overlap = sum(1 for t in page2 if trade_key(t) in keys1)
    newer = page2 and max(api._timestamp_seconds(t.get("timestamp")) for t in page2) > min(stamps)
    if not page2:
        report.add("Paginación con cursor", "WARN", "la segunda página está vacía")
    elif overlap == len(page2):
        report.add("Paginación con cursor", "FAIL", "la segunda página repite la primera")
    elif newer:
        report.add("Paginación con cursor", "FAIL", "la segunda página tiene operaciones más recientes que la primera")
    else:
        report.add("Paginación con cursor", "OK", f"página 2 con {len(page2)} operaciones más antiguas ({overlap} repetidas)")

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
    report.add("Valor de posiciones (/v2/positions)", "OK" if value is not None else "FAIL",
               f"${value:,.2f}" if value is not None else "la API falló")

    rpc_label = "secret POLYGON_RPC_URL" if os.getenv("POLYGON_RPC_URL") else f"RPC por defecto ({api.cfg.polygon_rpc_url})"
    pusd = await api.get_cash_balance(session, address)
    usdce = await api._erc20_balance(session, api.cfg.legacy_collateral_address, address)
    cash = await api.get_leader_cash_balance(session, address)
    report.add("Efectivo en Polygon (pUSD + USDC.e)", "OK" if cash is not None else "FAIL",
               f"${cash:,.2f} (pUSD ${pusd or 0:,.2f}, USDC.e ${usdce or 0:,.2f}) vía {rpc_label}"
               if cash is not None else f"no se pudo leer vía {rpc_label}")

    if value is not None and cash is not None:
        equity = value + cash
        buys = [t for t in last_trades if str(t.get("side", "")).upper() == "BUY"]
        if buys and equity > 0:
            usdc = float(buys[0].get("usdcSize") or 0)
            report.add("Ejemplo de cálculo", "INFO",
                       f"patrimonio ${equity:,.2f}; última compra ${usdc:,.2f} = {usdc / equity * 100:.2f}% del capital")


async def check_onchain_shares(api, session, address: str, report: Report):
    positions = await api.get_positions(session, address)
    if positions is None:
        report.add("Posiciones (/v2/positions)", "FAIL", "la API falló")
        return
    if not positions:
        report.add("Shares en la blockchain (CTF)", "WARN", "el trader no tiene posiciones abiertas para comparar")
        return
    missing = [f for f in EXPECTED_POSITION_FIELDS if f not in positions[0]]
    report.add("Campos de cada posición (v2)", "OK" if not missing else "FAIL",
               "todos presentes" if not missing else f"faltan: {', '.join(missing)} (recibidos: {', '.join(sorted(positions[0]))[:300]})")
    target = max(positions, key=lambda p: float(p.get("current_size") or 0))
    expected = float(target.get("current_size") or 0)
    onchain = await api.get_onchain_shares(session, address, str(target.get("token_id")))
    if onchain is None:
        report.add("Shares en la blockchain (CTF)", "FAIL", "no se pudo leer el contrato de posiciones")
        return
    ok = abs(onchain - expected) <= max(0.01, expected * 0.01)
    report.add("Shares en la blockchain (CTF)", "OK" if ok else "FAIL",
               f"Data API {expected:.4f} shares, blockchain {onchain:.4f}"
               + ("" if ok else " → revisa CTF_CONTRACT_ADDRESS/decimales: las ventas usarían cifras erróneas"))


async def find_resolved_condition(api, session, address: str) -> Optional[str]:
    positions = await api.get_positions(session, address) or []
    for p in positions:
        if (p.get("redeemable") or str(p.get("status", "")).upper() == "REDEEMABLE") and p.get("condition_id"):
            return str(p["condition_id"])
    try:
        _, redeems, _, _ = await activity_page(api, session, address, limit=20, kind="REDEEM")
    except Exception:
        redeems = []
    for r in redeems:
        if r.get("conditionId"):
            return str(r["conditionId"])
    return None


async def check_markets(api, session, address: str, report: Report, last_trades: List[Dict]):
    if last_trades:
        latest = last_trades[0]
        cid, token = str(latest.get("conditionId") or ""), str(latest.get("asset") or "")
        market = await api._get_json(session, f"{api.cfg.clob_host}/markets/{cid}", {})
        if isinstance(market, dict) and isinstance(market.get("tokens"), list):
            token_ids = [str(t.get("token_id")) for t in market["tokens"]]
            has_fields = all("winner" in t for t in market["tokens"]) and "closed" in market
            report.add("Mercado (CLOB /markets/{condition_id})", "OK" if has_fields and token in token_ids else "FAIL",
                       f"closed={market.get('closed')}, token de la operación {'presente' if token in token_ids else 'AUSENTE'}, "
                       f"campos winner/closed {'presentes' if has_fields else 'AUSENTES'}")
        else:
            status, body = await raw_get(session, f"{api.cfg.clob_host}/markets/{cid}", {})
            report.add("Mercado (CLOB /markets/{condition_id})", "FAIL", f"respuesta sin 'tokens': HTTP {status}: {str(body)[:200]}")

        buy = await api.get_best_orderbook_price(session, token, "BUY")
        sell = await api.get_best_orderbook_price(session, token, "SELL")
        valid = all(p is None or 0 < p < 1 for p in (buy, sell))
        report.add("Libro de órdenes (CLOB /book)", "OK" if valid else "FAIL",
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
            ("shares en la blockchain", lambda: check_onchain_shares(api, session, address, report)),
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
