#!/usr/bin/env python3
"""
Binance Whale Tracker - v3.0 (October 2025)
Author: Carmine Sanges

This script connects to the Binance WebSocket stream for a given trading pair
to monitor and analyze real-time whale activity. It identifies patterns of
accumulation and distribution, prints alerts for large trades, and can send
optional notifications via Telegram.

The script is fully configurable via the constants in the CONFIG section.
"""

import asyncio
import json
import time
import math
import signal
import os
from collections import deque, Counter
from typing import Deque, Dict, Tuple, Optional

import requests
import websockets

# ====== COLORS (Fallback if colorama is not installed) ======
try:
    from colorama import init as colorama_init, Fore, Style
    colorama_init(autoreset=True)
    GREEN = Fore.GREEN
    RED = Fore.RED
    YELLOW = Fore.YELLOW
    CYAN = Fore.CYAN
    BOLD = Style.BRIGHT
    RESET = Style.RESET_ALL
except ImportError:
    GREEN = RED = YELLOW = CYAN = BOLD = RESET = ""

# ================== CONFIG ==================
# -- Market and Analysis --
SYMBOL = "SOLUSDC"  # The trading pair to monitor (e.g., "BTCUSDC", "ETHUSDC")
WINDOW_SEC = 60     # The time window in seconds for trade analysis.
POLL_SEC = 3        # How often to evaluate and print the status (in seconds).
MIN_TRADES = 20     # Minimum number of trades required in the window for a valid analysis.

# -- Trade Size and Repetition --
MIN_NOTIONAL_USDC = 1.0     # Ignore trades smaller than this USD value.
SIZE_PRECISION = 2          # How many decimal places to round trade quantities to for repetition analysis.
REPEAT_FRAC = 0.08          # A size is considered "repeating" if it makes up this fraction of trades.
REPEAT_MIN = 5              # A size is also "repeating" if it appears at least this many times.

# -- Whale Alert Thresholds --
BIG_TRADE_THRESHOLD_USDC = 50_000.0  # Threshold for a single trade to be flagged as a "whale" trade.
K2_MULTIPLIER = 3.0                  # Multiplier for the dynamic threshold (k2).
DELTA_WHALE_MIN_USDC = 30_000.0      # Minimum net USD flow required for an Accumulation/Distribution signal.

# -- Alert Debouncing --
ALERT_DEBOUNCE_MS = 7000  # Minimum time in milliseconds between high-conviction or state-change alerts.
# ============================================

# ============== TELEGRAM CONFIG ==============
# Set to True and fill in the details below to enable Telegram alerts.
TELEGRAM_ENABLED = False  # CHANGE THIS TO True IF YOU WANT ALERTS

# Enter your bot token from @BotFather here.
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

# Enter your numerical chat_id here (positive for DMs, negative like -100... for groups).
TELEGRAM_CHAT_ID = 0

# Minimum time in milliseconds between Telegram alerts of the same type.
TELEGRAM_DEBOUNCE_MS = 5000
# ============================================


def get_current_hms() -> str:
    """Returns the current time as HH:MM:SS."""
    return time.strftime("%H:%M:%S", time.localtime())


# ---------- COMPACT FORMATTING (ENGLISH) ----------
def _format_k(value: float) -> str:
    """Formats a number into a compact k/M string, e.g., 1234 -> +1k."""
    s = int(round(abs(value)))
    sign = "+" if value > 0 else "-" if value < 0 else ""
    if s >= 1_000_000:
        return f"{sign}{s//1_000_000}M"
    if s >= 1_000:
        return f"{sign}{s//1_000}k"
    return f"{sign}{s}"


def _get_state_label(state: str) -> str:
    if state == "ACCUMULATION": return "Accumulation"
    if state == "DISTRIBUTION": return "Distribution"
    return "Neutral"


def _get_state_color(state: str) -> str:
    if state == "ACCUMULATION": return GREEN
    if state == "DISTRIBUTION": return RED
    return YELLOW


