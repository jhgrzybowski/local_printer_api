from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services.database import Database, User
from app.settings import SESSION_TTL_DAYS


USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,39}$")
PASSWORD_ITERATIONS = 600_000


class AuthError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class LoginSession:
    user: User
    token: str
    expires_at: str


class AuthService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def signup(
        self,
        username: str,
        password: str,
        display_name: str | None,
        user_agent: str | None,
        ip_address: str | None,
    ) -> LoginSession:
        username = normalize_username(username)
        validate_password(password)
        clean_display_name = clean_optional_display_name(display_name)
        salt = secrets.token_bytes(16)
        try:
            user = self.database.create_user(
                username=username,
                display_name=clean_display_name,
                password_hash=hash_password(password, salt, PASSWORD_ITERATIONS),
                password_salt=salt.hex(),
                password_iterations=PASSWORD_ITERATIONS,
            )
        except sqlite3.IntegrityError as exc:
            raise AuthError("Username already exists", 409) from exc
        return self.create_session(user, user_agent, ip_address)

    def login(
        self,
        username: str,
        password: str,
        user_agent: str | None,
        ip_address: str | None,
    ) -> LoginSession:
        username = normalize_username(username)
        row = self.database.get_user_credentials(username)
        if row is None:
            raise AuthError("Invalid username or password", 401)

        expected_hash = str(row["password_hash"])
        actual_hash = hash_password(
            password,
            bytes.fromhex(str(row["password_salt"])),
            int(row["password_iterations"]),
        )
        if not hmac.compare_digest(actual_hash, expected_hash):
            raise AuthError("Invalid username or password", 401)

        user = User(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=row["display_name"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )
        return self.create_session(user, user_agent, ip_address)

    def create_session(
        self,
        user: User,
        user_agent: str | None,
        ip_address: str | None,
    ) -> LoginSession:
        token = secrets.token_urlsafe(32)
        expires_at = (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            + timedelta(days=SESSION_TTL_DAYS)
        ).isoformat()
        self.database.create_session(
            user_id=user.id,
            token_hash=hash_token(token),
            expires_at=expires_at,
            user_agent=truncate_metadata(user_agent),
            ip_address=truncate_metadata(ip_address),
        )
        return LoginSession(user=user, token=token, expires_at=expires_at)

    def user_for_token(self, token: str | None) -> User | None:
        if not token:
            return None
        return self.database.get_user_for_session(hash_token(token))

    def logout(self, token: str | None) -> None:
        if token:
            self.database.delete_session(hash_token(token))


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not USERNAME_RE.fullmatch(normalized):
        raise AuthError(
            "Username must be 3-40 characters using lowercase letters, numbers, dot, underscore, or hyphen",
            400,
        )
    return normalized


def validate_password(password: str) -> None:
    if len(password) < 8:
        raise AuthError("Password must be at least 8 characters", 400)


def clean_optional_display_name(display_name: str | None) -> str | None:
    if display_name is None:
        return None
    cleaned = display_name.strip()
    if not cleaned:
        return None
    return cleaned[:80]


def hash_password(password: str, salt: bytes, iterations: int) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return digest.hex()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def truncate_metadata(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:255]


def public_user(user: User) -> dict[str, Any]:
    return asdict(user)
