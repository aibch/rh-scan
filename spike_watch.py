"""Real-time volume-spike watcher with push notifications (standalone).

Polls GeckoTerminal every INTERVAL seconds for the newest + hottest pools on
Robinhood Chain and pushes an alert when a pool's 5-minute volume explodes
relative to its liquidity. Completely separate from the scanner pipeline:
different cadence, different signal, its own state and event log. Needs an
always-on machine (a VPS, a desktop that stays awake) — GitHub Actions cannot
run at minute cadence.

Every alert is also appended to data/spike_events.jsonl so the standing
question — "does any class of spike have positive expectancy?" — keeps
accumulating evidence for the Stage 2/3 analysis.

Notifications (set one or both, else alerts just print):
  NTFY_TOPIC          -> pushes via https://ntfy.sh/<topic>
                         (install the ntfy app, subscribe to your topic —
                          pick something unguessable, it acts as a password)
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID -> pushes via a Telegram bot

Usage:
    python3 spike_watch.py               # watch loop, 60s interval
    python3 spike_watch.py --interval 90
    python3 spike_watch.py --once        # single poll (for testing)

A standing reminder, from this dataset's own spike study: entries at spike
detection showed median -37% at +6h and 61% rugged at +24h. This tool is for
WATCHING and LOGGING, not reflex buying.
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import db
import scanner   # read-only reuse of api_get / NETWORK; nothing is modified

MIN_M5_VOL_USD = 10_000   # 5-minute volume floor
M5_LIQ_RATIO = 0.5        # ...and at least half the pool's liquidity in 5 min
MIN_LIQ_USD = 5_000       # ignore dust pools
COOLDOWN_S = 3600         # one alert per pool per hour
STATE_PATH = os.path.join(db.DATA_DIR, "spike_state.json")
EVENTS_PATH = os.path.join(db.DATA_DIR, "spike_events.jsonl")


def should_alert(m5_vol, liquidity, last_alert_epoch, now_epoch):
    """Pure predicate so it can be unit-tested."""
    if now_epoch - last_alert_epoch < COOLDOWN_S:
        return False
    return (m5_vol >= MIN_M5_VOL_USD
            and liquidity >= MIN_LIQ_USD
            and m5_vol >= M5_LIQ_RATIO * liquidity)


def notify(title, body):
    sent = False
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:
        try:
            req = urllib.request.Request(
                f"https://ntfy.sh/{urllib.parse.quote(topic)}",
                data=body.encode(), headers={"Title": title, "Priority": "high"})
            urllib.request.urlopen(req, timeout=15).read()
            sent = True
        except Exception as e:
            print(f"ntfy failed: {e}")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if token and chat:
        try:
            data = urllib.parse.urlencode(
                {"chat_id": chat, "text": f"{title}\n{body}"}).encode()
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=data, timeout=15).read()
            sent = True
        except Exception as e:
            print(f"telegram failed: {e}")
    if not sent:
        print(f"[ALERT] {title}\n{body}")


def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(db.DATA_DIR, exist_ok=True)
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_PATH)


def log_event(event):
    os.makedirs(db.DATA_DIR, exist_ok=True)
    with open(EVENTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":")) + "\n")


def poll_once(state):
    now = datetime.now(timezone.utc)
    now_epoch = now.timestamp()
    alerts = 0
    for page_path in (f"/networks/{scanner.NETWORK}/new_pools?page=1",
                      f"/networks/{scanner.NETWORK}/pools?page=1",
                      f"/networks/{scanner.NETWORK}/pools?page=2"):
        try:
            data = scanner.api_get(page_path)
        except Exception as e:
            print(f"poll failed ({page_path}): {e}")
            continue
        time.sleep(2.1)
        if not data or not data.get("data"):
            continue
        for pool in data["data"]:
            a = pool["attributes"]
            addr = a["address"].lower()
            m5 = scanner.to_float((a.get("volume_usd") or {}).get("m5")) or 0
            liq = scanner.to_float(a.get("reserve_in_usd")) or 0
            last = state.get(addr, 0)
            if not should_alert(m5, liq, last, now_epoch):
                continue
            state[addr] = now_epoch
            alerts += 1
            move = (a.get("price_change_percentage") or {}).get("m5")
            event = {
                "ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "pool": addr, "name": a.get("name"),
                "m5_vol_usd": m5, "liquidity_usd": liq,
                "price_move_m5_pct": scanner.to_float(move),
                "price_usd": scanner.to_float(a.get("base_token_price_usd")),
            }
            log_event(event)
            notify(
                f"Volume spike: {a.get('name')}",
                f"${m5:,.0f} traded in 5 min against ${liq:,.0f} liquidity"
                f" ({m5 / liq:.1f}x)"
                + (f", price {float(move):+.1f}% (5m)" if move else "") + "\n"
                f"https://www.geckoterminal.com/robinhood/pools/{addr}\n"
                f"Reminder: in this dataset, buying at spike detection ran a "
                f"median -37% at +6h. Watch and log; don't reflex-buy.")
    return alerts


def main():
    ap = argparse.ArgumentParser(description="Real-time volume spike watcher")
    ap.add_argument("--interval", type=int, default=60, metavar="SECONDS")
    ap.add_argument("--once", action="store_true", help="single poll and exit")
    args = ap.parse_args()

    state = load_state()
    if args.once:
        n = poll_once(state)
        save_state(state)
        print(f"poll complete, {n} alert(s)")
        return
    interval = max(30, args.interval)
    print(f"watching every {interval}s — Ctrl-C to stop "
          f"(ntfy={'on' if os.environ.get('NTFY_TOPIC') else 'off'}, "
          f"telegram={'on' if os.environ.get('TELEGRAM_BOT_TOKEN') else 'off'})",
          flush=True)
    while True:
        started = time.time()
        try:
            n = poll_once(state)
            if n:
                save_state(state)
        except Exception as e:
            print(f"poll error: {e}", flush=True)
        time.sleep(max(5, interval - (time.time() - started)))


if __name__ == "__main__":
    main()
