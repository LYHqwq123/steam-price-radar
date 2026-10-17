"""Read-only public price adapters. No credentials, scraping bypass, or dependencies.

Prices from third party store endpoints are observations, not checkout guarantees.
Keep uncertain inventory and historical transaction references out of eligible minima.
"""
from __future__ import annotations

import html
import json
import math
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone


SOURCES = {"steam": "Steam 国区", "sonkwo": "杉果", "steampy": "SteamPY", "steamcici": "SteamCici"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def money(value, *, cents=False):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value) / (100 if cents else 1)
    except (ValueError, TypeError):
        return None
    return round(number, 2) if math.isfinite(number) and number >= 0 else None


def integer(value):
    try:
        return int(value) if not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def normalized_name(value):
    value = html.unescape(str(value or "")).casefold().replace("™", "").replace("®", "")
    return "".join(char for char in value if char.isalnum())


def quote(source, url, *, price=None, original=None, status="unknown", stock="unknown", eligible=False, note=""):
    price, original = money(price), money(original)
    return {
        "source": source, "kind": "direct" if source == "steam" else "cdk",
        "price": price, "originalPrice": original, "currency": "CNY",
        "discount": round((1 - price / original) * 100) if original and price is not None and price <= original else None,
        "url": url, "checkedAt": now_iso(), "status": status,
        "stock": stock, "eligible": eligible, "note": note,
    }


