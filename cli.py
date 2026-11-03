import argparse
import asyncio
import sys
from config import config
from src import follower_analysis, top_traders, traders_file
from src.database import DatabaseManager

try:
    from rich.console import Console
    from rich.table import Table
    console = Console()
except ImportError:
    console = None
    Table = None

def main():
    parser = argparse.ArgumentParser(description="Gestor de Sub-Bancas (Banks) para Polymarket CopyBot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_p = subparsers.add_parser("add-trader", help="Agrega una nueva persona a seguir con su respectivo bank")
    add_p.add_argument("--address", required=True, help="Dirección Proxy de Polymarket del trader (0x...)")
    add_p.add_argument("--alias", required=True, help="Nombre identificador (ej. WhalePoli, CryptoKing)")
    add_p.add_argument("--bank", type=float, required=True, help="Capital en USDC asignado exclusivamente a este trader")
    add_p.add_argument("--stop-loss", type=float, default=0.30, help="Caída máxima permitida para esta sub-banca (ej. 0.30 = 30%%)")

    upd_p = subparsers.add_parser("update-bank", help="Modifica el bank asignado a un trader")
    upd_p.add_argument("--alias", required=True, help="Alias del trader")
    upd_p.add_argument("--bank", type=float, required=True, help="Nuevo monto total del bank en USDC")

    pause_p = subparsers.add_parser("pause", help="Pausa el seguimiento de un trader")
    pause_p.add_argument("--alias", required=True, help="Alias del trader")

    resume_p = subparsers.add_parser("resume", help="Reanuda el seguimiento de un trader")
    resume_p.add_argument("--alias", required=True, help="Alias del trader")

    subparsers.add_parser("sync", help="Sincroniza los traders definidos en traders.json hacia la base de datos")
    subparsers.add_parser("status", help="Muestra el estado de todos los seguimientos y sus sub-bancas")

    top_p = subparsers.add_parser("top-traders", help="Lista los mejores traders del ranking de Polymarket para elegir a quién seguir")
    top_p.add_argument("--period", choices=list(top_traders.PERIODS), default="mes", help="Periodo del ranking (por defecto: mes)")
    top_p.add_argument("--order", choices=list(top_traders.ORDERS), default="ganancias", help="Ordenar por ganancias o por volumen")
    top_p.add_argument("--limit", type=int, default=15, help="Cuántos traders mostrar (1-50, por defecto 15)")
    top_p.add_argument("--sin-actividad", action="store_true", help="No contar las operaciones de cada trader (más rápido)")
    top_p.add_argument("--solo-copiables", action="store_true",
                       help=f"Solo traders con ganancias y activos en los últimos 7 días, buscando entre los "
                            f"{top_traders.COPYABLE_SCAN} primeros del ranking (los bots se incluyen, marcados con ⚠️)")
    top_p.add_argument("--markdown", action="store_true", help="Salida en Markdown (la usa el workflow de GitHub Actions)")

    fol_p = subparsers.add_parser("analizar-seguidores",
                                  help="Comprueba si a un trader le 'empujan' el precio otras carteras que le copian")
    fol_p.add_argument("--address", required=True, help="Dirección proxy del trader (0x...)")
    fol_p.add_argument("--dias", type=int, default=7, help="Días hacia atrás que se revisan (por defecto 7)")
    fol_p.add_argument("--ventana", type=int, default=10, help="Minutos antes y después de cada operación (por defecto 10)")
    fol_p.add_argument("--max-operaciones", type=int, default=60, help="Operaciones más recientes que se analizan (por defecto 60)")
    fol_p.add_argument("--max-paginas", type=int, default=30,
                       help="Páginas de 100 operaciones por mercado como máximo (por defecto 30; súbelo en mercados muy activos)")
    fol_p.add_argument("--markdown", action="store_true", help="Salida en Markdown (la usa el workflow de GitHub Actions)")

    args = parser.parse_args()
    if args.command == "top-traders":
        sys.exit(run_top_traders(args))
    if args.command == "analizar-seguidores":
        sys.exit(run_follower_analysis(args))
    db = DatabaseManager(config.db_path)
    path = config.traders_json_path

    # traders.json es la fuente de verdad: el bot lo sincroniza en cada ejecución, así
    # que estos comandos lo modifican a él (y después la base de datos), no solo la base de datos.
    try:
        if args.command == "add-trader":
            traders_file.add_trader(path, args.address, args.alias, args.bank, args.stop_loss)
            message = f"✔ Seguimiento creado: '{args.alias}' ({args.address}) con un Bank aislado de ${args.bank:.2f} USDC."
        elif args.command == "update-bank":
            traders_file.update_bank(path, args.alias, args.bank)
            message = f"✔ Bank actualizado para '{args.alias}': Nuevo capital base = ${args.bank:.2f} USDC."
        elif args.command == "pause":
            traders_file.set_active(path, args.alias, False)
            message = f"⏸ Seguimiento '{args.alias}' pausado."
        elif args.command == "resume":
            traders_file.set_active(path, args.alias, True)
            message = f"▶ Seguimiento '{args.alias}' reactivado."
        else:
            message = None
    except ValueError as e:
        print(f"✘ {e}")
        sys.exit(1)

    if args.command in ("sync", "add-trader", "update-bank", "pause", "resume"):
        removed = db.sync_from_json(path)
        print(message or "✔ Archivo traders.json sincronizado con la base de datos.")
        if removed:
            print(f"⏸ Desactivados porque ya no están en traders.json: {', '.join(removed)}")
        if args.command != "sync":
            print(f"  Cambio guardado en {path}. Si el bot corre en GitHub Actions, haz commit y push de ese archivo.")

    elif args.command == "status":
        db.sync_from_json(config.traders_json_path)
        traders = db.get_all_traders()
        if not traders:
            print("No hay traders registrados. Edita 'traders.json' o usa 'python cli.py add-trader'.")
            return

        if console and Table:
            table = Table(title="Estado de Sub-Bancas (Polymarket CopyBot)")
            table.add_column("Alias", style="cyan", no_wrap=True)
            table.add_column("Dirección Proxy", style="dim")
            table.add_column("Bank Inicial", justify="right", style="green")
            table.add_column("USDC Disponible", justify="right", style="bold yellow")
            table.add_column("Costo Posiciones", justify="right", style="magenta")
            table.add_column("Stop-Loss", justify="right")
            table.add_column("Estado", justify="center")

            for t in traders:
                positions = db.get_trader_positions(t["proxy_address"])
                invested_cost = sum(p["shares"] * p["avg_price"] for p in positions)
                status_badge = "[green]ACTIVO[/green]" if t["is_active"] else "[red]PAUSADO[/red]"
                table.add_row(
                    t["alias"],
                    f"{t['proxy_address'][:8]}...{t['proxy_address'][-6:]}",
                    f"${t['initial_bank']:.2f}",
                    f"${t['available_cash']:.2f}",
                    f"${invested_cost:.2f} ({len(positions)} pos)",
                    f"-{t['max_drawdown_pct']*100:.0f}%",
                    status_badge
                )
            console.print(table)
        else:
            print("=== Estado de Sub-Bancas (Polymarket CopyBot) ===")
            for t in traders:
                positions = db.get_trader_positions(t["proxy_address"])
                invested_cost = sum(p["shares"] * p["avg_price"] for p in positions)
                status_str = "ACTIVO" if t["is_active"] else "PAUSADO"
                print(
                    f"- {t['alias']} ({t['proxy_address'][:8]}...): "
                    f"Bank=${t['initial_bank']:.2f} | Disponible=${t['available_cash']:.2f} | "
                    f"Posiciones=${invested_cost:.2f} ({len(positions)}) | Estado={status_str}"
                )

def run_top_traders(args) -> int:
    """Consulta el ranking (solo lectura: no usa la base de datos ni claves)."""
    from src.polymarket_api import PolymarketClientWrapper
    try:
        import aiohttp
    except ImportError:
        aiohttp = None

    limit = max(1, min(50, args.limit))
    followed = [t.get("proxy_address", "") for t in traders_file.load(config.traders_json_path)["traders"]]

    async def fetch():
        api = PolymarketClientWrapper(config)
        session = aiohttp.ClientSession() if aiohttp is not None else None
        try:
            return await top_traders.fetch_top_traders(
                api, session, args.period, args.order, limit,
                with_activity=not args.sin_actividad, followed=followed,
                only_copyable=args.solo_copiables,
            )
        finally:
            if session is not None:
                await session.close()

    try:
        traders = asyncio.run(fetch())
    except Exception as e:
        print(f"✘ No se pudo consultar el ranking de Polymarket: {e}", file=sys.stderr)
        return 1
    if not traders:
        detail = (f" Ninguno de los {top_traders.COPYABLE_SCAN} primeros está activo y con ganancias; prueba otro periodo."
                  if args.solo_copiables else "")
        print(f"✘ El ranking de Polymarket no devolvió traders.{detail}", file=sys.stderr)
        return 1

    if args.markdown:
        print(top_traders.render_markdown(traders, args.period, args.order, args.solo_copiables), end="")
        return 0

    title = f"Mejores traders de Polymarket ({top_traders.PERIOD_LABELS[args.period]}, por {args.order})"
    if args.solo_copiables:
        title += " — solo activos y con ganancias"
    headers = ["#", "Trader", "Dirección", "Ganancias", "Volumen", "Margen", "Operaciones 7 días", "Última operación"]
    rows = top_traders.render_rows(traders)
    if console and Table:
        table = Table(title=title)
        for header in headers:
            table.add_column(header, no_wrap=header == "Dirección")
        for row in rows:
            table.add_row(*row)
        console.print(table)
    else:
        print(title)
        for row in rows:
            print(" | ".join(row))
    for note in top_traders.NOTES:
        print(f"- {note}")
    return 0


def run_follower_analysis(args) -> int:
    """Análisis de solo lectura: no usa la base de datos ni claves."""
    from src.polymarket_api import PolymarketClientWrapper
    try:
        import aiohttp
    except ImportError:
        aiohttp = None

    address = args.address.strip().lower()
    if not (address.startswith("0x") and len(address) == 42):
        print("✘ La dirección debe tener el formato 0x seguido de 40 caracteres.", file=sys.stderr)
        return 1

    async def run():
        api = PolymarketClientWrapper(config)
        session = aiohttp.ClientSession() if aiohttp is not None else None
        try:
            return await follower_analysis.analyze_followers(
                api, session, address, days=max(1, args.dias), window_minutes=max(1, args.ventana),
                max_trades=max(1, min(500, args.max_operaciones)),
                max_pages_per_market=max(1, min(200, args.max_paginas)),
            )
        finally:
            if session is not None:
                await session.close()

    try:
        report = asyncio.run(run())
    except Exception as e:
        print(f"✘ No se pudo completar el análisis: {e}", file=sys.stderr)
        return 1
    text = follower_analysis.render_markdown(report)
    if args.markdown or not console:
        print(text, end="")
    else:
        from rich.markdown import Markdown
        console.print(Markdown(text))
    return 0


if __name__ == "__main__":
    main()
