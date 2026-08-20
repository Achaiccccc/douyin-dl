"""访问密码校验、session 签发与登录守卫。"""
from __future__ import annotations

import hmac
import time
from collections import defaultdict

from fastapi import APIRouter, Cookie, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel

from . import config

router = APIRouter()

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 7 * 24 * 3600
MAX_FAILS = 5
LOCK_WINDOW_SECONDS = 60

_fails: dict[str, list[float]] = defaultdict(list)


class LoginBody(BaseModel):
    password: str


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(config.session_secret(), salt="douyin-dl")


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _verify(session: str | None) -> None:
    if not session:
        raise HTTPException(status_code=401, detail="未登录")
    try:
        _serializer().loads(session, max_age=SESSION_MAX_AGE)
    except BadSignature:
        raise HTTPException(status_code=401, detail="登录已过期，请重新输入密码")


def require_auth(session: str | None = Cookie(default=None)) -> None:
    _verify(session)


@router.post("/api/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict:
    ip = _client_ip(request)
    now = time.time()
    fails = [t for t in _fails[ip] if now - t < LOCK_WINDOW_SECONDS]
    _fails[ip] = fails
    if len(fails) >= MAX_FAILS:
        raise HTTPException(status_code=429, detail="尝试次数过多，请 1 分钟后再试")
    if not config.APP_PASSWORD:
        raise HTTPException(status_code=500, detail="服务端未配置 APP_PASSWORD")
    if not hmac.compare_digest(body.password, config.APP_PASSWORD):
        fails.append(now)
        raise HTTPException(status_code=401, detail="密码错误")
    _fails.pop(ip, None)
    token = _serializer().dumps("ok")
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True}


@router.get("/api/me")
async def me(session: str | None = Cookie(default=None)) -> dict:
    _verify(session)
    return {"ok": True}
