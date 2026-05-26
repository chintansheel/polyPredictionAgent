"""Signup, login, sessions, and auth dependency."""

from __future__ import annotations

import logging
import re
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
import bcrypt
from pydantic import BaseModel, field_validator

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

from web import db
from web.config import (
    AUTH_SECRET,
    COOKIE_NAME,
    RESET_PASSWORD_BASE_URL,
    RESET_TOKEN_MAX_AGE,
    SESSION_MAX_AGE,
)
from web.email import send_password_reset_email

logger = logging.getLogger(__name__)

_serializer = URLSafeTimedSerializer(AUTH_SECRET, salt="foretell-session")
_reset_serializer = URLSafeTimedSerializer(AUTH_SECRET, salt="foretell-reset")

_FORGOT_PASSWORD_MESSAGE = (
    "If an account exists for that email, we sent a password reset link."
)


class SignupBody(BaseModel):
    name: str
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v

    @field_validator("name")
    @classmethod
    def name_not_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        return v

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginBody(BaseModel):
    email: str
    password: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v


class ForgotPasswordBody(BaseModel):
    email: str

    @field_validator("email")
    @classmethod
    def valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email address")
        return v


class ResetPasswordBody(BaseModel):
    token: str
    password: str

    @field_validator("password")
    @classmethod
    def password_min_length(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def set_session_cookie(response: Response, user_id: str) -> None:
    token = _serializer.dumps({"user_id": user_id})
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_MAX_AGE,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def _user_id_from_request(request: Request) -> str | None:
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("user_id")


def signup(body: SignupBody, response: Response) -> dict:
    if db.get_user_by_email(body.email):
        raise HTTPException(status_code=409, detail="Email already registered")
    password_hash = hash_password(body.password)
    user = db.create_user(body.name.strip(), body.email, password_hash)
    set_session_cookie(response, user["id"])
    return {"name": user["name"], "email": user["email"]}


def login(body: LoginBody, response: Response) -> dict:
    user = db.get_user_by_email(body.email)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    db.update_last_login(user["id"])
    set_session_cookie(response, user["id"])
    return {"name": user["name"], "email": user["email"]}


def logout(response: Response) -> dict:
    clear_session_cookie(response)
    return {"ok": True}


def _make_reset_token(user_id: str) -> str:
    return _reset_serializer.dumps({"user_id": user_id})


def _user_id_from_reset_token(token: str) -> str | None:
    try:
        data = _reset_serializer.loads(token, max_age=RESET_TOKEN_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("user_id")


def forgot_password(body: ForgotPasswordBody) -> dict:
    user = db.get_user_by_email(body.email)
    if user:
        if not RESET_PASSWORD_BASE_URL:
            logger.error("RESET_PASSWORD_BASE_URL is not configured")
        else:
            token = _make_reset_token(user["id"])
            reset_url = f"{RESET_PASSWORD_BASE_URL}/reset-password?token={token}"
            try:
                send_password_reset_email(
                    to_email=user["email"],
                    to_name=user["name"],
                    reset_url=reset_url,
                )
            except Exception:
                logger.exception("Failed to send password reset email to %s", body.email)
    return {"message": _FORGOT_PASSWORD_MESSAGE}


def reset_password(body: ResetPasswordBody) -> dict:
    user_id = _user_id_from_reset_token(body.token)
    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset link. Request a new one.",
        )
    user = db.get_user_by_id(user_id)
    if not user:
        raise HTTPException(
            status_code=400,
            detail="Invalid or expired reset link. Request a new one.",
        )
    password_hash = hash_password(body.password)
    if not db.update_user_password(user_id, password_hash):
        raise HTTPException(status_code=400, detail="Could not update password")
    return {"ok": True}


def me(request: Request) -> dict:
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return {"name": user["name"], "email": user["email"]}


def _get_user(request: Request) -> dict | None:
    user_id = _user_id_from_request(request)
    if not user_id:
        return None
    return db.get_user_by_id(user_id)


async def get_current_user(request: Request) -> dict:
    user = _get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


CurrentUser = Annotated[dict, Depends(get_current_user)]
