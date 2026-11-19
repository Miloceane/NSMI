"""Device accounts: one per physical device, created by the admin (app/admin.py).

Each device has its own route and settings. One person with several
devices (home, work) simply gets several accounts; "owner" is a label.

Stored in DATA_DIR/accounts.json:

    {"devices": {"quin-home": {"name": "Home", "owner": "Quin",
                               "token": "...", "password_hash": "..."}}}

The token identifies the device (the ESP32 sends it); the token together
with the password logs a person into the web page. The file is re-read
when it changes, so accounts added with the CLI work without a restart.
"""
import hashlib
import hmac
import re
import secrets
import threading

from . import storage

ACCOUNTS_FILE = "accounts.json"
ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
PBKDF2_ROUNDS = 200_000

_lock = threading.Lock()
_cache = {"mtime": None, "devices": {}}


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), PBKDF2_ROUNDS
    ).hex()
    return f"pbkdf2${salt}${digest}"


def check_password(password, stored):
    try:
        _, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, salt), stored)
    except (AttributeError, ValueError):
        return False


def new_token():
    return secrets.token_urlsafe(18)


def new_password():
    return secrets.token_urlsafe(9)


def load():
    """All accounts as {device_id: account}. Empty if there is no file."""
    path = storage.DATA_DIR / ACCOUNTS_FILE
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}

    with _lock:
        if mtime != _cache["mtime"]:
            try:
                data = storage.read_json(ACCOUNTS_FILE)
                devices = data.get("devices") or {}
                if not isinstance(devices, dict):
                    raise ValueError("devices must be an object")
                _cache.update(mtime=mtime, devices=devices)
            except (OSError, ValueError, AttributeError) as exc:
                # Keep the last good version rather than locking everyone out.
                print("Could not read accounts file:", exc)
        return {k: dict(v) for k, v in _cache["devices"].items()}


def save(devices):
    storage.write_json(ACCOUNTS_FILE, {"devices": devices})
    with _lock:
        _cache["mtime"] = None  # re-read even if the timestamp didn't change


def get(device_id):
    return load().get(device_id)


def by_token(token):
    """(device_id, account) for a device token, or None."""
    if not token:
        return None
    found = None
    for device_id, account in load().items():
        # Compare against every account so timing doesn't reveal anything.
        if hmac.compare_digest(str(account.get("token", "")).encode(), token.encode()):
            found = (device_id, account)
    return found
