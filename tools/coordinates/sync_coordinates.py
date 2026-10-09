#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""從 pikmin.talllkai.com 重新產生座標圖鑑 coordinates/coordinates.json。

GitHub Actions（.github/workflows/sync-coordinates.yml）每 6 小時跑一次，也可以在本機手動跑。

來源：
    明信片  https://pikmin.talllkai.com/Postcard（伺服器端渲染的卡片列表，每頁 20 張，逐頁解析）
    純點    https://pikmin.talllkai.com/PureSpot/Map（全部景點以 gzip＋base64 內嵌在 HTML）
    活動    tools/coordinates/activities.json（作者手動維護）

只有分類或座標的內容和現在的 coordinates/coordinates.json 不同時，才把 revision +1 並寫檔。
兩個 App 只接受比手上更大的 revision，所以 coordinates/coordinates.json 是 revision 唯一的基準：
不要在別的 repo 或目錄另外產生座標庫再複製過來，revision 會比線上版小，已安裝的 App 會默默忽略。

用法：
    python tools/coordinates/sync_coordinates.py                 抓官網，有變才更新 coordinates/coordinates.json
    python tools/coordinates/sync_coordinates.py --dry-run       只顯示會怎麼變，不寫檔
    python tools/coordinates/sync_coordinates.py --raw-dir DIR   改用 DIR 裡的 postcards.json、purespots.json，不連網
    python tools/coordinates/sync_coordinates.py --save-raw DIR  另外存一份抓到的原始資料
    python tools/coordinates/sync_coordinates.py --wait-pages N  等 GitHub Pages 回傳 revision N 以上

