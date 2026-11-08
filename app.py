
from pathlib import Path
from threading import Lock
import json

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

app = FastAPI(title="Train Status API")

DATA_DIR = Path("/data")
ROUTE_FILE = DATA_DIR / "route.json"
lock = Lock()

DEFAULT_ROUTE = {
    "departure": "Utrecht Centraal",
    "arrival": "Amsterdam Centraal",
}

DEFAULT_STATUS = {
    "status": "ok",
    "message": "Running normally",
}

def load_route():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if not ROUTE_FILE.exists():
        save_route(DEFAULT_ROUTE)
        return DEFAULT_ROUTE.copy()

    try:
        data = json.loads(ROUTE_FILE.read_text(encoding="utf-8"))
        departure = data.get("departure")
        arrival = data.get("arrival")

        if (
            isinstance(departure, str) and departure.strip()
            and isinstance(arrival, str) and arrival.strip()
        ):
            return {
                "departure": departure.strip(),
                "arrival": arrival.strip(),
            }
    except (OSError, json.JSONDecodeError):
        pass

    save_route(DEFAULT_ROUTE)
    return DEFAULT_ROUTE.copy()

def save_route(route):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp_file = ROUTE_FILE.with_suffix(".tmp")
    temp_file.write_text(
        json.dumps(route, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    temp_file.replace(ROUTE_FILE)

route = load_route()
status = DEFAULT_STATUS.copy()


class RouteUpdate(BaseModel):
    departure: str = Field(min_length=1, max_length=100)
    arrival: str = Field(min_length=1, max_length=100)


class StatusUpdate(BaseModel):
    status: str
    message: str | None = Field(default=None, max_length=200)


@app.get("/", response_class=HTMLResponse)
def home():
    with lock:
        departure = route["departure"]
        arrival = route["arrival"]
        current_status = status.copy()

    # Escape values before inserting them into HTML.
    from html import escape
    departure_html = escape(departure, quote=True)
    arrival_html = escape(arrival, quote=True)
    status_html = escape(current_status["status"])
    message_html = escape(current_status["message"])

    return f"""
    <!doctype html>
    <html lang="en">
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Train indicator</title>
      <style>
        body {{ font: 16px system-ui; max-width: 520px;
                margin: 40px auto; padding: 0 18px; }}
        input, button {{ box-sizing: border-box; width: 100%;
                         padding: 12px; margin: 6px 0 16px; }}
        button {{ cursor: pointer; }}
        .panel {{ background: #f1f3f5; padding: 16px;
                  border-radius: 12px; }}
      </style>
    </head>
    <body>
      <h1>🚆 Train indicator</h1>
      <p>Choose the journey to monitor.</p>
      <form id="routeForm">
        <label for="departure">Departure station</label>
        <input id="departure" maxlength="100" required
               value="{departure_html}">
        <label for="arrival">Arrival station</label>
        <input id="arrival" maxlength="100" required
               value="{arrival_html}">
        <button type="submit">Save route</button>
      </form>
      <p id="result" role="status"></p>
      <div class="panel">
        <h2>Current test status</h2>
        <p>{status_html}</p>
        <p>{message_html}</p>
        <small>Live NS checking is not enabled yet.</small>
      </div>
      <script>
        document.getElementById("routeForm").addEventListener(
          "submit", async (event) => {{
            event.preventDefault();
            const result = document.getElementById("result");
            result.textContent = "Saving...";
            try {{
              const response = await fetch("/route", {{
                method: "POST",
                headers: {{ "Content-Type": "application/json" }},
                body: JSON.stringify({{
                  departure: document.getElementById("departure").value,
                  arrival: document.getElementById("arrival").value
                }})
              }});
              const data = await response.json();
              if (!response.ok) {{
                throw new Error(data.detail || "Save failed");
              }}
              result.textContent =
                "Saved: " + data.departure + " → " + data.arrival;
            }} catch (error) {{
              result.textContent = "Error: " + error.message;
            }}
          }}
        );
      </script>
    </body>
    </html>
    """


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/route")
def get_route():
    with lock:
        return route.copy()


@app.post("/route")
def set_route(update: RouteUpdate):
    departure = update.departure.strip()
    arrival = update.arrival.strip()

    if not departure or not arrival:
        raise HTTPException(
            status_code=422,
            detail="Both stations are required",
        )

    new_route = {
        "departure": departure,
        "arrival": arrival,
    }

    with lock:
        try:
            save_route(new_route)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail="Could not save route to disk",
            ) from exc

        route.update(new_route)

    return new_route.copy()


@app.get("/status")
def get_status():
    with lock:
        return {
            **status,
            "route": route.copy(),
        }


@app.post("/status")
def set_status(update: StatusUpdate):
    allowed = {"ok", "delayed", "cancelled"}

    if update.status not in allowed:
        raise HTTPException(
            status_code=422,
            detail="Status must be ok, delayed, or cancelled",
        )

    defaults = {
        "ok": "Running normally",
        "delayed": "Train delayed",
        "cancelled": "Train cancelled",
    }

    with lock:
        status["status"] = update.status
        status["message"] = update.message or defaults[update.status]
        return status.copy()
