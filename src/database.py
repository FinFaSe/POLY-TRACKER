import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

class DatabaseManager:
    """
    Gestiona el aislamiento lógico de múltiples sub-bancas (banks).
    Se guarda en 'state/copybot.db' para que GitHub Actions pueda persistir
    el estado entre ejecuciones sin perder el inventario de posiciones.
    """
    def __init__(self, db_path: str = "state/copybot.db"):
        self.db_path = db_path
        db_dir = os.path.dirname(self.db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_tables()

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_tables(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tracked_traders (
                    proxy_address TEXT PRIMARY KEY,
                    alias TEXT UNIQUE NOT NULL,
                    initial_bank REAL NOT NULL,
                    available_cash REAL NOT NULL,
                    max_drawdown_pct REAL DEFAULT 0.30,
                    is_active INTEGER DEFAULT 1,
                    last_seen_timestamp INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sub_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trader_address TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    market_title TEXT,
                    outcome TEXT,
                    shares REAL NOT NULL DEFAULT 0.0,
                    avg_price REAL NOT NULL DEFAULT 0.0,
                    UNIQUE(trader_address, token_id),
                    FOREIGN KEY(trader_address) REFERENCES tracked_traders(proxy_address)
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS processed_trades (
                    trade_id TEXT PRIMARY KEY,
                    trader_address TEXT NOT NULL,
                    side TEXT NOT NULL,
                    token_id TEXT NOT NULL,
                    leader_usdc REAL NOT NULL,
                    leader_price REAL NOT NULL,
                    copied_usdc REAL NOT NULL,
                    execution_price REAL NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.commit()

    def sync_from_json(self, json_path: str = "traders.json"):
        """
        Sincroniza el archivo 'traders.json' del repositorio con la base de datos.
        Esto permite agregar o editar traders directamente desde la web de GitHub.
        """
        if not os.path.exists(json_path):
            return

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        traders_list = data.get("traders", [])
        now_ts = int(time.time())
        now_iso = datetime.now(timezone.utc).isoformat()

        with self._get_conn() as conn:
            for item in traders_list:
                address = str(item["proxy_address"]).lower().strip()
                alias = str(item["alias"]).strip()
                bank = float(item["bank"])
                max_dd = float(item.get("max_drawdown_pct", 0.30))
                is_active = 1 if item.get("is_active", True) else 0

                existing = conn.execute(
                    "SELECT initial_bank, available_cash FROM tracked_traders WHERE proxy_address = ?",
                    (address,)
                ).fetchone()

                if not existing:
                    conn.execute("""
                        INSERT INTO tracked_traders
                        (proxy_address, alias, initial_bank, available_cash, max_drawdown_pct, is_active, last_seen_timestamp, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """, (address, alias, bank, bank, max_dd, is_active, now_ts, now_iso))
                else:
                    diff = bank - existing["initial_bank"]
                    new_available = max(0.0, existing["available_cash"] + diff)
                    conn.execute("""
                        UPDATE tracked_traders
                        SET alias = ?, initial_bank = ?, available_cash = ?, max_drawdown_pct = ?, is_active = ?
                        WHERE proxy_address = ?
                    """, (alias, bank, new_available, max_dd, is_active, address))
            conn.commit()

    def add_trader(self, address: str, alias: str, bank: float, max_drawdown_pct: float = 0.30):
        address = address.lower().strip()
        now_ts = int(time.time())
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO tracked_traders
                (proxy_address, alias, initial_bank, available_cash, max_drawdown_pct, is_active, last_seen_timestamp, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """, (address, alias, bank, bank, max_drawdown_pct, now_ts, now_iso))
            conn.commit()

    def update_trader_bank(self, alias: str, new_bank: float):
        with self._get_conn() as conn:
            row = conn.execute("SELECT initial_bank, available_cash FROM tracked_traders WHERE alias = ?", (alias,)).fetchone()
            if not row:
                raise ValueError(f"No se encontró el seguimiento con alias '{alias}'")
            diff = new_bank - row["initial_bank"]
            new_available = max(0.0, row["available_cash"] + diff)
            conn.execute("""
                UPDATE tracked_traders
                SET initial_bank = ?, available_cash = ?
                WHERE alias = ?
            """, (new_bank, new_available, alias))
            conn.commit()

    def update_last_seen_timestamp(self, address: str, timestamp_sec: int):
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE tracked_traders
                SET last_seen_timestamp = MAX(last_seen_timestamp, ?)
                WHERE proxy_address = ?
            """, (int(timestamp_sec), address.lower().strip()))
            conn.commit()

    def set_trader_active(self, alias: str, is_active: bool):
        with self._get_conn() as conn:
            conn.execute("UPDATE tracked_traders SET is_active = ? WHERE alias = ?", (1 if is_active else 0, alias))
            conn.commit()

    def get_active_traders(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM tracked_traders WHERE is_active = 1").fetchall()
            return [dict(r) for r in rows]

    def get_all_traders(self) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM tracked_traders").fetchall()
            return [dict(r) for r in rows]

    def get_sub_position(self, trader_address: str, token_id: str) -> Optional[Dict]:
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM sub_positions
                WHERE trader_address = ? AND token_id = ?
            """, (trader_address.lower(), token_id)).fetchone()
            return dict(row) if row else None

    def get_trader_positions(self, trader_address: str) -> List[Dict]:
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM sub_positions
                WHERE trader_address = ? AND shares > 0.0001
            """, (trader_address.lower(),)).fetchall()
            return [dict(r) for r in rows]

    def is_trade_processed(self, trade_id: str) -> bool:
        with self._get_conn() as conn:
            row = conn.execute("SELECT 1 FROM processed_trades WHERE trade_id = ?", (trade_id,)).fetchone()
            return row is not None

    def record_buy_execution(
        self,
        trade_id: str,
        trader_address: str,
        token_id: str,
        market_title: str,
        outcome: str,
        leader_usdc: float,
        leader_price: float,
        copied_usdc: float,
        exec_price: float,
        status: str,
        reason: str = ""
    ):
        trader_address = trader_address.lower()
        shares_bought = copied_usdc / exec_price if exec_price > 0 else 0.0
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            if status in ("EXECUTED", "SIMULATED") and copied_usdc > 0:
                conn.execute("""
                    UPDATE tracked_traders
                    SET available_cash = MAX(0.0, available_cash - ?)
                    WHERE proxy_address = ?
                """, (copied_usdc, trader_address))

                pos = conn.execute("""
                    SELECT shares, avg_price FROM sub_positions
                    WHERE trader_address = ? AND token_id = ?
                """, (trader_address, token_id)).fetchone()

                if pos:
                    old_shares = pos["shares"]
                    old_cost = old_shares * pos["avg_price"]
                    new_shares = old_shares + shares_bought
                    new_avg_price = (old_cost + copied_usdc) / new_shares if new_shares > 0 else exec_price
                    conn.execute("""
                        UPDATE sub_positions
                        SET shares = ?, avg_price = ?
                        WHERE trader_address = ? AND token_id = ?
                    """, (new_shares, new_avg_price, trader_address, token_id))
                else:
                    conn.execute("""
                        INSERT INTO sub_positions (trader_address, token_id, market_title, outcome, shares, avg_price)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (trader_address, token_id, market_title, outcome, shares_bought, exec_price))

            conn.execute("""
                INSERT OR IGNORE INTO processed_trades
                (trade_id, trader_address, side, token_id, leader_usdc, leader_price, copied_usdc, execution_price, status, reason, timestamp)
                VALUES (?, ?, 'BUY', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (trade_id, trader_address, token_id, leader_usdc, leader_price, copied_usdc, exec_price, status, reason, now_iso))
            conn.commit()

    def record_sell_execution(
        self,
        trade_id: str,
        trader_address: str,
        token_id: str,
        leader_usdc: float,
        leader_price: float,
        shares_sold: float,
        exec_price: float,
        status: str,
        reason: str = ""
    ):
        trader_address = trader_address.lower()
        proceeds_usdc = shares_sold * exec_price
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._get_conn() as conn:
            if status in ("EXECUTED", "SIMULATED") and shares_sold > 0:
                conn.execute("""
                    UPDATE tracked_traders
                    SET available_cash = available_cash + ?
                    WHERE proxy_address = ?
                """, (proceeds_usdc, trader_address))

                conn.execute("""
                    UPDATE sub_positions
                    SET shares = MAX(0.0, shares - ?)
                    WHERE trader_address = ? AND token_id = ?
                """, (shares_sold, trader_address, token_id))

            conn.execute("""
                INSERT OR IGNORE INTO processed_trades
                (trade_id, trader_address, side, token_id, leader_usdc, leader_price, copied_usdc, execution_price, status, reason, timestamp)
                VALUES (?, ?, 'SELL', ?, ?, ?, ?, ?, ?, ?, ?)
            """, (trade_id, trader_address, token_id, leader_usdc, leader_price, proceeds_usdc, exec_price, status, reason, now_iso))
            conn.commit()
