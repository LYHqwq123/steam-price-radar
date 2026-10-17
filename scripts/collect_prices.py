"""Generate the static site's price snapshot using Python's standard library."""
from __future__ import annotations

import argparse
import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

try:
    from .editions import load_content
    from .price_history import load_history
    from .providers import (JsonClient, SOURCES, fetch_cici, fetch_heybox, fetch_sonkwo, fetch_sonkwo_price_batches,
                            fetch_steam, fetch_steampy, now_iso, quote)
except ImportError:
    from editions import load_content
    from price_history import load_history
    from providers import (JsonClient, SOURCES, fetch_cici, fetch_heybox, fetch_sonkwo, fetch_sonkwo_price_batches,
                           fetch_steam, fetch_steampy, now_iso, quote)

ROOT = Path(__file__).resolve().parents[1]
CONTENT_PY_BATCH_LIMIT = 30


def stale_quote(previous, source, reason):
    if previous and previous.get("price") is not None:
        value = copy.deepcopy(previous)
        value.update(status="stale", stock="unknown", eligible=False,
                     note="本轮采集失败，保留上次报价与原更新时间；不参与最低价。" + reason)
        return value
    return quote(source, "https://store.steampowered.com/" if source == "steam" else {
        "sonkwo": "https://www.sonkwo.hk/", "steampy": "https://steampy.com/", "steamcici": "https://steamcici.com/",
    }[source], status="error", note="采集失败；未生成估算价格。" + reason)


def history_point(quotes, date):
    def minimum(kind):
        values = [q["price"] for q in quotes if q.get("kind") == kind and q.get("eligible")
                  and q.get("status") == "ok" and q.get("stock") == "in_stock"
                  and isinstance(q.get("price"), (float, int)) and not isinstance(q["price"], bool)]
        return min(values) if values else None
    return {"date": date, "steam": minimum("direct"), "cdk": minimum("cdk")}


def merge_history(old_history, quotes, date):
    points = {p["date"]: p for p in old_history if isinstance(p, dict) and isinstance(p.get("date"), str)}
    point = history_point(quotes, date)
    # Incomplete runs must not erase a valid observation from earlier today.
    if date in points:
        for field in ("steam", "cdk"):
            previous = points[date].get(field)
            if point[field] is None:
                point[field] = previous
            elif isinstance(previous, (int, float)) and not isinstance(previous, bool):
                point[field] = min(point[field], previous)
    if point["steam"] is not None or point["cdk"] is not None:
        points[date] = point
    return [points[key] for key in sorted(points)[-90:]]


