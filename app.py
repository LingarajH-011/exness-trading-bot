"""
EXNESS MULTI-STRATEGY BOT — web app

Pages
  /            Home: status at a glance and where to go next
  /settings    Account form: enter your Exness / MT5 details (saved encrypted)
  /dashboard   Live dashboard (strategies, risk, positions)
  /login       Sign in (first run: /setup)

Everything except /login, /setup and /healthz needs the dashboard password.
"""
from __future__ import annotations

import logging
import os
import secrets
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

from flask import (Flask, abort, flash, jsonify, redirect,
                   render_template, request, session, url_for)

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config.settings import (ACCOUNT, FIB, INTRADAY, MT5_CFG, NEWS, RISK, SCALP, SWING,  # noqa: E402
                             SYS, WHALE, describe_config)
from core.auth import (MIN_PASSWORD_LEN, AuthStore, LoginThrottle,  # noqa: E402
                       check_csrf, csrf_token, login_required, safe_next)
from core.bot_engine import BotEngine  # noqa: E402
from core.code_runner import RUNNER, synthetic_candles  # noqa: E402
from core.credentials import (CredentialStore, mask_login,  # noqa: E402
                              validate_account_form)
from core.mt5_connector import MT5_AVAILABLE  # noqa: E402
from core.strategy_store import (BUILTIN, RULES, TIMEFRAMES, StrategyStore,  # noqa: E402
                                 validate_custom, validate_tuning)

EXAMPLE_CODE = '''# df has columns: open, high, low, close, volume (the newest candle is the last row).
# Ready to use, no import needed: ema, sma, rsi, atr, crossed_above, crossed_below, np, pd, math
# Return "buy", "sell" or "hold"  (or a dict: {"signal": "buy", "sl": 1.0950, "tp": 1.1050, "reason": "why"})
# If you leave out sl / tp, the stop loss and take profit boxes (x ATR) below are used.

def signal(df):
    fast = ema(df["close"], 9)
    slow = ema(df["close"], 21)
    trend = ema(df["close"], 100)

    if crossed_above(fast, slow) and df["close"].iloc[-1] > trend.iloc[-1]:
        return "buy"
    if crossed_below(fast, slow) and df["close"].iloc[-1] < trend.iloc[-1]:
        return "sell"
    return "hold"
'''


def code_strategies_allowed() -> bool:
    """Python code strategies run code typed into the web page, so they are only on by default
    when the app listens on this computer only. Set ALLOW_CODE_STRATEGIES=1 / 0 to override."""
    env = os.getenv("ALLOW_CODE_STRATEGIES")
    if env in ("0", "1"):
        return env == "1"
    return os.getenv("FLASK_HOST", "127.0.0.1") in ("127.0.0.1", "localhost", "::1")


SERVER_SUGGESTIONS = (["Exness-MT5Real"] + [f"Exness-MT5Real{i}" for i in range(2, 43)]
                      + ["Exness-MT5Trial"] + [f"Exness-MT5Trial{i}" for i in range(2, 21)])


def _load_secret_key(data_dir: Path) -> str:
    env = os.getenv("FLASK_SECRET_KEY")
    if env:
        return env
    f = data_dir / ".flask_secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
        try:
            os.chmod(f, 0o600)
        except OSError:
            pass
    return f.read_text().strip()


