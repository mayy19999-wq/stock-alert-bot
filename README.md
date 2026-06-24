# Stock Alert Bot

Scans ~50 volatile small-cap stocks every 30 minutes during market hours (9:30–16:00 ET).
Fires a Telegram alert when **RSI < 40 AND RVOL > 2×**, capped at 4 alerts per day
(highest-scoring signals win).

## Alert format

```
🚨 ALERT: $GME
Price:     $18.42 (-3.1%)
RSI:       32.5
RVOL:      3.2x
Score:     24.0
Direction: ↓ Bearish
```

**Score** = `(40 − RSI) × RVOL` — rewards deeply oversold + unusually heavy volume.  
**Direction** compares today's price to 5 trading days ago.

---

## Prerequisites

- Python 3.11+
- A Telegram bot token ([create one via @BotFather](https://t.me/BotFather))
- Your Telegram chat ID (see below)

### Get your chat ID

1. Start a conversation with your bot on Telegram.
2. Send any message to it.
3. Open this URL in a browser (replace `<TOKEN>` with your bot token):
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
4. Look for `"chat":{"id":XXXXXXX}` — that number is your `TELEGRAM_CHAT_ID`.

---

## Local setup

```bash
git clone <repo-url>
cd stock-alert-bot

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID

python bot.py
```

The bot runs an immediate scan on startup, then repeats every 30 minutes during
market hours. Stop it with `Ctrl+C`.

---

## Deploy to Render (free tier)

1. Push this repo to GitHub.
2. Go to [render.com](https://render.com) → **New** → **Blueprint**.
3. Connect your repo — Render auto-detects `render.yaml` and creates a **worker** service.
4. In the Render dashboard, open the service → **Environment** and set:
   - `TELEGRAM_BOT_TOKEN` — your bot token
   - `TELEGRAM_CHAT_ID` — your chat ID
5. Click **Deploy**.

> **Note:** Render's free worker tier shares 750 compute hours/month across your free
> services. The bot runs ~143 hours/month during market hours, well within the limit.
> If Render suspends the worker for inactivity, re-deploy or upgrade to the Starter plan.

---

## Configuration

| Env var | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | **Required.** Bot token from @BotFather. |
| `TELEGRAM_CHAT_ID` | — | **Required.** Target chat/group ID. |
| `MAX_ALERTS_PER_DAY` | `4` | Max alerts sent per calendar day. |
| `RSI_THRESHOLD` | `40` | Alert when RSI is below this value. |
| `RVOL_THRESHOLD` | `2.0` | Alert when RVOL is above this multiple. |
| `WATCHLIST` | *(built-in 50)* | Comma-separated tickers to scan instead of the default list. |

### Custom watchlist example

```
WATCHLIST=TSLA,NVDA,AMD,SMCI,MSTR
```

---

## How it works

1. **RVOL** (Relative Volume) — compares today's pace-adjusted volume to the 20-day
   average daily volume. At 10:00 AM (1 hour into a 6.5-hour day), if a stock has
   already done 30% of its average daily volume, it's on pace for `0.30 / (60/390) ≈ 1.95×`.

2. **RSI** uses Wilder's smoothing (EWM) over 14 daily closes.

3. **Score** = `(RSI_THRESHOLD − RSI) × RVOL`. A stock at RSI 30 with RVOL 4× scores
   `(40−30) × 4 = 40`.

4. Each ticker alerts at most once per day. If multiple signals qualify in the same
   scan, only the top-scoring ones fill the remaining daily slots.
