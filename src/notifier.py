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
    # Telegram rechaza mensajes de más de 4096 caracteres.
    TELEGRAM_MAX_LEN = 4000

    def __init__(self, cfg: BotConfig):
        self.cfg = cfg

    def _sync_post_telegram(self, url: str, payload: dict):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")

    def build_telegram_payload(self, message: str, level: str) -> dict:
        # Texto plano a propósito: con parse_mode=Markdown, un '_' o '*' en el nombre
        # de un mercado o de un trader hace que Telegram rechace el mensaje entero.
        text = f"🤖 Polymarket CopyBot [{level}]\n{message}"
        if len(text) > self.TELEGRAM_MAX_LEN:
            text = text[: self.TELEGRAM_MAX_LEN - 1] + "…"
        return {"chat_id": self.cfg.telegram_chat_id, "text": text}

    async def notify(self, message: str, level: str = "INFO", send_telegram: bool = True):
        if console:
            color = {"INFO": "cyan", "SUCCESS": "green", "WARNING": "yellow", "ERROR": "red"}.get(level, "white")
            console.print(f"[{color}][{level}][/{color}] {message}")
        else:
            print(f"[{level}] {message}")

        if not send_telegram or not (self.cfg.telegram_token and self.cfg.telegram_chat_id):
            return

        url = f"https://api.telegram.org/bot{self.cfg.telegram_token}/sendMessage"
        payload = self.build_telegram_payload(message, level)
        try:
            if aiohttp is not None:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, json=payload, timeout=5) as resp:
                        status, body = resp.status, await resp.text()
            else:
                status, body = await asyncio.to_thread(self._sync_post_telegram, url, payload)
            if status != 200:
                print(f"[ERROR] Telegram rechazó la alerta (HTTP {status}): {body[:300]}")
        except Exception as e:
            print(f"[ERROR] Error enviando alerta a Telegram: {e}")
