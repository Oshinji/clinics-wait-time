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
MINUTES_PER  = int(os.getenv("MINUTES_PER_PATIENT", "10"))  # 直来再診の枠時間（pt_ratio で実効時間を計算）
SESSION_FILE = Path("session.json")
OUTPUT_FILE  = Path("data/status.json")
HISTORY_FILE = Path("data/history.json")
HOLIDAYS_FILE = Path("data/holidays.json")
JST          = timezone(timedelta(hours=9))

MIN_DURATION    = int(os.getenv("MIN_DURATION_MINUTES", "20"))  # 物療のみ患者の判定閾値（分）
MAX_DURATION    = 180    # エラーデータ除外閾値（分）
HISTORY_DAYS    = 30     # 履歴保持日数（30日でpt_ratioが直近の傾向を反映）
MIN_SAMPLES     = 5      # フォールバック閾値（件未満は固定値を使用）
# 予約枠時間（診療メニューごと）
# 予約枠は再診10分/初診15分だが、実際の医師診察時間は枠より短く
# 残り時間が直来の隙間になる。実績に基づき短縮値を採用。
MINUTES_RESIN          = int(os.getenv("MINUTES_RESIN",          "6"))   # 予約再診外来の実診察時間
MINUTES_SHINSIN        = int(os.getenv("MINUTES_SHINSIN",        "10"))  # 予約初診外来の実診察時間（枠15分内）
MINUTES_SHINSIN_WALKIN = int(os.getenv("MINUTES_SHINSIN_WALKIN", "10"))  # 直来初診の医師占有時間（予約初診と同じ10分）
MINUTES_IN_EXAM_REMAIN = int(os.getenv("MINUTES_IN_EXAM_REMAIN", "4"))   # 診察中患者の残り時間見積もり（再診枠6分より長くならないよう4分に設定）
ATTENDANCE_RATE        = float(os.getenv("ATTENDANCE_RATE",      "0.8")) # 未受付予約の出席率（キャンセル/遅刻/no-show を考慮、実測キャンセル率 約20%）
# ────────────────────────────────────────────────────────────────
# 受付時間
#   月火水金・土　午前  8:30〜11:30
#              　午後 14:30〜17:00（土曜は〜16:00）
#   木・日　休診

AM_START   = dt_time(8,  30)
AM_END     = dt_time(11, 30)
PM_START   = dt_time(14, 30)
PM_END     = dt_time(17,  0)
PM_END_SAT = dt_time(16,  0)


def _load_holidays() -> set:
    """data/holidays.json から休診日リストを読み込む。失敗時は空セット（=祝日扱いなし）。"""
    if not HOLIDAYS_FILE.exists():
        return set()
    try:
        data = json.loads(HOLIDAYS_FILE.read_text(encoding="utf-8"))
        return set(data.get("closed", []))
    except Exception as e:
        print(f"⚠️ holidays.json 読込失敗: {e}")
        return set()


def is_special_closed(now: datetime | None = None) -> bool:
    """holidays.json の closed リストに今日の日付が含まれるか"""
    if now is None:
        now = datetime.now(JST)
    today = now.strftime("%Y-%m-%d")
    return today in _load_holidays()