def refresh_content_py(client, editions, dlcs, previous, content_status):
    """Rotate exact-package references within a per-game request budget."""
    old_content = {row.get("id", f"app-{row.get('appid')}"): row
                   for row in previous.get("editions", []) + previous.get("dlc", [])}

    def old_py(content):
        return next((q for q in old_content.get(content.get("id"), {}).get("quotes", [])
                     if q.get("source") == "steampy"), None)

    def attempted(content):
        old = old_content.get(content.get("id"), {})
        stamp = old.get("pyAttemptedAt") or content.get("pyAttemptedAt")
        previous_quote = old_py(content)
        if not stamp and previous_quote and previous_quote.get("reasonCode") != "batch_pending":
            stamp = previous_quote.get("checkedAt")
        try:
            value = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            return value.timestamp() if value.tzinfo else 0
        except (ValueError, TypeError, OverflowError, OSError):
            return 0

    candidates = []
    for content in editions + dlcs:
        subid = content.get("subid")
        if (content.get("isStandard") or isinstance(subid, bool) or not isinstance(subid, int) or subid <= 0
                or not any(q.get("source") == "steam" and q.get("status") == "ok"
                           and q.get("price") is not None for q in content.get("quotes", []))):
            continue
        candidates.append(content)
    ordered = sorted(candidates, key=lambda content: (attempted(content), str(content.get("id", ""))))
    selected_ids = {content["id"] for content in ordered[:CONTENT_PY_BATCH_LIMIT]}
    failed = 0
    for content in candidates:
        old = old_content.get(content["id"], {})
        previous_quote = old_py(content)
        if not content.get("pyAttemptedAt"):
            carried_attempt = old.get("pyAttemptedAt")
            if not carried_attempt and previous_quote and previous_quote.get("reasonCode") != "batch_pending":
                carried_attempt = previous_quote.get("checkedAt")
            if carried_attempt:
                content["pyAttemptedAt"] = carried_attempt
        # Metadata caches may carry an older PY quote; replace it once rather
        # than returning duplicate source rows for the same purchase option.
        content["quotes"] = [q for q in content["quotes"] if q.get("source") != "steampy"]
        if content["id"] not in selected_ids:
            if previous_quote:
                pending = copy.deepcopy(previous_quote)
                pending.update(status="stale", stock="unknown", eligible=False,
                               note="SteamPY 版本报价分批待更新，保留上次报价与原检查时间；不参与最低价。")
            else:
                pending = quote("steampy", "https://steampy.com/", status="unavailable",
                                note="SteamPY 版本报价分批待更新；本轮尚未查询此购买包，暂无报价。")
            pending["reasonCode"] = "batch_pending"
            content["quotes"].append(pending)
            continue
        content["pyAttemptedAt"] = now_iso()
        reference = next((q.get("originalPrice") or q.get("price") for q in content["quotes"] if q.get("source") == "steam"), None)
        try:
            content_game = {"appid": content["appid"], "name": content["name"], "nameEn": content["name"]}
            content["quotes"].append(fetch_steampy(client, content_game, content["subid"], reference,
                                                   edition_name=content.get("nameZh") or content["name"]))
        except Exception:
            failed += 1
            content["quotes"].append(stale_quote(previous_quote, "steampy", "该版本的 SteamPY 接口暂不可用。"))
    pending_count = len(candidates) - len(selected_ids)
    content_status.update(pyBatchLimit=CONTENT_PY_BATCH_LIMIT, pyCheckedCount=len(selected_ids),
                          pyPendingCount=pending_count, pyFailedCount=failed)
    if pending_count:
        pending_ids = {content["id"] for content in candidates} - selected_ids
        content_status["note"] = (content_status.get("note", "") +
                                  f" SteamPY 版本参考价本轮最多查询 {CONTENT_PY_BATCH_LIMIT} 项，{pending_count} 项分批待更新；旧价保留原时间并退出比价。")
        for field, rows in (("editions", editions), ("dlc", dlcs)):
            if content_status.get(field) == "ok" and any(row.get("id") in pending_ids for row in rows):
                content_status[field] = "partial"


def collect_game(game, old, client, batches, *, refresh_history=False, refresh_content=False):
    old_quotes = {q["source"]: q for q in old.get("quotes", [])}
    errors, data, subid = {}, {}, None
    try:
        steam, subid, data = fetch_steam(client, game)
    except Exception as exc:
        errors["steam"] = type(exc).__name__
        try:
            steam = fetch_heybox(client, game)
        except Exception:
            steam = stale_quote(old_quotes.get("steam"), "steam", "Steam 与小黑盒国区接口暂不可用。")
    original = steam.get("originalPrice") or steam.get("price")
    quotes = [steam]
    for source, fetch in [
        ("sonkwo", lambda: fetch_sonkwo(client, game, batches)),
        ("steampy", lambda: fetch_steampy(client, game, subid, original)),
        ("steamcici", lambda: fetch_cici(client, game, subid, original)),
    ]:
        try:
            quotes.append(fetch())
        except Exception as exc:
            errors[source] = type(exc).__name__
            quotes.append(stale_quote(old_quotes.get(source), source, "平台接口暂不可用。"))
    appid = int(game["appid"])
    result = {
        "appid": appid, "name": game["name"], "nameEn": game["nameEn"], "aliases": game.get("aliases", []),
        "edition": "标准版", "image": data.get("header_image") or old.get("image") or f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
        "genres": game.get("genres", []), "steamUrl": f"https://store.steampowered.com/app/{appid}/?cc=cn&l=schinese",
        "steamdbUrl": f"https://steamdb.info/app/{appid}/", "quotes": quotes,
        "history": merge_history(old.get("history", []), quotes, now_iso()[:10]),
        "steamHistory": load_history(client, appid, old.get("steamHistory"), refresh=refresh_history),
    }
    editions, dlcs, content_status = load_content(client, game, data, subid, quotes, old, refresh=refresh_content)
    # Additional purchase options use their own exact SubID. Never reuse the
    # standard edition's CDK price for a deluxe package, bundle, or DLC.
    refresh_content_py(client, editions, dlcs, old, content_status)
    result.update(editions=editions, dlc=dlcs, contentStatus=content_status)
    return result, errors


