#!/usr/bin/env python3
"""
クリニクス 直来待ち時間スクレイパー
直来患者の診察待ち人数を取得し、待ち時間を推定します。
完了患者の履歴から物療比率(PT比率)を学習し、推定精度を向上させます。
"""

import asyncio
import json
import os
import re
import sys
from datetime import datetime, time as dt_time, timezone, timedelta
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

load_dotenv()

# ─── 設定 ────────────────────────────────────────────────────────
CLINICS_URL  = "https://karte.medley.life/d"
EMAIL        = os.getenv("CLINICS_EMAIL", "")
PASSWORD     = os.getenv("CLINICS_PASSWORD", "")
MINUTES_PER  = int(os.getenv("MINUTES_PER_PATIENT", "15"))
SESSION_FILE = Path("session.json")
OUTPUT_FILE  = Path("data/status.json")
HISTORY_FILE = Path("data/history.json")
OPEN_HOUR    = int(os.getenv("OPEN_HOUR",  "8"))
CLOSE_HOUR   = int(os.getenv("CLOSE_HOUR", "18"))
JST          = timezone(timedelta(hours=9))

MIN_DURATION    = int(os.getenv("MIN_DURATION_MINUTES", "20"))  # 物療のみ患者の判定閾値（分）
MAX_DURATION    = 180    # エラーデータ除外閾値（分）
HISTORY_DAYS    = 90     # 履歴保持日数
MIN_SAMPLES     = 5      # フォールバック閾値（件未満は固定値を使用）
# 予約枠時間（診療メニューごと）
# 予約枠は再診10分/初診15分だが、実際の医師診察時間は枠より短く
# 残り時間が直来の隙間になる。実績に基づき短縮値を採用。
MINUTES_RESIN         = int(os.getenv("MINUTES_RESIN",         "6"))   # 再診外来の実診察時間
MINUTES_SHINSIN       = int(os.getenv("MINUTES_SHINSIN",       "10"))  # 初診外来の実診察時間
MINUTES_IN_EXAM_REMAIN = int(os.getenv("MINUTES_IN_EXAM_REMAIN", "7"))  # 診察中患者の残り時間見積もり
# ────────────────────────────────────────────────────────────────
# 受付時間
#   月火水金・土　午前  8:30〜11:30
#              　午後 14:30〜17:30（土曜は〜16:30）
#   木・日　休診

AM_START   = dt_time(8,  30)
AM_END     = dt_time(11, 30)
PM_START   = dt_time(14, 30)
PM_END     = dt_time(17, 30)
PM_END_SAT = dt_time(16, 30)


def is_open() -> bool:
    now = datetime.now(JST)
    wd  = now.weekday()   # 0=月 … 3=木(休) … 5=土 … 6=日(休)
    t   = now.time()
    if wd in (3, 6):
        return False
    pm_end = PM_END_SAT if wd == 5 else PM_END
    return (AM_START <= t < AM_END) or (PM_START <= t < pm_end)


def is_exam_window() -> bool:
    """
    診察中の患者が残っている可能性がある時間帯（受付時間 + 終了後30分）。
    scraper.main() はこの範囲で実行される。is_open() は受付時間のみ True。
    """
    now = datetime.now(JST)
    wd  = now.weekday()
    t   = now.time()
    if wd in (3, 6):
        return False
    # 午前 8:30〜12:00（受付11:30 + 延長30分）
    if AM_START <= t < dt_time(12, 0):
        return True
    # 午後 14:30〜 17:00(土曜) / 18:00(平日)（受付 + 延長30分）
    pm_end_ext = dt_time(17, 0) if wd == 5 else dt_time(18, 0)
    if PM_START <= t < pm_end_ext:
        return True
    return False


def get_current_session_end_min() -> int:
    """
    現在のセッション（午前/午後）の受付終了時刻を「0時からの分数」で返す。
    受付時間外の場合は None 相当として -1 を返す。
    """
    now = datetime.now(JST)
    wd  = now.weekday()
    t   = now.time()
    if AM_START <= t < AM_END:
        return AM_END.hour * 60 + AM_END.minute  # 11:30 → 690
    pm_end = PM_END_SAT if wd == 5 else PM_END
    if PM_START <= t < pm_end:
        return pm_end.hour * 60 + pm_end.minute  # 17:30 or 16:30
    return -1


# ─── 時刻パーサー ─────────────────────────────────────────────────

