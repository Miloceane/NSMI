"""SNCF (Navitia) parsing, and SNCF/NS budgets being independent."""
from datetime import datetime, timedelta

import pytest

from app import accounts, api_cache, ns_client, providers, scheduler, sncf_client, state, storage
from app.config import DEFAULT_CONFIG
from app.evaluate import evaluate_route

from test_multi import clear_api

PARIS = sncf_client.PARIS
NOW = datetime.now(PARIS).replace(second=0, microsecond=0)
DEP = "stop_area:SNCF:87686006"   # Paris Gare de Lyon
ARR = "stop_area:SNCF:87723197"   # Lyon Part-Dieu


def nav(dt):
    return dt.strftime("%Y%m%dT%H%M%S")


def stop(area_id, name):
    return {
        "id": area_id, "name": name, "embedded_type": "stop_point",
        "stop_point": {"id": area_id.replace("stop_area", "stop_point") + ":Train",
                       "name": name, "stop_area": {"id": area_id, "name": name}},
    }


def journey(dep_min, dur=120, disruption_ids=()):
    dep = NOW + timedelta(minutes=dep_min)
    arr = dep + timedelta(minutes=dur)
    return {
        "nb_transfers": 0,
        "sections": [
            {"type": "street_network", "duration": 0},
            {
                "type": "public_transport",
                "from": stop(DEP, "Paris Gare de Lyon"),
                "to": stop(ARR, "Lyon Part-Dieu"),
                "base_departure_date_time": nav(dep), "departure_date_time": nav(dep),
                "base_arrival_date_time": nav(arr), "arrival_date_time": nav(arr),
                "display_informations": {
                    "commercial_mode": "TGV INOUI", "headsign": str(6600 + dep_min),
                    "links": [{"type": "disruption", "id": d, "rel": "disruptions"}
                              for d in disruption_ids],
                },
            },
        ],
    }


def delay(did, dep_min, minutes, dur=120):
    dep = NOW + timedelta(minutes=dep_min)
    arr = dep + timedelta(minutes=dur)
    late = timedelta(minutes=minutes)
    return {
        "id": did, "status": "active",
        "severity": {"effect": "SIGNIFICANT_DELAYS", "name": "trip delayed"},
        "messages": [{"text": f"Retard de {minutes} min : panne de signalisation"}],
        "impacted_objects": [{
            "pt_object": {"embedded_type": "trip", "id": "OCE:x"},
            "impacted_stops": [
                {"stop_point": stop(DEP, "Paris Gare de Lyon")["stop_point"],
                 "departure_status": "delayed",
                 "amended_departure_time": (dep + late).strftime("%H%M%S")},
                {"stop_point": stop(ARR, "Lyon Part-Dieu")["stop_point"],
                 "arrival_status": "delayed",
                 "amended_arrival_time": (arr + late).strftime("%H%M%S")},
            ],
        }],
    }


def cancel(did):
    return {
        "id": did, "status": "active",
        "severity": {"effect": "NO_SERVICE", "name": "trip canceled"},
        "messages": [{"text": "Train supprimé"}],
        "impacted_objects": [{"pt_object": {"embedded_type": "trip", "id": "OCE:y"}}],
    }


def line_disruption(did, effect, text):
    return {
        "id": did, "status": "active", "severity": {"effect": effect},
        "messages": [{"text": text}],
        "impacted_objects": [{"pt_object": {"embedded_type": "line", "id": "line:x"}}],
    }


@pytest.fixture
def sncf(monkeypatch):
    data = {"journeys": [], "disruptions": []}

    def fake_get(path, max_age=None, track=True):
        assert path.startswith("/journeys?")
        return {"journeys": data["journeys"], "disruptions": data["disruptions"]}

    monkeypatch.setattr(sncf_client.api, "get", fake_get)
    return data


def run(cfg=None):
    return evaluate_route(
        "Paris Gare de Lyon", "Lyon Part-Dieu", {**DEFAULT_CONFIG, **(cfg or {})},
        providers.get("sncf"), DEP, ARR,
    )


def test_on_time_is_green_and_french(sncf):
    sncf["journeys"] = [journey(10), journey(40)]
    result = run()
    assert result["color"] == "green"
    assert "Meilleure option" in result["message"]


def test_delay_from_impacted_stops(sncf):
    sncf["journeys"] = [journey(10, disruption_ids=["d1"]), journey(70)]
    sncf["disruptions"] = [delay("d1", 10, 20)]
    result = run()
    assert result["color"] == "red"
    assert "+20 min" in result["message"]
    assert "panne de signalisation" in result["message"]


def test_cancelled_train_then_later_works(sncf):
    sncf["journeys"] = [journey(10, disruption_ids=["c"]), journey(20)]
    sncf["disruptions"] = [cancel("c")]
    assert run()["color"] == "yellow"         # usual_cancelled default


def test_all_cancelled(sncf):
    sncf["journeys"] = [journey(10, disruption_ids=["c"]), journey(40, disruption_ids=["c"])]
    sncf["disruptions"] = [cancel("c")]
    assert run()["color"] == "red"


def test_line_disruptions_map_to_settings(sncf):
    sncf["journeys"] = [journey(10, disruption_ids=["l"]), journey(40)]
    sncf["disruptions"] = [line_disruption("l", "REDUCED_SERVICE", "Trafic réduit")]
    assert run()["color"] == "yellow"
    assert run({"reduced_service": "green"})["color"] == "green"
    sncf["disruptions"] = [line_disruption("l", "NO_SERVICE", "Aucun train")]
    assert run()["color"] == "red"
    sncf["disruptions"] = [line_disruption("l", "OTHER_EFFECT", "Cars de substitution entre A et B")]
    result = run({"replacement_transport": "red"})
    assert result["color"] == "red" and "Cars de substitution" in result["message"]


