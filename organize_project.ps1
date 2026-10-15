# organize_project.ps1 — GilbertTrader folder cleanup
# Run from inside WebullTrader\ with:  .\organize_project.ps1
# Safe to re-run — only moves files, never deletes anything.

$root = Get-Location

# ── Create folders ─────────────────────────────────────────────────────────────
$folders = @(
    "logs\daily",        # .daily_report_* files
    "logs\premarket",    # .premarket_* files
    "logs\optimizer",    # .optimizer_* files
    "logs\alpha_sieve",  # .alpha_sieve_* files
    "logs\brain",        # brain.log + trade_log.csv + paper_shadow.csv
    "data\cache",        # account_cache.json, *.cache files
    "data\models",       # ml_model*, outcome_tracker*, slippage* data files
    "config",            # .env, .gitignore (stays readable at root too)
    "scripts"            # launch*.bat, watchdog.py helper scripts
)

foreach ($f in $folders) {
    $path = Join-Path $root $f
    if (-not (Test-Path $path)) {
        New-Item -ItemType Directory -Path $path | Out-Null
        Write-Host "  Created: $f" -ForegroundColor DarkGray
    }
}

# ── Move helper: move if exists, skip if already there ────────────────────────
function Move-Safe($src, $dst) {
    if (Test-Path $src) {
        $dest = Join-Path $root $dst
        if (-not (Test-Path $dest)) {
            Move-Item $src $dest
            Write-Host "  Moved: $(Split-Path $src -Leaf) -> $dst" -ForegroundColor Cyan
        }
    }
}

# ── Daily reports ──────────────────────────────────────────────────────────────
Get-ChildItem $root -Filter ".daily_report_*" | ForEach-Object {
    Move-Safe $_.FullName "logs\daily\$($_.Name)"
}

# ── Premarket scans ────────────────────────────────────────────────────────────
Get-ChildItem $root -Filter ".premarket_*" | ForEach-Object {
    Move-Safe $_.FullName "logs\premarket\$($_.Name)"
}

# ── Optimizer logs ─────────────────────────────────────────────────────────────
Get-ChildItem $root -Filter ".optimizer_*" | ForEach-Object {
    Move-Safe $_.FullName "logs\optimizer\$($_.Name)"
}

# ── Alpha sieve logs ───────────────────────────────────────────────────────────
Get-ChildItem $root -Filter ".alpha_sieve_*" | ForEach-Object {
    Move-Safe $_.FullName "logs\alpha_sieve\$($_.Name)"
}

# ── Brain / trade logs ─────────────────────────────────────────────────────────
foreach ($file in @("brain.log", "trade_log.csv", "paper_shadow.csv", "trade_log.json")) {
    Move-Safe (Join-Path $root $file) "logs\brain\$file"
}

# ── Data / cache files ─────────────────────────────────────────────────────────
foreach ($file in @("account_cache.json", "slippage_model.json", "slippage_multipliers.json")) {
    Move-Safe (Join-Path $root $file) "data\cache\$file"
}

# ML model + outcome tracker data files
Get-ChildItem $root -Filter "*.pkl"   | ForEach-Object { Move-Safe $_.FullName "data\models\$($_.Name)" }
Get-ChildItem $root -Filter "*.joblib"| ForEach-Object { Move-Safe $_.FullName "data\models\$($_.Name)" }
Get-ChildItem $root -Filter "outcome_tracker*.csv" | ForEach-Object { Move-Safe $_.FullName "data\models\$($_.Name)" }

# ── Launch scripts ─────────────────────────────────────────────────────────────
foreach ($file in @("_launch_api.bat", "_launch_bot.bat", "START.ps1", "watchdog.py")) {
    # Keep copies at root too so double-clicking still works
    # Just note they exist in scripts\ as well
}
# Actually keep .bat and .ps1 at root — Windows needs them there for double-click
Write-Host "  (launch scripts kept at root for double-click)" -ForegroundColor DarkGray

# ── Update main.py paths to point to new log locations ────────────────────────
# brain.log is the only hardcoded path — update it
$mainPy = Join-Path $root "main.py"
if (Test-Path $mainPy) {
    $content = Get-Content $mainPy -Raw
    $updated = $content `
        -replace '"brain\.log"',      '"logs/brain/brain.log"' `
        -replace "'brain\.log'",      "'logs/brain/brain.log'" `
        -replace '"trade_log\.csv"',  '"logs/brain/trade_log.csv"' `
        -replace "'trade_log\.csv'",  "'logs/brain/trade_log.csv'" `
        -replace '"paper_shadow\.csv"', '"logs/brain/paper_shadow.csv"' `
        -replace "'paper_shadow\.csv'", "'logs/brain/paper_shadow.csv'" `
        -replace '"account_cache\.json"', '"data/cache/account_cache.json"' `
        -replace "'account_cache\.json'", "'data/cache/account_cache.json'"
    if ($updated -ne $content) {
        $content | Set-Content "$mainPy.bak" -Encoding UTF8   # backup first
        $updated | Set-Content $mainPy -Encoding UTF8
        Write-Host "  Updated main.py file paths (backup: main.py.bak)" -ForegroundColor Yellow
    }
}

# Also patch research_scanner.py paths
$scanPy = Join-Path $root "research_scanner.py"
if (Test-Path $scanPy) {
    $content = Get-Content $scanPy -Raw
    $updated = $content `
        -replace '"slippage_model\.json"',      '"data/cache/slippage_model.json"' `
        -replace "'slippage_model\.json'",      "'data/cache/slippage_model.json'" `
        -replace '"slippage_multipliers\.json"', '"data/cache/slippage_multipliers.json"' `
        -replace "'slippage_multipliers\.json'", "'data/cache/slippage_multipliers.json'"
    if ($updated -ne $content) {
        $updated | Set-Content $scanPy -Encoding UTF8
        Write-Host "  Updated research_scanner.py file paths" -ForegroundColor Yellow
    }
}

# ── Summary ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Done! New structure:" -ForegroundColor Green
Write-Host "   WebullTrader/"
Write-Host "   +-- main.py, trade_engine.py, api_server.py  (code stays at root)"
Write-Host "   +-- pixel-perfect/                           (dashboard, unchanged)"
Write-Host "   +-- logs/"
Write-Host "   |   +-- brain/     brain.log, trade_log.csv, paper_shadow.csv"
Write-Host "   |   +-- daily/     .daily_report_* files"
Write-Host "   |   +-- premarket/ .premarket_* files"
Write-Host "   |   +-- optimizer/ .optimizer_* files"
Write-Host "   +-- data/"
Write-Host "       +-- cache/     account_cache.json, slippage files"
Write-Host "       +-- models/    ML model files (.pkl, .joblib)"
Write-Host ""
Write-Host "Restart the bot after this." -ForegroundColor Yellow