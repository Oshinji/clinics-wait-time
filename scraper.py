#!/usr/bin/env python3
"""
クリニクス 直来待ち時間スクレイパー
直来患者の診察待ち人数を取得し、待ち時間を推定します
"""

import asyncio
import json
import os
import sys
from datetime import datetime, time as dt_time
from pathlib import Path

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout
from dotenv import load_dotenv

load_dotenv()

# ─── 設定 ────────────────────────────────────────────────────────
CLINICS_URL    = "https://karte.medley.life/d"
EMAIL          = os.getenv("CLINICS_EMAIL", "")
PASSWORD       = os.getenv("CLINICS_PASSWORD", "")
MINUTES_PER    = int(os.getenv("MINUTES_PER_PATIENT", "15"))
SESSION_FILE   = Path("session.json")
OUTPUT_FILE    = Path("data/status.json")
OPEN_HOUR      = int(os.getenv("OPEN_HOUR",  "8"))
CLOSE_HOUR     = int(os.getenv("CLOSE_HOUR", "18"))
# ────────────────────────────────────────────────────────────────


def is_open() -> bool:
    now = datetime.now().time()
    return dt_time(OPEN_HOUR, 0) <= now < dt_time(CLOSE_HOUR, 0)


async def do_login(page):
    print("ログイン中...")
    # メールアドレス
    await page.wait_for_selector(
        'input[type="email"], input[name="email"]', timeout=15000
    )
    await page.fill('input[type="email"], input[name="email"]', EMAIL)
    # パスワード
    await page.fill('input[type="password"]', PASSWORD)
    # ログインボタン
    await page.click(
        'button[type="submit"], '
        'input[type="submit"], '
        'button:has-text("ログイン"), '
        'button:has-text("Sign in")'
    )
    await page.wait_for_load_state("networkidle", timeout=30000)
    print("ログイン完了")


async def ensure_logged_in(page, context):
    """ログインが必要なら実施し、セッションを保存する"""
    needs_login = any(
        kw in page.url for kw in ["login", "sign_in", "signin", "auth", "session"]
    )
    # ログインフォームが見えているかもチェック
    if not needs_login:
        has_form = await page.query_selector('input[type="password"]')
        needs_login = has_form is not None

    if needs_login:
        await do_login(page)
        await context.storage_state(path=str(SESSION_FILE))


async def count_walk_in_waiting(page) -> int:
    """
    受付一覧テーブルから
    ステータス=診察待ち かつ ラベルに「直来」を含む行を数える
    """
    # テーブル行が現れるまで待機
    try:
        await page.wait_for_selector("tbody tr", timeout=20000)
    except PlaywrightTimeout:
        raise RuntimeError("テーブルが表示されませんでした（セッション切れの可能性）")

    # ヘッダーからカラム位置を特定
    header_cells = await page.query_selector_all("thead th, thead td")
    header_texts = [await c.inner_text() for c in header_cells]
    print(f"ヘッダー: {header_texts}")

    status_idx = next((i for i, t in enumerate(header_texts) if "ステータス" in t), None)
    label_idx  = next((i for i, t in enumerate(header_texts) if "ラベル"    in t), None)

    rows = await page.query_selector_all("tbody tr")
    count = 0

    if status_idx is not None and label_idx is not None:
        # カラム位置が特定できた場合（正確）
        for row in rows:
            cells = await row.query_selector_all("td")
            if len(cells) <= max(status_idx, label_idx):
                continue
            status_text = await cells[status_idx].inner_text()
            label_text  = await cells[label_idx].inner_text()
            if "診察待ち" in status_text and "直来" in label_text:
                count += 1
    else:
        # フォールバック: 行全体のテキストで判断
        print("警告: ヘッダーが特定できません。行テキスト全体で判断します")
        for row in rows:
            text = await row.inner_text()
            if "診察待ち" in text and "直来" in text:
                count += 1

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
            await page.goto(CLINICS_URL, wait_until="networkidle", timeout=30000)
            await ensure_logged_in(page, context)

            try:
                count = await count_walk_in_waiting(page)
            except RuntimeError:
                # セッション切れ → 再ログイン
                if SESSION_FILE.exists():
                    SESSION_FILE.unlink()
                print("セッション切れ。再ログイン中...")
                await page.goto(CLINICS_URL, wait_until="networkidle", timeout=30000)
                await ensure_logged_in(page, context)
                count = await count_walk_in_waiting(page)

            estimated = count * MINUTES_PER
            print(f"直来 診察待ち: {count}人 → 推定 約{estimated}分")

            return {
                "count": count,
                "estimated_minutes": estimated,
                "updated_at": datetime.now().isoformat(),
                "is_open": True,
                "error": None,
            }

        except Exception as e:
            print(f"エラー: {e}", file=sys.stderr)
            try:
                await page.screenshot(path="debug_screenshot.png")
                print("デバッグ用スクリーンショット → debug_screenshot.png")
            except Exception:
                pass
            return {
                "count": 0,
                "estimated_minutes": 0,
                "updated_at": datetime.now().isoformat(),
                "is_open": True,
                "error": str(e),
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
            "count": 0,
            "estimated_minutes": 0,
            "updated_at": datetime.now().isoformat(),
            "is_open": False,
            "error": None,
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
