"""Steam CN purchase packages, public bundles, and separately priced DLC.

Package IDs are identities: a cheap soundtrack, commercial license, or deluxe
upgrade must never replace the base game's standard package. Store metadata is
cached for 24 hours; displayed prices come from this collection run.
"""
from __future__ import annotations

import copy
import html
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

try:
    from .providers import integer, money, now_iso, quote, standard_package
except ImportError:
    from providers import integer, money, now_iso, quote, standard_package


CACHE_HOURS = 24
DLC_METADATA_BATCH_LIMIT = 30
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
_PRICE = r"(?:[¥￥$€£]\s*|(?:CNY|RMB|USD|EUR|GBP)\s*)[\d,.]+"


def store_url(kind, value):
    return f"https://store.steampowered.com/{kind}/{int(value)}/?cc=cn&l=schinese"


def option_name(value):
    text = re.sub(r"<[^>]*>", "", html.unescape(str(value or ""))).strip()
    text = re.sub(r"\s+-\s+" + _PRICE + r"(?:\s+" + _PRICE + r")*\s*$", "", text, flags=re.I)
    return re.sub(r"^(?:购买\s*|Buy\s+)", "", text, flags=re.I).strip()


def chinese_label(value, *, standard=False):
    """Translate only established edition labels; preserve the product title."""
    text = str(value or "").strip()
    for english, chinese in [
        ("Digital Deluxe Edition", "数字豪华版"), ("Deluxe Edition", "豪华版"),
        ("Standard Edition", "标准版"), ("Gold Edition", "黄金版"),
        ("Ultimate Edition", "终极版"), ("Complete Edition", "完整版"),
        ("Game of the Year Edition", "年度版"), ("Commercial License", "网吧商用许可"),
        ("4-Pack", "四人包"),
    ]:
        text = re.sub(r"\b" + re.escape(english) + r"\b", chinese, text, flags=re.I)
    if standard and "标准版" not in text:
        text += " · 标准版"
    return text


