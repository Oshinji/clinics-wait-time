# 専用PC用: 最新を取得 → スクレイパー実行 → 変更があれば commit/push
# タスクスケジューラ「ClinicsScraper」から5分ごとに実行される（手順書 9章）
Set-Location $PSScriptRoot
$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [Text.Encoding]::UTF8  # py の出力を正しく読む（ログの文字化け防止）
$log = Join-Path $PSScriptRoot "scraper_run.log"

# ログが1MBを超えたら1世代だけ残してローテーション
if ((Test-Path $log) -and (Get-Item $log).Length -gt 1MB) {
  Move-Item $log "$log.1" -Force
}

function Log($msg) { $msg | Out-File $log -Append -Encoding utf8 }
function Run-Git { git @args 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8; return $LASTEXITCODE }

Log "=== $(Get-Date -Format s) ==="

if ((Run-Git pull --rebase --autostash origin main) -ne 0) {
  git rebase --abort 2>$null
  Log "pull 失敗 → 今回は前回のコードのまま続行"
}

py scraper.py 2>&1 | ForEach-Object { "$_" } | Out-File $log -Append -Encoding utf8

git add data/status.json data/history.json
git diff --staged --quiet
if ($LASTEXITCODE -ne 0) {
  Run-Git -c user.name="scraper-pc" -c user.email="scraper-pc@users.noreply.github.com" `
      commit -m "chore: 待ち時間を更新 [skip ci]" | Out-Null
  if ((Run-Git pull --rebase --autostash origin main) -ne 0) {
    git rebase --abort 2>$null
    Log "push 前の pull 失敗 → 次回に再試行"
  } elseif ((Run-Git push origin HEAD:main) -ne 0) {
    Log "push 失敗 → 次回に再試行"
  }
}
