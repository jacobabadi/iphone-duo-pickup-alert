#!/usr/bin/env python3
"""iPhone Duo in-store pickup alert for Manhattan + Downtown Brooklyn Apple Stores.

Polls Apple's public pickup endpoint every 30 seconds and pushes an ntfy
notification when a (store, config) pair flips from unavailable to available.
Alerts only: it never signs in, never touches a cart, never checks out.

Standard library only, Python 3.9+. Adapted from bilune/apple-pickup-monitor
(MIT License, Copyright (c) 2026 Gonzalo Bilune).

Modes:
  python3 monitor.py                 run the watch loop (what the workflow does)
  python3 monitor.py --once          one check, print every store x config, no alerts
  python3 monitor.py --test-notify   send one test push to NTFY_TOPIC
  python3 monitor.py --simulate      replay a scripted restock through the alert logic
  python3 monitor.py --check-links   confirm each config's buy page loads on apple.com
  python3 monitor.py --forever       never exit; sleep outside store hours (Mac / launchd)
"""

import argparse
import gzip
import json
import os
import random
import signal
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ENDPOINT = "https://www.apple.com/shop/retail/pickup-message"
# 10001 returns the 12 stores nearest Midtown, which includes every Manhattan store.
LOCATION = "10001"

# Every US iPhone Duo part number (same for unlocked and every carrier). Each one was checked
# against the product title Apple's own pickup endpoint returns for it ("iPhone Duo 1TB Night Sky").
ALL_PARTS = {
    "MK1E4LL/A": ("256GB Star White", "256gb-star-white"),
    "MK1F4LL/A": ("256GB Night Sky", "256gb-night-sky"),
    "MK1G4LL/A": ("512GB Star White", "512gb-star-white"),
    "MK1H4LL/A": ("512GB Night Sky", "512gb-night-sky"),
    "MK1J4LL/A": ("1TB Star White", "1tb-star-white"),
    "MK1L4LL/A": ("1TB Night Sky", "1tb-night-sky"),
    "MK1Q4LL/A": ("2TB Star White", "2tb-star-white"),
    "MK1R4LL/A": ("2TB Night Sky", "2tb-night-sky"),
}

# What to watch, most wanted first: alerts that land in the same check go out in this order.
# 512GB only, either color, Night Sky (the dark one) preferred. Up to 8 parts fit in one request.
WATCH = ["MK1H4LL/A", "MK1G4LL/A"]
PARTS = {part: ALL_PARTS[part] for part in WATCH}

# Every Manhattan Apple Store plus Downtown Brooklyn. Everything else in the response is ignored.
STORES = {
    "R095": "Fifth Avenue",
    "R415": "Grand Central",
    "R250": "West 14th Street",
    "R032": "SoHo",
    "R654": "World Trade Center",
    "R251": "Upper West Side",
    "R582": "Upper East Side",
    "R715": "Downtown Brooklyn",
}

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)

