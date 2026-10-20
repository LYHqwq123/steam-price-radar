"""Synchronize the full Steam game directory without collecting prices.

With STEAM_WEB_API_KEY, use the official store API and its modification cursor.
Without a key, traverse the public Chinese store search page by page. A single
long run can walk the complete public CN result set (about 184k games); an
interrupted run keeps its cursor and resumes without skipping failed pages.
"""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

try:
    from .catalog_format import SCHEMA_VERSION, pack_games, unpack_games, write_catalog
except ImportError:
    from catalog_format import SCHEMA_VERSION, pack_games, unpack_games, write_catalog

ROOT = Path(__file__).resolve().parents[1]
SEARCH_URL = "https://store.steampowered.com/search/results/"
API_URL = "https://api.steampowered.com/IStoreService/GetAppList/v1/"
API_DOC = "https://partner.steamgames.com/doc/webapi/IStoreService"
SEARCH_SOURCE = "steam-public-search"
API_SOURCE = "steam-web-api"
VOID_TAGS = {"img", "br", "hr", "input", "meta", "link", "source", "wbr", "area", "base", "embed", "param", "track", "col"}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def positive_id(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        return None
    number = int(value)
    return number if 0 < number <= 2147483647 else None


def nonnegative_int(value, default=0):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def clean_text(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def normalize_game(value):
    if not isinstance(value, dict) or value.get("type", "game") != "game":
        return None
    appid, name = positive_id(value.get("appid")), clean_text(value.get("name"))
    if not appid or not name:
        return None
    result = {"appid": appid, "name": name, "aliases": [], "type": "game"}
    name_en = clean_text(value.get("nameEn"))
    if name_en:
        result["nameEn"] = name_en
    for alias in value.get("aliases", []) if isinstance(value.get("aliases"), list) else []:
        text = clean_text(alias)
        if text and text not in result["aliases"]:
            result["aliases"].append(text)
    return result


def merge_games(previous, incoming, curated):
    """Retain unknown old rows and local Chinese names/aliases during API refresh."""
    merged = {}
    for group, is_curated in [(previous, False), (incoming, False), (curated, True)]:
        for raw in group:
            game = normalize_game(raw)
            if not game:
                continue
            appid = game["appid"]
            old = merged.get(appid)
            if old:
                aliases = list(dict.fromkeys(old["aliases"] + game["aliases"]))
                # An unlocalized API name must not erase a known Chinese name.
                if not is_curated and re.search(r"[\u3400-\u9fff]", old["name"]) and not re.search(r"[\u3400-\u9fff]", game["name"]):
                    game.setdefault("nameEn", game["name"])
                    game["name"] = old["name"]
                if not game.get("nameEn") and old.get("nameEn"):
                    game["nameEn"] = old["nameEn"]
                game["aliases"] = aliases
            merged[appid] = game
    return list(merged.values())


class SearchParser(HTMLParser):
    """Accept only single-AppID Steam app anchors, never package/bundle rows."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.games = []
        self.current = None
        self.title_depth = 0
        self.title = []
        self.row_count = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.current, self.title_depth, self.title = None, 0, []
            if "search_result_row" in attrs.get("class", "").split():
                self.row_count += 1
            appid = positive_id(attrs.get("data-ds-appid"))
            href = urlparse(urljoin("https://store.steampowered.com/", attrs.get("href", "")))
            match = re.fullmatch(r"/app/([0-9]+)(?:/.*)?", href.path)
            if appid and href.scheme == "https" and href.hostname == "store.steampowered.com" and match and int(match[1]) == appid and not attrs.get("data-ds-bundleid"):
                self.current = appid
        elif self.current:
            if self.title_depth and tag not in VOID_TAGS:
                self.title_depth += 1
            elif tag == "span" and "title" in attrs.get("class", "").split():
                self.title_depth = 1

    def handle_endtag(self, tag):
        if tag == "a":
            name = clean_text("".join(self.title))
            if self.current and name:
                self.games.append({"appid": self.current, "name": name, "aliases": [], "type": "game"})
            self.current, self.title_depth, self.title = None, 0, []
        elif self.title_depth and tag not in VOID_TAGS:
            self.title_depth -= 1

    def handle_data(self, data):
        if self.current and self.title_depth:
            self.title.append(data)


def parse_search_page(payload):
    if not isinstance(payload, dict) or payload.get("success") not in (1, True):
        raise ValueError("invalid Steam search response")
    total = nonnegative_int(payload.get("total_count"), -1)
    html = payload.get("results_html")
    if total < 0 or not isinstance(html, str):
        raise ValueError("missing search total or HTML")
    parser = SearchParser()
    parser.feed(html)
    parser.close()
    return parser.games, total, parser.row_count


class CatalogClient:
    def get_json(self, url, params):
        # Do not log request URLs: API requests include the key in memory only.
        request = Request(url + "?" + urlencode(params), headers={
            "User-Agent": "Mozilla/5.0 (compatible; GamePriceRadar/1.0)",
            "Accept": "application/json", "Referer": "https://store.steampowered.com/search/",
        })
        with urlopen(request, timeout=30) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("Steam directory response is too large")
            return json.loads(raw.decode("utf-8"))


def request_page(client, url, params, *, attempts=3, delay=0.7, sleep=time.sleep):
    for attempt in range(attempts):
        try:
            return client.get_json(url, params)
        except Exception:
            if attempt + 1 == attempts:
                # Never propagate urllib's URL-bearing exception text.
                raise RuntimeError("Steam directory request failed") from None
            if delay:
                sleep(delay * (attempt + 1))


def _result(previous, curated, incoming, now, source, state, complete, total, successes, failed):
    games = pack_games(merge_games(previous.get("games", []), incoming, curated))
    if not successes:
        if previous.get("games"):
            result = copy.deepcopy(previous)
            result.update(schemaVersion=SCHEMA_VERSION, games=games, gameCount=len(games), status="stale", lastAttemptAt=now,
                          note="本轮目录同步失败，保留上次目录、覆盖状态与同步游标。")
            return result
        return {"schemaVersion": SCHEMA_VERSION, "generatedAt": now, "lastAttemptAt": now, "source": source,
                "sourceUrl": API_DOC if source == API_SOURCE else SEARCH_URL, "status": "error", "complete": False,
                "scope": "仅本地已配置游戏，在线目录尚未取得", "note": "目录接口暂不可用，未生成或虚构完整目录。",
                "games": games, "gameCount": len(games), "syncState": state}
    public = source == SEARCH_SOURCE
    scope = "中国大陆 Steam 公开搜索可见的游戏本体，不含受限条目、DLC、软件或捆绑包" if public else "Steam 商店 API 返回的游戏本体，不含 DLC、软件、视频或硬件"
    note = ("已完整遍历本次公开搜索结果，可按名称或 AppID 搜索全库目录。" if complete else "公开目录正在分批补全；已保存游标，下轮继续，当前并非完整游戏库。") if public else ("官方商店目录同步完成，后续按修改时间增量同步；名称可能未汉化。" if complete else "官方目录正在分页同步；已有目录保留，当前遍历尚未完成。")
    if failed:
        note += "本轮有页面读取失败，停在最后成功页之后，未跳过失败页。"
    result = {"schemaVersion": SCHEMA_VERSION, "generatedAt": now, "source": source, "sourceUrl": SEARCH_URL if public else API_DOC,
              "status": "ok" if complete and not failed else "partial", "complete": bool(complete and not failed),
              "scope": scope, "note": note, "games": games, "gameCount": len(games), "syncState": state}
    if total is not None:
        result["totalCount"] = total
    return result


def sync_search(previous, curated, client, *, pages=20, page_size=100, delay=0.7, now=None, sleep=time.sleep):
    now = now or utc_now()
    old_state = previous.get("syncState", {}) if previous.get("source") == SEARCH_SOURCE else {}
    start = nonnegative_int(old_state.get("nextStart"))
    contiguous = start == 0 or old_state.get("contiguousFromStart") is True
    state = {"mode": "search", "nextStart": start, "contiguousFromStart": contiguous,
             "firstSyncedAt": old_state.get("firstSyncedAt", now),
             "cycleStartedAt": now if start == 0 else old_state.get("cycleStartedAt", now)}
    if old_state.get("lastCompletedAt"):
        state["lastCompletedAt"] = old_state["lastCompletedAt"]
    incoming, total, successes, failed, complete = [], None, 0, False, False
    page_signatures = set()
    count = max(1, min(int(page_size), 100))
    for page in range(max(1, int(pages))):
        try:
            payload = request_page(client, SEARCH_URL, {"query": "", "start": start, "count": count,
                "category1": 998, "sort_by": "_ASC", "infinite": 1, "l": "schinese", "cc": "cn",
                "ignore_preferences": 1}, delay=delay, sleep=sleep)
            games, total, rows = parse_search_page(payload)
            if start < total and not rows and not games:
                raise ValueError("empty page before reported end")
            signature = tuple(game["appid"] for game in games)
            if signature and signature in page_signatures:
                raise ValueError("Steam repeated an earlier result page; cursor did not advance")
            if signature:
                page_signatures.add(signature)
        except Exception:
            failed = True
            break
        incoming.extend(games)
        successes += 1
        start += count
        state["nextStart"] = start
        if start >= total:
            complete = contiguous
            state.update(nextStart=0, contiguousFromStart=True, lastCompletedAt=now)
            break
        if delay and page + 1 < pages:
            sleep(delay)
    return _result(previous, curated, incoming, now, SEARCH_SOURCE, state, complete, total, successes, failed)


def sync_api(previous, curated, client, key, *, pages=20, page_size=10000, delay=0.7, now=None, sleep=time.sleep):
    now = now or utc_now()
    old_state = previous.get("syncState", {}) if previous.get("source") == API_SOURCE else {}
    baseline = old_state.get("apiBaselineComplete") is True
    continuing = bool(nonnegative_int(old_state.get("apiLastAppId")))
    since = nonnegative_int(old_state.get("cycleModifiedSince")) if continuing else nonnegative_int(old_state.get("lastModifiedSince")) if baseline else 0
    cursor = nonnegative_int(old_state.get("apiLastAppId")) if continuing else 0
    cycle_started = old_state.get("cycleStartedAt", now) if continuing else now
    state = {"mode": "api", "nextStart": 0, "apiLastAppId": cursor, "cycleModifiedSince": since,
             "apiBaselineComplete": baseline, "firstSyncedAt": old_state.get("firstSyncedAt", now),
             "cycleStartedAt": cycle_started}
    if old_state.get("lastModifiedSince") is not None:
        state["lastModifiedSince"] = old_state["lastModifiedSince"]
    incoming, successes, failed, complete = [], 0, False, False
    count = max(1, min(int(page_size), 50000))
    for page in range(max(1, int(pages))):
        params = {"include_games": True, "include_dlc": False, "include_software": False,
                  "include_videos": False, "include_hardware": False, "last_appid": cursor, "max_results": count}
        if since:
            params["if_modified_since"] = since
        try:
            payload = request_page(client, API_URL, {"key": key, "input_json": json.dumps(params, separators=(",", ":"))}, delay=delay, sleep=sleep)
            response = payload.get("response") if isinstance(payload, dict) else None
            if not isinstance(response, dict) or not isinstance(response.get("apps"), list):
                raise ValueError("invalid official app list")
            apps = response["apps"]
            ids = [positive_id(app.get("appid")) for app in apps if isinstance(app, dict)]
            ids = [appid for appid in ids if appid is not None]
            if any(appid <= cursor for appid in ids) or ids != sorted(set(ids)):
                raise ValueError("official app cursor did not advance")
            more = response.get("have_more_results", len(apps) >= count)
            next_cursor = positive_id(response.get("last_appid")) or (ids[-1] if ids else cursor)
            if more and (not ids or next_cursor <= cursor):
                raise ValueError("official app list has no continuation")
            if ids and next_cursor < ids[-1]:
                raise ValueError("invalid official continuation")
        except Exception:
            failed = True
            break
        incoming.extend({**app, "nameEn": app.get("name")} for app in apps if isinstance(app, dict))
        successes += 1
        cursor = next_cursor
        state["apiLastAppId"] = cursor
        if not more:
            complete = True
            watermark = max(0, int(datetime.fromisoformat(cycle_started.replace("Z", "+00:00")).timestamp()) - 1)
            state.update(apiBaselineComplete=True, apiLastAppId=0, cycleModifiedSince=0,
                         lastModifiedSince=watermark, lastCompletedAt=now)
            break
        if delay and page + 1 < pages:
            sleep(delay)
    return _result(previous, curated, incoming, now, API_SOURCE, state, complete, None, successes, failed)


def synchronize(previous=None, curated=None, *, client=None, key=None, pages=20, page_size=100, api_page_size=10000, delay=0.7, now=None, sleep=time.sleep):
    previous, curated, client = previous or {}, curated or [], client or CatalogClient()
    if isinstance(previous, dict):
        # Accept both schema v1 object rows and schema v2 compact rows.
        previous = {**previous, "games": unpack_games(previous)}
    if key:
        return sync_api(previous, curated, client, key, pages=pages, page_size=api_page_size, delay=delay, now=now, sleep=sleep)
    return sync_search(previous, curated, client, pages=pages, page_size=page_size, delay=delay, now=now, sleep=sleep)


def read_json(path, fallback):
    try:
        with Path(path).open(encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/data/catalog-index.json")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data/catalog.json")
    parser.add_argument("--pages", type=int, default=2500, help="maximum public search pages per run")
    parser.add_argument("--page-size", type=int, default=100, help="public search page size, maximum 100")
    parser.add_argument("--api-page-size", type=int, default=10000, help="official API page size, maximum 50000")
    parser.add_argument("--delay", type=float, default=0.7)
    args = parser.parse_args(argv)
    if args.pages < 1 or args.page_size < 1 or args.api_page_size < 1 or args.delay < 0:
        parser.error("pages and page sizes must be positive; delay must not be negative")
    catalog = read_json(args.catalog, {})
    curated = catalog.get("games", []) if isinstance(catalog, dict) else []
    previous = read_json(args.output, {})
    if not isinstance(previous, dict):
        previous = {}
    result = synchronize(previous, curated, key=os.environ.get("STEAM_WEB_API_KEY", "").strip(), pages=args.pages,
                         page_size=args.page_size, api_page_size=args.api_page_size, delay=args.delay)
    write_catalog(args.output, result)
    # Only counts and controlled labels are printed; keys and exception URLs are not.
    print(f"Steam directory: {result['gameCount']} games; status={result['status']}; complete={str(result['complete']).lower()}")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
