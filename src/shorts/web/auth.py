"""Passwort (scrypt-Hash, beim ersten Start festgelegt) und signierte Sitzungs-Cookies."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass, field

from ..db import Database

COOKIE = "shorts_session"
SESSION_SECONDS = 14 * 24 * 3600
MIN_PASSWORD_LEN = 10
MAX_FAILS, LOCK_SECONDS = 5, 300


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    dk = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk.hex(), dk_hex)


def is_configured(db: Database) -> bool:
    return bool(db.get_setting("password_hash"))


def set_password(db: Database, password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise ValueError(f"Passwort braucht mindestens {MIN_PASSWORD_LEN} Zeichen.")
    db.set_setting("password_hash", hash_password(password))
    db.set_setting("session_secret", secrets.token_hex(32))   # neues Secret → alle alten Sitzungen ungültig


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_cookie(db: Database, now: float | None = None) -> str:
    exp = int((now or time.time()) + SESSION_SECONDS)
    return f"{exp}.{_sign(db.get_setting('session_secret'), str(exp))}"


def valid_cookie(db: Database, value: str | None, now: float | None = None) -> bool:
    secret = db.get_setting("session_secret")
    if not value or not secret or "." not in value:
        return False
    exp, sig = value.split(".", 1)
    if not exp.isdigit() or int(exp) < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(secret, exp))


@dataclass
class LoginThrottle:
    """Sperrt eine IP nach MAX_FAILS Fehlversuchen für LOCK_SECONDS."""
    fails: dict[str, tuple[int, float]] = field(default_factory=dict)

    def locked(self, ip: str, now: float | None = None) -> bool:
        count, until = self.fails.get(ip, (0, 0.0))
        return count >= MAX_FAILS and (now or time.time()) < until

    def failed(self, ip: str, now: float | None = None) -> None:
        now = now or time.time()
        count, until = self.fails.get(ip, (0, 0.0))
        if count >= MAX_FAILS and now >= until:
            count = 0
        self.fails[ip] = (count + 1, now + LOCK_SECONDS)

    def succeeded(self, ip: str) -> None:
        self.fails.pop(ip, None)
