"""
fix_reentry2.py — targeted patch for session_stopped_tickers
=============================================================
Run from inside WebullTrader\:
    python fix_reentry2.py

What it changes:
- session_stopped_tickers permanent block → 30-min cooldown via trade_cooldowns
- Keeps all other logic intact
- Makes main.py.bak2 backup first
"""

import shutil, sys
from pathlib import Path

TARGET = Path("main.py")
BACKUP = Path("main.py.bak2")

if not TARGET.exists():
    print("ERROR: main.py not found. Run this from inside WebullTrader/")
    sys.exit(1)

src = TARGET.read_text(encoding="utf-8")
original = src
changes = []

# ── Fix 1: Change the stop-loss callback to use trade_cooldowns instead of session set ──
# Old: session_stopped_tickers.add(ticker.upper())
#      log.info("Re-entry blocked for %s for rest of session (stop loss)", ticker)
# New: trade_cooldowns[ticker.upper()] = time.time()  # 30-min cooldown
#      log.info("Re-entry cooldown set for %s (30 min)", ticker)

OLD_BLOCK = '''            if ticker:
                session_stopped_tickers.add(ticker.upper())
                log.info("Re-entry blocked for %s for rest of session (stop loss)", ticker)'''

NEW_BLOCK = '''            if ticker:
                # 30-min cooldown instead of permanent session block
                trade_cooldowns[ticker.upper()] = time.time()
                log.info("Re-entry cooldown: %s blocked for 30 min (stop loss)", ticker)'''

if OLD_BLOCK in src:
    src = src.replace(OLD_BLOCK, NEW_BLOCK, 1)
    changes.append("  ✓ Replaced permanent session block with 30-min trade_cooldowns entry")
else:
    # Try without exact whitespace — find and replace the key lines
    import re
    pat = re.compile(
        r'(if ticker:\s*\n\s*)session_stopped_tickers\.add\(ticker\.upper\(\)\)\s*\n'
        r'\s*log\.info\("Re-entry blocked[^"]*",\s*ticker\)',
        re.MULTILINE
    )
    def repl(m):
        indent = "                "
        return (
            f"{m.group(1)}"
            f"{indent}# 30-min cooldown instead of permanent session block\n"
            f"{indent}trade_cooldowns[ticker.upper()] = time.time()\n"
            f"{indent}log.info(\"Re-entry cooldown: %s blocked for 30 min (stop loss)\", ticker)"
        )
    new_src = pat.sub(repl, src)
    if new_src != src:
        src = new_src
        changes.append("  ✓ Replaced permanent session block with 30-min trade_cooldowns entry (regex)")

# ── Fix 2: Change the check at scan time to use trade_cooldowns (already 30-min) ──
# Old: if ticker.upper() in session_stopped_tickers:
#          console.print(...)
#          continue
# New: (remove — trade_cooldowns check below already handles this)

import re

# Remove the session_stopped_tickers check block in the scan loop
OLD_SCAN_BLOCK = re.compile(
    r'\s*# Block tickers stopped out earlier today[^\n]*\n'
    r'\s*if ticker\.upper\(\) in session_stopped_tickers:\s*\n'
    r'\s*console\.print\([^)]+\)\s*\n'
    r'\s*\)\s*\n'
    r'\s*continue\s*\n',
    re.MULTILINE
)
new_src = OLD_SCAN_BLOCK.sub('\n', src)
if new_src != src:
    src = new_src
    changes.append("  ✓ Removed session_stopped_tickers check in scan loop (trade_cooldowns handles it)")

# Also handle single-line console.print version
OLD_SCAN2 = re.compile(
    r'[ \t]*# Block tickers stopped out earlier today[^\n]*\n'
    r'[ \t]*if ticker\.upper\(\) in session_stopped_tickers:\n'
    r'(?:[ \t]+[^\n]*\n)*?'   # any lines indented inside
    r'[ \t]+continue\n',
    re.MULTILINE
)
new_src = OLD_SCAN2.sub('', src)
if new_src != src:
    src = new_src
    changes.append("  ✓ Removed session_stopped_tickers scan block (alt pattern)")

# ── Fix 3: Change the listener/Discord check too ──────────────────────────────
OLD_DISC = re.compile(
    r'[ \t]*if _tk\.upper\(\) in session_stopped_tickers:\s*\n'
    r'[ \t]+continue\s*\n',
    re.MULTILINE
)
new_src = OLD_DISC.sub('', src)
if new_src != src:
    src = new_src
    changes.append("  ✓ Removed session_stopped_tickers check in Discord signal handler")

# ── Fix 4: Also remove the listener.send_status_update for the old block message ──
OLD_LISTENER_MSG = re.compile(
    r'[ \t]*if listener:\s*\n'
    r'[ \t]+listener\.send_status_update\(\s*\n'
    r'[ \t]+f["\'].*RE-ENTRY BLOCKED.*\n'
    r'[ \t]+f["\'].*no new signals.*\n'
    r'[ \t]+\)\s*\n',
    re.MULTILINE
)
new_src = OLD_LISTENER_MSG.sub('', src)
if new_src != src:
    src = new_src
    changes.append("  ✓ Removed 'RE-ENTRY BLOCKED' Discord notification (no longer needed)")

# ── Apply ─────────────────────────────────────────────────────────────────────
if src == original:
    print("\n⚠️  No changes made — patterns not matched exactly.")
    print("   Your main.py may already be patched.")
    print("   Check manually: grep -n 'session_stopped_tickers' main.py")
    sys.exit(0)

shutil.copy(TARGET, BACKUP)
print(f"\n✓ Backup: {BACKUP}")
TARGET.write_text(src, encoding="utf-8")
print(f"✓ Patched with {len(changes)} change(s):\n")
for c in changes:
    print(c)
print("\n✅ Done — restart main.py.")
print("   TQQQ will now be on 30-min cooldown after a stop loss.")
print("   Bot will immediately scan other tickers.")
