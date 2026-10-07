# -*- coding: utf-8 -*-
"""sync_coordinates.py 的測試。GitHub Actions 每次同步前先跑：

    python -m unittest discover -s tools/coordinates -v
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import sync_coordinates as sync

ACTIVITIES = {
    "subcategories": [{"id": "gold-pot", "name": "皮克敏特殊地點金盆"}],
    "coordinates": [
        {"id": "evt-011", "subcategoryId": "gold-pot", "name": "香港海港城", "lat": 22.29482, "lng": 114.16581, "remindDays": 1, "note": "禮物飾品・每天 1 次"},
    ],
}

# 官網明信片卡片的實際結構（2026-10-08），名稱是 HTML 實體
CARD_HTML = """
<div class="pc-card" id="pc-444">
    <div class="pc-img-wrap">
            <img src="/uploads/postcards/894c736bfb654e6bbc1bccfeba6e3595.jpg" alt="&#x9F8D;&#x8C93;&#x7B49;&#x516C;&#x8ECA;" loading="lazy" />
    </div>
    <div class="pc-body">
        <div class="pc-name-row">
            <h6>&#x9F8D;&#x8C93;&#x7B49;&#x516C;&#x8ECA;</h6>
            <span class="pc-type-badge pc-type-flower">&#x1F338; &#x82B1;</span>
        </div>
            <p class="pc-desc">&#x8AB0;&#x80FD;&#x62D2;&#x7D55;</p>
        <div class="pc-meta">
                <span class="pc-country">🌏 &#x82F1;&#x56FD;;&#x82F1;&#x570B;</span>
            <span class="coord-chip" title="點擊複製座標"
                  data-lat="3.382400" data-lon="101.774482">
                📍 3.382400, 101.774482 📋
            </span>
        </div>
        <div class="pc-date-row">
            <span>📅 2026/09/05</span>
            <span>👤 JDHG</span>
        </div>
    </div>
