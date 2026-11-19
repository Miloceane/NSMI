const form = document.getElementById("loginForm");
const out = document.getElementById("loginResult");

// Before login we don't know the device yet: French browsers get French.
const FR = (navigator.language || "").toLowerCase().startsWith("fr");
if (FR) {
  document.documentElement.lang = "fr";
  const set = (selector, text) => {
    const el = document.querySelector(selector);
    if (el) el.textContent = text;
  };
  set(".login h2", "Connexion");
  set('label[for="device"]', "Nom de l'appareil");
  set('label[for="password"]', "Mot de passe");
  set("#loginForm button", "Se connecter");
  const device = document.getElementById("device");
  if (device) device.placeholder = "par ex. mamie-maison";
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  out.textContent = FR ? "Connexion..." : "Logging in...";

  const device = document.getElementById("device");
  const password = document.getElementById("password");
  if (!device || !password) {
    // Page and script out of sync (an old cached copy of one of them).
    out.textContent = "Error: this page is outdated, please reload it (Ctrl+F5)";
    return;
  }

  let response;
  try {
    response = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ device: device.value.trim(), password: password.value })
    });
  } catch (error) {
    out.textContent = "Error: could not reach the server";
    return;
  }

  if (response.ok) {
    window.location.href = "/";
    return;
  }

  const data = await response.json().catch(() => ({}));
  const detail = Array.isArray(data.detail)
    ? "this page is outdated, please reload it (Ctrl+F5)"
    : data.detail || "Login failed (" + response.status + ")";
  out.textContent = "Error: " + detail;
});