def cached(item, *, refresh=False):
    if refresh or not isinstance(item, dict) or not item.get("metadataCheckedAt"):
        return False
    try:
        stamp = datetime.fromisoformat(item["metadataCheckedAt"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        return 0 <= age < CACHE_HOURS * 3600
    except (ValueError, TypeError, OverflowError):
        return False


def stale_quotes(values, reason):
    result = copy.deepcopy(values or [])
    for value in result:
        value.update(status="stale", stock="unknown", eligible=False,
                     note="本轮内容报价采集失败，保留原报价与采集时间。" + reason)
    return result


def _unknown(url, note):
    return quote("steam", url, status="unavailable", note=note)


def _priced(url, overview, *, is_free=False, note="Steam 国区当前直售价。"):
    if is_free and not overview:
        return quote("steam", url, price=0, original=0, status="ok", stock="in_stock", eligible=True,
                     note="Steam 国区免费内容；仍需满足本体等使用条件。")
    if not isinstance(overview, dict) or overview.get("currency") != "CNY":
        return _unknown(url, "未取得该内容的人民币国区价格；未使用其他地区报价。")
    price, original = money(overview.get("final"), cents=True), money(overview.get("initial"), cents=True)
    if price is None:
        return _unknown(url, "该内容暂无可用的 Steam 国区价格。")
    return quote("steam", url, price=price, original=original, status="ok", stock="in_stock", eligible=True, note=note)


class BundleParser(HTMLParser):
    """Read only actual purchase wrappers, never recommendations or cart sums."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.currency = None
        self.bundles = []
        self.current = None
        self.stack = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("itemprop") == "priceCurrency":
            self.currency = attrs.get("content")
        if self.current is None:
            bundleid = integer(attrs.get("data-ds-bundleid"))
            if tag != "div" or not bundleid or "game_area_purchase_game_wrapper" not in attrs.get("class", "").split():
                return
            try:
                data = json.loads(attrs.get("data-ds-bundle-data", "{}"))
            except (ValueError, TypeError):
                data = {}
            included = sorted({appid for item in data.get("m_rgItems", []) for raw in item.get("m_rgIncludedAppIDs", [])
                               if (appid := integer(raw)) and appid > 0})
            self.current = {"bundleid": bundleid, "name": "", "priceText": "", "originalText": "",
                            "includedAppids": included, "mustPurchaseTogether": bool(data.get("m_bMustPurchaseAsSet"))}
            self.stack = ["wrapper"]
            return
        classes = attrs.get("class", "").split()
        role = "title" if tag == "h2" else "price" if "discount_final_price" in classes else "original" if "discount_original_price" in classes else ""
        if "data-price-final" in attrs:
            self.current["finalCents"] = integer(attrs["data-price-final"])
        if tag not in _VOID:
            self.stack.append(role)

    def handle_endtag(self, tag):
        if self.current is None or tag in _VOID:
            return
        if self.stack:
            self.stack.pop()
        if not self.stack:
            self.current["name"] = option_name(self.current["name"])
            self.bundles.append(self.current)
            self.current = None

    def handle_data(self, value):
        if self.current is None:
            return
        for role, field in [("title", "name"), ("price", "priceText"), ("original", "originalText")]:
            if role in self.stack:
                self.current[field] += value


def parse_bundles(markup, appid):
    parser = BundleParser()
    parser.feed(markup)
    if parser.currency != "CNY":
        raise ValueError("storefront did not explicitly identify CNY currency")
    result = []
    for row in parser.bundles:
        # An unrelated cross-promotion can also have a purchase wrapper.
        if int(appid) not in row["includedAppids"] or not row["name"]:
            continue
        url = store_url("bundle", row["bundleid"])
        price = money(row.get("finalCents"), cents=True)
        original = re.fullmatch(r"\s*[¥￥]\s*([\d,.]+)\s*", row["originalText"])
        original = money(original.group(1).replace(",", "")) if original else None
        note = "Steam 国区捆绑包整体参考价；不是单独豪华版或升级 DLC。"
        if not row["mustPurchaseTogether"]:
            note += " 已拥有部分内容时，登录后的补齐价格可能不同。"
        result.append({"id": f"bundle-{row['bundleid']}", "kind": "bundle", "appid": int(appid),
                       "bundleid": row["bundleid"], "name": row["name"], "nameZh": chinese_label(row["name"]),
                       "isStandard": False, "includedAppids": row["includedAppids"], "steamUrl": url, "note": note,
                       "quotes": [quote("steam", url, price=price, original=original, status="ok" if price is not None else "unavailable",
                                        stock="in_stock" if price is not None else "unknown", eligible=price is not None, note=note)]})
    return result


def fetch_storefront(client, appid):
    url = store_url("app", appid)
    if hasattr(client, "get_text"):
        return client.get_text(url)
    request = urllib.request.Request(url, headers={"User-Agent": "SteamPriceRadar/1.0 (public price comparison; read-only)",
                                                  "Accept": "text/html"})
    with urllib.request.urlopen(request, timeout=getattr(client, "timeout", 18)) as response:
        return response.read(4_000_000).decode("utf-8")


def _package(client, game, sub, old, *, standard, refresh):
    appid, subid = int(game["appid"]), int(sub["packageid"])
    url = store_url("sub", subid)
    row = {"id": f"sub-{subid}", "kind": "package", "appid": appid, "subid": subid,
           "name": option_name(sub.get("option_text")) or f"Steam 购买包 {subid}",
           "isStandard": standard, "steamUrl": url}
    metadata_ok = True
    if cached(old, refresh=refresh):
        for key in ("includedAppids", "metadataCheckedAt"):
            if key in old:
                row[key] = copy.deepcopy(old[key])
    else:
        try:
            payload = client.get(f"https://store.steampowered.com/api/packagedetails?packageids={subid}&cc=cn&l=schinese")
            record = payload.get(str(subid), {})
            if not record.get("success") or not isinstance(record.get("data"), dict):
                raise ValueError("package metadata unavailable")
            package = record["data"]
            row["name"] = package.get("name") or row["name"]
            row["includedAppids"] = sorted({value for item in package.get("apps", []) if (value := integer(item.get("id"))) and value > 0})
            row["metadataCheckedAt"] = now_iso()
        except Exception:
            metadata_ok = False
            for key in ("includedAppids", "metadataCheckedAt"):
                if key in old:
                    row[key] = copy.deepcopy(old[key])
    row["nameZh"] = chinese_label(row["name"], standard=standard)
    return row, metadata_ok


def _package_quote(row, sub, data):
    if not row.get("includedAppids") or int(row["appid"]) not in row["includedAppids"]:
        return _unknown(row["steamUrl"], "该购买包的包含内容尚未核验，暂不展示价格，避免将独立推广商品当作游戏版本。")
    # This group price is a current appdetails response for cc=cn. Its numeric
    # amount has no currency field, so require the same response's CNY overview.
    if data.get("price_overview", {}).get("currency") != "CNY":
        return _unknown(row["steamUrl"], "购买选项未附可核验的人民币货币代码，暂不展示其价格。")
    price = money(sub.get("price_in_cents_with_discount"), cents=True)
    if price is None:
        return _unknown(row["steamUrl"], "该购买包未返回当前国区价格。")
    option = html.unescape(str(sub.get("option_text", "")))
    original_match = re.search(r'class=[\"\']discount_original_price[\"\'][^>]*>\s*[¥￥]\s*([\d,.]+)', option)
    original = money(original_match.group(1).replace(",", "")) if original_match else price if integer(sub.get("percent_savings")) == 0 else None
    return quote("steam", row["steamUrl"], price=price, original=original, status="ok", stock="in_stock", eligible=True,
                 note="Steam 国区该购买包当前直售价；内容以对应 SubID 为准。")


def _dlc_metadata(client, appid, parent, old, *, refresh):
    url = store_url("app", appid)
    if cached(old, refresh=refresh) and old.get("metadataVerified"):
        row = {key: copy.deepcopy(value) for key, value in old.items() if key != "quotes"}
        return row, True
    row = _pending_dlc(appid, parent, old)
    row["metadataAttemptedAt"] = now_iso()
    try:
        payload = client.get(f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=cn&l=schinese&filters=basic,price_overview,packages,package_groups")
        record = payload.get(str(appid), {})
        data = record.get("data", {})
        if not record.get("success") or data.get("type") not in ("dlc", "music") or not data.get("name"):
            raise ValueError("DLC metadata unavailable or not additional content")
        fullgame = integer(data.get("fullgame", {}).get("appid"))
        if fullgame and fullgame != parent:
            raise ValueError("DLC parent does not match")
        row.update(name=data["name"], contentType=data["type"], metadataVerified=True, metadataStatus="ok",
                   metadataCheckedAt=now_iso(), isFree=data.get("is_free") is True,
                   note="DLC 需要游戏本体；请在 Steam 商店确认使用条件。" if data["type"] == "dlc" else "原声带内容，具体使用条件以 Steam 商店为准。")
        sub = standard_package(data, {"name": data["name"], "nameEn": data["name"], "aliases": []})
        if sub:
            row["subid"] = int(sub["packageid"])
        return row, True
    except Exception:
        if old.get("metadataVerified"):
            row = {key: copy.deepcopy(value) for key, value in old.items() if key != "quotes"}
            row["note"] = str(row.get("note", "")) + " 本轮名称与版本信息未刷新。"
        row["metadataAttemptedAt"] = now_iso()
        row["metadataStatus"] = "error"
        return row, False


def _pending_dlc(appid, parent, old):
    if old.get("metadataVerified"):
        return {key: copy.deepcopy(value) for key, value in old.items() if key != "quotes"}
    row = {"id": f"app-{appid}", "appid": appid, "type": "dlc", "parentAppid": parent,
           "name": f"DLC {appid} · 资料待同步", "steamUrl": store_url("app", appid),
           "metadataVerified": False, "metadataStatus": "pending",
           "note": "Steam 已列出该附加内容 AppID；名称、类型及报价等待后续分批核验。"}
    if old.get("metadataAttemptedAt"):
        row["metadataAttemptedAt"] = old["metadataAttemptedAt"]
    return row


def load_content(client, game, data, standard_subid, standard_quotes, previous=None, *, refresh=False):
    """Return editions, DLC, and explicit collection coverage for one game."""
    previous = previous or {}
    appid = int(game["appid"])
    old_editions = {row.get("id"): row for row in previous.get("editions", [])}
    old_dlcs = {int(row["appid"]): row for row in previous.get("dlc", []) if integer(row.get("appid"))}
    checked = now_iso()
    if not data or data.get("type") != "game":
        editions, dlcs = copy.deepcopy(previous.get("editions", [])), copy.deepcopy(previous.get("dlc", []))
        for row in editions + dlcs:
            row["quotes"] = stale_quotes(row.get("quotes"), "Steam 游戏元数据暂不可用。")
        return editions, dlcs, {"editions": "stale" if editions else "unavailable", "dlc": "stale" if dlcs else "unavailable",
                                 "checkedAt": checked, "note": "本轮未取得 Steam 本体信息，未推测版本或 DLC 列表。"}

    editions, metadata_failed, unrelated_packages = [], 0, 0
    seen = set()
    for group in data.get("package_groups", []):
        for sub in group.get("subs", []):
            subid = integer(sub.get("packageid"))
            if not subid or subid <= 0 or subid in seen:
                continue
            seen.add(subid)
            row, ok = _package(client, game, sub, old_editions.get(f"sub-{subid}", {}), standard=subid == standard_subid, refresh=refresh)
            metadata_failed += not ok
            if row.get("includedAppids") and appid not in row["includedAppids"]:
                # E.g. Portal 2 advertises The Final Hours as another purchase
                # option. A separate app is not an edition of Portal 2.
                unrelated_packages += 1
                continue
            row["quotes"] = copy.deepcopy(standard_quotes) if row["isStandard"] else [_package_quote(row, sub, data)]
            editions.append(row)
    if not any(row.get("isStandard") for row in editions):
        editions.insert(0, {"id": f"app-{appid}", "kind": "base", "appid": appid, "name": data.get("name", game["name"]),
                            "nameZh": chinese_label(data.get("name", game["name"]), standard=True), "isStandard": True,
                            "steamUrl": store_url("app", appid), "quotes": copy.deepcopy(standard_quotes)})
    bundle_ok = True
    try:
        editions.extend(parse_bundles(fetch_storefront(client, appid), appid))
    except Exception:
        bundle_ok = False
        for old in old_editions.values():
            if old.get("kind") == "bundle":
                row = copy.deepcopy(old)
                row["quotes"] = stale_quotes(row.get("quotes"), "公开商店购买区暂不可用。")
                editions.append(row)
    editions.sort(key=lambda row: (not row.get("isStandard"), row.get("kind") == "bundle", row.get("name", "")))

    dlc_ids = list(dict.fromkeys(value for raw in data.get("dlc", []) if (value := integer(raw)) and value > 0))
    dlcs, dlc_metadata_failed = [], 0
    needs_metadata = [dlcid for dlcid in dlc_ids if not cached(old_dlcs.get(dlcid, {}), refresh=refresh)
                      or not old_dlcs.get(dlcid, {}).get("metadataVerified")]
    # Unnamed additions take precedence over refreshing known names. Failed
    # attempts move behind entries never tried, preventing one failing batch
    # from permanently blocking the rest of a game's DLC directory.
    needs_metadata.sort(key=lambda dlcid: (bool(old_dlcs.get(dlcid, {}).get("metadataVerified")),
                                          old_dlcs.get(dlcid, {}).get("metadataAttemptedAt", "")))
    scheduled = set(needs_metadata[:DLC_METADATA_BATCH_LIMIT])
    for dlcid in dlc_ids:
        old = old_dlcs.get(dlcid, {})
        if dlcid in scheduled or (cached(old, refresh=refresh) and old.get("metadataVerified")):
            row, ok = _dlc_metadata(client, dlcid, appid, old, refresh=refresh)
            dlc_metadata_failed += not ok
        else:
            row = _pending_dlc(dlcid, appid, old)
        dlcs.append(row)
    price_rows, failed_ids = {}, set()
    # Only price_overview supports multiple AppIDs. Keep bounded batches and
    # preserve unknown entries instead of silently truncating a large DLC list.
    for start in range(0, len(dlc_ids), 100):
        ids = dlc_ids[start:start + 100]
        query = urllib.parse.urlencode({"appids": ",".join(map(str, ids)), "cc": "cn", "l": "schinese", "filters": "price_overview"})
        try:
            payload = client.get("https://store.steampowered.com/api/appdetails?" + query)
            if not isinstance(payload, dict):
                raise ValueError("DLC price response invalid")
            price_rows.update(payload)
        except Exception:
            failed_ids.update(ids)
    for row in dlcs:
        dlcid = row["appid"]
        old = old_dlcs.get(dlcid, {})
        if dlcid in failed_ids:
            row["quotes"] = stale_quotes(old.get("quotes"), "DLC 批量价格接口暂不可用。") or [_unknown(row["steamUrl"], "本轮 DLC 价格采集失败。")]
        elif not row.get("metadataVerified"):
            pending = quote("steam", row["steamUrl"], status="unknown",
                            note="DLC 资料待同步：名称、类型或本体关联尚未核验，暂不展示报价。")
            pending["reasonCode"] = "metadata_pending"
            row["quotes"] = [pending]
        else:
            record = price_rows.get(str(dlcid), {})
            if not record.get("success"):
                row["quotes"] = [_unknown(row["steamUrl"], "Steam 国区未返回该 DLC，可能暂未发售或已下架。")]
            else:
                price_data = record.get("data")
                overview = price_data.get("price_overview", {}) if isinstance(price_data, dict) else {}
                row["quotes"] = [_priced(row["steamUrl"], overview, is_free=row.get("isFree") is True,
                                         note="Steam 国区该 DLC / 原声带当前直售价；不含游戏本体。")]
    unavailable = sum(not row.get("metadataVerified") or not any(q.get("status") == "ok" for q in row["quotes"]) for row in dlcs)
    metadata_complete = sum(bool(row.get("metadataVerified")) for row in dlcs)
    metadata_remaining = len(dlcs) - metadata_complete
    note = "版本与 DLC 按 Steam 公开国区购买选项区分；捆绑包另行标注，缺失报价不填估算值。"
    if not bundle_ok:
        note += " 公开商店页面未取得可核验的 CNY 购买区，捆绑包可能不完整。"
    if metadata_failed or dlc_metadata_failed:
        note += " 部分内容名称或包含项目未能刷新。"
    if unrelated_packages:
        note += f" 已排除 {unrelated_packages} 个经购买包内容核验不含本体的独立商品。"
    if metadata_remaining or len(needs_metadata) > DLC_METADATA_BATCH_LIMIT:
        note += (f" DLC 资料已核验 {metadata_complete}/{len(dlcs)} 项，本轮最多读取 {DLC_METADATA_BATCH_LIMIT} 项；"
                 f"{metadata_remaining} 项资料待后续分批采集。")
    return editions, dlcs, {"editions": "ok" if bundle_ok and not metadata_failed else "partial",
                             "dlc": "partial" if failed_ids or unavailable or dlc_metadata_failed else "ok",
                             "checkedAt": checked, "editionCount": len(editions), "dlcCount": len(dlcs),
                             "dlcMetadataComplete": metadata_complete, "dlcMetadataPending": metadata_remaining,
                             "dlcMetadataFetched": len(scheduled), "dlcMetadataBatchLimit": DLC_METADATA_BATCH_LIMIT,
                             "note": note}
