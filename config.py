import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

@dataclass
class BotConfig:
    dry_run: bool = os.getenv("DRY_RUN", "true").lower() == "true"
    max_slippage_pct: float = float(os.getenv("MAX_SLIPPAGE_PCT", "0.025"))
    max_position_weight_pct: float = float(os.getenv("MAX_POSITION_WEIGHT_PCT", "0.25"))
    min_order_usdc: float = float(os.getenv("MIN_ORDER_USDC", "2.0"))
    poll_interval: int = int(os.getenv("POLL_INTERVAL_SECONDS", "4"))
    # Cada cuánto se revisa si los mercados con posiciones abiertas ya se resolvieron.
    resolution_check_interval: int = int(os.getenv("RESOLUTION_CHECK_INTERVAL_SECONDS", "300"))
    # En modo continuo, cada cuántas horas avisar por Telegram de que el bot sigue vivo (0 = nunca).
    heartbeat_hours: float = float(os.getenv("HEARTBEAT_HOURS") or "24")
    # Durante cuántos segundos se reutiliza el patrimonio calculado de cada líder.
    equity_cache_seconds: int = int(os.getenv("EQUITY_CACHE_SECONDS") or "60")
    # Lectura de la actividad del líder por páginas (hasta activity_page_size * max_activity_pages operaciones).
    activity_page_size: int = int(os.getenv("ACTIVITY_PAGE_SIZE") or "100")
    max_activity_pages: int = int(os.getenv("MAX_ACTIVITY_PAGES") or "10")
    # Operaciones del líder más antiguas que esto no se copian (el precio ya no es el suyo).
    max_trade_age_minutes: int = int(os.getenv("MAX_TRADE_AGE_MINUTES") or "60")

    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    proxy_address: str = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    clob_api_key: str = os.getenv("CLOB_API_KEY", "")
    clob_secret: str = os.getenv("CLOB_SECRET", "")
    clob_passphrase: str = os.getenv("CLOB_PASSPHRASE", "")
    # Tipo de firma de las órdenes: 2 = cartera proxy de polymarket.com (Gnosis Safe), 1 = Poly proxy, 0 = EOA.
    signature_type: int = int(os.getenv("POLYMARKET_SIGNATURE_TYPE") or "2")

    # RPC de Polygon propio (Alchemy, Infura…). Vacío si no se configura: GitHub Actions pasa
    # los secrets no configurados como cadena vacía.
    polygon_rpc_url: str = os.getenv("POLYGON_RPC_URL") or ""
    # RPCs públicos que se prueban en orden si el propio no está configurado o falla.
    rpc_fallback_urls: tuple = tuple(
        u.strip() for u in (os.getenv("RPC_FALLBACK_URLS") or
        "https://polygon-bor-rpc.publicnode.com,https://polygon.drpc.org,https://1rpc.io/matic,https://polygon-rpc.com"
        ).split(",") if u.strip()
    )
    # Colateral de Polymarket desde CLOB V2 (pUSD en Polygon): saldo con el que se opera.
    collateral_token_address: str = os.getenv("COLLATERAL_TOKEN_ADDRESS") or "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
    # Colateral anterior (USDC.e): se sigue sumando al efectivo de los líderes porque se convierte a pUSD 1:1.
    legacy_collateral_address: str = os.getenv("LEGACY_COLLATERAL_ADDRESS", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174")
    collateral_decimals: int = int(os.getenv("COLLATERAL_DECIMALS") or "6")
    # Contrato donde Polymarket guarda las posiciones (Conditional Tokens, ERC1155).
    ctf_contract_address: str = os.getenv("CTF_CONTRACT_ADDRESS") or "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    http_proxy_url: str = os.getenv("HTTP_PROXY_URL", "").strip()

    db_path: str = os.getenv("DB_PATH", "state/copybot.db")
    traders_json_path: str = os.getenv("TRADERS_JSON_PATH", "traders.json")

    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    chain_id: int = 137
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"

config = BotConfig()