def parse_hhmm(s: str):
    """
    'HH:MM' / 'HH:MM:SS' / 改行混じり などを受け取り、
    その時刻を「午前0時からの分数」で返す。パース失敗は None。
    """
    # 改行・スペースをすべて除去してから処理
    s = "".join(s.split()).strip()
    if not s:
        return None
    # strptime で試す
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.strptime(s[:8], fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            pass
    # "8:26" など先頭ゼロなしの場合
    if ":" in s:
        parts = s.split(":")
        try:
            h = int(parts[0])
            m = int(parts[1][:2])
            if 0 <= h <= 23 and 0 <= m <= 59:
                return h * 60 + m
        except (ValueError, IndexError):
            pass
    return None


def extract_slot_start(text: str):
    """
    診察予定カラムの文字列から先頭の HH:MM を抽出し、午前0時からの分数で返す。
    例: '17:30〜17:45' → 17*60+30, '17:30' → 17*60+30, 空/不正 → None
    """
    if not text:
        return None
    m = re.search(r'(\d{1,2}):(\d{2})', text)
    if not m:
        return None
    h, mm = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mm <= 59:
        return h * 60 + mm
    return None


def calc_duration(checkin: str, checkout: str):
    """
    受付時刻・終了時刻の文字列から経過時間（分）を返す。
    計算できない場合や異常値は None。
    """
    t1 = parse_hhmm(checkin)
    t2 = parse_hhmm(checkout)
    if t1 is None or t2 is None:
        return None
    diff = t2 - t1
    if diff < 0 or diff > MAX_DURATION:
        return None
    return diff


# ─── ブラウザ操作 ──────────────────────────────────────────────────

async def save_debug_screenshot(page, name="debug_screenshot.png"):
    try:
        await page.screenshot(path=name, full_page=True)
        print(f"スクリーンショット保存: {name}")
    except Exception as e:
        print(f"スクリーンショット保存失敗: {e}")


async def do_login(page):
    print(f"ログイン中... (現在URL: {page.url})")
    await page.wait_for_selector('input[type="password"]', timeout=15000)
    for sel in ['input[type="email"]', 'input[name="email"]', 'input[id*="email"]',
                'input[placeholder*="メール"]', 'input[placeholder*="mail"]']:
        el = await page.query_selector(sel)
        if el:
            await el.fill(EMAIL)
            print(f"メール入力完了 ({sel})")
            break
    await page.fill('input[type="password"]', PASSWORD)
    print("パスワード入力完了")
    for sel in ['button[type="submit"]', 'input[type="submit"]',
                'button:has-text("ログイン")', 'button:has-text("サインイン")',
                'button:has-text("Sign in")', 'button:has-text("Login")']:
        el = await page.query_selector(sel)
        if el:
            await el.click()
            print(f"ログインボタンクリック ({sel})")
            break
    await page.wait_for_load_state("networkidle", timeout=30000)
    print(f"ログイン後URL: {page.url}")


async def wait_for_page(page, context):
    """テーブルまたはログインフォームが表示されるまで待ち、必要ならログインする"""
    print(f"ページ待機中... URL: {page.url}")
    logout_btn = await page.query_selector(
        'button:has-text("ログインへ戻る"), a:has-text("ログインへ戻る")'
    )
    if logout_btn:
        print("「ログアウトしました」画面を検出 → ログインへ戻るをクリック")
        await save_debug_screenshot(page, "debug_logout_screen.png")
        await logout_btn.click()
        await page.wait_for_load_state("networkidle", timeout=15000)
    try:
        await page.wait_for_selector(
            'tbody tr, input[type="password"]', timeout=30000
        )
    except PlaywrightTimeout:
        await save_debug_screenshot(page, "debug_no_element.png")
        raise RuntimeError(f"ページ読み込みタイムアウト (URL: {page.url})")
    if await page.query_selector('input[type="password"]'):
        await save_debug_screenshot(page, "debug_login_page.png")
        await do_login(page)
        await context.storage_state(path=str(SESSION_FILE))
        try:
            await page.wait_for_selector('tbody tr', timeout=30000)
        except PlaywrightTimeout:
            await save_debug_screenshot(page, "debug_after_login.png")
            raise RuntimeError("ログイン後もテーブルが表示されません")


async def wait_for_rows_stable(page, max_wait_ms: int = 5000, poll_interval_ms: int = 500) -> int:
    """
    tbody の行数が連続2回同じ値になるまで待機（DOMの段階的レンダリング対策）。
    レースコンディションで診察待ち患者を取りこぼすのを防ぐ。
    戻り値: 最終的な行数
    """
    previous = -1
    elapsed = 0
    while elapsed < max_wait_ms:
        current = len(await page.query_selector_all('tbody tr'))
        if current == previous and current > 0:
            print(f"DOM行数安定: {current}行 (経過{elapsed}ms)")
            return current
        previous = current
        await page.wait_for_timeout(poll_interval_ms)
        elapsed += poll_interval_ms
    print(f"DOM行数タイムアウト: {previous}行で打ち切り ({max_wait_ms}ms)")
    return previous


async def scan_all_pages(page) -> tuple:
    """
    受付一覧の全ページを1回走査して以下を同時に収集する：
      - 診察待ち + 直来 の人数（walkin_count）
      - 診察待ち + 予約 の合計枠時間・件数（appt_minutes / appt_count）
      - 未受付 + 予約 の合計枠時間・件数（upcoming_minutes / upcoming_count）
        ← セッション終了までの未到着予約も事前計算に含める
      - 診察中 + 予約（直来・リハ除く）の最早開始時刻（current_slot）
      - 会計完了 + 直来 の (date, checkin_str, duration_min) リスト（履歴学習用）

    予約枠時間マッピング:
      - 再診外来   → MINUTES_RESIN (default 6分)
      - 初診外来   → MINUTES_SHINSIN (default 10分)
      - リハビリ   → 0分（医師を使わない）
      - その他     → MINUTES_PER (default 15分)

    ページネーションは tbody 外の数字ボタンを JavaScript でクリックして進む。
    """
    await page.wait_for_selector('tbody tr', timeout=30000)
    # DOMロードのレースコンディション対策：行数が安定するまで待機
    await wait_for_rows_stable(page)

    # ヘッダーからカラム位置を特定
    header_cells  = await page.query_selector_all("thead th, thead td")
    header_texts  = [await c.inner_text() for c in header_cells]
    print(f"ヘッダー: {header_texts}")

    status_idx   = next((i for i, t in enumerate(header_texts) if "ステータス" in t), None)
    label_idx    = next((i for i, t in enumerate(header_texts) if "ラベル"    in t), None)
    checkin_idx  = next((i for i, t in enumerate(header_texts) if "受付"      in t), None)
    checkout_idx = next((i for i, t in enumerate(header_texts) if "終了"      in t), None)
    menu_idx     = next((i for i, t in enumerate(header_texts) if "メニュー"  in t), None)
    appt_idx     = next((i for i, t in enumerate(header_texts) if "診察予定" in t), None)

    print(f"カラム: ステータス={status_idx}, ラベル={label_idx}, メニュー={menu_idx}, "
          f"受付={checkin_idx}, 終了={checkout_idx}, 診察予定={appt_idx}")

    today           = datetime.now(JST).strftime("%Y-%m-%d")
    walkin_count    = 0      # 直来 診察待ち
    appt_count      = 0      # 予約 診察待ち（件数）
    appt_minutes    = 0      # 予約 診察待ちの合計枠時間（分）
    upcoming_count  = 0      # 未受付予約（今後来院予定）の件数
    upcoming_minutes = 0     # 未受付予約の合計枠時間（分）
    completed_rec   = []
    current_min     = None   # 予約 診察中の最早開始時刻（分）
    walkin_in_exam  = False  # 直来（予約外）が1件でも診察中か

    # 未受付予約カウント用：セッション終了時刻と現在時刻
    now_jst         = datetime.now(JST)
    now_min         = now_jst.hour * 60 + now_jst.minute
    session_end_min = get_current_session_end_min()

    def slot_minutes_for(menu_text: str) -> int:
        """診療メニューから予約枠時間を決定。リハビリは0分（医師不使用）。"""
        if "リハビリ" in menu_text:
            return 0
        if "再診" in menu_text:
            return MINUTES_RESIN
        if "初診" in menu_text:
            return MINUTES_SHINSIN
        return MINUTES_PER  # 不明な場合はデフォルト

    for page_num in range(1, 21):   # 最大20ページ（安全ガード）
        rows = await page.query_selector_all("tbody tr")
        print(f"ページ {page_num}: {len(rows)} 行")

        for row in rows:
            if status_idx is not None and label_idx is not None:
                cells = await row.query_selector_all("td")
                max_idx = max(
                    status_idx, label_idx,
                    checkin_idx  if checkin_idx  is not None else 0,
                    checkout_idx if checkout_idx is not None else 0,
                    menu_idx     if menu_idx     is not None else 0,
                    appt_idx     if appt_idx     is not None else 0,
                )
                if len(cells) <= max_idx:
                    continue

                status_text  = await cells[status_idx].inner_text()
                label_text   = await cells[label_idx].inner_text()
                menu_text    = (await cells[menu_idx].inner_text())    if menu_idx    is not None else ""
                checkin_text = (await cells[checkin_idx].inner_text()) if checkin_idx is not None else ""
                appt_text    = (await cells[appt_idx].inner_text())    if appt_idx    is not None else ""
                is_walkin    = "直来" in label_text

                if "診察待ち" in status_text:
                    if is_walkin:
                        walkin_count += 1
                        print(f"  → 直来 診察待ち 発見 (p{page_num})")
                    else:
                        slot = slot_minutes_for(menu_text)
                        appt_count   += 1
                        appt_minutes += slot
                        print(f"  → 予約 診察待ち 発見 [{menu_text.strip()[:10]}] "
                              f"{slot}分枠 (p{page_num})")

                elif "受付待ち" in status_text:
                    # 未受付の予約（今後来院予定）をカウント
                    # 条件: 直来ラベルなし + 診察予定がセッション終了以内 + 医師診察あり(リハ除く)
                    if (not is_walkin
                            and session_end_min > 0
                            and not checkin_text.strip()):
                        slot_start = extract_slot_start(appt_text)
                        if slot_start is not None and now_min <= slot_start < session_end_min:
                            slot = slot_minutes_for(menu_text)
                            if slot > 0:
                                upcoming_count   += 1
                                upcoming_minutes += slot
                                print(f"  → 未受付予約 発見 [{menu_text.strip()[:10]}] "
                                      f"開始={slot_start // 60:02d}:{slot_start % 60:02d} "
                                      f"{slot}分枠 (p{page_num})")

                elif "診察中" in status_text:
                    # 3分岐: リハビリは無視 / 直来はフラグ化 / 予約は current_slot へ
                    is_rehab = "リハビリ" in menu_text
                    if is_rehab:
                        print(f"  → 診察中 発見 [除外: リハビリ] (p{page_num})")
                    elif is_walkin:
                        walkin_in_exam = True
                        print(f"  → 診察中 発見 [直来診察中 → walkin_in_exam=True] (p{page_num})")
                    elif appt_idx is not None and len(cells) > appt_idx:
                        start_min = extract_slot_start(appt_text)
                        if start_min is not None and (current_min is None or start_min < current_min):
                            current_min = start_min
                            print(f"  → 診察中 発見 [予約/{menu_text.strip()[:10]}/{appt_text.strip()[:15]}] "
                                  f"開始={start_min // 60:02d}:{start_min % 60:02d} (p{page_num})")

                elif "会計完了" in status_text and is_walkin:
                    # 直来の受付〜終了の時間を記録（物療比率の学習に使用）
                    if checkin_idx is not None and checkout_idx is not None:
                        ci  = await cells[checkin_idx].inner_text()
                        co  = await cells[checkout_idx].inner_text()
                        dur = calc_duration(ci, co)
                        if dur is not None:
                            checkin_clean = "".join(ci.split())[:5]  # "HH:MM"
                            completed_rec.append((today, checkin_clean, dur))

            else:
                # フォールバック: 行テキスト全体で判断（時刻情報は取得不可）
                text = await row.inner_text()
                if "直来" in text and "診察待ち" in text:
                    walkin_count += 1
                    print(f"  → 直来 診察待ち 発見（フォールバック, p{page_num}）")
                elif "直来" not in text and "診察待ち" in text:
                    appt_count += 1
                    print(f"  → 予約 診察待ち 発見（フォールバック, p{page_num}）")

        # ── 次ページへ ──
        # tbody 外にある「次のページ番号」ボタンを JavaScript でクリック
        next_page = page_num + 1
        clicked = await page.evaluate("""(np) => {
            for (const el of document.querySelectorAll('button, a')) {
                if (el.closest('tbody')) continue;          // 患者行は除外
                if (el.disabled) continue;
                if (el.getAttribute('aria-disabled') === 'true') continue;
                if (el.textContent.trim() === String(np)) {
                    el.click();
                    return true;
                }
            }
            return false;
        }""", next_page)

        if not clicked:
            print(f"ページ {next_page} のボタンが見つからないため終了")
            break

        await page.wait_for_timeout(1500)
        await page.wait_for_selector('tbody tr', timeout=10000)

    current_slot = (
        f"{current_min // 60:02d}:{current_min % 60:02d}"
        if current_min is not None else None
    )
    in_exam_remain = MINUTES_IN_EXAM_REMAIN if current_min is not None else 0
    print(f"集計完了: 直来 診察待ち={walkin_count}人, 予約 診察待ち={appt_count}人"
          f"（合計{appt_minutes}分枠）, 未受付予約={upcoming_count}人"
          f"（合計{upcoming_minutes}分枠）, 完了直来（履歴用）={len(completed_rec)}件, "
          f"診察中={current_slot or 'なし'}（残{in_exam_remain}分）, "
          f"直来診察中={walkin_in_exam}")
    return (walkin_count, appt_count, appt_minutes,
            upcoming_count, upcoming_minutes,
            in_exam_remain, completed_rec, current_slot, walkin_in_exam)


# ─── 履歴管理 ──────────────────────────────────────────────────────

def update_history(new_records: list) -> None:
    """
    data/history.json を更新する。
      - 重複 (date, checkin) は追加しない
      - HISTORY_DAYS 日より古いレコードは削除
      - 物療比率(pt_ratio)を再計算して保存
    """
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {
            "records":      [],
            "pt_ratio":     None,
            "record_count": 0,
            "updated_at":   None,
        }

    existing_keys = {(r["date"], r["checkin"]) for r in data["records"]}
    added = 0

    for date, checkin, duration in new_records:
        key = (date, checkin)
        if key in existing_keys:
            continue
        data["records"].append({
            "date":         date,
            "checkin":      checkin,
            "duration_min": duration,
        })
        existing_keys.add(key)
        added += 1

    # 古いレコードを削除
    cutoff = (datetime.now(JST) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
    data["records"] = [r for r in data["records"] if r["date"] >= cutoff]

    # 物療比率を計算
    all_d    = [r["duration_min"] for r in data["records"]]
    pt_count = sum(1 for d in all_d if d < MIN_DURATION)
    total    = len(all_d)

    data["record_count"] = total
    data["pt_ratio"]     = round(pt_count / total, 4) if total > 0 else None
    data["updated_at"]   = datetime.now(JST).isoformat()

    print(f"履歴更新: +{added}件, 合計{total}件, "
          f"PT比率={data['pt_ratio']} "
          f"（物療のみ{pt_count}件 / 全直来{total}件）")

    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def compute_estimated(walkin_count: int, appt_count: int, appt_minutes: int,
                      upcoming_count: int, upcoming_minutes: int,
                      in_exam_remain: int) -> int:
    """
    現在の待ち人数から推定待ち時間（分）を計算する。

    当院は予約優先のため、以下を合算する：
      - 診察中の残り時間（in_exam_remain）
      - 診察待ちの予約患者の合計枠時間（appt_minutes）
      - 未受付予約（今後来院予定）の合計枠時間（upcoming_minutes）
      - 直来の pt_ratio 反映時間

    予約枠: 再診外来=MINUTES_RESIN(6)分、初診外来=MINUTES_SHINSIN(10)分、
            リハビリ=0分（医師不使用）

    物療比率(pt_ratio)が学習済みの場合:
        estimated = in_exam_remain + appt_minutes + upcoming_minutes
                  + walkin_count × (1 − pt_ratio) × MINUTES_PER
    データ不足の場合:
        walkin は固定値 MINUTES_PER で計算

    ※ pt_ratio は直来患者の履歴から算出（物療のみ患者の割合）。
    """
    appt_sum = appt_minutes + upcoming_minutes

    pt_ratio = None
    record_count = 0
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        record_count = data.get("record_count", 0)
        pt_ratio = data.get("pt_ratio")

    use_pt_ratio = (pt_ratio is not None and record_count >= MIN_SAMPLES)

    if use_pt_ratio:
        walkin_effective = walkin_count * (1.0 - pt_ratio) * MINUTES_PER
        walkin_desc = f"直来{walkin_count}人 × (1 - {pt_ratio:.2f}) × {MINUTES_PER}分"
    else:
        walkin_effective = walkin_count * MINUTES_PER
        walkin_desc = f"直来{walkin_count}人 × {MINUTES_PER}分"
        if not HISTORY_FILE.exists():
            print(f"履歴なし: フォールバック {MINUTES_PER}分/人")
        else:
            print(f"データ不足({record_count}件 < {MIN_SAMPLES}件): フォールバック")

    estimated = max(0, round(in_exam_remain + appt_sum + walkin_effective))
    print(f"推定: 診察中残{in_exam_remain}分 + 診察待ち予約{appt_minutes}分({appt_count}人) "
          f"+ 未受付予約{upcoming_minutes}分({upcoming_count}人) + {walkin_desc} "
          f"= {estimated}分")
    return estimated


# ─── スクレイピング本体 ────────────────────────────────────────────

async def scrape() -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        storage = str(SESSION_FILE) if SESSION_FILE.exists() else None
        context = await browser.new_context(
            storage_state=storage,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = await context.new_page()

        try:
            print(f"アクセス中: {CLINICS_URL}")
            await page.goto(CLINICS_URL, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)
            await save_debug_screenshot(page, "debug_initial.png")
            print(f"初期URL: {page.url}")
            print(f"ページタイトル: {await page.title()}")

            await wait_for_page(page, context)

            # 全ページ走査（全カウント + 履歴収集 + 診察中予約枠 + 直来診察中フラグ）
            (walkin_count, appt_count, appt_minutes,
             upcoming_count, upcoming_minutes, in_exam_remain,
             completed_records, current_slot, walkin_in_exam) = await scan_all_pages(page)

            # 完了患者データで履歴を更新 → pt_ratio を学習
            update_history(completed_records)

            # 予約優先・未来予約含めて推定待ち時間を計算
            estimated = compute_estimated(
                walkin_count, appt_count, appt_minutes,
                upcoming_count, upcoming_minutes, in_exam_remain
            )

            print(f"直来: {walkin_count}人, 予約(待ち): {appt_count}人"
                  f"/{appt_minutes}分, 未受付予約: {upcoming_count}人"
                  f"/{upcoming_minutes}分, 診察中残: {in_exam_remain}分 "
                  f"→ 推定 約{estimated}分")
            await save_debug_screenshot(page, "debug_success.png")

            return {
                "count":             walkin_count,
                "appt_count":        appt_count,
                "appt_minutes":      appt_minutes,
                "appt_upcoming_count":   upcoming_count,
                "appt_upcoming_minutes": upcoming_minutes,
                "in_exam_remain":    in_exam_remain,
                "estimated_minutes": estimated,
                "current_slot":      current_slot,
                "walkin_in_exam":    walkin_in_exam,
                "updated_at":        datetime.now(JST).isoformat(),
                "is_open":           is_open(),
                "error":             None,
            }

        except Exception as e:
            print(f"エラー: {e}", file=sys.stderr)
            await save_debug_screenshot(page, "debug_screenshot.png")
            return {
                "count":             0,
                "appt_count":        0,
                "appt_minutes":      0,
                "appt_upcoming_count":   0,
                "appt_upcoming_minutes": 0,
                "in_exam_remain":    0,
                "estimated_minutes": 0,
                "current_slot":      None,
                "walkin_in_exam":    False,
                "updated_at":        datetime.now(JST).isoformat(),
                "is_open":           is_open(),
                "error":             str(e),
            }

        finally:
            await context.close()
            await browser.close()


def main():
    if not EMAIL or not PASSWORD:
        print("エラー: .env に CLINICS_EMAIL と CLINICS_PASSWORD を設定してください")
        sys.exit(1)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not is_exam_window():
        data = {
            "count":             0,
            "appt_count":        0,
            "appt_minutes":      0,
            "appt_upcoming_count":   0,
            "appt_upcoming_minutes": 0,
            "in_exam_remain":    0,
            "estimated_minutes": 0,
            "current_slot":      None,
            "walkin_in_exam":    False,
            "updated_at":        datetime.now(JST).isoformat(),
            "is_open":           False,
            "error":             None,
        }
        print("診察窓外（受付終了後30分超過）のため、スクレイピングをスキップします")
    else:
        data = asyncio.run(scrape())

    OUTPUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"保存完了: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
