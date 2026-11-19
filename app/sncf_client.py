"""SNCF API (api.sncf.com, a Navitia instance): station search and trips.

Calls go through `api` (see app/api_cache.py) with their own budget, fully
separate from NS: SNCF_CALLS_PER_MINUTE (default 2) and a daily quota
SNCF_CALLS_PER_DAY (default 4500; the free SNCF plan allows 5000 a day).

Trips are asked for with data_freshness=base_schedule: that gives the
planned trains *including* cancelled ones, with links to the disruptions
that affect them. Delays and cancellations are then read from those
disruptions, and every journey is converted into the same shape as an NS
trip, so app/trips.py and app/evaluate.py work for both.
"""
import os
import re
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

from . import api_cache
from .text import normalize, words

SNCF_BASE = "https://api.sncf.com/v1/coverage/sncf"
CALLS_PER_MINUTE = api_cache.env_int("SNCF_CALLS_PER_MINUTE", 2, 1, 10)
CALLS_PER_DAY = api_cache.env_int("SNCF_CALLS_PER_DAY", 4500, 50, 1_000_000)
SEARCH_MAX_AGE = 7 * 86400    # station search results barely change
ID_PATTERN = re.compile(r"stop_area:[A-Za-z0-9:_.-]{1,80}")

try:
    PARIS = ZoneInfo("Europe/Paris")
except Exception:  # no tz database: see trips.local_tz()
    PARIS = datetime.now().astimezone().tzinfo

# Text in a disruption that means buses/coaches replace the train.
REPLACEMENT_WORDS = [
    "autocar", "autocars", "car de substitution", "cars de substitution",
    "bus de substitution", "bus de remplacement", "substitution routiere",
    "transport de substitution", "taxi", "taxis",
]


class SncfApi(api_cache.Api):
    def request(self, path):
        key = os.getenv("SNCF_API_KEY")
        if not key:
            raise RuntimeError("SNCF_API_KEY is not configured")
        # Navitia: the key is the Basic auth user name, the password is empty.
        return httpx.get(self.base_url + path, auth=(key, ""), timeout=20)

    def parse(self, response):
        if response.status_code in (401, 403):
            raise RuntimeError(f"SNCF refused the API key ({response.status_code})")
        if response.status_code == 404:
            # "no_solution" (no train found) and friends are normal answers.
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            if error.get("id") in ("no_solution", "no_origin", "no_destination",
                                   "no_origin_nor_destination", "date_out_of_bounds"):
                return {"journeys": [], "places": [], "no_solution": error.get("id")}
        response.raise_for_status()
        return response.json()


api = SncfApi(
    name="sncf",
    label="SNCF",
    base_url=SNCF_BASE,
    calls_per_minute=CALLS_PER_MINUTE,
    last_call_file="sncf_last_call.json",
    urgent_per_minute=8,
    urgent_min_gap=1.0,
    daily_limit=CALLS_PER_DAY,
    min_block=600,
)


# ------------------------------------------------------------ stations

def search(query, limit=10):
    """Stations matching what the person typed: [{"id", "name", "label"}].

    Uses the urgent budget (someone is typing). Raises api_cache.Busy when
    that budget is used up."""
    query = " ".join(str(query).split()).casefold()[:60]
    if len(query) < 3:  # same minimum as the page: fewer distinct (costly) queries
        return []
    path = "/places?" + urlencode(
        {"q": query, "type[]": "stop_area", "count": limit, "disable_geojson": "true"}
    )
    try:
        data = api.get_now(path, SEARCH_MAX_AGE)
    except RuntimeError:
        api.forget(path)  # let the next try ask again instead of retrying in the background
        raise

    found, seen = [], set()
    for place in data.get("places") or [] if isinstance(data, dict) else []:
        if place.get("embedded_type") != "stop_area":
            continue
        sid = place.get("id") or ""
        name = (place.get("stop_area") or {}).get("name") or place.get("name")
        if not ID_PATTERN.fullmatch(sid) or not name or sid in seen:
            continue
        seen.add(sid)
        found.append({"id": sid, "name": name, "label": place.get("name") or name})
    return found


def find_station(name, station_id=None):
    """The station for a stored route: {"code": stop_area id, "name": name}.

    SNCF has no cheap "all stations" list, so routes store the stop_area id
    picked from the search; there is nothing to look up here."""
    if station_id and ID_PATTERN.fullmatch(station_id):
        return {"code": station_id, "name": name}
    return None


