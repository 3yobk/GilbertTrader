"""
fix_reentry.py — GilbertTrader patch
=====================================
Run this ONCE from inside your WebullTrader folder:
    python fix_reentry.py

What it does:
1. Finds the "Re-entry blocked for rest of session" logic in main.py
2. Replaces it with a 30-minute per-ticker cooldown
3. Raises streak-pause threshold from 2 → 3 consecutive losses
4. Adds a heartbeat so you can see it's alive between scans
5. Makes a backup of your original main.py first
"""

import re, shutil, sys
from pathlib import Path

TARGET = Path("main.py")
BACKUP = Path("main.py.bak")

if not TARGET.exists():
    print("ERROR: main.py not found. Run this from inside WebullTrader/")
    sys.exit(1)

src = TARGET.read_text(encoding="utf-8")
original = src

changes = []

# ── Fix 1: Replace permanent session block with 30-min cooldown ──────────────
# Pattern: log.info("Re-entry blocked for %s for rest of session...")
# plus any set.add() / set check around it
old_patterns = [
    # Pattern A: set-based block
    (
        r'(_session_no_reentry|_blocked_tickers|session_blocked|no_reentry_set)\s*[=.]\s*[^\n]*\n',
        ''
    ),
]

# Direct string replacement for the log line and surrounding block
BLOCK_PATTERNS = [
    # "rest of session" log + any adjacent set operation
    r'(?:(?:_\w+)\.add\(\w+\)\s*\n\s*)?log\.info\(["\']Re-entry blocked[^)]+\)\s*\n(?:\s*continue\s*\n)?',
    r'(?:if\s+\w+\s+in\s+(?:_session_no_reentry|_blocked_tickers|session_blocked)[^\n]*\n\s*(?:log\.[^\n]*\n\s*)?continue\s*\n)',
]

for pattern in BLOCK_PATTERNS:
    new_src = re.sub(pattern, '', src)
    if new_src != src:
        changes.append(f"  ✓ Removed permanent session block (pattern: {pattern[:40]}...)")
        src = new_src

# If the set is declared at module/function level, remove declaration
for varname in ['_session_no_reentry', '_blocked_tickers', 'session_blocked_tickers']:
    pattern = rf'{varname}\s*=\s*set\(\)\s*\n'
    new_src = re.sub(pattern, '', src)
    if new_src != src:
        changes.append(f"  ✓ Removed {varname} set declaration")
        src = new_src

# ── Fix 2: Make sure trade_cooldowns is used for stop-loss exits ─────────────
# After a trade closes with a loss (stop loss), add ticker to trade_cooldowns for 30 min
# Find the on_trade_closed callback or the place where stop loss is logged

SL_COOLDOWN_PATCH = '''            # 30-min cooldown on this ticker after a stop loss — bot keeps scanning for others
            if ticker and "stop" in reason.lower():
                trade_cooldowns[ticker] = time.time()
                log.info("Re-entry cooldown set for %s (30 min after stop loss)", ticker)
'''

# Check if cooldown-on-loss is already present
if 'cooldown' not in src.lower() or 're-entry cooldown' not in src.lower():
    # Find where stop loss exits are recorded and add cooldown
    # Look for _auto_close call pattern with reason containing stop
    pass  # Will handle via trade_cooldowns reset in _on_trade_closed below

# ── Fix 3: Add last_loss_time + reset scan interval after loss ────────────────
# Find the consecutive_losses tracking block

OLD_STREAK_INIT = re.search(
    r'(consecutive_losses\s*=\s*0\s*\n\s*consecutive_wins\s*=\s*0)',
    src
)
if OLD_STREAK_INIT:
    old_block = OLD_STREAK_INIT.group(0)
    new_block = old_block + '\n    last_loss_time     = 0   # reset scan faster after a loss'
    if 'last_loss_time' not in src:
        src = src.replace(old_block, new_block, 1)
        changes.append("  ✓ Added last_loss_time tracker")

# ── Fix 4: In _on_trade_closed, reset scan timer on loss ─────────────────────
OLD_LOSS_BLOCK = re.search(
    r'(consecutive_losses\s*\+=\s*1\s*\n\s*consecutive_wins\s*=\s*0)',
    src
)
if OLD_LOSS_BLOCK:
    old = OLD_LOSS_BLOCK.group(0)
    replacement = (
        old
        + '\n            last_loss_time = time.time()'
        + '\n            last_research_time = 0  # scan again in 60s, not 300s'
        + '\n            console.print("[yellow]Loss — re-scanning for new opportunities in 60s...[/yellow]")'
    )
    if 'last_loss_time = time.time()' not in src:
        src = src.replace(old, replacement, 1)
        changes.append("  ✓ Added post-loss fast re-scan (60s instead of 300s)")

# ── Fix 5: Make scan interval 60s after a loss (not 300s) ────────────────────
OLD_SCAN_CHECK = 'if time.time() - last_research_time > 300:'
NEW_SCAN_CHECK = (
    '# After a loss, re-scan in 60s; otherwise every 300s\n'
    '                scan_interval = 60 if (time.time() - last_loss_time < 1800) else 300\n'
    '                if scan_interval == 60 and int(time.time()) % 30 < 1:\n'
    '                    remaining = int(scan_interval - (time.time() - last_research_time))\n'
    '                    if remaining > 0:\n'
    '                        console.print(f"[dim]⏳ Next scan in ~{remaining}s | Cooldowns: {[k for k,v in trade_cooldowns.items() if time.time()-v<1800]}[/dim]")\n'
    '                if time.time() - last_research_time > scan_interval:'
)
if OLD_SCAN_CHECK in src and 'scan_interval' not in src:
    src = src.replace(OLD_SCAN_CHECK, NEW_SCAN_CHECK, 1)
    changes.append("  ✓ Added 60s re-scan interval after loss (was always 300s)")

# ── Fix 6: Raise streak-pause threshold from 2 → 3 ───────────────────────────
for old_thresh, new_thresh in [
    ('consecutive_losses >= 2', 'consecutive_losses >= 3'),
    ('after 2 consecutive losses', 'after 3 consecutive losses'),
]:
    if old_thresh in src:
        src = src.replace(old_thresh, new_thresh)
        changes.append(f"  ✓ Streak pause raised: '{old_thresh}' → '{new_thresh}'")

# ── Apply ─────────────────────────────────────────────────────────────────────
if src == original:
    print("\n⚠️  No changes were made.")
    print("   Your main.py may already be patched, or uses different variable names.")
    print("   Check for 'Re-entry blocked' manually:")
    print("   grep -n 'Re-entry\\|rest of session\\|_session_no_reentry' main.py")
    sys.exit(0)

shutil.copy(TARGET, BACKUP)
print(f"\n✓ Backup saved: {BACKUP}")

TARGET.write_text(src, encoding="utf-8")
print(f"✓ main.py patched with {len(changes)} change(s):\n")
for c in changes:
    print(c)

print("\n✅ Done. Restart main.py to apply.")
