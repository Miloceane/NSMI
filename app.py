from datetime import datetime, timedelta
from html import escape
from pathlib import Path
from threading import Lock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import json
import os
import re
import time
import unicodedata

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Train Status API")

DATA_DIR = Path("/data")
ROUTE_FILE = DATA_DIR / "route.json"
CONFIG_FILE = DATA_DIR / "config.json"

lock = Lock()          # protects route, config and status
refresh_lock = Lock()  # makes sure only one request calls NS at a time

DEFAULT_ROUTE = {
    "departure": "Utrecht Centraal",
    "arrival": "Amsterdam Centraal",
}

# Colors are what the ESP32 shows. "status" is kept for older clients:
# green = ok, yellow = delayed, red = cancelled.
COLOR_RANK = {"green": 0, "yellow": 1, "red": 2}
STATUS_OF_COLOR = {"green": "ok", "yellow": "delayed", "red": "cancelled"}
COLOR_OF_STATUS = {v: k for k, v in STATUS_OF_COLOR.items()}

DEFAULT_STATUS = {
    "status": "ok",
    "color": "green",
    "message": "Running normally",
    "watching": None,
}

# What colour each situation gives. The worst colour among all situations
# that apply wins. Choosing "green" for a situation means "ignore it".
DEFAULT_CONFIG = {
    "delay_yellow_min": 3,           # arrives this many minutes late -> yellow
    "delay_red_min": 15,             # arrives this many minutes late -> red
    "usual_cancelled": "yellow",     # the train is cancelled but a later one works
    "replacement_transport": "yellow",  # replacement bus / odd trip status
    "reduced_service": "yellow",     # NS: fewer trains on the route
    "no_trains": "red",              # NS: no trains on the route
    "all_cancelled": "red",          # every upcoming trip is cancelled
    "watch_times": [],               # ["07:48", "08:18"]; empty = next train
}
CONDITION_KEYS = [
    "usual_cancelled",
    "replacement_transport",
    "reduced_service",
    "no_trains",
    "all_cancelled",
]
CONFIG_KEYS = ["delay_yellow_min", "delay_red_min", *CONDITION_KEYS, "watch_times"]

NS_API_KEY = os.getenv("NS_API_KEY")
NS_BASE = "https://gateway.apiportal.ns.nl/reisinformatie-api/api"
NS_MIN_INTERVAL = 60       # seconds between NS checks
TRIPS_TO_CHECK = 4         # how many upcoming trips to look at
WATCH_LOOKBACK_MIN = 60    # a chosen train this far past its time can still count
MAX_WATCH_LOOKUPS = 3      # NS calls per check when specific times are chosen

NO_TRAIN_PHRASES = [
    "geen treinen",
    "er rijden geen treinen",
    "no trains",
    "all trains cancelled",
]

# None means "never checked" (time.monotonic() can be small right after boot).
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


