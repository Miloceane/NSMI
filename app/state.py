"""Route, settings and the latest status, plus refreshing them from NS."""
import time
from threading import Lock

from . import config as cfgmod
from . import storage
from .config import COLOR_OF_STATUS, STATUS_OF_COLOR
from .evaluate import evaluate_route

NS_MIN_INTERVAL = 60            # seconds between NS checks
STALE_AFTER_SECONDS = 15 * 60   # keep the last good answer this long if NS fails

ROUTE_FILE = "route.json"
DEFAULT_ROUTE = {
    "departure": "Utrecht Centraal",
    "arrival": "Amsterdam Centraal",
}
DEFAULT_STATUS = {
    "status": "ok",
    "color": "green",
    "message": "Running normally",
    "watching": None,
    "reliable": True,
}

lock = Lock()          # protects route, config and status
refresh_lock = Lock()  # only one request talks to NS at a time

# None means "never" (time.monotonic() can be small right after boot).
last_ns_check = None
last_ok = None


def load_route():
    try:
        data = storage.read_json(ROUTE_FILE)
        departure, arrival = data.get("departure"), data.get("arrival")
        if (
            isinstance(departure, str) and departure.strip()
            and isinstance(arrival, str) and arrival.strip()
        ):
            return {"departure": departure.strip(), "arrival": arrival.strip()}
    except (OSError, ValueError, AttributeError):
        pass

    try:
        storage.write_json(ROUTE_FILE, DEFAULT_ROUTE)
    except OSError:
        pass
    return DEFAULT_ROUTE.copy()


route = load_route()
config = cfgmod.load_config()
status = DEFAULT_STATUS.copy()


def _config_copy():
    return {**config, "watch_times": list(config["watch_times"])}


def _check_is_fresh():
    return (
        last_ns_check is not None
        and time.monotonic() - last_ns_check < NS_MIN_INTERVAL
    )


def refresh():
    global last_ns_check, last_ok

    if _check_is_fresh():
        return

    # The others wait here and then reuse the fresh result.
    with refresh_lock:
        if _check_is_fresh():
            return

        with lock:
            checked_route = route.copy()
            checked_config = _config_copy()

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
            # Route or settings changed while we were talking to NS: this
            # result is for the wrong input, discard it.
            if route != checked_route or config != checked_config:
                return

            now = time.monotonic()

            if error is None:
                status.update(
                    color=outcome["color"],
                    status=STATUS_OF_COLOR[outcome["color"]],
                    message=outcome["message"],
                    watching=outcome["watching"],
                    reliable=outcome["reliable"],
                    last_checked=time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                status.pop("error", None)
                if outcome["reliable"]:
                    last_ok = now
            else:
                status["error"] = "Could not reach NS; showing last known status"
                # A short outage keeps the last good colour. After that the
                # old colour would be a lie, so use the "no data" setting.
                if last_ok is None or now - last_ok > STALE_AFTER_SECONDS:
                    color = checked_config["no_data"]
                    status.update(
                        color=color,
                        status=STATUS_OF_COLOR[color],
                        message="Could not reach NS and there is no recent data",
                        watching=None,
                        reliable=False,
                    )

        last_ns_check = time.monotonic()


def invalidate():
    """Force a fresh NS check on the next request."""
    global last_ns_check
    last_ns_check = None


def get_route():
    with lock:
        return route.copy()


def get_config():
    with lock:
        return _config_copy()


def get_status():
    refresh()
    with lock:
        return {**status, "route": route.copy()}


def get_color():
    refresh()
    with lock:
        return status["color"]


def set_route(new_route):
    with lock:
        storage.write_json(ROUTE_FILE, new_route)
        route.update(new_route)
        # The old status belongs to the old route.
        status.clear()
        status.update(DEFAULT_STATUS)
        invalidate()
    return new_route.copy()


def set_config(new_config):
    with lock:
        cfgmod.save_config(new_config)
        config.clear()
        config.update(new_config)
        invalidate()  # re-check with the new settings right away
        return _config_copy()


def set_manual_status(new_status, message):
    global last_ns_check

    defaults = {
        "ok": "Running normally",
        "delayed": "Train delayed",
        "cancelled": "Train cancelled",
    }
    with lock:
        status["status"] = new_status
        status["color"] = COLOR_OF_STATUS[new_status]
        status["message"] = message or defaults[new_status]
        status["watching"] = None
        status["reliable"] = True
        status.pop("error", None)
        # Let the manual value stand for one interval.
        last_ns_check = time.monotonic()
        return status.copy()