def test_watched_train(sncf):
    sncf["journeys"] = [journey(10), journey(40, disruption_ids=["d"]), journey(70)]
    sncf["disruptions"] = [delay("d", 40, 25)]
    chosen = (NOW + timedelta(minutes=40)).strftime("%H:%M")
    result = run({"watch_times": [chosen]})
    assert result["color"] == "red" and result["watching"] == chosen


def test_no_solution_is_no_data(sncf):
    result = run({"no_data": "red"})
    assert result["color"] == "red" and not result["reliable"]


def test_missing_station_id():
    result = evaluate_route("Paris", "Lyon", dict(DEFAULT_CONFIG), providers.get("sncf"))
    assert not result["reliable"] and "introuvable" in result["message"]


def test_search_parses_places(monkeypatch):
    monkeypatch.setattr(sncf_client.api, "get_now", lambda path, max_age: {"places": [
        {"embedded_type": "stop_area", "id": DEP, "name": "Paris Gare de Lyon (Paris)",
         "stop_area": {"name": "Paris Gare de Lyon"}},
        {"embedded_type": "address", "id": "2.3;48.8", "name": "Rue de Lyon"},
        {"embedded_type": "stop_area", "id": "bad id; drop", "name": "x"},
    ]})
    assert sncf_client.search("gare de lyon") == [
        {"id": DEP, "name": "Paris Gare de Lyon", "label": "Paris Gare de Lyon (Paris)"}]
    station, hints = sncf_client.resolve("Paris Gare de Lyon (Paris)")
    assert station["id"] == DEP and hints == []


# ------------------------------------------------------- separate budgets

@pytest.fixture
def two_providers(monkeypatch):
    clock = {"t": 1_000_000.0}
    calls = {"ns": [], "sncf": []}
    monkeypatch.setattr(api_cache.time, "time", lambda: clock["t"])
    monkeypatch.setattr(state.time, "time", lambda: clock["t"])
    monkeypatch.setattr(api_cache.time, "sleep", lambda s: clock.update(t=clock["t"] + s))
    for name, a in (("ns", ns_client.api), ("sncf", sncf_client.api)):
        clear_api(a)
        monkeypatch.setattr(a, "_http_get", lambda p, n=name: calls[n].append(p) or {})
    yield clock, calls
    for a in (ns_client.api, sncf_client.api):
        clear_api(a)
    accounts.save({})
    state._devices.clear()


def test_budgets_are_independent(two_providers):
    clock, calls = two_providers
    ns, sn = ns_client.api, sncf_client.api
    for i in range(3):
        ns.want(f"/ns{i}")
        sn.want(f"/sncf{i}")
    # Use up SNCF entirely: NS is unaffected.
    sn._blocked_until = clock["t"] + 3600
    assert sn.fetch_next() is None
    assert ns.fetch_next() == "/ns0"
    clock["t"] += ns.call_interval
    assert ns.fetch_next() == "/ns1"
    # And an NS call doesn't delay SNCF.
    sn._blocked_until = 0
    assert sn.fetch_next() == "/sncf0"
    assert len(calls["ns"]) == 2 and len(calls["sncf"]) == 1


def test_sncf_daily_quota(two_providers, monkeypatch):
    clock, calls = two_providers
    sn = sncf_client.api
    monkeypatch.setattr(sn, "daily_limit", 3)
    for i in range(10):
        sn.want(f"/p{i}")
    for _ in range(10):
        sn.fetch_next()
        clock["t"] += 60
    assert len(calls["sncf"]) == 3
    assert not sn.fetch_urgent("/urgent")
    clock["t"] += 86400                        # next day: budget is back
    sn.want("/tomorrow")
    assert sn.fetch_next() == "/tomorrow"


def test_urgent_calls_leave_reserve_for_background(two_providers, monkeypatch):
    clock, calls = two_providers
    sn = sncf_client.api
    monkeypatch.setattr(sn, "daily_limit", 10)   # reserve: 2 calls
    for i in range(20):
        clock["t"] += 61
        sn.fetch_urgent(f"/search{i}")
    assert len(calls["sncf"]) == 8
    sn.want("/journeys")
    assert sn.fetch_next() == "/journeys"        # background still gets its share


def test_no_calls_for_unused_provider(two_providers):
    _, calls = two_providers
    accounts.save({"a": {"token": "t", "password_hash": "x", "provider": "sncf"}})
    scheduler.run_once()
    assert calls["ns"] == []                   # no NS device: not even the station list


def test_sncf_device_route_roundtrip(two_providers, monkeypatch):
    accounts.save({"fr": {"token": "t", "password_hash": "x", "provider": "sncf"}})
    state._devices.clear()
    try:
        (storage.DATA_DIR / "devices" / "fr.json").unlink()
    except OSError:
        pass
    assert "Choisissez" in state.get_status("fr")["message"]
    monkeypatch.setattr(sncf_client, "get_trips", lambda s, e, w=None: [])
    route = {"departure": "Paris Gare de Lyon", "arrival": "Lyon Part-Dieu",
             "departure_id": DEP, "arrival_id": ARR}
    assert state.set_route("fr", route) == route
    state._devices.clear()
    assert state.get_route("fr") == route
    assert state.get_provider("fr").name == "sncf"