def resolve(text, station_id=None):
    """Check a station typed/picked on the web page. Returns
    (station or None, [suggested names])."""
    results = search(text)
    if station_id:
        for r in results:
            if r["id"] == station_id:
                return r, []
    wanted = normalize(text)
    for r in results:
        if wanted in (normalize(r["name"]), normalize(r["label"])):
            return r, []
    return None, [r["label"] for r in results[:3]]


# ------------------------------------------------------------ trips

def journeys_path(start_id, end_id, when=None):
    params = {
        "from": start_id,
        "to": end_id,
        "datetime_represents": "departure",
        "data_freshness": "base_schedule",   # keep cancelled trains in the answer
        "count": 5,
        "min_nb_journeys": 4,
        "max_nb_transfers": 3,
        "direct_path": "none",               # trains only, no walking/car trip
        "disable_geojson": "true",
    }
    if when is not None:
        params["datetime"] = when.astimezone(PARIS).strftime("%Y%m%dT%H%M%S")
    return "/journeys?" + urlencode(params)


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=PARIS)
    except (TypeError, ValueError):
        return None


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")  # the NS format, see trips.parse_ns_time


def _amend(base, hhmmss):
    """Base datetime with the time of day replaced; handles midnight."""
    try:
        h, m, s = int(hhmmss[0:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
        new = base.replace(hour=h, minute=m, second=s)
    except (TypeError, ValueError, IndexError):
        return None
    if new < base - timedelta(hours=12):
        new += timedelta(days=1)
    elif new > base + timedelta(hours=12):
        new -= timedelta(days=1)
    return new


def _uic(text):
    match = re.search(r":(\d{7,8})(?::|$)", str(text or ""))
    return match[1] if match else None


def _keys(point):
    """Ways to recognise a stop: ids, UIC code and name."""
    if not isinstance(point, dict):
        return set()
    stop = point.get("stop_point") or point.get("stop_area") or point
    keys = set()
    for obj in (point, stop, stop.get("stop_area") or {}):
        if obj.get("id"):
            keys.add(obj["id"])
            uic = _uic(obj["id"])
            if uic:
                keys.add("uic:" + uic)
    for name in (stop.get("name"), (stop.get("stop_area") or {}).get("name")):
        if name:
            keys.add("name:" + normalize(name))
    return keys


def _clean(text):
    text = re.sub(r"<[^>]+>", " ", str(text or ""))
    return " ".join(text.split())


def disruption_text(disruption):
    messages = [_clean(m.get("text")) for m in disruption.get("messages") or [] if isinstance(m, dict)]
    messages = [m for m in messages if m]
    if messages:
        return min(messages, key=len)  # the short (SMS-style) one
    return _clean((disruption.get("severity") or {}).get("name") or disruption.get("cause"))


def _is_replacement(text):
    folded = f" {words(text)} "
    return any(f" {words(w)} " in folded for w in REPLACEMENT_WORDS)


def _applies(disruption, when):
    if str(disruption.get("status", "")).lower() == "past":
        return False
    periods = disruption.get("application_periods") or []
    if not periods or when is None:
        return True
    for period in periods:
        begin, end = parse_time(period.get("begin")), parse_time(period.get("end"))
        if (begin is None or begin <= when) and (end is None or when <= end):
            return True
    return False


def _section_disruptions(section, by_id):
    ids = []
    for holder in (section.get("display_informations") or {}, section):
        for link in holder.get("links") or []:
            if link.get("type") == "disruption" and link.get("id") in by_id:
                ids.append(link["id"])
    return [by_id[i] for i in dict.fromkeys(ids)]


def convert_journey(journey, by_id):
    """A Navitia journey as an NS-style trip dict, or None.

    Extra keys `_kinds` / `_notes` carry route-wide disruptions (line or
    network level) for check_disruptions()."""
    sections = [s for s in journey.get("sections") or [] if s.get("type") == "public_transport"]
    if not sections:
        return None

    legs, messages, kinds, notes = [], [], set(), []
    for section in sections:
        dep_planned = parse_time(section.get("base_departure_date_time")
                                 or section.get("departure_date_time"))
        arr_planned = parse_time(section.get("base_arrival_date_time")
                                 or section.get("arrival_date_time"))
        if not dep_planned or not arr_planned:
            return None
        dep_actual, arr_actual = dep_planned, arr_planned
        origin_keys, dest_keys = _keys(section.get("from")), _keys(section.get("to"))
        cancelled = replacement = False

        for disruption in _section_disruptions(section, by_id):
            if not _applies(disruption, dep_planned):
                continue
            effect = str((disruption.get("severity") or {}).get("effect", "")).upper()
            text = disruption_text(disruption)
            objects = disruption.get("impacted_objects") or []
            on_trip = any(
                (o.get("pt_object") or {}).get("embedded_type") == "trip" for o in objects
            ) or not objects

            if not on_trip:
                # The whole line/network/station: like the NS disruption feed.
                if effect == "NO_SERVICE":
                    kinds.add("no_trains")
                elif effect in ("REDUCED_SERVICE", "SIGNIFICANT_DELAYS", "DETOUR", "MODIFIED_SERVICE"):
                    kinds.add("reduced_service")
                if _is_replacement(text):
                    kinds.add("replacement_transport")
                if effect != "UNKNOWN_EFFECT" and text:
                    notes.append(text)
                continue

            if effect == "NO_SERVICE":
                cancelled = True
            for obj in objects:
                for stop in obj.get("impacted_stops") or []:
                    keys = _keys(stop)
                    deleted = "deleted" in (str(stop.get("departure_status")),
                                            str(stop.get("arrival_status")),
                                            str(stop.get("stop_time_effect")))
                    if keys & origin_keys:
                        if deleted:
                            cancelled = True
                        elif stop.get("amended_departure_time"):
                            dep_actual = _amend(dep_planned, stop["amended_departure_time"]) or dep_actual
                    if keys & dest_keys:
                        if deleted:
                            cancelled = True  # the train no longer stops where you get off
                        elif stop.get("amended_arrival_time"):
                            arr_actual = _amend(arr_planned, stop["amended_arrival_time"]) or arr_actual
            if effect == "DETOUR" or _is_replacement(text):
                replacement = True
            if text and effect != "UNKNOWN_EFFECT":
                messages.append(text)

        info = section.get("display_informations") or {}
        legs.append({
            "name": " ".join(p for p in (info.get("commercial_mode"), info.get("headsign")) if p),
            "cancelled": cancelled,
            "alternativeTransport": replacement,
            "origin": {"name": (section.get("from") or {}).get("name"),
                       "plannedDateTime": fmt(dep_planned), "actualDateTime": fmt(dep_actual)},
            "destination": {"name": (section.get("to") or {}).get("name"),
                            "plannedDateTime": fmt(arr_planned), "actualDateTime": fmt(arr_actual)},
        })

    any_cancelled = any(leg["cancelled"] for leg in legs)
    return {
        "status": "CANCELLED" if any_cancelled else "NORMAL",
        "transfers": journey.get("nb_transfers", len(legs) - 1),
        "legs": legs,
        "messages": [{"text": m} for m in dict.fromkeys(messages)],
        "_kinds": sorted(kinds),
        "_notes": list(dict.fromkeys(notes)),
    }


def convert_response(data):
    """All journeys of a /journeys answer as NS-style trips, by departure."""
    if not isinstance(data, dict) or not isinstance(data.get("journeys"), list):
        raise RuntimeError("Unexpected response from SNCF journeys API")
    by_id = {}
    for d in data.get("disruptions") or []:
        for key in (d.get("id"), d.get("disruption_id")):
            if key:
                by_id[key] = d

    trips, seen = [], set()
    for journey in data["journeys"]:
        trip = convert_journey(journey, by_id)
        if not trip:
            continue
        legs = trip["legs"]
        key = (legs[0]["origin"]["plannedDateTime"], legs[-1]["destination"]["plannedDateTime"],
               len(legs))
        if key in seen:
            continue
        seen.add(key)
        trips.append(trip)
    trips.sort(key=lambda t: t["legs"][0]["origin"]["plannedDateTime"])
    return trips


def get_trips(start, end, when=None):
    data = api.get(journeys_path(start["code"], end["code"], when))
    return convert_response(data)


def check_disruptions(trip):
    """Line/network-wide disruptions found on this trip's journey."""
    return set(trip.get("_kinds") or []), list(trip.get("_notes") or [])
