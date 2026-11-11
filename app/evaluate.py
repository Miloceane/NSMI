from . import ns_client
from .config import COLOR_RANK
from .disruptions import check_disruptions, route_stops
from .trips import (
    TRIPS_TO_CHECK,
    fetch_trips,
    find_watched,
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
    """NS gave us nothing usable. Never 'unknown': use the configured colour."""
    return result(cfg["no_data"], message, reliable=False)


def evaluate_route(departure, arrival, cfg):
    start = ns_client.find_station(departure)
    end = ns_client.find_station(arrival)

    if not start:
        return no_data(cfg, f"Departure station not found: {departure}")

    if not end:
        return no_data(cfg, f"Arrival station not found: {arrival}")

    watching = None
    note = ""
    usable = []

    if cfg["watch_times"]:
        watching, usable, found_note = find_watched(start, end, cfg["watch_times"])
        if usable:
            note = f"{found_note}. " if found_note else ""
        else:
            note = (
                f"No train found at {', '.join(cfg['watch_times'])}; "
                "following the next train. "
            )

    if not usable:
        usable = fetch_trips(start, end)[:TRIPS_TO_CHECK]
        if not usable:
            return no_data(cfg, "NS returned no trips for this route")

    # The first trip in the list is the reference: the next train, or the
    # chosen one. Everything is measured against when it should arrive.
    usual_trip, usual = usable[0]

    working = [(t, tm) for t, tm in usable if not trip_is_cancelled(t)]
    if not working:
        return result(
            cfg["all_cancelled"],
            f"{note}All of the next {len(usable)} trips are cancelled",
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
    return result(color, note + summary, watching)
