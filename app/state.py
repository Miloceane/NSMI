"""Per-device route, settings and latest status.

Each device's route + settings live in DATA_DIR/devices/<id>.json. Status
is kept in memory. Evaluating a device only reads the API cache of its
provider (see app/api_cache.py, app/providers.py), so it is cheap and never
calls an API itself.
"""
import time
from datetime import datetime, timedelta
from threading import Lock

from . import accounts, api_cache, providers, storage
from .config import COLOR_OF_STATUS, DEFAULT_CONFIG, STATUS_OF_COLOR, clean_config
from .evaluate import evaluate_route
from .i18n import t
from .trips import local_tz

STALE_AFTER_SECONDS = 15 * 60   # older data than this is not trusted
                                # (longer for quiet routes, see _stale_after)

# How fresh each device's data should be (seconds), see refresh_interval().
URGENT_MAX_AGE = 60             # a chosen train leaves within URGENT_WINDOW_MIN
NORMAL_MAX_AGE = 120            # following "the next train", daytime
QUIET_MAX_AGE = 15 * 60         # nothing coming up, night, or nobody watching
URGENT_WINDOW_MIN = 90          # minutes before a chosen train that we watch closely
DAY_HOURS = range(5, 24)        # "following the next train" is quiet outside this
UNWATCHED_AFTER = 10 * 60       # no ESP32/browser poll for this long -> quiet

ROUTE_KEYS = ("departure", "arrival", "departure_id", "arrival_id")


def default_status(lang="en", message=None):
    return {
        "status": "ok",
        "color": "white",
        "message": message or t(lang, "starting"),
        "watching": None,
        "reliable": True,
    }


lock = Lock()
_devices = {}   # device id -> {"route", "config", "status", "last_ok", "manual_until"}


def _file(device_id):
    return f"devices/{device_id}.json"


def provider_of(device_id):
    return providers.of_account(accounts.get(device_id))


def _clean_route(raw):
    """Route dict with only known string keys, or None if incomplete."""
    if not isinstance(raw, dict):
        return None
    route = {k: raw[k] for k in ROUTE_KEYS if isinstance(raw.get(k), str) and raw[k]}
    if not route.get("departure") or not route.get("arrival"):
        return None
    return route


def _defaults(provider):
    """Starting route/settings for a new device: the old single-user files
    if they exist (NS only), otherwise the built-in defaults."""
    route, config = dict(provider.default_route), clean_config(DEFAULT_CONFIG)
    if provider.name != "ns":
        return route, config
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
    provider = provider_of(device_id)
    record = _devices.get(device_id)
    if record is not None and record["provider"] == provider.name:
        return record

    route, config = _defaults(provider)
    try:
        data = storage.read_json(_file(device_id))
        if data.get("provider", "ns") == provider.name:
            route = _clean_route(data.get("route")) or route
        config = clean_config({**DEFAULT_CONFIG, **(data.get("config") or {})})
    except (OSError, ValueError, AttributeError, TypeError):
        pass

    record = {
        "provider": provider.name,
        "route": route,
        "config": config,
        "status": default_status(
            provider.lang, None if _has_route(route) else t(provider.lang, "choose_route")),
        "last_ok": None,
        "manual_until": 0,
        "last_seen": record["last_seen"] if record else time.time(),
    }
    _devices[device_id] = record
    return record


def _save(device_id, record):
    storage.write_json(_file(device_id), {
        "provider": record["provider"],
        "route": record["route"],
        "config": record["config"],
    })


def _config_copy(config):
    return {**config, "watch_times": list(config["watch_times"])}


def _has_route(route):
    return bool(route.get("departure") and route.get("arrival"))


def _run(provider, route, config):
    return evaluate_route(
        route["departure"], route["arrival"], config, provider,
        route.get("departure_id"), route.get("arrival_id"),
    )


# ---------------------------------------------------------------- evaluate

