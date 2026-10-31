import argparse
from config import config
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

    args = parser.parse_args()
    db = DatabaseManager(config.db_path)

    if args.command == "sync":
        db.sync_from_json(config.traders_json_path)
        print("✔ Archivo traders.json sincronizado con la base de datos.")

    elif args.command == "add-trader":
        db.add_trader(args.address, args.alias, args.bank, args.stop_loss)
        print(f"✔ Seguimiento creado: '{args.alias}' ({args.address}) con un Bank aislado de ${args.bank:.2f} USDC.")

    elif args.command == "update-bank":
        db.update_trader_bank(args.alias, args.bank)
        print(f"✔ Bank actualizado para '{args.alias}': Nuevo capital base = ${args.bank:.2f} USDC.")

    elif args.command == "pause":
        db.set_trader_active(args.alias, False)
        print(f"⏸ Seguimiento '{args.alias}' pausado.")

    elif args.command == "resume":
        db.set_trader_active(args.alias, True)
        print(f"▶ Seguimiento '{args.alias}' reactivado.")

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

if __name__ == "__main__":
    main()