</div>
"""


def pure(spot_id, spot_type="公園", name=None, lat=25.0, lon=121.5, **extra):
    spot = {"Id": spot_id, "Name": name or f"純點{spot_id}", "Type": spot_type, "Lat": lat, "Lon": lon,
            "City": "台北市", "District": "大安區", "Icon": "🌳", "UpdateDate": "2026-10-01T10:00:00"}
    spot.update(extra)
    return spot


def card(card_id, card_type="花", name=None, **extra):
    postcard = {"Id": card_id, "Name": name or f"明信片{card_id}", "Type": card_type, "Country": "日本",
                "Lat": 35.6, "Lon": 139.7, "Desc": "說明", "Thumb": f"https://pikmin.talllkai.com/uploads/postcards/{card_id}.jpg",
                "Date": "2026-10-06"}
    postcard.update(extra)
    return postcard


def build(purespots, postcards, previous=None, revision=2):
    library, notes = sync.build_library(purespots, postcards, ACTIVITIES, previous, revision, "2026-10-08")
    sync.validate_types(library)
    return library, notes


class ParsePostcardPageTest(unittest.TestCase):
    def test_reads_every_field_of_a_card(self):
        cards = sync.parse_postcard_page("<html>共 539 張" + CARD_HTML + CARD_HTML.replace("pc-444", "pc-445"))
        self.assertEqual([444, 445], [c["Id"] for c in cards])
        self.assertEqual(
            {
                "Id": 444,
                "Name": "龍貓等公車",
                "Type": "花",
                "Country": "英國",
                "Lat": 3.3824,
                "Lon": 101.774482,
                "Desc": "誰能拒絕",
                "Thumb": "https://pikmin.talllkai.com/uploads/postcards/894c736bfb654e6bbc1bccfeba6e3595.jpg",
                "Date": "2026-09-05",
            },
            cards[0],
        )
        self.assertEqual(539, sync.postcard_total("<p>共 539 張</p>"))
        self.assertIsNone(sync.postcard_total("<p>沒有總數</p>"))


class BuildLibraryTest(unittest.TestCase):
    def test_same_source_data_is_not_a_new_revision(self):
        first, _ = build([pure(1), pure(2)], [card(10)])
        second, _ = build([pure(1), pure(2)], [card(10)], previous=first, revision=3)
        self.assertEqual(sync.content(first), sync.content(second))

    def test_a_new_postcard_is_a_change(self):
        first, _ = build([pure(1)], [card(10)])
        second, _ = build([pure(1)], [card(10), card(11, "隱藏")], previous=first, revision=3)
        self.assertNotEqual(sync.content(first), sync.content(second))
        lines, names = sync.describe_changes(first, second)
        self.assertIn("明信片 2 筆：新增 1、移除 0、內容改變 0", lines)
        self.assertEqual(["＋ 明信片11（pc-11）"], names)

    def test_pure_subcategory_order_follows_the_previous_revision(self):
        previous, _ = build([pure(1, "郵局"), pure(2, "公園")], [])
        # 官網把最前面那一筆「郵局」刪掉之後，第一次出現的順序變成 公園、郵局
        library, notes = build([pure(2, "公園"), pure(3, "郵局"), pure(4, "車站")], [], previous=previous)
        pure_category = library["categories"][1]
        self.assertEqual(["郵局", "公園", "車站"], [sub["id"] for sub in pure_category["subcategories"]])
        self.assertEqual(["純點出現新的種類：車站"], notes)

    def test_one_bad_record_does_not_stop_the_update(self):
        library, notes = build(
            [
                pure(1, name="  前後有空白  "),
                pure(2, name="長" * 90),
                pure(3, lat="not a number"),
                pure(4, lat=True),
                pure(5, lon=181),
                pure(6, name="   "),
                pure(1, name="重複的 id"),
            ],
            [card(10, Thumb="http://not-https.example/a.jpg"), card(11, "特別")],
        )
        by_id = {c["id"]: c for c in library["coordinates"]}
        self.assertEqual(["evt-011", "pure-1", "pure-2", "pc-10", "pc-11"], list(by_id))
        self.assertEqual("前後有空白", by_id["pure-1"]["name"])
        self.assertEqual(80, len(by_id["pure-2"]["name"]))
        self.assertIsNone(by_id["pc-10"]["thumbnail"])
        self.assertIsNone(by_id["pc-11"]["subcategoryId"])
        self.assertEqual("日本；說明", by_id["pc-10"]["note"])
        self.assertEqual("台北市·大安區", by_id["pure-1"]["note"])
        self.assertIn("純點有 5 筆缺 id、名稱或有效座標（或 id 重複），已略過", notes)
        self.assertIn("明信片出現不認得的種類，先不放進子分類：特別", notes)

    def test_a_mistake_in_activities_stops_the_sync(self):
        activities = json.loads(json.dumps(ACTIVITIES))
        activities["coordinates"][0]["subcategoryId"] = "no-such-event"
        with self.assertRaises(sync.SyncError):
            sync.build_library([pure(1)], [], activities, None, 2, "2026-10-08")
        activities["coordinates"][0]["subcategoryId"] = "gold-pot"
        activities["coordinates"][0]["name"] = "名稱後面多了空白 "
        library, _ = sync.build_library([pure(1)], [], activities, None, 2, "2026-10-08")
        with self.assertRaises(sync.SyncError):
            sync.validate_types(library)


class GuardTest(unittest.TestCase):
    def test_refuses_to_publish_when_a_category_shrinks_too_much(self):
        previous, _ = build([pure(i) for i in range(100)], [card(i) for i in range(100)])
        library, _ = build([pure(i) for i in range(89)], [card(i) for i in range(95)], previous=previous)
        with self.assertRaises(sync.SyncError) as raised:
            sync.check_shrink(previous, library, allow_shrink=False)
        self.assertIn("純點從 100 筆變成 89 筆", str(raised.exception))
        with redirect_stdout(io.StringIO()):
            sync.check_shrink(previous, library, allow_shrink=True)
        smaller, _ = build([pure(i) for i in range(90)], [card(i) for i in range(90)], previous=previous)
        sync.check_shrink(previous, smaller, allow_shrink=False)

    def test_switches_to_one_line_before_the_app_limit(self):
        library, _ = build([pure(i) for i in range(20)], [])
        pretty, compact = sync.serialize(library)
        self.assertFalse(compact)
        self.assertTrue(pretty.startswith(b'{\n "schemaVersion": 1,\n "revision": 2,'))
        self.assertNotIn(b"\r", pretty)
        with mock.patch.object(sync, "PRETTY_LIMIT_BYTES", len(pretty) - 1), redirect_stdout(io.StringIO()):
            one_line, compact = sync.serialize(library)
        self.assertTrue(compact)
        self.assertTrue(one_line.startswith(b'{"schemaVersion":1,"revision":2,'))
        self.assertEqual(json.loads(pretty), json.loads(one_line))
        with mock.patch.object(sync, "PRETTY_LIMIT_BYTES", 0), mock.patch.object(sync, "MAX_LIBRARY_BYTES", len(one_line) - 1):
            with self.assertRaises(sync.SyncError):
                sync.serialize(library)


class SyncCommandTest(unittest.TestCase):
    def setUp(self):
        # 在 GitHub Actions 裡跑測試時，不要把測試的結果寫進那一步的 output 與 summary
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in ("GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            os.environ.pop(key, None)

    def test_bumps_the_revision_only_when_the_content_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library_path = root / "coordinates.json"
            activities_path = root / "activities.json"
            raw = root / "raw"
            raw.mkdir()
            activities_path.write_text(json.dumps(ACTIVITIES, ensure_ascii=False), encoding="utf-8")
            previous, _ = build([pure(1)], [card(10)], revision=5)
            library_path.write_bytes(sync.serialize(previous)[0])

            def run(purespots, postcards, *extra):
                (raw / "purespots.json").write_text(json.dumps(purespots, ensure_ascii=False), encoding="utf-8")
                (raw / "postcards.json").write_text(json.dumps(postcards, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(sync, "LIBRARY_PATH", library_path), \
                        mock.patch.object(sync, "ACTIVITIES_PATH", activities_path), \
                        redirect_stdout(io.StringIO()):
                    code = sync.main(["--raw-dir", str(raw), *extra])
                return code, json.loads(library_path.read_text(encoding="utf-8"))

            code, library = run([pure(1)], [card(10)])
            self.assertEqual((0, 5), (code, library["revision"]))
            code, library = run([pure(1)], [card(10), card(11)], "--dry-run")
            self.assertEqual((0, 5), (code, library["revision"]))
            code, library = run([pure(1)], [card(10), card(11)])
            self.assertEqual((0, 6), (code, library["revision"]))
            self.assertEqual(["evt-011", "pure-1", "pc-10", "pc-11"], [c["id"] for c in library["coordinates"]])
            code, library = run([pure(1)], [card(10), card(11)])
            self.assertEqual((0, 6), (code, library["revision"]))
            code, library = run([], [card(10), card(11)])
            self.assertEqual((1, 6), (code, library["revision"]), "純點變成 0 筆是官網出問題，不發布")


if __name__ == "__main__":
    unittest.main()
