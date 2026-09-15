"""抖音分享链接最小解析器（支持批量）。

主路径：Web API `aweme/detail`（httpx + 真 msToken + cookie UIFID + a_bogus），
从 `video.bit_rate` 按分辨率优先选出最高档。
备路径：分享页 / 详情页 HTML（curl_cffi），只保证能解析，标记为非最高档。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import unquote

import httpx

from . import config

try:
    from curl_cffi.requests import AsyncSession as CffiAsyncSession

    _HAS_CFFI = True
except ImportError:  # 本机 Python 3.14 可能没有 wheel，容器内 3.12 会装上
    CffiAsyncSession = None
    _HAS_CFFI = False

logger = logging.getLogger("douyin-dl.parser")
if _HAS_CFFI:
    logger.info("HTTP 客户端: curl_cffi（模拟 Chrome TLS）")
else:
    logger.info("HTTP 客户端: httpx（未安装 curl_cffi）")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_URL_RE = re.compile(r"https?://[^\s\"'<>]*douyin\.com/[^\s\"'<>，。！；）】]*")
_BARE_SHORT_RE = re.compile(r"v\.douyin\.com/[A-Za-z0-9]+/?")
_AWEME_RE = re.compile(r"/(?:video|note)/(\d{6,})")
_MODAL_RE = re.compile(r"[?&]modal_id=(\d{6,})")
_ROUTER_DATA_RE = re.compile(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", re.DOTALL)
_RENDER_DATA_RE = re.compile(
    r'<script id="RENDER_DATA" type="application/json">(.*?)</script>', re.DOTALL
)

MAX_BATCH = 30
MAX_CONCURRENCY = 2
# 批量时每条请求前的随机间隔，降低触发抖音风控的概率
BATCH_DELAY_RANGE = (0.3, 1.0)
# 页面已返回但无作品数据（疑似风控验证页）时的自动重试间隔
RETRY_DELAY = 2.0

COOKIE_HINT = "Cookie 失效，请更新 NAS 上的 cookie.txt 后重启容器"
RISK_OR_COOKIE_HINT = (
    "抖音未返回作品数据（请求较频繁触发风控，或 Cookie 失效）。"
    "请稍后点「重试」；若多次重试仍失败，" + COOKIE_HINT
)
WEBAPI_403_HINT = (
    "抖音风控拦截了最高清晰度接口（HTTP 403），页面档也未能解析。"
    "请稍后重试；若多次失败请更新 cookie.txt 后重启容器"
)
HTML_ONLY_HINT = "未能取到最高清晰度，当前为页面档（样本约 2MB）"


class ParseError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


IMAGE_AWEME_TYPES = {2, 68, 150}


@dataclass
class ParsedVideo:
    aweme_id: str
    title: str
    cover: str
    video_url: str
    author: str = ""
    date: str = ""
    source_url: str = ""
    kind: str = "video"  # video | image
    image_urls: list[str] | None = None
    quality_source: str = ""  # web-api | html | upstream
    width: int = 0
    height: int = 0
    gear_name: str = ""
    data_size: int | None = None
    bit_rate: int | None = None


@dataclass
class PlayPick:
    url: str
    gear_name: str = ""
    width: int = 0
    height: int = 0
    bit_rate: int = 0
    data_size: int | None = None


@dataclass
class ParseItem:
    url: str
    video: ParsedVideo | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.video is not None


_cache: dict[str, tuple[float, ParsedVideo]] = {}


def get_cached(aweme_id: str) -> ParsedVideo | None:
    item = _cache.get(aweme_id)
    if not item:
        return None
    ts, video = item
    if time.time() - ts > config.CACHE_TTL_SECONDS:
        _cache.pop(aweme_id, None)
        return None
    return video


def _put_cache(video: ParsedVideo) -> None:
    _cache[video.aweme_id] = (time.time(), video)


def extract_urls(text: str) -> list[str]:
    """从粘贴文案中抽出全部抖音链接，去重保序。"""
    urls = list(_URL_RE.findall(text))
    for bare in _BARE_SHORT_RE.findall(text):
        if not any(bare in u for u in urls):
            urls.append("https://" + bare)
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        key = url.rstrip("/")
        if key not in seen:
            seen.add(key)
            result.append(url)
    if not result:
        raise ParseError("无法识别抖音链接，请粘贴包含链接的分享文案")
    if len(result) > MAX_BATCH:
        raise ParseError(f"一次最多解析 {MAX_BATCH} 条链接，请分批粘贴")
    return result


def _aweme_id_from(url: str) -> str | None:
    match = _AWEME_RE.search(url) or _MODAL_RE.search(url)
    return match.group(1) if match else None


def _headers(
    ua: str,
    cookie: str,
    *,
    referer: str = "https://www.douyin.com/",
    with_cookie: bool = True,
) -> dict[str, str]:
    headers = {
        "Referer": referer,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    # curl_cffi impersonate 会带匹配的 UA；再覆盖会破坏 TLS/JA3 一致性
    if not _HAS_CFFI:
        headers["User-Agent"] = ua
    if with_cookie and cookie:
        headers["Cookie"] = cookie
    return headers


def _page_hint(html: str) -> str:
    """失败时从 HTML 抽出标题/风控关键字，便于对照 NAS 日志。"""
    match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", match.group(1)).strip()[:80] if match else ""
    flags = [kw for kw in ("captcha", "verify", "login", "验证", "登录", "空空如也") if kw in html]
    return f"title={title!r} flags={flags}"


def _make_http_client(timeout: float, impersonate: str | None = None):
    """优先 curl_cffi 模拟浏览器 TLS（Linux 容器必须）；本机可回落到 httpx。"""
    if _HAS_CFFI:
        from crawlers.douyin.web.web_crawler import active_profile

        names: list[str] = []
        if impersonate:
            names.append(impersonate)
        else:
            profile = active_profile(True)
            names.append(profile.impersonate or "chrome131")
        seen: list[str] = []
        for name in names:
            if name and name not in seen:
                seen.append(name)
        last_error: Exception | None = None
        for name in seen:
            kwargs = {
                "impersonate": name,
                "timeout": timeout,
                "allow_redirects": True,
                "max_clients": 8,
            }
            try:
                from curl_cffi import CurlOpt

                kwargs["curl_options"] = {CurlOpt.IPRESOLVE: 1}
            except Exception:
                pass
            try:
                session = CffiAsyncSession(**kwargs)
                logger.info("curl_cffi 会话 impersonate=%s", name)
                return session
            except TypeError:
                kwargs.pop("curl_options", None)
                kwargs.pop("max_clients", None)
                try:
                    session = CffiAsyncSession(**kwargs)
                    logger.info("curl_cffi 会话 impersonate=%s", name)
                    return session
                except Exception as exc:
                    last_error = exc
                    logger.warning("impersonate=%s 不可用: %s", name, exc)
            except Exception as exc:
                last_error = exc
                logger.warning("impersonate=%s 不可用: %s", name, exc)
        raise RuntimeError(f"无法创建 curl_cffi 会话: {last_error}")
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout),
        transport=transport,
    )


def _make_web_api_client(timeout: float):
    """详情接口不用 curl_cffi。Chrome TLS 会打开 Argus，接着要浏览器 Signature。"""
    transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
    logger.info("Web API 会话 httpx")
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout),
        transport=transport,
    )


def _make_html_client(timeout: float):
    if not _HAS_CFFI:
        return _make_http_client(timeout)
    from crawlers.douyin.web.web_crawler import html_impersonate_names

    last_error: Exception | None = None
    for name in html_impersonate_names():
        try:
            return _make_http_client(timeout, impersonate=name)
        except Exception as exc:
            last_error = exc
            logger.warning("HTML impersonate=%s 失败: %s", name, exc)
    if last_error:
        logger.warning("HTML 移动端 impersonate 全部失败，回落 chrome131: %s", last_error)
    return _make_http_client(timeout, impersonate="chrome131")


async def _close_http_client(client) -> None:
    close = getattr(client, "aclose", None) or getattr(client, "close", None)
    if not close:
        return
    result = close()
    if asyncio.iscoroutine(result):
        await result


def _first_url(node) -> str | None:
    """从抖音多变的 JSON 结构里挖出第一个可用 URL。"""
    if isinstance(node, str):
        if node.startswith("http"):
            return node
        if node.startswith("//"):
            return "https:" + node
        return None
    if isinstance(node, dict):
        for key in ("url_list", "urlList", "urls"):
            urls = node.get(key)
            if isinstance(urls, list) and urls:
                found = _first_url(urls[0])
                if found:
                    return found
        for key in ("src", "url", "uri"):
            if key in node:
                found = _first_url(node[key])
                if found:
                    return found
    if isinstance(node, list) and node:
        return _first_url(node[0])
    return None


def _bump_ratio(url: str) -> str:
    """把播放地址的 ratio 参数提到 1080p（源视频没有该档位时抖音会自动给最高可用档）。"""
    if "ratio=" in url:
        return re.sub(r"ratio=[^&]+", "ratio=1080p", url)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}ratio=1080p"


def _int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _entry_dims(entry: dict, play_addr: dict | None) -> tuple[int, int]:
    addr = play_addr if isinstance(play_addr, dict) else {}
    width = _int_or_zero(entry.get("width") or addr.get("width"))
    height = _int_or_zero(entry.get("height") or addr.get("height"))
    return width, height


def _pick_bit_rate(video: dict) -> PlayPick | None:
    """有 bit_rate 时：分辨率优先，码率其次，体积再次。不对选出的 URL 调 ratio。"""
    bit_rates = video.get("bit_rate") or video.get("bitRate")
    if not isinstance(bit_rates, list):
        return None
    best: PlayPick | None = None
    best_key = None
    for entry in bit_rates:
        if not isinstance(entry, dict):
            continue
        play_addr = entry.get("play_addr") or entry.get("playAddr")
        url = _first_url(play_addr)
        if not url:
            continue
        width, height = _entry_dims(entry, play_addr if isinstance(play_addr, dict) else None)
        bps = _int_or_zero(entry.get("bit_rate") or entry.get("bitRate"))
        size_raw = entry.get("data_size") or entry.get("dataSize")
        if size_raw in (None, "") and isinstance(play_addr, dict):
            size_raw = play_addr.get("data_size") or play_addr.get("dataSize")
        size = _int_or_zero(size_raw) if size_raw not in (None, "") else None
        key = (width * height, bps, size or 0)
        if best_key is None or key > best_key:
            best_key = key
            best = PlayPick(
                url=url.replace("playwm", "play"),
                gear_name=str(entry.get("gear_name") or entry.get("gearName") or ""),
                width=width,
                height=height,
                bit_rate=bps,
                data_size=size,
            )
    return best


def _html_play_url(video: dict) -> PlayPick | None:
    """HTML 兜底：只用页面 play_addr，允许 bump ratio，明确不是 4K。"""
    url = None
    play_addr = video.get("play_addr")
    width = height = 0
    if isinstance(play_addr, dict):
        url = _first_url(play_addr.get("url_list"))
        width = _int_or_zero(play_addr.get("width"))
        height = _int_or_zero(play_addr.get("height"))
    if not url:
        url = _first_url(video.get("playAddr")) or _first_url(video.get("playApi"))
    if not url:
        return None
    return PlayPick(
        url=_bump_ratio(url.replace("playwm", "play")),
        gear_name="html_play_addr",
        width=width,
        height=height,
    )


def _pick_play_url(video: dict) -> str | None:
    # 兼容旧调用（RENDER_DATA 递归查找）：有码率列表则取最高分辨率档，否则 bump 页面地址
    picked = _pick_bit_rate(video) or _html_play_url(video)
    return picked.url if picked else None


def _pick_author(item: dict) -> str:
    author = item.get("author")
    if isinstance(author, dict):
        return (author.get("nickname") or author.get("nickName") or "").strip()
    return ""


def _pick_date(item: dict) -> str:
    ts = item.get("create_time") or item.get("createTime")
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return ""
    if ts > 1_000_000_000_000:  # 毫秒时间戳
        ts //= 1000
    try:
        return time.strftime("%y-%m-%d", time.localtime(ts))
    except (OSError, OverflowError):
        return ""


def _is_watermark_url(url: str) -> bool:
    return "tplv-dy-water" in url or "watermark=1" in url


def _image_url_from(img) -> str | None:
    """单张图的无水印地址：url_list / display_image，不用 download_url_list（带水印）。"""
    if isinstance(img, str):
        return img if img.startswith("http") and not _is_watermark_url(img) else None
    if not isinstance(img, dict):
        return None
    candidates = [img.get("url_list"), img.get("urlList")]
    for nested_key in ("display_image", "largest"):
        nested = img.get(nested_key)
        if isinstance(nested, dict):
            candidates.append(nested.get("url_list"))
    for item in candidates:
        found = _first_url(item)
        if found and not _is_watermark_url(found):
            return found
    return None


def _pick_images(node: dict) -> list[str]:
    """取出图文作品的全部无水印原图（与 evil0ctal 一样用 images[].url_list[0]）。"""
    urls: list[str] = []
    images = node.get("images")
    if isinstance(images, list):
        for img in images:
            found = _image_url_from(img)
            if found:
                urls.append(found)
    image_data = node.get("image_data") if isinstance(node.get("image_data"), dict) else {}
    if not urls:
        for found in image_data.get("no_watermark_image_list") or []:
            if isinstance(found, str) and found.startswith("http") and not _is_watermark_url(found):
                urls.append(found)
    return urls


def _apply_quality(video: ParsedVideo, pick: PlayPick | None, quality_source: str) -> ParsedVideo:
    video.quality_source = quality_source
    if pick:
        video.video_url = pick.url
        video.width = pick.width
        video.height = pick.height
        video.gear_name = pick.gear_name
        video.data_size = pick.data_size
        video.bit_rate = pick.bit_rate or None
        extra = ""
        if pick.data_size:
            extra = f" data_size={pick.data_size}"
        logger.info(
            "gear_name=%s %sx%s bps=%s%s quality_source=%s",
            pick.gear_name or "-",
            pick.width,
            pick.height,
            pick.bit_rate,
            extra,
            quality_source,
        )
    return video


def _build_parsed(
    detail: dict,
    aweme_id: str,
    source_url: str,
    quality_source: str = "web-api",
    play: PlayPick | None = None,
) -> ParsedVideo:
    images = _pick_images(detail)
    aweme_type = detail.get("aweme_type")
    video_node = detail.get("video") or {}
    if play is None:
        if quality_source == "html":
            play = _html_play_url(video_node)
        else:
            play = _pick_bit_rate(video_node) or _html_play_url(video_node)
        if play is None:
            uri = (video_node.get("play_addr") or {}).get("uri") if isinstance(video_node.get("play_addr"), dict) else None
            if uri:
                raw = f"https://aweme.snssdk.com/aweme/v1/play/?video_id={uri}&ratio=1080p&line=0"
                if quality_source == "html":
                    play = PlayPick(url=_bump_ratio(raw), gear_name="html_uri")
                else:
                    play = PlayPick(url=raw)
    play_url = play.url if play else ""
    is_image = bool(images) and (
        aweme_type in IMAGE_AWEME_TYPES or aweme_type is None or not play_url
    )
    if is_image:
        cover = (
            images[0]
            or _first_url(video_node.get("cover"))
            or ""
        )
        video = ParsedVideo(
            aweme_id=str(detail.get("aweme_id") or aweme_id),
            title=(detail.get("desc") or "").strip() or f"抖音图文_{aweme_id}",
            cover=cover,
            video_url="",
            author=_pick_author(detail),
            date=_pick_date(detail),
            source_url=source_url,
            kind="image",
            image_urls=images,
            quality_source=quality_source,
        )
        _put_cache(video)
        return video
    if not play_url:
        raise ParseError("该作品未找到无水印视频或图片")
    cover = (
        _first_url(video_node.get("cover"))
        or _first_url(video_node.get("origin_cover"))
        or ""
    )
    video = ParsedVideo(
        aweme_id=str(detail.get("aweme_id") or aweme_id),
        title=(detail.get("desc") or "").strip() or f"抖音视频_{aweme_id}",
        cover=cover,
        video_url=play_url,
        author=_pick_author(detail),
        date=_pick_date(detail),
        source_url=source_url,
        kind="video",
    )
    _apply_quality(video, play, quality_source)
    _put_cache(video)
    return video


def _from_router_data(html: str, aweme_id: str) -> ParsedVideo | None:
    match = _ROUTER_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    loader = data.get("loaderData") or {}
    for page in loader.values():
        if not isinstance(page, dict):
            continue
        info = page.get("videoInfoRes") or {}
        items = info.get("item_list") or []
        if items:
            return _build_parsed(items[0], aweme_id, "", quality_source="html")
    return None


def _find_aweme(node) -> dict | None:
    """在 RENDER_DATA 里递归找带可播放 video 的作品对象。"""
    if isinstance(node, dict):
        video = node.get("video")
        if isinstance(video, dict) and _pick_play_url(video):
            return node
        for value in node.values():
            found = _find_aweme(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_aweme(value)
            if found:
                return found
    return None


def _from_render_data(html: str, aweme_id: str) -> ParsedVideo | None:
    match = _RENDER_DATA_RE.search(html)
    if not match:
        return None
    try:
        data = json.loads(unquote(match.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None
    item = _find_aweme(data)
    if not item:
        return None
    return _build_parsed(item, aweme_id, "", quality_source="html")


async def _resolve_aweme_id(client, url: str, cookie: str) -> str:
    direct = _aweme_id_from(url)
    if direct:
        return direct
    try:
        resp = await client.get(url, headers=_headers(MOBILE_UA, cookie))
    except Exception as exc:
        logger.warning("短链打开失败: %s (%s)", url, exc)
        raise ParseError("短链接打开失败，请稍后重试", 502)
    aweme_id = _aweme_id_from(str(resp.url)) or _aweme_id_from(resp.text[:200000])
    if not aweme_id:
        logger.warning("短链未解析出作品 ID: final=%s %s", resp.url, _page_hint(resp.text))
        raise ParseError("无法从链接中获取作品 ID", 502)
    return aweme_id


async def _fetch_detail_once(client, aweme_id: str, cookie: str) -> tuple[ParsedVideo | None, bool]:
    """返回 (解析结果, 页面是否成功返回)。细节进容器日志。"""
    page_ok = False
    # 主路径：分享页 _ROUTER_DATA
    try:
        resp = await client.get(
            f"https://www.iesdouyin.com/share/video/{aweme_id}",
            headers=_headers(
                MOBILE_UA,
                cookie,
                referer="https://www.iesdouyin.com/",
                with_cookie=False,
            ),
        )
        page_ok = True
        video = _from_router_data(resp.text, aweme_id)
        if video:
            return video, page_ok
        logger.warning(
            "分享页已返回但无作品数据: status=%s len=%d router_data=%s final_url=%s %s",
            resp.status_code,
            len(resp.text),
            bool(_ROUTER_DATA_RE.search(resp.text)),
            resp.url,
            _page_hint(resp.text),
        )
    except Exception as exc:
        logger.warning("分享页请求失败: %s", exc)
    # 备用路径：网页版详情页 RENDER_DATA
    try:
        resp = await client.get(
            f"https://www.douyin.com/video/{aweme_id}",
            headers=_headers(DESKTOP_UA, cookie),
        )
        page_ok = True
        video = _from_render_data(resp.text, aweme_id)
        if video:
            return video, page_ok
        logger.warning(
            "详情页已返回但无作品数据: status=%s len=%d render_data=%s %s",
            resp.status_code,
            len(resp.text),
            bool(_RENDER_DATA_RE.search(resp.text)),
            _page_hint(resp.text),
        )
    except Exception as exc:
        logger.warning("详情页请求失败: %s", exc)
    return None, page_ok


async def _fetch_detail(client, aweme_id: str, cookie: str) -> ParsedVideo:
    """成功返回 ParsedVideo；失败抛出带具体原因的 ParseError。

    页面返回但无数据（疑似风控验证页）时，隔 2 秒自动再试一次；仍失败则抛出。
    """
    video = None
    page_ok = False
    for attempt in range(2):
        if attempt:
            logger.info("解析自动重试: aweme_id=%s", aweme_id)
            await asyncio.sleep(RETRY_DELAY)
        video, page_ok = await _fetch_detail_once(client, aweme_id, cookie)
        if video:
            return video
    if not page_ok:
        raise ParseError("无法连接抖音服务器，请检查 NAS 网络后重试", 502)
    if not cookie:
        raise ParseError("未配置 Cookie：请在 NAS 上放入 cookie.txt 后重启容器", 502)
    raise ParseError(RISK_OR_COOKIE_HINT, 502)


async def _parse_url(client, url: str, cookie: str) -> ParsedVideo:
    aweme_id = await _resolve_aweme_id(client, url, cookie)
    cached = get_cached(aweme_id)
    if cached:
        return cached
    video = await _fetch_detail(client, aweme_id, cookie)
    video.source_url = url
    _put_cache(video)
    return video


def _hybrid_error(payload: dict) -> str | None:
    code = payload.get("code")
    if code in (None, 200, "200", 0, "0"):
        return None
    return str(payload.get("message") or payload.get("msg") or payload.get("detail") or f"上游返回 {code}")


def _from_hybrid(payload: dict, source_url: str) -> ParsedVideo:
    err = _hybrid_error(payload)
    if err:
        raise ParseError(f"上游解析失败：{err}", 502)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ParseError("上游未返回作品数据", 502)
    video_data = data.get("video_data") if isinstance(data.get("video_data"), dict) else {}
    play = (
        video_data.get("nwm_video_url_HQ")
        or video_data.get("nwm_video_url")
        or _pick_play_url(data.get("video") or {})
    )
    image_data = data.get("image_data") if isinstance(data.get("image_data"), dict) else {}
    hybrid_images = [
        u for u in (image_data.get("no_watermark_image_list") or []) if isinstance(u, str)
    ]
    merged = dict(data)
    if hybrid_images and "images" not in merged:
        merged["image_data"] = image_data
    if not play and not hybrid_images and not _pick_images(merged):
        raise ParseError("上游未返回无水印视频或图片")
    aweme_id = str(data.get("aweme_id") or data.get("video_id") or _aweme_id_from(source_url) or "")
    if not aweme_id:
        raise ParseError("上游未返回作品 ID", 502)
    cover_data = data.get("cover_data") if isinstance(data.get("cover_data"), dict) else {}
    if not merged.get("video"):
        merged["video"] = {"cover": cover_data.get("cover"), "play_addr": {"url_list": [play] if play else []}}
    elif play:
        merged.setdefault("video", {})
    video = _build_parsed(merged, aweme_id, source_url, quality_source="upstream")
    if play and video.kind == "video":
        video.video_url = play
        video.quality_source = "upstream"
    return video


async def _parse_via_upstream(client: httpx.AsyncClient, url: str) -> ParsedVideo:
    aweme_id = _aweme_id_from(url)
    if aweme_id:
        cached = get_cached(aweme_id)
        if cached:
            return cached
    try:
        resp = await client.get(
            f"{config.UPSTREAM_API}/api/hybrid/video_data",
            params={"url": url, "minimal": "false"},
        )
    except httpx.HTTPError as exc:
        logger.warning("上游解析请求失败 %s: %s", url, exc)
        raise ParseError("无法连接已部署的解析服务（evil0ctal），请检查 UPSTREAM_API", 502)
    if resp.status_code >= 400:
        logger.warning("上游 HTTP %s: %s", resp.status_code, resp.text[:300])
        raise ParseError(f"上游解析服务返回 {resp.status_code}", 502)
    try:
        payload = resp.json()
    except ValueError:
        raise ParseError("上游返回了非 JSON（可能是登录页或 PyWebIO 页面）", 502)
    if not isinstance(payload, dict):
        raise ParseError("上游返回格式无法识别", 502)
    return _from_hybrid(payload, url)


async def _parse_via_web_api(url: str, cookie: str, aweme_id: str) -> ParsedVideo:
    from crawlers.douyin.web.web_crawler import CHROME90, WebApiError, fetch_aweme_detail

    client = _make_web_api_client(config.HTTP_TIMEOUT)
    try:
        raw = await fetch_aweme_detail(
            aweme_id, cookie, client, has_cffi=False, profile=CHROME90
        )
        detail = raw.get("aweme_detail") if isinstance(raw, dict) else None
        if not isinstance(detail, dict):
            raise WebApiError("empty", "最高清晰度接口未返回作品数据")
        pick = _pick_bit_rate(detail.get("video") or {})
        return _build_parsed(detail, aweme_id, url, quality_source="web-api", play=pick)
    except WebApiError:
        raise
    except Exception as exc:
        logger.warning("Web API 请求异常 client=httpx: %s", exc)
        raise WebApiError("http", str(exc))
    finally:
        await _close_http_client(client)


async def _parse_via_html(client, url: str, cookie: str, aweme_id: str) -> ParsedVideo:
    video = await _fetch_detail(client, aweme_id, cookie)
    video.source_url = url
    if video.kind == "video":
        video.quality_source = video.quality_source or "html"
    _put_cache(video)
    return video


async def _parse_layered(client, url: str, cookie: str) -> ParsedVideo:
    from crawlers.douyin.web.web_crawler import WebApiError

    aweme_id = await _resolve_aweme_id(client, url, cookie)
    cached = get_cached(aweme_id)
    if cached:
        cached.source_url = url
        return cached
    web_err: WebApiError | None = None
    try:
        return await _parse_via_web_api(url, cookie, aweme_id)
    except WebApiError as exc:
        web_err = exc
        logger.warning(
            "Web API 主路径失败 kind=%s status=%s: %s，进入 HTML 兜底",
            exc.kind,
            exc.status,
            exc,
        )
    except Exception as exc:
        logger.warning("Web API 主路径异常: %s，进入 HTML 兜底", exc)
        web_err = WebApiError("http", str(exc))
    try:
        video = await _parse_via_html(client, url, cookie, aweme_id)
        if video.kind == "video":
            logger.info("HTML 兜底成功 aweme_id=%s quality_source=html（非本里程碑验收档）", aweme_id)
        return video
    except ParseError as html_exc:
        if "未配置 Cookie" in str(html_exc):
            raise html_exc
        if web_err and web_err.kind == "forbidden":
            raise ParseError(WEBAPI_403_HINT, 502)
        if web_err and web_err.kind == "connect" and "无法连接" in str(html_exc):
            raise ParseError("无法连接抖音服务器，请检查 NAS 网络后重试", 502)
        raise html_exc


_crawler = None


def _inject_cookie(cookie: str) -> None:
    from crawlers.douyin.web import utils as web_utils
    from crawlers.douyin.web import web_crawler as web_mod

    if not cookie:
        return
    web_mod.config["TokenManager"]["douyin"]["headers"]["Cookie"] = cookie
    web_utils.config["TokenManager"]["douyin"]["headers"]["Cookie"] = cookie


def parse_health() -> dict:
    from crawlers.douyin.web.web_crawler import cookie_value, html_impersonate_names

    cookie = config.load_cookie()
    if config.UPSTREAM_API:
        parse_mode = "upstream"
        parser_name = "upstream"
    else:
        parse_mode = "web-api+html-fallback"
        parser_name = "embedded"
    return {
        "parser": parser_name,
        "parse_mode": parse_mode,
        "http_client": "curl_cffi" if _HAS_CFFI else "httpx",
        "web_api_client": "httpx",
        "impersonate": "",
        "impersonate_chain": "httpx",
        "html_impersonate": ",".join(html_impersonate_names()) if _HAS_CFFI else "",
        "cookie_has_sessionid": bool(cookie_value(cookie, "sessionid")),
        "cookie_has_mstoken": bool(cookie_value(cookie, "msToken")),
        "cookie_has_uifid": bool(cookie_value(cookie, "UIFID") or cookie_value(cookie, "UIFID_TEMP")),
        "cookie_has_secsdk_key": bool(cookie_value(cookie, "__security_mc_1_s_sdk_sign_data_key_web_protect")),
    }


def _get_crawler():
    global _crawler
    if _crawler is None:
        _inject_cookie(config.load_cookie())
        from crawlers.douyin.web.web_crawler import DouyinWebCrawler

        _crawler = DouyinWebCrawler()
    return _crawler


async def iter_parse_urls(urls: list[str]):
    """逐条产出 (index, ParseItem)，完成一条 yield 一条。"""
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)
    queue: asyncio.Queue[tuple[int, ParseItem]] = asyncio.Queue()
    use_upstream = bool(config.UPSTREAM_API)
    cookie = config.load_cookie()
    client = None if use_upstream else _make_html_client(config.HTTP_TIMEOUT)

    async def run_one(url: str) -> ParsedVideo:
        if use_upstream:
            timeout = httpx.Timeout(config.UPSTREAM_TIMEOUT)
            async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as upstream:
                return await _parse_via_upstream(upstream, url)
        return await _parse_layered(client, url, cookie)

    async def worker(index: int, url: str) -> None:
        async with semaphore:
            await asyncio.sleep(random.uniform(*BATCH_DELAY_RANGE))
            try:
                item = ParseItem(url=url, video=await run_one(url))
            except ParseError as exc:
                logger.info("解析失败 %s: %s", url, exc)
                item = ParseItem(url=url, error=str(exc))
            await queue.put((index, item))

    if use_upstream:
        logger.info("解析走 HTTP 上游: %s", config.UPSTREAM_API)
    else:
        from crawlers.douyin.web.web_crawler import html_impersonate_names

        logger.info(
            "解析走 Web API 主路径 + HTML 兜底 web_api_client=httpx html_client=%s html_impersonate=%s",
            "curl_cffi" if _HAS_CFFI else "httpx",
            ",".join(html_impersonate_names()) if _HAS_CFFI else "httpx",
        )
        _inject_cookie(cookie)

    tasks = [asyncio.create_task(worker(i, u)) for i, u in enumerate(urls)]
    try:
        for _ in urls:
            yield await queue.get()
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)
        if client is not None:
            await _close_http_client(client)


async def parse_urls(urls: list[str]) -> list[ParseItem]:
    """批量解析，结果按原始顺序返回。"""
    results: list[ParseItem | None] = [None] * len(urls)
    async for index, item in iter_parse_urls(urls):
        results[index] = item
    return results  # type: ignore[return-value]


async def parse_text(text: str) -> list[ParseItem]:
    return await parse_urls(extract_urls(text))


async def parse_single(url: str) -> ParseItem:
    """重试单条（前端传入失败条目的原始 URL）。"""
    urls = extract_urls(url)
    return (await parse_urls(urls[:1]))[0]
