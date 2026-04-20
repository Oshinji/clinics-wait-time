#!/usr/bin/env python3
"""
クリニクス 直来待ち時間スクレイパー
直来患者の診察待ち人数を取得し、待ち時間を推定します
"""

import asyncio
import json
import os
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
OPEN_HOUR    = int(os.getenv("OPEN_HOUR",  "8"))
CLOSE_HOUR   = int(os.getenv("CLOSE_HOUR", "18"))
JST          = timezone(timedelta(hours=9))
# ────────────────────────────────────────────────────────────────
# 受付時間
#   月火水金・土　午前  8:30〜11:30
#              　午後 14:30〜17:30（土曜は〜16:30）
#   木・日　休診

AM_START = dt_time(8,  30)
AM_END   = dt_time(11, 30)
PM_START = dt_time(14, 30)
PM_END   = dt_time(17, 30)   # 平日
PM_END_SAT = dt_time(16, 30) # 土曜


def is_open() -> bool:
    now = datetime.now(JST)
    wd  = now.weekday()   # 0=月,1=火,2=水,3=木,4=金,5=土,6=日
    t   = now.time()

    if wd in (3, 6):      # 木・日は休診
        return False

    pm_end = PM_END_SAT if wd == 5 else PM_END
    return (AM_START <= t < AM_END) or (PM_START <= t < pm_end)


async def save_debug_screenshot(page, name="debug_screenshot.png"):
    try:
        await page.screenshot(path=name, full_page=True)
        print(f"スクリーンショット保存: {name}")
    except Exception as e:
        print(f"スクリーンショット保存失敗: {e}")


async def do_login(page):
    print(f"ログイン中... (現在URL: {page.url})")

    # パスワードフィールドが現れるまで待つ
    await page.wait_for_selector('input[type="password"]', timeout=15000)

    # メールアドレス入力（複数セレクタを試す）
    for sel in ['input[type="email"]', 'input[name="email"]', 'input[id*="email"]',
                'input[placeholder*="メール"]', 'input[placeholder*="mail"]']:
        el = await page.query_selector(sel)
        if el:
            await el.fill(EMAIL)
            print(f"メール入力完了 ({sel})")
            break

    # パスワード入力
    await page.fill('input[type="password"]', PASSWORD)
    print("パスワード入力完了")

    # ログインボタンをクリック
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
    """
    テーブルまたはログインフォームが表示されるまで待ち、
    必要ならログインを行う
    """
    print(f"ページ待機中... URL: {page.url}")

    # 「ログアウトしました」画面の「ログインへ戻る」ボタンを検出してクリック
    logout_btn = await page.query_selector('button:has-text("ログインへ戻る"), a:has-text("ログインへ戻る")')
    if logout_btn:
        print("「ログアウトしました」画面を検出 → ログインへ戻るをクリック")
        await save_debug_screenshot(page, "debug_logout_screen.png")
        await logout_btn.click()
        await page.wait_for_load_state("networkidle", timeout=15000)

    # テーブルまたはパスワードフォームが出るまで待つ
    try:
        await page.wait_for_selector(
            'tbody tr, input[type="password"]',
            timeout=30000
        )
    except PlaywrightTimeout:
        await save_debug_screenshot(page, "debug_no_element.png")
        raise RuntimeError(f"ページ読み込みタイムアウト (URL: {page.url})")

    # ログインフォームが表示されていたらログイン
    if await page.query_selector('input[type="password"]'):
        await save_debug_screenshot(page, "debug_login_page.png")
        await do_login(page)
        await context.storage_state(path=str(SESSION_FILE))

        # ログイン後にテーブルを待つ
        try:
            await page.wait_for_selector('tbody tr', timeout=30000)
        except PlaywrightTimeout:
            await save_debug_screenshot(page, "debug_after_login.png")
            raise RuntimeError("ログイン後もテーブルが表示されません")