def create_app(data_dir=None, engine=None) -> Flask:
    ddir = Path(data_dir or os.getenv("BOT_DATA_DIR") or ROOT / "data")
    ddir.mkdir(parents=True, exist_ok=True)

    creds = CredentialStore(ddir)
    auth = AuthStore(ddir)
    throttle = LoginThrottle()
    bot = engine or BotEngine(creds=creds.load(), data_dir=ddir)
    strategies = StrategyStore(ddir)

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.secret_key = _load_secret_key(ddir)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE") == "1",   # set to 1 when served over HTTPS
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
        MAX_CONTENT_LENGTH=32 * 1024,
        # One-time code, shown only in the terminal, needed to create the first password.
        SETUP_CODE=None if auth.is_configured() else secrets.token_urlsafe(6),
    )
    app.extensions["bot"] = bot
    app.extensions["creds"] = creds
    app.extensions["strategies"] = strategies

    if os.getenv("TRUST_PROXY") == "1":   # behind nginx / a load balancer
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    app.jinja_env.globals["csrf_token"] = csrf_token

    # ------------------------------------------------------------------ helpers
    def status() -> dict:
        stored = creds.load()
        login = stored["login"] if stored else MT5_CFG.login
        server = stored["server"] if stored else MT5_CFG.server
        return {
            "configured": bool(stored) or bool(MT5_CFG.login and MT5_CFG.password),
            "login_masked": mask_login(login),
            "server": server or "not set",
            "bot_running": bot.running,
            "mt5_available": MT5_AVAILABLE,
            "mt5_connected": bot.mt5.connected,
            "mt5_error": bot.mt5.last_error(),
            "mode": SYS.mode,
            "auto_trade": SYS.auto_trade,
        }

    def apply_strategies():
        cfg = strategies.config()
        names = {k: v["name"] for k, v in BUILTIN.items()}
        names.update({k: v["name"] for k, v in cfg["custom"].items()})
        bot.set_strategy_config(cfg, names)

    apply_strategies()

    @app.before_request
    def _csrf():
        check_csrf()

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    def _error(err):
        if request.path.startswith("/api/"):
            return jsonify({"error": err.name}), err.code
        return render_template("error.html", err=err), err.code

    # ------------------------------------------------------------------ auth pages
    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if auth.is_configured():
            return redirect(url_for("login"))
        ip = request.remote_addr or "?"
        if request.method == "POST":
            wait = throttle.seconds_locked(ip)
            code = request.form.get("setup_code", "").strip()
            pw, pw2 = request.form.get("password", ""), request.form.get("confirm", "")
            if wait:
                flash(f"Too many attempts. Try again in {wait // 60 + 1} minute(s).", "error")
                return render_template("setup.html", min_len=MIN_PASSWORD_LEN), 429
            if not secrets.compare_digest(code.encode(), (app.config["SETUP_CODE"] or "").encode()):
                throttle.failed(ip)
                flash("Setup code is wrong. It is printed in the terminal where the app was started.", "error")
            elif len(pw) < MIN_PASSWORD_LEN:
                flash(f"Use at least {MIN_PASSWORD_LEN} characters.", "error")
            elif pw != pw2:
                flash("The two passwords do not match.", "error")
            else:
                auth.set_password(pw)
                app.config["SETUP_CODE"] = None
                throttle.success(ip)
                session.clear()
                session["auth"] = True
                session.permanent = True
                flash("Password created. Next, add your Exness account.", "success")
                return redirect(url_for("settings"))
            return render_template("setup.html", min_len=MIN_PASSWORD_LEN), 400
        return render_template("setup.html", min_len=MIN_PASSWORD_LEN)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.is_configured():
            return redirect(url_for("setup"))
        if request.method == "POST":
            ip = request.remote_addr or "?"
            wait = throttle.seconds_locked(ip)
            if wait:
                flash(f"Too many wrong attempts. Try again in {wait // 60 + 1} minute(s).", "error")
                return render_template("login.html"), 429
            if auth.verify(request.form.get("password", "")):
                throttle.success(ip)
                session.clear()
                session["auth"] = True
                session.permanent = True
                return redirect(safe_next(request.args.get("next")))
            throttle.failed(ip)
            flash("Wrong password.", "error")
            return render_template("login.html"), 401
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ------------------------------------------------------------------ pages
    @app.get("/")
    @login_required
    def home():
        return render_template("home.html", s=status(), active="home")

    @app.get("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html", account_size=ACCOUNT.account_size,
                               server=status()["server"])

    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        stored = creds.load()

        def page(values, code=200):
            return render_template("settings.html", s=status(), active="settings",
                                   v=values, has_password=bool(stored),
                                   servers=SERVER_SUGGESTIONS), code

        if request.method == "GET":
            return page({"login": stored["login"] if stored else "",
                         "server": stored["server"] if stored else (MT5_CFG.server or "Exness-MT5Real"),
                         "path": stored["path"] if stored else ""})

        if request.form.get("action") == "clear":
            creds.clear()
            flash("Saved account details removed.", "info")
            return redirect(url_for("settings"))

        form = request.form
        clean, errors = validate_account_form(form.get("login", ""), form.get("password", ""),
                                              form.get("server", ""), form.get("path", ""),
                                              have_saved_password=bool(stored))
        if (not errors and clean.get("password") is None and stored
                and clean["login"] != stored["login"]):
            errors.append("You changed the account number, so please enter the password for the new account.")
        if errors:
            for e in errors:
                flash(e, "error")
            return page({"login": form.get("login", ""), "server": form.get("server", ""),
                         "path": form.get("path", "")}, 400)

        password = clean["password"] if clean["password"] is not None else stored["password"]
        creds.save(clean["login"], password, clean["server"], clean["path"])

        if form.get("action") == "save_connect":
            threading.Thread(target=bot.apply_credentials, daemon=True,
                             args=(clean["login"], password, clean["server"], clean["path"])).start()
            if MT5_AVAILABLE:
                flash("Saved. Connecting to Exness now: refresh in a few seconds to see the status.", "success")
            else:
                flash("Saved. MetaTrader5 is not available on this machine (it only runs on Windows), "
                      "so the bot stays in demo mode here.", "warn")
        else:
            flash("Saved. Use “Save and connect” to apply it now.", "success")
        return redirect(url_for("settings"))

    # ------------------------------------------------------------------ strategies
    builtin_live = {"scalping": SCALP, "intraday": INTRADAY, "swing": SWING,
                    "news": NEWS, "whale": WHALE, "fibonacci": FIB}

    def builtin_values(sid):
        o = strategies.config()["overrides"].get(sid)
        if o:
            return o
        p = builtin_live[sid]
        sl, tp = (p.sl_pips, p.tp_pips) if sid == "scalping" else (p.sl_atr_mult, p.tp_atr_mult)
        return {"risk_pct": round(RISK.risk_per_trade.get(sid, 0.01) * 100, 2), "sl": sl, "tp": tp}

    @app.get("/strategies")
    @login_required
    def strategies_page():
        rows = strategies.listing()
        errs = getattr(bot, "last_errors", {})
        for r in rows:
            r["error"] = errs.get(r["id"])
        return render_template("strategies.html", active="strategies", rows=rows,
                               on_count=sum(1 for r in rows if r["enabled"]))

    @app.post("/strategies/select")
    @login_required
    def strategies_select():
        valid = {r["id"] for r in strategies.listing()}
        chosen = [x for x in request.form.getlist("on") if x in valid]
        strategies.set_enabled(chosen)
        apply_strategies()
        if chosen:
            flash(f"Saved. The bot will use {len(chosen)} strategy(ies).", "success")
        else:
            flash("Saved. All strategies are off, so the bot will not look for trades.", "warn")
        return redirect(url_for("strategies_page"))

    FORM_FIELDS = ("name", "symbols", "timeframe", "rule", "p1", "p2", "risk_pct", "sl", "tp", "code")

    def check_python(spec, errors):
        """Extra checks for code strategies: allowed here? does it run on test data?"""
        if spec.get("rule") != "python" or errors:
            return
        if not code_strategies_allowed():
            errors.append("Python code strategies are switched off on this server. "
                          "Set ALLOW_CODE_STRATEGIES=1 to allow them (only do this if just you can log in).")
            return
        res, err = RUNNER.run(spec["code"], synthetic_candles(), timeout=8)
        if err:
            errors.append(f"Test run failed: {err}")

    def edit_page(sid, v, errors, code=200):
        custom = strategies.config()["custom"]
        is_custom = sid is None or sid in custom
        return render_template("strategy_edit.html", active="strategies", sid=sid, v=v,
                               is_custom=is_custom, builtin=BUILTIN.get(sid), rules=RULES,
                               timeframes=TIMEFRAMES, errors=errors, code_ok=code_strategies_allowed(),
                               unit="ATR" if is_custom else BUILTIN[sid]["unit"]), code

    @app.route("/strategies/new", methods=["GET", "POST"])
    @login_required
    def strategy_new():
        if request.method == "GET":
            return edit_page(None, {"name": "", "symbols": "EURUSD", "timeframe": "M15", "rule": "ema_cross",
                                    "p1": 9, "p2": 21, "risk_pct": 0.5, "sl": 1.5, "tp": 3, "code": EXAMPLE_CODE}, [])
        spec, errors = validate_custom(request.form)
        check_python(spec, errors)
        if not errors:
            sid = strategies.save_custom(spec)
            if sid is None:
                errors.append("You have reached the limit of custom strategies. Delete one first.")
        if errors:
            f = request.form
            return edit_page(None, {k: f.get(k, "") for k in FORM_FIELDS}, errors, 400)
        apply_strategies()
        flash(f"“{spec['name']}” created. It is switched off until you tick it on this page.", "success")
        return redirect(url_for("strategies_page"))

    @app.route("/strategies/<sid>/edit", methods=["GET", "POST"])
    @login_required
    def strategy_edit(sid):
        custom = strategies.config()["custom"]
        if sid not in BUILTIN and sid not in custom:
            abort(404)
        if request.method == "GET":
            if sid in custom:
                spec = custom[sid]
                return edit_page(sid, {"code": EXAMPLE_CODE, **spec, "symbols": ", ".join(spec["symbols"])}, [])
            return edit_page(sid, builtin_values(sid), [])
        if sid in custom:
            spec, errors = validate_custom(request.form)
            check_python(spec, errors)
            if not errors:
                strategies.save_custom(spec, sid)
        else:
            spec, errors = validate_tuning(request.form, BUILTIN[sid]["unit"])
            if not errors:
                strategies.save_override(sid, spec)
        if errors:
            f = request.form
            return edit_page(sid, {k: f.get(k, "") for k in FORM_FIELDS}, errors, 400)
        apply_strategies()
        flash("Strategy saved. It applies from the next check.", "success")
        return redirect(url_for("strategies_page"))

    @app.post("/strategies/<sid>/delete")
    @login_required
    def strategy_delete(sid):
        if not strategies.delete_custom(sid):
            abort(404)
        apply_strategies()
        flash("Strategy deleted.", "info")
        return redirect(url_for("strategies_page"))

    @app.post("/strategies/<sid>/reset")
    @login_required
    def strategy_reset(sid):
        if sid not in BUILTIN:
            abort(404)
        strategies.reset_override(sid)
        flash("Saved changes removed. The original values are back.", "info")
        apply_strategies()
        return redirect(url_for("strategies_page"))

    # ------------------------------------------------------------------ API (login required)
    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True})

    @app.get("/api/snapshot")
    @login_required
    def api_snapshot():
        snap = bot.get_snapshot()
        snap["config"] = describe_config()
        snap["decisions"] = bot.decisions[:30]
        snap["strategy_names"] = getattr(bot, "names", {})
        return jsonify(snap)

    @app.get("/api/guardian")
    @login_required
    def api_guardian():
        return jsonify({"daily_stats": bot.guardian.get_daily_stats(),
                        "strategy_stats": bot.strategy_stats})

    @app.post("/api/control/start")
    @login_required
    def api_start():
        return jsonify({"ok": bot.start(), "running": bot.running})

    @app.post("/api/control/stop")
    @login_required
    def api_stop():
        bot.stop()
        return jsonify({"ok": True})

    @app.post("/api/control/auto_trade")
    @login_required
    def api_auto_trade():
        enable = request.json.get("enable", False) if request.is_json else False
        if enable:
            bot.enable_auto_trade()
        else:
            bot.disable_auto_trade()
        return jsonify({"ok": True, "auto_trade": SYS.auto_trade})

    @app.post("/api/control/close_all")
    @login_required
    def api_close_all():
        return jsonify({"ok": True, "closed": bot.close_all()})

    @app.get("/api/status")
    @login_required
    def api_status():
        return jsonify({"running": bot.running, "mt5": bot.mt5.connected,
                        "auto_trade": SYS.auto_trade, "mode": SYS.mode})

    return app


def main() -> None:
    os.makedirs(SYS.log_dir, exist_ok=True)
    logging.basicConfig(
        level=getattr(logging, SYS.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(),
                  logging.FileHandler(f"{SYS.log_dir}/bot_{datetime.now():%Y%m%d}.log")],
    )
    app = create_app()
    bot = app.extensions["bot"]

    print("=" * 60)
    print("  EXNESS MULTI-STRATEGY BOT")
    print(f"  Open: http://{SYS.flask_host}:{SYS.flask_port}")
    print(f"  Mode: {SYS.mode} | Auto-trade: {'ON' if SYS.auto_trade else 'OFF'}")
    if app.config["SETUP_CODE"]:
        print("  FIRST RUN: open the page above and create your password.")
        print(f"  Setup code (needed once): {app.config['SETUP_CODE']}")
    print("=" * 60)

    bot.start()
    try:
        try:
            from waitress import serve
            serve(app, host=SYS.flask_host, port=SYS.flask_port, threads=6)
        except ImportError:
            app.run(host=SYS.flask_host, port=SYS.flask_port, debug=False, threaded=True, use_reloader=False)
    finally:
        bot.stop()


if __name__ == "__main__":
    main()
