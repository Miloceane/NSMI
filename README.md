# Train status light

Tells an ESP32 (or a browser) whether a train is fine (green), delayed (yellow)
or not workable (red), using the NS Reisinformatie API.

## Run

    pip install -r requirements.txt
    NS_API_KEY=... DATA_DIR=./data uvicorn app.main:app --reload

or build the Dockerfile and mount a volume on `/data`.

Environment: `NS_API_KEY` (required), `DATA_DIR` (default `/data`),
`TIMEZONE` (default `Europe/Amsterdam`).

## Devices and login

Every physical device has its own account with its own route (departure +
arrival) and light settings. One person with several devices (home, work)
gets one account per device; `--owner` is just a label. There is no sign-up:
you create the accounts and hand out the token + password.

    python -m app.admin add quin-home --name Home --owner Quin   # prints token + password
    python -m app.admin list
    python -m app.admin passwd quin-home       # new password, logs that device out
    python -m app.admin new-token quin-home    # new token (reflash the ESP32)
    python -m app.admin remove quin-home

In Docker: `docker compose exec train-status python -m app.admin ...`.
Changes are picked up without a restart.

- Web page: log in with the device name (the id, e.g. `quin-home`) + password.
  People can change their own password on the page (min. 8 characters);
  that logs out their other browsers. Sessions last 30 days; 5 wrong
  attempts lock that address out for 10 minutes.
- ESP32: sends only its long random token (never typed by people) to the
  read-only endpoints:

      GET /color  with header  X-Device-Token: <token>
      GET /color?token=<token>

Storage is plain JSON in `DATA_DIR` (no database needed at this scale):
`accounts.json` (passwords are PBKDF2-hashed), `devices/<id>.json` (route +
settings), `session.key` (cookie signing secret, or set `SESSION_SECRET`).
New devices start from the old `route.json` / `config.json` if present.

Put it behind HTTPS if it's reachable from the internet.

## NS rate limit and freshness

NS allows 300 calls per 5 minutes per key. This app uses a small fixed share:
**at most 6 calls per minute in total** (one every 10 s), however many
devices or browsers are polling. Set `NS_CALLS_PER_MINUTE` (1-30) to change it.
The time of the last call is kept on disk so restarts can't cause bursts, and
a 429 from NS pauses all calls for at least 5 minutes.

Requests never call NS; a background worker fetches into a shared cache keyed
by URL (same route = shared trips, one disruption feed for everyone). Calls
go where a wrong colour would hurt most. Each device asks for data this fresh:

| Situation                                           | Max age  |
|-----------------------------------------------------|----------|
| A chosen train leaves in the next 90 min (or just left) | 1 min |
| Following "the next train", 05:00-24:00             | 2 min    |
| Chosen trains but none soon, night, or nobody has polled the device for 10 min | 15 min |

When the budget is tight, the most overdue item (relative to its max age)
goes first. A quiet device that starts being polled again asks for fresh
data right away. If data is older than max(15 min, 2 x max age + 5 min), the
"no data" colour is shown instead of a possibly wrong green.

Rough capacity at 6 calls/min: each route with a train coming up costs
~1 call/min (trips) plus the shared disruption feed, so about 5 routes can be
watched at 1-minute freshness at the same time.
## For the ESP32

- `GET /color`  -> `green`, `yellow` or `red` (plain text, never anything else)
- `GET /status` -> JSON with `color`, `status`, `message`, `watching`, `reliable`

`reliable` is false when NS gave no usable answer and the colour comes from
the "NS can't be reached or has no answer" setting.

## Layout

    app/main.py         HTTP routes
    app/state.py        route/config/status in memory + refresh from NS
    app/evaluate.py     the decision: trips + disruptions + settings -> colour
    app/trips.py        NS trip planner parsing, watched trains
    app/disruptions.py  NS disruption matching on the stops of the route
    app/ns_client.py    NS HTTP calls, station list and search
    app/auth.py         per-device login + device token
    app/accounts.py     device accounts (accounts.json)
    app/admin.py        CLI to manage accounts
    app/scheduler.py    background worker: one NS call per minute
    app/config.py       settings: defaults, validation, persistence
    app/storage.py      atomic JSON files in DATA_DIR
    app/text.py         name normalising / whole-word matching
    app/static/         the web page

## Tests

    pip install pytest
    pytest