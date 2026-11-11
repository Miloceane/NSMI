from datetime import datetime
from html import escape
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode
import json
import os
import re
import time
import unicodedata

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Train Status API")

DATA_DIR = Path("/data")
ROUTE_FILE = DATA_DIR / "route.json"

lock = Lock()          # protects route and status
refresh_lock = Lock()  # makes sure only one request calls NS at a time

DEFAULT_ROUTE = {
    "departure": "Utrecht Centraal",
    "arrival": "Amsterdam Centraal",
}

DEFAULT_STATUS = {
    "status": "ok",
    "message": "Running normally",
}

NS_API_KEY = os.getenv("NS_API_KEY")
NS_BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"
NS_MIN_INTERVAL = 60  # seconds between NS checks

# How the answer is decided (all compared with the usual, first upcoming train):
#   arrives < DELAY_MINOR_MIN late  -> ok
#   arrives >= DELAY_MINOR_MIN late -> delayed
#   arrives >= DELAY_MAJOR_MIN late, or no working trip -> cancelled
DELAY_MINOR_MIN = int(os.getenv("DELAY_MINOR_MIN", "3"))
DELAY_MAJOR_MIN = int(os.getenv("DELAY_MAJOR_MIN", "15"))
TRIPS_TO_CHECK = 4  # how many upcoming trips to look at

# None means "never checked" (time.monotonic() can be small right after boot,
# so 0 is not a safe "never" value).
last_ns_check = None
last_station_fetch = None
station_list = []


# ---------------------------------------------------------------- helpers

def strip_accents(text):
    text = unicodedata.normalize("NFKD", text.casefold())
    return "".join(c for c in text if not unicodedata.combining(c))


def normalize(text):
    """Lowercase, no accents, only a-z0-9. Used for exact name comparison."""
    return re.sub(r"[^a-z0-9]", "", strip_accents(text))


# ------------------------------------------------------------------- NS API

