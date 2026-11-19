// If the session has expired, any call comes back 401: go to the login page.
const realFetch = window.fetch;
window.fetch = async (...args) => {
  const response = await realFetch(...args);
  if (response.status === 401) window.location.href = "/login";
  return response;
};

const $ = (id) => document.getElementById(id);

const COLORS = { green: "#2e9d4f", yellow: "#e6b800", red: "#d33a2c", white: "#f4f4f4" };

// Page texts. English is in the HTML itself; French devices (SNCF) get these.
// {api} is replaced by the device's train company (NS, SNCF).
const TEXTS = {
  en: {
    color: { green: "Green", yellow: "Yellow", red: "Red", white: "White" },
    usual_cancelled: "My train is cancelled, but a later one works",
    replacement_transport: "Replacement bus or other trip problem",
    reduced_service: "{api} reports fewer trains on my route",
    no_trains: "{api} reports no trains on my route",
    all_cancelled: "All upcoming trips are cancelled",
    no_data: "{api} can't be reached or has no answer",
    id: "id", owner: "owner",
    following: "Following the {time} train",
    last_checked: "Last checked: ",
    saving: "Saving...",
    save_failed: "Save failed",
    settings_saved: "Settings saved!",
    defaults_back: "Back to default settings.",
    pick_station: "Error: pick \"{name}\" from the station suggestions",
    saving_route: "Saving route...",
    route_saved: "Route saved! Checking {api}, the status below updates in a few seconds.",
    error: "Error: ",
    pw_mismatch: "Error: the new passwords don't match",
    pw_failed: "Could not change password",
    pw_changed: "Password changed. Other browsers logged in to this device are logged out.",
    searching: "Searching stations...",
    no_route: "No route chosen yet"
  },
  fr: {
    color: { green: "Vert", yellow: "Jaune", red: "Rouge", white: "Blanc" },
    usual_cancelled: "Mon train est supprimé, mais un suivant convient",
    replacement_transport: "Car de substitution ou autre problème",
    reduced_service: "{api} annonce moins de trains sur mon trajet",
    no_trains: "{api} annonce aucun train sur mon trajet",
    all_cancelled: "Tous les prochains trains sont supprimés",
    no_data: "{api} injoignable ou sans réponse",
    id: "identifiant", owner: "propriétaire",
    following: "On suit le train de {time}",
    last_checked: "Dernière vérification : ",
    saving: "Enregistrement...",
    save_failed: "Échec de l'enregistrement",
    settings_saved: "Réglages enregistrés !",
    defaults_back: "Réglages par défaut rétablis.",
    pick_station: "Erreur : choisissez « {name} » dans la liste des gares",
    saving_route: "Enregistrement du trajet...",
    route_saved: "Trajet enregistré ! Vérification auprès de {api}, l'état ci-dessous se met à jour dans quelques secondes.",
    error: "Erreur : ",
    pw_mismatch: "Erreur : les nouveaux mots de passe ne correspondent pas",
    pw_failed: "Impossible de changer le mot de passe",
    pw_changed: "Mot de passe changé. Les autres navigateurs connectés à cet appareil sont déconnectés.",
    searching: "Recherche des gares...",
    no_route: "Pas encore de trajet choisi",
    // static page texts (data-t attributes)
    page: {
      logout: "Se déconnecter",
      configuring: "Vous configurez",
      choose_route: "Choisissez votre trajet",
      departure: "Gare de départ",
      arrival: "Gare d'arrivée",
      type_station: "Tapez le nom d'une gare…",
      save_route: "Enregistrer le trajet",
      notice: "⏱ La lumière du Quintrein peut mettre une minute ou deux à changer. Astuce : indiquez vos horaires sous « Trains précis » pour qu'ils soient vérifiés chaque minute avant votre départ.",
      current_route: "Trajet actuel",
      train_status: "État du train",
      light_settings: "Réglages de la lumière",
      settings_hint: "Les retards sont mesurés par rapport au train que vous prenez d'habitude (ou au train précis choisi ci-dessous). La pire couleur l'emporte.",
      delay: "Retard",
      yellow_from: "🟡 Jaune à partir de (minutes de retard)",
      red_from: "🔴 Rouge à partir de (minutes de retard)",
      other_situations: "Autres situations",
      pick_green: "Choisissez vert pour ignorer une situation.",
      specific_trains: "Trains précis (facultatif)",
      specific_hint: "Heures de départ de votre gare, par ex. 07:48, 08:18. Le prochain qui n'est pas encore parti décide de la couleur. Laissez vide pour suivre le prochain train.",
      save_settings: "Enregistrer les réglages",
      reset: "Réglages par défaut",
      change_password: "Changer le mot de passe",
      pw_current: "Mot de passe actuel",
      pw_new: "Nouveau mot de passe (8 caractères minimum)",
      pw_repeat: "Répétez le nouveau mot de passe"
    }
  }
};