def words(text):
    """Lowercase, no accents, words separated by single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]", " ", strip_accents(text)).split())


def mentions(haystack_words, name):
    """True if `name` appears in the text as whole words ("Ede" must not
    match inside "verleden")."""
    needle = words(name)
    return bool(needle) and f" {needle} " in f" {haystack_words} "


def local_tz():
    try:
        return ZoneInfo(os.getenv("TIMEZONE", "Europe/Amsterdam"))
    except Exception:
        # No tz database in the container (add `tzdata` to requirements).
        return datetime.now().astimezone().tzinfo


def write_json(path, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = path.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(path)


# --------------------------------------------------------------- settings

def clean_config(raw):
    """Validate a settings dict and return a clean copy. Raises ValueError."""
    cfg = {}

    for key in ("delay_yellow_min", "delay_red_min"):
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 720:
            raise ValueError(f"{key} must be a whole number between 0 and 720")
        cfg[key] = value

    if cfg["delay_yellow_min"] > cfg["delay_red_min"]:
        raise ValueError("The yellow delay can't be larger than the red delay")

    for key in CONDITION_KEYS:
        if raw.get(key) not in COLOR_RANK:
            raise ValueError(f"{key} must be green, yellow or red")
        cfg[key] = raw[key]

    times = []
    for item in raw.get("watch_times") or []:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(item))
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError(f"Invalid time '{item}' (use HH:MM, e.g. 07:48)")
        times.append(f"{int(match[1]):02d}:{match[2]}")

    times = sorted(set(times))
    if len(times) > 20:
        raise ValueError("At most 20 departure times")
    cfg["watch_times"] = times

    return cfg


def load_config():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return clean_config({**DEFAULT_CONFIG, **data})
    except (OSError, json.JSONDecodeError, ValueError, AttributeError, TypeError):
        pass

    config = clean_config(DEFAULT_CONFIG)
    try:
        write_json(CONFIG_FILE, config)
    except OSError:
        pass
    return config


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


def match_names(station):
    names = station.get("namen", {})
    return [n for n in (names.get("lang"), names.get("middel")) if n]


def find_station(name):
    wanted = normalize(name)

    for station in get_stations():
        if wanted in [normalize(n) for n in station_names(station)]:
            return station

    return None


# ------------------------------------------------------------------- trips

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


def fetch_trips(start, end, when=None):
    """Trips from `when` (default: now), as a list of (trip, times)."""
    params = {"fromStation": start["code"], "toStation": end["code"]}
    if when is not None:
        params["dateTime"] = when.isoformat(timespec="seconds")

    data = ns_get(f"/v3/trips?{urlencode(params)}")

    trips = data.get("trips") if isinstance(data, dict) else None
    if not isinstance(trips, list):
        raise RuntimeError("Unexpected response from NS trips API")

    result = []
    for trip in trips:
        times = trip_times(trip)
        if times:
            result.append((trip, times))
    return result


def watch_candidates(watch_times, now):
    """Chosen departure times from shortly ago until tomorrow, in order."""
    found = []
    for day_offset in (0, 1):
        day = (now + timedelta(days=day_offset)).date()
        for item in watch_times:
            hour, minute = (int(part) for part in item.split(":"))
            target = datetime(day.year, day.month, day.day, hour, minute, tzinfo=now.tzinfo)
            if target >= now - timedelta(minutes=WATCH_LOOKBACK_MIN):
                found.append(target)
    return sorted(found)


def find_watched(start, end, watch_times):
    """The next chosen train that hasn't left yet.

    Returns (target time, [(trip, times), ...]) where the list starts with
    the chosen train followed by the trips after it (the fallbacks), or
    (None, []) if none of the chosen trains could be found.
    """
    now = datetime.now(local_tz())

    for target in watch_candidates(watch_times, now)[:MAX_WATCH_LOOKUPS]:
        trips = fetch_trips(start, end, target)

        matches = [
            i for i, (_, tm) in enumerate(trips)
            if abs((tm["dep_planned"] - target).total_seconds()) < 60
        ]
        if not matches:
            continue  # no such train planned; try the next chosen time

        # Several options can leave at the same time: prefer fewest transfers.
        index = min(matches, key=lambda i: trips[i][0].get("transfers", 0))

        if trips[index][1]["dep_actual"] < now:
            continue  # that train has already left

        return target, trips[index:index + TRIPS_TO_CHECK]

    return None, []


# ------------------------------------------------------------- disruptions

def route_stops(trip):
    """Every station the trip passes: a list of name-lists (one per station)."""
    by_uic = {
        str(s.get("UICCode")): s for s in get_stations() if s.get("UICCode")
    }
    seen = {}

    for leg in trip.get("legs") or []:
        points = list(leg.get("stops") or [])
        points += [leg.get("origin", {}), leg.get("destination", {})]

        for point in points:
            if not isinstance(point, dict):
                continue
            key = str(point.get("uicCode") or point.get("name") or "")
            if not key or key in seen:
                continue

            station = by_uic.get(str(point.get("uicCode")))
            names = match_names(station) if station else []
            if point.get("name"):
                names.append(point["name"])
            seen[key] = names

    return list(seen.values())


def get_disruption_text(disruption):
    parts = [
        disruption.get("title", ""),
        disruption.get("topic", ""),
    ]

    for timespan in disruption.get("timespans", []):
        for section in ("cause", "situation"):
            item = timespan.get(section, {})
            if isinstance(item, dict):
                parts.append(item.get("label", ""))

        advice = timespan.get("alternativeTransport", {})
        if isinstance(advice, dict):
            parts.append(advice.get("label", ""))

    return " ".join(str(part) for part in parts if part)


def disruption_summary(disruption):
    """The most useful single sentence: the 'situation' text if present."""
    for timespan in disruption.get("timespans", []):
        situation = timespan.get("situation", {})
        if isinstance(situation, dict) and situation.get("label"):
            return str(situation["label"])
    return str(disruption.get("title") or "NS disruption")


def check_disruptions(stops):
    """Disruptions that mention at least two stations of the route.

    NS names the two ends of the affected stretch, which can be stations
    in the middle of someone's trip, so every stop is considered.
    Returns ({"reduced_service" and/or "no_trains"}, [sentences]).
    """
    disruptions = ns_get("/v3/disruptions?isActive=true")

    if not isinstance(disruptions, list):
        raise RuntimeError("Unexpected response from NS disruptions API")

    kinds = set()
    notes = []

    for disruption in disruptions:
        text = get_disruption_text(disruption)
        text_words = words(text)

        hits = sum(
            1 for names in stops
            if any(mentions(text_words, n) for n in names)
        )
        if hits < 2:
            continue

        if any(phrase in text.casefold() for phrase in NO_TRAIN_PHRASES):
            kinds.add("no_trains")
        else:
            kinds.add("reduced_service")
        notes.append(disruption_summary(disruption))

    return kinds, notes


# -------------------------------------------------------------- evaluation

def result(color, message, watching=None):
    return {"color": color, "message": message[:400], "watching": watching}


def evaluate_route(departure, arrival, cfg):
    start = find_station(departure)
    end = find_station(arrival)

    if not start:
        return result("unknown", f"Departure station not found: {departure}")

    if not end:
        return result("unknown", f"Arrival station not found: {arrival}")

    watching = None
    if cfg["watch_times"]:
        target, usable = find_watched(start, end, cfg["watch_times"])
        if not usable:
            return result(
                "unknown",
                "No trip found leaving at " + ", ".join(cfg["watch_times"]),
            )
        watching = f"{target:%H:%M}"
    else:
        usable = fetch_trips(start, end)[:TRIPS_TO_CHECK]
        if not usable:
            return result("unknown", "NS returned no trips for this route")

    # The first trip in the list is the reference: the next train, or the
    # chosen one. Everything is measured against when it should arrive.
    usual_trip, usual = usable[0]

    working = [(t, tm) for t, tm in usable if not trip_is_cancelled(t)]
    if not working:
        return result(
            cfg["all_cancelled"],
            f"All of the next {len(usable)} trips are cancelled",
            watching,
        )

    best_trip, best = min(working, key=lambda item: item[1]["arr_actual"])
    late = round((best["arr_actual"] - usual["arr_planned"]).total_seconds() / 60)

    reference = f"the {watching} train" if watching else "usual"
    summary = (
        f"Best option leaves {best['dep_actual']:%H:%M}, "
        f"arrives {best['arr_actual']:%H:%M} ({late:+d} min vs {reference})"
    )
    extra = trip_messages(best_trip) or trip_messages(usual_trip)
    if extra:
        summary += ". " + extra[0]

    # Every situation that applies proposes a colour; the worst one wins.
    colors = ["green"]

    if late >= cfg["delay_red_min"]:
        colors.append("red")
    elif late >= cfg["delay_yellow_min"]:
        colors.append("yellow")

    if trip_is_cancelled(usual_trip):
        colors.append(cfg["usual_cancelled"])

    if trip_has_issue(best_trip):
        colors.append(cfg["replacement_transport"])

    # Reduced service (e.g. only half the trains run) does not show up as a
    # delay or cancellation in the trip planner, only in the disruption feed.
    try:
        kinds, notes = check_disruptions(route_stops(best_trip))
    except Exception as exc:
        print("NS disruptions error:", str(exc))
        kinds, notes = set(), []
        summary += " (could not check NS disruptions)"

    colors.extend(cfg[kind] for kind in kinds)

    if notes:
        summary = " | ".join(notes[:2]) + " | " + summary

    color = max(colors, key=COLOR_RANK.get)
    return result(color, summary, watching)


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
            checked_config = {**config, "watch_times": list(config["watch_times"])}

        try:
            outcome = evaluate_route(
                checked_route["departure"],
                checked_route["arrival"],
                checked_config,
            )
            error = None
        except Exception as exc:
            print("NS API error:", str(exc))
            outcome, error = None, str(exc)

        with lock:
            # Route or settings may have changed while we were talking to NS.
            # This result is then for the wrong input; discard it.
            if route != checked_route or config != checked_config:
                return

            if error is None:
                status["color"] = outcome["color"]
                status["status"] = STATUS_OF_COLOR.get(outcome["color"], "unknown")
                status["message"] = outcome["message"]
                status["watching"] = outcome["watching"]
                status["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
                status.pop("error", None)
            else:
                # Keep the last known status, but tell the client it is stale.
                status["error"] = "Could not reach NS; showing last known status"

        last_ns_check = time.monotonic()


# ------------------------------------------------------------------ storage

def save_route(new_route):
    write_json(ROUTE_FILE, new_route)


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
config = load_config()
status = DEFAULT_STATUS.copy()


# ------------------------------------------------------------------- models

class RouteUpdate(BaseModel):
    departure: str = Field(min_length=1, max_length=100)
    arrival: str = Field(min_length=1, max_length=100)


class StatusUpdate(BaseModel):
    status: str
    message: str | None = Field(default=None, max_length=200)


class ConfigUpdate(BaseModel):
    delay_yellow_min: int
    delay_red_min: int
    usual_cancelled: str
    replacement_transport: str
    reduced_service: str
    no_trains: str
    all_cancelled: str
    watch_times: list[str] = []


# --------------------------------------------------------------------- page

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Train indicator</title>
  <style>
    body {
      font: 16px system-ui;
      max-width: 520px;
      margin: 30px auto;
      padding: 0 18px;
      background: #f7f7f7;
    }
    h1 { text-align: center; }
    h4 { margin: 18px 0 4px; }
    .panel {
      background: white;
      padding: 20px;
      border-radius: 14px;
      margin: 18px 0;
      box-shadow: 0 2px 8px #0001;
    }
    input, button, select {
      box-sizing: border-box;
      width: 100%;
      padding: 14px;
      margin: 7px 0 16px;
      border-radius: 8px;
      font-size: 16px;
    }
    input, select { border: 1px solid #ccc; background: white; }
    button {
      background: #1769aa;
      color: white;
      border: none;
      font-weight: bold;
      cursor: pointer;
    }
    button.secondary { background: #777; margin-top: 0; }
    #result, #configResult { min-height: 24px; }
    .route { font-weight: bold; }
    .hint { color: #666; font-size: 14px; margin: 0 0 8px; }
    .status-row { display: flex; align-items: center; gap: 12px; }
    .status-row h3 { margin: 0; }
    .dot {
      width: 26px; height: 26px; border-radius: 50%;
      background: #999; flex: none;
    }
  </style>
</head>
<body>
  <h1>🚆 Train indicator</h1>

  <div class="panel">
    <h2>Choose your route</h2>
    <form id="routeForm">
      <label for="departure">Departure station</label>
      <input id="departure" maxlength="100" required value="__DEPARTURE__">

      <label for="arrival">Arrival station</label>
      <input id="arrival" maxlength="100" required value="__ARRIVAL__">

      <button type="submit">Save route</button>
    </form>
    <p id="result" role="status"></p>
  </div>

  <div class="panel">
    <h2>Current route</h2>
    <p class="route" id="savedRoute">__DEPARTURE__ → __ARRIVAL__</p>
  </div>

  <div class="panel">
    <h2>Train status</h2>
    <div class="status-row">
      <span class="dot" id="dot"></span>
      <h3 id="status">__STATUS__</h3>
    </div>
    <p id="message">__MESSAGE__</p>
    <small id="watching"></small><br>
    <small id="checked"></small>
  </div>

  <div class="panel">
    <h2>Light settings</h2>
    <form id="configForm">
      <p class="hint">Delays are measured against the train you'd normally take
      (or the specific train chosen below). The worst colour that applies wins.</p>

      <h4>Delay</h4>
      <label for="cfg_delay_yellow_min">Yellow from (minutes late)</label>
      <input id="cfg_delay_yellow_min" type="number" min="0" max="720" required>

      <label for="cfg_delay_red_min">Red from (minutes late)</label>
      <input id="cfg_delay_red_min" type="number" min="0" max="720" required>

      <h4>Other situations</h4>
      <p class="hint">Pick green to ignore a situation.</p>
      <div id="conditions"></div>

      <h4>Specific trains (optional)</h4>
      <p class="hint">Departure times from your station, e.g. 07:48, 08:18.
      The next one that hasn't left yet decides the colour.
      Leave empty to follow the next train.</p>
      <input id="cfg_watch_times" placeholder="07:48, 08:18">

      <button type="submit">Save settings</button>
      <button type="button" class="secondary" id="resetConfig">Reset to defaults</button>
    </form>
    <p id="configResult" role="status"></p>
  </div>

  <script>
    const COLORS = {
      green: "#2e9d4f", yellow: "#e6b800", red: "#d33a2c", unknown: "#999"
    };
    const CONDITIONS = [
      ["usual_cancelled", "My train is cancelled, but a later one works"],
      ["replacement_transport", "Replacement bus or other trip problem"],
      ["reduced_service", "NS reports fewer trains on my route"],
      ["no_trains", "NS reports no trains on my route"],
      ["all_cancelled", "All upcoming trips are cancelled"]
    ];

    document.getElementById("conditions").innerHTML = CONDITIONS.map(
      ([key, text]) =>
        `<label for="cfg_${key}">${text}</label>` +
        `<select id="cfg_${key}">` +
        `<option value="green">🟢 Green</option>` +
        `<option value="yellow">🟡 Yellow</option>` +
        `<option value="red">🔴 Red</option></select>`
    ).join("");

    function errorText(data, fallback) {
      if (Array.isArray(data.detail)) {
        return data.detail.map((d) => d.msg).join(", ");
      }
      return data.detail || fallback;
    }

    async function loadStatus() {
      try {
        const response = await fetch("/status");
        if (!response.ok) return;
        const data = await response.json();

        document.getElementById("dot").style.background =
          COLORS[data.color] || COLORS.unknown;
        document.getElementById("status").textContent =
          data.color + " (" + data.status + ")";
        document.getElementById("message").textContent = data.error
          ? data.message + " (" + data.error + ")"
          : data.message;
        document.getElementById("watching").textContent = data.watching
          ? "Following the " + data.watching + " train" : "";
        document.getElementById("checked").textContent =
          data.last_checked ? "Last checked: " + data.last_checked : "";
      } catch (error) {
        /* keep showing what we have */
      }
    }

    function fillConfig(c) {
      document.getElementById("cfg_delay_yellow_min").value = c.delay_yellow_min;
      document.getElementById("cfg_delay_red_min").value = c.delay_red_min;
      for (const [key] of CONDITIONS) {
        document.getElementById("cfg_" + key).value = c[key];
      }
      document.getElementById("cfg_watch_times").value = c.watch_times.join(", ");
    }

    async function loadConfig() {
      try {
        const response = await fetch("/config");
        if (response.ok) fillConfig(await response.json());
      } catch (error) {
        /* leave the form empty */
      }
    }

    async function sendConfig(url, body, okText) {
      const out = document.getElementById("configResult");
      out.textContent = "Saving...";
      try {
        const response = await fetch(url, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body ? JSON.stringify(body) : undefined
        });
        const data = await response.json();
        if (!response.ok) throw new Error(errorText(data, "Save failed"));
        fillConfig(data);
        out.textContent = okText;
        loadStatus();
      } catch (error) {
        out.textContent = "Error: " + error.message;
      }
    }

    document.getElementById("configForm").addEventListener("submit", (event) => {
      event.preventDefault();
      const body = {
        delay_yellow_min: Number(document.getElementById("cfg_delay_yellow_min").value),
        delay_red_min: Number(document.getElementById("cfg_delay_red_min").value),
        watch_times: document.getElementById("cfg_watch_times").value
          .split(/[,\\s]+/).filter(Boolean)
      };
      for (const [key] of CONDITIONS) {
        body[key] = document.getElementById("cfg_" + key).value;
      }
      sendConfig("/config", body, "Settings saved!");
    });

    document.getElementById("resetConfig").addEventListener("click", () => {
      sendConfig("/config/reset", null, "Back to default settings.");
    });

    document.getElementById("routeForm").addEventListener(
      "submit", async (event) => {
        event.preventDefault();

        const result = document.getElementById("result");
        result.textContent = "Saving route...";

        try {
          const response = await fetch("/route", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              departure: document.getElementById("departure").value,
              arrival: document.getElementById("arrival").value
            })
          });

          const data = await response.json();

          if (!response.ok) {
            throw new Error(errorText(data, "Save failed"));
          }

          document.getElementById("savedRoute").textContent =
            data.departure + " → " + data.arrival;

          result.textContent = "Route saved successfully!";
          loadStatus();
        } catch (error) {
          result.textContent = "Error: " + error.message;
        }
      }
    );

    loadConfig();
    loadStatus();
    setInterval(loadStatus, 60000);
  </script>
</body>
</html>
"""