def ns_get(path):
    if not NS_API_KEY:
        raise RuntimeError("NS_API_KEY is not configured")

    response = httpx.get(
        NS_BASE + path,
        headers={
            "Ocp-Apim-Subscription-Key": NS_API_KEY,
            "Accept": "application/json",
        },
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def get_stations():
    global station_list, last_station_fetch

    # Station names rarely change, so cache them for one day.
    if (
        station_list
        and last_station_fetch is not None
        and time.monotonic() - last_station_fetch < 86400
    ):
        return station_list

    data = ns_get("/v2/stations?countryCodes=NL")
    station_list = data.get("payload", [])
    last_station_fetch = time.monotonic()
    return station_list


def station_names(station):
    names = station.get("namen", {})
    return [n for n in (names.get("lang"), names.get("middel"), names.get("kort")) if n]


def find_station(name):
    wanted = normalize(name)

    for station in get_stations():
        if wanted in [normalize(n) for n in station_names(station)]:
            return station

    return None


def parse_ns_time(value):
    # NS timestamps look like 2026-11-11T08:04:00+0100
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None


def trip_times(trip):
    """Planned/actual departure and arrival of a whole trip, or None."""
    legs = trip.get("legs") or []
    if not legs:
        return None

    origin = legs[0].get("origin", {})
    destination = legs[-1].get("destination", {})

    dep_planned = parse_ns_time(origin.get("plannedDateTime"))
    arr_planned = parse_ns_time(destination.get("plannedDateTime"))
    if not dep_planned or not arr_planned:
        return None

    return {
        "dep_planned": dep_planned,
        "dep_actual": parse_ns_time(origin.get("actualDateTime")) or dep_planned,
        "arr_planned": arr_planned,
        "arr_actual": parse_ns_time(destination.get("actualDateTime")) or arr_planned,
    }


def trip_is_cancelled(trip):
    if str(trip.get("status", "")).upper() in {"CANCELLED", "NOT_POSSIBLE"}:
        return True
    return any(leg.get("cancelled") for leg in trip.get("legs") or [])


def trip_has_issue(trip):
    """Not cancelled, but something is off (replacement bus, odd status)."""
    if str(trip.get("status", "NORMAL")).upper() != "NORMAL":
        return True
    return any(leg.get("alternativeTransport") for leg in trip.get("legs") or [])


def trip_messages(trip):
    found = []
    for message in trip.get("messages") or []:
        text = message.get("text") if isinstance(message, dict) else message
        if text:
            found.append(str(text))
    return found


def evaluate_route(departure, arrival):
    start = find_station(departure)
    end = find_station(arrival)

    if not start:
        return "unknown", f"Departure station not found: {departure}"

    if not end:
        return "unknown", f"Arrival station not found: {arrival}"

    query = urlencode({
        "fromStation": start["code"],
        "toStation": end["code"],
    })
    data = ns_get(f"/v3/trips?{query}")

    trips = data.get("trips") if isinstance(data, dict) else None
    if not isinstance(trips, list):
        raise RuntimeError("Unexpected response from NS trips API")

    usable = []
    for trip in trips[:TRIPS_TO_CHECK]:
        times = trip_times(trip)
        if times:
            usable.append((trip, times))

    if not usable:
        return "unknown", "NS returned no trips for this route"

    # The first upcoming trip is "the usual train". Everything is measured
    # against when that train should have arrived.
    usual_trip, usual = usable[0]

    working = [(t, tm) for t, tm in usable if not trip_is_cancelled(t)]
    if not working:
        return "cancelled", f"All of the next {len(usable)} trips are cancelled"

    best_trip, best = min(working, key=lambda item: item[1]["arr_actual"])
    late = round((best["arr_actual"] - usual["arr_planned"]).total_seconds() / 60)

    summary = (
        f"Best option leaves {best['dep_actual']:%H:%M}, "
        f"arrives {best['arr_actual']:%H:%M} ({late:+d} min vs usual)"
    )
    extra = trip_messages(best_trip) or trip_messages(usual_trip)
    if extra:
        summary += ". " + extra[0]
    summary = summary[:300]

    if late >= DELAY_MAJOR_MIN:
        return "cancelled", summary

    if (
        late >= DELAY_MINOR_MIN
        or trip_is_cancelled(usual_trip)
        or trip_has_issue(best_trip)
    ):
        return "delayed", summary

    return "ok", summary


def ns_check_is_fresh():
    return (
        last_ns_check is not None
        and time.monotonic() - last_ns_check < NS_MIN_INTERVAL
    )


def refresh_ns_status():
    global last_ns_check

    if ns_check_is_fresh():
        return

    # Only one thread talks to NS at a time; the others wait and then see
    # the fresh result instead of all hitting the API.
    with refresh_lock:
        if ns_check_is_fresh():
            return

        with lock:
            checked_route = route.copy()

        try:
            new_status, message = evaluate_route(
                checked_route["departure"],
                checked_route["arrival"],
            )
            error = None
        except Exception as exc:
            print("NS API error:", str(exc))
            new_status, message, error = None, None, str(exc)

        with lock:
            # The route may have been changed while we were talking to NS.
            # In that case this result is for the wrong route; discard it.
            if route != checked_route:
                return

            if error is None:
                status["status"] = new_status
                status["message"] = message
                status["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
                status.pop("error", None)
            else:
                # Keep the last known status, but tell the client it is stale.
                status["error"] = "Could not reach NS; showing last known status"

        last_ns_check = time.monotonic()


# ------------------------------------------------------------------ storage

def save_route(new_route):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = ROUTE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(new_route, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(ROUTE_FILE)


def load_route():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not ROUTE_FILE.exists():
        save_route(DEFAULT_ROUTE)
        return DEFAULT_ROUTE.copy()

    try:
        data = json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
        departure = data.get("departure")
        arrival = data.get("arrival")

        if (
            isinstance(departure, str) and departure.strip()
            and isinstance(arrival, str) and arrival.strip()
        ):
            return {
                "departure": departure.strip(),
                "arrival": arrival.strip(),
            }
    except (OSError, json.JSONDecodeError, AttributeError):
        pass

    save_route(DEFAULT_ROUTE)
    return DEFAULT_ROUTE.copy()


route = load_route()
status = DEFAULT_STATUS.copy()


# ------------------------------------------------------------------- models

class RouteUpdate(BaseModel):
    departure: str = Field(min_length=1, max_length=100)
    arrival: str = Field(min_length=1, max_length=100)


class StatusUpdate(BaseModel):
    status: str
    message: str | None = Field(default=None, max_length=200)


# ------------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def home():
    with lock:
        departure = route["departure"]
        arrival = route["arrival"]
        current_status = status.copy()

    departure_html = escape(departure, quote=True)
    arrival_html = escape(arrival, quote=True)
    status_html = escape(current_status["status"])
    message_html = escape(current_status["message"])

    return f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Train indicator</title>
      <style>
        body {{
          font: 16px system-ui;
          max-width: 520px;
          margin: 30px auto;
          padding: 0 18px;
          background: #f7f7f7;
        }}
        h1 {{ text-align: center; }}
        .panel {{
          background: white;
          padding: 20px;
          border-radius: 14px;
          margin: 18px 0;
          box-shadow: 0 2px 8px #0001;
        }}
        input, button {{
          box-sizing: border-box;
          width: 100%;
          padding: 14px;
          margin: 7px 0 16px;
          border-radius: 8px;
          font-size: 16px;
        }}
        input {{ border: 1px solid #ccc; }}
        button {{
          background: #1769aa;
          color: white;
          border: none;
          font-weight: bold;
          cursor: pointer;
        }}
        #result {{ min-height: 24px; }}
        .route {{ font-weight: bold; }}
      </style>
    </head>
    <body>
      <h1>🚆 Train indicator</h1>

      <div class="panel">
        <h2>Choose your route</h2>
        <form id="routeForm">
          <label for="departure">Departure station</label>
          <input id="departure" maxlength="100" required
                 value="{departure_html}">

          <label for="arrival">Arrival station</label>
          <input id="arrival" maxlength="100" required
                 value="{arrival_html}">

          <button type="submit">Save route</button>
        </form>
        <p id="result" role="status"></p>
      </div>

      <div class="panel">
        <h2>Current route</h2>
        <p class="route" id="savedRoute">
          {departure_html} → {arrival_html}
        </p>
      </div>

      <div class="panel">
        <h2>Train status</h2>
        <h3 id="status">{status_html}</h3>
        <p id="message">{message_html}</p>
        <small id="checked"></small>
      </div>

      <script>
        async function loadStatus() {{
          try {{
            const response = await fetch("/status");
            if (!response.ok) return;
            const data = await response.json();

            document.getElementById("status").textContent = data.status;
            document.getElementById("message").textContent = data.error
              ? data.message + " (" + data.error + ")"
              : data.message;
            document.getElementById("checked").textContent =
              data.last_checked ? "Last checked: " + data.last_checked : "";
          }} catch (error) {{
            /* keep showing what we have */
          }}
        }}

        document.getElementById("routeForm").addEventListener(
          "submit", async (event) => {{
            event.preventDefault();

            const result = document.getElementById("result");
            result.textContent = "Saving route...";

            try {{
              const response = await fetch("/route", {{
                method: "POST",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify({{
                  departure: document.getElementById("departure").value,
                  arrival: document.getElementById("arrival").value
                }})
              }});

              const data = await response.json();

              if (!response.ok) {{
                const detail = Array.isArray(data.detail)
                  ? data.detail.map((d) => d.msg).join(", ")
                  : data.detail;
                throw new Error(detail || "Save failed");
              }}

              document.getElementById("savedRoute").textContent =
                data.departure + " → " + data.arrival;

              result.textContent = "Route saved successfully!";
              loadStatus();
            }} catch (error) {{
              result.textContent = "Error: " + error.message;
            }}
          }}
        );

        loadStatus();
        setInterval(loadStatus, 60000);
      </script>
    </body>
    </html>
    """


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/route")
def get_route():
    with lock:
        return route.copy()


@app.post("/route")
def set_route(update: RouteUpdate):
    global last_ns_check

    departure = update.departure.strip()
    arrival = update.arrival.strip()

    if not departure or not arrival:
        raise HTTPException(
            status_code=422,
            detail="Both stations are required",
        )

    new_route = {
        "departure": departure,
        "arrival": arrival,
    }

    with lock:
        try:
            save_route(new_route)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="Could not save route to disk",
            ) from exc

        route.update(new_route)

        # The old status belongs to the old route: reset it and force a
        # fresh NS check on the next /status request.
        status.clear()
        status.update(DEFAULT_STATUS)
        last_ns_check = None

    return new_route.copy()


@app.get("/status")
def get_status():
    refresh_ns_status()

    with lock:
        return {
            **status,
            "route": route.copy(),
        }leiden


@app.post("/status")
def set_status(update: StatusUpdate):
    global last_ns_check

    allowed = {"ok", "delayed", "cancelled"}

    if update.status not in allowed:
        raise HTTPException(
            status_code=422,
            detail="Status must be ok, delayed, or cancelled",
        )

    defaults = {
        "ok": "Running normally",
        "delayed": "Train delayed",
        "cancelled": "Train cancelled",
    }

    with lock:
        status["status"] = update.status
        status["message"] = update.message or defaults[update.status]
        status.pop("error", None)
        # Let the manual value stand for one interval instead of being
        # overwritten by the next NS check immediately.
        last_ns_check = time.monotonic()
        return status.copy()