"""路由：页面、登录、解析（批量/单条重试）、封面代理、下载代理、健康检查。"""
from __future__ import annotations

import json
import logging
import re
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, parser

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("douyin-dl.main")


@asynccontextmanager
async def lifespan(_: FastAPI):
    cookie = config.load_cookie()
    logger.info(
        "启动: COOKIE_FILE=%s cookie长度=%d parser=%s",
        config.COOKIE_FILE,
        len(cookie),
        f"upstream:{config.UPSTREAM_API}" if config.UPSTREAM_API else "embedded-crawler",
    )
    if not cookie and not config.UPSTREAM_API:
        logger.warning("未读到 Cookie，内嵌解析将不可用")
    yield


app = FastAPI(title="douyin-dl", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.include_router(auth.router)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


class ParseBody(BaseModel):
    text: str


class ParseOneBody(BaseModel):
    url: str


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    # no-cache：保证部署新版本后浏览器能拿到最新页面（静态资源用 ?v= 做缓存穿透）
    return FileResponse(
        config.STATIC_DIR / "index.html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/healthz")
async def healthz() -> dict:
    cookie = config.load_cookie()
    path = config.COOKIE_FILE
    exists = path.exists()
    return {
        "ok": True,
        "upstream": config.UPSTREAM_API or "",
        "parser": "upstream" if config.UPSTREAM_API else "embedded-crawler",
        "http_client": "curl_cffi" if parser._HAS_CFFI else "httpx",
        "cookie_length": len(cookie),
        "cookie_file_exists": exists,
        "cookie_is_file": path.is_file() if exists else False,
    }


def _item_to_json(item: parser.ParseItem) -> dict:
    if not item.ok:
        return {"ok": False, "url": item.url, "error": item.error}
    video = item.video
    payload = {
        "ok": True,
        "url": item.url,
        "aweme_id": video.aweme_id,
        "title": video.title,
        "author": video.author,
        "date": video.date,
        "type": video.kind,
        "cover_url": f"/api/cover?id={video.aweme_id}",
    }
    if video.kind == "image":
        images = video.image_urls or []
        payload["image_count"] = len(images)
        payload["images"] = [
            {"index": i, "url": f"/api/image?id={video.aweme_id}&i={i}"}
            for i in range(len(images))
        ]
    else:
        payload["download_url"] = f"/api/download?id={video.aweme_id}"
    return payload


@app.post("/api/parse", dependencies=[Depends(auth.require_auth)])
async def api_parse(body: ParseBody) -> StreamingResponse:
    try:
        urls = parser.extract_urls(body.text)
    except parser.ParseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    total = len(urls)

    async def stream():
        yield json.dumps({"event": "start", "total": total}, ensure_ascii=False) + "\n"
        async for index, item in parser.iter_parse_urls(urls):
            data = _item_to_json(item)
            data["event"] = "item"
            data["index"] = index
            data["total"] = total
            yield json.dumps(data, ensure_ascii=False) + "\n"
        yield json.dumps({"event": "done", "total": total}, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/parse_one", dependencies=[Depends(auth.require_auth)])
async def api_parse_one(body: ParseOneBody) -> dict:
    try:
        item = await parser.parse_single(body.url)
    except parser.ParseError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc))
    return _item_to_json(item)


def _get_video_or_400(aweme_id: str) -> parser.ParsedVideo:
    video = parser.get_cached(aweme_id)
    if not video:
        raise HTTPException(status_code=400, detail="解析结果已过期，请重新解析")
    return video