def format_short_line(result: Dict, show_mu_sigma: bool = False) -> str:
    """
    Formats the analysis result into a single compact line.
    Example: hh:mm:ss | SOLUSDC  | Status: Accumulation | Δ:+33k | k2:+2k | rpt:✔ | top_size: 0.07×17 | n:78
    """
    state = result.get('status', 'NEUTRAL')
    delta = result.get('net_usdc', 0.0)
    k2 = result.get('k2', 0.0)
    n = result.get('n', 0)
    is_repeat = "✔" if result.get('repeat', False) else "·"
    top_sizes = result.get('top_sizes', [])
    top1_str = f"{top_sizes[0][0]}×{top_sizes[0][1]}" if top_sizes else "-"

    state_text = _get_state_label(state)
    state_color = _get_state_color(state)
    delta_color = GREEN if delta > 0 else RED if delta < 0 else RESET

    hhmmss = get_current_hms()
    sym_str = f"{SYMBOL:<8}"
    state_str = f"{state_text:<13}"[:13]
    delta_str = f"{_format_k(delta):>6}"
    k2_str = f"{_format_k(k2):>5}"
    top_str = f"{top1_str:<10}"[:10]
    n_str = f"{n:>3}"

    core = (f"{hhmmss} | {BOLD}{sym_str}{RESET} | {state_color}Status: {state_str}{RESET} | "
            f"Δ:{delta_color}{delta_str}{RESET} | k2:{k2_str} | rpt:{is_repeat} | top_size: {top_str} | n:{n_str}")

    if show_mu_sigma:
        mu = result.get('mu', 0.0)
        sigma = result.get('sigma', 0.0)
        core += f" | μ:{mu} σ:{sigma}"
    return core


def print_legend():
    """Prints the script's legend."""
    print(f"{CYAN}Legend{RESET}: {GREEN}Accumulation{RESET}, {RED}Distribution{RESET}, {YELLOW}Neutral{RESET}. "
          f"Δ = Net USDC Flow, k2 = Dynamic Threshold, rpt = Size Repetition, top_size = Dominant Trade Size Bucket.")


# -------------- TELEGRAM SENDER (async-safe) --------------
class TelegramSender:
    def __init__(self, enabled: bool, token: str, chat_id: int):
        self.enabled = enabled and bool(token) and chat_id != 0
        self.token = token
        self.chat_id = chat_id
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.last_sent: Dict[str, int] = {}  # key -> timestamp_ms for debouncing

    async def run(self):
        if not self.enabled: return
        while True:
            text = await self.queue.get()
            await asyncio.to_thread(self._send_blocking, text)
            await asyncio.sleep(0.05)  # Slight pacing

    def _send_blocking(self, text: str):
        try:
            url = f"https://api.telegram.org/bot{self.token}/sendMessage"
            payload = {"chat_id": self.chat_id, "text": text, "disable_web_page_preview": True}
            # Low timeouts: if Telegram lags, we don't block the main loop
            requests.post(url, json=payload, timeout=4)
        except Exception:
            pass  # Fail silently for resilience

    async def notify(self, text: str, key: Optional[str] = None, debounce_ms: int = TELEGRAM_DEBOUNCE_MS):
        if not self.enabled: return
        now_ms = int(time.time() * 1000)
        if key:
            last = self.last_sent.get(key, 0)
            if now_ms - last < debounce_ms:
                return
            self.last_sent[key] = now_ms
        await self.queue.put(text)


# ------------------------------------------

