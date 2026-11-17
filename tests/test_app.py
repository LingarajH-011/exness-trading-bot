import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app import create_app  # noqa: E402


class FakeMT5:
    connected = False

    def last_error(self):
        return None


class FakeGuardian:
    def get_daily_stats(self):
        return []


class FakeEngine:
    """Stands in for the real engine so tests never touch MetaTrader5."""

    def __init__(self):
        self.mt5 = FakeMT5()
        self.guardian = FakeGuardian()
        self.strategy_stats = {}
        self.decisions = []
        self.running = False
        self.applied = None
        self.applied_event = threading.Event()
        self.closed = 0

    def start(self):
        self.running = True
        return True

    def stop(self):
        self.running = False

    def get_snapshot(self):
        return {}

    def enable_auto_trade(self):
        pass

    def disable_auto_trade(self):
        pass

    def close_all(self):
        self.closed += 1
        return 0

    def set_strategy_config(self, cfg, names=None):
        self.strategy_cfg = cfg
        self.names = names or {}

    def apply_credentials(self, login, password, server, path=""):
        self.applied = (login, password, server, path)
        self.applied_event.set()
        return True


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("DASHBOARD_PASSWORD", "BOT_SECRET_KEY", "FLASK_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture()
def engine():
    return FakeEngine()


@pytest.fixture()
def app(tmp_path, engine):
    a = create_app(data_dir=tmp_path, engine=engine)
    a.config["TESTING"] = True
    return a


def token(client):
    client.get("/login")
    with client.session_transaction() as s:
        return s["csrf"]


@pytest.fixture()
def authed(app):
    """A client that is already signed in, with a known CSRF token."""
    from core.auth import AuthStore
    AuthStore(app.extensions["creds"].dir).set_password("a-long-test-password")
    app.config["SETUP_CODE"] = None
    c = app.test_client()
    with c.session_transaction() as s:
        s["auth"] = True
        s["csrf"] = "test-token"
    c.csrf = "test-token"
    return c


VALID = {"login": "12345678", "password": "s3cret-Pass!", "server": "Exness-MT5Trial",
         "path": "", "csrf_token": "test-token"}


# ------------------------------------------------------------------ credential store
def test_credentials_roundtrip_and_encrypted_on_disk(tmp_path):
    from core.credentials import CredentialStore
    store = CredentialStore(tmp_path)
    store.save(12345678, "s3cret-Pass!", "Exness-MT5Real", r"C:\mt5\terminal64.exe")
    assert store.load() == {"login": 12345678, "password": "s3cret-Pass!",
                            "server": "Exness-MT5Real", "path": r"C:\mt5\terminal64.exe"}
    raw = store.file.read_bytes()
    assert b"s3cret-Pass!" not in raw and b"12345678" not in raw


def test_wrong_key_returns_none(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from core.credentials import CredentialStore
    monkeypatch.setenv("BOT_SECRET_KEY", Fernet.generate_key().decode())
    CredentialStore(tmp_path).save(1, "pw", "S")
    monkeypatch.setenv("BOT_SECRET_KEY", Fernet.generate_key().decode())
    assert CredentialStore(tmp_path).load() is None


def test_clear(tmp_path):
    from core.credentials import CredentialStore
    s = CredentialStore(tmp_path)
    s.save(1, "pw", "S")
    s.clear()
    s.clear()  # twice is fine
    assert s.load() is None


@pytest.mark.parametrize("login,pw,server,ok", [
    ("12345678", "x", "Exness-MT5Real", True),
    ("abc", "x", "Exness-MT5Real", False),
    ("", "x", "Exness-MT5Real", False),
    ("0", "x", "Exness-MT5Real", False),
    ("123456789012345", "x", "Exness-MT5Real", False),
    ("1", "", "Exness-MT5Real", False),
    ("1", "x", "bad server;rm -rf", False),
    ("1", "x", "", False),
])
def test_validation(login, pw, server, ok):
    from core.credentials import validate_account_form
    _, errors = validate_account_form(login, pw, server, "", have_saved_password=False)
    assert (errors == []) is ok


def test_validation_keeps_saved_password_when_blank():
    from core.credentials import validate_account_form
    clean, errors = validate_account_form("1", "", "Exness-MT5Real", "", have_saved_password=True)
    assert errors == [] and clean["password"] is None


def test_mask_login():
    from core.credentials import mask_login
    assert mask_login(12345678).endswith("678") and "12345" not in mask_login(12345678)
    assert mask_login(0) == "not set"


# ------------------------------------------------------------------ first-run setup and login
def test_first_run_redirects_to_setup(app):
    r = app.test_client().get("/")
    assert r.status_code == 302 and "/login" in r.location
    r = app.test_client().get("/login")
    assert r.status_code == 302 and "/setup" in r.location


def test_setup_needs_correct_code_then_logs_in(app):
    c = app.test_client()
    c.get("/setup")
    with c.session_transaction() as s:
        t = s["csrf"]
    bad = c.post("/setup", data={"csrf_token": t, "setup_code": "wrong",
                                 "password": "a-long-password", "confirm": "a-long-password"})
    assert bad.status_code == 400
    code = app.config["SETUP_CODE"]
    short = c.post("/setup", data={"csrf_token": t, "setup_code": code, "password": "short", "confirm": "short"})
    assert short.status_code == 400
    ok = c.post("/setup", data={"csrf_token": t, "setup_code": code,
                                "password": "a-long-password", "confirm": "a-long-password"})
    assert ok.status_code == 302 and "/settings" in ok.location
    assert c.get("/settings").status_code == 200
    # setup is closed afterwards
    assert c.get("/setup").status_code == 302


def test_login_wrong_then_right_and_logout(authed, app):
    c = app.test_client()
    t = token(c)
    assert c.post("/login", data={"csrf_token": t, "password": "nope"}).status_code == 401
    assert c.get("/").status_code == 302
    ok = c.post("/login", data={"csrf_token": t, "password": "a-long-test-password"})
    assert ok.status_code == 302
    assert c.get("/").status_code == 200
    with c.session_transaction() as s:
        t2 = s["csrf"]
    c.post("/logout", data={"csrf_token": t2})
    assert c.get("/").status_code == 302


def test_login_lockout_after_five_failures(authed, app):
    c = app.test_client()
    t = token(c)
    for _ in range(5):
        c.post("/login", data={"csrf_token": t, "password": "bad"})
    r = c.post("/login", data={"csrf_token": t, "password": "a-long-test-password"})
    assert r.status_code == 429


def test_login_ignores_offsite_next(authed, app):
    c = app.test_client()
    t = token(c)
    r = c.post("/login?next=//evil.example", data={"csrf_token": t, "password": "a-long-test-password"})
    assert r.status_code == 302 and "evil" not in r.location


def test_env_password_override(tmp_path, engine, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "env-password-123")
    a = create_app(data_dir=tmp_path, engine=engine)
    c = a.test_client()
    t = token(c)
    assert c.post("/login", data={"csrf_token": t, "password": "env-password-123"}).status_code == 302


# ------------------------------------------------------------------ protection of pages and API
def test_pages_and_api_need_login(app, authed):
    anon = app.test_client()
    for path in ("/", "/settings", "/dashboard"):
        assert anon.get(path).status_code == 302
    r = anon.get("/api/status")
    assert r.status_code == 401 and r.get_json() == {"error": "login required"}
    assert anon.get("/api/snapshot").status_code == 401


def test_healthz_is_public(app):
    assert app.test_client().get("/healthz").get_json() == {"ok": True}


def test_post_without_csrf_is_rejected(authed, engine):
    assert authed.post("/api/control/close_all").status_code == 400
    assert engine.closed == 0
    ok = authed.post("/api/control/close_all", headers={"X-CSRF-Token": authed.csrf})
    assert ok.status_code == 200 and engine.closed == 1


def test_non_ascii_csrf_does_not_crash(authed):
    r = authed.post("/api/control/close_all", headers={"X-CSRF-Token": "tökén"})
    assert r.status_code == 400


def test_security_headers(authed):
    r = authed.get("/")
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "no-store" in r.headers["Cache-Control"]


def test_pages_render(authed):
    home = authed.get("/")
    assert home.status_code == 200 and b"Account settings" in home.data
    dash = authed.get("/dashboard")
    assert dash.status_code == 200 and b"test-token" in dash.data   # CSRF token is wired into the page
    assert authed.get("/nope").status_code == 404


# ------------------------------------------------------------------ settings form
def test_save_and_connect_applies_credentials(authed, engine, app):
    r = authed.post("/settings", data={**VALID, "action": "save_connect"})
    assert r.status_code == 302
    assert engine.applied_event.wait(2)
    assert engine.applied == (12345678, "s3cret-Pass!", "Exness-MT5Trial", "")
    saved = app.extensions["creds"].load()
    assert saved["login"] == 12345678 and saved["password"] == "s3cret-Pass!"


def test_password_is_never_sent_back_to_the_browser(authed):
    authed.post("/settings", data={**VALID, "action": "save"})
    page = authed.get("/settings")
    assert b"s3cret-Pass!" not in page.data
    assert b"Saved. Leave blank to keep it." in page.data


def test_blank_password_keeps_saved_one(authed, app):
    authed.post("/settings", data={**VALID, "action": "save"})
    authed.post("/settings", data={**VALID, "password": "", "server": "Exness-MT5Real", "action": "save"})
    saved = app.extensions["creds"].load()
    assert saved["password"] == "s3cret-Pass!" and saved["server"] == "Exness-MT5Real"


def test_changing_login_requires_password(authed, app):
    authed.post("/settings", data={**VALID, "action": "save"})
    r = authed.post("/settings", data={**VALID, "login": "99999999", "password": "", "action": "save"})
    assert r.status_code == 400
    assert app.extensions["creds"].load()["login"] == 12345678


def test_invalid_form_is_rejected_and_nothing_saved(authed, app):
    r = authed.post("/settings", data={**VALID, "login": "abc", "action": "save"})
    assert r.status_code == 400 and b"digits only" in r.data
    assert app.extensions["creds"].load() is None


def test_settings_post_needs_csrf(authed):
    bad = {k: v for k, v in VALID.items() if k != "csrf_token"}
    assert authed.post("/settings", data={**bad, "action": "save"}).status_code == 400


def test_remove_saved_details(authed, app):
    authed.post("/settings", data={**VALID, "action": "save"})
    assert app.extensions["creds"].load() is not None
    authed.post("/settings", data={"csrf_token": "test-token", "action": "clear"})
    assert app.extensions["creds"].load() is None


# ------------------------------------------------------------------ strategies page
CUSTOM = {"name": "My EMA", "symbols": "eurusd, xauusd", "timeframe": "M15", "rule": "ema_cross",
          "p1": "9", "p2": "21", "risk_pct": "0.5", "sl": "1.5", "tp": "3", "csrf_token": "test-token"}


def test_strategies_page_needs_login_and_lists_all(app, authed):
    assert app.test_client().get("/strategies").status_code == 302
    page = authed.get("/strategies")
    assert page.status_code == 200
    for name in (b"Scalping", b"Intraday", b"Swing", b"Fibonacci", b"Create new strategy"):
        assert name in page.data


def test_select_only_relevant_strategies(authed, engine):
    authed.post("/strategies/select", data={"csrf_token": "test-token", "on": ["swing", "bogus"]})
    en = engine.strategy_cfg["enabled"]
    assert en["swing"] is True and en["scalping"] is False and "bogus" not in en


def test_select_needs_csrf(authed):
    assert authed.post("/strategies/select", data={"on": "swing"}).status_code == 400


def test_create_custom_strategy_starts_off_then_edit_and_delete(authed, engine):
    r = authed.post("/strategies/new", data=CUSTOM)
    assert r.status_code == 302
    cfg = engine.strategy_cfg
    sid = next(iter(cfg["custom"]))
    assert cfg["custom"][sid]["symbols"] == ["EURUSD", "XAUUSD"] and cfg["enabled"][sid] is False
    assert engine.names[sid] == "My EMA"
    assert b"My EMA" in authed.get("/strategies").data
    authed.post(f"/strategies/{sid}/edit", data={**CUSTOM, "name": "Renamed", "p1": "5"})
    assert engine.strategy_cfg["custom"][sid]["name"] == "Renamed"
    authed.post(f"/strategies/{sid}/delete", data={"csrf_token": "test-token"})
    assert engine.strategy_cfg["custom"] == {}
    assert authed.post(f"/strategies/{sid}/delete", data={"csrf_token": "test-token"}).status_code == 404


@pytest.mark.parametrize("change", [
    {"name": ""}, {"symbols": "EUR USD;"}, {"timeframe": "M2"}, {"rule": "magic"},
    {"p1": "30", "p2": "10"}, {"risk_pct": "50"}, {"sl": "5", "tp": "1"}, {"symbols": "A,B,C,D,E,F"},
])
def test_invalid_custom_strategy_rejected(authed, engine, change):
    r = authed.post("/strategies/new", data={**CUSTOM, **change})
    assert r.status_code == 400
    assert engine.strategy_cfg["custom"] == {}


def test_edit_and_reset_builtin(authed, engine):
    assert authed.get("/strategies/swing/edit").status_code == 200
    r = authed.post("/strategies/swing/edit", data={"csrf_token": "test-token", "risk_pct": "0.7", "sl": "2.5", "tp": "5"})
    assert r.status_code == 302
    assert engine.strategy_cfg["overrides"]["swing"] == {"risk_pct": 0.7, "sl": 2.5, "tp": 5.0}
    bad = authed.post("/strategies/swing/edit", data={"csrf_token": "test-token", "risk_pct": "9", "sl": "2", "tp": "4"})
    assert bad.status_code == 400
    authed.post("/strategies/swing/reset", data={"csrf_token": "test-token"})
    assert "swing" not in engine.strategy_cfg["overrides"]
    assert authed.get("/strategies/nope/edit").status_code == 404


def test_custom_limit(authed):
    for i in range(10):
        assert authed.post("/strategies/new", data={**CUSTOM, "name": f"s{i}"}).status_code == 302
    assert authed.post("/strategies/new", data=CUSTOM).status_code == 400


def test_custom_signal_ema_cross_and_breakout():
    import pandas as pd
    from strategies.custom import custom_signal
    close = [1.0] * 60 + [1.0 + 0.01 * i for i in range(1, 15)]
    df = pd.DataFrame({"close": close, "high": [c + 0.002 for c in close], "low": [c - 0.002 for c in close]})
    spec = {"name": "t", "rule": "breakout", "p1": 20, "p2": 0, "sl": 1.5, "tp": 3}
    sig = custom_signal(df, spec, "cx1")
    assert sig["signal"] == "buy" and sig["sl"] < df.close.iloc[-1] < sig["tp"]
    flat = df.iloc[:60]
    assert custom_signal(flat, spec, "cx1")["signal"] == "hold"
    assert custom_signal(flat.iloc[:10], spec, "cx1")["signal"] == "hold"


# ------------------------------------------------------------------ python code strategies
GOOD_CODE = '''def signal(df):
    if crossed_above(ema(df["close"], 5), ema(df["close"], 20)):
        return "buy"
    return "hold"
'''
PYSTRAT = {**CUSTOM, "rule": "python", "code": GOOD_CODE}


@pytest.mark.parametrize("bad", [
    "import os\ndef signal(df):\n    return 'hold'",
    "def signal(df):\n    return open('x').read()",
    "def signal(df):\n    return ().__class__",
    "def signal(df):\n    return eval('1')",
    "def signal(df):\n    df.to_csv('x.csv')\n    return 'hold'",
    "def signal(df):\n    return pd.read_csv('x.csv')",
    "def signal(df):\n    return '{0.__class__}'.format(df)",
    "def signal(df):\n    g = (i for i in [1])\n    return g.gi_frame.f_back.f_globals",
    "class A: pass\ndef signal(df):\n    return 'hold'",
    "x = 1",
    "def signal(df:\n    return",
    "",
])
def test_unsafe_or_broken_code_is_refused(bad):
    from core.code_worker import check_code
    assert check_code(bad)


def test_good_code_passes_check():
    from core.code_worker import check_code
    assert check_code(GOOD_CODE) == []


def test_runner_runs_code_and_handles_failures():
    from core.code_runner import CodeRunner, synthetic_candles
    r = CodeRunner(timeout=3)
    df = synthetic_candles()
    try:
        res, err = r.run(GOOD_CODE, df)
        assert err is None and res["signal"] in ("buy", "hold")
        res, err = r.run("def signal(df):\n    return 'maybe'", df)
        assert res is None and "unknown signal" in err
        res, err = r.run("def signal(df):\n    return 1 / 0", df)
        assert res is None and "ZeroDivisionError" in err
        res, err = r.run("def signal(df):\n    return {'signal': 'buy', 'sl': 99, 'tp': 100}", df)
        assert res is None and "sl must be below" in err
        res, err = r.run("def signal(df):\n    while True:\n        pass", df, timeout=1)
        assert res is None and "stopped" in err
        res, err = r.run(GOOD_CODE, df)          # the worker restarts after a timeout
        assert err is None
    finally:
        r.stop()


def test_create_python_strategy_and_it_runs_in_the_engine_path(authed, engine):
    r = authed.post("/strategies/new", data=PYSTRAT)
    assert r.status_code == 302
    sid = next(iter(engine.strategy_cfg["custom"]))
    spec = engine.strategy_cfg["custom"][sid]
    assert spec["rule"] == "python" and "crossed_above" in spec["code"]
    from core.code_runner import RUNNER, synthetic_candles
    from strategies.custom import custom_signal
    try:
        sig = custom_signal(synthetic_candles(), {**spec, "sl": 1.5, "tp": 3}, sid)
        assert sig["signal"] in ("buy", "hold") and "error" not in sig
    finally:
        RUNNER.stop()
    assert b"Python" in authed.get("/strategies").data


def test_python_strategy_with_bad_code_is_rejected(authed, engine):
    r = authed.post("/strategies/new", data={**PYSTRAT, "code": "import os\ndef signal(df): return 'hold'"})
    assert r.status_code == 400 and b"not allowed" in r.data
    r = authed.post("/strategies/new", data={**PYSTRAT, "code": "def signal(df):\n    return df['nope']"})
    assert r.status_code == 400 and b"Test run failed" in r.data
    assert engine.strategy_cfg["custom"] == {}


def test_code_strategies_off_when_exposed(authed, engine, monkeypatch):
    monkeypatch.setenv("FLASK_HOST", "0.0.0.0")
    r = authed.post("/strategies/new", data=PYSTRAT)
    assert r.status_code == 400 and b"switched off" in r.data
    monkeypatch.setenv("ALLOW_CODE_STRATEGIES", "1")
    assert authed.post("/strategies/new", data=PYSTRAT).status_code == 302
    from core.code_runner import RUNNER
    RUNNER.stop()
