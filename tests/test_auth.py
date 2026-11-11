from types import SimpleNamespace

from app import auth


def request(method="GET", path="/color", cookies=None, headers=None, query=None):
    return SimpleNamespace(
        method=method,
        url=SimpleNamespace(path=path),
        cookies=cookies or {},
        headers=headers or {},
        query_params=query or {},
    )


def test_session_roundtrip_expiry_and_tampering():
    cookie = auth.make_session(now=1000)
    assert auth.valid_session(cookie, now=1001)
    assert not auth.valid_session(cookie, now=1000 + auth.SESSION_SECONDS + 1)

    expires, signature = cookie.split(".")
    assert not auth.valid_session(f"{int(expires) + 999999}.{signature}", now=1001)
    assert not auth.valid_session("garbage", now=1001)
    assert not auth.valid_session(None, now=1001)


def test_password_and_token(monkeypatch):
    monkeypatch.setattr(auth, "PASSWORD", "hunter2")
    monkeypatch.setattr(auth, "DEVICE_TOKEN", "tok")
    assert auth.password_ok("hunter2")
    assert not auth.password_ok("hunter3") and not auth.password_ok("")
    assert auth.device_token_ok("tok")
    assert not auth.device_token_ok("") and not auth.device_token_ok("nope")


def test_session_stops_working_when_password_changes(monkeypatch):
    monkeypatch.setattr(auth, "PASSWORD", "old")
    cookie = auth.make_session()
    assert auth.valid_session(cookie)
    monkeypatch.setattr(auth, "PASSWORD", "new")
    assert not auth.valid_session(cookie)


def test_device_token_only_opens_read_only_endpoints(monkeypatch):
    monkeypatch.setattr(auth, "DEVICE_TOKEN", "tok")
    assert auth.request_is_authorized(request(headers={"x-device-token": "tok"}))
    assert auth.request_is_authorized(request(query={"token": "tok"}))
    assert not auth.request_is_authorized(request(query={"token": "bad"}))
    assert not auth.request_is_authorized(request())
    # the token must not open the settings or let anyone change things
    assert not auth.request_is_authorized(request(path="/config", query={"token": "tok"}))
    assert not auth.request_is_authorized(request(method="POST", path="/status", query={"token": "tok"}))


def test_valid_session_cookie_opens_everything():
    cookie = auth.make_session()
    assert auth.request_is_authorized(
        request(method="POST", path="/config", cookies={auth.COOKIE_NAME: cookie})
    )


def test_lockout_after_repeated_failures():
    client = "10.0.0.9"
    auth.clear_failures(client)
    for _ in range(auth.MAX_FAILURES):
        assert not auth.is_locked(client, now=100)
        auth.record_failure(client, now=100)
    assert auth.is_locked(client, now=101)
    assert not auth.is_locked(client, now=100 + auth.LOCKOUT_SECONDS + 1)