NTFY_TOPIC = os.getenv("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

# Never poll faster than this: Apple blocks clients that hammer the endpoint.
MIN_INTERVAL = 30
INTERVAL = max(MIN_INTERVAL, int(os.getenv("INTERVAL", "30")))
TZ = ZoneInfo(os.getenv("STORE_TZ", "America/New_York"))
WINDOW_START, WINDOW_END = (int(h) for h in os.getenv("ACTIVE_HOURS", "7-22").split("-"))

# A pair counts as sold out only after this many consecutive explicit "unavailable" answers,
# so a one-off blip in Apple's data can't cause a "gone" followed by a duplicate alert.
GONE_AFTER_CHECKS = 2
# Consecutive failed checks before the "monitor is failing" alert (sent at most once per day).
FAIL_ALERT_AFTER = 5
# If there has been no successful check for this long, we can't know what happened in the gap,
# so previously seen stock is forgotten and anything still available is announced again.
STALE_AFTER_SECONDS = 15 * 60

PRIORITY_STOCK = 5     # ntfy "urgent": delivered as Time Sensitive on iOS, breaks through Focus
PRIORITY_FAILING = 4   # ntfy "high"
PRIORITY_GONE = 3      # ntfy "default"
PRIORITY_QUIET = 2     # ntfy "low": recovery note
PRIORITY_MIN = 1       # ntfy "min": daily heartbeat, silent

STOP = threading.Event()


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if NTFY_TOPIC:
        msg = str(msg).replace(NTFY_TOPIC, "***")
    print(f"[{ts}] {msg}", flush=True)


def buy_url(part):
    return f"https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-{PARTS[part][1]}-unlocked"


def config_name(part):
    return PARTS[part][0]


def format_duration(seconds):
    if seconds < 60:
        return "under a minute"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"about {minutes} min"
    return f"about {minutes // 60} h {minutes % 60} min"


# --------------------------------------------------------------------------- Apple


class CheckFailed(Exception):
    """The check produced no trustworthy answer. Stock state is unknown, not 'out of stock'."""


def fetch_stores(timeout=20):
    params = [("pl", "true"), ("location", LOCATION)]
    params += [(f"parts.{i}", part) for i, part in enumerate(PARTS)]
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip",
        "Referer": "https://www.apple.com/shop/buy-iphone/iphone-duo",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            fetch_stores.last = (resp.status, resp.headers.get("Content-Type"), len(raw))
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        raise CheckFailed(f"HTTP {exc.code}") from None
    except Exception as exc:
        raise CheckFailed(f"{type(exc).__name__}: {exc}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        snippet = raw[:80].decode("utf-8", "replace").replace("\n", " ")
        raise CheckFailed(f"non-JSON response (bot challenge?): {snippet!r}") from None
    return extract_stores(data)


def extract_stores(data):
    body = data.get("body") if isinstance(data, dict) else None
    stores = body.get("stores") if isinstance(body, dict) else None
    if not isinstance(stores, list):
        reason = body.get("errorMessage") if isinstance(body, dict) else None
        raise CheckFailed(f"no store data in response ({reason or 'unexpected shape'})")
    return stores


def parse_availability(stores):
    """Return ({(store, part): True|False}, set of watched stores seen).

    True = Apple says available, False = Apple says unavailable. Pairs Apple didn't answer
    for are left out entirely: they're unknown, and unknown never counts as sold out.
    """
    observed, seen = {}, set()
    for store in stores:
        number = store.get("storeNumber")
        if number not in STORES:
            continue
        seen.add(number)
        for part, info in (store.get("partsAvailability") or {}).items():
            if part not in PARTS or not isinstance(info, dict):
                continue
            display = info.get("pickupDisplay")
            if display == "available":
                observed[(number, part)] = True
            elif display in ("unavailable", "ineligible"):
                observed[(number, part)] = False
            # Anything else is a shape we don't understand: leave it unknown.
    if len(seen) < len(STORES) / 2:
        raise CheckFailed(f"only {len(seen)}/{len(STORES)} watched stores in response")
    if not observed:
        raise CheckFailed("response had no availability for any watched config")
    return observed, seen


# --------------------------------------------------------------------------- ntfy


def notify(title, message, priority, click=None, actions=None, tags=None):
    """Publish to ntfy. Returns True only if ntfy accepted the message."""
    if not NTFY_TOPIC:
        log(f"(no NTFY_TOPIC) would notify p{priority}: {title} | {message!r}")
        return False
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message, "priority": priority}
    if click:
        payload["click"] = click
    if actions:
        payload["actions"] = actions
    if tags:
        payload["tags"] = tags
    body = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        req = urllib.request.Request(NTFY_SERVER, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                log(f"notified p{priority}: {title}")
                return True
        except urllib.error.HTTPError as exc:
            log(f"ntfy attempt {attempt + 1} failed: HTTP {exc.code}")
        except Exception as exc:
            log(f"ntfy attempt {attempt + 1} failed: {type(exc).__name__}")
        if attempt < 2:
            time.sleep(1 + 2 * attempt)
    return False


# --------------------------------------------------------------------------- state machine


def new_state():
    return {
        "version": 1,
        "last_ok": None,          # epoch of the last successful check
        "pairs": {},              # "R095|MK1E4LL/A" -> {since, last_seen, misses, alerted}
        "failures": 0,            # consecutive failed checks
        "fail_alerted": False,    # a failing alert went out for the current outage
        "fail_alert_day": None,   # local date of the last failing alert
        "heartbeat_day": None,    # local date of the last daily "watching" note
    }


def load_state(path):
    try:
        with open(path) as fh:
            state = json.load(fh)
        if state.get("version") == 1:
            merged = new_state()
            merged.update(state)
            return merged
        log("state file has an unknown version; starting fresh")
    except FileNotFoundError:
        log("no saved state; starting fresh")
    except Exception as exc:
        log(f"could not read state ({exc}); starting fresh")
    return new_state()


def save_state(path, state):
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-")
    with os.fdopen(fd, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def local_day(epoch):
    return datetime.fromtimestamp(epoch, TZ).date().isoformat()


class Monitor:
    def __init__(self, state, send=notify):
        self.state = state
        self.send = send

    def on_success(self, observed, now):
        st = self.state
        if st["last_ok"] is not None and now - st["last_ok"] > STALE_AFTER_SECONDS and st["pairs"]:
            log(f"no data for {int((now - st['last_ok']) / 60)} min; forgetting "
                f"{len(st['pairs'])} previously available pair(s)")
            st["pairs"] = {}

        if st["fail_alerted"]:
            self.send("iPhone Duo monitor recovered",
                      "Apple is answering again. Stock alerts are back on.", PRIORITY_QUIET)
        st["failures"] = 0
        st["fail_alerted"] = False

        pairs = st["pairs"]
        gone = []
        for (store, part), available in observed.items():
            key = f"{store}|{part}"
            if available:
                if key in pairs:
                    pairs[key]["misses"] = 0
                    pairs[key]["last_seen"] = now
                else:
                    pairs[key] = {"since": now, "last_seen": now, "misses": 0, "alerted": False}
            elif key in pairs:
                pairs[key]["misses"] += 1
                if pairs[key]["misses"] >= GONE_AFTER_CHECKS:
                    gone.append((store, part, pairs.pop(key), now))
            # Pairs missing from `observed` are unknown this round: leave them untouched.

        self._send_stock_alerts(observed)
        self._send_gone(gone)
        st["last_ok"] = now

        today = local_day(now)
        if st["heartbeat_day"] != today:
            if self.send("iPhone Duo monitor is watching",
                         f"Checking {len(PARTS)} configs at {len(STORES)} stores every "
                         f"{INTERVAL}s, {WINDOW_START}:00 to {WINDOW_END}:00 Eastern.", PRIORITY_MIN):
                st["heartbeat_day"] = today

    def _send_stock_alerts(self, observed):
        """One push per config, listing every store where it just appeared.

        A pair is marked alerted only after ntfy accepts the push; if ntfy is down the alert
        is retried on the next check instead of being lost.
        """
        pending = {}
        for key, info in self.state["pairs"].items():
            store, part = key.split("|")
            if not info["alerted"] and observed.get((store, part)) is True:
                pending.setdefault(part, []).append(store)
        for part in PARTS:  # stable order
            stores = pending.get(part)
            if not stores:
                continue
            names = [STORES[s] for s in sorted(stores, key=lambda s: STORES[s])]
            url = buy_url(part)
            title = f"iPhone Duo {config_name(part)} in stock for pickup"
            message = f"{', '.join(names)}\nTap to open the buy page:\n{url}"
            actions = [{"action": "view", "label": f"Buy {config_name(part)}", "url": url, "clear": True}]
            if self.send(title, message, PRIORITY_STOCK, click=url, actions=actions,
                         tags=["rotating_light", "iphone"]):
                for s in stores:
                    self.state["pairs"][f"{s}|{part}"]["alerted"] = True

    def _send_gone(self, gone):
        lines = []
        for store, part, info, now in sorted(gone, key=lambda g: (STORES[g[0]], g[1])):
            if info.get("alerted"):  # only report stock you were actually told about
                lasted = format_duration(info["last_seen"] - info["since"] + INTERVAL / 2)
                lines.append(f"{STORES[store]}: {config_name(part)} (lasted {lasted})")
        if lines:
            self.send("Sold out", "\n".join(lines), PRIORITY_GONE, tags=["x"])

    def on_failure(self, reason, now):
        st = self.state
        st["failures"] += 1
        log(f"check failed ({st['failures']} in a row): {reason}")
        today = local_day(now)
        # At most one failing alert per calendar day, even if the outage spans several days.
        if st["failures"] >= FAIL_ALERT_AFTER and st["fail_alert_day"] != today:
            if self.send("iPhone Duo monitor is failing",
                         f"{st['failures']} checks in a row failed (last error: {reason}). "
                         "You will not get stock alerts until Apple answers again. "
                         "This alert won't repeat today.", PRIORITY_FAILING, tags=["warning"]):
                st["fail_alerted"] = True
                st["fail_alert_day"] = today


# --------------------------------------------------------------------------- run loop


def window_bounds(now_local):
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight + timedelta(hours=WINDOW_START), midnight + timedelta(hours=WINDOW_END)


def annotate(level, title, message):
    """Attach a note to the GitHub Actions run (shown on the run page once the step ends)."""
    if os.getenv("GITHUB_ACTIONS") == "true":
        print(f"::{level} title={title}::{message}", flush=True)


def set_github_output(name, value):
    path = os.getenv("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{name}={value}\n")


def run(state_path, max_runtime, wait_for_window, forever=False):
    if not NTFY_TOPIC:
        sys.exit("NTFY_TOPIC is not set; refusing to run without a way to alert you.")
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())

    state = load_state(state_path)
    save_state(state_path, state)
    monitor = Monitor(state)
    deadline = time.time() + max_runtime
    exit_reason = "stopped"

    announced_wait = None
    checks = failed = 0
    while not STOP.is_set():
        now_local = datetime.now(TZ)
        start, end = window_bounds(now_local)
        if not start <= now_local < end:
            next_start = start if now_local < start else start + timedelta(days=1)
            wait = (next_start - now_local).total_seconds()
            early_enough = now_local < start and wait <= wait_for_window and time.time() + wait < deadline
            if not (forever or early_enough):
                exit_reason = "too early" if now_local < start else "window closed"
                break
            if announced_wait != next_start:
                log(f"outside {WINDOW_START}:00-{WINDOW_END}:00 Eastern; next check in {int(wait / 60)} min")
                announced_wait = next_start
            # Short waits, re-checking the clock: on a Mac, a long wait can overshoot after sleep.
            STOP.wait(min(wait, 60))
            continue
        if time.time() >= deadline:
            exit_reason = "max runtime"
            break

        started = time.monotonic()
        checks += 1
        try:
            observed, seen = parse_availability(fetch_stores())
            monitor.on_success(observed, time.time())
            if checks - failed == 1:
                annotate("notice", "Monitor running",
                         f"First check OK at {datetime.now(TZ):%H:%M} ET: {len(seen)}/{len(STORES)} stores, "
                         f"{len(observed)}/{len(STORES) * len(PARTS)} store-config pairs answered.")
            available = sorted(f"{STORES[s]}/{config_name(p)}" for (s, p), a in observed.items() if a)
            log(f"ok stores={len(seen)}/{len(STORES)} pairs={len(observed)} "
                f"available={len(available)} {', '.join(available)}".rstrip()
                + f" ({int((time.monotonic() - started) * 1000)} ms)")
        except CheckFailed as exc:
            failed += 1
            monitor.on_failure(str(exc), time.time())
        except Exception as exc:  # a bug must not kill the loop; it counts as a failed check
            failed += 1
            monitor.on_failure(f"internal error {type(exc).__name__}: {exc}", time.time())
        save_state(state_path, state)
        STOP.wait(max(0.0, started + INTERVAL + random.uniform(0, 3) - time.monotonic()))

    save_state(state_path, state)
    log(f"exiting: {exit_reason}")
    if checks:
        annotate("warning" if failed > checks / 2 else "notice", "Run summary",
                 f"{checks} checks, {failed} failed. Exit reason: {exit_reason}.")
    # Tell the workflow to start the next run right away if we stopped only for the job time limit.
    set_github_output("continue", "true" if exit_reason == "max runtime" else "false")


# --------------------------------------------------------------------------- one-off modes


def run_once():
    try:
        stores = fetch_stores()
    except CheckFailed as exc:
        print(f"CHECK FAILED: {exc}")
        return 1
    status, ctype, size = fetch_stores.last
    print(f"HTTP {status}, {ctype}, {size:,} bytes")
    print(f"Apple returned {len(stores)} stores near {LOCATION}:")
    for s in stores:
        mark = "watching" if s.get("storeNumber") in STORES else "ignored"
        print(f"  {s.get('storeNumber')}  {s.get('storeName', '?'):<24} {mark}")
    try:
        observed, seen = parse_availability(stores)
    except CheckFailed as exc:
        print(f"CHECK FAILED: {exc}")
        return 1
    print(f"\nWatched stores present: {len(seen)}/{len(STORES)}; pairs answered: "
          f"{len(observed)}/{len(STORES) * len(PARTS)}")
    print(f"{'':<20}" + "".join(f"{config_name(p):>18}" for p in PARTS))
    for number, name in STORES.items():
        cells = []
        for part in PARTS:
            v = observed.get((number, part))
            cells.append(f"{'AVAILABLE' if v else ('-' if v is False else '?'):>18}")
        print(f"{name:<20}" + "".join(cells))
    return 0


def check_links():
    ok = True
    for part in PARTS:
        url = buy_url(part)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                html = resp.read().decode("utf-8", "replace")
                final = resp.geturl()
        except Exception as exc:
            print(f"  FAIL {config_name(part):<18} {url}  ({exc})")
            ok = False
            continue
        good = final.rstrip("/") == url and f"iPhone Duo {config_name(part)}" in html
        ok &= good
        print(f"  {'ok  ' if good else 'FAIL'} {config_name(part):<18} {url}"
              + ("" if final.rstrip('/') == url else f"  -> redirected to {final}"))
    return 0 if ok else 1


def test_notify():
    if not NTFY_TOPIC:
        print("NTFY_TOPIC is not set")
        return 1
    sent = notify("iPhone Duo monitor: test", "If you can read this, alerts reach your phone.",
                  PRIORITY_STOCK, click=buy_url(WATCH[0]), tags=["white_check_mark"])
    print("test notification accepted by ntfy" if sent else "test notification FAILED")
    return 0 if sent else 1


def fake_response(available_pairs, missing_stores=()):
    """Build an Apple-shaped response where only `available_pairs` say 'available'."""
    stores = []
    for number, name in list(STORES.items()) + [("R594", "Williamsburg"), ("R612", "Queens Center")]:
        if number in missing_stores:
            continue
        stores.append({"storeNumber": number, "storeName": name, "partsAvailability": {
            p: {"partNumber": p, "pickupDisplay": "available" if (number, p) in available_pairs
                else "unavailable"} for p in PARTS}})
    return {"head": {"status": "200"}, "body": {"stores": stores}}


def simulate(send_for_real):
    """Replay a scripted restock through the real parsing and alert code.

    The Williamsburg/Queens entries prove other stores are filtered out. State is written to
    disk and reloaded mid-way to prove a job restart doesn't re-alert.
    """
    sent = []

    def recorder(title, message, priority, **kw):
        if title == "iPhone Duo monitor is watching":
            return True  # the daily heartbeat isn't part of this scenario
        sent.append((title, message))
        print(f"      -> PUSH p{priority}: {title} | {message.splitlines()[0]}")
        if send_for_real:
            return notify("[TEST] " + title, message, priority, **kw)
        return True

    flip = ("R095", "MK1H4LL/A")       # Fifth Avenue, 512GB Night Sky
    noise = ("R594", "MK1H4LL/A")      # Williamsburg: not watched, must never alert
    script = [
        ("everything unavailable", fake_response(set())),
        ("Fifth Avenue 512GB Night Sky flips to available", fake_response({flip, noise})),
        ("still available", fake_response({flip, noise})),
        ("request blocked (HTTP 541): unknown, not sold out", CheckFailed("HTTP 541")),
        ("RESTART: state saved, process replaced, state reloaded", "restart"),
        ("still available after restart", fake_response({flip})),
        ("Fifth Avenue missing from response: unknown", fake_response({flip}, missing_stores={"R095"})),
        ("one 'unavailable' blip", fake_response(set())),
        ("available again right after the blip", fake_response({flip})),
        ("unavailable (1st)", fake_response(set())),
        ("unavailable (2nd): sold out", fake_response(set())),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "state.json")
        monitor = Monitor(new_state(), send=recorder)
        start = now = time.time()
        for label, event in script:
            if event == "restart":
                print(f"           {label}")
                save_state(path, monitor.state)
                monitor = Monitor(load_state(path), send=recorder)
                continue
            print(f"  t+{int(now - start):>4}s  {label}")
            if isinstance(event, CheckFailed):
                monitor.on_failure(str(event), now)
            else:
                observed, _ = parse_availability(extract_stores(event))
                monitor.on_success(observed, now)
            now += INTERVAL
    stock = [t for t, _ in sent if "in stock" in t]
    gone = [t for t, _ in sent if t == "Sold out"]
    print(f"\n  stock alerts sent: {len(stock)} (expected 1)")
    print(f"  sold-out messages: {len(gone)} (expected 1)")
    print(f"  other messages:    {len(sent) - len(stock) - len(gone)} (expected 0)")
    passed = len(stock) == 1 and len(gone) == 1 and len(sent) == 2
    print("  SIMULATION PASSED" if passed else "  SIMULATION FAILED")
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--send", action="store_true", help="with --simulate: also push [TEST] alerts via ntfy")
    ap.add_argument("--check-links", action="store_true")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--max-runtime", type=int, default=24 * 3600, help="seconds before exiting")
    ap.add_argument("--wait-for-window", type=int, default=90 * 60,
                    help="if started before the window opens, wait up to this many seconds")
    ap.add_argument("--forever", action="store_true",
                    help="never exit; sleep outside the window (for launchd on a Mac)")
    args = ap.parse_args()
    if args.once:
        return run_once()
    if args.test_notify:
        return test_notify()
    if args.simulate:
        return simulate(args.send)
    if args.check_links:
        return check_links()
    run(args.state, 10 ** 9 if args.forever else args.max_runtime, args.wait_for_window, args.forever)
    return 0


if __name__ == "__main__":
    sys.exit(main())
