"""Rate limiting, shared cache and per-device state."""
import pytest

from app import accounts, ns_client, scheduler, state, storage

from test_evaluate import STATIONS, trip


@pytest.fixture
def fake_ns(monkeypatch):
    calls = []
    clock = {"t": 1_000_000.0}
    data = {"trips": [trip(10), trip(40)], "fail": False}

    def http_get(path):
        calls.append(path)
        if data["fail"]:
            raise RuntimeError("down")
        if path.startswith("/v2/stations"):
            return STATIONS
        if path.startswith("/v3/disruptions"):
            return []
        return {"trips": data["trips"]}

    monkeypatch.setattr(ns_client, "_http_get", http_get)
    monkeypatch.setattr(ns_client.time, "time", lambda: clock["t"])
    monkeypatch.setattr(state.time, "time", lambda: clock["t"])
    monkeypatch.setattr(ns_client, "CALL_INTERVAL", 60)
    monkeypatch.setattr(state, "refresh_interval", lambda *a, **k: 60)
    ns_client.clear_cache()
    try:
        (storage.DATA_DIR / ns_client.LAST_CALL_FILE).unlink()
    except OSError:
        pass
    accounts.save({
        "a": {"token": "ta", "password_hash": accounts.hash_password("x")},
        "b": {"token": "tb", "password_hash": accounts.hash_password("x")},
    })
    for d in ("a", "b"):
        try:
            (storage.DATA_DIR / "devices" / f"{d}.json").unlink()
        except OSError:
            pass
    state._devices.clear()
    for d in ("a", "b"):
        state.set_route(d, {"departure": "Arnhem Centraal", "arrival": "Doetinchem"})
    # set_route fetches urgently; start the tests from a clean slate.
    calls.clear()
    ns_client.clear_cache()
    ns_client._urgent_calls.clear()
    try:
        (storage.DATA_DIR / ns_client.LAST_CALL_FILE).unlink()
    except OSError:
        pass
    yield SimpleClock(clock), calls, data
    accounts.save({})
    state._devices.clear()


class SimpleClock:
    def __init__(self, clock):
        self.clock = clock

    def advance(self, seconds):
        self.clock["t"] += seconds


def test_never_more_than_one_call_per_interval(fake_ns):
    clock, calls, _ = fake_ns
    scheduler.run_once(wait=False)
    assert len(calls) == 1
    for _ in range(5):                      # no time passes: no more calls
        scheduler.run_once(wait=False)
    assert len(calls) == 1
    clock.advance(59)
    scheduler.run_once(wait=False)
    assert len(calls) == 1
    clock.advance(1)
    scheduler.run_once(wait=False)
    assert len(calls) == 2


def test_last_call_survives_restart(fake_ns):
    clock, calls, _ = fake_ns
    scheduler.run_once(wait=False)
    ns_client.clear_cache()                 # like a restart: memory gone
    scheduler.run_once(wait=False)
    assert len(calls) == 1


def test_saving_route_fetches_immediately(fake_ns, monkeypatch):
    clock, calls, _ = fake_ns
    monkeypatch.setattr(ns_client.time, "sleep", lambda s: clock.advance(s))
    state.set_route("a", {"departure": "Doetinchem", "arrival": "Arnhem Centraal"})
    assert state.get_status("a")["color"] != "white"   # no scheduler involved
    assert any("fromStation=DTC" in c for c in calls)


def test_urgent_fetches_are_capped(fake_ns, monkeypatch):
    clock, calls, _ = fake_ns
    monkeypatch.setattr(ns_client.time, "sleep", lambda s: clock.advance(s))
    for i in range(ns_client.URGENT_PER_MINUTE + 5):
        ns_client.fetch_urgent(f"/x{i}")
    assert len(calls) == ns_client.URGENT_PER_MINUTE


def run_minutes(clock, n):
    for _ in range(n):
        scheduler.run_once(wait=False)
        clock.advance(60)


def test_devices_have_own_routes_and_share_identical_data(fake_ns):
    clock, calls, _ = fake_ns
    state.set_route("b", {"departure": "Doetinchem", "arrival": "Arnhem Centraal"})
    assert state.get_route("a") != state.get_route("b")

    # stations, disruptions, trips a, trips b: 4 different things to fetch
    run_minutes(clock, 6)
    assert state.get_status("a")["color"] == "green"
    assert state.get_status("b")["color"] == "green"

    # same route on both devices -> same cached trips, no extra call
    before = len(calls)
    state.set_route("b", state.get_route("a"))
    assert state.get_status("b")["color"] == "green"
    assert len(calls) == before


def test_settings_are_per_device(fake_ns):
    clock, _, data = fake_ns
    data["trips"] = [trip(10, delay=5), trip(40)]
    run_minutes(clock, 4)
    assert state.get_color("a") == "yellow"
    cfg = state.get_config("b")
    state.set_config("b", {**cfg, "delay_yellow_min": 10, "delay_red_min": 20})
    assert state.get_color("b") == "green"
    assert state.get_color("a") == "yellow"


def test_ns_outage_keeps_colour_then_falls_back(fake_ns):
    clock, _, data = fake_ns
    data["trips"] = [trip(10, delay=20), trip(40)]
    run_minutes(clock, 4)
    assert state.get_color("a") == "red"

    data["fail"] = True
    run_minutes(clock, 5)
    assert state.get_color("a") == "red"            # short outage: keep last good
    run_minutes(clock, 15)
    assert state.get_color("a") == "white"          # stale: no trustworthy data
    assert not state.get_status("a")["reliable"]



def test_urgent_route_is_fetched_before_quiet_one(fake_ns, monkeypatch):
    clock, calls, _ = fake_ns
    state.set_route("b", {"departure": "Doetinchem", "arrival": "Arnhem Centraal"})
    run_minutes(clock, 5)                      # everything fetched once
    monkeypatch.setattr(
        state, "refresh_interval",
        lambda config, last_seen, now=None: 60 if config["delay_red_min"] == 99 else 900,
    )
    cfg = state.get_config("a")
    state.set_config("a", {**cfg, "delay_red_min": 99})   # marks "a" as urgent
    before = len(calls)
    run_minutes(clock, 6)
    new = calls[before:]
    # trips and disruptions for "a" alternate; "b" has to wait
    assert new.count("/v3/trips?fromStation=AH&toStation=DTC") >= 3
    assert "/v3/trips?fromStation=DTC&toStation=AH" not in new


def test_refresh_interval_policy():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Europe/Amsterdam")
    seen = state.time.time()
    morning = datetime(2026, 11, 19, 7, 0, tzinfo=tz)
    night = datetime(2026, 11, 19, 2, 0, tzinfo=tz)
    watched = {"watch_times": ["07:48"]}
    assert state.refresh_interval(watched, seen, morning) == state.URGENT_MAX_AGE
    assert state.refresh_interval({"watch_times": ["17:30"]}, seen, morning) == state.QUIET_MAX_AGE
    assert state.refresh_interval({"watch_times": []}, seen, morning) == state.NORMAL_MAX_AGE
    assert state.refresh_interval({"watch_times": []}, seen, night) == state.QUIET_MAX_AGE
    # nobody has polled the device for a while
    assert state.refresh_interval(watched, seen - 3600, morning) == state.QUIET_MAX_AGE