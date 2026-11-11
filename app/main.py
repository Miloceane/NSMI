import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import auth, ns_client, state
from .config import COLOR_OF_STATUS, CONFIG_KEYS, DEFAULT_CONFIG, clean_config

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Train Status API")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Reachable without logging in: the login page itself and what it needs.
PUBLIC_PATHS = {"/login", "/health", "/static/style.css", "/static/login.js"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS or auth.request_is_authorized(request):
        return await call_next(request)

    # Browsers opening a page get sent to the login page; everything else
    # (fetch calls, the ESP32 with a wrong token) gets a plain 401.
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)

    return JSONResponse({"detail": "Not logged in"}, status_code=401)


class LoginBody(BaseModel):
    password: str = Field(max_length=200)


class RouteUpdate(BaseModel):
    departure: str = Field(min_length=1, max_length=100)
    arrival: str = Field(min_length=1, max_length=100)


class StatusUpdate(BaseModel):
    status: str
    message: str | None = Field(default=None, max_length=200)


class ConfigUpdate(BaseModel):
    delay_yellow_min: int
    delay_red_min: int
    usual_cancelled: str
    replacement_transport: str
    reduced_service: str
    no_trains: str
    all_cancelled: str
    no_data: str
    watch_times: list[str] = []


@app.get("/")
def home():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/login")
def login_page(request: Request):
    if auth.has_valid_session(request):
        return RedirectResponse("/", status_code=303)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login")
def login(body: LoginBody, request: Request):
    client = request.client.host if request.client else "unknown"

    if auth.is_locked(client):
        raise HTTPException(
            status_code=429,
            detail="Too many wrong passwords. Try again in a few minutes.",
        )

    if not auth.password_ok(body.password):
        auth.record_failure(client)
        time.sleep(1)  # slows guessing down
        raise HTTPException(status_code=401, detail="Wrong password")

    auth.clear_failures(client)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.make_session(),
        max_age=auth.SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/stations")
def stations():
    """Station names for the dropdown."""
    try:
        return ns_client.station_options()
    except Exception as exc:
        print("Station list error:", str(exc))
        raise HTTPException(status_code=503, detail="Could not load stations from NS")


def resolve_station(text):
    try:
        station = ns_client.find_station(text)
    except Exception as exc:
        print("Station lookup error:", str(exc))
        raise HTTPException(
            status_code=503,
            detail="Could not load the station list from NS, try again in a minute",
        )

    if not station:
        hints = ns_client.suggest(text)
        extra = f" Did you mean: {', '.join(hints)}?" if hints else ""
        raise HTTPException(status_code=422, detail=f"Station not found: {text}.{extra}")

    return station


@app.get("/route")
def get_route():
    return state.get_route()


@app.post("/route")
def set_route(update: RouteUpdate):
    start = resolve_station(update.departure.strip())
    end = resolve_station(update.arrival.strip())

    if start["code"] == end["code"]:
        raise HTTPException(
            status_code=422,
            detail="Departure and arrival must be different stations",
        )

    # Store the official names, whatever spelling was typed.
    new_route = {
        "departure": start["namen"]["lang"],
        "arrival": end["namen"]["lang"],
    }
    try:
        return state.set_route(new_route)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not save route to disk") from exc


@app.get("/config")
def get_config():
    return state.get_config()


@app.post("/config")
def set_config(update: ConfigUpdate):
    try:
        new_config = clean_config({key: getattr(update, key) for key in CONFIG_KEYS})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return save_config(new_config)


@app.post("/config/reset")
def reset_config():
    return save_config(clean_config(DEFAULT_CONFIG))


def save_config(new_config):
    try:
        return state.set_config(new_config)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not save settings to disk") from exc


@app.get("/status")
def get_status():
    return state.get_status()


@app.get("/color", response_class=PlainTextResponse)
def get_color():
    """Just the colour as plain text: green, yellow or red."""
    return state.get_color()


@app.post("/status")
def set_status(update: StatusUpdate):
    if update.status not in COLOR_OF_STATUS:
        raise HTTPException(
            status_code=422,
            detail="Status must be ok, delayed, or cancelled",
        )
    return state.set_manual_status(update.status, update.message)