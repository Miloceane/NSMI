import re

from . import storage

COLOR_RANK = {"green": 0, "yellow": 1, "red": 2}

# Older clients read "status"; the ESP32 should use "color".
STATUS_OF_COLOR = {"green": "ok", "yellow": "delayed", "red": "cancelled"}
COLOR_OF_STATUS = {v: k for k, v in STATUS_OF_COLOR.items()}

# What colour each situation gives. The worst colour among all situations
# that apply wins. "green" for a situation means "ignore it".
DEFAULT_CONFIG = {
    "delay_yellow_min": 3,              # arrives this many minutes late -> yellow
    "delay_red_min": 15,                # arrives this many minutes late -> red
    "usual_cancelled": "yellow",        # the train is cancelled but a later one works
    "replacement_transport": "yellow",  # replacement bus / odd trip status
    "reduced_service": "yellow",        # NS: fewer trains on the route
    "no_trains": "red",                 # NS: no trains on the route
    "all_cancelled": "red",             # every upcoming trip is cancelled
    "no_data": "yellow",                # NS unreachable / no answer (never "unknown")
    "watch_times": [],                  # ["07:48", "08:18"]; empty = next train
}
CONDITION_KEYS = [
    "usual_cancelled",
    "replacement_transport",
    "reduced_service",
    "no_trains",
    "all_cancelled",
    "no_data",
]
CONFIG_KEYS = ["delay_yellow_min", "delay_red_min", *CONDITION_KEYS, "watch_times"]

CONFIG_FILE = "config.json"


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
    try:
        data = storage.read_json(CONFIG_FILE)
        return clean_config({**DEFAULT_CONFIG, **data})
    except (OSError, ValueError, AttributeError, TypeError):
        pass

    config = clean_config(DEFAULT_CONFIG)
    try:
        storage.write_json(CONFIG_FILE, config)
    except OSError:
        pass
    return config


def save_config(config):
    storage.write_json(CONFIG_FILE, config)
