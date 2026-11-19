"""Per-device route, settings and latest status.

Each device's route + settings live in DATA_DIR/devices/<id>.json. Status
is kept in memory. Evaluating a device only reads the NS cache (see
app/ns_client.py), so it is cheap and never calls NS itself.
"""
import time
from datetime import datetime, timedelta
from threading import Lock

from . import accounts, ns_client, storage
from .config import COLOR_OF_STATUS, DEFAULT_CONFIG, STATUS_OF_COLOR, clean_config
from .evaluate import evaluate_route
from .trips import local_tz

STALE_AFTER_SECONDS = 15 * 60   # older NS data than this is not trusted
                                # (longer for quiet routes, see _stale_after)

# How fresh each device's data should be (seconds), see refresh_interval().
URGENT_MAX_AGE = 60             # a chosen train leaves within URGENT_WINDOW_MIN
NORMAL_MAX_AGE = 120            # following "the next train", daytime
QUIET_MAX_AGE = 15 * 60         # nothing coming up, night, or nobody watching
URGENT_WINDOW_MIN = 90          # minutes before a chosen train that we watch closely
DAY_HOURS = range(5, 24)        # "following the next train" is quiet outside this
UNWATCHED_AFTER = 10 * 60       # no ESP32/browser poll for this long -> quiet

DEFAULT_ROUTE = {
    "departure": "Utrecht Centraal",
    "arrival": "Amsterdam Centraal",
}
DEFAULT_STATUS = {
    "status": "ok",
    "color": "white",
    "message": "Starting up, no data yet",
    "watching": None,
    "reliable": True,
}

lock = Lock()
_devices = {}   # device id -> {"route", "config", "status", "last_ok", "manual_until"}


def _file(device_id):
    return f"devices/{device_id}.json"


def _defaults():
    """Starting route/settings for a new device: the old single-user files
    if they exist, otherwise the built-in defaults."""
    route, config = DEFAULT_ROUTE.copy(), clean_config(DEFAULT_CONFIG)
    try:
        data = storage.read_json("route.json")
        if data.get("departure") and data.get("arrival"):
            route = {"departure": data["departure"], "arrival": data["arrival"]}
    except (OSError, ValueError, AttributeError):
        pass
    try:
        config = clean_config({**DEFAULT_CONFIG, **storage.read_json("config.json")})
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return route, config


def _load(device_id):
    """In-memory record for a device (caller holds the lock)."""
    if device_id in _devices:
        return _devices[device_id]

    route, config = _defaults()
    try:
        data = storage.read_json(_file(device_id))
        r = data.get("route") or {}
        if isinstance(r.get("departure"), str) and isinstance(r.get("arrival"), str):
            route = {"departure": r["departure"], "arrival": r["arrival"]}
        config = clean_config({**DEFAULT_CONFIG, **(data.get("config") or {})})
    except (OSError, ValueError, AttributeError, TypeError):
        pass

    record = {
        "route": route,
        "config": config,
        "status": DEFAULT_STATUS.copy(),
        "last_ok": None,
        "manual_until": 0,
        "last_seen": time.time(),
    }
    _devices[device_id] = record
    return record


def _save(device_id, record):
    storage.write_json(_file(device_id), {"route": record["route"], "config": record["config"]})


def _config_copy(config):
    return {**config, "watch_times": list(config["watch_times"])}


# ---------------------------------------------------------------- evaluate

def refresh_interval(config, last_seen, now=None):
    """How old this device's NS data may get before it is fetched again.

    Calls go where a wrong colour would hurt: right before a chosen train.
    """
    now = now or datetime.now(local_tz())
    if time.time() - last_seen > UNWATCHED_AFTER:
        return QUIET_MAX_AGE

    if config["watch_times"]:
        for item in config["watch_times"]:
            hour, minute = (int(p) for p in item.split(":"))
            leaves = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            for target in (leaves, leaves + timedelta(days=1)):
                minutes_left = (target - now).total_seconds() / 60
                if -10 <= minutes_left <= URGENT_WINDOW_MIN:
                    return URGENT_MAX_AGE
        return QUIET_MAX_AGE

    return NORMAL_MAX_AGE if now.hour in DAY_HOURS else QUIET_MAX_AGE


def _stale_after(max_age):
    return max(STALE_AFTER_SECONDS, 2 * max_age + 5 * 60)


