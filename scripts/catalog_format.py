"""Read and write the compact Steam search directory (schema v2).

Schema v2 stores each game as a short array to keep a full-library directory
small enough to publish and load:

    [appid, name]
    [appid, name, nameEn]
    [appid, name, nameEn, [aliases]]

v1 object rows remain readable so an existing checkout can migrate in place.
"""
from __future__ import annotations

import json
from pathlib import Path
import re


SCHEMA_VERSION = 2


def app_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        return None
    number = int(value)
    return number if 0 < number <= 2147483647 else None


def clean_text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def normalize_aliases(value):
    if not isinstance(value, list):
        return []
    result = []
    for alias in value:
        text = clean_text(alias)
        if text and text not in result:
            result.append(text)
    return result


def _row_to_game(row):
    if isinstance(row, dict):
        if row.get("type", "game") != "game":
            return None
        appid = app_id(row.get("appid"))
        name = clean_text(row.get("name"))
        if not appid or not name:
            return None
        name_en = clean_text(row.get("nameEn"))
        result = {"appid": appid, "name": name, "aliases": normalize_aliases(row.get("aliases"))}
        if name_en and name_en != name:
            result["nameEn"] = name_en
        return result
    if isinstance(row, list) and 2 <= len(row) <= 4:
        appid = app_id(row[0])
        name = clean_text(row[1])
        if not appid or not name:
            return None
        name_en = clean_text(row[2]) if len(row) > 2 else ""
        result = {"appid": appid, "name": name, "aliases": normalize_aliases(row[3]) if len(row) > 3 else []}
        if name_en and name_en != name:
            result["nameEn"] = name_en
        return result
    return None


def unpack_games(catalog):
    """Return normalized game dicts from a v1 object catalog or a v2 array catalog."""
    if isinstance(catalog, dict):
        rows = catalog.get("games")
    else:
        rows = catalog
    if not isinstance(rows, list):
        return []
    games, seen = [], set()
    for row in rows:
        game = _row_to_game(row)
        if not game or game["appid"] in seen:
            continue
        seen.add(game["appid"])
        games.append(game)
    return games


def catalog_appids(catalog):
    """Return sorted unique AppIDs without expanding every game into a dict."""
    rows = catalog.get("games") if isinstance(catalog, dict) else catalog
    if not isinstance(rows, list):
        return []
    ids = set()
    for row in rows:
        if isinstance(row, list) and len(row) >= 2:
            appid = app_id(row[0])
        elif isinstance(row, dict) and row.get("type", "game") == "game":
            appid = app_id(row.get("appid"))
        else:
            appid = None
        if appid:
            ids.add(appid)
    return sorted(ids)


def pack_game(game):
    appid = app_id(game.get("appid"))
    name = clean_text(game.get("name"))
    if not appid or not name:
        return None
    name_en = clean_text(game.get("nameEn"))
    aliases = normalize_aliases(game.get("aliases"))
    row = [appid, name]
    if name_en and name_en != name:
        row.append(name_en)
    if aliases:
        if len(row) == 2:
            row.append("")
        row.append(aliases)
    return row


def pack_games(games):
    """Serialize game dicts as compact, deterministic v2 rows sorted by AppID."""
    packed, seen = {}, set()
    for game in games:
        if not isinstance(game, dict):
            continue
        appid = app_id(game.get("appid"))
        if not appid or appid in seen:
            continue
        row = pack_game(game)
        if not row:
            continue
        seen.add(appid)
        packed[appid] = row
    return [packed[appid] for appid in sorted(packed)]


def read_catalog(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_catalog(path, catalog):
    """Atomically write a catalog document; games must already be v2 rows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")) + "\n",
                          encoding="utf-8")
    temporary.replace(path)


def valid_appid_text(value):
    text = str(value or "")
    return bool(re.fullmatch(r"[0-9]+", text)) and 0 < int(text) <= 2147483647
