const form = document.getElementById("loginForm");
const out = document.getElementById("loginResult");

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  out.textContent = "Logging in...";

  try {
    const response = await fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: document.getElementById("password").value })
    });

    if (response.ok) {
      window.location.href = "/";
      return;
    }

    const data = await response.json().catch(() => ({}));
    out.textContent = "Error: " + (data.detail || "Login failed");
  } catch (error) {
    out.textContent = "Error: could not reach the server";
  }
});