class JsonClient:
    def __init__(self, timeout=18, attempts=2):
        self.timeout, self.attempts = timeout, attempts

    def get(self, url, data=None, headers=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
        request_headers = {
            "User-Agent": "SteamPriceRadar/1.0 (public price comparison; read-only)",
            "Accept": "application/json", "Content-Type": "application/json",
        }
        request_headers.update(headers or {})
        req = urllib.request.Request(url, data=body, headers=request_headers)
        for attempt in range(self.attempts):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    # Decode bytes explicitly; no terminal/Windows locale dependence.
                    return json.loads(response.read().decode("utf-8"))
            except Exception:
                if attempt + 1 == self.attempts:
                    raise
                time.sleep(0.6 * (attempt + 1))


def standard_package(data, game):
    """Match the base game's complete name, never the cheapest DLC/edition."""
    names = {normalized_name(n) for n in [data.get("name"), game.get("name"), game.get("nameEn"), *game.get("aliases", [])] if n}
    candidates = []
    for group in data.get("package_groups", []):
        for sub in group.get("subs", []):
            # Remove only a terminal currency/price segment. Splitting at the
            # first dash would turn "Game - Deluxe Edition" into "Game".
            option = re.sub(r"<[^>]*>", "", html.unescape(sub.get("option_text", "")))
            price_segment = r"(?:[¥￥$€£]\s*|(?:CNY|RMB|USD|EUR|GBP)\s*)[\d,.]+"
            title = re.sub(r"\s+-\s+" + price_segment + r"(?:\s+" + price_segment + r")*\s*$", "", option, flags=re.I)
            title = re.sub(r"^(?:购买|购买\s+|Buy\s+)", "", title, flags=re.I).strip()
            title = re.sub(r"\s*[-:]?\s*(?:Standard Edition|标准版)\s*$", "", title, flags=re.I).strip()
            if normalized_name(title) in names and integer(sub.get("packageid")):
                candidates.append(sub)
    ids = {integer(sub["packageid"]) for sub in candidates}
    if len(ids) == 1:
        return candidates[0]
    # Steam can expose two identically named base-game packages. Its own
    # overview identifies the default purchasable package when prices differ.
    expected = integer(data.get("price_overview", {}).get("final"))
    if expected is not None and candidates and all(integer(c.get("price_in_cents_with_discount")) is not None for c in candidates):
        matching = [c for c in candidates if integer(c.get("price_in_cents_with_discount")) == expected]
        if len(matching) == 1:
            return matching[0]
    return None


def fetch_steam(client, game):
    appid = int(game["appid"])
    url = f"https://store.steampowered.com/app/{appid}/?cc=cn&l=schinese"
    payload = client.get(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=cn&l=schinese")
    result = payload.get(str(appid), {})
    if not result.get("success"):
        return quote("steam", url, status="unavailable", note="Steam 国区未返回该游戏；可能未上架或暂不可用。"), None, {}
    data = result.get("data", {})
    if data.get("type") != "game":
        return quote("steam", url, status="unavailable", note="不是游戏本体，已排除。"), None, data
    overview = data.get("price_overview", {})
    sub = standard_package(data, game)
    subid = integer(sub["packageid"]) if sub else None
    if data.get("is_free"):
        return quote("steam", url, price=0, original=0, status="ok", stock="in_stock", eligible=True, note="Steam 国区免费游戏。"), subid, data
    if overview.get("currency") != "CNY":
        return quote("steam", url, status="unavailable", note="未取得人民币国区价格；未使用其他地区报价。"), subid, data
    price = money(overview.get("final"), cents=True)
    original = money(overview.get("initial"), cents=True)
    if sub is None or price is None:
        return quote("steam", url, status="unknown", note="未能唯一确认标准版购买包；为避免串版本暂不展示价格。"), None, data
    package_price = money(sub.get("price_in_cents_with_discount"), cents=True)
    if package_price is None:
        return quote("steam", url, status="unknown", note="标准版购买包暂无人民币报价。"), subid, data
    if price != package_price:
        original = None  # App overview can describe another edition; never mix its list price.
    return quote("steam", url, price=package_price, original=original, status="ok", stock="in_stock", eligible=True,
                 note="Steam 国区标准版当前直售价；不含钱包折扣或代购。"), subid, data


def suspicious_price(price, reference):
    if price is None or price <= 0:
        return False
    return price < 1 or (reference is not None and reference >= 20 and price < reference * 0.03)


def steampy_product_url(product_id):
    # Verified against app.eb7bcbe6.js: root route children path=cdkDetail;
    # detail chunk-f87f50c4.b44a9bd8.js reads query.name and query.gameId.
    product_id = str(product_id or "")
    if not product_id.isascii() or not product_id.isdigit() or int(product_id) <= 0:
        return "https://steampy.com/"
    return "https://steampy.com/cdkDetail?" + urllib.parse.urlencode({"name": "cn", "gameId": product_id})


def fetch_heybox(client, game):
    """Fallback CN Steam price observation, never present it as a direct check."""
    appid = int(game["appid"])
    query = urllib.parse.urlencode({"lang": "zh-cn", "os_type": "web", "offset": 0, "limit": 100, "steam_appid": appid})
    payload = client.get("https://api.xiaoheihe.cn/game/get_game_global_prices/?" + query,
                         headers={"Referer": f"https://www.xiaoheihe.cn/app/topic/game/pc/{appid}"})
    if payload.get("status") != "ok":
        raise ValueError("Heybox price endpoint did not return success")
    rows = payload.get("result", {}).get("prices", [])
    china = next((row for row in rows if str(row.get("cc", "")).casefold() == "cn"), None)
    if not china or money(china.get("current")) is None:
        raise ValueError("Heybox price endpoint did not return a CN quote")
    value = quote("steam", f"https://store.steampowered.com/app/{appid}/?cc=cn&l=schinese",
                  price=china["current"], original=china.get("initial"), status="unknown",
                  note="Steam 接口暂不可用；小黑盒提供的 Steam 国区参考价，尚未向商店核对。")
    value["dataProvider"] = "heybox"
    return value


def steampy_reference_problem(result, appid, subid):
    """Explain why a public plugin reference must not be shown as a CDK quote.

    The anonymous endpoint does not expose a live order book. In particular,
    repeated 0.1/1 values occur for unrelated full-price games. Treat a tiny
    unverified reference as unconfirmed, rather than declaring every deep
    discount fake based on its percentage of Steam's list price.
    """
    for field, expected in (("appId", appid), ("appid", appid), ("subId", subid), ("subid", subid)):
        if result.get(field) is not None and steampy_id(result[field]) != expected:
            return "version_mismatch", "接口回显的 AppID 或 SubID 与所选版本不一致，已排除。"
    for field in ("currency", "priceCurrency", "keyCurrency"):
        if result.get(field) not in (None, "") and str(result[field]).upper() not in ("CNY", "RMB", "CN¥", "¥", "￥"):
            return "currency_mismatch", "接口报价不是人民币，未混用外币金额。"
    for field in ("cc", "country", "countryCode", "region", "area"):
        if result.get(field) not in (None, "") and str(result[field]).casefold() not in (
                "cn", "china", "中国", "国区", "中国大陆", "mainland china", "zh-cn"):
            return "region_mismatch", "接口回显的激活地区不是中国大陆，已排除异区报价。"
    for field in ("tradeType", "purchaseType", "deliveryType"):
        value = str(result.get(field) or "").casefold()
        if any(term in value for term in ("gift", "wallet", "balance", "代购", "礼物", "余额")):
            return "delivery_mismatch", "该商品属于礼物、余额购或代购，未纳入 CDK 价格。"
    for field in ("stock", "inventory", "stockNum"):
        if result.get(field) is not None:
            inventory = integer(result[field])
            if inventory is None or inventory <= 0:
                return "no_confirmed_stock", "公开接口未返回有效可售库存，已排除该报价。"
    price = money(result.get("keyPrice"))
    if price is None or price <= 0:
        return "no_valid_cdk_price", "未返回有效 CDKey 报价；未使用余额购或代购价。"
    if price <= 1:
        return "unverified_tiny_reference", "未核实的微额 CDKey 参考价（不超过 1 元），可能是占位或无效挂单；已隐藏。"
    return None


def steampy_id(value):
    value = str(value or "")
    return int(value) if value.isascii() and value.isdigit() and int(value) > 0 else None


def fetch_steampy(client, game, subid, original, *, edition_name=None):
    """Read the public CN CDKey reference for an exact Steam package.

    Website source: app.eb7bcbe6.js maps listKeyApi to /steamKeySale/listSale;
    chunk-f87f50c4.b44a9bd8.js sorts by keyPrice ascending. That actual order
    book and /steamGame/getOne both return code 401 anonymously (2026-10-17).
    No access-token or captcha bypass is attempted here. Plugin keyPrice must
    stay a reference even when it looks plausible, never an executable offer.
    """
    url = "https://steampy.com/"
    appid, subid = steampy_id(game.get("appid")), steampy_id(subid)
    edition_name = edition_name or game.get("edition") or "所选版本"
    def observed(*, price=None, status="unknown", reason_code, reason, endpoint=None):
        value = quote("steampy", url, price=price, original=original, status=status, note=reason)
        value.update({"reasonCode": reason_code, "reason": reason,
                      "sourceUrl": endpoint or url, "lastCheckedAt": value["checkedAt"],
                      "priceKind": "reference", "editionSubId": subid,
                      "matchMethod": "requested_subid", "editionName": edition_name})
        return value
    if not appid or appid <= 0 or not subid or subid <= 0:
        return observed(reason_code="missing_package", reason="所选版本的 Steam AppID / SubID 未确认，未请求可能错版的报价。")
    query = urllib.parse.urlencode({"subId": subid, "appId": appid, "type": "subid"})
    endpoint = "https://steampy.com/xboot/common/plugIn/getGame?" + query
    data = client.get(endpoint)
    if integer(data.get("code")) == 203:
        return observed(status="unavailable", reason_code="not_in_catalog",
                        reason="SteamPY 尚未收录这个精确购买包，未改用其他版本的报价。", endpoint=endpoint)
    if integer(data.get("code")) in (401, 403):
        return observed(status="unavailable", reason_code="reference_requires_login",
                        reason="SteamPY 公开参考接口当前也要求登录，未返回匿名报价。", endpoint=endpoint)
    if not data.get("success") or not isinstance(data.get("result"), dict):
        raise ValueError("SteamPY public quote endpoint did not return success")
    result = data["result"]
    url = steampy_product_url(result.get("id"))
    problem = steampy_reference_problem(result, appid, subid)
    if problem:
        reason_code, reason = problem
        return observed(status="suspect" if reason_code == "unverified_tiny_reference" else "unavailable",
                        reason_code=reason_code, reason=reason, endpoint=endpoint)
    if url == "https://steampy.com/":
        return observed(status="unavailable", reason_code="missing_product_id",
                        reason="接口未返回有效商品 ID，无法核对对应商品，已隐藏报价。", endpoint=endpoint)
    return observed(price=result["keyPrice"], reason_code="orderbook_requires_login",
                    reason="按所选 Steam SubID 查询的公开 CDKey 参考价；实际在售挂单接口需要登录，无法确认实时最低价、库存和国区激活条件。",
                    endpoint=endpoint)


def match_cici_row(rows, appid, subid):
    if not subid:
        return None
    matches = [row for row in rows if integer(row.get("parentId")) == int(appid)
               and integer(row.get("gameId")) == int(subid)
               and str(row.get("isDeluxe", "0")) == "0"
               and str(row.get("bundle", "0")) == "0"
               and str(row.get("dlcType", "0")) in ("0", "None", "")]
    return matches[0] if len(matches) == 1 else None


def fetch_cici(client, game, subid, original):
    url = "https://steamcici.com/"
    if not subid:
        return quote("steamcici", url, status="unknown", note="标准版 Steam SubID 未确认，未采用同名商品报价。")
    row = None
    # Search results are fuzzy: identify each result by both AppID and SubID.
    for search in dict.fromkeys([game["nameEn"], game["name"]]):
        query = urllib.parse.urlencode({"pageNum": 1, "pageSize": 50, "name": search})
        data = client.get("https://steamcici.com/prod-api/user/system/shopGame/list?" + query)
        if integer(data.get("code")) != 200:
            raise ValueError("SteamCici public list endpoint did not return success")
        row = match_cici_row(data.get("rows", []), game["appid"], subid)
        if row:
            break
    if not row:
        return quote("steamcici", url, status="unavailable", note="搜索结果中没有同时匹配 AppID 和标准版 SubID 的商品。")
    price = money(row.get("lastLowSellPrice"))
    if price is None or price <= 0:
        return quote("steamcici", url, status="unavailable", note="标准版已匹配，但平台未返回有效低价参考。")
    status = "suspect" if suspicious_price(price, original) else "unknown"
    return quote("steamcici", url, price=price, original=original, status=status,
                 note=("平台低价参考 lastLowSellPrice；未确认当前库存、可成交价格与国区激活限制。"
                       " 该站使用登录后商品弹窗，链接打开平台首页。"
                       + (" 报价异常，不参与最低参考价。" if status == "suspect" else "")))


def sonkwo_host(game):
    mapping = game.get("sonkwo", {})
    return "https://api.sonkwo.hk" if mapping.get("area") == "abroad" else "https://api.sonkwo.cn"


def fetch_sonkwo_price_batches(client, games):
    result = {}
    for host in ("https://api.sonkwo.cn", "https://api.sonkwo.hk"):
        skus = sorted({int(g["sonkwo"]["sku"]) for g in games if g.get("sonkwo", {}).get("verified") and sonkwo_host(g) == host})
        if not skus:
            continue
        try:
            payload = client.get(host + "/sku/" + ",".join(map(str, skus)) + "/price")
            if not payload.get("success"):
                raise ValueError("Sonkwo price endpoint did not return success")
            for row in payload.get("data", []):
                result[(host, integer(row.get("id")))] = row
        except Exception as exc:
            for sku in skus:
                result[(host, sku)] = exc
    return result


def fetch_sonkwo(client, game, batches):
    mapping = game.get("sonkwo", {})
    url = f"https://www.sonkwo.hk/sku/{mapping['sku']}" if mapping.get("sku") and mapping.get("area") == "abroad" else "https://www.sonkwo.cn/"
    if not mapping.get("verified") or mapping.get("platform") != "steam" or mapping.get("edition") != "standard":
        return quote("sonkwo", url, status="unavailable", note="尚无已核验的国区 Steam 标准版 SKU 映射；不采用模糊搜索最低价。")
    host, sku = sonkwo_host(game), int(mapping["sku"])
    # The search endpoint is used to revalidate the exact known SKU, not discover
    # the cheapest similarly named edition (e.g. Gold Edition).
    payload = client.get("https://api.sonkwo.cn/product/sku/page", {
        "regions": ["cn"], "searchWord": mapping.get("search", game["nameEn"]), "page": 1, "per": 50,
    })
    if not payload.get("success"):
        raise ValueError("Sonkwo product list endpoint did not return success")
    row = next((r for r in payload.get("data", {}).get("list", []) if integer(r.get("id")) == sku), None)
    if not row:
        return quote("sonkwo", url, status="unavailable", note="已核验 SKU 未出现在当前国区列表，暂不展示旧商品价格。")
    expected_names = {normalized_name(mapping.get("name", game["nameEn"]))}
    actual_names = {normalized_name(name) for name in row.get("skuNames", {}).values() if name}
    if not expected_names.intersection(actual_names) or integer(row.get("kind")) != 0 or integer(row.get("keyType")) != 0:
        return quote("sonkwo", url, status="unavailable", note="SKU 的版本或激活平台发生变化，已排除该商品。")
    batch = batches.get((host, sku))
    if isinstance(batch, Exception):
        raise batch
    if not batch:
        return quote("sonkwo", url, status="unavailable", note="对应区域价格接口未返回该 SKU；未借用其他区域价格。")
    price, original = money(batch.get("salePrice")), money(row.get("listPrice"))
    if price is None or price <= 0:
        return quote("sonkwo", url, status="unavailable", note="平台未返回有效现金价格。")
    info = client.get(host + f"/sku/{sku}/baseInfo")
    details = next((r for r in info.get("data", []) if integer(r.get("id")) == sku), {}) if info.get("success") else {}
    inventory = integer(details.get("inventorySellableCount"))
    # Do not use /virtuality/.../inventory here: that endpoint is for another
    # product flow and its zero value does not establish game-key availability.
    stock = "unknown" if inventory is None else "in_stock" if inventory > 0 else "out_of_stock"
    eligible = stock == "in_stock"
    return quote("sonkwo", url, price=price, original=original,
                 status="ok" if eligible else "unavailable" if stock == "out_of_stock" else "unknown",
                 stock=stock, eligible=eligible,
                 note="已核验国区 Steam 标准版 SKU；不含领券、会员或新客优惠。" +
                 (" 接口未确认可售库存，价格仅供参考。" if stock == "unknown" else " 当前可售库存为零。" if stock == "out_of_stock" else ""))
