from types import SimpleNamespace

import pytest

from app import accounts, auth


def request(method="GET", path="/color", cookies=None, headers=None, query=None):
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        cookies=cookies or {},
        headers=headers or {},
        query_params=query or {},
    )


@pytest.fixture
def devices():
    accounts.save({
        "home": {"name": "Home", "owner": "A", "token": "tok-home",
                 "password_hash": accounts.hash_password("pw-home")},
        "work": {"name": "Work", "owner": "A", "token": "tok-work",
                 "password_hash": accounts.hash_password("pw-work")},
    })
    yield
    accounts.save({})


def test_password_hashing():
    stored = accounts.hash_password("hunter2")
    assert accounts.check_password("hunter2", stored)
    assert not accounts.check_password("hunter3", stored)
    assert not accounts.check_password("x", "garbage")


def test_login_needs_matching_token_and_password(devices):
    assert auth.login("tok-home", "pw-home") == "home"
    assert auth.login("tok-work", "pw-work") == "work"
    assert auth.login("tok-home", "pw-work") is None   # other device's password
    assert auth.login("nope", "pw-home") is None
    assert auth.login("", "") is None
    # the short device id works too, case-insensitive
    assert auth.login("home", "pw-home") == "home"
    assert auth.login(" Home ", "pw-home") == "home"
    assert auth.login("work", "pw-home") is None


def test_session_roundtrip_expiry_and_tampering(devices):
    cookie = auth.make_session("home", now=1000)
    assert auth.session_device(cookie, now=1001) == "home"
    assert auth.session_device(cookie, now=1000 + auth.SESSION_SECONDS + 1) is None

    device, expires, signature = cookie.split(".")
    assert auth.session_device(f"work.{expires}.{signature}", now=1001) is None
    assert auth.session_device(f"home.{int(expires) + 999}.{signature}", now=1001) is None
    assert auth.session_device("garbage", now=1001) is None
    assert auth.session_device(None, now=1001) is None


def test_password_change_or_removal_ends_sessions(devices):
    cookie = auth.make_session("home")
    assert auth.session_device(cookie) == "home"
    all_devices = accounts.load()
    all_devices["home"]["password_hash"] = accounts.hash_password("new")
    accounts.save(all_devices)
    assert auth.session_device(cookie) is None

    cookie = auth.make_session("work")
    accounts.save({})
    assert auth.session_device(cookie) is None


def test_device_token_picks_device_and_is_read_only(devices):
    assert auth.device_for(request(headers={"x-device-token": "tok-home"})) == "home"
    assert auth.device_for(request(query={"token": "tok-work"})) == "work"
    assert auth.device_for(request(query={"token": "bad"})) is None
    assert auth.device_for(request()) is None
    assert auth.device_for(request(path="/config", query={"token": "tok-home"})) is None
    assert auth.device_for(request(method="POST", path="/status", query={"token": "tok-home"})) is None


def test_session_cookie_opens_everything_for_its_device(devices):
    cookie = auth.make_session("work")
    assert auth.device_for(
        request(method="POST", path="/config", cookies={auth.COOKIE_NAME: cookie})
    ) == "work"


def test_lockout_after_repeated_failures():
    client = "10.0.0.9"
    auth.clear_failures(client)
    for _ in range(auth.MAX_FAILURES):
        assert not auth.is_locked(client, now=100)
        auth.record_failure(client, now=100)
    assert auth.is_locked(client, now=101)
    assert not auth.is_locked(client, now=100 + auth.LOCKOUT_SECONDS + 1)


def test_change_password(devices):
    cookie = auth.make_session("home")
    with pytest.raises(ValueError, match="wrong"):
        auth.change_password("home", "nope", "newpassword")
    with pytest.raises(ValueError, match="at least"):
        auth.change_password("home", "pw-home", "short")
    auth.change_password("home", "pw-home", "newpassword")
    assert auth.login("home", "newpassword") == "home"
    assert auth.login("home", "pw-home") is None
    assert auth.session_device(cookie) is None          # old sessions end
    assert auth.login("work", "pw-work") == "work"      # other devices untouched