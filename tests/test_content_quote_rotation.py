"""Bound extra SteamPY calls while advancing past failures and preserving age."""
import copy
import unittest
from unittest.mock import patch

from scripts.collect_prices import CONTENT_PY_BATCH_LIMIT, refresh_content_py
from scripts.providers import quote

T0 = "2026-10-17T00:00:00Z"
T1 = "2026-10-17T00:15:00Z"
T2 = "2026-10-17T00:30:00Z"
T3 = "2026-10-17T00:45:00Z"


def steam_quote(price=10, status="ok"):
    value = quote("steam", "https://store.steampowered.com/", price=price, original=20, status=status,
                  stock="in_stock", eligible=status == "ok")
    value["checkedAt"] = T0
    return value


def py_quote(stamp, price=7):
    value = quote("steampy", "https://steampy.com/", price=price, status="unknown")
    value["checkedAt"] = stamp
    return value


def content():
    editions = [{"id": "sub-100", "appid": 10, "subid": 100, "name": "Standard", "isStandard": True,
                 "quotes": [steam_quote(), py_quote(T0)]},
                {"id": "sub-200", "appid": 10, "subid": 200, "name": "Deluxe", "quotes": [steam_quote()]}]
    dlcs = [{"id": f"app-{1000 + i}", "appid": 1000 + i, "subid": 5000 + i,
             "name": f"Expansion {i}", "quotes": [steam_quote()]} for i in range(65)]
    dlcs.extend([{"id": "missing", "appid": 2000, "name": "No exact package", "quotes": [steam_quote()]},
                 {"id": "unavailable", "appid": 2001, "subid": 8000, "name": "No Steam price",
                  "quotes": [steam_quote(price=None, status="unavailable"), py_quote(T0)]}])
    return editions, dlcs


def run(previous=None, *, stamp=T1, fail=False):
    editions, dlcs = content()
    status = {"editions": "ok", "dlc": "ok", "note": "Steam metadata checked."}
    called = []
    def fetch(client, game, subid, original, **kwargs):
        called.append(subid)
        if fail:
            raise TimeoutError("offline")
        return py_quote(stamp)
    with patch("scripts.collect_prices.fetch_steampy", side_effect=fetch), patch("scripts.collect_prices.now_iso", return_value=stamp):
        refresh_content_py(object(), editions, dlcs, previous or {}, status)
    return {"editions": editions, "dlc": dlcs}, status, called


class ContentQuoteRotationTests(unittest.TestCase):
    def test_budget_excludes_standard_missing_subid_and_missing_steam_price(self):
        previous_standard = content()[0][0]
        result, status, called = run()
        self.assertEqual(len(called), CONTENT_PY_BATCH_LIMIT)
        self.assertEqual(len(set(called)), CONTENT_PY_BATCH_LIMIT)
        self.assertNotIn(100, called)
        self.assertNotIn(8000, called)
        self.assertEqual(result["editions"][0], previous_standard)
        self.assertEqual(status["pyCheckedCount"], 30)
        self.assertEqual(status["pyBatchLimit"], 30)
        self.assertEqual(status["pyPendingCount"], 36)
        self.assertIn("分批待更新", status["note"])
        self.assertEqual(status["dlc"], "partial")

    def test_rotates_across_rounds_instead_of_repeating_first_thirty(self):
        first, _, calls1 = run(stamp=T1)
        second, _, calls2 = run(first, stamp=T2)
        _, _, calls3 = run(second, stamp=T3)
        self.assertTrue(set(calls1).isdisjoint(calls2))
        self.assertEqual(set(calls1 + calls2 + calls3), {200, *range(5000, 5065)})

    def test_failed_attempts_advance_and_do_not_block_next_batch(self):
        first, status, calls1 = run(stamp=T1, fail=True)
        self.assertEqual(status["pyFailedCount"], 30)
        attempted = [row for row in first["dlc"] + first["editions"] if row.get("pyAttemptedAt") == T1]
        self.assertEqual(len(attempted), 30)
        self.assertTrue(all(next(q for q in row["quotes"] if q["source"] == "steampy")["price"] is None for row in attempted))
        _, _, calls2 = run(first, stamp=T2)
        self.assertTrue(set(calls1).isdisjoint(calls2))

    def test_pending_old_quote_keeps_price_and_actual_timestamp(self):
        editions, dlcs = content()
        row = copy.deepcopy(next(row for row in dlcs if row["id"] == "app-1064"))
        row["quotes"].append(py_quote(T0, price=23))
        previous = {"editions": [], "dlc": [row]}
        frozen = copy.deepcopy(previous)
        result, _, called = run(previous, stamp=T1)
        self.assertNotIn(5064, called)
        pending = next(row for row in result["dlc"] if row["id"] == "app-1064")
        old_quote = next(q for q in pending["quotes"] if q["source"] == "steampy")
        self.assertEqual(old_quote["price"], 23)
        self.assertEqual(old_quote["checkedAt"], T0)
        self.assertEqual(old_quote["status"], "stale")
        self.assertFalse(old_quote["eligible"])
        self.assertEqual(pending["pyAttemptedAt"], T0)
        new_pending = next(q for q in result["editions"][1]["quotes"] if q["source"] == "steampy")
        self.assertIsNone(new_pending["price"])
        self.assertEqual(new_pending["status"], "unavailable")
        self.assertIn("尚未查询", new_pending["note"])
        self.assertEqual(previous, frozen)


if __name__ == "__main__":
    unittest.main()
