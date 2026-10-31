import asyncio
import json
import urllib.request
from config import BotConfig

try:
    import aiohttp
except ImportError:
    aiohttp = None

try:
    from rich.console import Console
    console = Console()
except ImportError:
    console = None

class Notifier:
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    def _sync_post_telegram(self, url: str, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status

    async def notify(self, message: str, level: str = "INFO"):
        if console:
            color = {"INFO": "cyan", "SUCCESS": "green", "WARNING": "yellow", "ERROR": "red"}.get(level, "white")
            console.print(f"[{color}][{level}][/{color}] {message}")
        else:
            print(f"[{level}] {message}")

        if self.cfg.telegram_token and self.cfg.telegram_chat_id:
            url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
            payload = {
                "chat_id": self.cfg.telegram_chat_id,
                "text": f"🤖 *Polymarket CopyBot* [{level}]\n{message}",
                "parse_mode": "Markdown"
            }
            try:
                if aiohttp is not None:
                    async with aiohttp.ClientSession() as session:
                        await session.post(url, json=payload, timeout=5)
                else:
                    await asyncio.to_thread(self._sync_post_telegram, url, payload)
            except Exception as e:
                print(f"[ERROR] Error enviando alerta a Telegram: {e}")
