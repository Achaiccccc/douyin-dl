"""环境变量与 Cookie 文件读取。"""
from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

logger = logging.getLogger("douyin-dl.config")

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = BASE_DIR / "static"

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()

_cookie_env = os.environ.get("COOKIE_FILE", "").strip()
if _cookie_env:
    COOKIE_FILE = Path(_cookie_env)
elif Path("/data/cookie.txt").exists():
    COOKIE_FILE = Path("/data/cookie.txt")
else:
    # 本机开发时的回落路径
    COOKIE_FILE = BASE_DIR / "data" / "cookie.txt"

CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "600"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "15"))
DOWNLOAD_TIMEOUT = float(os.environ.get("DOWNLOAD_TIMEOUT", "120"))
# 复用已部署的 evil0ctal 解析引擎。例：http://host.docker.internal:36933
UPSTREAM_API = os.environ.get("UPSTREAM_API", "").strip().rstrip("/")
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "45"))


def session_secret() -> str:
    # 从访问密码派生签名密钥：容器重启后会话仍有效，且不额外暴露密钥变量
    return hashlib.sha256(f"douyin-dl-session:{APP_PASSWORD}".encode("utf-8")).hexdigest()


def load_cookie() -> str:
    """读取挂载的 Cookie 文件；文件不存在时返回空串并记日志。"""
    try:
        # utf-8-sig：容忍网页编辑器保存时带入的 BOM 头
        text = COOKIE_FILE.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        logger.warning("Cookie 文件不存在: %s", COOKIE_FILE)
        return ""
    except IsADirectoryError:
        # 宿主机挂载源缺失时 Docker 会自动创建同名空目录，属典型部署事故
        logger.error(
            "Cookie 路径是目录而不是文件: %s（请在宿主机放好 cookie.txt 后删除该目录并重建容器）",
            COOKIE_FILE,
        )
        return ""
    except OSError as exc:
        logger.error("Cookie 文件读取失败: %s (%s)", COOKIE_FILE, exc)
        return ""
    lines = [line.strip() for line in text.splitlines()]
    cookie = " ".join(line for line in lines if line and not line.startswith("#"))
    # 容忍直接粘贴浏览器请求头整行（"Cookie: xxx"）的情况
    if cookie.lower().startswith("cookie:"):
        cookie = cookie[len("cookie:"):].strip()
    return cookie
