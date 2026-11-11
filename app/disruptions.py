from . import ns_client
from .text import mentions, words

# A disruption that puts buses/taxis in place of trains counts as
# "replacement transport", even if it also says there are no trains.
BUS_WORDS = {
    "bus", "bussen", "buses", "busvervoer",
    "taxi", "taxis", "taxibus", "taxibussen",
    "vervangend",
}
NO_BUS_PHRASES = [
    "geen bussen",
    "geen bus ",
    "no buses",
    "geen alternatief",
    "geen vervangend",
    "no alternative",
]

NO_TRAIN_PHRASES = [
    "geen treinen",
    "er rijden geen treinen",
    "no trains",
    "all trains cancelled",
]


def route_stops(trip):
    """Every station the trip passes: a list of name-lists (one per station)."""
    by_uic = {
        str(s.get("UICCode")): s for s in ns_client.get_stations() if s.get("UICCode")
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
            names = ns_client.match_names(station) if station else []
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


def classify_disruption(text):
    """'replacement_transport', 'no_trains' or 'reduced_service'."""
    folded = text.casefold()

    has_buses = bool(set(words(text).split()) & BUS_WORDS)
    if has_buses and not any(p in folded for p in NO_BUS_PHRASES):
        return "replacement_transport"

    if any(phrase in folded for phrase in NO_TRAIN_PHRASES):
        return "no_trains"

    return "reduced_service"


def check_disruptions(stops):
    """Disruptions that mention at least two stations of the route.

    NS names the two ends of the affected stretch, which can be stations
    in the middle of someone's trip, so every stop is considered.
    Returns (set of "replacement_transport" / "no_trains" / "reduced_service",
    [sentences]).
    """
    disruptions = ns_client.ns_get("/v3/disruptions?isActive=true")

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

        kinds.add(classify_disruption(text))
        notes.append(disruption_summary(disruption))

    return kinds, notes