async def click_status_filter(page, label: str) -> bool:
    """
    画面上部のステータスフィルタボタンをクリックする。
    クリックできたら True を返す。
    """
    selectors = [
        f'button:has-text("{label}")',
        f'label:has-text("{label}")',
        f'span:has-text("{label}")',
        f'div:has-text("{label}")',
        f'a:has-text("{label}")',
    ]
    for sel in selectors:
        els = await page.query_selector_all(sel)
        for el in els:
            text = (await el.inner_text()).strip()
            if text == label or label in text:
                await el.click()
                print(f"フィルタクリック: 「{label}」({sel})")
                await page.wait_for_timeout(2000)
                return True
    print(f"警告: フィルタ「{label}」が見つかりませんでした")
    return False


async def count_walk_in_waiting(page) -> int:
    """
    「診察待ち」フィルタを適用し、直来患者の待ち人数を返す。

    ページネーションで午後の患者が別ページにいる問題を回避するため、
    先にステータスフィルタを「診察待ち」に絞ってから集計する。
    """
    await page.wait_for_selector('tbody tr', timeout=30000)

    # 「診察待ち」フィルタをクリック（ページ送り不要になる）
    filtered = await click_status_filter(page, "診察待ち")
    await page.wait_for_selector('tbody tr, .no-result, [class*="empty"]', timeout=15000)

    rows = await page.query_selector_all("tbody tr")
    print(f"診察待ちフィルタ後の行数: {len(rows)}")

    if not rows:
        return 0

    count = 0

    if filtered:
        # フィルタ成功 → 全行が「診察待ち」なので直来ラベルだけ確認
        for row in rows:
            text = await row.inner_text()
            if "直来" in text:
                count += 1
                print(f"  直来 診察待ち: {text[:80].strip()}")
    else:
        # フィルタ失敗 → 全行からステータスとラベルを両方チェック
        header_cells = await page.query_selector_all("thead th, thead td")
        header_texts = [await c.inner_text() for c in header_cells]
        print(f"ヘッダー: {header_texts}")

        status_idx = next((i for i, t in enumerate(header_texts) if "ステータス" in t), None)
        label_idx  = next((i for i, t in enumerate(header_texts) if "ラベル"    in t), None)

        for row in rows:
            if status_idx is not None and label_idx is not None:
                cells = await row.query_selector_all("td")
                if len(cells) <= max(status_idx, label_idx):
                    continue
                status_text = await cells[status_idx].inner_text()
                label_text  = await cells[label_idx].inner_text()
                if "診察待ち" in status_text and "直来" in label_text:
                    count += 1
            else:
                text = await row.inner_text()
                if "診察待ち" in text and "直来" in text:
                    count += 1

    print(f"直来 診察待ち: {count}人")
    return count


async def scrape() -> dict:
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        # 保存済みセッションがあれば復元
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

            # SPAのレンダリングを待つ
            await page.wait_for_timeout(5000)
            await save_debug_screenshot(page, "debug_initial.png")
            print(f"初期URL: {page.url}")
            print(f"ページタイトル: {await page.title()}")

            # ログイン処理（必要な場合）
            await wait_for_page(page, context)

            # 受付一覧の取得
            count    = await count_walk_in_waiting(page)
            estimated = count * MINUTES_PER

            print(f"直来 診察待ち: {count}人 → 推定 約{estimated}分")

            # 成功時もスクリーンショットを残す（デバッグ用）
            await save_debug_screenshot(page, "debug_success.png")

            return {
                "count":             count,
                "estimated_minutes": estimated,
                "updated_at":        datetime.now(JST).isoformat(),
                "is_open":           True,
                "error":             None,
            }

        except Exception as e:
            print(f"エラー: {e}", file=sys.stderr)
            await save_debug_screenshot(page, "debug_screenshot.png")
            return {
                "count":             0,
                "estimated_minutes": 0,
                "updated_at":        datetime.now(JST).isoformat(),
                "is_open":           True,
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

    if not is_open():
        data = {
            "count":             0,
            "estimated_minutes": 0,
            "updated_at":        datetime.now(JST).isoformat(),
            "is_open":           False,
            "error":             None,
        }
        print("診療時間外のため、スクレイピングをスキップします")
    else:
        data = asyncio.run(scrape())

    OUTPUT_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"保存完了: {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