# ------------------------------------------------------------------- routes

@app.get("/", response_class=HTMLResponse)
def home():
    with lock:
        departure = route["departure"]
        arrival = route["arrival"]
        current_status = status.copy()

    return (
        PAGE
        .replace("__DEPARTURE__", escape(departure, quote=True))
        .replace("__ARRIVAL__", escape(arrival, quote=True))
        .replace("__STATUS__", escape(current_status["status"]))
        .replace("__MESSAGE__", escape(current_status["message"]))
    )


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


@app.get("/config")
def get_config():
    with lock:
        return {**config, "watch_times": list(config["watch_times"])}


@app.post("/config")
def set_config(update: ConfigUpdate):
    global last_ns_check

    try:
        new_config = clean_config({key: getattr(update, key) for key in CONFIG_KEYS})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return store_config(new_config)


@app.post("/config/reset")
def reset_config():
    return store_config(clean_config(DEFAULT_CONFIG))


def store_config(new_config):
    global last_ns_check

    with lock:
        try:
            write_json(CONFIG_FILE, new_config)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="Could not save settings to disk",
            ) from exc

        config.clear()
        config.update(new_config)
        last_ns_check = None  # re-check with the new settings right away

        return {**config, "watch_times": list(config["watch_times"])}


@app.get("/status")
def get_status():
    refresh_ns_status()

    with lock:
        return {
            **status,
            "route": route.copy(),
        }


@app.get("/color", response_class=PlainTextResponse)
def get_color():
    """Just the colour as plain text (green, yellow, red or unknown),
    handy for an ESP32 that doesn't want to parse JSON."""
    refresh_ns_status()

    with lock:
        return status["color"]


@app.post("/status")
def set_status(update: StatusUpdate):
    global last_ns_check

    if update.status not in COLOR_OF_STATUS:
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
        status["color"] = COLOR_OF_STATUS[update.status]
        status["message"] = update.message or defaults[update.status]
        status["watching"] = None
        status.pop("error", None)
        # Let the manual value stand for one interval instead of being
        # overwritten by the next NS check immediately.
        last_ns_check = time.monotonic()
        return status.copy()