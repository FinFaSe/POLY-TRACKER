import asyncio
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
from config import BotConfig

try:
    import aiohttp
except ImportError:
    aiohttp = None

@dataclass
class OrderResult:
    """Resultado de una orden: solo success=True si Polymarket confirma que se llenó."""
    success: bool
    message: str
    filled_usdc: float = 0.0     # USDC pagados (compra) o recibidos (venta)
    filled_shares: float = 0.0   # shares compradas o vendidas
    avg_price: float = 0.0       # precio medio de ejecución


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
        self.clob_init_error = ""
        if not self.cfg.dry_run and self.cfg.private_key:
            self._init_clob_client()

    @staticmethod
    def configure_clob_http_proxy(proxy_url: str):
        """
        py-clob-client envía las órdenes con un cliente httpx propio, creado al importar
        la librería, que no usa HTTP_PROXY_URL. Lo sustituimos por uno que pase por el
        proxy para que las órdenes no salgan desde una IP bloqueada (p. ej. GitHub Actions en EE.UU.).
        """
        import httpx
        from py_clob_client.http_helpers import helpers

        if not hasattr(helpers, "_http_client"):
            raise RuntimeError(
                "Esta versión de py-clob-client no expone '_http_client'; no se puede aplicar el proxy a las órdenes"
            )
        old_client = helpers._http_client
        helpers._http_client = httpx.Client(http2=True, proxy=proxy_url)
        try:
            old_client.close()
        except Exception:
            pass

    def _init_clob_client(self):
        if os.getenv("GITHUB_ACTIONS") == "true" and not self.proxy:
            self.clob_init_error = (
                "Modo real bloqueado: en GitHub Actions hace falta HTTP_PROXY_URL, porque Polymarket "
                "rechaza órdenes desde IPs de EE.UU."
            )
            print(f"[ERROR] {self.clob_init_error}")
            return
        try:
            if self.proxy:
                self.configure_clob_http_proxy(self.proxy)

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
            self.clob_init_error = f"No se pudo inicializar py-clob-client: {e}"
            print(f"[ERROR] {self.clob_init_error}")
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

    def _sync_post_json(self, url: str, payload: Dict):
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json", "User-Agent": "PolymarketCopyBot/2.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return json.loads(resp.read().decode("utf-8"))
        return None

    async def _post_json(self, session, url: str, payload: Dict):
        if session is not None and aiohttp is not None:
            async with session.post(url, json=payload, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                return None
        return await asyncio.to_thread(self._sync_post_json, url, payload)

    async def _get_json(self, session, url: str, params: Dict[str, str]):
        if session is not None and aiohttp is not None:
            async with session.get(url, params=params, proxy=self.proxy, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                return None
        return await asyncio.to_thread(self._sync_get_json, url, params)

    async def get_leader_trades_since(self, session, proxy_address: str, since_timestamp: int) -> Tuple[List[Dict], bool]:
        """
        Devuelve las operaciones del líder desde 'since_timestamp' (segundos), de la más
        reciente a la más antigua, recorriendo la Data API página a página.
        El segundo valor es True si se alcanzó el tope de páginas antes de llegar a
        'since_timestamp', es decir, si pueden faltar operaciones más antiguas.
        """
        url = f"{self.cfg.data_api_host}/activity"
        page_size = max(1, self.cfg.activity_page_size)
        trades: List[Dict] = []
        for page in range(max(1, self.cfg.max_activity_pages)):
            params = {
                "user": proxy_address.lower(), "type": "TRADE",
                "limit": str(page_size), "offset": str(page * page_size),
            }
            data = await self._get_json(session, url, params)
            if not isinstance(data, list):
                # Si falta una página no procesamos nada: procesar solo las recientes
                # dejaría atrás para siempre las más antiguas. Se reintenta en el próximo ciclo.
                raise RuntimeError(f"La Data API no devolvió la página {page + 1} de actividad del trader")
            trades.extend(data)
            if len(data) < page_size:
                return trades, False
            oldest = min(self._timestamp_seconds(t.get("timestamp", 0)) for t in data)
            if oldest < since_timestamp:
                return trades, False
        return trades, True

    @staticmethod
    def _timestamp_seconds(raw_ts) -> int:
        ts = int(float(raw_ts or 0))
        return ts // 1000 if ts > 1_000_000_000_000 else ts

    async def get_leader_portfolio_value(self, session, proxy_address: str) -> Optional[float]:
        """
        Valor de las posiciones abiertas del líder (la Data API no incluye su efectivo).
        Devuelve None si la API falla, para no confundir un error con una cartera vacía.
        """
        url = f"{self.cfg.data_api_host}/value"
        params = {"user": proxy_address.lower()}
        data = await self._get_json(session, url, params)
        if isinstance(data, list):
            return float(data[0].get("value", 0.0)) if data else 0.0
        elif isinstance(data, dict):
            return float(data.get("value", 0.0))
        return None

    async def _eth_call_uint(self, session, contract: str, data: str) -> Optional[int]:
        """eth_call en Polygon que devuelve un uint256. None si el RPC falla (con un reintento)."""
        if not self.cfg.polygon_rpc_url:
            return None
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": contract, "data": data}, "latest"],
        }
        for _ in range(2):
            try:
                response = await self._post_json(session, self.cfg.polygon_rpc_url, payload)
            except Exception:
                response = None
            result = response.get("result") if isinstance(response, dict) else None
            if isinstance(result, str) and result.startswith("0x"):
                return int(result, 16) if len(result) > 2 else 0
        return None

    @staticmethod
    def _abi_address(address: str) -> str:
        return address.lower().replace("0x", "").rjust(64, "0")

    async def get_cash_balance(self, session, address: str) -> Optional[float]:
        """Efectivo (USDC) de una wallet de Polymarket, leído en Polygon con ERC20 balanceOf()."""
        raw = await self._eth_call_uint(
            session, self.cfg.collateral_token_address, "0x70a08231" + self._abi_address(address)
        )
        return None if raw is None else raw / 10 ** self.cfg.collateral_decimals

    async def get_leader_cash_balance(self, session, proxy_address: str) -> Optional[float]:
        """Efectivo del líder. Devuelve None si el RPC falla."""
        return await self.get_cash_balance(session, proxy_address)

    async def get_onchain_shares(self, session, address: str, token_id: str) -> Optional[float]:
        """
        Shares de un resultado que tiene una wallet, leídas en el contrato de posiciones de
        Polymarket (Conditional Tokens, ERC1155 balanceOf). None si el RPC falla.
        """
        try:
            token_hex = format(int(token_id), "x").rjust(64, "0")
        except (TypeError, ValueError):
            return None
        raw = await self._eth_call_uint(
            session, self.cfg.ctf_contract_address, "0x00fdd58e" + self._abi_address(address) + token_hex
        )
        return None if raw is None else raw / 10 ** self.cfg.collateral_decimals

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

    async def get_market_resolution(self, session, condition_id: str) -> Optional[Dict[str, float]]:
        """
        Devuelve {token_id: pago_por_share} si el mercado ya está resuelto
        (1.0 para el resultado ganador, 0.0 para los demás), o None si sigue abierto
        o aún no tiene ganador.
        """
        if not condition_id:
            return None
        market = await self._get_json(session, f"{self.cfg.clob_host}/markets/{condition_id}", {})
        if not isinstance(market, dict) or not market.get("closed"):
            return None
        tokens = market.get("tokens") or []
        if not any(t.get("winner") for t in tokens):
            return None
        return {str(t.get("token_id")): (1.0 if t.get("winner") else 0.0) for t in tokens}

    def check_slippage(self, leader_price: float, current_market_price: float, side: str) -> Tuple[bool, float]:
        if leader_price <= 0:
            return False, 0.0
        if side.upper() == "BUY":
            slippage = (current_market_price - leader_price) / leader_price
        else:
            slippage = (leader_price - current_market_price) / leader_price

        is_acceptable = slippage <= self.cfg.max_slippage_pct
        return is_acceptable, slippage

    def limit_price(self, leader_price: float, side: str) -> float:
        """
        Peor precio aceptable para la orden: el del líder más el slippage permitido.
        La orden de mercado puede llenarse en varios niveles del libro, pero nunca peor que esto.
        """
        if side.upper() == "BUY":
            price = leader_price * (1 + self.cfg.max_slippage_pct)
        else:
            price = leader_price * (1 - self.cfg.max_slippage_pct)
        return min(0.99, max(0.01, price))

    @staticmethod
    def _parse_fill(resp: Dict, side: str, requested_usdc: float, requested_shares: float, price: float):
        """
        Cantidades realmente ejecutadas según la respuesta del CLOB (makingAmount/takingAmount).
        Compra: making = USDC pagados, taking = shares recibidas. Venta: al revés.
        Si no vienen o no son coherentes, se usan las solicitadas.
        """
        try:
            making = float(resp.get("makingAmount") or 0)
            taking = float(resp.get("takingAmount") or 0)
        except (TypeError, ValueError):
            making = taking = 0.0
        usdc, shares = (making, taking) if side == "BUY" else (taking, making)
        expected_usdc = requested_usdc if side == "BUY" else requested_shares * price
        # Algunas respuestas vienen en unidades base (6 decimales).
        if usdc > 0 and expected_usdc > 0 and usdc > expected_usdc * 1000:
            usdc, shares = usdc / 1e6, shares / 1e6
        if usdc > 0 and shares > 0 and 0 < usdc / shares <= 1:
            return usdc, shares, usdc / shares
        if side == "BUY":
            return requested_usdc, requested_usdc / price, price
        return requested_shares * price, requested_shares, price

    async def execute_order(
        self,
        token_id: str,
        side: str,
        price: float,
        usdc_amount: float = 0.0,
        shares: float = 0.0,
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """
        Envía una orden de mercado FOK (se llena entera o no se ejecuta).
        'price' es el mejor precio actual del libro, usado para la simulación y como
        referencia; 'limit_price' es el peor precio aceptable.
        """
        side = side.upper()
        if self.cfg.dry_run:
            qty_info = f"${usdc_amount:.2f} USDC" if side == "BUY" else f"{shares:.4f} shares"
            message = f"[SIMULACIÓN DRY_RUN] Orden {side} ejecutada por {qty_info} a ${price:.4f}"
            if side == "BUY":
                return OrderResult(True, message, usdc_amount, usdc_amount / price if price > 0 else 0.0, price)
            return OrderResult(True, message, shares * price, shares, price)

        if not self.clob_client:
            return OrderResult(False, self.clob_init_error or "Cliente CLOB no inicializado (revisa tus Secrets en GitHub o .env)")

        try:
            from py_clob_client.clob_types import MarketOrderArgs, OrderType
            from py_clob_client.order_builder.constants import BUY, SELL

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(usdc_amount, 2) if side == "BUY" else round(shares, 2),
                side=BUY if side == "BUY" else SELL,
                price=limit_price if limit_price else price,
                order_type=OrderType.FOK,
            )
            signed_order = await asyncio.to_thread(self.clob_client.create_market_order, order_args)
            resp = await asyncio.to_thread(self.clob_client.post_order, signed_order, OrderType.FOK)
        except Exception as e:
            return OrderResult(False, f"Error en ejecución CLOB: {e}")

        if not isinstance(resp, dict):
            return OrderResult(False, f"Respuesta inesperada del CLOB: {resp}")
        status = str(resp.get("status", "")).lower()
        order_id = resp.get("orderID") or resp.get("orderId") or ""
        if resp.get("success") is False or resp.get("errorMsg"):
            return OrderResult(False, f"Polymarket rechazó la orden: {resp.get('errorMsg') or resp}")
        if status != "matched":
            # 'delayed' (mercados con retardo) o 'unmatched': no hay confirmación de que se llenara.
            if order_id:
                try:
                    await asyncio.to_thread(self.clob_client.cancel, order_id)
                except Exception:
                    pass
            return OrderResult(
                False,
                f"Orden no confirmada (estado '{status or 'desconocido'}', id {order_id or '?'}): "
                "revisa en polymarket.com si llegó a ejecutarse",
            )

        usdc, filled_shares, avg_price = self._parse_fill(resp, side, usdc_amount, shares, price)
        return OrderResult(
            True,
            f"Orden {side} llenada: {filled_shares:.4f} shares por ${usdc:.2f} (precio medio ${avg_price:.4f}, id {order_id})",
            usdc, filled_shares, avg_price,
        )
