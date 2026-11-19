from datetime import datetime

from . import api_cache, providers
from .config import COLOR_RANK
from .i18n import t
from .trips import (
    TRIPS_TO_CHECK,
    fetch_trips,
    find_watched,
    local_tz,
    trip_has_issue,
    trip_is_cancelled,
    trip_messages,
)


def result(color, message, watching=None, reliable=True):
    return {
        "color": color,
        "message": message[:400],
        "watching": watching,
        "reliable": reliable,
    }


def no_data(cfg, message):
    """The API gave us nothing usable. Never 'unknown': use the configured colour."""
    return result(cfg["no_data"], message, reliable=False)


def evaluate_route(departure, arrival, cfg, provider=None,
                   departure_id=None, arrival_id=None):
    provider = provider or providers.get(None)
    lang, api = provider.lang, provider.label
    start = provider.find_station(departure, departure_id)
    end = provider.find_station(arrival, arrival_id)

    if not start:
        return no_data(cfg, t(lang, "dep_not_found", name=departure))

    if not end:
        return no_data(cfg, t(lang, "arr_not_found", name=arrival))

    watching = None
    note = ""
    usable = []

    if cfg["watch_times"]:
        watching, usable, found_note = find_watched(provider, start, end, cfg["watch_times"])
        if usable:
            note = f"{found_note}. " if found_note else ""
        else:
            note = t(lang, "no_watched", times=", ".join(cfg["watch_times"]))

    if not usable:
        now = datetime.now(local_tz(provider.timezone))
        trips = fetch_trips(provider, start, end)
        upcoming = [(tr, tm) for tr, tm in trips if tm["dep_actual"] >= now]
        usable = (upcoming or trips)[:TRIPS_TO_CHECK]
        if not usable:
            return no_data(cfg, t(lang, "no_trips", api=api))

    # The first trip in the list is the reference: the next train, or the
    # chosen one. Everything is measured against when it should arrive.
    usual_trip, usual = usable[0]

    working = [(tr, tm) for tr, tm in usable if not trip_is_cancelled(tr)]
    if not working:
        return result(
            cfg["all_cancelled"],
            note + t(lang, "all_cancelled", n=len(usable)),
            watching,
        )

    best_trip, best = min(working, key=lambda item: item[1]["arr_actual"])
    late = round((best["arr_actual"] - usual["arr_planned"]).total_seconds() / 60)

    reference = t(lang, "ref_watched", time=watching) if watching else t(lang, "ref_usual")
    summary = t(
        lang, "summary",
        dep=f"{best['dep_actual']:%H:%M}", arr=f"{best['arr_actual']:%H:%M}",
        late=late, ref=reference,
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
        kinds, notes = provider.check_disruptions(best_trip)
    except api_cache.NeedData:
        raise  # not fetched yet: wait for it rather than show a partial answer
    except Exception as exc:
        print(f"{api} disruptions error:", str(exc))
        kinds, notes = set(), []
        summary += t(lang, "no_disruptions", api=api)

    colors.extend(cfg[kind] for kind in kinds)

    notes = [n for n in notes if n not in summary]
    if notes:
        summary = " | ".join(notes[:2]) + " | " + summary

    color = max(colors, key=COLOR_RANK.get)
    return result(color, note + summary, watching)
