"""
Dashboard login, CSRF protection and brute-force throttling.

There is one admin password for the dashboard. It is set on first run (using a one-time
setup code printed in the terminal, so nobody else can claim a freshly deployed server)
or supplied with the ``DASHBOARD_PASSWORD`` environment variable.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import tempfile
import time
from functools import wraps
from pathlib import Path
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse

from flask import abort, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD_LEN = 10


def _write_private(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class AuthStore:
    def __init__(self, directory: os.PathLike | str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "auth.json"

    def is_configured(self) -> bool:
        return bool(os.getenv("DASHBOARD_PASSWORD")) or self.file.exists()

    def set_password(self, password: str) -> None:
        _write_private(self.file, json.dumps({"hash": generate_password_hash(password)}).encode())

    def verify(self, password: str) -> bool:
        env_pw = os.getenv("DASHBOARD_PASSWORD")
        if env_pw:
            return hmac.compare_digest(env_pw.encode(), (password or "").encode())
        try:
            stored = json.loads(self.file.read_text())["hash"]
        except (OSError, ValueError, KeyError):
            return False
        return check_password_hash(stored, password or "")


class LoginThrottle:
    """After 5 wrong passwords from one address, block that address for 5 minutes."""

    MAX_FAILS = 5
    LOCK_SECONDS = 300

    def __init__(self) -> None:
        self._state: Dict[str, Tuple[int, float]] = {}

    def seconds_locked(self, key: str) -> int:
        count, until = self._state.get(key, (0, 0.0))
        remaining = int(until - time.time())
        return remaining if remaining > 0 else 0

    def failed(self, key: str) -> None:
        if len(self._state) > 5000:
            self._state.clear()
        count, until = self._state.get(key, (0, 0.0))
        if until and until < time.time():
            count = 0
        count += 1
        self._state[key] = (count, time.time() + self.LOCK_SECONDS if count >= self.MAX_FAILS else 0.0)

    def success(self, key: str) -> None:
        self._state.pop(key, None)


# ----------------------------------------------------------------------------- helpers
def csrf_token() -> str:
    tok = session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(32)
        session["csrf"] = tok
    return tok


def check_csrf() -> None:
    """Reject state-changing requests without a valid token (form field or X-CSRF-Token header)."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
        expected = session.get("csrf") or ""
        if not expected or not hmac.compare_digest(sent.encode(), expected.encode()):
            abort(400, description="Security token missing or expired. Go back, refresh the page and try again.")


def safe_next(target: Optional[str]) -> str:
    """Only allow redirects to paths on this site."""
    if not target:
        return url_for("home")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return url_for("home")
    return target


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("auth") is True:
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "login required"}), 401
        return redirect(url_for("login", next=request.full_path.rstrip("?")))

    return wrapper
