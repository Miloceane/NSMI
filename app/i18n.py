"""Texts the server shows to people, in English and French.

The language follows the device's provider (NS devices: English, SNCF
devices: French), see app/providers.py.
"""

TEXTS = {
    "en": {
        "starting": "Starting up, no data yet",
        "choose_route": "Choose your route on the web page",
        "waiting": "Waiting for {api} data (can take a few minutes)",
        "unreachable_keep": "Could not reach {api}; showing last known status",
        "no_recent": "No recent {api} data",
        "unreachable_none": "Could not reach {api} and there is no recent data",
        "checking_route": "Checking {api} for your new route...",
        "dep_not_found": "Departure station not found: {name}",
        "arr_not_found": "Arrival station not found: {name}",
        "no_trips": "{api} returned no trips for this route",
        "no_watched": "No train found at {times}; following the next train. ",
        "all_cancelled": "All of the next {n} trips are cancelled",
        "ref_watched": "the {time} train",
        "ref_usual": "usual",
        "summary": "Best option leaves {dep}, arrives {arr} ({late:+d} min vs {ref})",
        "no_disruptions": " (could not check {api} disruptions)",
        "closest": "Using the {label} train (closest to {target})",
        "station_not_found": "Station not found: {text}.",
        "did_you_mean": " Did you mean: {hints}?",
        "same_station": "Departure and arrival must be different stations",
        "loading": "The {what} is still being loaded from {api}, try again in a minute",
        "station_list": "station list",
        "station_error": "Could not load the station list from {api}, try again in a minute",
        "search_busy": "Too many station searches right now, try again in a minute",
        "disruption": "{api} disruption",
    },
    "fr": {
        "starting": "Démarrage, pas encore de données",
        "choose_route": "Choisissez votre trajet sur la page web",
        "waiting": "En attente des données {api} (peut prendre quelques minutes)",
        "unreachable_keep": "{api} injoignable ; dernier état connu affiché",
        "no_recent": "Pas de données {api} récentes",
        "unreachable_none": "{api} injoignable et pas de données récentes",
        "checking_route": "Vérification de votre nouveau trajet auprès de {api}...",
        "dep_not_found": "Gare de départ introuvable : {name}",
        "arr_not_found": "Gare d'arrivée introuvable : {name}",
        "no_trips": "{api} n'a trouvé aucun train pour ce trajet",
        "no_watched": "Aucun train trouvé à {times} ; on suit le prochain train. ",
        "all_cancelled": "Les {n} prochains trains sont tous supprimés",
        "ref_watched": "le train de {time}",
        "ref_usual": "l'horaire prévu",
        "summary": "Meilleure option : départ {dep}, arrivée {arr} ({late:+d} min par rapport à {ref})",
        "no_disruptions": " (impossible de vérifier les perturbations {api})",
        "closest": "Train de {label} utilisé (le plus proche de {target})",
        "station_not_found": "Gare introuvable : {text}.",
        "did_you_mean": " Vouliez-vous dire : {hints} ?",
        "same_station": "Les gares de départ et d'arrivée doivent être différentes",
        "loading": "La {what} est en cours de chargement depuis {api}, réessayez dans une minute",
        "station_list": "liste des gares",
        "station_error": "Impossible de charger les gares depuis {api}, réessayez dans une minute",
        "search_busy": "Trop de recherches de gares en ce moment, réessayez dans une minute",
        "disruption": "Perturbation {api}",
    },
}


def t(lang, key, **values):
    text = TEXTS.get(lang, TEXTS["en"]).get(key) or TEXTS["en"][key]
    return text.format(**values)
