"""Password hashing, symmetric encryption and token helpers.

* Passwords use :func:`hashlib.scrypt` (stdlib only, no native dependency).
* Panel API tokens, customer subscription links and WireGuard configs are
  encrypted at rest with Fernet (AES-128-CBC + HMAC), keyed from ``SECRET_KEY``
  through HKDF-SHA256.
* Session cookies are signed with ``itsdangerous``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.core.config import settings

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def hash_password(password: str) -> str:
    """Hash a password with a per-password random salt."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time password check; never raises on malformed input."""
    if not stored or not password:
        return False
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


def password_strength_error(password: str) -> str | None:
    """Return a Persian error message when a password is too weak."""
    if len(password) < 8:
        return "رمز عبور باید حداقل ۸ کاراکتر باشد."
    if password.isdigit() or password.isalpha():
        return "رمز عبور باید ترکیبی از حرف و رقم باشد."
    return None


# ---------------------------------------------------------------------------
# Symmetric encryption (secrets at rest)
# ---------------------------------------------------------------------------


def _derive_key(purpose: bytes) -> bytes:
    raw = hashlib.sha256(b"wg-guard-bot|" + purpose + b"|" + settings.secret_key.encode()).digest()
    return base64.urlsafe_b64encode(raw)


_fernet_cache: dict[bytes, Fernet] = {}


def _fernet(purpose: bytes = b"default") -> Fernet:
    if purpose not in _fernet_cache:
        _fernet_cache[purpose] = Fernet(_derive_key(purpose))
    return _fernet_cache[purpose]


def encrypt_secret(plaintext: str | None, *, purpose: str = "default") -> str | None:
    """Encrypt a sensitive string for storage.  ``None`` passes through."""
    if plaintext is None:
        return None
    if plaintext == "":
        return ""
    return _fernet(purpose.encode()).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str | None, *, purpose: str = "default") -> str | None:
    """Decrypt a stored secret.  Returns ``None`` when the value is unreadable
    (for example after ``SECRET_KEY`` rotation) instead of crashing a handler."""
    if not ciphertext:
        return ciphertext
    try:
        return _fernet(purpose.encode()).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Tokens & identifiers
# ---------------------------------------------------------------------------

_ALPHABET = string.ascii_letters + string.digits
_SAFE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no look-alike characters


def random_token(length: int = 32) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def random_code(length: int = 6) -> str:
    """Human-friendly code for receipts/orders (no 0/O/1/I confusion)."""
    return "".join(secrets.choice(_SAFE_ALPHABET) for _ in range(length))


def new_api_token() -> str:
    """A panel-scoped token placeholder; WG-Guard tokens are issued upstream."""
    return f"wg_{random_token(40)}"


def constant_time_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def mask_secret(value: str | None, *, keep: int = 4) -> str:
    """``wg_abcd…wxyz`` for safe display in the admin panel."""
    if not value:
        return "—"
    if len(value) <= keep * 2:
        return "•" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"


def mask_card(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit())
    if len(digits) < 8:
        return number
    return f"{digits[:4]}-{digits[4:8]}-****-{digits[-4:]}"


# ---------------------------------------------------------------------------
# Signed cookies (admin panel sessions)
# ---------------------------------------------------------------------------

_SESSION_SALT = "wg-guard-bot.session"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key, salt=_SESSION_SALT)


def sign_session(payload: dict[str, Any]) -> str:
    return _serializer().dumps(payload)


def load_session(token: str, *, max_age: int) -> dict[str, Any] | None:
    try:
        data = _serializer().loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None


def csrf_token_for(session_id: str) -> str:
    """Deterministic CSRF token bound to the current session."""
    return hmac.new(settings.secret_key.encode(), f"csrf|{session_id}".encode(), hashlib.sha256).hexdigest()
