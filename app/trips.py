import os
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from . import ns_client

TRIPS_TO_CHECK = 4         # how many upcoming trips to look at
WATCH_LOOKBACK_MIN = 60    # a chosen train this far past its time can still count
WATCH_TOLERANCE_MIN = 10   # "07:49" may mean the train that leaves at 07:48
MAX_WATCH_LOOKUPS = 3      # NS calls per check when specific times are chosen


def local_tz():
    try:
        return ZoneInfo(os.getenv("TIMEZONE", "Europe/Amsterdam"))
    except Exception:
        # No tz database in the container (add `tzdata` to requirements).
        return datetime.now().astimezone().tzinfo


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

    data = ns_client.ns_get(f"/v3/trips?{urlencode(params)}")

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

    Returns (label, [(trip, times), ...], note). The list starts with the
    chosen train, followed by the trips after it (the fallbacks). If none
    of the chosen trains can be found it returns (None, [], None).
    """
    now = datetime.now(local_tz())
    tolerance = timedelta(minutes=WATCH_TOLERANCE_MIN)

    for target in watch_candidates(watch_times, now)[:MAX_WATCH_LOOKUPS]:
        trips = fetch_trips(start, end, target - tolerance)

        def gap(i):
            return abs((trips[i][1]["dep_planned"] - target).total_seconds())

        near = [i for i in range(len(trips)) if gap(i) <= tolerance.total_seconds()]
        if not near:
            continue

        # Closest to the chosen time; among equals prefer fewest transfers.
        index = min(near, key=lambda i: (round(gap(i) / 60), trips[i][0].get("transfers", 0)))
        trip, times = trips[index]

        if times["dep_actual"] < now:
            continue  # that train has already left

        label = f"{times['dep_planned']:%H:%M}"
        note = None
        if gap(index) >= 60:
            note = f"Using the {label} train (closest to {target:%H:%M})"
        return label, trips[index:index + TRIPS_TO_CHECK], note

    return None, [], None
