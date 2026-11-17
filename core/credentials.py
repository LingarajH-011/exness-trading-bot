"""
Encrypted storage for the Exness / MT5 account details entered in the web form.

Why this exists
---------------
Account details used to live in a plain-text ``.env`` file. They are now entered on the
Settings page and stored encrypted (Fernet / AES) in ``data/credentials.enc``.

What this does and does not protect
-----------------------------------
* The file on disk is not readable without the key, so a copied backup or a leaked
  ``data/`` folder does not expose your MT5 password.
* The key lives next to the data by default (``data/.secret_key``). Anyone who can read
  both files on the server can decrypt them. For stronger separation, set the
  ``BOT_SECRET_KEY`` environment variable instead, and the key is never written to disk.
* Nothing here replaces locking down the server (firewall, HTTPS, login).
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

log = logging.getLogger(__name__)

_SERVER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,62}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PATH_BAD_RE = re.compile(r'[\x00-\x1f\x7f"<>|*?]')


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write a file that only the current user can read (best effort on Windows)."""
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


def mask_login(login: object) -> str:
    """Show only the last 3 digits of the account number."""
    s = str(login or "").strip()
    if not s or s == "0":
        return "not set"
    return "•" * max(len(s) - 3, 3) + s[-3:]


def validate_account_form(
    login: str, password: str, server: str, path: str, have_saved_password: bool
) -> Tuple[Dict[str, object], List[str]]:
    """Validate form input. Returns (clean_values, error_messages)."""
    errors: List[str] = []
    clean: Dict[str, object] = {}

    login = (login or "").strip()
    if not login.isdigit() or not (1 <= len(login) <= 12) or int(login) <= 0:
        errors.append("Account number must be digits only (for example 12345678).")
    else:
        clean["login"] = int(login)

    password = password or ""
    if password == "" and have_saved_password:
        clean["password"] = None  # keep the saved one
    elif password == "":
        errors.append("Enter your MT5 password.")
    elif len(password) > 128 or _CONTROL_RE.search(password):
        errors.append("Password is too long or contains invalid characters.")
    else:
        clean["password"] = password

    server = (server or "").strip()
    if not _SERVER_RE.match(server):
        errors.append("Server name looks wrong. Copy it exactly from your MT5 terminal, e.g. Exness-MT5Real.")
    else:
        clean["server"] = server

    path = (path or "").strip()
    if len(path) > 260 or _PATH_BAD_RE.search(path):
        errors.append("MT5 terminal path contains invalid characters.")
    else:
        clean["path"] = path

    return clean, errors


class CredentialStore:
    """Read/write the encrypted account file."""

    def __init__(self, directory: os.PathLike | str):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.file = self.dir / "credentials.enc"
        self.key_file = self.dir / ".secret_key"

    def _fernet(self) -> Fernet:
        env_key = os.getenv("BOT_SECRET_KEY")
        if env_key:
            return Fernet(env_key.encode())
        if not self.key_file.exists():
            _write_private(self.key_file, Fernet.generate_key())
        return Fernet(self.key_file.read_bytes().strip())

    def exists(self) -> bool:
        return self.file.exists()

    def load(self) -> Optional[Dict[str, object]]:
        """Return {'login', 'password', 'server', 'path'} or None if nothing usable is saved."""
        if not self.file.exists():
            return None
        try:
            raw = self._fernet().decrypt(self.file.read_bytes())
            data = json.loads(raw.decode("utf-8"))
            return {
                "login": int(data["login"]),
                "password": str(data["password"]),
                "server": str(data["server"]),
                "path": str(data.get("path", "")),
            }
        except (InvalidToken, ValueError, KeyError, OSError):
            log.error("Saved account details could not be read (wrong key or damaged file). Re-enter them on the Settings page.")
            return None

    def save(self, login: int, password: str, server: str, path: str = "") -> None:
        payload = json.dumps(
            {"login": int(login), "password": password, "server": server, "path": path}
        ).encode("utf-8")
        _write_private(self.file, self._fernet().encrypt(payload))

    def clear(self) -> None:
        try:
            self.file.unlink()
        except FileNotFoundError:
            pass
