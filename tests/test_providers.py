"""High-impact matching/eligibility rules; never depend on live price values."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.providers import (fetch_cici, fetch_heybox, fetch_sonkwo, fetch_steam, fetch_steampy,
                               match_cici_row, money, quote, standard_package, steampy_product_url, suspicious_price)
from scripts.collect_prices import collect, history_point, merge_history, stale_quote

GAME = {"appid": 1245620, "name": "艾尔登法环", "nameEn": "ELDEN RING", "aliases": ["老头环"]}


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url, data=None, headers=None):
        self.calls.append((url, data, headers))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response(url, data, headers) if callable(self.response) else copy.deepcopy(self.response)


def app_data():
    return {"name": "艾尔登法环", "type": "game", "price_overview": {"currency": "CNY", "final": 29800, "initial": 29800},
            "package_groups": [{"subs": [
                {"packageid": 440408, "option_text": "艾尔登法环 - ¥ 298.00", "price_in_cents_with_discount": 29800},
                {"packageid": 9999, "option_text": "艾尔登法环 数字豪华版 - ¥ 398.00", "price_in_cents_with_discount": 39800},
                {"packageid": 8888, "option_text": "艾尔登法环 Soundtrack - ¥ 1.00", "price_in_cents_with_discount": 100},
            ]}]}


class MatchingTests(unittest.TestCase):
    def test_standard_package_ignores_cheaper_dlc(self):
        self.assertEqual(standard_package(app_data(), GAME)["packageid"], 440408)

    def test_ambiguous_standard_package_fails_closed(self):
        data = app_data()
        data["package_groups"][0]["subs"].append({"packageid": 123, "option_text": "ELDEN RING - ¥ 1"})
        self.assertIsNone(standard_package(data, GAME))

    def test_dash_delimited_deluxe_is_not_base(self):
        data = app_data()
        data["package_groups"][0]["subs"] = [{"packageid": 123, "option_text": "ELDEN RING - Deluxe Edition - ¥ 1"}]
        self.assertIsNone(standard_package(data, GAME))

    def test_discount_markup_retains_base_title(self):
        data = app_data()
        data["package_groups"][0]["subs"][0]["option_text"] = '艾尔登法环 - <span class="discount_original_price">¥ 298.00</span> ¥ 149.00'
        self.assertEqual(standard_package(data, GAME)["packageid"], 440408)

    def test_same_name_packages_need_unique_steam_overview_price(self):
        data = app_data()
        data["package_groups"][0]["subs"].append({"packageid": 123, "option_text": "艾尔登法环 - ¥ 398.00", "price_in_cents_with_discount": 39800})
        self.assertEqual(standard_package(data, GAME)["packageid"], 440408)
        data["package_groups"][0]["subs"][-1]["price_in_cents_with_discount"] = 29800
        self.assertIsNone(standard_package(data, GAME))

    def test_steam_foreign_currency_and_dlc_fail_closed(self):
        data = app_data()
        data["price_overview"]["currency"] = "USD"
        value, _, _ = fetch_steam(FakeClient({"1245620": {"success": True, "data": data}}), GAME)
        self.assertIsNone(value["price"])
        self.assertFalse(value["eligible"])
        data["type"] = "dlc"
        value, _, _ = fetch_steam(FakeClient({"1245620": {"success": True, "data": data}}), GAME)
        self.assertEqual(value["status"], "unavailable")

    def test_cici_requires_both_ids_and_base_version(self):
        base = {"parentId": 1245620, "gameId": "440408", "isDeluxe": "0", "bundle": "0", "dlcType": "0"}
        wrong_app = dict(base, parentId=999)
        deluxe = dict(base, isDeluxe="1")
        wrong_package = dict(base, gameId="1010505")
        self.assertEqual(match_cici_row([wrong_app, deluxe, wrong_package, base], 1245620, 440408), base)
        self.assertIsNone(match_cici_row([base, base], 1245620, 440408))

    def test_cici_transaction_price_not_eligible_even_if_stock_positive(self):
        row = {"parentId": 1245620, "gameId": "440408", "isDeluxe": "0", "bundle": "0", "dlcType": "0", "lastLowSellPrice": 210, "sumStockNum": 5}
        value = fetch_cici(FakeClient({"code": 200, "rows": [row]}), GAME, 440408, 298)
        self.assertEqual(value["price"], 210)
        self.assertEqual(value["stock"], "unknown")
        self.assertFalse(value["eligible"])

    def test_py_excludes_balance_and_surprisingly_low_prices(self):
        client = FakeClient({"success": True, "result": {"keyPrice": 0.1, "marketPrice": 20, "daiPrice": 10}})
        value = fetch_steampy(client, GAME, 440408, 298)
        self.assertIsNone(value["price"])
        self.assertEqual(value["status"], "suspect")
        self.assertFalse(value["eligible"])
        self.assertEqual(value["reasonCode"], "unverified_tiny_reference")
        client.response["result"]["keyPrice"] = None
        self.assertIsNone(fetch_steampy(client, GAME, 440408, 298)["price"])

    def test_py_detail_url_uses_returned_product_id(self):
        client = FakeClient({"success": True, "result": {"keyPrice": 100, "id": "468688264593674240"}})
        value = fetch_steampy(client, GAME, 440408, 298)
        self.assertEqual(value["url"], "https://steampy.com/cdkDetail?name=cn&gameId=468688264593674240")
        self.assertEqual(steampy_product_url("bad&id=1"), "https://steampy.com/")

    def test_py_plausible_reference_never_becomes_live_order(self):
        client = FakeClient({"success": True, "result": {"keyPrice": 23.8, "id": "325949970807001088", "stock": 9}})
        value = fetch_steampy(client, GAME, 440408, 298)
        self.assertEqual(value["price"], 23.8)
        self.assertEqual(value["status"], "unknown")
        self.assertEqual(value["priceKind"], "reference")
        self.assertEqual(value["reasonCode"], "orderbook_requires_login")
        self.assertEqual(value["stock"], "unknown")
        self.assertFalse(value["eligible"])
        self.assertIn("登录", value["note"])

    def test_py_does_not_reject_deep_discount_only_by_ratio(self):
        client = FakeClient({"success": True, "result": {"keyPrice": 3, "id": "123"}})
        value = fetch_steampy(client, GAME, 440408, 999)
        self.assertEqual(value["price"], 3)
        self.assertEqual(value["status"], "unknown")

    def test_py_invalid_and_tiny_values_are_hidden(self):
        for price in (None, True, "NaN", "Infinity", -1, 0, 0.01, 0.1, 0.5, 1):
            with self.subTest(price=price):
                value = fetch_steampy(FakeClient({"success": True, "result": {"keyPrice": price, "id": "123"}}), GAME, 440408, 298)
                self.assertIsNone(value["price"])
                self.assertFalse(value["eligible"])

    def test_py_does_not_substitute_wallet_gift_or_balance_price(self):
        for field in ("marketPrice", "daiPrice", "giftPrice", "balancePrice"):
            client = FakeClient({"success": True, "result": {field: 50, "id": "123"}})
            self.assertIsNone(fetch_steampy(client, GAME, 440408, 298)["price"])
        client = FakeClient({"success": True, "result": {"keyPrice": 50, "id": "123", "deliveryType": "gift"}})
        value = fetch_steampy(client, GAME, 440408, 298)
        self.assertIsNone(value["price"])
        self.assertEqual(value["reasonCode"], "delivery_mismatch")

    def test_py_requires_exact_ids_if_endpoint_echoes_them(self):
        for mismatch in ({"appId": 620}, {"subId": 1010506}, {"subId": 440408.5}, {"appId": True}):
            result = dict(keyPrice=100, id="123", **mismatch)
            value = fetch_steampy(FakeClient({"success": True, "result": result}), GAME, 440408, 298)
            self.assertIsNone(value["price"])
            self.assertEqual(value["reasonCode"], "version_mismatch")

    def test_py_foreign_currency_and_region_are_excluded(self):
        for mismatch, code in (({"currency": "USD"}, "currency_mismatch"),
                               ({"cc": "us"}, "region_mismatch"),
                               ({"region": "ru"}, "region_mismatch")):
            value = fetch_steampy(FakeClient({"success": True, "result": dict(keyPrice=100, id="123", **mismatch)}), GAME, 440408, 298)
            self.assertIsNone(value["price"])
            self.assertEqual(value["reasonCode"], code)

    def test_py_no_package_no_request_and_no_fuzzy_name_match(self):
        for subid in (None, 0, -1, True, 440408.5, "440408&foo=bar"):
            client = FakeClient({})
            value = fetch_steampy(client, GAME, subid, 298)
            self.assertEqual(client.calls, [])
            self.assertIsNone(value["price"])
            self.assertEqual(value["reasonCode"], "missing_package")

    def test_py_explicit_deluxe_package_stays_separate(self):
        client = FakeClient({"success": True, "result": {"keyPrice": 200, "id": "772935684620029952", "subId": 1010506, "appId": 1245620}})
        value = fetch_steampy(client, GAME, 1010506, 498, edition_name="黄金树之影豪华版")
        self.assertIn("subId=1010506", client.calls[0][0])
        self.assertIn("appId=1245620", client.calls[0][0])
        self.assertEqual(value["editionSubId"], 1010506)
        self.assertEqual(value["editionName"], "黄金树之影豪华版")
        self.assertEqual(value["price"], 200)
        self.assertEqual(value["sourceUrl"], client.calls[0][0])
        self.assertEqual(value["lastCheckedAt"], value["checkedAt"])

    def test_py_missing_product_and_zero_stock_are_not_quotes(self):
        for result in ({"keyPrice": 100}, {"keyPrice": 100, "id": "0"}, {"keyPrice": 100, "id": "123", "stock": 0}):
            value = fetch_steampy(FakeClient({"success": True, "result": result}), GAME, 440408, 298)
            self.assertIsNone(value["price"])
            self.assertFalse(value["eligible"])

    def test_py_unlisted_package_and_login_required_are_explicit(self):
        for code, reason in ((203, "not_in_catalog"), (401, "reference_requires_login"), (403, "reference_requires_login")):
            value = fetch_steampy(FakeClient({"success": False, "code": code, "result": None}), GAME, 1010506, 498)
            self.assertIsNone(value["price"])
            self.assertEqual(value["status"], "unavailable")
            self.assertEqual(value["reasonCode"], reason)

    def test_money_rejects_non_finite_and_boolean(self):
        for value in (True, "NaN", "Infinity", -1, {}, None):
            self.assertIsNone(money(value))
        self.assertEqual(money(29800, cents=True), 298)

    def test_heybox_uses_cn_and_labels_reference(self):
        payload = {"status": "ok", "result": {"prices": [
            {"cc": "us", "current": "59.99", "initial": "59.99"},
            {"cc": "cn", "current": "298", "initial": "298"},
        ]}}
        client = FakeClient(payload)
        value = fetch_heybox(client, GAME)
        self.assertEqual(value["price"], 298)
        self.assertEqual(value["dataProvider"], "heybox")
        self.assertFalse(value["eligible"])
        self.assertIn("Referer", client.calls[0][2])


class IntegrityTests(unittest.TestCase):
    def test_stale_quote_preserves_actual_timestamp(self):
        old = quote("steam", "https://example.com", price=10, status="ok", stock="in_stock", eligible=True)
        old["checkedAt"] = "2026-01-01T00:00:00Z"
        value = stale_quote(old, "steam", "接口失败")
        self.assertEqual(value["checkedAt"], old["checkedAt"])
        self.assertEqual(value["status"], "stale")
        self.assertFalse(value["eligible"])
        self.assertEqual(old["status"], "ok")

    def test_history_excludes_unverified_cdk_and_does_not_invent_past(self):
        values = [quote("steam", "https://example.com", price=100, status="ok", stock="in_stock", eligible=True),
                  quote("steamcici", "https://example.com", price=1, status="unknown")]
        history = merge_history([], values, "2026-10-17")
        self.assertEqual(history, [{"date": "2026-10-17", "steam": 100, "cdk": None}])
        self.assertEqual(history_point([], "2026-10-17")["steam"], None)

    def test_failed_daily_run_preserves_prior_observation(self):
        history = [{"date": "2026-10-17", "steam": 100, "cdk": None}]
        self.assertEqual(merge_history(history, [], "2026-10-17"), history)

    def test_same_day_history_preserves_observed_low(self):
        values = [quote("steam", "https://example.com", price=200, status="ok", stock="in_stock", eligible=True)]
        result = merge_history([{"date": "2026-10-17", "steam": 100, "cdk": None}], values, "2026-10-17")
        self.assertEqual(result[0]["steam"], 100)

    def test_sonkwo_exact_sku_rejects_gold_and_wrong_platform(self):
        game = dict(GAME, sonkwo={"sku": 10, "area": "abroad", "platform": "steam", "edition": "standard", "verified": True})
        payload = {"success": True, "data": {"list": [{"id": 10, "skuNames": {"en": "ELDEN RING Gold Edition"}, "kind": 0, "keyType": 0}]}}
        result = fetch_sonkwo(FakeClient(payload), game, {})
        self.assertIsNone(result["price"])
        payload["data"]["list"][0].update(skuNames={"en": "ELDEN RING"}, keyType=6)
        self.assertIsNone(fetch_sonkwo(FakeClient(payload), game, {})["price"])

    def test_sonkwo_explicit_zero_inventory_cannot_be_eligible(self):
        game = dict(GAME, sonkwo={"sku": 10, "area": "abroad", "platform": "steam", "edition": "standard", "verified": True})
        def response(url, data, headers):
            if "page" in url:
                return {"success": True, "data": {"list": [{"id": 10, "skuNames": {"en": "ELDEN RING"}, "kind": 0, "keyType": 0, "listPrice": 298}]}}
            return {"success": True, "data": [{"id": 10, "inventorySellableCount": 0}]}
        result = fetch_sonkwo(FakeClient(response), game, {("https://api.sonkwo.hk", 10): {"salePrice": 200}})
        self.assertEqual(result["stock"], "out_of_stock")
        self.assertFalse(result["eligible"])

    def test_all_errors_are_represented_without_fake_prices(self):
        snapshot = collect([GAME], client=FakeClient(TimeoutError()), workers=1)
        self.assertEqual(snapshot["schemaVersion"], 1)
        self.assertTrue(all(q["price"] is None for q in snapshot["games"][0]["quotes"]))
        self.assertEqual(snapshot["games"][0]["history"], [])


if __name__ == "__main__":
    unittest.main()
