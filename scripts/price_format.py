"""Compact, sharded Steam current-price storage (schema v2).

A full Steam directory contains roughly 184k games. Prices are split by
``appid % shard_count`` so a browser can read a small manifest and fetch shards
progressively. Amounts are stored as integer CNY cents.

Shard row: [appid, finalCents, originalCents, discount, statusCode, checkedAt?]
A sixth item, when present, overrides the shard time; ``null`` means the app
has never been checked. Status codes: 0 ok, 1 unavailable, 2 stale, 3 error.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import tempfile


SCHEMA_VERSION = 2
SHARD_COUNT = 64
STATUS_BY_NAME = {"ok": 0, "unavailable": 1, "stale": 2, "error": 3}
NAME_BY_STATUS = {value: key for key, value in STATUS_BY_NAME.items()}
SOURCE_URL = "https://store.steampowered.com/api/appdetails"


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def app_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str) and (not value.isascii() or not value.isdigit()):
        return None
    number = int(value)
    return number if 0 < number <= 2147483647 else None


def amount(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(float(value), 2) if math.isfinite(value) and value >= 0 else None


def to_cents(value):
    value = amount(value)
    return int(round(value * 100)) if value is not None else None


def from_cents(value):
    return round(value / 100, 2) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def status_code(value, default="error"):
    return STATUS_BY_NAME.get(value, STATUS_BY_NAME[default])


def status_name(value):
    return NAME_BY_STATUS.get(value)


def shard_index(appid, shard_count):
    if not isinstance(shard_count, int) or isinstance(shard_count, bool) or not 1 <= shard_count <= 1000:
        raise ValueError("shard_count must be an integer between 1 and 1000")
    if not isinstance(appid, int) or isinstance(appid, bool) or appid <= 0:
        raise ValueError("appid must be a positive integer")
    return appid % shard_count


def shard_filename(index):
    return f"price-{index:02d}.json"


def shard_relative_path(index, prefix="price-shards"):
    return f"{prefix}/{shard_filename(index)}"


def pack_record(record, shard_time=None):
    appid = app_id(record.get("appid"))
    if not appid:
        return None
    status = record.get("status") if record.get("status") in STATUS_BY_NAME else "error"
    priced = status in ("ok", "stale")
    price = to_cents(record.get("price")) if priced else None
    original = to_cents(record.get("originalPrice")) if priced else None
    discount = amount(record.get("discount")) if priced else None
    if discount is not None:
        discount = int(round(discount)) if 0 <= discount <= 100 else None
    if priced and price is None:
        status = "error"
        priced = False
    row = [appid, price if priced else None, original if priced else None,
           discount if priced else None, status_code(status)]
    checked = record.get("checkedAt") if isinstance(record.get("checkedAt"), str) else None
    if checked != shard_time:
        row.append(checked)
    return row


def unpack_record(row, shard_time=None):
    if not isinstance(row, list) or len(row) < 5:
        return None
    appid = app_id(row[0])
    status = status_name(row[4]) if isinstance(row[4], int) and not isinstance(row[4], bool) else None
    if not appid or not status:
        return None
    price, original, discount = from_cents(row[1]), from_cents(row[2]), amount(row[3])
    if discount is not None:
        discount = int(round(discount)) if 0 <= discount <= 100 else None
    if status == "ok" and price is None:
        status = "unavailable"
    if status == "stale" and price is None:
        status = "error"
    checked = row[5] if len(row) > 5 else shard_time
    return {"appid": appid, "price": price, "originalPrice": original, "discount": discount,
            "status": status, "checkedAt": checked if isinstance(checked, str) and checked else None}


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def atomic_json(path, value, *, compact=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            if compact:
                json.dump(value, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            else:
                json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def read_shard(path, expected_index=None, expected_count=None):
    payload = read_json(path)
    if not isinstance(payload, dict) or payload.get("schemaVersion") != SCHEMA_VERSION:
        return None
    index, count, rows = payload.get("shard"), payload.get("shardCount"), payload.get("games")
    checked = payload.get("checkedAt")
    if (not isinstance(index, int) or isinstance(index, bool) or not isinstance(count, int)
            or isinstance(count, bool) or not isinstance(rows, list) or not timestamp(checked)):
        return None
    if expected_index is not None and index != expected_index:
        return None
    if expected_count is not None and count != expected_count:
        return None
    return {"shard": index, "shardCount": count, "checkedAt": checked, "games": rows,
            "schemaVersion": SCHEMA_VERSION}


def write_shard(path, index, count, checked_at, records):
    rows = []
    for record in records:
        row = pack_record(record, checked_at)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda row: row[0])
    atomic_json(path, {"schemaVersion": SCHEMA_VERSION, "shard": index, "shardCount": count,
                       "checkedAt": checked_at, "games": rows})
    return {"file": Path(path).name, "index": index, "count": len(rows), "checkedAt": checked_at}


def load_shard_records(path, expected_index=None, expected_count=None, shard_time=None):
    shard = read_shard(path, expected_index, expected_count)
    if not shard:
        return {}, None
    records = {}
    for row in shard["games"]:
        record = unpack_record(row, shard["checkedAt"] if shard_time is None else shard_time)
        if record:
            records[record["appid"]] = record
    return records, shard


def legacy_records(payload):
    """Read a schema v1 direct-prices document for one-time migration."""
    if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
        return {}
    records = {}
    for raw in payload["games"]:
        if not isinstance(raw, dict):
            continue
        appid = app_id(raw.get("appid"))
        if not appid:
            continue
        records[appid] = {"appid": appid, "price": amount(raw.get("price")),
                          "originalPrice": amount(raw.get("originalPrice")),
                          "discount": amount(raw.get("discount")),
                          "status": raw.get("status") if raw.get("status") in STATUS_BY_NAME else "error",
                          "checkedAt": raw.get("checkedAt") if isinstance(raw.get("checkedAt"), str) else None}
    return records


def _relative_file(path, output_dir, index, prefix):
    try:
        return path.resolve().relative_to(Path(output_dir).resolve()).as_posix()
    except ValueError:
        return shard_relative_path(index, prefix)


def build_manifest(shard_dir, *, catalog_appids=None, output_dir=None, refresh_minutes=120,
                   generated_at=None, prefix="price-shards", shard_count=SHARD_COUNT):
    """Validate all shard files and describe their coverage in one manifest."""
    shard_dir = Path(shard_dir)
    output_dir = Path(output_dir) if output_dir else shard_dir.parent
    catalog = set(catalog_appids) if catalog_appids is not None else None
    generated_at = generated_at or now_iso()
    generated_ts = timestamp(generated_at)
    cutoff = generated_ts.timestamp() - refresh_minutes * 60 * 3 if generated_ts else None
    shards, seen = [], set()
    priced = duplicate_count = invalid_count = stale_records = 0
    status_counts = {name: 0 for name in STATUS_BY_NAME}
    checked_times = []
    for index in range(shard_count):
        path = shard_dir / shard_filename(index)
        shard = read_shard(path, index, shard_count)
        entry = {"file": _relative_file(path, output_dir, index, prefix), "index": index,
                 "count": 0, "priced": 0, "checkedAt": None,
                 "available": bool(shard)}
        if not shard:
            shards.append(entry)
            continue
        shard_latest = None
        for row in shard["games"]:
            record = unpack_record(row, shard["checkedAt"])
            if not record or record["appid"] % shard_count != index:
                invalid_count += 1
                continue
            if record["appid"] in seen:
                duplicate_count += 1
                continue
            if catalog is not None and record["appid"] not in catalog:
                continue
            seen.add(record["appid"])
            entry["count"] += 1
            status_counts[record["status"]] += 1
            if record["status"] == "ok" and record["price"] is not None:
                entry["priced"] += 1
                priced += 1
            checked = timestamp(record["checkedAt"])
            if checked is None:
                stale_records += 1
            else:
                checked_times.append(checked)
                if cutoff is not None and checked.timestamp() < cutoff:
                    stale_records += 1
                if shard_latest is None or checked.timestamp() > shard_latest.timestamp():
                    shard_latest = checked
        entry["checkedAt"] = shard_latest.isoformat(timespec="seconds").replace("+00:00", "Z") if shard_latest else None
        shards.append(entry)
    missing = len(catalog - seen) if catalog is not None else 0
    covered = len(seen)
    checked_at = max(checked_times).isoformat(timespec="seconds").replace("+00:00", "Z") if checked_times else None
    oldest = min(checked_times).isoformat(timespec="seconds").replace("+00:00", "Z") if checked_times else None
    if not seen:
        status = "error"
    elif priced == 0:
        status = "partial"
    elif missing or duplicate_count or invalid_count or stale_records or status_counts["error"] or status_counts["stale"]:
        status = "partial"
    else:
        status = "ok"
    return {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "checkedAt": checked_at,
        "oldestCheckedAt": oldest,
        "source": "steam",
        "sourceUrl": SOURCE_URL,
        "country": "CN",
        "currency": "CNY",
        "refreshMinutes": int(refresh_minutes),
        "status": status,
        "gameCount": covered,
        "catalogGameCount": len(catalog) if catalog is not None else covered,
        "pricedCount": priced,
        "missingCount": missing,
        "statusCounts": status_counts,
        "staleRecordCount": stale_records,
        "duplicateCount": duplicate_count,
        "invalidCount": invalid_count,
        "shardCount": shard_count,
        "availableShardCount": sum(1 for shard in shards if shard["available"]),
        "shards": shards,
        "scope": "Steam 中国大陆商店所有公开游戏的当前人民币价格概览，不含具体购买包、DLC、CDK 与历史。",
        "note": "未返回价格不等于免费；具体版本、DLC、CDK 和历史由详情采集任务另行补充。",
    }


def write_manifest(path, manifest):
    atomic_json(path, manifest)