只讀取該站公開展示的資料，請求之間間隔 1 秒；App 內標示資料來源：皮克敏純點明信片地圖 pikmin.talllkai.com
"""
from __future__ import annotations

import argparse
import base64
import gzip
import html
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOOL_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOL_DIR.parent.parent
LIBRARY_PATH = REPO_ROOT / "coordinates" / "coordinates.json"
ACTIVITIES_PATH = TOOL_DIR / "activities.json"
PAGES_URL = "https://michaelcheung0125-svg.github.io/GFlyer-updates/coordinates/coordinates.json"

SITE = "https://pikmin.talllkai.com"
POSTCARD_PAGE_URL = SITE + "/Postcard?keyword=&country=&sort=date_desc&page={page}"
PURESPOT_MAP_URL = SITE + "/PureSpot/Map"
USER_AGENT = "GFlyer-coordinate-sync/1.0 (+https://github.com/michaelcheung0125-svg/GFlyer-updates)"
REQUEST_DELAY_SECONDS = 1.0
POSTCARDS_PER_PAGE = 20
POSTCARD_SCRAPE_ATTEMPTS = 3
POSTCARD_TYPES = {"flower": "花", "mushroom": "菇", "hidden": "隱藏"}

SOURCE_NOTE = "皮克敏純點明信片地圖 pikmin.talllkai.com"
# runner 是 UTC；updatedAt 用香港時間的日期（香港時間 02:17 那一次在 UTC 還是前一天）
LOCAL_TIMEZONE = timezone(timedelta(hours=8))
# 活動座標的 updatedAt 一律是這個日期（沿用 GFlyer 原本的 tools/build_coordinate_library.py）
EVENT_UPDATED_AT = "2026-08-24"

# App 載入時的上限（GFlyer-Suite contracts/coordinate-library.schema.json）。
# Python 的 str 以 Unicode code point 為單位，和 App 的上限是同一個單位。
MAX_NAME_CODE_POINTS = 80
MAX_NOTE_CODE_POINTS = 300
MAX_ICON_CODE_POINTS = 8
MAX_THUMBNAIL_LENGTH = 400
# 兩個 App 下載座標庫的上限（Android CoordinateLibraryRepository.MaxLibraryBytes、
# iOS CoordinateLibraryRepository.maxLibraryBytes），算的是解壓縮後的大小；超過的話 App 會拒收，不會告訴使用者
MAX_LIBRARY_BYTES = 4 * 1024 * 1024
# 縮排版（git diff 看得懂）超過這個大小就改寫成單行，大約小兩成
PRETTY_LIMIT_BYTES = int(3.5 * 1024 * 1024)
# 純點或明信片比線上版少超過這個比例，就當成官網出問題或改版，不發布
MAX_SHRINK_RATIO = 0.10
SHRINK_CHECK_MIN_COUNT = 50
CATEGORY_LABELS = {"event": "活動", "pure": "純點", "postcard": "明信片"}


class SyncError(Exception):
    """這次不能發布。線上版維持原樣，GitHub Actions 的這次執行會失敗並寄通知。"""


# ── GitHub Actions ──


def in_github_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def warn(message: str) -> None:
    print(f"::warning::{message}" if in_github_actions() else f"警告：{message}")


def set_outputs(**values) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def append_summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(markdown + "\n")


# ── 抓官網 ──


def fetch(url: str, attempts: int = 3) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except (OSError, http.client.HTTPException, UnicodeDecodeError) as error:
            if attempt == attempts:
                raise SyncError(f"讀不到 {url}：{error}") from error
            time.sleep(15 * attempt)
    raise AssertionError("unreachable")


CARD_SPLIT = '<div class="pc-card"'


def _first(pattern: str, block: str) -> str:
    match = re.search(pattern, block, re.S)
    return html.unescape(match.group(1)).strip() if match else ""


def _to_float(text: str) -> float | None:
    try:
        return float(text) if text else None
    except ValueError:
        return None


def parse_postcard_page(page_html: str) -> list[dict]:
    """解析一頁明信片卡片。欄位名稱沿用 GFlyer 原本的 docs/purespot_data/postcards.json。"""
    cards = []
    for block in page_html.split(CARD_SPLIT)[1:]:
        card_id = _first(r'id="pc-(\d+)"', block)
        if not card_id:
            continue
        thumb = _first(r'<img src="([^"]*uploads/postcards[^"]*)"', block)
        country = _first(r'<span class="pc-country">🌏([^<]*)</span>', block)
        # 國家常寫成「英国;英國」簡繁並列，取最後一段
        countries = [part.strip() for part in country.split(";") if part.strip()]
        cards.append(
            {
                "Id": int(card_id),
                "Name": _first(r'alt="([^"]*)"', block) or _first(r"<h6>([^<]*)</h6>", block),
                "Type": POSTCARD_TYPES.get(_first(r"pc-type-badge pc-type-(flower|mushroom|hidden)", block), ""),
                "Country": countries[-1] if countries else "",
                "Lat": _to_float(_first(r'data-lat="([-\d.]+)"', block)),
                "Lon": _to_float(_first(r'data-lon="([-\d.]+)"', block)),
                "Desc": _first(r'<p class="pc-desc">(.*?)</p>', block),
                "Thumb": (thumb if thumb.startswith("https://") else SITE + thumb) if thumb else "",
                "Date": _first(r"📅\s*([\d/]+)", block).replace("/", "-"),
            }
        )
    return cards


def postcard_total(page_html: str) -> int | None:
    match = re.search(r"共\s*(\d+)\s*張", page_html)
    return int(match.group(1)) if match else None


def _dedupe(items: list[dict], key: str) -> list[dict]:
    seen = set()
    unique = []
    for item in items:
        if item[key] not in seen:
            seen.add(item[key])
            unique.append(item)
    return unique


def fetch_postcards() -> list[dict]:
    """抓全部明信片。翻頁途中官網新增或下架會讓分頁錯位、漏掉幾張，所以張數不足時整份重抓。"""
    best: list[dict] = []
    total = 0
    for attempt in range(1, POSTCARD_SCRAPE_ATTEMPTS + 1):
        if attempt > 1:
            time.sleep(30)
        first = fetch(POSTCARD_PAGE_URL.format(page=1))
        total = postcard_total(first)
        if total is None:
            raise SyncError("明信片頁面找不到「共 N 張」，官網可能改版了")
        pages = max(1, math.ceil(total / POSTCARDS_PER_PAGE))
        cards = parse_postcard_page(first)
        for page in range(2, pages + 1):
            time.sleep(REQUEST_DELAY_SECONDS)
            cards.extend(parse_postcard_page(fetch(POSTCARD_PAGE_URL.format(page=page))))
        unique = _dedupe(cards, "Id")
        print(f"明信片：官網共 {total} 張，{pages} 頁抓到 {len(unique)} 張")
        if len(unique) >= total:
            return unique
        if len(unique) > len(best):
            best = unique
    warn(f"明信片官網寫共 {total} 張，抓了 {POSTCARD_SCRAPE_ATTEMPTS} 次都只拿到 {len(best)} 張")
    return best


def fetch_purespots() -> list[dict]:
    page = fetch(PURESPOT_MAP_URL)
    match = re.search(r'atob\("([A-Za-z0-9+/=\\u]+)"\)', page)
    if not match:
        raise SyncError("純點地圖頁找不到內嵌的 gzip/base64 資料，官網可能改版了")
    # HTML 內的 JS 字串會把部分字元寫成 \uXXXX 跳脫（如 \u002B = +）
    encoded = match.group(1)
    for escape, char in (("\\u002B", "+"), ("\\u002F", "/"), ("\\u003D", "=")):
        encoded = encoded.replace(escape, char)
    try:
        data = json.loads(gzip.decompress(base64.b64decode(encoded)).decode("utf-8"))
    except (ValueError, OSError, EOFError) as error:
        raise SyncError(f"純點資料解不開：{error}") from error
    if isinstance(data, dict):
        data = data.get("spots") or data.get("data")
    if not isinstance(data, list) or not data:
        raise SyncError("純點資料不是預期的清單格式，官網可能改版了")
    print(f"純點：抓到 {len(data)} 筆")
    return data


# ── 產生座標庫 ──


def _text(value) -> str:
    """App 讀字串欄位時會去掉前後空白；不是字串就當成空字串（數字不會被轉成字串）。"""
    return value.strip() if isinstance(value, str) else ""


def _clip(text: str, limit: int) -> str:
    """App 載入時截斷到 limit 個 code point；截斷後再去一次空白，寫出去的就是 App 看到的。"""
    return text[:limit].strip()


def _degrees(value, limit: float) -> float | None:
    """原始資料的經緯度可能是數字或字串；布林、非有限數字或超出範圍都不收。"""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not -limit <= number <= limit:
        return None
    return round(number, 6)


def _thumbnail(value) -> str | None:
    url = _text(value)
    if not url.lower().startswith("https://") or len(url) > MAX_THUMBNAIL_LENGTH:
        return None
    return url


def _source_id(value) -> str:
    """官網的 Id 是整數；布林或空值不算。"""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return ""
    return str(value).strip()


def build_events(activities: dict) -> tuple[dict, list[dict]]:
    """活動座標由作者手寫，寫錯就停下來（GitHub Actions 失敗會寄通知），不默默略過。"""
    subcategories = activities.get("subcategories")
    items = activities.get("coordinates")
    if not isinstance(subcategories, list) or not isinstance(items, list):
        raise SyncError("activities.json 需要 subcategories 與 coordinates 兩個陣列")
    subcategory_by_id: dict[str, dict] = {}
    for sub in subcategories:
        if not isinstance(sub, dict) or not _text(sub.get("id")) or not _text(sub.get("name")):
            raise SyncError(f"activities.json 的子分類缺少 id 或 name：{sub!r}")
        subcategory_by_id[sub["id"]] = sub
    category = {
        "id": "event",
        "name": "活動座標",
        "icon": "🎪",
        "subcategories": [
            {
                "id": sub["id"],
                "name": sub["name"],
                **{
                    key: sub[key]
                    for key in ("startDate", "endDate", "defaultRemindDays", "defaultNote")
                    if key in sub
                },
            }
            for sub in subcategories
        ],
    }
    # id 寫死在 activities.json：以前依排列順序編號，刪掉前面的活動就會讓後面的座標
    # 換到別人的 id 上，使用者的最愛／標記／前往紀錄跟著錯位
    coordinates = []
    for item in items:
        if not isinstance(item, dict) or not _text(item.get("id")):
            raise SyncError(f"活動座標缺少固定 id：{item!r}")
        sub = subcategory_by_id.get(item.get("subcategoryId"))
        if sub is None:
            raise SyncError(f"活動座標 {item['id']} 的 subcategoryId 不在 subcategories 裡：{item.get('subcategoryId')!r}")
        note_parts = [part for part in (item.get("note"), sub.get("defaultNote")) if part]
        period = ""
        if sub.get("startDate") or sub.get("endDate"):
            period = f"{sub.get('startDate', '')} ~ {sub.get('endDate', '')}"
        coordinates.append(
            {
                "id": item["id"],
                "categoryId": "event",
                "subcategoryId": item["subcategoryId"],
                "name": item.get("name"),
                "lat": item.get("lat"),
                "lng": item.get("lng"),
                "note": "；".join(dict.fromkeys(note_parts)),
                "period": period,
                "remindDays": item.get("remindDays", sub.get("defaultRemindDays")),
                "thumbnail": None,
                "enabled": True,
                "updatedAt": EVENT_UPDATED_AT,
            }
        )
    return category, coordinates


def previous_subcategory_order(previous: dict | None, category_id: str) -> list[str]:
    for category in (previous or {}).get("categories") or []:
        if isinstance(category, dict) and category.get("id") == category_id:
            return [
                sub["id"]
                for sub in category.get("subcategories") or []
                if isinstance(sub, dict) and isinstance(sub.get("id"), str)
            ]
    return []


def build_library(
    purespots: list,
    postcards: list,
    activities: dict,
    previous: dict | None,
    revision: int,
    updated_at: str,
) -> tuple[dict, list[str]]:
    """把三份原始資料轉成 App 讀的格式。回傳 (座標庫, 要提醒的事)。

    官網的單筆資料有問題（缺座標、名稱空白）只略過那一筆、照 App 的規則截斷過長的文字，
    不讓一筆壞資料擋住整份更新；整體不合理（筆數大減、檔案過大）由 check_shrink、serialize 擋下。
    """
    notes: list[str] = []
    skipped: Counter = Counter()
    clipped = 0
    event_category, coordinates = build_events(activities)

    # ── 純點座標 ──
    # 子分類沿用線上版的順序，新出現的種類接在後面：照第一次出現的順序排的話，
    # 某個種類最前面那一筆被官網刪掉，整排 chip 的順序就會跟著變
    present_types: list[str] = []
    seen_ids: set[str] = set()
    for spot in purespots:
        spot = spot if isinstance(spot, dict) else {}
        source_id = _source_id(spot.get("Id"))
        name = _text(spot.get("Name"))
        lat, lng = _degrees(spot.get("Lat"), 90), _degrees(spot.get("Lon"), 180)
        if not source_id or not name or lat is None or lng is None or f"pure-{source_id}" in seen_ids:
            skipped["pure"] += 1
            continue
        seen_ids.add(f"pure-{source_id}")
        spot_type = _text(spot.get("Type")) or None
        if spot_type and spot_type not in present_types:
            present_types.append(spot_type)
        location = "·".join(part for part in (_text(spot.get("City")), _text(spot.get("District"))) if part)
        clipped += len(name) > MAX_NAME_CODE_POINTS
        coordinates.append(
            {
                "id": f"pure-{source_id}",
                "categoryId": "pure",
                "subcategoryId": spot_type,
                "name": _clip(name, MAX_NAME_CODE_POINTS),
                "lat": lat,
                "lng": lng,
                "note": _clip(location, MAX_NOTE_CODE_POINTS),
                "period": "",
                "remindDays": None,
                "thumbnail": None,
                "icon": _clip(_text(spot.get("Icon")), MAX_ICON_CODE_POINTS) or None,
                "enabled": True,
                "updatedAt": str(spot.get("UpdateDate") or "")[:10],
            }
        )
    previous_order = previous_subcategory_order(previous, "pure")
    pure_types = [t for t in previous_order if t in present_types]
    pure_types += [t for t in present_types if t not in pure_types]
    if [t for t in present_types if t not in previous_order] and previous_order:
        notes.append("純點出現新的種類：" + "、".join(t for t in present_types if t not in previous_order))

    # ── 明信片座標 ──
    postcard_types = list(POSTCARD_TYPES.values())
    unknown_types: set[str] = set()
    for card in postcards:
        card = card if isinstance(card, dict) else {}
        source_id = _source_id(card.get("Id"))
        name = _text(card.get("Name"))
        lat, lng = _degrees(card.get("Lat"), 90), _degrees(card.get("Lon"), 180)
        if not source_id or not name or lat is None or lng is None or f"pc-{source_id}" in seen_ids:
            skipped["postcard"] += 1
            continue
        seen_ids.add(f"pc-{source_id}")
        card_type = _text(card.get("Type"))
        if card_type not in postcard_types:
            unknown_types.add(card_type or "（空白）")
            card_type = None
        note = "；".join(part for part in (_text(card.get("Country")), _text(card.get("Desc"))) if part)
        clipped += len(name) > MAX_NAME_CODE_POINTS
        clipped += len(note) > MAX_NOTE_CODE_POINTS
        coordinates.append(
            {
                "id": f"pc-{source_id}",
                "categoryId": "postcard",
                "subcategoryId": card_type,
                "name": _clip(name, MAX_NAME_CODE_POINTS),
                "lat": lat,
                "lng": lng,
                "note": _clip(note, MAX_NOTE_CODE_POINTS),
                "period": "",
                "remindDays": None,
                "thumbnail": _thumbnail(card.get("Thumb")),
                "enabled": True,
                "updatedAt": _text(card.get("Date")) or None,
            }
        )
    if unknown_types:
        notes.append("明信片出現不認得的種類，先不放進子分類：" + "、".join(sorted(unknown_types)))
    for category_id, count in skipped.items():
        notes.append(f"{CATEGORY_LABELS[category_id]}有 {count} 筆缺 id、名稱或有效座標（或 id 重複），已略過")
    if clipped:
        notes.append(f"{clipped} 個名稱或說明超過 App 的長度上限，已照 App 的規則截斷")

    library = {
        "schemaVersion": 1,
        "revision": revision,
        "updatedAt": updated_at,
        "source": SOURCE_NOTE,
        "categories": [
            event_category,
            {"id": "pure", "name": "純點座標", "icon": "⚪", "subcategories": [{"id": t, "name": t} for t in pure_types]},
            {
                "id": "postcard",
                "name": "明信片座標",
                "icon": "📮",
                "subcategories": [
                    {"id": "花", "name": "🌸 花"},
                    {"id": "菇", "name": "🍄 菇"},
                    {"id": "隱藏", "name": "🙈 隱藏"},
                ],
            },
        ],
        "coordinates": coordinates,
    }
    return library, notes


# ── 檢查 ──
# App 解析座標庫時型別不對的欄位一律當成沒有這個欄位（GFlyer-Suite
# contracts/fixtures/coordinate-library/type-strictness.*.json），寫錯型別的座標會在 App 裡
# 無聲消失。所以寫出之前先檢查，錯了就停下來，不發布。


def _is_int(value) -> bool:
    return type(value) is int


def _is_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _is_stripped_text(value) -> bool:
    """不是空字串、前後沒有空白的字串。App 會去掉前後空白，寫進檔案的要和 App 讀到的一樣。"""
    return isinstance(value, str) and value != "" and value == value.strip()


def _fail(where: str, field: str, expected: str, value) -> None:
    raise SyncError(f"{where}.{field} 型別或值不對，需要{expected}：{value!r}")


def validate_types(library: dict) -> None:
    """檢查每個欄位的型別與範圍（Python 的 len 就是 Unicode code point 數，和 App 的上限同一個單位）。"""
    if list(library)[:2] != ["schemaVersion", "revision"]:
        # Android 只讀種子開頭的幾百個 bytes 找 revision（CoordinateLibrary.peekRevision）
        raise SyncError("schemaVersion 與 revision 必須是前兩個欄位")
    if not _is_int(library.get("schemaVersion")) or library["schemaVersion"] != 1:
        _fail("頂層", "schemaVersion", "整數 1", library.get("schemaVersion"))
    if not _is_int(library.get("revision")) or library["revision"] < 1:
        _fail("頂層", "revision", " >= 1 的整數", library.get("revision"))
    for field in ("updatedAt", "source"):
        value = library.get(field)
        if not isinstance(value, str) or value == "":
            _fail("頂層", field, "非空字串", value)

    category_ids: set[str] = set()
    for category in library["categories"]:
        where = category.get("id") if isinstance(category.get("id"), str) else "(分類)"
        for field in ("id", "name"):
            if not _is_stripped_text(category.get(field)):
                _fail(where, field, "前後沒有空白的非空字串", category.get(field))
        # App 讀分類的 icon 時原樣保留、不去空白
        icon = category.get("icon")
        if not isinstance(icon, str) or icon != icon.strip():
            _fail(where, "icon", "前後沒有空白的字串", icon)
        subcategories = category.get("subcategories")
        if not isinstance(subcategories, list) or not all(isinstance(sub, dict) for sub in subcategories):
            _fail(where, "subcategories", "物件的陣列", subcategories)
        for sub in subcategories:
            sub_where = f"{where}/{sub.get('id')}"
            for field in ("id", "name"):
                if not _is_stripped_text(sub.get(field)):
                    _fail(sub_where, field, "前後沒有空白的非空字串", sub.get(field))
            for field in ("startDate", "endDate", "defaultNote"):
                if field in sub and not isinstance(sub[field], str):
                    _fail(sub_where, field, "字串", sub[field])
            if "defaultRemindDays" in sub and (
                not _is_int(sub["defaultRemindDays"]) or sub["defaultRemindDays"] < 1
            ):
                _fail(sub_where, "defaultRemindDays", " >= 1 的整數", sub["defaultRemindDays"])
        category_ids.add(category["id"])

    seen_ids: set[str] = set()
    for coordinate in library["coordinates"]:
        where = coordinate.get("id") if isinstance(coordinate.get("id"), str) else "(座標)"
        for field in ("id", "categoryId", "name"):
            if not _is_stripped_text(coordinate.get(field)):
                _fail(where, field, "前後沒有空白的非空字串", coordinate.get(field))
        if coordinate["id"] in seen_ids:
            raise SyncError(f"座標 id 重複：{coordinate['id']}")
        seen_ids.add(coordinate["id"])
        if coordinate["categoryId"] not in category_ids:
            _fail(where, "categoryId", f"已有的分類 {sorted(category_ids)}", coordinate["categoryId"])
        if len(coordinate["name"]) > MAX_NAME_CODE_POINTS:
            _fail(where, "name", f"最多 {MAX_NAME_CODE_POINTS} 個字", coordinate["name"])
        subcategory_id = coordinate.get("subcategoryId")
        if subcategory_id is not None and not _is_stripped_text(subcategory_id):
            _fail(where, "subcategoryId", " null 或前後沒有空白的非空字串", subcategory_id)
        for field, limit in (("lat", 90), ("lng", 180)):
            value = coordinate.get(field)
            if not _is_number(value) or not -limit <= value <= limit:
                _fail(where, field, f" -{limit}〜{limit} 之間的數字（不可以是字串或布林）", value)
        for field in ("note", "period"):
            if not isinstance(coordinate.get(field), str):
                _fail(where, field, "字串", coordinate.get(field))
        if len(coordinate["note"]) > MAX_NOTE_CODE_POINTS:
            _fail(where, "note", f"最多 {MAX_NOTE_CODE_POINTS} 個字", coordinate["note"])
        remind_days = coordinate.get("remindDays")
        if remind_days is not None and (not _is_int(remind_days) or remind_days < 1):
            _fail(where, "remindDays", " null 或 >= 1 的整數", remind_days)
        thumbnail = coordinate.get("thumbnail")
        if thumbnail is not None and (
            not isinstance(thumbnail, str)
            or not thumbnail.lower().startswith("https://")
            or len(thumbnail) > MAX_THUMBNAIL_LENGTH
        ):
            _fail(where, "thumbnail", f" null 或最多 {MAX_THUMBNAIL_LENGTH} 字的 https 網址", thumbnail)
        icon = coordinate.get("icon")
        if icon is not None and (not _is_stripped_text(icon) or len(icon) > MAX_ICON_CODE_POINTS):
            _fail(where, "icon", f" null 或最多 {MAX_ICON_CODE_POINTS} 字的非空字串", icon)
        if type(coordinate.get("enabled")) is not bool:
            _fail(where, "enabled", "布林", coordinate.get("enabled"))
        updated_at = coordinate.get("updatedAt")
        if updated_at is not None and not isinstance(updated_at, str):
            _fail(where, "updatedAt", " null 或字串", updated_at)


def content(library: dict) -> dict:
    """比較新舊時不看 revision 與 updatedAt：只有分類或座標真的變了才發新的 revision。"""
    return {key: value for key, value in library.items() if key not in ("revision", "updatedAt")}


def category_counts(library: dict) -> Counter:
    return Counter(
        coordinate.get("categoryId")
        for coordinate in library.get("coordinates") or []
        if isinstance(coordinate, dict)
    )


def check_shrink(previous: dict, library: dict, allow_shrink: bool) -> None:
    before, after = category_counts(previous), category_counts(library)
    problems = [
        f"{CATEGORY_LABELS[category_id]}從 {before[category_id]} 筆變成 {after[category_id]} 筆"
        for category_id in ("pure", "postcard")
        if (before[category_id] > 0 and after[category_id] == 0)
        or (
            before[category_id] >= SHRINK_CHECK_MIN_COUNT
            and after[category_id] < before[category_id] * (1 - MAX_SHRINK_RATIO)
        )
    ]
    if not problems:
        return
    message = "、".join(problems)
    if allow_shrink:
        warn(f"筆數大減，但指定了 --allow-shrink，照樣發布：{message}")
        return
    raise SyncError(
        f"筆數比線上版少超過 {MAX_SHRINK_RATIO:.0%}（{message}），可能是官網出問題或改版，這次不發布。"
        "確定是官網真的下架的話，手動執行 workflow 並勾選 allow_shrink，或在本機加 --allow-shrink 重跑。"
    )


def serialize(library: dict) -> tuple[bytes, bool]:
    """回傳 (檔案內容, 是否為單行)。固定 LF、結尾不換行，和以前的線上版相同。"""
    pretty = json.dumps(library, ensure_ascii=False, indent=1).encode("utf-8")
    if len(pretty) <= PRETTY_LIMIT_BYTES:
        return pretty, False
    compact = json.dumps(library, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(compact) > MAX_LIBRARY_BYTES:
        raise SyncError(
            f"座標庫有 {len(compact) / 1024 / 1024:.2f} MB，超過兩個 App 的下載上限 4 MB，"
            "App 會拒收。要先發新版 App 提高上限，或拆小資料"
        )
    warn(f"縮排版超過 {PRETTY_LIMIT_BYTES / 1024 / 1024:.1f} MB，改寫成單行（{len(compact) / 1024 / 1024:.2f} MB，上限 4 MB）")
    return compact, True


def describe_changes(previous: dict, library: dict) -> tuple[list[str], list[str]]:
    """回傳 (各分類的變化, 新增與移除的明信片名稱)。"""
    old = {c["id"]: c for c in previous.get("coordinates") or [] if isinstance(c, dict) and isinstance(c.get("id"), str)}
    new = {c["id"]: c for c in library["coordinates"]}
    counts = category_counts(library)
    lines = []
    for category_id, label in CATEGORY_LABELS.items():
        added = sum(1 for i, c in new.items() if c["categoryId"] == category_id and i not in old)
        removed = sum(1 for i, c in old.items() if c.get("categoryId") == category_id and i not in new)
        changed = sum(1 for i, c in new.items() if c["categoryId"] == category_id and i in old and old[i] != c)
        lines.append(f"{label} {counts[category_id]} 筆：新增 {added}、移除 {removed}、內容改變 {changed}")
    if previous.get("categories") != library["categories"]:
        lines.append("分類或子分類有變")
    names = [f"＋ {c['name']}（{c['id']}）" for i, c in new.items() if c["categoryId"] == "postcard" and i not in old]
    names += [f"－ {c.get('name')}（{i}）" for i, c in old.items() if c.get("categoryId") == "postcard" and i not in new]
    return lines, names


# ── 主流程 ──


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SyncError(f"找不到 {path}") from error
    except ValueError as error:
        raise SyncError(f"{path} 不是有效的 JSON：{error}") from error


def load_previous() -> dict:
    previous = load_json(LIBRARY_PATH)
    revision = previous.get("revision") if isinstance(previous, dict) else None
    # bool 是 int 的子類別，要用 type() 才分得開；字串 "42" 也不算
    if type(revision) is not int or revision < 1:
        raise SyncError(f"{LIBRARY_PATH} 的 revision 不對，不知道下一個 revision 要從哪裡接續：{revision!r}")
    return previous


def sync(args: argparse.Namespace) -> int:
    previous = load_previous()
    activities = load_json(ACTIVITIES_PATH)
    if args.raw_dir:
        postcards = load_json(args.raw_dir / "postcards.json")
        purespots = load_json(args.raw_dir / "purespots.json")
    else:
        postcards = fetch_postcards()
        purespots = fetch_purespots()
    if args.save_raw:
        args.save_raw.mkdir(parents=True, exist_ok=True)
        for name, data in (("postcards.json", postcards), ("purespots.json", purespots)):
            (args.save_raw / name).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    today = datetime.now(LOCAL_TIMEZONE).date().isoformat()
    library, notes = build_library(purespots, postcards, activities, previous, previous["revision"] + 1, today)
    validate_types(library)
    for note in notes:
        warn(note)

    if content(library) == content(previous):
        print(f"內容和線上的 revision {previous['revision']} 相同，不用更新")
        set_outputs(changed="false", revision=previous["revision"])
        append_summary(f"座標圖鑑沒有變化，維持 revision {previous['revision']}。")
        return 0

    check_shrink(previous, library, args.allow_shrink)
    data, compact = serialize(library)
    lines, names = describe_changes(previous, library)
    size = f"{len(data) / 1024:.0f} KB" + ("，單行" if compact else "")
    print(f"revision {previous['revision']} → {library['revision']}（{size}）")
    for line in lines:
        print(f"  {line}")
    for name in names[:40]:
        print(f"  {name}")

    if args.dry_run:
        print("--dry-run：不寫檔")
        set_outputs(changed="false", revision=previous["revision"])
        return 0

    LIBRARY_PATH.write_bytes(data)
    print(f"輸出：{LIBRARY_PATH}")
    set_outputs(changed="true", revision=library["revision"])
    append_summary(
        "\n".join(
            [f"### 座標圖鑑 revision {library['revision']}", "", *[f"- {line}" for line in lines]]
            + ([f"- {note}" for note in notes])
            + (["", "明信片：", "", *[f"- {name}" for name in names[:100]]] if names else [])
        )
    )
    if args.commit_message:
        args.commit_message.write_text(
            "\n".join(
                [
                    f"Coordinate library revision {library['revision']} (daily sync)",
                    "",
                    *lines,
                    *notes,
                    "",
                    "Generated by tools/coordinates/sync_coordinates.py from pikmin.talllkai.com.",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


def live_revision() -> int | None:
    """讀 GitHub Pages 上線上版開頭的 revision（App 拿到的就是這一份）。"""
    request = urllib.request.Request(PAGES_URL, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            head = response.read(512).decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException):
        return None
    match = re.search(r'"revision"\s*:\s*(\d+)', head)
    return int(match.group(1)) if match else None


def wait_for_pages(revision: int, timeout: float, interval: float = 20) -> int:
    deadline = time.monotonic() + timeout
    while True:
        live = live_revision()
        if live is not None and live >= revision:
            print(f"GitHub Pages 已經回傳 revision {live}")
            return 0
        if time.monotonic() >= deadline:
            print(f"等了 {timeout:.0f} 秒，GitHub Pages 還是 revision {live}，不是 {revision}")
            return 1
        print(f"GitHub Pages 目前是 revision {live}，{interval:.0f} 秒後再看")
        time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="從 pikmin.talllkai.com 重新產生 coordinates/coordinates.json")
    parser.add_argument("--dry-run", action="store_true", help="只顯示會怎麼變，不寫檔")
    parser.add_argument("--raw-dir", type=Path, help="改用這個目錄裡的 postcards.json、purespots.json，不連網")
    parser.add_argument("--save-raw", type=Path, help="另外存一份抓到的原始資料到這個目錄")
    parser.add_argument("--allow-shrink", action="store_true", help=f"筆數比線上版少超過 {MAX_SHRINK_RATIO:.0%} 也照樣發布")
    parser.add_argument("--commit-message", type=Path, help="有更新時把 commit 訊息寫到這個檔案")
    parser.add_argument("--wait-pages", type=int, metavar="REVISION", help="不產生，只等 GitHub Pages 回傳這個 revision 以上")
    parser.add_argument("--timeout", type=float, default=600, help="--wait-pages 最多等幾秒（預設 600）")
    args = parser.parse_args(argv)
    try:
        if args.wait_pages is not None:
            return wait_for_pages(args.wait_pages, args.timeout)
        return sync(args)
    except SyncError as error:
        print(f"::error::{error}" if in_github_actions() else f"錯誤：{error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