const CONDITION_KEYS = [
  "usual_cancelled", "replacement_transport", "reduced_service",
  "no_trains", "all_cancelled", "no_data"
];

let lang = "en";
let apiLabel = "NS";
let provider = "ns";
let stationNames = new Set();   // NS: every valid station name
const stationIds = new Map();   // SNCF: label (lowercase) -> stop_area id

function T(key, values = {}) {
  const text = (TEXTS[lang] && TEXTS[lang][key]) || TEXTS.en[key] || key;
  return text.replace(/\{(\w+)\}/g, (_, k) => (k === "api" ? apiLabel : values[k] ?? ""));
}

function buildConditions() {
  const colors = TEXTS[lang].color;
  const current = {};
  for (const key of CONDITION_KEYS) {
    const select = $("cfg_" + key);
    if (select) current[key] = select.value;
  }
  $("conditions").innerHTML = CONDITION_KEYS.map(
    (key) =>
      `<label for="cfg_${key}">${T(key)}</label>` +
      `<select id="cfg_${key}">` +
      `<option value="green">🟢 ${colors.green}</option>` +
      `<option value="yellow">🟡 ${colors.yellow}</option>` +
      `<option value="red">🔴 ${colors.red}</option></select>`
  ).join("");
  for (const key of CONDITION_KEYS) {
    if (current[key]) $("cfg_" + key).value = current[key];
  }
}
buildConditions();

function translatePage() {
  document.documentElement.lang = lang;
  const page = TEXTS[lang].page;
  if (!page) return;
  document.querySelectorAll("[data-t]").forEach((el) => {
    const text = page[el.dataset.t];
    if (text) el.textContent = text;
  });
  document.querySelectorAll("[data-t-placeholder]").forEach((el) => {
    const text = page[el.dataset.tPlaceholder];
    if (text) el.placeholder = text;
  });
}

function errorText(data, fallback) {
  if (Array.isArray(data.detail)) {
    return data.detail.map((d) => d.msg).join(", ");
  }
  return data.detail || fallback;
}

async function loadMe() {
  try {
    const response = await fetch("/me");
    if (!response.ok) return;
    const me = await response.json();
    lang = TEXTS[me.lang] ? me.lang : "en";
    apiLabel = me.provider_label || "NS";
    provider = me.provider || "ns";
    translatePage();
    buildConditions();
    $("pwUser").value = me.id;
    $("deviceName").textContent = me.name;
    const details = [];
    if (me.name !== me.id) details.push(T("id") + ": " + me.id);
    if (me.owner) details.push(T("owner") + ": " + me.owner);
    details.push(apiLabel);
    $("deviceDetails").textContent = details.join(" · ");
    $("deviceBanner").hidden = false;
    document.title = me.name + " – Quintrein Project";
  } catch (error) {
    /* not important */
  }
}

function fillStations(names) {
  $("stations").replaceChildren(
    ...names.map((name) => {
      const option = document.createElement("option");
      option.value = name;
      return option;
    })
  );
}

async function loadStations() {
  if (provider !== "ns") return; // SNCF: searched while typing
  try {
    const response = await fetch("/stations");
    if (response.status === 503) {
      setTimeout(loadStations, 30000); // NS list not loaded yet
      return;
    }
    if (!response.ok) return;
    const names = await response.json();
    stationNames = new Set(names.map((n) => n.toLowerCase()));
    fillStations(names);
  } catch (error) {
    /* no suggestions; the server still validates what is typed */
  }
}

// SNCF has no full station list: search while the person types. Each
// search costs an API call, so wait for a pause in typing and remember
// every answer.
const searchCache = new Map();
let searchTimer = null;

function searchStations(input) {
  if (provider !== "sncf") return;
  clearTimeout(searchTimer);
  const query = input.value.trim().toLowerCase();
  if (query.length < 3 || stationIds.has(query)) return;
  searchTimer = setTimeout(async () => {
    let results = searchCache.get(query);
    if (!results) {
      try {
        const response = await fetch("/stations/search?q=" + encodeURIComponent(query));
        if (!response.ok) {
          const data = await response.json().catch(() => ({}));
          $("result").textContent = T("error") + errorText(data, response.status);
          return;
        }
        results = await response.json();
        searchCache.set(query, results);
      } catch (error) {
        return;
      }
    }
    for (const r of results) stationIds.set(r.label.toLowerCase(), r.id);
    fillStations(results.map((r) => r.label));
    if ($("result").textContent.startsWith(T("error"))) $("result").textContent = "";
  }, 500);
}

for (const id of ["departure", "arrival"]) {
  $(id).addEventListener("input", () => searchStations($(id)));
}

