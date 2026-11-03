"""
Lectura y escritura de traders.json, la fuente de verdad de los traders seguidos.

El bot sincroniza este archivo con la base de datos en cada ejecución, así que los
cambios hechos con cli.py se guardan aquí para que no se pierdan en la siguiente.
"""
import json
import os
from typing import Dict, List


def load(path: str) -> Dict:
    if not os.path.exists(path):
        return {"traders": []}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    data.setdefault("traders", [])
    return data


def save(path: str, data: Dict):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def _find(traders: List[Dict], alias: str) -> Dict:
    for item in traders:
        if str(item.get("alias", "")).strip() == alias:
            return item
    raise ValueError(f"No hay ningún trader con alias '{alias}' en traders.json")


def add_trader(path: str, address: str, alias: str, bank: float, max_drawdown_pct: float = 0.30):
    data = load(path)
    address = address.lower().strip()
    for item in data["traders"]:
        if str(item.get("alias", "")).strip() == alias:
            raise ValueError(f"Ya existe un trader con alias '{alias}' en traders.json")
        if str(item.get("proxy_address", "")).lower().strip() == address:
            raise ValueError(f"La dirección {address} ya está en traders.json (alias '{item.get('alias')}')")
    data["traders"].append({
        "alias": alias,
        "proxy_address": address,
        "bank": float(bank),
        "max_drawdown_pct": float(max_drawdown_pct),
        "is_active": True,
    })
    save(path, data)


def update_bank(path: str, alias: str, bank: float):
    data = load(path)
    _find(data["traders"], alias)["bank"] = float(bank)
    save(path, data)


def set_active(path: str, alias: str, is_active: bool):
    data = load(path)
    _find(data["traders"], alias)["is_active"] = bool(is_active)
    save(path, data)