@app.get("/api/cover", dependencies=[Depends(auth.require_auth)])
async def api_cover(id: str = Query(...)) -> Response:
    video = _get_video_or_400(id)
    cover = video.cover
    if not cover and video.image_urls:
        cover = video.image_urls[0]
    if not cover:
        raise HTTPException(status_code=404, detail="该作品没有封面")
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=config.HTTP_TIMEOUT
        ) as client:
            resp = await client.get(
                cover, headers={"User-Agent": parser.MOBILE_UA, "Referer": "https://www.douyin.com/"}
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("封面拉取失败: %s", exc)
        raise HTTPException(status_code=502, detail="封面拉取失败，请重试")
    return Response(
        content=resp.content,
        media_type=resp.headers.get("content-type", "image/jpeg"),
        headers={"Cache-Control": "private, max-age=600"},
    )


def _safe_part(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", text).strip().strip(".").strip()


def _build_filename(video: parser.ParsedVideo, ext: str = ".mp4", extra: str = "") -> str:
    author = _safe_part(video.author) or "未知作者"
    desc = _safe_part(video.title)[:10] or ("图文" if video.kind == "image" else "视频")
    date = video.date or time.strftime("%y-%m-%d")
    mid = f"{author}-{desc}-{date}"
    if extra:
        mid = f"{mid}-{extra}"
    return mid + ext


def _guess_image_ext(content_type: str, url: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/heic": ".heic",
    }
    if content_type:
        for key, ext in mapping.items():
            if key in content_type.lower():
                return ext
    path = url.split("?")[0].lower()
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ".jpg"


@app.get("/api/image", dependencies=[Depends(auth.require_auth)])
async def api_image(
    id: str = Query(...),
    i: int = Query(0),
    preview: bool = Query(False),
) -> Response:
    video = _get_video_or_400(id)
    images = video.image_urls or []
    if i < 0 or i >= len(images):
        raise HTTPException(status_code=404, detail="没有这张图片")
    source = images[i]
    try:
        async with httpx.AsyncClient(
            follow_redirects=True, timeout=config.HTTP_TIMEOUT
        ) as client:
            resp = await client.get(
                source, headers={"User-Agent": parser.MOBILE_UA, "Referer": "https://www.douyin.com/"}
            )
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("图片拉取失败: %s", exc)
        raise HTTPException(status_code=502, detail="图片拉取失败，请重试")
    media = resp.headers.get("content-type", "image/jpeg").split(";")[0]
    ext = _guess_image_ext(media, source)
    filename = quote(_build_filename(video, ext, f"{i + 1:02d}"))
    disposition = "inline" if preview else "attachment"
    return Response(
        content=resp.content,
        media_type=media or "image/jpeg",
        headers={
            "Content-Disposition": f"{disposition}; filename=\"image{ext}\"; filename*=UTF-8''{filename}",
            "Cache-Control": "private, max-age=600",
        },
    )


@app.get("/api/download", dependencies=[Depends(auth.require_auth)])
async def api_download(id: str = Query(...)) -> StreamingResponse:
    video = _get_video_or_400(id)
    if video.kind == "image":
        raise HTTPException(status_code=400, detail="这是图文作品，请使用页面上的图片下载")
    timeout = httpx.Timeout(config.DOWNLOAD_TIMEOUT, connect=config.HTTP_TIMEOUT)
    client = httpx.AsyncClient(follow_redirects=True, timeout=timeout)
    headers_in = {"User-Agent": parser.MOBILE_UA, "Referer": "https://www.douyin.com/"}
    source = video.video_url
    try:
        request = client.build_request("GET", source, headers=headers_in)
        upstream = await client.send(request, stream=True)
        if upstream.status_code >= 400 and config.UPSTREAM_API and video.source_url:
            await upstream.aclose()
            logger.warning("直链下载 %s，改走上游 /api/download", upstream.status_code)
            request = client.build_request(
                "GET",
                f"{config.UPSTREAM_API}/api/download",
                params={"url": video.source_url, "prefix": "false", "with_watermark": "false"},
            )
            upstream = await client.send(request, stream=True)
        if upstream.status_code >= 400:
            await upstream.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail=f"视频源返回错误（{upstream.status_code}），请重新解析")
    except httpx.HTTPError as exc:
        await client.aclose()
        logger.warning("视频拉取失败: %s", exc)
        raise HTTPException(status_code=502, detail="视频拉取失败，请重试或重新解析")

    filename = quote(_build_filename(video))
    headers = {
        "Content-Disposition": f"attachment; filename=\"video.mp4\"; filename*=UTF-8''{filename}",
        "Content-Type": "video/mp4",
    }
    if upstream.headers.get("content-length"):
        headers["Content-Length"] = upstream.headers["content-length"]

    async def streamer():
        try:
            async for chunk in upstream.aiter_bytes(64 * 1024):
                yield chunk
        except httpx.HTTPError as exc:
            logger.warning("视频流中断: %s", exc)
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(streamer(), headers=headers)
