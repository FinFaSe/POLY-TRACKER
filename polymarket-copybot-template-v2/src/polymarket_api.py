import asyncio
import json
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple
from config import BotConfig

try:
    import aiohttp
except ImportError:
    aiohttp = None

class PolymarketClientWrapper:
    """
    Conecta con la Data API y el CLOB de Polymarket.
    Soporta aiohttp asíncrono y cuenta con fallback nativo a urllib para máxima compatibilidad.
    Soporta proxy HTTP opcional para ejecuciones desde GitHub Actions.
    """
    def __init__(self, cfg: BotConfig):
        self.cfg = cfg
        self.proxy = cfg.http_proxy_url if cfg.http_proxy_url else None
        self.clob_client = None
        if not self.cfg.dry_run and self.cfg.private_key:
            self._init_clob_client()

    def _init_clob_client(self):
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=self.cfg.clob_api_key,
                api_secret=self.cfg.clob_secret,
                api_passphrase=self.cfg.clob_passphrase,
            )
            self.clob_client = ClobClient(
                host=self.cfg.clob_host,
                key=self.cfg.private_key,
                chain_id=self.cfg.chain_id,
                creds=creds,
                signature_type=2,
                funder=self.cfg.proxy_address
            )
        except Exception as e:
            print(f"[ERROR] No se pudo inicializar py-clob-client: {e}")
            self.clob_client = None

    def _sync_get_json(self, url: str, params: Dict[str, str]):
        query = urllib.parse.urlencode(params)
        full_url = f"{url}?{query}" if query else url
        req = urllib.request.Request(full_url, headers={"User-Agent": "PolymarketCopyBot/2.0"})
        if self.proxy:
            proxy_handler = urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy})
            opener = urllib.request.build_opener(proxy_handler)
        else:
            opener = urllib.request.build_opener()
        with opener.open(req, timeout=10) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
        return None

    async def _get_json(self, session, url: str, params: Dict[str, str]):
        if session is not None and aiohttp is not None:
            async with session.get(url, params=params, proxy=self.proxy, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        return await asyncio.to_thread(self._sync_get_json, url, params)

    async def get_recent_leader_trades(self, session, proxy_address: str, limit: int = 10) -> List[Dict]:
        url = f"{self.cfg.data_api_host}/activity"
        params = {"user": proxy_address.lower(), "type": "TRADE", "limit": str(limit)}
        data = await self._get_json(session, url, params)
        return data if isinstance(data, list) else []

    async def get_leader_portfolio_value(self, session, proxy_address: str) -> float:
        url = f"{self.cfg.data_api_host}/value"
        params = {"user": proxy_address.lower()}
        data = await self._get_json(session, url, params)
        if isinstance(data, list) and len(data) > 0:
            return float(data[0].get("value", 0.0))
        elif isinstance(data, dict):
            return float(data.get("value", 0.0))
        return 0.0

    async def get_leader_token_balance(self, session, proxy_address: str, token_id: str) -> float:
        url = f"{self.cfg.data_api_host}/positions"
        params = {"user": proxy_address.lower(), "asset": token_id}
        positions = await self._get_json(session, url, params)
        if isinstance(positions, list) and len(positions) > 0:
            return float(positions[0].get("size", 0.0))
        return 0.0

    async def get_best_orderbook_price(self, session, token_id: str, side: str) -> Optional[float]:
        """
        Obtiene el mejor precio real disponible en el libro de órdenes (CLOB):
        - Para BUY toma el menor ASK disponible (`min`).
        - Para SELL toma el mayor BID disponible (`max`).
        """
        url = f"{self.cfg.clob_host}/book"
        params = {"token_id": token_id}
        book = await self._get_json(session, url, params)
        if not isinstance(book, dict):
            return None
        if side.upper() == "BUY":
            asks = book.get("asks", [])
            return min(float(a["price"]) for a in asks) if asks else None
        else:
            bids = book.get("bids", [])
            return max(float(b["price"]) for b in bids) if bids else None

    def check_slippage(self, leader_price: float, current_market_price: float, side: str) -> Tuple[bool, float]:
        if leader_price <= 0:
            return False, 0.0
        if side.upper() == "BUY":
            slippage = (current_market_price - leader_price) / leader_price
        else:
            slippage = (leader_price - current_market_price) / leader_price

        is_acceptable = slippage <= self.cfg.max_slippage_pct
        return is_acceptable, slippage

    async def execute_order(
        self,
        token_id: str,
        side: str,
        price: float,
        usdc_amount: float = 0.0,
        shares: float = 0.0
    ) -> Tuple[bool, str]:
        if self.cfg.dry_run:
            qty_info = f"${usdc_amount:.2f} USDC" if side == "BUY" else f"{shares:.4f} shares"
            return True, f"[SIMULACIÓN DRY_RUN] Orden {side} ejecutada por {qty_info} a ${price:.4f}"

        if not self.clob_client:
            return False, "Cliente CLOB no inicializado (revisa tus Secrets en GitHub o .env)"

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_side = BUY if side.upper() == "BUY" else SELL
            size = (usdc_amount / price) if order_side == BUY else shares

            order_args = OrderArgs(
                price=round(price, 3),
                size=round(size, 2),
                side=order_side,
                token_id=token_id
            )
            signed_order = self.clob_client.create_order(order_args)
            resp = self.clob_client.post_order(signed_order, OrderType.FOK)
            return True, f"Orden enviada al CLOB: {resp}"
        except Exception as e:
            return False, f"Error en ejecución CLOB: {e}"
