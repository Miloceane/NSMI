// If the session has expired, any call comes back 401: go to the login page.
const realFetch = window.fetch;
window.fetch = async (...args) => {
  const response = await realFetch(...args);
  if (response.status === 401) window.location.href = "/login";
  return response;
};

const $ = (id) => document.getElementById(id);

const COLORS = { green: "#2e9d4f", yellow: "#e6b800", red: "#d33a2c" };
const CONDITIONS = [
  ["usual_cancelled", "My train is cancelled, but a later one works"],
  ["replacement_transport", "Replacement bus or other trip problem"],
  ["reduced_service", "NS reports fewer trains on my route"],
  ["no_trains", "NS reports no trains on my route"],
  ["all_cancelled", "All upcoming trips are cancelled"],
  ["no_data", "NS can't be reached or has no answer"]
];

let stationNames = new Set();

$("conditions").innerHTML = CONDITIONS.map(
  ([key, text]) =>
    `<label for="cfg_${key}">${text}</label>` +
    `<select id="cfg_${key}">` +
    `<option value="green">🟢 Green</option>` +
    `<option value="yellow">🟡 Yellow</option>` +
    `<option value="red">🔴 Red</option></select>`
).join("");

function errorText(data, fallback) {
  if (Array.isArray(data.detail)) {
    return data.detail.map((d) => d.msg).join(", ");
  }
  return data.detail || fallback;
}

async function loadStations() {
  try {
    const response = await fetch("/stations");
    if (!response.ok) return;
    const names = await response.json();
    stationNames = new Set(names.map((n) => n.toLowerCase()));
    $("stations").replaceChildren(
      ...names.map((name) => {
        const option = document.createElement("option");
        option.value = name;
        return option;
      })
    );
  } catch (error) {
    /* no suggestions; the server still validates what is typed */
  }
}

async function loadRoute() {
  try {
    const response = await fetch("/route");
    if (!response.ok) return;
    const route = await response.json();
    $("departure").value = route.departure;
    $("arrival").value = route.arrival;
    $("savedRoute").textContent = route.departure + " → " + route.arrival;
  } catch (error) {
    /* leave as is */
  }
}

async function loadStatus() {
  try {
    const response = await fetch("/status");
    if (!response.ok) return;
    const data = await response.json();

    $("dot").style.background = COLORS[data.color] || "#999";
    $("status").textContent =
      data.color.charAt(0).toUpperCase() + data.color.slice(1);
    $("message").textContent = data.error
      ? data.message + " (" + data.error + ")"
      : data.message;
    $("watching").textContent = data.watching
      ? "Following the " + data.watching + " train" : "";
    $("checked").textContent =
      data.last_checked ? "Last checked: " + data.last_checked : "";
  } catch (error) {
    /* keep showing what we have */
  }
}

function fillConfig(c) {
  $("cfg_delay_yellow_min").value = c.delay_yellow_min;
  $("cfg_delay_red_min").value = c.delay_red_min;
  for (const [key] of CONDITIONS) {
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
  out.textContent = "Saving...";
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined
    });
    const data = await response.json();
    if (!response.ok) throw new Error(errorText(data, "Save failed"));
    fillConfig(data);
    out.textContent = okText;
    loadStatus();
  } catch (error) {
    out.textContent = "Error: " + error.message;
  }
}

$("configForm").addEventListener("submit", (event) => {
  event.preventDefault();
  const body = {
    delay_yellow_min: Number($("cfg_delay_yellow_min").value),
    delay_red_min: Number($("cfg_delay_red_min").value),
    watch_times: $("cfg_watch_times").value.split(/[,\s]+/).filter(Boolean)
  };
  for (const [key] of CONDITIONS) {
    body[key] = $("cfg_" + key).value;
  }
  sendConfig("/config", body, "Settings saved!");
});

$("resetConfig").addEventListener("click", () => {
  sendConfig("/config/reset", null, "Back to default settings.");
});

$("routeForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const result = $("result");

  const departure = $("departure").value.trim();
  const arrival = $("arrival").value.trim();

  for (const value of [departure, arrival]) {
    if (stationNames.size && !stationNames.has(value.toLowerCase())) {
      result.textContent = "Error: pick \"" + value + "\" from the station suggestions";
      return;
    }
  }

  result.textContent = "Saving route...";
  try {
    const response = await fetch("/route", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ departure, arrival })
    });
    const data = await response.json();
    if (!response.ok) throw new Error(errorText(data, "Save failed"));

    $("departure").value = data.departure;
    $("arrival").value = data.arrival;
    $("savedRoute").textContent = data.departure + " → " + data.arrival;
    result.textContent = "Route saved successfully!";
    loadStatus();
  } catch (error) {
    result.textContent = "Error: " + error.message;
  }
});

loadStations();
loadRoute();
loadConfig();
loadStatus();
setInterval(loadStatus, 60000);

$("logout").addEventListener("click", async () => {
  await fetch("/logout", { method: "POST" });
  window.location.href = "/login";
});