"""Directory coverage, cursor, retention and credential handling checks."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import sync_catalog as catalog
from scripts.catalog_format import unpack_games

NOW = "2026-10-17T12:00:00Z"


def search_page(ids, total, name="游戏"):
    html = "".join(f'<a class="search_result_row" data-ds-appid="{appid}" href="https://store.steampowered.com/app/{appid}/"><div><span class="title">{name}{appid}</span></div></a>' for appid in ids)
    return {"success": 1, "total_count": total, "results_html": html}


def api_page(ids, more=False):
    return {"response": {"apps": [{"appid": appid, "name": f"Game {appid}", "last_modified": 1700000000} for appid in ids],
                         "have_more_results": more, "last_appid": ids[-1] if ids else 0}}


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def get_json(self, url, params):
        self.calls.append((url, copy.deepcopy(params)))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class CatalogSyncTests(unittest.TestCase):
    def test_parser_accepts_exact_single_app_and_rejects_bundle_mismatch_and_boolean(self):
        html = ('<a data-ds-appid="10" href="https://store.steampowered.com/app/10/"><span class="title">十</span></a>'
                '<a data-ds-appid="11,12" href="https://store.steampowered.com/app/11/"><span class="title">多</span></a>'
                '<a data-ds-appid="13" href="https://store.steampowered.com/bundle/13/"><span class="title">包</span></a>'
                '<a data-ds-appid="14" href="https://store.steampowered.com/app/15/"><span class="title">错</span></a>'
                '<a data-ds-appid="16" href="https://elsewhere.example/app/16/"><span class="title">外</span></a>')
        games, _, _ = catalog.parse_search_page({"success": 1, "total_count": 5, "results_html": html})
        self.assertEqual([g["appid"] for g in games], [10])
        self.assertIsNone(catalog.normalize_game({"appid": True, "name": "not an ID"}))

    def test_parser_nested_title_and_entities_excludes_price(self):
        parser = catalog.SearchParser()
        parser.feed('<a data-ds-appid="10" href="/app/10/"><img src="x"><span class="title">中文 <b>游戏</b> &amp; 朋友</span><span>¥100</span></a>')
        self.assertEqual(parser.games[0]["name"], "中文 游戏 & 朋友")

    def test_merge_local_chinese_and_aliases_win_without_losing_old_games(self):
        merged = catalog.merge_games([{"appid": 10, "name": "旧中文", "aliases": ["旧别名"]}, {"appid": 20, "name": "Keep"}],
            [{"appid": 10, "name": "English API Name", "aliases": ["API Alias"]}],
            [{"appid": 10, "name": "人工中文", "nameEn": "Curated English", "aliases": ["小名"]}])
        self.assertEqual(merged[0]["name"], "人工中文")
        self.assertEqual(merged[0]["nameEn"], "Curated English")
        self.assertEqual(merged[0]["aliases"], ["旧别名", "API Alias", "小名"])
        self.assertEqual(len(merged), 2)

    def test_search_partial_cursor_and_maximum_page_size_do_not_claim_full_library(self):
        client = FakeClient(search_page([10, 20], 1000))
        result = catalog.synchronize(client=client, pages=1, page_size=500, now=NOW, delay=0)
        self.assertEqual(client.calls[0][1]["count"], 100)
        self.assertEqual(result["syncState"]["nextStart"], 100)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertIn("不含受限", result["scope"])

    def test_search_continues_at_saved_offset_and_only_completes_contiguous_coverage(self):
        first = catalog.synchronize(client=FakeClient(search_page([10, 20], 3)), pages=1, page_size=2, now=NOW, delay=0)
        client = FakeClient(search_page([30], 3))
        result = catalog.synchronize(first, client=client, pages=1, page_size=2, now=NOW, delay=0)
        self.assertEqual(client.calls[0][1]["start"], 2)
        self.assertEqual(result["gameCount"], 3)
        self.assertTrue(result["complete"])
        self.assertEqual(result["syncState"]["nextStart"], 0)
        first["syncState"]["contiguousFromStart"] = False
        self.assertFalse(catalog.synchronize(first, client=FakeClient(search_page([30], 3)), pages=1, page_size=2, now=NOW, delay=0)["complete"])

    def test_catalog_is_written_as_compact_v2_rows(self):
        result = catalog.synchronize(client=FakeClient(search_page([10], 1)), now=NOW, delay=0)
        self.assertEqual(result["schemaVersion"], 2)
        self.assertEqual(result["games"], [[10, "游戏10"]])

    def test_search_failure_after_success_retains_rows_and_retries_failed_page_next_time(self):
        previous = {"games": [{"appid": 1, "name": "Already here"}]}
        client = FakeClient(search_page([10, 20], 100), RuntimeError("offline"), RuntimeError("offline"), RuntimeError("offline"))
        result = catalog.synchronize(previous, client=client, pages=2, page_size=2, now=NOW, delay=0)
        self.assertEqual(result["syncState"]["nextStart"], 2)
        self.assertEqual([g["appid"] for g in unpack_games(result)], [1, 10, 20])
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(len(client.calls), 4)

    def test_search_total_failure_preserves_original_timestamp_and_cursor(self):
        previous = catalog.synchronize(client=FakeClient(search_page([10], 100)), pages=1, now=NOW, delay=0)
        result = catalog.synchronize(previous, client=FakeClient(*[RuntimeError("fail")] * 3), now="2026-10-18T12:00:00Z", delay=0)
        self.assertEqual(result["generatedAt"], NOW)
        self.assertEqual(result["syncState"], previous["syncState"])
        self.assertEqual(result["games"], previous["games"])
        self.assertEqual(result["status"], "stale")

    def test_empty_html_before_reported_end_is_failure_not_complete(self):
        result = catalog.synchronize(client=FakeClient(search_page([], 100)), now=NOW, delay=0)
        self.assertEqual(result["status"], "error")
        self.assertFalse(result["complete"])
        self.assertEqual(result["syncState"]["nextStart"], 0)

    def test_repeated_page_stops_without_claiming_full_coverage_or_skipping_cursor(self):
        result = catalog.synchronize(client=FakeClient(search_page([10, 20], 4),
                                    search_page([10, 20], 4)),
                                     pages=2, page_size=2, now=NOW, delay=0)
        self.assertEqual(result["gameCount"], 2)
        self.assertEqual(result["syncState"]["nextStart"], 2)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])

    def test_official_api_uses_last_appid_and_game_only_flags(self):
        client = FakeClient(api_page([10, 20], True), api_page([30], False))
        result = catalog.synchronize(client=client, key="private-test-key", pages=2, api_page_size=2, now=NOW, delay=0)
        params = [json.loads(call[1]["input_json"]) for call in client.calls]
        self.assertEqual([p["last_appid"] for p in params], [0, 20])
        self.assertTrue(params[0]["include_games"])
        self.assertFalse(params[0]["include_dlc"])
        self.assertFalse(params[0]["include_software"])
        self.assertEqual(result["gameCount"], 3)
        self.assertTrue(result["complete"])
        self.assertNotIn("private-test-key", json.dumps(result))

    def test_official_empty_incremental_update_retains_games_and_uses_watermark(self):
        old = catalog.synchronize(client=FakeClient(api_page([10], False)), key="key", now=NOW, delay=0)
        client = FakeClient(api_page([], False))
        result = catalog.synchronize(old, client=client, key="key", now="2026-10-18T12:00:00Z", delay=0)
        self.assertEqual(result["games"], old["games"])
        self.assertTrue(result["complete"])
        self.assertEqual(json.loads(client.calls[0][1]["input_json"])["if_modified_since"], old["syncState"]["lastModifiedSince"])

    def test_official_partial_baseline_resumes_instead_of_pretending_incremental(self):
        old = catalog.synchronize(client=FakeClient(api_page([10], True)), key="key", pages=1, now=NOW, delay=0)
        self.assertFalse(old["complete"])
        client = FakeClient(api_page([20], False))
        result = catalog.synchronize(old, client=client, key="key", pages=1, now="2026-10-18T12:00:00Z", delay=0)
        params = json.loads(client.calls[0][1]["input_json"])
        self.assertEqual(params["last_appid"], 10)
        self.assertNotIn("if_modified_since", params)
        self.assertTrue(result["complete"])

    def test_cli_initial_failure_returns_one_and_never_logs_or_saves_key(self):
        secret = "TOP-SECRET-DO-NOT-PUBLISH"
        client = FakeClient(*[RuntimeError("https://api.example/?key=" + secret)] * 3)
        output = io.StringIO()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as temporary:
            target = Path(temporary) / "catalog-index.json"
            curated = Path(temporary) / "curated.json"
            curated.write_text('{"games":[]}', encoding="utf-8")
            with patch.dict("os.environ", {"STEAM_WEB_API_KEY": secret}), patch.object(catalog, "CatalogClient", return_value=client), contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                code = catalog.main(["--output", str(target), "--catalog", str(curated), "--delay", "0"])
            self.assertEqual(code, 1)
            self.assertNotIn(secret, output.getvalue())
            self.assertNotIn(secret, target.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
