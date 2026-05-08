"""
main.py  —  AI Brain Trading System · Launch Entry Point

Sequence:
  1. Open Webull web app in a real, stealthy Chromium session
  2. Wait for YOU to log in manually (no credentials stored here)
  3. Arm the Discord listener on your paid signal channel
  4. Start the live market data feed (Polygon.io WebSocket)
  5. Hand every incoming signal to the AI parser + trade engine

Run:
    python main.py              # paper trading (safe default)
    python main.py --real       # real money — prompts for confirmation
"""

import requests
import logging
import sys
from rich.console import Console
from rich.panel import Panel
import argparse
import os
import io
from rich.text import Text
from webull_engine import WebullEngine #
from trade_engine import TradeEngine   #
from market_feed import MarketFeed     #
from discord_listener import DiscordListener #
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

def ask_ai_for_verdict(signal_text):
    # Use the Local IP address of your AI Computer
    AI_PC_URL = "http://192.168.0.15:5000/ask"
    API_KEY = "sk-7d643db8"

    payload = {
        "question": (
            f"Clean this Discord message: '{signal_text}'. "
            "Remove all emojis, 'everyone' tags, and filler words. "
            "Reformat it into this EXACT structure: 'BTO [TICKER] [STRIKE][C/P] [EXPIRY]'. "
            "Example: 'BTO LULU 330C 5/8'. "
            "Also, ensure the trade fits our 30% daily budget rule. "
            "If it's garbage text, just reply 'Rejected'. "
            "Otherwise, reply with the cleaned BTO string."
        )
    }
    headers = {"Authorization": f"Bearer {API_KEY}"}

    try:
        response = requests.post(AI_PC_URL, json=payload, headers=headers, timeout=60)
        data = response.json()
        return data.get("answer", "")
    except Exception as e:
        print(f"AI Computer unreachable: {e}")
        return "Rejected: Connection Error"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    handlers=[
        logging.FileHandler("brain.log"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("main")
console = Console()


def parse_args():
    p = argparse.ArgumentParser(description="AI Brain Trading System")
    p.add_argument("--real", action="store_true",
                   help="⚠️  Trade with real money. Paper mode is the default.")
    p.add_argument("--profit-target", type=float, default=15.0,
                   help="Auto-sell profit target %% (default: 15)")
    p.add_argument("--stop-loss", type=float, default=20.0,
                   help="Auto-sell stop-loss %% (default: 20)")
    p.add_argument("--discord-channel", type=int,
                   default=int(os.getenv("DISCORD_CHANNEL_ID", "0")),
                   help="Discord channel ID to listen on")
    p.add_argument("--discord-token", type=str,
                   default=os.getenv("DISCORD_TOKEN", ""),
                   help="Discord bot/user token (or set DISCORD_TOKEN env var)")
    p.add_argument("--polygon-key", type=str,
                   default=os.getenv("POLYGON_API_KEY", ""),
                   help="Polygon.io API key for live market data")
    return p.parse_args()


def confirm_real_money() -> bool:
    console.print("\n[bold red]⚠️  REAL MONEY MODE[/bold red]")
    console.print("This will place ACTUAL options orders with your Webull account.")
    console.print("Type [bold]CONFIRM REAL MONEY[/bold] to proceed, or anything else to exit.\n")
    answer = input("→ ").strip()
    return answer == "CONFIRM REAL MONEY"


def main():
    args = parse_args()
    paper = not args.real

    # ── Pre-flight checks ─────────────────────────────────────────────────────
    if not paper and not confirm_real_money():
        console.print("[yellow]Aborted. Running in paper mode instead.[/yellow]")
        paper = True

    if not args.discord_token:
        console.print("[red]ERROR: Discord token missing.[/red]\n"
                      "Set DISCORD_TOKEN env var or pass --discord-token")
        sys.exit(1)

    if not args.discord_channel:
        console.print("[red]ERROR: Discord channel ID missing.[/red]\n"
                      "Set DISCORD_CHANNEL_ID env var or pass --discord-channel")
        sys.exit(1)

    # ── Banner ────────────────────────────────────────────────────────────────
    console.print(Panel(
        Text.assemble(
            ("AI BRAIN TRADING SYSTEM\n", "bold white"),
            (f"Mode: {'📄 PAPER' if paper else '💵 REAL MONEY'}\n", "bold yellow" if not paper else "bold cyan"),
            (f"Profit target: +{args.profit_target}%  |  Stop-loss: -{args.stop_loss}%\n", "dim"),
            (f"Discord channel: {args.discord_channel}", "dim"),
        ),
        border_style="bright_blue",
        padding=(1, 4),
    ))

    # ── Step 1: Launch Webull ─────────────────────────────────────────────────
    console.print("\n[bold cyan]Step 1/4[/bold cyan]  Launching Webull in Chromium...")
    webull = WebullEngine(paper=paper)
    webull.open_browser()   # opens browser, goes to Webull, then WAITS for you

    # ── Step 2: Market data feed ──────────────────────────────────────────────
    console.print("\n[bold cyan]Step 2/4[/bold cyan]  Connecting to live market data...")
    market = MarketFeed(api_key=args.polygon_key)
    market.start()
    time.sleep(2)
    console.print(f"  [green]✓[/green] Market feed {'connected' if market.connected else 'using fallback scrape'}")

    # ... Step 3 was here ...
    engine = TradeEngine(
        webull            = webull,
        market            = market,
        paper             = paper,
        profit_target_pct = args.profit_target,
        stop_loss_pct     = args.stop_loss,
    )

    # ── Step 4: Discord listener ──────────────────────────────────────────────
    console.print("\n[bold cyan]Step 4/4[/bold cyan]  Connecting Discord listener...")

    # Define the helper right before creating the listener
    def capture_signal(raw_text):
        listener._latest_raw_message = raw_text

    listener = DiscordListener(
        channel_id = args.discord_channel,
        token      = args.discord_token,
        on_signal  = capture_signal,
    )
    listener.start_in_thread()

    # ── Live ──────────────────────────────────────────────────────────────────
    console.print("\n[bold green]✅  System ARMED.[/bold green]  Waiting for signals...\n")
    # ... the while loop follows ...

    try:
        while True:
            # 1. Get the raw text from Discord
            raw_text = listener.get_latest_signal()

            if raw_text:
                console.print(f"🔍 [cyan]New Message:[/cyan] {raw_text[:50]}...")

                # 2. Ask the AI computer to clean the mess and check the 30% budget
                # This turns "5/8 $LULU 139c @.18 🎯" into "BTO LULU 139C 5/8"
                cleaned_signal = ask_ai_for_verdict(raw_text)

                if "Rejected" not in cleaned_signal and len(cleaned_signal) > 3:
                    console.print(f"🤖 [green]AI Cleaned:[/green] {cleaned_signal}")
                    # 3. Feed the PERFECT text to the engine
                    engine.on_raw_signal(cleaned_signal)
                else:
                    console.print("[yellow]🤖 AI ignored the noise or budget limit hit.[/yellow]")

            time.sleep(1) # Check every second for new Discord messages

            # Optional: Print status every 30 seconds
            if int(time.time()) % 30 == 0:
                engine.print_status()

    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")
        listener.stop()
        market.stop()
        webull.close()
        console.print("[green]Clean exit.[/green]")


if __name__ == "__main__":
    main()
