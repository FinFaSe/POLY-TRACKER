import json
import os
import sqlite3
import time
from contextlib import contextmanager
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

    @contextmanager
    def _get_conn(self):
        # 'with sqlite3.connect()' confirma la transacción pero no cierra la conexión;
        # aquí se confirma (o se deshace si hay error) y además se cierra.
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

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
                    condition_id TEXT,
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

            # Migración: bases de datos creadas antes de guardar el condition_id del mercado.
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(sub_positions)")}
            if "condition_id" not in columns:
                conn.execute("ALTER TABLE sub_positions ADD COLUMN condition_id TEXT")
            conn.commit()

    def sync_from_json(self, json_path: str = "traders.json") -> List[str]:
        """
        Sincroniza el archivo 'traders.json' del repositorio con la base de datos.
        Esto permite agregar o editar traders directamente desde la web de GitHub.

        traders.json manda: los traders que ya no aparecen en él se desactivan
        (sus posiciones y su historial se conservan). Devuelve los alias desactivados.
        """
        if not os.path.exists(json_path):
            return []

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
                    "SELECT alias, initial_bank, available_cash, max_drawdown_pct, is_active "
                    "FROM tracked_traders WHERE proxy_address = ?",
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
                    # Solo escribir si algo cambia: reescribir los mismos valores modifica el
                    # archivo y, en GitHub Actions, provoca un commit de estado en cada ejecución.
                    if (existing["alias"], existing["initial_bank"], existing["available_cash"],
                            existing["max_drawdown_pct"], existing["is_active"]) == (alias, bank, new_available, max_dd, is_active):
                        continue
                    conn.execute("""
                        UPDATE tracked_traders
                        SET alias = ?, initial_bank = ?, available_cash = ?, max_drawdown_pct = ?, is_active = ?
                        WHERE proxy_address = ?
                    """, (alias, bank, new_available, max_dd, is_active, address))

            listed = [str(item["proxy_address"]).lower().strip() for item in traders_list]
            placeholders = ",".join("?" * len(listed)) or "''"
            removed = conn.execute(
                f"SELECT alias FROM tracked_traders WHERE is_active = 1 AND proxy_address NOT IN ({placeholders})",
                listed,
            ).fetchall()
            conn.execute(
                f"UPDATE tracked_traders SET is_active = 0 WHERE is_active = 1 AND proxy_address NOT IN ({placeholders})",
                listed,
            )
            conn.commit()
        return [r["alias"] for r in removed]

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
                SET last_seen_timestamp = ?
                WHERE proxy_address = ? AND last_seen_timestamp < ?
            """, (int(timestamp_sec), address.lower().strip(), int(timestamp_sec)))
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
        reason: str = "",
        condition_id: str = ""
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
                        SET shares = ?, avg_price = ?, condition_id = COALESCE(NULLIF(condition_id, ''), ?)
                        WHERE trader_address = ? AND token_id = ?
                    """, (new_shares, new_avg_price, condition_id, trader_address, token_id))
                else:
                    conn.execute("""
                        INSERT INTO sub_positions (trader_address, token_id, market_title, outcome, condition_id, shares, avg_price)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (trader_address, token_id, market_title, outcome, condition_id, shares_bought, exec_price))

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

    def settle_resolved_position(self, trader_address: str, token_id: str, payout_per_share: float) -> float:
        """
        Cierra la posición de una sub-banca en un mercado ya resuelto: abona
        shares * pago (1.0 si ganó, 0.0 si perdió) al efectivo disponible y deja la
        posición en cero. Devuelve el USDC abonado.
        """
        trader_address = trader_address.lower()
        now = datetime.now(timezone.utc)
        with self._get_conn() as conn:
            pos = conn.execute("""
                SELECT shares FROM sub_positions
                WHERE trader_address = ? AND token_id = ?
            """, (trader_address, token_id)).fetchone()
            if not pos or pos["shares"] <= 0.0001:
                return 0.0

            shares = pos["shares"]
            payout_usdc = shares * payout_per_share
            conn.execute("""
                UPDATE tracked_traders
                SET available_cash = available_cash + ?
                WHERE proxy_address = ?
            """, (payout_usdc, trader_address))
            conn.execute("""
                UPDATE sub_positions SET shares = 0.0
                WHERE trader_address = ? AND token_id = ?
            """, (trader_address, token_id))

            status = "RESOLVED_WON" if payout_per_share > 0 else "RESOLVED_LOST"
            conn.execute("""
                INSERT OR IGNORE INTO processed_trades
                (trade_id, trader_address, side, token_id, leader_usdc, leader_price, copied_usdc, execution_price, status, reason, timestamp)
                VALUES (?, ?, 'RESOLVE', ?, 0.0, 0.0, ?, ?, ?, ?, ?)
            """, (
                f"resolve_{trader_address}_{token_id}_{int(now.timestamp())}",
                trader_address, token_id, payout_usdc, payout_per_share, status,
                f"Mercado resuelto: {shares:.4f} shares a ${payout_per_share:.2f}", now.isoformat()
            ))
            conn.commit()
            return payout_usdc