class MarketState:
    """Manages trades and signals for a given trading pair (bootstrap + live)."""
    def __init__(self, tg_sender: TelegramSender):
        # Stores (ts_ms, qty, price, is_buy, notional)
        self.trades: Deque[Tuple[int, float, float, bool, float]] = deque()
        self.last_status: str = "NEUTRAL"
        self.last_alert_emit_ms: int = 0
        self.last_big_trade_ms: int = 0
        self.last_big_trade_side: Optional[str] = None  # "BUY" or "SELL"
        self.tg = tg_sender

    def add_trade(self, ts_ms: int, qty: float, price: float, is_buy: bool, min_notional: float = MIN_NOTIONAL_USDC):
        notional = qty * price
        if notional < min_notional:
            return
        self.trades.append((ts_ms, qty, price, is_buy, notional))

        # Prune old trades from the window
        cutoff_ts = ts_ms - WINDOW_SEC * 1000
        while self.trades and self.trades[0][0] < cutoff_ts:
            self.trades.popleft()

        # Check for individual big trades
        if notional >= BIG_TRADE_THRESHOLD_USDC:
            side = "BUY" if is_buy else "SELL"
            color = GREEN if is_buy else RED
            print(f"{BOLD}[{get_current_hms()}] 🐳 {color}{side}{RESET} {SYMBOL} "
                  f"{BOLD}${notional:,.0f}{RESET} @ {price:.2f} (qty {qty:.2f})")
            self.last_big_trade_ms = ts_ms
            self.last_big_trade_side = "BUY" if is_buy else "SELL"

            # Send Telegram alert for the big trade
            key = f"whale_{self.last_big_trade_side}"
            msg = f"🐳 Large {side} on {SYMBOL}\nNotional: ${notional:,.0f}\nPrice: {price:.2f} | Qty: {qty:.2f}\n{time.strftime('%H:%M:%S')}"
            asyncio.create_task(self.tg.notify(msg, key=key))

    def evaluate(self) -> Dict:
        n = len(self.trades)
        if n == 0:
            return {"status": "NEUTRAL", "n": 0, "net_usdc": 0.0, "k2": 0.0, "repeat": False, "top_sizes": []}

        qtys = [q for _, q, _, _, _ in self.trades]
        prices = [p for _, _, p, _, _ in self.trades]
        notionals = [nt for _, _, _, _, nt in self.trades]

        buy_notional = sum(nt for (_, _, _, is_b, nt) in self.trades if is_b)
        sell_notional = sum(nt for (_, _, _, is_b, nt) in self.trades if not is_b)
        net_usdc = buy_notional - sell_notional

        # Price mean/stdev
        mu_p = sum(prices) / n
        var_p = sum((p - mu_p) ** 2 for p in prices) / max(1, n - 1)
        sigma_p = math.sqrt(var_p) if var_p > 0 else 0.0

        # Check for repeated trade sizes
        size_buckets = Counter([round(q, SIZE_PRECISION) for q in qtys])
        top_sizes = size_buckets.most_common(3)
        repeat_signal = any(count >= max(REPEAT_MIN, REPEAT_FRAC * n) for _, count in top_sizes)

        # Dynamic threshold (k2)
        avg_notional = sum(notionals) / n
        k2 = K2_MULTIPLIER * avg_notional

        # Determine status
        status = "NEUTRAL"
        is_strong_signal = abs(net_usdc) >= max(2.0 * k2, DELTA_WHALE_MIN_USDC)
        if repeat_signal and net_usdc > k2 and is_strong_signal:
            status = "ACCUMULATION"
        elif repeat_signal and net_usdc < -k2 and is_strong_signal:
            status = "DISTRIBUTION"

        return {
            "status": status, "n": n, "net_usdc": round(net_usdc, 2),
            "avg_notional": round(avg_notional, 2), "k2": round(k2, 2),
            "mu": round(mu_p, 4), "sigma": round(sigma_p, 4),
            "repeat": repeat_signal, "top_sizes": [(float(s), c) for s, c in top_sizes],
        }


def bootstrap_state(state: MarketState):
    """Fills the initial window with recent trades from the REST API."""
    print(f"{get_current_hms()} | {BOLD}Bootstrap {SYMBOL}{RESET}: fetching latest aggTrades…")
    rest_url = "https://api.binance.com/api/v3/aggTrades"
    params = {"symbol": SYMBOL, "limit": 1000}
    try:
        r = requests.get(rest_url, params=params, timeout=5)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"{get_current_hms()} | {YELLOW}Bootstrap REST error:{RESET}", e)
        return

    now_ms = int(time.time() * 1000)
    min_ts = now_ms - WINDOW_SEC * 1000
    trades = sorted(data, key=lambda x: x.get("T", 0))

    for t in trades:
        ts = int(t.get("T", now_ms))
        if ts < min_ts: continue
        price = float(t.get("p", "0"))
        qty = float(t.get("q", "0"))
        is_buy = not bool(t.get("m", True))  # m=True means maker was seller -> aggressor was buyer
        state.add_trade(ts, qty, price, is_buy, min_notional=MIN_NOTIONAL_USDC)

    # If not enough trades, relax the notional filter to get a baseline
    if len(state.trades) < MIN_TRADES:
        needed = MIN_TRADES - len(state.trades)
        print(f"{get_current_hms()} | {YELLOW}Bootstrap: found {len(state.trades)}/{MIN_TRADES} trades. "
              f"Relaxing filter for ~{needed} more.{RESET}")
        for t in trades:
            if len(state.trades) >= MIN_TRADES: break
            ts = int(t.get("T", now_ms))
            if ts < min_ts: continue
            price = float(t.get("p", "0"))
            qty = float(t.get("q", "0"))
            is_buy = not bool(t.get("m", True))
            state.add_trade(ts, qty, price, is_buy, min_notional=0.1)

    result = state.evaluate()
    print(CYAN + "[BOOT]" + RESET, format_short_line(result, show_mu_sigma=True))


