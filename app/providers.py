"""The train APIs a device can use: NS (Netherlands) or SNCF (France).

Each provider has its own cache and call budget (app/api_cache.py), so
SNCF devices never take calls away from NS devices or the other way round.
Everything else (evaluation, colours, settings, the ESP32 endpoints) is
shared; the provider only supplies stations, trips and disruptions in the
NS trip format.
"""
from urllib.parse import urlencode

from . import ns_client, sncf_client
from .disruptions import check_disruptions as ns_check_disruptions
from .disruptions import route_stops


class Provider:
    name = label = lang = timezone = None
    api = None
    default_route = {}

    def find_station(self, name, station_id=None):
        raise NotImplementedError

    def fetch_trips(self, start, end, when=None):
        """Raw NS-style trip dicts, from `when` (default: now)."""
        raise NotImplementedError

    def check_disruptions(self, trip):
        """(set of kinds, [sentences]) for the stretch this trip covers."""
        raise NotImplementedError

    def background_paths(self):
        """Paths to keep fresh even when no device needs them yet."""
        return []


class Ns(Provider):
    name, label, lang, timezone = "ns", "NS", "en", "Europe/Amsterdam"
    api = ns_client.api
    default_route = {"departure": "Utrecht Centraal", "arrival": "Amsterdam Centraal"}

    def find_station(self, name, station_id=None):
        return ns_client.find_station(name)

    def fetch_trips(self, start, end, when=None):
        params = {"fromStation": start["code"], "toStation": end["code"]}
        if when is not None:
            params["dateTime"] = when.isoformat(timespec="seconds")
        data = ns_client.ns_get(f"/v3/trips?{urlencode(params)}")
        trips = data.get("trips") if isinstance(data, dict) else None
        if not isinstance(trips, list):
            raise RuntimeError("Unexpected response from NS trips API")
        return trips

    def check_disruptions(self, trip):
        return ns_check_disruptions(route_stops(trip))

    def background_paths(self):
        return [(ns_client.STATIONS_PATH, ns_client.STATIONS_MAX_AGE)]


class Sncf(Provider):
    name, label, lang, timezone = "sncf", "SNCF", "fr", "Europe/Paris"
    api = sncf_client.api
    default_route = {}   # no default: SNCF stations are picked with the search

    def find_station(self, name, station_id=None):
        return sncf_client.find_station(name, station_id)

    def fetch_trips(self, start, end, when=None):
        return sncf_client.get_trips(start, end, when)

    def check_disruptions(self, trip):
        return sncf_client.check_disruptions(trip)


PROVIDERS = {"ns": Ns(), "sncf": Sncf()}
DEFAULT = "ns"


def get(name):
    return PROVIDERS.get(name) or PROVIDERS[DEFAULT]


def of_account(account):
    return get((account or {}).get("provider"))
