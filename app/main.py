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

from contextlib import asynccontextmanager

from . import accounts, auth, ns_client, scheduler, state
from .config import COLOR_OF_STATUS, CONFIG_KEYS, DEFAULT_CONFIG, clean_config

STATIC_DIR = Path(__file__).parent / "static"

@asynccontextmanager
async def lifespan(app):
    if not accounts.load():
        print("No device accounts yet. Create one with: python -m app.admin add <id>")
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Train Status API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Reachable without logging in: the login page itself and what it needs.
PUBLIC_PATHS = {"/login", "/health", "/static/style.css", "/static/login.js"}


@app.middleware("http")
async def no_stale_pages(request: Request, call_next):
    # Pages and scripts change together on every deploy; never let a
    # browser or Cloudflare keep an old copy of one of them.
    response = await call_next(request)
    path = request.url.path
    if path in ("/", "/login") or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def require_login(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)

    device_id = auth.device_for(request)
    if device_id:
        request.state.device = device_id
        return await call_next(request)

    # Browsers opening a page get sent to the login page; everything else
    # (fetch calls, the ESP32 with a wrong token) gets a plain 401.
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=303)

    return JSONResponse({"detail": "Not logged in"}, status_code=401)


class LoginBody(BaseModel):
    device: str = Field(max_length=200)
    password: str = Field(max_length=200)


class PasswordChange(BaseModel):
    current: str = Field(max_length=200)
    new: str = Field(max_length=200)


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
    if auth.session_of(request):
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

    device_id = auth.login(body.device, body.password)
    if not device_id:
        auth.record_failure(client)
        time.sleep(1)  # slows guessing down
        raise HTTPException(status_code=401, detail="Wrong device name or password")

    auth.clear_failures(client)
    return session_response(device_id, request)


def session_response(device_id, request):
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.make_session(device_id),
        max_age=auth.SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


@app.post("/password")
def change_password(body: PasswordChange, request: Request):
    client = request.client.host if request.client else "unknown"
    if auth.is_locked(client):
        raise HTTPException(status_code=429, detail="Too many wrong passwords. Try again later.")
    try:
        auth.change_password(request.state.device, body.current, body.new)
    except ValueError as exc:
        if "wrong" in str(exc):
            auth.record_failure(client)
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Other sessions of this device are now logged out; keep this one.
    return session_response(request.state.device, request)


@app.post("/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(auth.COOKIE_NAME)
    return response



@app.get("/health")
def health():
    return {"ok": True}


def NS_WAIT(what):
    return HTTPException(
        status_code=503,
        detail=f"The {what} is still being loaded from NS, try again in a minute",
    )


@app.get("/me")
def me(request: Request):
    """Which device this session is for."""
    device_id = request.state.device
    account = accounts.get(device_id) or {}
    return {
        "id": device_id,
        "name": account.get("name", device_id),
        "owner": account.get("owner", ""),
    }


@app.get("/stations")
def stations():
    """Station names for the dropdown."""
    try:
        names = ns_client.station_options()
    except ns_client.NeedData:
        raise NS_WAIT("station list")
    except Exception as exc:
        print("Station list error:", str(exc))
        raise HTTPException(status_code=503, detail="Could not load stations from NS")
    if not names:
        raise NS_WAIT("station list")
    return names


def resolve_station(text):
    try:
        station = ns_client.find_station(text)
        hints = [] if station else ns_client.suggest(text)
    except ns_client.NeedData:
        raise NS_WAIT("station list")
    except Exception as exc:
        print("Station lookup error:", str(exc))
        raise HTTPException(
            status_code=503,
            detail="Could not load the station list from NS, try again in a minute",
        )

    if not station:
        extra = f" Did you mean: {', '.join(hints)}?" if hints else ""
        raise HTTPException(status_code=422, detail=f"Station not found: {text}.{extra}")

    return station


@app.get("/route")
def get_route(request: Request):
    return state.get_route(request.state.device)


@app.post("/route")
def set_route(update: RouteUpdate, request: Request):
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
        return state.set_route(request.state.device, new_route)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not save route to disk") from exc


@app.get("/config")
def get_config(request: Request):
    return state.get_config(request.state.device)


@app.post("/config")
def set_config(update: ConfigUpdate, request: Request):
    try:
        new_config = clean_config({key: getattr(update, key) for key in CONFIG_KEYS})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return save_config(request.state.device, new_config)


@app.post("/config/reset")
def reset_config(request: Request):
    return save_config(request.state.device, clean_config(DEFAULT_CONFIG))


def save_config(device_id, new_config):
    try:
        return state.set_config(device_id, new_config)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not save settings to disk") from exc


@app.get("/status")
def get_status(request: Request):
    return state.get_status(request.state.device)


@app.get("/color", response_class=PlainTextResponse)
def get_color(request: Request):
    """Just the colour as plain text."""
    return state.get_color(request.state.device)


@app.post("/status")
def set_status(update: StatusUpdate, request: Request):
    if update.status not in COLOR_OF_STATUS:
        raise HTTPException(
            status_code=422,
            detail="Status must be ok, delayed, or cancelled",
        )
    return state.set_manual_status(request.state.device, update.status, update.message)