def collect(catalog, previous=None, *, client=None, workers=3, refresh_history=False, refresh_content=False):
    client = client or JsonClient()
    previous = previous or {}
    games = catalog["games"] if isinstance(catalog, dict) else catalog
    if not isinstance(games, list) or not games:
        raise ValueError("catalog must contain a nonempty games array")
    seen = set()
    for game in games:
        appid = int(game["appid"])
        if appid in seen or not game.get("name") or not game.get("nameEn"):
            raise ValueError("catalog requires unique AppIDs and Chinese/English names")
        seen.add(appid)
    previous_games = {int(g["appid"]): g for g in previous.get("games", [])}
    batches = fetch_sonkwo_price_batches(client, games)
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        rows = list(pool.map(lambda game: collect_game(game, previous_games.get(int(game["appid"]), {}), client, batches,
                                                      refresh_history=refresh_history, refresh_content=refresh_content), games))
    generated = now_iso()
    sources = []
    for source, name in SOURCES.items():
        relevant = [next(q for q in game["quotes"] if q["source"] == source) for game, _ in rows]
        failed = sum(source in errors for _, errors in rows)
        valid = sum(q["price"] is not None and q["status"] in ("ok", "unknown") for q in relevant)
        status = "error" if failed == len(games) else "partial" if failed else "ok"
        message = f"{valid}/{len(games)} 款有有效报价"
        if failed:
            message += f"；{failed} 款采集失败"
        if source == "steamcici":
            message += "；平台低价仅供参考，库存及可成交价未确认"
        if source == "steampy":
            message += "；仅 CDKey，异常价已排除"
        sources.append({"id": source, "name": name, "status": status, "checkedAt": generated, "message": message})
    return {"schemaVersion": 1, "generatedAt": generated, "refreshMinutes": 15,
            "country": "CN", "currency": "CNY", "sources": sources, "games": [game for game, _ in rows]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "catalog.json")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "data" / "prices.json")
    parser.add_argument("--limit", type=int, help="collect the first N catalog games")
    parser.add_argument("--refresh-history", action="store_true", help="refresh Heybox history even within its 24-hour cache")
    parser.add_argument("--refresh-content", action="store_true", help="refresh Steam edition and DLC metadata even within its 24-hour cache")
    args = parser.parse_args(argv)
    catalog = json.loads(args.catalog.read_text(encoding="utf-8-sig"))
    games = catalog["games"] if isinstance(catalog, dict) else catalog
    if args.limit is not None:
        if args.limit <= 0:
            parser.error("--limit must be positive")
        games = games[:args.limit]
    previous = json.loads(args.output.read_text(encoding="utf-8")) if args.output.exists() else {}
    snapshot = collect(games, previous, refresh_history=args.refresh_history, refresh_content=args.refresh_content)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(f"Wrote {len(snapshot['games'])} games to {args.output}")
    for source in snapshot["sources"]:
        # Compatible with Windows terminals configured for legacy encodings.
        print(json.dumps(source, ensure_ascii=True))
    return 0 if any(q["status"] == "ok" for g in snapshot["games"] for q in g["quotes"]) else 1


if __name__ == "__main__":
    sys.exit(main())