def evaluate(device_id):
    """Recompute one device's status from the NS cache."""
    with lock:
        record = _load(device_id)
        if time.time() < record["manual_until"]:
            return
        route = record["route"].copy()
        config = _config_copy(record["config"])
        max_age = refresh_interval(config, record["last_seen"])
        record["max_age"] = max_age

    waiting = error = outcome = None
    ns_client.track_age(max_age)
    try:
        outcome = evaluate_route(route["departure"], route["arrival"], config)
    except ns_client.NeedData:
        waiting = True
    except Exception as exc:
        print(f"[{device_id}] evaluation error:", str(exc))
        error = str(exc)
    age = ns_client.data_age()

    with lock:
        # Route or settings changed meanwhile: this result is for old input.
        if record["route"] != route or record["config"] != config:
            return
        status = record["status"]
        now = time.time()

        stale_after = _stale_after(max_age)
        if outcome is not None and age <= stale_after:
            checked = datetime.now(local_tz()) - timedelta(seconds=age)
            status.update(
                color=outcome["color"],
                status=STATUS_OF_COLOR[outcome["color"]],
                message=outcome["message"],
                watching=outcome["watching"],
                reliable=outcome["reliable"],
                last_checked=checked.strftime("%Y-%m-%d %H:%M:%S %Z"),
            )
            status.pop("error", None)
            if outcome["reliable"]:
                record["last_ok"] = now - age  # when the data came from NS
            return

        if waiting and status["color"] == "white":
            status["message"] = "Waiting for NS data (can take a few minutes)"
            return

        if not waiting:
            status["error"] = "Could not reach NS; showing last known status"
        last_ok = record["last_ok"]
        if last_ok is None or now - last_ok > stale_after:
            if waiting and status["color"] == "white":
                return
            color = config["no_data"]
            status.update(
                color=color,
                status=STATUS_OF_COLOR[color],
                message="No recent NS data" if waiting else
                        "Could not reach NS and there is no recent data",
                watching=None,
                reliable=False,
            )


def evaluate_all():
    ns_client.new_round()
    for device_id in accounts.load():
        evaluate(device_id)
    with lock:  # forget removed devices
        for device_id in [d for d in _devices if d not in accounts.load()]:
            del _devices[device_id]


def _wake_scheduler():
    from . import scheduler  # imported here: scheduler imports this module
    scheduler.wake()


# ------------------------------------------------------------------ access

def get_route(device_id):
    with lock:
        return _load(device_id)["route"].copy()


def get_config(device_id):
    with lock:
        return _config_copy(_load(device_id)["config"])


def _seen(device_id):
    """Someone (the ESP32 or a browser) looked at this device."""
    record = _load(device_id)
    quiet = time.time() - record["last_seen"] > UNWATCHED_AFTER
    record["last_seen"] = time.time()
    return quiet


def get_status(device_id):
    with lock:
        woke_up = _seen(device_id)
        record = _load(device_id)
        result = {
            **record["status"],
            "route": record["route"].copy(),
            "refresh_seconds": record.get("max_age"),
        }
    if woke_up:
        evaluate(device_id)  # ask for fresh data at the normal pace again
    return result


def get_color(device_id):
    with lock:
        woke_up = _seen(device_id)
        color = _load(device_id)["status"]["color"]
    if woke_up:
        evaluate(device_id)
    return color


def set_route(device_id, new_route):
    with lock:
        record = _load(device_id)
        old = record["route"]
        record["route"] = new_route.copy()
        try:
            _save(device_id, record)
        except OSError:
            record["route"] = old
            raise
        record["status"] = {**DEFAULT_STATUS, "message": "Checking NS for your new route..."}
        record["last_ok"] = None
        record["manual_until"] = 0
    evaluate(device_id)  # instant if another device already uses this route
    _wake_scheduler()
    return new_route.copy()


def set_config(device_id, new_config):
    with lock:
        record = _load(device_id)
        old = record["config"]
        record["config"] = new_config
        try:
            _save(device_id, record)
        except OSError:
            record["config"] = old
            raise
        record["manual_until"] = 0
    evaluate(device_id)
    _wake_scheduler()
    return get_config(device_id)


def set_manual_status(device_id, new_status, message, hold_seconds=60):
    defaults = {
        "ok": "Running normally",
        "delayed": "Train delayed",
        "cancelled": "Train cancelled",
    }
    with lock:
        record = _load(device_id)
        status = record["status"]
        status["status"] = new_status
        status["color"] = COLOR_OF_STATUS[new_status]
        status["message"] = message or defaults[new_status]
        status["watching"] = None
        status["reliable"] = True
        status.pop("error", None)
        record["manual_until"] = time.time() + hold_seconds
        return status.copy()
