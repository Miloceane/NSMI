"""Per-device login.

- The web page: log in with the device id (e.g. "quin-home") + password.
  The long device token also works instead of the id.
  The session cookie says which device it belongs to.
- The ESP32: sends only its device token to the read-only endpoints.

Session cookies are signed with a server secret (SESSION_SECRET env var, or
a random one kept in DATA_DIR/session.key) and with the device's password
hash, so changing a password logs that device's sessions out.
"""
import hashlib
import hmac
import os
import secrets
import time

from . import accounts, storage

COOKIE_NAME = "train_session"
SESSION_SECONDS = 30 * 24 * 3600   # stay logged in for 30 days
MAX_FAILURES = 5                   # wrong logins before a lockout...
LOCKOUT_SECONDS = 600              # ...of this many seconds (per client address)

# The ESP32 can read these without a login session, using its token.
DEVICE_PATHS = {"/color", "/status"}


def _load_secret():
    from_env = os.getenv("SESSION_SECRET")
    if from_env:
        return from_env
    path = storage.DATA_DIR / "session.key"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    secret = secrets.token_hex(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secret, encoding="utf-8")
    except OSError:
        print("WARNING: could not store session.key; sessions end on restart")
    return secret


SECRET = _load_secret()

# ---------------------------------------------------------------- sessions


def _sign(device_id, expires, password_hash):
    key = hashlib.sha256(f"train-session:{SECRET}:{password_hash}".encode()).digest()
    return hmac.new(key, f"{device_id}.{expires}".encode(), hashlib.sha256).hexdigest()


def make_session(device_id, now=None):
    """Cookie value: device id + expiry + signature. Nothing to store."""
    account = accounts.get(device_id)
    expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
    return f"{device_id}.{expires}.{_sign(device_id, expires, account['password_hash'])}"


def session_device(value, now=None):
    """The device id a cookie belongs to, or None if it isn't valid."""
    try:
        device_id, expires_text, signature = value.split(".")
        expires = int(expires_text)
    except (AttributeError, ValueError):
        return None

    if expires <= (now if now is not None else time.time()):
        return None

    account = accounts.get(device_id)
    if not account:
        return None

    expected = _sign(device_id, expires, account.get("password_hash", ""))
    if not hmac.compare_digest(signature.encode(), expected.encode()):
        return None
    return device_id


def login(name, password):
    """The device id if the name (device id, or its token) and password
    match, else None."""
    name = name.strip()
    account = accounts.get(name.lower())
    found = (name.lower(), account) if account else accounts.by_token(name)
    if not found:
        accounts.check_password(password, accounts.hash_password("x"))  # same timing
        return None
    device_id, account = found
    if not accounts.check_password(password, account.get("password_hash")):
        return None
    return device_id


MIN_PASSWORD_LENGTH = 8


def change_password(device_id, current, new):
    """Raises ValueError with a message for the user."""
    devices = accounts.load()
    account = devices.get(device_id)
    if not account or not accounts.check_password(current, account.get("password_hash")):
        raise ValueError("Current password is wrong")
    if len(new) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"New password must be at least {MIN_PASSWORD_LENGTH} characters")
    if new == current:
        raise ValueError("New password is the same as the current one")
    account["password_hash"] = accounts.hash_password(new)
    accounts.save(devices)


# ------------------------------------------------------- brute-force limit

_failures = {}  # client address -> (failure count, time of first failure)


def is_locked(client, now=None):
    now = now if now is not None else time.time()
    record = _failures.get(client)
    if not record:
        return False

    count, first = record
    if now - first > LOCKOUT_SECONDS:
        _failures.pop(client, None)
        return False

    return count >= MAX_FAILURES


def record_failure(client, now=None):
    now = now if now is not None else time.time()
    count, first = _failures.get(client, (0, now))
    if now - first > LOCKOUT_SECONDS:
        count, first = 0, now
    _failures[client] = (count + 1, first)


def clear_failures(client):
    _failures.pop(client, None)


# ---------------------------------------------------------------- requests


def session_of(request):
    return session_device(request.cookies.get(COOKIE_NAME))


def device_for(request):
    """Which device this request acts for, or None if not allowed."""
    device_id = session_of(request)
    if device_id:
        return device_id

    if request.method == "GET" and request.url.path in DEVICE_PATHS:
        token = request.headers.get("x-device-token") or request.query_params.get("token")
        found = accounts.by_token(token or "")
        if found:
            return found[0]

    return None
