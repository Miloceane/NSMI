# Train status light

Tells an ESP32 (or a browser) whether a train is fine (green), delayed (yellow)
or not workable (red), using the NS Reisinformatie API.

## Run

    pip install -r requirements.txt
    NS_API_KEY=... DATA_DIR=./data uvicorn app.main:app --reload

or build the Dockerfile and mount a volume on `/data`.

Environment: `NS_API_KEY` (required), `DATA_DIR` (default `/data`),
`TIMEZONE` (default `Europe/Amsterdam`).

## Login

The web page needs a password. It is hardcoded in `app/auth.py` (`PASSWORD`),
or set the `APP_PASSWORD` environment variable. Sessions last 30 days and
5 wrong attempts lock that address out for 10 minutes.

The ESP32 doesn't log in; it sends a device token (`DEVICE_TOKEN` in
`app/auth.py` or the environment) to the two read-only endpoints:

    GET /color  with header  X-Device-Token: <token>
    GET /color?token=<token>

Change both default values before using this. If it's reachable from the
internet, put it behind HTTPS (the token and password travel in clear text
over plain HTTP).

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
    app/auth.py         password login + device token
    app/config.py       settings: defaults, validation, persistence
    app/storage.py      atomic JSON files in DATA_DIR
    app/text.py         name normalising / whole-word matching
    app/static/         the web page

## Tests

    pip install pytest
    pytest