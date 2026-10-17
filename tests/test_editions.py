"""Check version identity, native currency, DLC parentage, and failed refreshes."""
import copy
import html
import json
import sys
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.editions import DLC_METADATA_BATCH_LIMIT, chinese_label, load_content, option_name, parse_bundles
from scripts.providers import now_iso, quote


GAME = {"appid": 10, "name": "示例游戏", "nameEn": "Example Game"}


def base_data(currency="CNY"):
    return {"type": "game", "name": "示例游戏", "price_overview": {"currency": currency, "initial": 10000, "final": 5000},
            "package_groups": [{"subs": [
                {"packageid": 100, "option_text": '示例游戏 - <span class="discount_original_price">¥ 100.00</span> ¥ 50.00',
                 "price_in_cents_with_discount": 5000, "percent_savings": 50},
                {"packageid": 101, "option_text": "Example Game - Deluxe Edition - ¥ 80.00", "price_in_cents_with_discount": 8000,
                 "percent_savings": 0},
            ]}], "dlc": [20]}


def standard_quotes():
    return [quote("steam", "https://store.steampowered.com/app/10/", price=50, original=100,
                  status="ok", stock="in_stock", eligible=True),
            quote("steampy", "https://steampy.com/", price=40, status="unknown")]


def bundle_markup(currency="CNY", parent=10):
    data = html.escape(json.dumps({"m_bMustPurchaseAsSet": 0, "m_rgItems": [{"m_rgIncludedAppIDs": [parent, 20]}]}), quote=True)
    return f'''<meta itemprop="priceCurrency" content="{currency}">
      <div class="game_area_purchase_game_wrapper dynamic_bundle_description" data-ds-bundleid="300" data-ds-bundle-data="{data}">
       <div><h2>购买 Example Game Ultimate Edition</h2><input name="bundleid" value="300">
        <div class="discount_block" data-price-final="9000"><div class="discount_original_price">¥ 160.00</div>
        <div class="discount_final_price">¥ 90.00</div></div></div>
      </div>'''


class Client:
    def __init__(self):
        self.calls = []
        self.fail_prices = False
        self.fail_all = False
        self.currency = "CNY"
        self.parent = "10"
        self.dlc_type = "dlc"
        self.free = False
        self.empty_free_price = False
        self.empty_price_list = False

    def get_text(self, url):
        self.calls.append(url)
        if self.fail_all:
            raise TimeoutError()
        return '<meta itemprop="priceCurrency" content="CNY">'

    def get(self, url, data=None, headers=None):
        self.calls.append(url)
        if self.fail_all:
            raise TimeoutError()
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "packagedetails" in url:
            subid = parsed["packageids"][0]
            return {subid: {"success": True, "data": {"name": "示例游戏" if subid == "100" else "Example Game - Deluxe Edition",
                                                       "apps": [{"id": 10}, {"id": 20}]}}}
        if parsed.get("filters") == ["price_overview"]:
            if self.fail_prices:
                raise TimeoutError()
            overview = {} if self.empty_free_price else {"price_overview": {"currency": self.currency, "initial": 3000, "final": 1500}}
            return {"20": {"success": True, "data": [] if self.empty_price_list else overview}}
        return {"20": {"success": True, "data": {"type": self.dlc_type, "name": "Example Game - Expansion",
                "fullgame": {"appid": self.parent}, "is_free": self.free,
                "package_groups": [{"subs": [{"packageid": 200, "option_text": "Example Game - Expansion - ¥ 15.00"}]}]}}}


class LargeDlcClient(Client):
    def __init__(self, *, fail_metadata=False):
        super().__init__()
        self.metadata_ids = []
        self.price_batches = []
        self.fail_metadata = fail_metadata

    def get(self, url, data=None, headers=None):
        parsed = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if "packagedetails" in url:
            return super().get(url, data, headers)
        ids = parsed.get("appids", [""])[0].split(",")
        if parsed.get("filters") == ["price_overview"]:
            self.price_batches.append(ids)
            return {appid: {"success": True, "data": {"price_overview": {"currency": "CNY", "initial": 3000, "final": 1500}}}
                    for appid in ids}
        appid = ids[0]
        self.metadata_ids.append(int(appid))
        if self.fail_metadata:
            raise TimeoutError()
        return {appid: {"success": True, "data": {"type": "dlc", "name": f"真实附加内容 {appid}",
                       "fullgame": {"appid": "10"}, "is_free": False}}}


