import argparse
import asyncio
from config import config
from src.listener import CopyBotMonitor

async def main():
    parser = argparse.ArgumentParser(description="Polymarket Multi-Bank CopyBot")
    parser.add_argument("--once", action="store_true", help="Ejecuta un único ciclo de escaneo y termina (ideal para GitHub Actions)")
    parser.add_argument("--max-runtime-minutes", type=int, default=None, help="Detiene el bot tras X minutos de ejecución continua")
    args = parser.parse_args()

    monitor = CopyBotMonitor(config)
    await monitor.run(once=args.once, max_runtime_minutes=args.max_runtime_minutes)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Bot detenido manualmente por el usuario.")