function showRoute(route) {
  $("departure").value = route.departure || "";
  $("arrival").value = route.arrival || "";
  $("savedRoute").textContent = route.departure && route.arrival
    ? route.departure + " → " + route.arrival : T("no_route");
  if (route.departure_id) stationIds.set(route.departure.toLowerCase(), route.departure_id);
  if (route.arrival_id) stationIds.set(route.arrival.toLowerCase(), route.arrival_id);
}

async function loadRoute() {
  try {
    const response = await fetch("/route");
    if (!response.ok) return;
    showRoute(await response.json());
  } catch (error) {
    /* leave as is */
  }
}

async function loadStatus() {
  try {
    const response = await fetch("/status");
    if (!response.ok) return null;
    const data = await response.json();

    $("dot").style.background = COLORS[data.color] || "#999";
    $("status").textContent = TEXTS[lang].color[data.color] || data.color;
    $("message").textContent = data.error
      ? data.message + " (" + data.error + ")"
      : data.message;
    $("watching").textContent = data.watching
      ? T("following", { time: data.watching }) : "";
    $("checked").textContent =
      data.last_checked ? T("last_checked") + data.last_checked : "";
    return data;
  } catch (error) {
    return null; /* keep showing what we have */
  }
}

// After a change, check every few seconds until the server has a real
// answer (it fetches within ~10 s), then go back to the slow pace.
let fastPoll = null;
function pollUntilFresh() {
  clearInterval(fastPoll);
  const started = Date.now();
  fastPoll = setInterval(async () => {
    const data = await loadStatus();
    const ready = data && data.color !== "white";
    if (ready || Date.now() - started > 90000) clearInterval(fastPoll);
  }, 3000);
}

function fillConfig(c) {
  $("cfg_delay_yellow_min").value = c.delay_yellow_min;
  $("cfg_delay_red_min").value = c.delay_red_min;
  for (const key of CONDITION_KEYS) {
    $("cfg_" + key).value = c[key];
  }
  $("cfg_watch_times").value = c.watch_times.join(", ");
}

async function loadConfig() {
  try {
    const response = await fetch("/config");
    if (response.ok) fillConfig(await response.json());
  } catch (error) {
    /* leave the form empty */
  }
}

async function sendConfig(url, body, okText) {
  const out = $("configResult");
  out.textContent = T("saving");
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await response.json();
    if (!response.ok) throw new Error(errorText(data, T("save_failed")));
    fillConfig(data);
    out.textContent = okText;
    loadStatus();
    pollUntilFresh();
  } catch (error) {
    out.textContent = T("error") + error.message;
  }
}

$("configForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const body = {
    delay_yellow_min: Number($("cfg_delay_yellow_min").value),
    delay_red_min: Number($("cfg_delay_red_min").value),
    watch_times: $("cfg_watch_times").value.split(/[,\s]+/).filter(Boolean)
  };
  for (const key of CONDITION_KEYS) {
    body[key] = $("cfg_" + key).value;
  }
  sendConfig("/config", body, T("settings_saved"));
});

$("resetConfig").addEventListener("click", () => {
  sendConfig("/config/reset", null, T("defaults_back"));
});

$("routeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = $("result");

  const departure = $("departure").value.trim();
  const arrival = $("arrival").value.trim();
  const body = { departure, arrival };

  for (const value of [departure, arrival]) {
    const known = provider === "sncf"
      ? stationIds.has(value.toLowerCase())
      : !stationNames.size || stationNames.has(value.toLowerCase());
    if (!known) {
      result.textContent = T("pick_station", { name: value });
      return;
    }
  }
  if (provider === "sncf") {
    body.departure_id = stationIds.get(departure.toLowerCase());
    body.arrival_id = stationIds.get(arrival.toLowerCase());
  }

  result.textContent = T("saving_route");
  try {
    const response = await fetch("/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    const data = await response.json();
    if (!response.ok) throw new Error(errorText(data, T("save_failed")));

    showRoute(data);
    result.textContent = T("route_saved");
    loadStatus();
    pollUntilFresh();
  } catch (error) {
    result.textContent = T("error") + error.message;
  }
});

(async () => {
  await loadMe(); // language and provider first
  loadStations();
  loadRoute();
  loadConfig();
  loadStatus();
  setInterval(loadStatus, 30000);
})();

$("logout").addEventListener("click", async () => {
  await fetch("/logout", { method: "POST" });
  window.location.href = "/login";
});

$("passwordForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const out = $("passwordResult");
  if ($("pwNew").value !== $("pwRepeat").value) {
    out.textContent = T("pw_mismatch");
    return;
  }
  out.textContent = T("saving");
  try {
    const response = await fetch("/password", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current: $("pwCurrent").value, new: $("pwNew").value })
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(errorText(data, T("pw_failed")));
    $("passwordForm").reset();
    out.textContent = T("pw_changed");
  } catch (error) {
    out.textContent = T("error") + error.message;
  }
});
