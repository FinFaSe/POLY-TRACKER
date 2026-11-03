import asyncio
import json
import os
import time
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
        py-clob-client-v2 envía las órdenes con un cliente httpx propio, creado al importar
        la librería, que no usa HTTP_PROXY_URL. Lo sustituimos por uno que pase por el
        proxy para que las órdenes no salgan desde una IP bloqueada (p. ej. GitHub Actions en EE.UU.).
        """
        import httpx
        from py_clob_client_v2.http_helpers import helpers

        if not hasattr(helpers, "_http_client"):
            raise RuntimeError(
                "Esta versión de py-clob-client-v2 no expone '_http_client'; no se puede aplicar el proxy a las órdenes"
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

            # Polymarket pasó a CLOB V2 (colateral pUSD) en 2026: la librería antigua
            # (py-clob-client) firma órdenes que el exchange ya rechaza.
            from py_clob_client_v2 import ApiCreds, ClobClient

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
                signature_type=self.cfg.signature_type,
                funder=self.cfg.proxy_address
            )
        except Exception as e:
            self.clob_init_error = f"No se pudo inicializar py-clob-client-v2: {e}"
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

    async def _data_api_v2(self, session, path: str, params: Dict[str, str]) -> Tuple[List[Dict], Optional[str], bool]:
        """
        Una página de la Data API v2 (la v1 se retiró el 24/10/2026). Responde
        {"data": [...], "pagination": {"next_cursor", "has_more"}} y pagina con 'cursor'
        ('offset' ya no se admite). Lanza RuntimeError si la respuesta no tiene ese formato.
        """
        data = await self._get_json(session, f"{self.cfg.data_api_host}{path}", params)
        # Exigir el campo 'data': una respuesta de error ({"error": ...}) no es una lista vacía.
        if not isinstance(data, dict) or "data" not in data or not isinstance(data["data"], (list, type(None))):
            raise RuntimeError(f"La Data API v2 no devolvió datos válidos para {path}")
        pagination = data.get("pagination") or {}
        return list(data.get("data") or []), pagination.get("next_cursor"), bool(pagination.get("has_more"))

    @staticmethod
    def normalize_activity(row: Dict) -> Dict:
        """
        Traduce una fila de actividad de la v2 (snake_case) a los nombres que usa el bot,
        que son los de la antigua v1. Conserva los campos originales.
        """
        out = dict(row)
        for v1_key, v2_key in (
            ("transactionHash", "transaction_hash"), ("asset", "token_id"),
            ("conditionId", "condition_id"), ("usdcSize", "usdc_size"),
            ("proxyWallet", "proxy_wallet"), ("outcomeIndex", "outcome_index"),
        ):
            if v1_key not in out and v2_key in row:
                out[v1_key] = row[v2_key]
        return out

    async def get_leader_trades_since(self, session, proxy_address: str, since_timestamp: int) -> Tuple[List[Dict], bool]:
        """
        Devuelve las operaciones del líder desde 'since_timestamp' (segundos), de la más
        reciente a la más antigua, recorriendo la Data API v2 página a página.
        El segundo valor es True si se alcanzó el tope de páginas antes de llegar a
        'since_timestamp', es decir, si pueden faltar operaciones más antiguas.
        """
        page_size = max(1, self.cfg.activity_page_size)
        params = {
            "user": proxy_address.lower(), "type": "TRADE", "limit": str(page_size),
            "sort_by": "TIMESTAMP", "sort_direction": "DESC",
        }
        trades: List[Dict] = []
        seen_cursors = set()
        for _ in range(max(1, self.cfg.max_activity_pages)):
            # Si falla una página, se lanza la excepción y no se procesa nada: procesar solo
            # las recientes dejaría atrás para siempre las más antiguas. Se reintenta en el próximo ciclo.
            rows, next_cursor, has_more = await self._data_api_v2(session, "/v2/activity", params)
            page = [self.normalize_activity(r) for r in rows]
            trades.extend(page)
            if not has_more or not page:
                return trades, False
            oldest = min(self._timestamp_seconds(t.get("timestamp", 0)) for t in page)
            if oldest < since_timestamp:
                return trades, False
            if not next_cursor or next_cursor in seen_cursors:
                raise RuntimeError("La Data API v2 indicó más páginas de actividad pero sin un cursor nuevo")
            seen_cursors.add(next_cursor)
            params = dict(params, cursor=str(next_cursor))
        return trades, True

    async def get_leaderboard(self, session, period: str = "MONTH", order_by: str = "PNL", limit: int = 20) -> List[Dict]:
        """
        Ranking de traders de Polymarket (Data API v2). period: DAY, WEEK, MONTH o ALL;
        order_by: PNL (ganancias) o VOL (volumen). Lanza RuntimeError si la API falla.
        """
        rows: List[Dict] = []
        params = {"timePeriod": period, "orderBy": order_by, "limit": str(min(limit, 50))}
        seen_cursors = set()
        while len(rows) < limit:
            page, next_cursor, has_more = await self._data_api_v2(session, "/v2/leaderboard", params)
            rows.extend(page)
            if not page or not has_more or not next_cursor or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            params = dict(params, cursor=str(next_cursor))
        return rows[:limit]

    async def count_recent_trades(self, session, address: str, days: int = 7, max_pages: int = 3) -> Tuple[int, bool, Optional[int]]:
        """
        Cuántas operaciones hizo una wallet en los últimos 'days' días (hasta max_pages páginas
        de 100). Devuelve (número, si se llegó al tope de páginas, timestamp de la última).
        """
        since = int(time.time()) - days * 86400
        params = {"user": address.lower(), "type": "TRADE", "limit": "100",
                  "sort_by": "TIMESTAMP", "sort_direction": "DESC"}
        count, last_ts = 0, None
        for _ in range(max_pages):
            rows, next_cursor, has_more = await self._data_api_v2(session, "/v2/activity", params)
            stamps = [self._timestamp_seconds(r.get("timestamp", 0)) for r in rows]
            if stamps and last_ts is None:
                last_ts = max(stamps)
            count += sum(1 for ts in stamps if ts >= since)
            if not has_more or not next_cursor or not stamps or min(stamps) < since:
                return count, False, last_ts
            params = dict(params, cursor=str(next_cursor))
        return count, True, last_ts

    async def get_market_trades(self, session, condition_id: str, since_ts: int, max_pages: int = 10,
                                end_ts: Optional[int] = None) -> Tuple[List[Dict], bool]:
        """
        Operaciones de todas las carteras en un mercado entre 'since_ts' y 'end_ts' (Data API v2,
        /v2/trades?condition=…), de la más reciente a la más antigua y con los nombres de
        campos que usa el bot. El segundo valor es True si se alcanzó el tope de páginas
        sin llegar a 'since_ts' (faltan datos más antiguos).

        Con 'end_ts' se pide a la API que empiece en ese momento (parámetro 'end'), lo que
        evita recorrer toda la actividad posterior en mercados muy activos. Si la API no lo
        admite (lo rechaza o lo ignora), se recuerda y se recorre desde la operación más reciente.
        """
        base = {"condition": condition_id, "limit": "100"}
        use_end = end_ts is not None and getattr(self, "_trades_end_supported", True)
        params = dict(base, end=str(end_ts)) if use_end else dict(base)
        trades: List[Dict] = []
        seen_cursors = set()
        for page_number in range(max_pages):
            try:
                rows, next_cursor, has_more = await self._data_api_v2(session, "/v2/trades", params)
            except RuntimeError:
                if use_end and page_number == 0:
                    self._trades_end_supported = False
                    return await self.get_market_trades(session, condition_id, since_ts, max_pages, end_ts)
                raise
            page = [self.normalize_activity(r) for r in rows]
            stamps = [self._timestamp_seconds(t.get("timestamp", 0)) for t in page]
            if use_end and page_number == 0 and stamps and max(stamps) > end_ts:
                self._trades_end_supported = False  # la API ignora 'end'
            trades.extend(page)
            if not has_more or not next_cursor or not stamps or min(stamps) < since_ts or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            params = dict(params, cursor=str(next_cursor))
        else:
            return self._in_range(trades, since_ts, end_ts), True
        return self._in_range(trades, since_ts, end_ts), False

    def _in_range(self, trades: List[Dict], since_ts: int, end_ts: Optional[int]) -> List[Dict]:
        return [t for t in trades
                if self._timestamp_seconds(t.get("timestamp", 0)) >= since_ts
                and (end_ts is None or self._timestamp_seconds(t.get("timestamp", 0)) <= end_ts)]

    async def get_positions(self, session, address: str, max_pages: int = 20) -> Optional[List[Dict]]:
        """Posiciones abiertas de una wallet (Data API v2, todas las páginas). None si la API falla."""
        params = {"user": address.lower(), "limit": "100"}
        positions: List[Dict] = []
        try:
            for _ in range(max_pages):
                rows, next_cursor, has_more = await self._data_api_v2(session, "/v2/positions", params)
                positions.extend(rows)
                if not has_more or not next_cursor:
                    break
                params = dict(params, cursor=str(next_cursor))
        except Exception:
            return None
        return positions

    @staticmethod
    def _timestamp_seconds(raw_ts) -> int:
        if isinstance(raw_ts, str) and raw_ts and not raw_ts.replace(".", "", 1).isdigit():
            from datetime import datetime
            return int(datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).timestamp())
        ts = int(float(raw_ts or 0))
        return ts // 1000 if ts > 1_000_000_000_000 else ts

    async def get_leader_portfolio_value(self, session, proxy_address: str) -> Optional[float]:
        """
        Valor de las posiciones abiertas del líder: suma de 'current_value' de sus posiciones
        en la Data API v2 (no incluye su efectivo). Devuelve None si la API falla, para no
        confundir un error con una cartera vacía.
        """
        positions = await self.get_positions(session, proxy_address)
        if positions is None:
            return None
        return sum(float(p.get("current_value") or 0.0) for p in positions)

    def rpc_urls(self) -> List[str]:
        """RPCs de Polygon en orden de preferencia: el propio (si hay) y luego los públicos."""
        urls = [self.cfg.polygon_rpc_url] if self.cfg.polygon_rpc_url else []
        urls += [u for u in self.cfg.rpc_fallback_urls if u not in urls]
        # El último que funcionó va primero, para no repetir los que fallan en cada llamada.
        preferred = getattr(self, "_preferred_rpc", None)
        if preferred in urls:
            urls.remove(preferred)
            urls.insert(0, preferred)
        return urls

    async def _eth_call_uint(self, session, contract: str, data: str) -> Optional[int]:
        """
        eth_call en Polygon que devuelve un uint256. Prueba cada RPC en orden (dos veces si
        solo hay uno). None si ninguno responde.
        """
        urls = self.rpc_urls()
        if not urls:
            return None
        payload = {
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": contract, "data": data}, "latest"],
        }
        for url in (urls if len(urls) > 1 else urls * 2):
            try:
                response = await self._post_json(session, url, payload)
            except Exception:
                response = None
            result = response.get("result") if isinstance(response, dict) else None
            if isinstance(result, str) and result.startswith("0x"):
                self._preferred_rpc = url
                return int(result, 16) if len(result) > 2 else 0
        return None

    @staticmethod
    def _abi_address(address: str) -> str:
        return address.lower().replace("0x", "").rjust(64, "0")

    async def _erc20_balance(self, session, token: str, address: str) -> Optional[float]:
        raw = await self._eth_call_uint(session, token, "0x70a08231" + self._abi_address(address))
        return None if raw is None else raw / 10 ** self.cfg.collateral_decimals

    async def get_cash_balance(self, session, address: str) -> Optional[float]:
        """
        Saldo con el que se puede operar: pUSD, el colateral de Polymarket desde CLOB V2.
        Se lee en Polygon con ERC20 balanceOf(). None si el RPC falla.
        """
        return await self._erc20_balance(session, self.cfg.collateral_token_address, address)

    async def get_leader_cash_balance(self, session, proxy_address: str) -> Optional[float]:
        """
        Efectivo del líder: pUSD más el USDC.e que aún no haya convertido (se puede pasar
        a pUSD en cualquier momento, así que también es capital suyo). None si el RPC falla.
        """
        pusd = await self._erc20_balance(session, self.cfg.collateral_token_address, proxy_address)
        if pusd is None:
            return None
        if not self.cfg.legacy_collateral_address:
            return pusd
        legacy = await self._erc20_balance(session, self.cfg.legacy_collateral_address, proxy_address)
        return None if legacy is None else pusd + legacy

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

    async def get_leader_token_balance(self, session, proxy_address: str, token_id: str) -> Optional[float]:
        """
        Shares que le quedan al líder de un resultado (para vender en la misma proporción).
        Se leen en el contrato de posiciones; si el RPC falla, en sus posiciones de la Data API v2.
        Devuelve None si no se puede saber: antes un fallo contaba como 0 y se vendía el 100%.
        """
        onchain = await self.get_onchain_shares(session, proxy_address, token_id)
        if onchain is not None:
            return onchain
        positions = await self.get_positions(session, proxy_address)
        if positions is None:
            return None
        for p in positions:
            if str(p.get("token_id") or p.get("asset")) == str(token_id):
                return float(p.get("current_size") or p.get("size") or 0.0)
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
        Devuelve {token_id: pago_por_share} si el mercado ya está resuelto (normalmente 1.0
        para el resultado ganador y 0.0 para los demás; 0.5 y 0.5 si se resolvió a partes
        iguales), o None si sigue abierto o no se puede saber.
        """
        if not condition_id:
            return None
        market = await self._get_json(session, f"{self.cfg.clob_host}/markets/{condition_id}", {})
        tokens = (market.get("tokens") or []) if isinstance(market, dict) else []
        if not tokens:
            return None
        if market.get("closed") and any(t.get("winner") for t in tokens):
            return {str(t.get("token_id")): (1.0 if t.get("winner") else 0.0) for t in tokens}
        # El CLOB puede tardar en marcar el mercado como cerrado aunque ya esté resuelto (y
        # cobrable) en la blockchain: el resultado oficial está en el contrato de posiciones.
        return await self._onchain_resolution(session, condition_id, [str(t.get("token_id")) for t in tokens])

    async def _onchain_resolution(self, session, condition_id: str, token_ids: List[str]) -> Optional[Dict[str, float]]:
        """
        Pago por share de cada resultado según el contrato de posiciones (CTF):
        payoutNumerators(conditionId, i) / payoutDenominator(conditionId). Un denominador 0
        significa que aún no está resuelto. Los tokens van en el orden de los resultados
        (el mismo en que los devuelve el CLOB). Cubre también las resoluciones 50/50.
        """
        cid = condition_id.lower().replace("0x", "").rjust(64, "0")
        denominator = await self._eth_call_uint(session, self.cfg.ctf_contract_address, "0xdd34de67" + cid)
        if not denominator:
            return None
        payouts = {}
        for index, token_id in enumerate(token_ids):
            numerator = await self._eth_call_uint(
                session, self.cfg.ctf_contract_address, "0x0504c814" + cid + format(index, "x").rjust(64, "0")
            )
            if numerator is None:
                return None
            payouts[token_id] = numerator / denominator
        return payouts

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
            from py_clob_client_v2 import MarketOrderArgs, OrderType

            order_args = MarketOrderArgs(
                token_id=token_id,
                amount=round(usdc_amount, 2) if side == "BUY" else round(shares, 2),
                side=side,
                price=limit_price if limit_price else price,
                order_type=OrderType.FOK,
            )
            # create_and_post reintenta solo si el exchange cambia de versión entre medias.
            resp = await asyncio.to_thread(
                self.clob_client.create_and_post_market_order, order_args, None, OrderType.FOK
            )
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
                    from py_clob_client_v2 import OrderPayload
                    await asyncio.to_thread(self.clob_client.cancel_order, OrderPayload(orderID=order_id))
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