class EditionTests(unittest.TestCase):
    def test_version_labels_preserve_full_title_and_separate_bundle(self):
        self.assertEqual(option_name('Example Game - Deluxe Edition - <span class="discount_original_price">¥ 100</span> ¥ 50'),
                         "Example Game - Deluxe Edition")
        self.assertEqual(chinese_label("Example Game - Deluxe Edition"), "Example Game - 豪华版")
        self.assertEqual(chinese_label("Another Title"), "Another Title")
        self.assertEqual(option_name("\n\t 购买 《示例终极版》\t\n"), "《示例终极版》")
        row = parse_bundles(bundle_markup(), 10)[0]
        self.assertEqual(row["kind"], "bundle")
        self.assertEqual(row["id"], "bundle-300")
        self.assertEqual(row["quotes"][0]["price"], 90)
        self.assertIn("补齐价格可能不同", row["note"])

    def test_bundle_requires_explicit_cny_and_parent_inclusion(self):
        for foreign in ("JPY", "USD", ""):
            with self.assertRaises(ValueError):
                parse_bundles(bundle_markup(foreign), 10)
        self.assertEqual(parse_bundles(bundle_markup(parent=999), 10), [])

    def test_standard_quotes_not_reused_for_deluxe(self):
        values = standard_quotes()
        editions, dlcs, status = load_content(Client(), GAME, base_data(), 100, values)
        standard, deluxe = editions
        self.assertTrue(standard["isStandard"])
        self.assertEqual(standard["quotes"], values)
        self.assertEqual(deluxe["subid"], 101)
        self.assertEqual(deluxe["quotes"][0]["price"], 80)
        self.assertEqual(len(deluxe["quotes"]), 1)
        self.assertEqual(dlcs[0]["appid"], 20)
        self.assertEqual(dlcs[0]["subid"], 200)
        self.assertEqual(dlcs[0]["quotes"][0]["price"], 15)
        self.assertEqual(status["dlcCount"], 1)

    def test_package_price_without_cny_is_not_guessed(self):
        editions, _, _ = load_content(Client(), GAME, base_data("USD"), 100, standard_quotes())
        self.assertIsNone(editions[1]["quotes"][0]["price"])
        self.assertFalse(editions[1]["quotes"][0]["eligible"])

    def test_unrelated_advertised_package_is_not_a_game_edition(self):
        client = Client()
        original_get = client.get
        def get(url, data=None, headers=None):
            response = original_get(url, data, headers)
            if "packageids=101" in url:
                response["101"]["data"]["apps"] = [{"id": 999}]
            return response
        client.get = get
        editions, _, status = load_content(client, GAME, base_data(), 100, standard_quotes())
        self.assertEqual([row["subid"] for row in editions], [100])
        self.assertIn("不含本体", status["note"])

    def test_new_package_without_verified_inclusions_has_no_price(self):
        client = Client()
        original_get = client.get
        def get(url, data=None, headers=None):
            if "packageids=101" in url:
                raise TimeoutError()
            return original_get(url, data, headers)
        client.get = get
        editions, _, _ = load_content(client, GAME, base_data(), 100, standard_quotes())
        deluxe = next(row for row in editions if row.get("subid") == 101)
        self.assertIsNone(deluxe["quotes"][0]["price"])
        self.assertEqual(deluxe["quotes"][0]["status"], "unavailable")
        self.assertIn("包含内容尚未核验", deluxe["quotes"][0]["note"])

    def test_prior_verified_package_inclusions_allow_current_group_price(self):
        client = Client()
        editions, dlcs, _ = load_content(client, GAME, base_data(), 100, standard_quotes())
        old = {"editions": editions, "dlc": dlcs}
        original_get = client.get
        def get(url, data=None, headers=None):
            if "packageids=101" in url:
                raise TimeoutError()
            return original_get(url, data, headers)
        client.get = get
        data = base_data()
        data["package_groups"][0]["subs"][1]["price_in_cents_with_discount"] = 6500
        editions, _, _ = load_content(client, GAME, data, 100, standard_quotes(), old, refresh=True)
        deluxe = next(row for row in editions if row.get("subid") == 101)
        self.assertIn(10, deluxe["includedAppids"])
        self.assertEqual(deluxe["quotes"][0]["price"], 65)

    def test_dlc_must_have_verified_type_parent_and_currency(self):
        for attr, value in [("parent", "999"), ("dlc_type", "game"), ("currency", "JPY")]:
            client = Client()
            setattr(client, attr, value)
            _, dlcs, status = load_content(client, GAME, base_data(), 100, standard_quotes())
            self.assertIsNone(dlcs[0]["quotes"][0]["price"])
            self.assertEqual(status["dlc"], "partial")

    def test_free_dlc_and_zero_price_are_preserved(self):
        client = Client()
        client.free, client.empty_free_price = True, True
        _, dlcs, _ = load_content(client, GAME, base_data(), 100, standard_quotes())
        self.assertEqual(dlcs[0]["quotes"][0]["price"], 0)
        self.assertTrue(dlcs[0]["quotes"][0]["eligible"])

    def test_steam_empty_list_price_data_does_not_crash_or_become_free(self):
        client = Client()
        client.empty_price_list = True
        _, dlcs, status = load_content(client, GAME, base_data(), 100, standard_quotes())
        self.assertIsNone(dlcs[0]["quotes"][0]["price"])
        self.assertEqual(status["dlc"], "partial")

    def test_metadata_cache_keeps_names_but_refreshes_current_prices(self):
        first = Client()
        editions, dlcs, status = load_content(first, GAME, base_data(), 100, standard_quotes())
        second = Client()
        result = load_content(second, GAME, base_data(), 100, standard_quotes(), {"editions": editions, "dlc": dlcs})
        metadata_calls = [url for url in second.calls if "packagedetails" in url or "filters=basic" in url]
        self.assertEqual(metadata_calls, [])
        self.assertTrue(any("filters=price_overview" in url for url in second.calls))
        self.assertEqual(result[1][0]["name"], "Example Game - Expansion")
        third = Client()
        load_content(third, GAME, base_data(), 100, standard_quotes(), {"editions": editions, "dlc": dlcs}, refresh=True)
        self.assertTrue(any("packagedetails" in url for url in third.calls))

    def test_failed_dlc_refresh_preserves_original_quote_time(self):
        client = Client()
        editions, dlcs, _ = load_content(client, GAME, base_data(), 100, standard_quotes())
        dlcs[0]["quotes"][0]["checkedAt"] = "2026-01-01T00:00:00Z"
        old = copy.deepcopy(dlcs)
        client.fail_prices = True
        _, result, status = load_content(client, GAME, base_data(), 100, standard_quotes(), {"editions": editions, "dlc": dlcs})
        value = result[0]["quotes"][0]
        self.assertEqual(value["price"], 15)
        self.assertEqual(value["checkedAt"], "2026-01-01T00:00:00Z")
        self.assertEqual(value["status"], "stale")
        self.assertFalse(value["eligible"])
        self.assertEqual(dlcs, old)
        self.assertEqual(status["dlc"], "partial")

    def test_no_base_metadata_preserves_content_without_inventing_it(self):
        client = Client()
        editions, dlcs, _ = load_content(client, GAME, base_data(), 100, standard_quotes())
        old = {"editions": editions, "dlc": dlcs}
        values, addons, status = load_content(client, GAME, {}, None, [], old)
        self.assertEqual(values[1]["subid"], 101)
        self.assertEqual(addons[0]["quotes"][0]["status"], "stale")
        self.assertEqual(status["editions"], "stale")
        self.assertEqual(load_content(client, GAME, {}, None, [])[0:2], ([], []))

    def test_deduplicate_public_package_and_dlc_ids(self):
        data = base_data()
        data["package_groups"].append(copy.deepcopy(data["package_groups"][0]))
        data["dlc"] = [20, 20, True, -1]
        editions, dlcs, _ = load_content(Client(), GAME, data, 100, standard_quotes())
        self.assertEqual(len(editions), 2)
        self.assertEqual(len(dlcs), 1)

    def test_large_dlc_directory_progresses_in_bounded_batches(self):
        data = base_data()
        data["dlc"] = list(range(1000, 1123))
        first = LargeDlcClient()
        editions, dlcs, status = load_content(first, GAME, data, 100, standard_quotes())
        self.assertEqual(len(dlcs), 123)
        self.assertEqual(len(first.metadata_ids), DLC_METADATA_BATCH_LIMIT)
        self.assertEqual([len(batch) for batch in first.price_batches], [100, 23])
        self.assertEqual(status["dlcMetadataComplete"], 30)
        self.assertEqual(status["dlcMetadataPending"], 93)
        pending = dlcs[30]
        self.assertEqual(pending["name"], "DLC 1030 · 资料待同步")
        self.assertIsNone(pending["quotes"][0]["price"])
        self.assertEqual(pending["quotes"][0]["reasonCode"], "metadata_pending")
        previous = {"editions": editions, "dlc": dlcs}
        for expected in (60, 90, 120, 123):
            client = LargeDlcClient()
            editions, dlcs, status = load_content(client, GAME, data, 100, standard_quotes(), previous)
            self.assertLessEqual(len(client.metadata_ids), DLC_METADATA_BATCH_LIMIT)
            self.assertEqual(status["dlcMetadataComplete"], expected)
            self.assertEqual(status["dlcMetadataPending"], 123 - expected)
            previous = {"editions": editions, "dlc": dlcs}
        self.assertEqual(status["dlc"], "ok")
        self.assertEqual(dlcs[-1]["name"], "真实附加内容 1122")
        self.assertEqual(dlcs[-1]["quotes"][0]["price"], 15)

    def test_failed_first_metadata_batch_does_not_starve_next_items(self):
        data = base_data()
        data["dlc"] = list(range(1000, 1035))
        first = LargeDlcClient(fail_metadata=True)
        editions, dlcs, status = load_content(first, GAME, data, 100, standard_quotes())
        self.assertEqual(status["dlcMetadataPending"], 35)
        second = LargeDlcClient()
        _, dlcs, _ = load_content(second, GAME, data, 100, standard_quotes(), {"editions": editions, "dlc": dlcs})
        self.assertTrue(set(range(1030, 1035)).issubset(second.metadata_ids))
        self.assertEqual(len(second.metadata_ids), DLC_METADATA_BATCH_LIMIT)
        self.assertTrue(all(dlc["metadataVerified"] for dlc in dlcs[-5:]))


if __name__ == "__main__":
    unittest.main()
