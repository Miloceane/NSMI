"""Single-password login for the web page, plus a token for the ESP32.

Both values are hardcoded here on purpose (simple, one user). Change them
below, or override with the APP_PASSWORD / DEVICE_TOKEN environment
variables. Changing the password logs everyone out.
"""
import hashlib
import hmac
import os
import time

PASSWORD = os.getenv("APP_PASSWORD", "change-this-password")
DEVICE_TOKEN = os.getenv("DEVICE_TOKEN", "change-this-device-token")

COOKIE_NAME = "train_session"
SESSION_SECONDS = 30 * 24 * 3600   # stay logged in for 30 days
MAX_FAILURES = 5                   # wrong passwords before a lockout...
LOCKOUT_SECONDS = 600              # ...of this many seconds (per client address)

# The ESP32 can read these two without a login session, using the token.
DEVICE_PATHS = {"/color", "/status"}

if PASSWORD == "change-this-password" or DEVICE_TOKEN == "change-this-device-token":
    print("WARNING: default password/device token in use; edit app/auth.py")


# ---------------------------------------------------------------- sessions

def _sign(text):
    key = hashlib.sha256(("train-status-session:" + PASSWORD).encode()).digest()
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()


def make_session(now=None):
    """A cookie value: expiry timestamp + signature. Nothing to store."""
    expires = int((now if now is not None else time.time()) + SESSION_SECONDS)
    return f"{expires}.{_sign(str(expires))}"


def valid_session(value, now=None):
    try:
        expires_text, signature = value.split(".", 1)
        expires = int(expires_text)
    except (AttributeError, ValueError):
        return False

    if expires <= (now if now is not None else time.time()):
        return False

    return hmac.compare_digest(signature.encode(), _sign(expires_text).encode())


def password_ok(candidate):
    return hmac.compare_digest(candidate.encode(), PASSWORD.encode())


def device_token_ok(candidate):
    return bool(candidate) and hmac.compare_digest(
        candidate.encode(), DEVICE_TOKEN.encode()
    )


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

def has_valid_session(request):
    return valid_session(request.cookies.get(COOKIE_NAME))


def request_is_authorized(request):
    if has_valid_session(request):
        return True

    if request.method == "GET" and request.url.path in DEVICE_PATHS:
        token = request.headers.get("x-device-token") or request.query_params.get("token")
        return device_token_ok(token or "")

    return False