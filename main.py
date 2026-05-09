"""
main.py — GilbertTrader: AI Brain Trading System (Final Version)
Includes: 1. Risk Profiles, 2. AI Budgeting, 3. Custom Injection, 4. RSI Patching
"""

import requests
import logging
import sys
import argparse
import os
import io
import time
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Core Engine Imports
from webull_engine import WebullEngine
from trade_engine import TradeEngine
from market_feed import MarketFeed
from discord_listener import DiscordListener
import research_scanner

# Windows Terminal UTF-8 Setup
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
console = Console()

# --- FEATURE 1 & 4: RISK PROFILES & RSI LOGIC ---
def get_risk_profile():
    """Interactive menu to choose the 'Strategy Brain' for the session."""
    console.print("\n[bold yellow]🛡️  SELECT RISK PROFILE[/bold yellow]")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Option", style="dim")
    table.add_column("Profile")
    table.add_column("Target (TP)")
    table.add_column("Stop (SL)")
    table.add_column("RSI Trigger")
    table.add_column("Max Trade")

    table.add_row("1", "Safe", "+15%", "-10%", "< 25", "$150")
    table.add_row("2", "Normal", "+30%", "-20%", "< 30", "$300")
    table.add_row("3", "Risky", "+100%", "-40%", "< 35", "$500")
    table.add_row("4", "CUSTOM", "???", "???", "???", "???")

    console.print(table)
    choice = input("\nChoose Profile (1/2/3/4): ").strip()

    # --- FEATURE 3: CUSTOM PARAMETER INJECTION ---
    if choice == "4":
        console.print("\n[bold cyan]🛠️  CUSTOM STRATEGY CONFIGURATION[/bold cyan]")
        tp = float(input("Enter Profit Target % (e.g. 20): "))
        sl = float(input("Enter Stop Loss % (e.g. 15): "))
        rsi = int(input("Enter RSI Trigger Level (0-100): "))
        budget = int(input("Enter Max Trade Budget $: "))
        return {"name": "CUSTOM", "tp": tp, "sl": sl, "rsi": rsi, "budget": budget}
    elif choice == "1":
        return {"name": "SAFE", "tp": 15.0, "sl": 10.0, "rsi": 25, "budget": 150}
    elif choice == "3":
        return {"name": "RISKY", "tp": 100.0, "sl": 40.0, "rsi": 35, "budget": 500}
    else:
        return {"name": "NORMAL", "tp": 30.0, "sl": 20.0, "rsi": 30, "budget": 300}

# --- FEATURE 2: AI BUDGET GUARDRAILS ---
def ask_ai_for_verdict(signal_text, budget):
    """Signals verified by AI PC based on the current profile's budget."""
    AI_PC_URL = "http://192.168.0.15:5000/ask"
    API_KEY = "sk-7d643db8"

    payload = {
        "question": (
            f"Senior Risk Manager Role. INPUT: '{signal_text}'\n"
            f"1. REFORMAT: 'BTO [TICKER] [STRIKE][C/P] [EXPIRY]'\n"
            f"2. BUDGET LIMIT: Max ${budget} for this trade.\n"
            "3. REJECT if strike is >20% away or doesn't fit budget.\n"
            "OUTPUT: ONLY the cleaned BTO string or 'Rejected' + reason."
        )
    }
    headers = {"Authorization": f"Bearer {API_KEY}"}
    try:
        response = requests.post(AI_PC_URL, json=payload, headers=headers, timeout=60)
        return response.json().get("answer", "").strip()
    except Exception:
        return "Rejected: AI PC Offline"

def main():
    profile = get_risk_profile()

    # Visual Confirmation Panel
    console.print(Panel(
        Text.assemble(
            ("GILBERT TRADER SYSTEM ARMED\n", "bold white"),
            (f"Active Strategy: {profile['name']}\n", "bold green"),
            (f"Parameters: TP {profile['tp']}% | SL {profile['sl']}% | RSI <{profile['rsi']} | Budget ${profile['budget']}", "dim"),
        ),
        border_style="bright_blue", padding=(1, 4),
    ))

    # --- Engine Initialization ---
    webull = WebullEngine(paper=True)
    webull.open_browser()

    market = MarketFeed()
    market.start()

    engine = TradeEngine(
        webull=webull, market=market, paper=True,
        profit_target_pct=profile['tp'], stop_loss_pct=profile['sl']
    )

    console.print("\n1. Discord | 2. Research | 3. Hybrid")
    mode_choice = input("\nSelect Operation Mode (1/2/3): ").strip()

    listener = DiscordListener(
        channel_id=int(os.getenv("DISCORD_CHANNEL_ID", "0")),
        token=os.getenv("DISCORD_TOKEN", ""),
        on_signal=lambda x: None
    )
    listener.start_in_thread()

    last_research_time = 0
    trade_cooldowns = {}

    try:
        while True:
            raw_text = None

            # --- Active Research Scanner ---
            if mode_choice in ["2", "3"]:
                if time.time() - last_research_time > 300: # 5 min scan
                    console.print(f"[dim]🔎 Scanning Small-Caps (RSI Threshold: {profile['rsi']})...[/dim]")
                    found = research_scanner.scan_markets(rsi_threshold=profile['rsi'])
                    if found:
                        raw_text = found[0]
                    last_research_time = time.time()

            # --- Passive Discord Listener ---
            if not raw_text and mode_choice in ["1", "3"]:
                raw_text = listener.get_latest_signal()

            # --- Process and Execute ---
            if raw_text:
                parts = raw_text.split()
                ticker = parts[1].upper() if len(parts) > 1 else "UNK"

                # Check 30-minute ticker cooldown
                if ticker in trade_cooldowns and (time.time() - trade_cooldowns[ticker] < 1800):
                    pass
                else:
                    console.print(f"🔍 [cyan]Signal Captured:[/cyan] {raw_text[:50]}")
                    # Send to AI with Profile-Specific Budget
                    verdict = ask_ai_for_verdict(raw_text, profile['budget'])

                    if "BTO" in verdict:
                        console.print(f"✅ [bold green]TRADE APPROVED:[/bold green] {verdict}")
                        trade_cooldowns[ticker] = time.time()
                        engine.on_raw_signal(verdict)
                    else:
                        console.print(f"❌ [bold red]RISK REJECTION:[/bold red] {verdict}")

            time.sleep(1)
            if int(time.time()) % 30 == 0:
                engine.print_status()

    except KeyboardInterrupt:
        console.print("\n[yellow]Shutdown initiated...[/yellow]")
        webull.close()
        market.stop()
        listener.stop()

if __name__ == "__main__":
    main()