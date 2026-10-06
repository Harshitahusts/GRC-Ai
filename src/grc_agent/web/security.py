"""Password hashing and CSRF tokens.

Passwords are hashed with Argon2id through the argon2-cffi library: the algorithm OWASP
recommends first, with the library's default settings (RFC 9106's recommended profile:
3 passes over 64 MiB, 4 lanes). Each hash string carries its own salt and settings, so
they can be raised later without breaking existing passwords.

Older versions of GRC Flow stored scrypt hashes ("scrypt$..."). Those still verify, and
`needs_rehash()` tells the sign-in code to replace them with Argon2id the next time that
person signs in, so the whole workspace moves over without anyone noticing.

CSRF tokens are random strings from the standard library's `secrets` module, compared in
constant time so their value can't be guessed from how long a check takes.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()  # Argon2id with the library's recommended defaults

_LEGACY = "scrypt$"  # prefix of hashes made by earlier versions


def hash_password(password: str) -> str:
    """A new Argon2id hash for `password` (salt included in the result)."""
    return _hasher.hash(password)


def verify_password(password: str, stored: str) -> bool:
    """True if `password` matches the stored hash, whichever scheme made it.

    Anything that isn't a valid hash (for example the "!none" marker of accounts that
    sign in with Google or Microsoft only) never matches.
    """
    if stored.startswith(_LEGACY):
        return _verify_scrypt(password, stored)
    try:
        return _hasher.verify(stored, password)
    except (VerificationError, InvalidHashError):  # wrong password, or not a hash at all
        return False


def needs_rehash(stored: str) -> bool:
    """True when a hash should be replaced at the next successful sign-in: an old scrypt
    hash, or an Argon2 hash made with weaker settings than the current ones."""
    if stored.startswith(_LEGACY):
        return True
    try:
        return _hasher.check_needs_rehash(stored)
    except InvalidHashError:  # "!none" and other markers are left alone
        return False


def _verify_scrypt(password: str, stored: str) -> bool:
    """Check a hash made by earlier versions: scrypt$N$r$p$salt-hex$digest-hex."""
    try:
        _scheme, n, r, p, salt, digest = stored.split("$")
        candidate = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p)
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate.hex(), digest)


# Checked against when the username doesn't exist, so a sign-in attempt takes the same
# time whether or not the account exists (no guessing usernames from response times).
DUMMY_HASH = hash_password(secrets.token_hex(16))


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def csrf_matches(expected: str | None, received: str | None) -> bool:
    """True only if both tokens are present and equal (compared in constant time)."""
    return bool(expected and received) and hmac.compare_digest(expected, received)
