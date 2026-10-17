"""Rotating backfill must make progress while preserving independently dated data."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.refresh_prices import atomic_write, main, normalize_catalog, parse_appids, refresh, select_batch


CORE = {"games": [{"appid": 1, "name": "核心", "nameEn": "Core", "aliases": ["core"]}]}
INDEX = {"games": [{"appid": appid, "name": f"游戏 {appid}", "aliases": [], "type": "game"}
                   for appid in range(1, 9)], "complete": False, "status": "partial"}
T0 = "2026-10-17T00:00:00Z"
T1 = "2026-10-17T00:15:00Z"
T2 = "2026-10-17T00:30:00Z"


def old_game(appid, checked=T0):
    return {"appid": appid, "name": f"游戏 {appid}", "nameEn": f"Game {appid}", "aliases": [],
            "quotes": [{"source": "steam", "kind": "direct", "price": 25, "currency": "CNY",
                        "checkedAt": checked, "status": "ok", "stock": "in_stock", "eligible": True}],
            "history": [{"date": "2026-01-01", "steam": 10, "cdk": None}],
            "steamHistory": {"source": "heybox", "country": "CN", "currency": "CNY", "status": "ok",
                             "fetchedAt": checked, "points": [{"date": "2026-01-01", "price": 10}],
                             "coverageStart": "2026-01-01", "coverageEnd": "2026-01-01"}}


def fake_collect(catalog, previous, **kwargs):
    return {"games": [dict(old_game(row["appid"]), name=row["name"], nameEn=row["nameEn"], aliases=row["aliases"])
                      for row in catalog]}


class RotationTests(unittest.TestCase):
    def test_core_always_runs_and_new_batches_advance(self):
        seen = []
        def collector(catalog, previous, **kwargs):
            seen.append([row["appid"] for row in catalog])
            return fake_collect(catalog, previous, **kwargs)
        first = refresh(CORE, INDEX, collector=collector, scheduled_at=T0)
        second = refresh(CORE, INDEX, first, collector=collector, scheduled_at=T1)
        self.assertEqual(seen, [[1, 2, 3, 4], [1, 5, 6, 2]])
        self.assertEqual({row["appid"] for row in second["games"]}, set(range(1, 7)))
        self.assertEqual(second["rotation"]["checkedCount"], 4)
        self.assertEqual(second["rotation"]["coreCount"], 1)
        self.assertEqual(second["rotation"]["newCount"], 2)
        self.assertEqual(second["rotation"]["scheduledAt"], T1)

    def test_oldest_noncore_is_refreshed_after_all_have_been_seen(self):
        previous = {"games": [old_game(1), old_game(2, T1), old_game(3, T0), old_game(4, T2)]}
        directory = {"games": INDEX["games"][:4]}
        first = refresh(CORE, directory, previous, batch_size=1, scheduled_at=T2, collector=fake_collect)
        self.assertEqual(first["rotation"]["batchAppIds"], [3])
        second = refresh(CORE, directory, first, batch_size=1, scheduled_at="2026-10-17T00:45:00Z", collector=fake_collect)
        self.assertEqual(second["rotation"]["batchAppIds"], [2])

    def test_newer_observation_takes_precedence_over_old_attempt_metadata(self):
        previous = {"games": [old_game(1), old_game(2, T2), old_game(3, T1)],
                    "rotation": {"lastAttempts": {"2": T0}}}
        value = refresh(CORE, {"games": INDEX["games"][:3]}, previous, batch_size=1,
                        scheduled_at="2026-10-17T00:45:00Z", collector=fake_collect)
        self.assertEqual(value["rotation"]["batchAppIds"], [3])

    def test_failure_preserves_data_and_does_not_retry_first_batch_forever(self):
        previous = {"games": [old_game(1), old_game(999)]}
        frozen = copy.deepcopy(previous)
        def fail(*args, **kwargs):
            raise TimeoutError("offline")
        first = refresh(CORE, INDEX, previous, batch_size=2, scheduled_at=T1, collector=fail)
        second = refresh(CORE, INDEX, first, batch_size=2, scheduled_at=T2, collector=fail)
        self.assertEqual(first["games"], frozen["games"])
        self.assertEqual(previous, frozen)
        self.assertEqual(first["rotation"]["batchAppIds"], [2, 3])
        self.assertEqual(second["rotation"]["batchAppIds"], [4, 2])
        self.assertEqual(first["rotation"]["status"], "error")
        self.assertEqual(first["rotation"]["newCount"], 0)
        self.assertEqual(first["games"][0]["quotes"][0]["checkedAt"], T0)

    def test_prior_games_outside_index_and_all_histories_are_retained(self):
        previous = {"games": [old_game(1), old_game(999)]}
        def update(catalog, previous, **kwargs):
            game = old_game(1, T1)
            game["history"] = [{"date": "2026-10-17", "steam": 25, "cdk": None}]
            game["steamHistory"]["points"] = [{"date": "2026-10-17", "price": 25}]
            return {"games": [game]}
        value = refresh(CORE, INDEX, previous, batch_size=0, scheduled_at=T1, collector=update)
        games = {g["appid"]: g for g in value["games"]}
        self.assertEqual(games[999], previous["games"][1])
        self.assertEqual([p["date"] for p in games[1]["history"]], ["2026-01-01", "2026-10-17"])
        self.assertEqual([p["date"] for p in games[1]["steamHistory"]["points"]], ["2026-01-01", "2026-10-17"])
        self.assertEqual(games[1]["steamHistory"]["coverageStart"], "2026-01-01")
        self.assertEqual(games[1]["steamHistory"]["coverageEnd"], "2026-10-17")
        self.assertEqual(games[999]["quotes"][0]["checkedAt"], T0)
        self.assertEqual(value["sources"][0]["totalCount"], 2)
        self.assertEqual(value["sources"][0]["checkedCount"], 1)

    def test_partial_collector_does_not_drop_unreturned_existing_game(self):
        previous = {"games": [old_game(1), old_game(2)]}
        value = refresh(CORE, INDEX, previous, batch_size=2, scheduled_at=T1,
                        collector=lambda *a, **k: {"games": [old_game(1, T1)]})
        self.assertEqual({row["appid"] for row in value["games"]}, {1, 2})
        self.assertEqual(value["games"][1], previous["games"][1])
        self.assertEqual(value["rotation"]["status"], "partial")

    def test_directory_metadata_gets_safe_fallback_without_manual_mappings(self):
        rows = normalize_catalog({"games": [{"appid": 2, "name": "中文名", "aliases": []},
                                             {"appid": 3, "name": "English", "aliases": "bad"},
                                             {"appid": 4, "name": "DLC", "type": "dlc"},
                                             {"appid": True, "name": "bad"},
                                             {"appid": 2, "name": "duplicate"}]})
        self.assertEqual([row["appid"] for row in rows], [2, 3])
        self.assertEqual(rows[0]["nameEn"], "中文名")
        self.assertEqual(rows[1]["aliases"], [])
        self.assertNotIn("sonkwo", rows[0])
        value = refresh(CORE, {"games": rows}, collector=fake_collect, scheduled_at=T1)
        game = next(row for row in value["games"] if row["appid"] == 2)
        self.assertEqual(game["catalogOrigin"], "steam_directory")

    def test_same_day_observed_minimum_is_not_erased(self):
        previous = {"games": [old_game(1)]}
        current = old_game(1, T1)
        current["history"][0]["steam"] = 100
        value = refresh(CORE, {}, previous, batch_size=0, scheduled_at=T1,
                        collector=lambda *a, **k: {"games": [current]})
        self.assertEqual(value["games"][0]["history"][0]["steam"], 10)

    def test_negative_or_boolean_batch_rejected(self):
        for batch in (-1, True, 1.5):
            with self.assertRaises(ValueError):
                refresh(CORE, INDEX, batch_size=batch, collector=fake_collect)

    def test_backfill_reserves_an_oldest_refresh_slot(self):
        previous = {"games": [old_game(1), old_game(2, T1), old_game(3, T0)]}
        value = refresh(CORE, INDEX, previous, batch_size=3, collector=fake_collect, scheduled_at=T2)
        self.assertEqual(value["rotation"]["batchAppIds"], [4, 5, 3])
        following = refresh(CORE, INDEX, value, batch_size=3, collector=fake_collect,
                            scheduled_at="2026-10-17T00:45:00Z")
        self.assertEqual(following["rotation"]["batchAppIds"], [6, 7, 2])

    def test_spare_slots_are_borrowed_between_old_and_new(self):
        core, directory = normalize_catalog(CORE), normalize_catalog(INDEX)
        previous = {"games": [old_game(appid) for appid in range(1, 8)]}
        self.assertEqual([row["appid"] for row in select_batch(core, directory, previous, 3)], [8, 2, 3])
        self.assertEqual([row["appid"] for row in select_batch(core, directory, {}, 3)], [2, 3, 4])
        small_directory = normalize_catalog({"games": INDEX["games"][:3]})
        self.assertEqual([row["appid"] for row in select_batch(core, small_directory,
                         {"games": [old_game(2)]}, 5)], [3, 2])

    def test_one_slot_alternates_new_and_old_even_after_failure(self):
        def fail(*args, **kwargs):
            raise TimeoutError("offline")
        first = refresh(CORE, INDEX, batch_size=1, collector=fail, scheduled_at=T0)
        second = refresh(CORE, INDEX, first, batch_size=1, collector=fail, scheduled_at=T1)
        third = refresh(CORE, INDEX, second, batch_size=1, collector=fail, scheduled_at=T2)
        self.assertEqual([row["rotation"]["batchAppIds"] for row in (first, second, third)], [[2], [2], [3]])
        self.assertEqual([row["rotation"]["lastSingleBatchKind"] for row in (first, second, third)],
                         ["new", "seen", "new"])
        self.assertTrue(all(row["rotation"]["status"] == "error" for row in (first, second, third)))

    def test_requested_input_is_bounded_deduplicated_and_data_only(self):
        self.assertEqual(parse_appids("2, 3\n2\t4"), [2, 3, 4])
        self.assertEqual(parse_appids([2, "3", 2]), [2, 3])
        for value in ("", " ", "0", "-1", "2.0", "True", "１", "2;echo bad", "$(id)",
                      "2`id`", "2/../3", "2\"", True, 2, [True], [2.0], [0], [],
                      ",".join(str(appid) for appid in range(1, 12)), "2" * 257):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_appids(value)

    def test_requested_collects_only_specified_ids_and_retains_other_games(self):
        previous = {"games": [old_game(1), old_game(2), old_game(999)],
                    "rotation": {"lastAttempts": {"999": T0}, "lastSingleBatchKind": "new"}}
        frozen = copy.deepcopy(previous)
        calls = []
        def update(catalog, previous, **kwargs):
            calls.append([row["appid"] for row in catalog])
            game = old_game(2, T1)
            game["history"] = [{"date": "2026-10-17", "steam": 25, "cdk": None}]
            game["steamHistory"]["points"] = [{"date": "2026-10-17", "price": 25}]
            return {"games": [game, old_game(3, T1)]}
        value = refresh(CORE, INDEX, previous, requested_appids="2,2", scheduled_at=T1, collector=update)
        games = {row["appid"]: row for row in value["games"]}
        self.assertEqual(calls, [[2]])
        self.assertEqual(set(games), {1, 2, 999})
        self.assertEqual(games[1], frozen["games"][0])
        self.assertEqual(games[999], frozen["games"][2])
        self.assertEqual([p["date"] for p in games[2]["history"]], ["2026-01-01", "2026-10-17"])
        self.assertEqual([p["date"] for p in games[2]["steamHistory"]["points"]], ["2026-01-01", "2026-10-17"])
        self.assertEqual(value["rotation"]["kind"], "requested")
        self.assertEqual(value["rotation"]["requestedAppIds"], [2])
        self.assertEqual(value["rotation"]["coreCount"], 0)
        self.assertEqual(value["rotation"]["checkedCount"], 1)
        self.assertEqual(value["rotation"]["lastAttempts"]["999"], T0)
        self.assertEqual(value["rotation"]["lastSingleBatchKind"], "new")
        self.assertEqual(previous, frozen)

    def test_requested_core_uses_curated_metadata_and_no_extra_batch(self):
        seen = []
        def update(catalog, previous, **kwargs):
            seen.extend(catalog)
            return fake_collect(catalog, previous, **kwargs)
        value = refresh(CORE, INDEX, requested_appids=[1], scheduled_at=T0, collector=update)
        self.assertEqual([row["appid"] for row in seen], [1])
        self.assertEqual(seen[0]["name"], "核心")
        self.assertEqual(value["rotation"]["coreCount"], 1)
        self.assertEqual(value["rotation"]["batchCount"], 0)

    def test_requested_unknown_id_is_rejected_before_collection(self):
        with patch("scripts.refresh_prices.collect") as collect:
            with self.assertRaisesRegex(ValueError, "outside the synchronized directory"):
                refresh(CORE, INDEX, requested_appids="999")
            collect.assert_not_called()

    def test_requested_failure_keeps_old_snapshot_and_original_dates(self):
        previous = {"games": [old_game(1), old_game(2)]}
        def fail(*args, **kwargs):
            raise TimeoutError("offline")
        value = refresh(CORE, INDEX, previous, requested_appids="2", collector=fail, scheduled_at=T1)
        self.assertEqual(value["games"], previous["games"])
        self.assertEqual(value["rotation"]["status"], "error")
        self.assertEqual(value["rotation"]["lastAttempts"], {"2": T1})

    def test_atomic_write_keeps_old_file_on_serialization_failure(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            path = Path(folder) / "prices.json"
            path.write_text('{"previous":true}', encoding="utf8")
            with self.assertRaises(ValueError):
                atomic_write(path, {"bad": float("nan")})
            self.assertEqual(json.loads(path.read_text(encoding="utf8")), {"previous": True})
            self.assertEqual(list(Path(folder).glob("*.tmp")), [])
            atomic_write(path, {"new": True})
            self.assertEqual(json.loads(path.read_text(encoding="utf8")), {"new": True})

    def test_cli_default_is_three_additional_games(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            directory = Path(folder)
            core, index, output = [directory / name for name in ("core.json", "index.json", "prices.json")]
            core.write_text(json.dumps(CORE), encoding="utf8")
            index.write_text(json.dumps(INDEX), encoding="utf8")
            with patch("scripts.refresh_prices.collect", side_effect=fake_collect):
                self.assertEqual(main(["--catalog", str(core), "--index", str(index), "--output", str(output)]), 0)
            result = json.loads(output.read_text(encoding="utf8"))
            self.assertEqual(result["rotation"]["batchCount"], 3)
            self.assertEqual(result["rotation"]["checkedCount"], 4)

    def test_cli_only_collects_valid_requested_ids(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as folder:
            directory = Path(folder)
            core, index, output = [directory / name for name in ("core.json", "index.json", "prices.json")]
            core.write_text(json.dumps(CORE), encoding="utf8")
            index.write_text(json.dumps(INDEX), encoding="utf8")
            output.write_text(json.dumps({"games": [old_game(1)]}), encoding="utf8")
            args = ["--catalog", str(core), "--index", str(index), "--output", str(output)]
            with patch("scripts.refresh_prices.collect", side_effect=fake_collect) as collect:
                self.assertEqual(main(args + ["--appids", "4, 5 4"]), 0)
            self.assertEqual([row["appid"] for row in collect.call_args.args[0]], [4, 5])
            result = json.loads(output.read_text(encoding="utf8"))
            self.assertEqual({row["appid"] for row in result["games"]}, {1, 4, 5})
            self.assertEqual(result["rotation"]["requestedAppIds"], [4, 5])
            before = output.read_bytes()
            for bad in ("999", "4; echo bad", "true", "", "0"):
                with self.subTest(value=bad), patch("scripts.refresh_prices.collect") as collect, \
                        patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                    main(args + ["--appids", bad])
                self.assertEqual(error.exception.code, 2)
                collect.assert_not_called()
                self.assertEqual(output.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