async def evaluator_loop(state: MarketState):
    """Periodically evaluates state, prints output, and sends alerts."""
    while True:
        now_ms = int(time.time() * 1000)
        result = state.evaluate()
        print(format_short_line(result))

        current_status = result.get("status", "NEUTRAL")

        # High-Conviction Alert (coincidence of a recent big trade and current state)
        hca = False
        big_trade_side = state.last_big_trade_side
        if big_trade_side and (now_ms - state.last_big_trade_ms) <= WINDOW_SEC * 1000:
            if big_trade_side == "BUY" and current_status == "ACCUMULATION":
                hca = True
            elif big_trade_side == "SELL" and current_status == "DISTRIBUTION":
                hca = True

        if hca and (now_ms - state.last_alert_emit_ms) >= ALERT_DEBOUNCE_MS:
            tag = "BUY" if big_trade_side == "BUY" else "SELL"
            color = GREEN if big_trade_side == "BUY" else RED
            line = format_short_line(result)
            print(f"{BOLD}{color}High-Conviction ALERT — {tag}{RESET} | " + line)
            state.last_alert_emit_ms = now_ms
            # Send Telegram HCA
            msg = f"🚨 High-Conviction Alert — {tag} {SYMBOL}\n{line}"
            await state.tg.notify(msg, key=f"hca_{tag}")

        # State Change Alert (to highlight transitions)
        if current_status != state.last_status and (now_ms - state.last_alert_emit_ms) >= ALERT_DEBOUNCE_MS:
            tag_color = _get_state_color(current_status)
            line = format_short_line(result)
            print(f"{BOLD}{tag_color}State Change{RESET} | " + line)

            # Send Telegram state change
            status_text = _get_state_label(current_status)
            msg = f"🔁 Status Change for {SYMBOL}: {status_text}\n{line}"
            await state.tg.notify(msg, key=f"state_{status_text.lower()}")
            state.last_status = current_status
            state.last_alert_emit_ms = now_ms

        await asyncio.sleep(POLL_SEC)


async def websocket_loop(state: MarketState):
    """Connects to Binance WebSocket and pushes trades to the state manager."""
    ws_url = f"wss://stream.binance.com:9443/stream?streams={SYMBOL.lower()}@aggTrade"
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=15, ping_timeout=20, max_queue=2000) as ws:
                async for raw_message in ws:
                    msg = json.loads(raw_message)
                    data = msg.get("data", {})
                    if not data or data.get("s") != SYMBOL:
                        continue
                    price = float(data.get("p", "0"))
                    qty = float(data.get("q", "0"))
                    is_buy = not data.get("m", True)  # m=True => maker was seller
                    ts = int(data.get("T", time.time() * 1000))
                    state.add_trade(ts, qty, price, is_buy)
        except Exception as e:
            print(f"{get_current_hms()} | {YELLOW}WS error, reconnecting in 2s...{RESET}", repr(e))
            await asyncio.sleep(2)


async def main():
    """Main function to initialize and run the tracker."""
    print(f"{get_current_hms()} | {BOLD}Whale Tracker started for {SYMBOL}{RESET} | Window: {WINDOW_SEC}s | Poll: {POLL_SEC}s")
    print_legend()

    # Initialize Telegram sender
    tg_sender = TelegramSender(TELEGRAM_ENABLED, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)
    tg_task = asyncio.create_task(tg_sender.run(), name="telegram")

    # Initialize market state manager
    market_state = MarketState(tg_sender)

    # 1) Bootstrap with REST data to fill the initial window
    bootstrap_state(market_state)

    # 2) Run live loops (WebSocket and Evaluator)
    tasks = [
        asyncio.create_task(websocket_loop(market_state), name="ws"),
        asyncio.create_task(evaluator_loop(market_state), name="eval"),
    ]

    # Handle graceful shutdown on CTRL+C
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Signal handlers are not supported on all platforms (e.g., some Windows setups)
            pass

    await stop_event.wait()
    print(f"\n{get_current_hms()} | {BOLD}Shutting down...{RESET}")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    tg_task.cancel()
    await asyncio.gather(tg_task, return_exceptions=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
