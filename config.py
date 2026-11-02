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

    private_key: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    proxy_address: str = os.getenv("POLYMARKET_PROXY_ADDRESS", "")
    clob_api_key: str = os.getenv("CLOB_API_KEY", "")
    clob_secret: str = os.getenv("CLOB_SECRET", "")
    clob_passphrase: str = os.getenv("CLOB_PASSPHRASE", "")

    polygon_rpc_url: str = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    http_proxy_url: str = os.getenv("HTTP_PROXY_URL", "").strip()

    db_path: str = os.getenv("DB_PATH", "state/copybot.db")
    traders_json_path: str = os.getenv("TRADERS_JSON_PATH", "traders.json")

    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")

    chain_id: int = 137
    clob_host: str = "https://clob.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"

config = BotConfig()
