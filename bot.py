#!/usr/bin/env python3
"""
Volatile small-cap stock scanner — sends Telegram alerts when
RSI < 40 AND RVOL > 2x, max 4 alerts/day ranked by score.
"""

import asyncio
import logging
import os
from datetime import date, datetime, time
from typing import Optional

import numpy as np
import pandas as pd
import pytz
import yfinance as yf
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Bot
from telegram.error import TelegramError

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
MAX_ALERTS: int = int(os.getenv("MAX_ALERTS_PER_DAY", "4"))
RSI_MAX: float = float(os.getenv("RSI_THRESHOLD", "40"))
RVOL_MIN: float = float(os.getenv("RVOL_THRESHOLD", "2.0"))

# Override via WATCHLIST env var (comma-separated tickers).
_DEFAULT_WATCHLIST: list[str] = [
    "GME",  "AMC",  "SPCE", "NKLA", "WKHS", "GOEV", "HYLN",
    "CHPT", "BLNK", "FCEL", "PLUG", "MVIS", "ACMR", "LMND",
    "MARA", "RIOT", "BITF", "HUT",  "CLSK", "CIFR",
    "HOOD", "SOFI", "AFRM", "OPEN", "PAYO",
    "RBLX", "SKLZ", "DKNG", "PENN",
    "SAVA", "ACAD", "ARDX", "BNGO", "CTIC",
    "NIO",  "XPEV", "VNET",
    "SNDL", "TLRY", "CLOV", "HIMS", "BARK", "IDEX",
    "GSIT", "CXAI", "BTBT", "NCTY", "SOS",  "ZKIN",
]


def _watchlist() -> list[str]:
    custom = os.getenv("WATCHLIST", "").strip()
    if custom:
        return [t.strip().upper() for t in custom.split(",") if t.strip()]
    return _DEFAULT_WATCHLIST


def _rsi(closes: pd.Series, period: int = 14) -> Optional[float]:
    """Wilder's RSI using EWM smoothing."""
    if len(closes) < period + 1:
        return None
    delta = closes.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_g = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_l = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    last_l = avg_l.iloc[-1]
    if last_l == 0:
        return 100.0
    return float(100 - (100 / (1 + avg_g.iloc[-1] / last_l)))


def _rvol(volumes: pd.Series, now_et: datetime) -> Optional[float]:
    """Pace-adjusted today volume divided by 20-day average daily volume."""
    if len(volumes) < 2:
        return None
    today_vol = float(volumes.iloc[-1])
    avg_vol = float(volumes.iloc[:-1].tail(20).mean())
    if avg_vol <= 0 or np.isnan(avg_vol):
        return None
    # Minutes elapsed since 9:30 AM ET (570 = 9 * 60 + 30)
    elapsed = max(1, now_et.hour * 60 + now_et.minute - 570)
    fraction = min(1.0, elapsed / 390)  # 390 min = full 6.5-hr trading day
    return (today_vol / fraction) / avg_vol


def _is_market_hours(now_et: datetime) -> bool:
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now_et.time()
    return time(9, 30) <= t < time(16, 0)


def _scan(watchlist: list[str], now_et: datetime) -> list[dict]:
    try:
        raw = yf.download(
            tickers=watchlist,
            period="26d",
            interval="1d",
            auto_adjust=True,
            progress=False,
            threads=True,
        )
    except Exception as exc:
        log.error("Download error: %s", exc)
        return []

    is_multi = isinstance(raw.columns, pd.MultiIndex)
    signals = []

    for symbol in watchlist:
        try:
            if is_multi:
                closes = raw["Close"][symbol].dropna()
                volumes = raw["Volume"][symbol].dropna()
            else:
                # Single-ticker download has no MultiIndex
                closes = raw["Close"].dropna()
                volumes = raw["Volume"].dropna()

            if len(closes) < 16:
                continue

            rsi = _rsi(closes)
            if rsi is None or rsi >= RSI_MAX:
                continue

            rvol = _rvol(volumes, now_et)
            if rvol is None or rvol < RVOL_MIN:
                continue

            price = float(closes.iloc[-1])
            prev_close = float(closes.iloc[-2])
            score = round((RSI_MAX - rsi) * rvol, 2)

            # 5-day trend for direction signal
            ref = float(closes.iloc[-5]) if len(closes) >= 5 else prev_close
            direction = "↑ Bullish" if price > ref else "↓ Bearish"

            signals.append({
                "ticker": symbol,
                "price": round(price, 2),
                "prev_close": round(prev_close, 2),
                "rsi": round(rsi, 1),
                "rvol": round(rvol, 2),
                "score": score,
                "direction": direction,
            })
        except Exception as exc:
            log.debug("Skip %s: %s", symbol, exc)

    return signals