def is_open() -> bool:
    now = datetime.now(JST)
    if is_special_closed(now):
        return False
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
    holidays.json で指定された休診日も False を返す。
    """
    now = datetime.now(JST)
    if is_special_closed(now):
        return False
    wd  = now.weekday()
    t   = now.time()
    if wd in (3, 6):
        return False
    # 午前 8:30〜13:00（受付11:30 + 延長1.5時間）
    if AM_START <= t < dt_time(13, 0):
        return True
    # 午後 14:30〜19:00（受付17:00/16:00 + 延長）
    if PM_START <= t < dt_time(19, 0):
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
        return pm_end.hour * 60 + pm_end.minute  # 17:00 or 16:00
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
        # ログイン後は別URLへリダイレクトされることがある（ホーム画面等）。
        # 患者一覧ページ(/d)でない場合は明示的に再遷移して tbody を確保する。
        post_login_url = page.url
        print(f"ログイン後URL: {post_login_url}")
        if CLINICS_URL not in post_login_url:
            print(f"→ 患者一覧と異なるURL。{CLINICS_URL} へ再遷移します")
            await page.goto(CLINICS_URL, wait_until="networkidle", timeout=30000)
            print(f"再遷移後URL: {page.url}")
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
    walkin_count    = 0      # 直来 診察待ちの総数（旧来の急減検知セーフティ用）
    walkin_resin_count   = 0 # 直来 診察待ち のうち再診外来（pt_ratio で実効時間を計算）
    walkin_shoshin_count = 0 # 直来 診察待ち のうち初診外来（必ず医師診察 → 固定時間）
    walkin_shoshin_minutes = 0  # 上記の合計時間（MINUTES_SHINSIN × 件数）
    appt_count      = 0      # 予約 診察待ち（件数）
    appt_minutes    = 0      # 予約 診察待ちの合計枠時間（分）
    upcoming_slots  = []     # 未受付予約（今後来院予定）のリスト [(slot_start_min, duration_min), ...]
                              # ← 反復計算で「自分が呼ばれる時刻までに到着する予約のみ」加算するために個別保持
    completed_rec   = []
    current_mins    = []     # 予約 診察中の全開始時刻（分）リスト（最大3件取得）
    walkin_in_exam  = False  # 直来（予約外）が1件でも診察中か
    last_called_min = None   # 予約（医師診察）で「呼ばれた」ことのある最新 slot 開始分
                              # （診察中/算定待ち/会計待ち/完了/会計完了 を"呼ばれた"と見なす）

    # 未受付予約カウント用：セッション終了時刻と現在時刻
    now_jst         = datetime.now(JST)
    now_min         = now_jst.hour * 60 + now_jst.minute
    session_end_min = get_current_session_end_min()

    # セッション内時刻フィルタ（前日・他セッションの枠が混入しないよう）
    # CLINICS の一覧には前日患者が残るため、現在のセッション時間帯のみを対象とする
    _now_time = now_jst.time()
    if AM_START <= _now_time < dt_time(13, 0):
        _session_lo, _session_hi = 8 * 60, 13 * 60    # 8:00-13:00
    elif PM_START <= _now_time < dt_time(19, 0):
        _session_lo, _session_hi = 14 * 60, 19 * 60   # 14:00-19:00
    else:
        _session_lo, _session_hi = 0, 24 * 60         # フィルタなし（念のため）

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

                # 予約（医師診察）で「呼ばれた」経歴のある最新 slot を追跡
                #   診察中/算定待ち/会計待ち/完了/会計完了 いずれかなら "呼ばれた" と見なす
                if ((not is_walkin)
                        and "リハビリ" not in menu_text
                        and any(x in status_text for x in ("診察中", "算定", "会計", "完了"))):
                    _s = extract_slot_start(appt_text)
                    if (_s is not None
                            and _session_lo <= _s < _session_hi   # セッション内の枠のみ
                            and _s <= now_min + 60                 # 現在+60分以内（前日の未来枠を除外）
                            and (last_called_min is None or _s > last_called_min)):
                        last_called_min = _s

                if "診察待ち" in status_text:
                    if is_walkin:
                        walkin_count += 1
                        # メニューによって扱いを分岐
                        if "リハビリ" in menu_text:
                            # リハビリのみは医師時間を消費しない（カウントだけ残す）
                            print(f"  → 直来 診察待ち [リハビリ→0分扱い] (p{page_num})")
                        elif "初診" in menu_text:
                            # 直来初診は予約と違い「枠なしの割込」となるため、
                            # 予約初診の MINUTES_SHINSIN(10) より長い MINUTES_SHINSIN_WALKIN(15) を使う
                            walkin_shoshin_count   += 1
                            walkin_shoshin_minutes += MINUTES_SHINSIN_WALKIN
                            print(f"  → 直来 診察待ち [初診→固定{MINUTES_SHINSIN_WALKIN}分（直来用）] (p{page_num})")
                        else:
                            # 再診（または不明）: pt_ratio で実効時間を計算
                            walkin_resin_count += 1
                            print(f"  → 直来 診察待ち [再診→pt_ratio適用] (p{page_num})")
                    else:
                        slot = slot_minutes_for(menu_text)
                        appt_count   += 1
                        appt_minutes += slot
                        print(f"  → 予約 診察待ち 発見 [{menu_text.strip()[:10]}] "
                              f"{slot}分枠 (p{page_num})")

                elif "受付待ち" in status_text:
                    # 未受付の予約（今後来院予定）を個別スロットで保持
                    # 条件: 直来ラベルなし + 診察予定がセッション終了以内 + 医師診察あり(リハ除く)
                    if (not is_walkin
                            and session_end_min > 0
                            and not checkin_text.strip()):
                        slot_start = extract_slot_start(appt_text)
                        if slot_start is not None and now_min <= slot_start < session_end_min:
                            slot = slot_minutes_for(menu_text)
                            if slot > 0:
                                upcoming_slots.append((slot_start, slot))
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
                        if start_min is not None:
                            in_range   = _session_lo <= start_min < _session_hi
                            not_future = start_min <= now_min + 60
                            if in_range and not_future:
                                current_mins.append(start_min)
                                print(f"  → 診察中 発見 [予約/{menu_text.strip()[:10]}/{appt_text.strip()[:15]}] "
                                      f"開始={start_min // 60:02d}:{start_min % 60:02d} (p{page_num})")
                            else:
                                reason = "セッション外" if not in_range else "現在時刻より大幅未来（前日データ疑い）"
                                print(f"  ⚠️ 診察中 発見 [予約/{menu_text.strip()[:10]}] "
                                      f"開始={start_min // 60:02d}:{start_min % 60:02d} {reason} → スキップ (p{page_num})")
                        else:
                            # 診察予定セルが空 or 解析不能 → 枠時刻不明のためスキップ
                            print(f"  ⚠️ 診察中 発見 [予約/{menu_text.strip()[:10]}] "
                                  f"診察予定セル空 or 解析不能 (appt={repr(appt_text.strip()[:20])}) → スキップ (p{page_num})")

                elif "会計完了" in status_text and is_walkin:
                    # 直来の受付〜終了の時間を記録（物療比率の学習に使用）
                    # ※ 初診とリハビリは pt_ratio 学習対象から除外:
                    #   - 初診は必ず医師診察あり → 物療のみと混ぜると pt_ratio が下がってしまう
                    #   - リハビリ単独受診はそもそも医師時間を使わない別カテゴリ
                    if "初診" in menu_text or "リハビリ" in menu_text:
                        pass  # 履歴学習対象外
                    elif checkin_idx is not None and checkout_idx is not None:
                        ci  = await cells[checkin_idx].inner_text()
                        co  = await cells[checkout_idx].inner_text()
                        dur = calc_duration(ci, co)
                        if dur is not None:
                            checkin_clean = "".join(ci.split())[:5]  # "HH:MM"
                            completed_rec.append((today, checkin_clean, dur))
            # 注: ステータス/ラベル カラムが検出できない場合（CLINICS のヘッダーが変わった等）は
            # 行を 全てスキップ → status.json は全ゼロ → main() のセーフティネットで前回値保持。

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

    # 重複排除 → 昇順ソート → 最大3枠
    current_mins_sorted = sorted(set(current_mins))[:3]
    current_slots = [f"{m // 60:02d}:{m % 60:02d}" for m in current_mins_sorted]
    current_slot  = current_slots[0] if current_slots else None   # 後方互換
    in_exam_remain = MINUTES_IN_EXAM_REMAIN if (current_slots or walkin_in_exam) else 0

    # 直近に呼ばれた予約 slot（walkin_in_exam 時のフォールバック表示用）
    if last_called_min is not None:
        last_predicted_slot = f"{last_called_min // 60:02d}:{last_called_min % 60:02d}"
    else:
        # 誰もまだ予約診察してない → セッション開始時刻
        _now_t = datetime.now(JST).time()
        if AM_START <= _now_t < dt_time(12, 0):
            last_predicted_slot = "08:30"
        elif PM_START <= _now_t < dt_time(18, 0):
            last_predicted_slot = "14:30"
        else:
            last_predicted_slot = None

    # 未受付予約の集計値（表示・ログ用）
    upcoming_count   = len(upcoming_slots)
    upcoming_minutes = sum(d for _, d in upcoming_slots)

    print(f"集計完了: 直来 診察待ち={walkin_count}人 "
          f"(初診{walkin_shoshin_count}人/{walkin_shoshin_minutes}分・再診{walkin_resin_count}人), "
          f"予約 診察待ち={appt_count}人（合計{appt_minutes}分枠）, "
          f"未受付予約={upcoming_count}人（合計{upcoming_minutes}分枠）, "
          f"完了直来（履歴用）={len(completed_rec)}件, "
          f"診察中={current_slots or 'なし'}（残{in_exam_remain}分）, "
          f"直来診察中={walkin_in_exam}, 直近呼ばれた予約枠={last_predicted_slot}")
    return (walkin_count, walkin_resin_count,
            walkin_shoshin_count, walkin_shoshin_minutes,
            appt_count, appt_minutes,
            upcoming_count, upcoming_minutes, upcoming_slots,
            in_exam_remain, completed_rec, current_slot, current_slots,
            walkin_in_exam, last_predicted_slot, now_min)


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


def compute_estimated(walkin_resin_count: int, walkin_shoshin_count: int,
                      walkin_shoshin_minutes: int,
                      appt_count: int, appt_minutes: int,
                      upcoming_count: int, upcoming_minutes: int,
                      upcoming_slots: list, in_exam_remain: int,
                      now_min: int) -> int:
    """
    現在の待ち状況から推定待ち時間（分）を反復計算する。

    当院は予約優先。新規直来の待ち時間は「自分が呼ばれる予想時刻までに
    到着する予約」のみが影響する（朝一に全予約分を足すと過大評価になる）。

    直来の扱い:
      - 初診外来 直来 → 必ず医師診察あり → 固定 MINUTES_SHINSIN 分（pt_ratio無視）
      - 再診外来 直来 → 物療のみが多いため pt_ratio で実効時間を計算
      - リハビリ 直来 → 0分（医師不使用、scan側でカウント外）

    反復計算：
        base = 診察中残 + 診察待ち予約 + 直来初診時間 + 直来再診×(1-pt_ratio)×MINUTES_PER
        total = base
        loop:
            到着予定時刻 <= 現在時刻 + total の未受付予約のみ加算
            new_total = base + (加算分 × ATTENDANCE_RATE)
            |new_total - total| < 1 で収束

    朝一や昼一で「セッション残り全員を足す」過大評価を防ぎ、
    現実的な値に収束する（60〜70分前後）。

    予約枠: 再診外来=MINUTES_RESIN(6)分、初診外来=MINUTES_SHINSIN(10)分、
            リハビリ=0分（医師不使用）

    出席率(ATTENDANCE_RATE=0.9): 当院の実測キャンセル率 約10% に合わせて設定。
    朝一の過大評価を一部抑制（180分→約160分相当）。
    残りの過大評価は「直来が予約の隙間に入る」「医師の実処理時間が短め」等の
    要因によるもので、必要に応じて MINUTES_RESIN/MINUTES_SHINSIN を別途調整。
    """
    pt_ratio = None
    record_count = 0
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, encoding="utf-8") as f:
            data = json.load(f)
        record_count = data.get("record_count", 0)
        pt_ratio = data.get("pt_ratio")

    use_pt_ratio = (pt_ratio is not None and record_count >= MIN_SAMPLES)

    if use_pt_ratio:
        walkin_resin_eff = walkin_resin_count * (1.0 - pt_ratio) * MINUTES_PER
        resin_desc = f"再診直来{walkin_resin_count}人 × (1 - {pt_ratio:.2f}) × {MINUTES_PER}分"
    else:
        walkin_resin_eff = walkin_resin_count * MINUTES_PER
        resin_desc = f"再診直来{walkin_resin_count}人 × {MINUTES_PER}分"
        if not HISTORY_FILE.exists():
            print(f"履歴なし: フォールバック {MINUTES_PER}分/人")
        else:
            print(f"データ不足({record_count}件 < {MIN_SAMPLES}件): フォールバック")

    walkin_effective = walkin_resin_eff + walkin_shoshin_minutes
    walkin_desc = (f"{resin_desc} + 初診直来{walkin_shoshin_count}人×"
                   f"{MINUTES_SHINSIN_WALKIN}分(固定)")

    base = in_exam_remain + appt_minutes + walkin_effective

    # 反復計算：自分が呼ばれる予想時刻までに到着する未受付予約のみ加算
    # ※ 未受付予約には ATTENDANCE_RATE（出席率）を掛けてキャンセル/遅刻/no-showを考慮
    # ※ 上界（全未受付予約を含む値）から収束させる。
    #   下界(base)からだと予約が後半集中している場合に収束が早すぎ過小推定になる。
    total = base + sum(d for _, d in upcoming_slots) * ATTENDANCE_RATE
    added = 0
    converged_iter = 0
    for i in range(10):  # 最大10回で収束
        cutoff = now_min + total
        raw_added = sum(dur for slot, dur in upcoming_slots if slot <= cutoff)
        new_added = raw_added * ATTENDANCE_RATE
        new_total = base + new_added
        converged_iter = i + 1
        if abs(new_total - total) < 1:
            added = new_added
            total = new_total
            break
        added = new_added
        total = new_total

    estimated = max(0, round(total))
    included_cnt = sum(1 for slot, _ in upcoming_slots if slot <= now_min + estimated)
    print(f"推定: 診察中残{in_exam_remain}分 + 診察待ち予約{appt_minutes}分({appt_count}人) "
          f"+ 反復加算{added:.0f}分（未受付{included_cnt}/{upcoming_count}人×出席率{ATTENDANCE_RATE}, "
          f"{converged_iter}回反復） + {walkin_desc} = {estimated}分")
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
            # wait_for_page() 内で tbody または login フォームを待機するので個別の sleep は不要
            print(f"初期URL: {page.url}")

            await wait_for_page(page, context)

            # 全ページ走査（全カウント + 履歴収集 + 診察中予約枠 + 直来診察中フラグ + 直近予約枠）
            (walkin_count, walkin_resin_count,
             walkin_shoshin_count, walkin_shoshin_minutes,
             appt_count, appt_minutes,
             upcoming_count, upcoming_minutes, upcoming_slots,
             in_exam_remain, completed_records, current_slot, current_slots,
             walkin_in_exam, last_predicted_slot,
             now_min) = await scan_all_pages(page)

            # ─── セーフティネット: 直来カウントの急減を検出して前回値を保持 ───
            # シナリオ: 直前に直来 N(≥2) 名いたのに今回 0 名 + 他に活動継続中
            #          → ラベル取得タイミングのDOM取りこぼしで誤検出している可能性大
            #          → 前回の再診/初診カウントを1サイクルだけ保持して再計算
            #
            # 連鎖的な保護を避けるため、前回が既に保護状態だった場合は今回は保護しない
            # （最大1サイクル＝5分間だけ保護、その後は scraper の真の値を信頼）
            walkin_protected = False
            if is_open():
                prev = _load_previous_status()
                if prev is not None and _prev_is_recent(prev, max_age_sec=900):
                    prev_walkin = int(prev.get("count", 0) or 0)
                    prev_was_protected = bool(prev.get("walkin_protected", False))
                    activity = (appt_count > 0
                                or upcoming_count > 0
                                or in_exam_remain > 0
                                or walkin_in_exam)
                    if (prev_walkin >= 2
                            and walkin_count == 0
                            and not prev_was_protected
                            and activity):
                        prev_resin = int(prev.get("walkin_resin_count", 0) or 0)
                        prev_shoshin = int(prev.get("walkin_shoshin_count", 0) or 0)
                        prev_shoshin_min = int(prev.get("walkin_shoshin_minutes", 0) or 0)
                        print(f"⚠️ 直来カウント急減検出 (前回{prev_walkin}名→今回0名) "
                              f"+ 他活動あり → 前回値で保護（次回は保護しない）")
                        walkin_count            = prev_walkin
                        walkin_resin_count      = prev_resin
                        walkin_shoshin_count    = prev_shoshin
                        walkin_shoshin_minutes  = prev_shoshin_min
                        walkin_protected        = True

            # 完了患者データで履歴を更新 → pt_ratio を学習（再診直来のみ）
            update_history(completed_records)

            # 予約優先・反復計算で過大評価を防ぎつつ推定待ち時間を計算
            estimated = compute_estimated(
                walkin_resin_count, walkin_shoshin_count, walkin_shoshin_minutes,
                appt_count, appt_minutes,
                upcoming_count, upcoming_minutes, upcoming_slots,
                in_exam_remain, now_min
            )

            print(f"直来: 計{walkin_count}人 (再診{walkin_resin_count}・初診{walkin_shoshin_count})"
                  f"{'(保護中)' if walkin_protected else ''}, "
                  f"予約(待ち): {appt_count}人/{appt_minutes}分, "
                  f"未受付予約: {upcoming_count}人/{upcoming_minutes}分, "
                  f"診察中残: {in_exam_remain}分 → 推定 約{estimated}分")

            return {
                "count":             walkin_count,
                "walkin_resin_count":     walkin_resin_count,
                "walkin_shoshin_count":   walkin_shoshin_count,
                "walkin_shoshin_minutes": walkin_shoshin_minutes,
                "appt_count":        appt_count,
                "appt_minutes":      appt_minutes,
                "appt_upcoming_count":   upcoming_count,
                "appt_upcoming_minutes": upcoming_minutes,
                "in_exam_remain":    in_exam_remain,
                "estimated_minutes": estimated,
                "current_slot":      current_slot,
                "current_slots":     current_slots,
                "walkin_in_exam":    walkin_in_exam,
                "last_predicted_slot": last_predicted_slot,
                "walkin_protected":  walkin_protected,
                "updated_at":        datetime.now(JST).isoformat(),
                "is_open":           is_open(),
                "is_holiday":        is_special_closed(),
                "error":             None,
            }

        except Exception as e:
            print(f"エラー: {e}", file=sys.stderr)
            await save_debug_screenshot(page, "debug_screenshot.png")
            return _empty_status_data(error=str(e), is_open_val=is_open())

        finally:
            await context.close()
            await browser.close()


# ─── 状態データ・セーフティネット用ヘルパー ────────────────────────────

def _empty_status_data(error: str | None = None,
                      is_open_val: bool = False,
                      is_holiday_val: bool | None = None) -> dict:
    """全ゼロの status.json データを生成（エラー時・診察窓外時で共通化）"""
    if is_holiday_val is None:
        is_holiday_val = is_special_closed()
    return {
        "count":             0,
        "walkin_resin_count":     0,
        "walkin_shoshin_count":   0,
        "walkin_shoshin_minutes": 0,
        "appt_count":        0,
        "appt_minutes":      0,
        "appt_upcoming_count":   0,
        "appt_upcoming_minutes": 0,
        "in_exam_remain":    0,
        "estimated_minutes": 0,
        "current_slot":      None,
        "current_slots":     [],
        "walkin_in_exam":    False,
        "last_predicted_slot": None,
        "walkin_protected":  False,
        "updated_at":        datetime.now(JST).isoformat(),
        "is_open":           is_open_val,
        "is_holiday":        is_holiday_val,
        "error":             error,
    }


def _should_preserve_previous(data: dict) -> bool:
    """前回値を保持すべきかを判定する。
    対象:
      ① エラー発生時（ログイン失敗・DOM未表示等）→ 前回の良いデータを失わないように保持
      ② 全ゼロ＋診察中なし（誤検出の可能性大）→ 前回値を保持
    どちらの場合も、最大 _prev_is_recent の有効期限まで前回値を維持する。
    """
    if data.get("error"):
        return True
    people = (data.get("count", 0)
              + data.get("appt_count", 0)
              + data.get("appt_upcoming_count", 0))
    nothing_in_exam = (not data.get("walkin_in_exam")
                       and not data.get("current_slot"))
    return people == 0 and nothing_in_exam


def _load_previous_status() -> dict | None:
    """前回の status.json を読む。存在しない/壊れている時は None。"""
    if not OUTPUT_FILE.exists():
        return None
    try:
        return json.loads(OUTPUT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _prev_is_recent(prev: dict, max_age_sec: int = 3600) -> bool:
    """前回値の鮮度チェック（デフォルト1時間）"""
    prev_at = prev.get("updated_at")
    if not prev_at:
        return False
    try:
        prev_dt = datetime.fromisoformat(prev_at)
        age_sec = (datetime.now(JST) - prev_dt).total_seconds()
        return age_sec < max_age_sec
    except Exception:
        return False


# ─── エントリポイント ──────────────────────────────────────────────

def main():
    if not EMAIL or not PASSWORD:
        print("エラー: .env に CLINICS_EMAIL と CLINICS_PASSWORD を設定してください")
        sys.exit(1)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not is_exam_window():
        is_holiday_today = is_special_closed()
        data = _empty_status_data(is_holiday_val=is_holiday_today)
        if is_holiday_today:
            print("本日は休診日です（holidays.json）→ スクレイピングをスキップ")
        else:
            print("診察窓外（受付終了後30分超過）のため、スクレイピングをスキップします")
    else:
        data = asyncio.run(scrape())

        # ─── セーフティネット①: 全ゼロ誤報なら前回値を保持 ───
        # 診察窓内なのに count/appt/walkin/current 全て空 = scraper が行を拾えていない
        # 可能性大。前回値が1時間以内なら書き込みをスキップして保持する。
        if _should_preserve_previous(data):
            prev = _load_previous_status()
            if prev is not None and _prev_is_recent(prev, max_age_sec=900) and not prev.get("error"):
                # 前回値がエラーの場合は保持しない（エラーを上書きして正常化する）
                prev_at = prev.get("updated_at", "unknown")
                print(f"⚠️ 診察時間中に全ゼロ検出 → 前回値を保持（前回updated_at={prev_at}）")
                print(f"   (status.json 書き込みスキップ。history.json は通常通り更新)")
                return
            else:
                reason = "前回がエラー" if (prev is not None and prev.get("error")) else "前回値が1時間超過 or 無し"
                print(f"⚠️ 全ゼロ検出だが{reason} → そのまま書き込み")

        # ─── セーフティネット②: 未受付予約の急減（DOM過渡状態検出）───
        # 受付時間中に未受付予約が 5人以上→0人 に急減した場合、
        # ページ再描画中の一時的な中間状態を読んだ可能性が高い。
        # 前回値が15分以内なら書き込みをスキップして保持する。
        if (not data.get("error")
                and data.get("is_open")
                and data.get("appt_upcoming_count", 0) == 0):
            prev = _load_previous_status()
            if (prev is not None
                    and prev.get("appt_upcoming_count", 0) >= 5
                    and _prev_is_recent(prev, max_age_sec=900)
                    and not prev.get("error")):
                prev_up  = prev.get("appt_upcoming_count")
                prev_at  = prev.get("updated_at", "unknown")
                print(f"⚠️ 未受付予約が急減（{prev_up}人→0人）かつ受付中 "
                      f"→ DOM過渡状態の可能性 → 前回値を保持（前回updated_at={prev_at}）")
                return

    OUTPUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"保存完了: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
