"""HTTP Basic Auth for the web UI, checked against a password hash stored
in config -- never plaintext. PBKDF2 (stdlib hashlib) rather than bcrypt/
argon2 to avoid adding a dependency for a single admin-tool login.
"""
import hashlib
import hmac
import os

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .. import config

_ALGORITHM = "pbkdf2_sha256"
_ITERATIONS = 600_000

_security = HTTPBasic()


def hash_password(plain: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(plain: str, stored_hash: str) -> bool:
    try:
        algo, iterations_s, salt_hex, digest_hex = stored_hash.split("$")
        if algo != _ALGORITHM:
            return False
        iterations = int(iterations_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, AttributeError):
        return False
    computed = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(computed, expected)


def require_auth(credentials: HTTPBasicCredentials = Depends(_security)) -> None:
    if not config.WEBUI_USERNAME or not config.WEBUI_PASSWORD_HASH:
        # Fail closed: an unconfigured password locks the UI out rather
        # than serving it wide open. Set webui.username/password_hash in
        # config.toml (see cache_proxy.webui.hash_password) to unlock it.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Web UI login is not configured -- set [webui] username/password_hash in config.toml",
            headers={"WWW-Authenticate": "Basic"},
        )
    user_ok = hmac.compare_digest(credentials.username, config.WEBUI_USERNAME)
    pass_ok = verify_password(credentials.password, config.WEBUI_PASSWORD_HASH)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
