# iPhone Duo pickup alert (NYC)

A personal alert that pushes to my phone when a **512GB iPhone Duo** (Night Sky preferred, Star
White also fine) becomes available for **in-store pickup** at any Manhattan Apple Store or Apple
Downtown Brooklyn. It runs for free on GitHub Actions and notifies through [ntfy](https://ntfy.sh).

Alerts only. It never signs in to an Apple account, never adds to a bag, never checks out.

## What it watches

| Config | Part number (US, unlocked) |
|---|---|
| iPhone Duo 512GB Night Sky (preferred, alerted first) | `MK1H4LL/A` |
| iPhone Duo 512GB Star White | `MK1G4LL/A` |

Stores: Fifth Avenue (R095), Grand Central (R415), West 14th Street (R250), SoHo (R032),
World Trade Center (R654), Upper West Side (R251), Upper East Side (R582), Downtown Brooklyn (R715).
Williamsburg, Queens Center, Bay Plaza and American Dream also come back from Apple and are ignored.

All eight iPhone Duo part numbers are in `ALL_PARTS` in `monitor.py`, each checked against the
product title Apple's own endpoint returns for it. To watch other configs, edit the `WATCH` line.

## How it works

```
GET https://www.apple.com/shop/retail/pickup-message?pl=true&location=10001&parts.0=MK1H4LL/A&parts.1=MK1G4LL/A
```

Apple returns the 12 stores nearest ZIP 10001, each with
`partsAvailability[<part>].pickupDisplay` = `available` / `unavailable`. One request every 75 s
(plus 0-3 s jitter; `INTERVAL` in the workflow, never below 30), 7 AM to 10 PM Eastern.
Apple rate-limits this endpoint at roughly 60 requests an hour: at 30 s and at 40 s it answered
HTTP 541 for ~30 minutes after every ~60 checks, leaving the monitor blind 40-45% of the time.

- **Stock alert** (ntfy priority 5, *urgent*): sent once when a (store, config) pair flips from
  unavailable to available. One push per config listing the store(s), with a Buy button and a
  tap-through link to that config's apple.com buy page. If ntfy is down, it retries on the next check.
- **Everything else is muted by default** (`STOCK_ALERTS_ONLY=1`): your phone only ever gets
  "in stock" pushes. These are written to the Actions log instead, and come back with
  `STOCK_ALERTS_ONLY=0` in the workflow's `env`:
  - *Sold out* (priority 3): after two consecutive "unavailable" answers, with how long it lasted.
  - *Monitor is failing* (priority 4): after 5 failed checks in a row, at most once per day,
    then *recovered* (priority 2) when Apple answers again.
  - *Daily heartbeat* (priority 1, silent): "monitor is watching" on the first good check of the day.
  
  With these muted, a broken monitor is silent: glance at the Actions tab now and then.
- A failed, blocked (HTTP 541/403), non-JSON or partial response is **unknown**, never "out of
  stock": it can't trigger a sold-out message or a duplicate alert.
- **State** (`state.json`: what's been alerted, failure streak) is saved after every check and
  carried between runs with the Actions cache, so a restart doesn't re-alert. If there's been no
  good check for 15+ minutes (overnight, outages), old stock is forgotten and anything still
  available is announced again: a possible duplicate beats a missed restock.

### Hosting

`.github/workflows/monitor.yml` is one long-running job. GitHub caps jobs at 6 hours, so each run
exits after ~5 h 45 m and immediately dispatches the next. The `monitor` concurrency group
guarantees two copies never overlap: the new run waits as *pending* and starts the moment the
old one finishes. The morning start comes from [cron-job.org](https://cron-job.org), which calls
GitHub's `workflow_dispatch` API every 20 min from 6:00 to 9:40 AM ET (a run that starts before
7 AM waits for 7:00; later calls while a run is going just queue and get replaced). GitHub's own
cron entries stay as an extra backstop. Runs triggered outside 7 AM-10 PM ET exit in seconds, and
pending runs replaced by newer ones show as *cancelled*. Both are expected.

The repo is public so Actions minutes are free. The only secret is `NTFY_TOPIC`.

## Setup

1. **Secret:** Settings → Secrets and variables → Actions → New repository secret.
   Name `NTFY_TOPIC`, value: a long random topic name (anyone who knows it can read your alerts).
2. **Phone:** install [ntfy](https://apps.apple.com/app/ntfy/id1625396347), tap **+**, enter the
   topic, keep the server as `ntfy.sh`, Subscribe. In iOS Settings → Notifications → ntfy, allow
   notifications, turn on **Time Sensitive Notifications**, and add ntfy to your Focus modes'
   allowed apps.
3. **Verify:** Actions → *verify* → Run workflow. You should get a test push plus a simulated
   restock (`[TEST] ... in stock for pickup`).
4. **Start:** Actions → *monitor* → Run workflow (or wait for the next morning trigger).
5. **Morning trigger (cron-job.org):** a job that sends `POST
   https://api.github.com/repos/jacobabadi/iphone-duo-pickup-alert/actions/workflows/monitor.yml/dispatches`
   with body `{"ref":"main"}` and headers `Accept: application/vnd.github+json`,
   `X-GitHub-Api-Version: 2022-11-28`, `Authorization: Bearer <token>`. The token is a GitHub
   fine-grained personal access token for this repo only, with **Actions: Read and write**.
   GitHub answers `204` on success. Schedule: America/New_York, hours 6-9, minutes 0/20/40.

## Commands

```sh
python3 monitor.py --once          # one check: HTTP status, stores, availability table
python3 monitor.py --check-links   # buy-page links resolve to the right configs
python3 monitor.py --simulate      # scripted restock through the real logic: exactly one alert
python3 monitor.py --test-notify   # one test push (needs NTFY_TOPIC)
python3 -m unittest -v test_monitor
```

## Plan B: run it on a Mac (if Apple starts blocking GitHub's runners)

Apple has blocked datacenter IPs before. If you get "monitor is failing" alerts with `HTTP 541`
or `HTTP 403` that don't clear, run it from home instead. The Mac must stay awake: plugged in,
lid open. Turn the GitHub workflow off first so you don't get duplicate alerts
(Actions → monitor → ⋯ → Disable workflow).

```sh
git clone https://github.com/jacobabadi/iphone-duo-pickup-alert.git ~/iphone-duo-pickup-alert
```

```sh
python3 ~/iphone-duo-pickup-alert/monitor.py --once
```

(If macOS offers to install the command line developer tools, accept, then rerun.)

```sh
read -rs "NTFY_TOPIC?Paste your ntfy topic: "; echo
```

```sh
cat > ~/Library/LaunchAgents/local.iphone-duo-pickup-alert.plist <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.iphone-duo-pickup-alert</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/caffeinate</string><string>-i</string>
    <string>/usr/bin/python3</string><string>$HOME/iphone-duo-pickup-alert/monitor.py</string>
    <string>--forever</string>
    <string>--state</string><string>$HOME/iphone-duo-pickup-alert/state.json</string>
  </array>
  <key>EnvironmentVariables</key><dict><key>NTFY_TOPIC</key><string>$NTFY_TOPIC</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/iphone-duo-pickup-alert/monitor.log</string>
  <key>StandardErrorPath</key><string>$HOME/iphone-duo-pickup-alert/monitor.log</string>
</dict></plist>
EOF
```

```sh
chmod 600 ~/Library/LaunchAgents/local.iphone-duo-pickup-alert.plist
```

```sh
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/local.iphone-duo-pickup-alert.plist
```

```sh
tail -f ~/iphone-duo-pickup-alert/monitor.log
```

To stop it:

```sh
launchctl bootout gui/$(id -u)/local.iphone-duo-pickup-alert
```

## Caveats

- Unofficial, undocumented endpoint; Apple can change or block it at any time. The failing alert
  and the daily heartbeat are there so you find out.
- Do **not** use `/shop/fulfillment-messages`: it returns HTTP 541 to non-browser clients.
- GitHub's cron is best-effort and, on this repo, never fired at all. That's why each run chains
  the next one itself and cron-job.org handles the morning start.

## Credits

Adapted from [bilune/apple-pickup-monitor](https://github.com/bilune/apple-pickup-monitor)
(MIT, © 2026 Gonzalo Bilune). MIT licensed, see [LICENSE](LICENSE).
