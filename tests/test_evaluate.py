from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import ns_client, state
from app.config import DEFAULT_CONFIG, clean_config
from app.evaluate import evaluate_route

TZ = ZoneInfo("Europe/Amsterdam")
NOW = datetime.now(TZ).replace(second=0, microsecond=0)

STATIONS = {"payload": [
    {"code": "AH", "UICCode": "1", "namen": {"lang": "Arnhem Centraal", "middel": "Arnhem C.", "kort": "Ah"}},
    {"code": "DTC", "UICCode": "2", "namen": {"lang": "Doetinchem", "middel": "Doetinchem", "kort": "Dtc"}},
    {"code": "AHP", "UICCode": "3", "namen": {"lang": "Arnhem Velperpoort", "middel": "Arnhem Velp.", "kort": "Ahp"}},
]}

DISRUPTION = {
    "title": "Arnhem C.Doetinchem",
    "timespans": [{"situation": {"label": "tussen Arnhem Centraal en Doetinchem rijden er minder stoptreinen."}}],
}


def fmt(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def trip(dep_min, dur=30, delay=0, cancelled=False):
    dep = NOW + timedelta(minutes=dep_min)
    arr = dep + timedelta(minutes=dur)
    delayed = timedelta(minutes=delay)
    return {
        "status": "CANCELLED" if cancelled else "NORMAL",
        "transfers": 0,
        "legs": [{
            "cancelled": cancelled,
            "origin": {"name": "Arnhem Centraal", "uicCode": "1",
                       "plannedDateTime": fmt(dep), "actualDateTime": fmt(dep + delayed)},
            "destination": {"name": "Doetinchem", "uicCode": "2",
                            "plannedDateTime": fmt(arr), "actualDateTime": fmt(arr + delayed)},
            "stops": [{"name": "Arnhem Velperpoort", "uicCode": "3"}],
        }],
    }


@pytest.fixture
def ns(monkeypatch):
    data = {"trips": [], "disruptions": []}

    def fake_get(path):
        if path.startswith("/v2/stations"):
            return STATIONS
        if path.startswith("/v3/disruptions"):
            return data["disruptions"]
        if path.startswith("/v3/trips"):
            return {"trips": data["trips"]}
        raise AssertionError(path)

    monkeypatch.setattr(ns_client, "ns_get", fake_get)
    monkeypatch.setattr(ns_client, "_stations", [])
    return data


def run(cfg=None):
    return evaluate_route("Arnhem Centraal", "Doetinchem", {**DEFAULT_CONFIG, **(cfg or {})})


def test_healthy_is_green(ns):
    ns["trips"] = [trip(10), trip(40)]
    assert run()["color"] == "green"


def test_reduced_service_default_yellow_and_user_choice(ns):
    ns["trips"] = [trip(10), trip(40)]
    ns["disruptions"] = [DISRUPTION]
    assert run()["color"] == "yellow"
    assert run({"reduced_service": "green"})["color"] == "green"
    assert run({"reduced_service": "red"})["color"] == "red"


def test_delay_thresholds(ns):
    ns["trips"] = [trip(10, delay=5), trip(40)]
    assert run()["color"] == "yellow"
    assert run({"delay_yellow_min": 10, "delay_red_min": 20})["color"] == "green"
    ns["trips"] = [trip(10, delay=20), trip(40)]
    assert run()["color"] == "red"


def test_usual_cancelled_but_alternative_works(ns):
    ns["trips"] = [trip(10, cancelled=True), trip(20)]
    assert run()["color"] == "yellow"


def test_everything_cancelled(ns):
    ns["trips"] = [trip(10, cancelled=True), trip(40, cancelled=True)]
    assert run()["color"] == "red"


def test_watched_train_decides(ns):
    ns["trips"] = [trip(10), trip(40, delay=25), trip(70)]
    chosen = (NOW + timedelta(minutes=40)).strftime("%H:%M")
    result = run({"watch_times": [chosen]})
    assert result["color"] == "red" and result["watching"] == chosen


def test_watched_time_slightly_off_uses_closest_train(ns):
    ns["trips"] = [trip(40), trip(70)]
    typed = (NOW + timedelta(minutes=41)).strftime("%H:%M")
    result = run({"watch_times": [typed]})
    assert result["watching"] == (NOW + timedelta(minutes=40)).strftime("%H:%M")
    assert "closest" in result["message"]


def test_watched_time_without_train_falls_back_to_next_train(ns):
    ns["trips"] = [trip(10), trip(40)]
    result = run({"watch_times": [(NOW + timedelta(minutes=300)).strftime("%H:%M")]})
    assert result["color"] == "green" and result["reliable"]
    assert "following the next train" in result["message"]


@pytest.mark.parametrize("trips", [[]])
def test_no_trips_uses_no_data_colour_not_unknown(ns, trips):
    ns["trips"] = trips
    result = run({"no_data": "red"})
    assert result["color"] == "red" and not result["reliable"]


def test_unknown_station_uses_no_data_colour(ns):
    result = evaluate_route("Nowhere", "Doetinchem", dict(DEFAULT_CONFIG))
    assert result["color"] == "yellow" and not result["reliable"]


def test_config_validation():
    with pytest.raises(ValueError):
        clean_config({**DEFAULT_CONFIG, "delay_yellow_min": 30, "delay_red_min": 10})
    with pytest.raises(ValueError):
        clean_config({**DEFAULT_CONFIG, "no_trains": "purple"})
    with pytest.raises(ValueError):
        clean_config({**DEFAULT_CONFIG, "watch_times": ["25:00"]})
    assert clean_config({**DEFAULT_CONFIG, "watch_times": ["7:48", "07:48"]})["watch_times"] == ["07:48"]


def test_ns_outage_keeps_last_good_colour_then_falls_back(monkeypatch):
    calls = {"fail": False}

    def fake_eval(*args):
        if calls["fail"]:
            raise RuntimeError("down")
        return {"color": "red", "message": "bad", "watching": None, "reliable": True}

    monkeypatch.setattr(state, "evaluate_route", fake_eval)
    state.invalidate()
    assert state.get_status()["color"] == "red"

    calls["fail"] = True
    state.invalidate()
    assert state.get_status()["color"] == "red"          # short outage: keep last good

    state.last_ok -= state.STALE_AFTER_SECONDS + 1       # pretend it was long ago
    state.invalidate()
    assert state.get_status()["color"] == DEFAULT_CONFIG["no_data"]