def refresh_interval(config, last_seen, now=None, tz=None):
    """How old this device's data may get before it is fetched again.

    Calls go where a wrong colour would hurt: right before a chosen train.
    """
    now = now or datetime.now(tz or local_tz())
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
    """Recompute one device's status from its provider's cache."""
    with lock:
        record = _load(device_id)
        if time.time() < record["manual_until"]:
            return
        provider = providers.get(record["provider"])
        lang, api = provider.lang, provider.label
        route = record["route"].copy()
        config = _config_copy(record["config"])
        if not _has_route(route):
            record["status"] = default_status(lang, t(lang, "choose_route"))
            return
        tz = local_tz(provider.timezone)
        max_age = refresh_interval(config, record["last_seen"], tz=tz)
        record["max_age"] = max_age

    waiting = error = outcome = None
    api_cache.track_age(max_age)
    try:
        outcome = _run(provider, route, config)
    except api_cache.NeedData:
        waiting = True
    except Exception as exc:
        print(f"[{device_id}] evaluation error:", str(exc))
        error = str(exc)
    age = api_cache.data_age()

    with lock:
        # Route, settings or provider changed meanwhile: this result is for old input.
        if (record["route"] != route or record["config"] != config
                or _devices.get(device_id) is not record):
            return
        status = record["status"]
        now = time.time()

        stale_after = _stale_after(max_age)
        if outcome is not None and age <= stale_after:
            checked = datetime.now(tz) - timedelta(seconds=age)
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
                record["last_ok"] = now - age  # when the data came from the API
            return

        if waiting and status["color"] == "white":
            status["message"] = t(lang, "waiting", api=api)
            return

        if not waiting:
            status["error"] = t(lang, "unreachable_keep", api=api)
        last_ok = record["last_ok"]
        if last_ok is None or now - last_ok > stale_after:
            if waiting and status["color"] == "white":
                return
            # No trustworthy data: white, not a train colour.
            status.update(
                color="white",
                status="unknown",
                message=t(lang, "no_recent" if waiting else "unreachable_none", api=api),
                watching=None,
                reliable=False,
            )


def evaluate_all(provider=None):
    """Re-evaluate every device (of one provider, or of all)."""
    names = [provider.name] if provider else list(providers.PROVIDERS)
    for name in names:
        providers.get(name).api.new_round()
    for device_id, account in accounts.load().items():
        if providers.of_account(account).name in names:
            evaluate(device_id)
    with lock:  # forget removed devices
        known = accounts.load()
        for device_id in [d for d in _devices if d not in known]:
            del _devices[device_id]


def _wake_scheduler():
    from . import scheduler  # imported here: scheduler imports this module
    scheduler.wake()


# ------------------------------------------------------------------ access

def get_provider(device_id):
    with lock:
        return providers.get(_load(device_id)["provider"])


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
        provider = providers.get(record["provider"])
        old = record["route"]
        record["route"] = _clean_route(new_route) or {}
        try:
            _save(device_id, record)
        except OSError:
            record["route"] = old
            raise
        record["status"] = default_status(
            provider.lang, t(provider.lang, "checking_route", api=provider.label))
        record["last_ok"] = None
        record["manual_until"] = 0
        route = record["route"].copy()
        config = _config_copy(record["config"])
    _fetch_now(provider, route, config)  # the person is watching: don't wait
    evaluate(device_id)
    _wake_scheduler()
    return route


def _fetch_now(provider, route, config, max_tries=4):
    """Fetch the data this route needs right away (urgent budget), one
    missing path at a time, instead of waiting for the scheduler."""
    for _ in range(max_tries):
        api_cache.track_age(URGENT_MAX_AGE)
        try:
            _run(provider, route, config)
            return
        except api_cache.NeedData as missing:
            if not provider.api.fetch_urgent(missing.args[0]):
                return  # budget used up: the scheduler will get it
        except Exception:
            return


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