class _AlertManager:
    """Tracks daily alert count and deduplicates per-ticker alerts."""

    def __init__(self) -> None:
        self._day: date = date.min
        self._count: int = 0
        self._seen: set[str] = set()

    def _reset(self) -> None:
        today = datetime.now(ET).date()
        if today != self._day:
            self._day = today
            self._count = 0
            self._seen.clear()

    @property
    def remaining(self) -> int:
        self._reset()
        return MAX_ALERTS - self._count

    def skip(self, ticker: str) -> bool:
        self._reset()
        return ticker in self._seen

    def record(self, ticker: str) -> None:
        self._reset()
        self._count += 1
        self._seen.add(ticker)


def _format(s: dict) -> str:
    chg = (s["price"] - s["prev_close"]) / s["prev_close"] * 100
    sign = "+" if chg >= 0 else ""
    return (
        f"\U0001f6a8 *ALERT: ${s['ticker']}*\n"
        f"Price:     `${s['price']}` ({sign}{chg:.1f}%)\n"
        f"RSI:       `{s['rsi']}`\n"
        f"RVOL:      `{s['rvol']}x`\n"
        f"Score:     `{s['score']}`\n"
        f"Direction: {s['direction']}"
    )


async def run_scan(bot: Bot, mgr: _AlertManager) -> None:
    now_et = datetime.now(ET)
    if not _is_market_hours(now_et):
        log.info("Outside market hours — skipping.")
        return

    remaining = mgr.remaining
    if remaining <= 0:
        log.info("Daily alert limit reached.")
        return

    wl = _watchlist()
    log.info("Scanning %d tickers at %s ET  (alerts remaining: %d)",
             len(wl), now_et.strftime("%H:%M"), remaining)

    signals = [s for s in _scan(wl, now_et) if not mgr.skip(s["ticker"])]

    if not signals:
        log.info("No qualifying signals.")
        return

    signals.sort(key=lambda s: s["score"], reverse=True)
    to_send = signals[:remaining]
    log.info("Qualified: %d  Sending: %d", len(signals), len(to_send))

    for s in to_send:
        try:
            await bot.send_message(
                chat_id=CHAT_ID,
                text=_format(s),
                parse_mode="Markdown",
            )
            mgr.record(s["ticker"])
            log.info(
                "Alert sent — %s | RSI %.1f | RVOL %.2fx | score %.2f",
                s["ticker"], s["rsi"], s["rvol"], s["score"],
            )
        except TelegramError as exc:
            log.error("Telegram error (%s): %s", s["ticker"], exc)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set.")
    if not CHAT_ID:
        raise SystemExit("TELEGRAM_CHAT_ID is not set.")

    bot = Bot(token=BOT_TOKEN)
    mgr = _AlertManager()

    # Fires at :00 and :30 each hour, Mon–Fri, 9:00–15:30 ET.
    # The 9:00 run is filtered out by _is_market_hours (< 9:30).
    scheduler = AsyncIOScheduler(timezone=ET)
    scheduler.add_job(
        run_scan,
        trigger="cron",
        kwargs={"bot": bot, "mgr": mgr},
        day_of_week="mon-fri",
        hour="9-15",
        minute="*/30",
    )
    scheduler.start()

    log.info("Bot live — every 30 min, Mon–Fri 9:30–16:00 ET")
    log.info("RSI < %.0f | RVOL > %.1fx | max %d alerts/day",
             RSI_MAX, RVOL_MIN, MAX_ALERTS)

    # Immediate scan on cold start so we don't wait up to 30 min.
    await run_scan(bot, mgr)

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
