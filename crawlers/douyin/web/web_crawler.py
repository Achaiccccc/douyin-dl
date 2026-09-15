# ==============================================================================
# Copyright (C) 2021 Evil0ctal
#
# This file is part of the Douyin_TikTok_Download_API project.
#
# This project is licensed under the Apache License 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at:
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
# 　　　　 　　  ＿＿
# 　　　 　　 ／＞　　フ
# 　　　 　　| 　_　 _ l
# 　 　　 　／` ミ＿xノ
# 　　 　 /　　　 　 |       Feed me Stars ⭐ ️
# 　　　 /　 ヽ　　 ﾉ
# 　 　 │　　|　|　|
# 　／￣|　　 |　|　|
# 　| (￣ヽ＿_ヽ_)__)
# 　＼二つ
# ==============================================================================
#
# Contributor Link:
# - https://github.com/Evil0ctal
# - https://github.com/Johnserf-Seed
#
# ==============================================================================


import asyncio  # 异步I/O
import hashlib
import logging
import os  # 系统操作
import re
import time  # 时间操作
from dataclasses import dataclass
from urllib.parse import urlencode, quote  # URL编码
import yaml  # 配置文件

# 基础爬虫客户端和抖音API端点
from crawlers.base_crawler import BaseCrawler
from crawlers.douyin.web.endpoints import DouyinAPIEndpoints
# 抖音接口数据请求模型
from crawlers.douyin.web.models import (
    BaseRequestModel, LiveRoomRanking, PostComments,
    PostCommentsReply, PostDetail,
    UserProfile, UserCollection, UserLike, UserLive,
    UserLive2, UserMix, UserPost
)
# 抖音应用的工具类
from crawlers.douyin.web.utils import (AwemeIdFetcher,  # Aweme ID获取
                                       BogusManager,  # XBogus管理
                                       SecUserIdFetcher,  # 安全用户ID获取
                                       TokenManager,  # 令牌管理
                                       VerifyFpManager,  # 验证管理
                                       WebCastIdFetcher,  # 直播ID获取
                                       extract_valid_urls  # URL提取
                                       )

# 配置文件路径
path = os.path.abspath(os.path.dirname(__file__))

# 读取配置文件
with open(f"{path}/config.yaml", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

logger = logging.getLogger("douyin-dl.web_api")
WEB_API_ATTEMPTS = 3  # 首次 + 重新签名最多 2 次


@dataclass(frozen=True)
class BrowserProfile:
    impersonate: str
    ua: str
    browser_version: str
    engine_version: str
    browser_name: str = "Chrome"
    engine_name: str = "Blink"


CHROME131 = BrowserProfile(
    impersonate="chrome131",
    ua=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    browser_version="131.0.0.0",
    engine_version="131.0.0.0",
)
CHROME124 = BrowserProfile(
    impersonate="chrome124",
    ua=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    browser_version="124.0.0.0",
    engine_version="124.0.0.0",
)
CHROME90 = BrowserProfile(
    impersonate="",
    ua=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
    ),
    browser_version="90.0.4430.212",
    engine_version="90.0.4430.212",
)
SAFARI184 = BrowserProfile(
    impersonate="safari184",
    ua=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.4 Safari/605.1.15"
    ),
    browser_version="18.4",
    engine_version="18.4",
    browser_name="Safari",
    engine_name="WebKit",
)
PROFILES = {
    "chrome131": CHROME131,
    "chrome124": CHROME124,
    "chrome90": CHROME90,
    "safari184": SAFARI184,
    "safari18_4": SAFARI184,
}

# 同一浏览器指纹的别名：curl_cffi 版本不同，名称可能是 safari184 / safari18_0
IMPERSONATE_ALIASES = {
    "chrome131": ["chrome131"],
    "chrome124": ["chrome124", "chrome123"],
    "safari184": ["safari184", "safari18_4", "safari18_0", "safari17_0"],
}


class WebApiError(Exception):
    def __init__(self, kind: str, message: str, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def cookie_value(cookie: str, name: str) -> str:
    if not cookie:
        return ""
    prefix = name.lower() + "="
    for part in cookie.split(";"):
        item = part.strip()
        if item.lower().startswith(prefix):
            return item.split("=", 1)[1]
    return ""


def resolve_ms_token(cookie: str) -> tuple[str, str]:
    token = cookie_value(cookie, "msToken")
    if token:
        return token, "cookie"
    try:
        token = TokenManager.gen_real_msToken()
        if token:
            return token, "generated"
    except Exception as exc:
        logger.warning("生成 msToken 失败: %s", exc)
    return "", "missing"


def active_profile(has_cffi: bool) -> BrowserProfile:
    name = os.environ.get("DOUYIN_IMPERSONATE", "").strip().lower()
    if name and name in PROFILES:
        return PROFILES[name]
    return CHROME131 if has_cffi else CHROME90


def api_profile_chain(has_cffi: bool) -> list[BrowserProfile]:
    """Web API 固定 httpx + Chrome 90。容器里 curl_cffi Chrome TLS 会触发 Argus Signature。"""
    del has_cffi
    return [CHROME90]


def impersonate_names(profile: BrowserProfile) -> list[str]:
    name = profile.impersonate
    if not name:
        return []
    aliases = IMPERSONATE_ALIASES.get(name, [name])
    seen: list[str] = []
    for item in aliases:
        if item not in seen:
            seen.append(item)
    return seen


def html_impersonate_names() -> list[str]:
    locked = os.environ.get("DOUYIN_HTML_IMPERSONATE", "").strip()
    if locked:
        return [locked]
    # 分享页必须像手机浏览器，否则 iesdouyin 会 302 到 www.douyin.com 验证页
    return [
        "safari17_2_ios",
        "safari18_0_ios",
        "safari184",
        "safari18_0",
        "safari17_0",
    ]


def _html_hint(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = re.sub(r"\s+", " ", match.group(1)).strip()[:80] if match else ""
    flags = [kw for kw in ("captcha", "verify", "login", "验证", "登录") if kw in html]
    return f"title={title!r} flags={flags}"


def _looks_html(text: str) -> bool:
    head = text.lstrip()[:32].lower()
    return head.startswith("<!doctype") or head.startswith("<html")


def _body_hint(text: str) -> str:
    compact = re.sub(r"\s+", " ", (text or "").strip())[:80]
    if _looks_html(text or ""):
        return _html_hint(text)
    return f"body_len={len(text or '')} body={compact!r}"


def resolve_uifid(cookie: str) -> tuple[str, str]:
    token = cookie_value(cookie, "UIFID")
    if token:
        return token, "cookie"
    token = cookie_value(cookie, "UIFID_TEMP")
    if token:
        return token, "cookie_temp"
    return "", "missing"


# secsdk webSign 固定盐。Argus「Signature Not Found」查的是这个 MD5，不是 a_bogus。
WEB_SIGN_SALT = "A96D855A08C0A9707F8BEF0D9A527E4E"


def apply_secsdk_web_sign(url: str, uifid: str, now: int | None = None) -> tuple[str, dict[str, str]]:
    """给 aweme/detail URL 补 timestamp + x-secsdk-web-signature。

    浏览器 secsdk 的算法是：
    MD5(f"{uifid}_{timestamp}_{salt}_{query_with_uifid_and_timestamp}")
    查询里已有 uifid 则不再重复；timestamp / signature 永远挂在末尾。
    """
    if not uifid or not url:
        return url, {}
    ts = str(int(now if now is not None else time.time()))
    if "?" in url:
        head, query = url.split("?", 1)
    else:
        head, query = url, ""
    parts = []
    for item in query.split("&"):
        if not item:
            continue
        key = item.split("=", 1)[0]
        if key in {"timestamp", "x-secsdk-web-signature"}:
            continue
        parts.append(item)
    if not any(item.startswith("uifid=") for item in parts):
        parts.append(f"uifid={uifid}")
    parts.append(f"timestamp={ts}")
    signed_query = "&".join(parts)
    payload = f"{uifid}_{ts}_{WEB_SIGN_SALT}_{signed_query}"
    signature = hashlib.md5(payload.encode("utf-8")).hexdigest()
    signed_url = f"{head}?{signed_query}&x-secsdk-web-signature={signature}"
    return signed_url, {
        "uifid": uifid,
        "x-secsdk-web-signature": signature,
        "x-secsdk-web-expire": ts,
    }


def _params_dict(aweme_id: str, profile: BrowserProfile, ms_token: str, cookie: str = "") -> dict:
    params = PostDetail(aweme_id=aweme_id)
    data = params.dict() if hasattr(params, "dict") else params.model_dump()
    data["msToken"] = ms_token
    data["browser_version"] = profile.browser_version
    data["engine_version"] = profile.engine_version
    data["browser_name"] = profile.browser_name
    data["engine_name"] = profile.engine_name
    # ArgusSecurityPlugin 查的是查询参数 uifid，不是 Cookie 里同名键本身
    uifid, _source = resolve_uifid(cookie)
    if uifid:
        data["uifid"] = uifid
    verify_fp = cookie_value(cookie, "s_v_web_id")
    if verify_fp:
        data["verifyFp"] = verify_fp
        data["fp"] = verify_fp
    return data


async def fetch_aweme_detail(
    aweme_id: str,
    cookie: str,
    client,
    has_cffi: bool,
    profile: BrowserProfile | None = None,
) -> dict:
    """请求 aweme/detail。失败重新签名最多 2 次。不要用 Chrome TLS impersonate 打此接口。"""
    profile = profile or active_profile(has_cffi)
    ms_token, ms_source = resolve_ms_token(cookie)
    if not ms_token:
        logger.warning("msToken 为空（cookie 无此字段且生成失败），禁止再写死空串之外已无值可填")
    uifid, uifid_source = resolve_uifid(cookie)
    if not uifid:
        logger.warning("uifid 为空（cookie 无 UIFID / UIFID_TEMP），Argus 会直接 403")
    headers = {
        "Referer": "https://www.douyin.com/",
        "Origin": "https://www.douyin.com",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie
    if uifid:
        headers["uifid"] = uifid
    if not has_cffi:
        headers["User-Agent"] = profile.ua

    last_status = None
    last_kind = "empty"
    for attempt in range(1, WEB_API_ATTEMPTS + 1):
        params_dict = _params_dict(aweme_id, profile, ms_token, cookie)
        a_bogus = BogusManager.ab_model_2_endpoint(params_dict, profile.ua)
        endpoint = f"{DouyinAPIEndpoints.POST_DETAIL}?{urlencode(params_dict)}&a_bogus={a_bogus}"
        req_headers = dict(headers)
        web_sign = "no"
        if uifid:
            endpoint, sign_headers = apply_secsdk_web_sign(endpoint, uifid)
            req_headers.update(sign_headers)
            web_sign = "md5"
        logger.info(
            "Web API 详情 aweme_id=%s client=%s sign_ua=%s msToken_source=%s uifid_source=%s uifid_len=%s a_bogus_len=%s uifid_header=%s web_sign=%s attempt=%s/%s",
            aweme_id,
            "curl_cffi" if has_cffi else "httpx",
            profile.ua,
            ms_source,
            uifid_source,
            len(uifid),
            len(a_bogus),
            "yes" if uifid else "no",
            web_sign,
            attempt,
            WEB_API_ATTEMPTS,
        )
        try:
            resp = await client.get(endpoint, headers=req_headers)
        except Exception as exc:
            last_kind = "connect"
            logger.warning("Web API 请求失败 client=%s attempt=%s: %s", "curl_cffi" if has_cffi else "httpx", attempt, exc)
            if attempt == WEB_API_ATTEMPTS:
                raise WebApiError("connect", "无法连接抖音服务器，请检查 NAS 网络后重试")
            continue

        last_status = getattr(resp, "status_code", None)
        text = resp.text or ""
        if last_status == 403:
            last_kind = "forbidden"
            logger.warning(
                "Web API 403 client=%s attempt=%s %s",
                "curl_cffi" if has_cffi else "httpx",
                attempt,
                _body_hint(text),
            )
            continue
        if last_status != 200:
            last_kind = "http"
            logger.warning(
                "Web API HTTP %s client=%s attempt=%s %s",
                last_status,
                "curl_cffi" if has_cffi else "httpx",
                attempt,
                _body_hint(text),
            )
            continue
        if not text.strip():
            last_kind = "empty"
            logger.warning("Web API 空包 client=%s attempt=%s", "curl_cffi" if has_cffi else "httpx", attempt)
            continue
        if _looks_html(text):
            last_kind = "forbidden"
            logger.warning(
                "Web API 返回验证页 HTML client=%s attempt=%s %s",
                "curl_cffi" if has_cffi else "httpx",
                attempt,
                _html_hint(text),
            )
            continue
        try:
            payload = resp.json()
        except Exception:
            last_kind = "empty"
            logger.warning("Web API 非 JSON client=%s attempt=%s prefix=%r", "curl_cffi" if has_cffi else "httpx", attempt, text[:80])
            continue
        if not isinstance(payload, dict):
            last_kind = "empty"
            continue
        detail = payload.get("aweme_detail")
        if not isinstance(detail, dict):
            last_kind = "empty"
            logger.warning(
                "Web API aweme_detail 为空 client=%s attempt=%s keys=%s",
                "curl_cffi" if has_cffi else "httpx",
                attempt,
                list(payload.keys())[:8],
            )
            continue
        video = detail.get("video") if isinstance(detail.get("video"), dict) else {}
        bit_rate = video.get("bit_rate")
        if not isinstance(bit_rate, list) or not bit_rate:
            raise WebApiError("no_bit_rate", "详情接口未返回清晰度列表", last_status)
        logger.info(
            "Web API 成功 aweme_id=%s client=%s bit_rate档数=%s",
            aweme_id,
            "curl_cffi" if has_cffi else "httpx",
            len(bit_rate),
        )
        return payload

    if last_kind == "forbidden":
        raise WebApiError("forbidden", "抖音风控拦截了最高清晰度接口（HTTP 403）", last_status)
    if last_kind == "connect":
        raise WebApiError("connect", "无法连接抖音服务器，请检查 NAS 网络后重试", last_status)
    raise WebApiError(last_kind, "最高清晰度接口未返回作品数据", last_status)


class DouyinWebCrawler:

    # 从配置文件中获取抖音的请求头
    async def get_douyin_headers(self):
        douyin_config = config["TokenManager"]["douyin"]
        kwargs = {
            "headers": {
                "Accept-Language": douyin_config["headers"]["Accept-Language"],
                "User-Agent": douyin_config["headers"]["User-Agent"],
                "Referer": douyin_config["headers"]["Referer"],
                "Cookie": douyin_config["headers"]["Cookie"],
            },
            "proxies": {"http://": douyin_config["proxies"]["http"], "https://": douyin_config["proxies"]["https"]},
        }
        return kwargs

    "-------------------------------------------------------handler接口列表-------------------------------------------------------"

    # 获取单个作品数据（产品解析请走 parser 传入的 curl_cffi 会话）
    async def fetch_one_video(self, aweme_id: str):
        kwargs = await self.get_douyin_headers()
        cookie = kwargs["headers"].get("Cookie") or ""
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            return await fetch_aweme_detail(aweme_id, cookie, crawler.aclient, has_cffi=False)

    # 获取用户发布作品数据
    async def fetch_user_post_videos(self, sec_user_id: str, max_cursor: int, count: int):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = UserPost(sec_user_id=sec_user_id, max_cursor=max_cursor, count=count)
            # endpoint = BogusManager.xb_model_2_endpoint(
            #     DouyinAPIEndpoints.USER_POST, params.dict(), kwargs["headers"]["User-Agent"]
            # )
            # response = await crawler.fetch_get_json(endpoint)

            # 生成一个用户发布作品数据的带有a_bogus加密参数的Endpoint
            params_dict = params.dict()
            params_dict["msToken"] = ''
            a_bogus = BogusManager.ab_model_2_endpoint(params_dict, kwargs["headers"]["User-Agent"])
            endpoint = f"{DouyinAPIEndpoints.USER_POST}?{urlencode(params_dict)}&a_bogus={a_bogus}"

            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取用户喜欢作品数据
    async def fetch_user_like_videos(self, sec_user_id: str, max_cursor: int, count: int):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = UserLike(sec_user_id=sec_user_id, max_cursor=max_cursor, count=count)
            # endpoint = BogusManager.xb_model_2_endpoint(
            #     DouyinAPIEndpoints.USER_FAVORITE_A, params.dict(), kwargs["headers"]["User-Agent"]
            # )
            # response = await crawler.fetch_get_json(endpoint)

            params_dict = params.dict()
            params_dict["msToken"] = ''
            a_bogus = BogusManager.ab_model_2_endpoint(params_dict, kwargs["headers"]["User-Agent"])
            endpoint = f"{DouyinAPIEndpoints.USER_FAVORITE_A}?{urlencode(params_dict)}&a_bogus={a_bogus}"

            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取用户收藏作品数据（用户提供自己的Cookie）
    async def fetch_user_collection_videos(self, cookie: str, cursor: int = 0, count: int = 20):
        kwargs = await self.get_douyin_headers()
        kwargs["headers"]["Cookie"] = cookie
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = UserCollection(cursor=cursor, count=count)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.USER_COLLECTION, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_post_json(endpoint)
        return response

    # 获取用户合辑作品数据
    async def fetch_user_mix_videos(self, mix_id: str, cursor: int = 0, count: int = 20):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = UserMix(mix_id=mix_id, cursor=cursor, count=count)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.MIX_AWEME, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取用户直播流数据
    async def fetch_user_live_videos(self, webcast_id: str, room_id_str=""):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = UserLive(web_rid=webcast_id, room_id_str=room_id_str)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.LIVE_INFO, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取指定用户的直播流数据
    async def fetch_user_live_videos_by_room_id(self, room_id: str):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = UserLive2(room_id=room_id)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.LIVE_INFO_ROOM_ID, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取直播间送礼用户排行榜
    async def fetch_live_gift_ranking(self, room_id: str, rank_type: int = 30):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = LiveRoomRanking(room_id=room_id, rank_type=rank_type)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.LIVE_GIFT_RANK, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取指定用户的信息
    async def handler_user_profile(self, sec_user_id: str):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = UserProfile(sec_user_id=sec_user_id)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.USER_DETAIL, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取指定视频的评论数据
    async def fetch_video_comments(self, aweme_id: str, cursor: int = 0, count: int = 20):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = PostComments(aweme_id=aweme_id, cursor=cursor, count=count)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.POST_COMMENT, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取指定视频的评论回复数据
    async def fetch_video_comments_reply(self, item_id: str, comment_id: str, cursor: int = 0, count: int = 20):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = PostCommentsReply(item_id=item_id, comment_id=comment_id, cursor=cursor, count=count)
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.POST_COMMENT_REPLY, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    # 获取抖音热榜数据
    async def fetch_hot_search_result(self):
        kwargs = await self.get_douyin_headers()
        base_crawler = BaseCrawler(proxies=kwargs["proxies"], crawler_headers=kwargs["headers"])
        async with base_crawler as crawler:
            params = BaseRequestModel()
            endpoint = BogusManager.xb_model_2_endpoint(
                DouyinAPIEndpoints.DOUYIN_HOT_SEARCH, params.dict(), kwargs["headers"]["User-Agent"]
            )
            response = await crawler.fetch_get_json(endpoint)
        return response

    "-------------------------------------------------------utils接口列表-------------------------------------------------------"

    # 生成真实msToken
    async def gen_real_msToken(self, ):
        result = {
            "msToken": TokenManager().gen_real_msToken()
        }
        return result

    # 生成ttwid
    async def gen_ttwid(self, ):
        result = {
            "ttwid": TokenManager().gen_ttwid()
        }
        return result

    # 生成verify_fp
    async def gen_verify_fp(self, ):
        result = {
            "verify_fp": VerifyFpManager.gen_verify_fp()
        }
        return result

    # 生成s_v_web_id
    async def gen_s_v_web_id(self, ):
        result = {
            "s_v_web_id": VerifyFpManager.gen_s_v_web_id()
        }
        return result

    # 使用接口地址生成Xb参数
    async def get_x_bogus(self, url: str, user_agent: str):
        url = BogusManager.xb_str_2_endpoint(url, user_agent)
        result = {
            "url": url,
            "x_bogus": url.split("&X-Bogus=")[1],
            "user_agent": user_agent
        }
        return result

    # 使用接口地址生成Ab参数
    async def get_a_bogus(self, url: str, user_agent: str):
        endpoint = url.split("?")[0]
        # 将URL参数转换为dict
        params = dict([i.split("=") for i in url.split("?")[1].split("&")])
        # 去除URL中的msToken参数
        params["msToken"] = ""
        a_bogus = BogusManager.ab_model_2_endpoint(params, user_agent)
        result = {
            "url": f"{endpoint}?{urlencode(params)}&a_bogus={a_bogus}",
            "a_bogus": a_bogus,
            "user_agent": user_agent
        }
        return result

    # 提取单个用户id
    async def get_sec_user_id(self, url: str):
        return await SecUserIdFetcher.get_sec_user_id(url)

    # 提取列表用户id
    async def get_all_sec_user_id(self, urls: list):
        # 提取有效URL
        urls = extract_valid_urls(urls)

        # 对于URL列表
        return await SecUserIdFetcher.get_all_sec_user_id(urls)

    # 提取单个作品id
    async def get_aweme_id(self, url: str):
        return await AwemeIdFetcher.get_aweme_id(url)

    # 提取列表作品id
    async def get_all_aweme_id(self, urls: list):
        # 提取有效URL
        urls = extract_valid_urls(urls)

        # 对于URL列表
        return await AwemeIdFetcher.get_all_aweme_id(urls)

    # 提取单个直播间号
    async def get_webcast_id(self, url: str):
        return await WebCastIdFetcher.get_webcast_id(url)

    # 提取列表直播间号
    async def get_all_webcast_id(self, urls: list):
        # 提取有效URL
        urls = extract_valid_urls(urls)

        # 对于URL列表
        return await WebCastIdFetcher.get_all_webcast_id(urls)

    async def update_cookie(self, cookie: str):
        """
        更新指定服务的Cookie
        
        Args:
            service: 服务名称 (如: douyin_web)
            cookie: 新的Cookie值
        """
        global config
        service = "douyin"
        print('DouyinWebCrawler before update', config["TokenManager"][service]["headers"]["Cookie"])
        print('DouyinWebCrawler to update', cookie)
        # 1. 更新内存中的配置（立即生效）
        config["TokenManager"][service]["headers"]["Cookie"] = cookie
        print('DouyinWebCrawler cookie updated', config["TokenManager"][service]["headers"]["Cookie"])
        # 2. 写入配置文件（持久化）
        config_path = f"{path}/config.yaml"
        with open(config_path, 'w', encoding='utf-8') as file:
            yaml.dump(config, file, default_flow_style=False, allow_unicode=True, indent=2)

    async def main(self):
        """-------------------------------------------------------handler接口列表-------------------------------------------------------"""

        # 获取单一视频信息
        # aweme_id = "7372484719365098803"
        # result = await self.fetch_one_video(aweme_id)
        # print(result)

        # 获取用户发布作品数据
        # sec_user_id = "MS4wLjABAAAANXSltcLCzDGmdNFI2Q_QixVTr67NiYzjKOIP5s03CAE"
        # max_cursor = 0
        # count = 10
        # result = await self.fetch_user_post_videos(sec_user_id, max_cursor, count)
        # print(result)

        # 获取用户喜欢作品数据
        # sec_user_id = "MS4wLjABAAAAW9FWcqS7RdQAWPd2AA5fL_ilmqsIFUCQ_Iym6Yh9_cUa6ZRqVLjVQSUjlHrfXY1Y"
        # max_cursor = 0
        # count = 10
        # result = await self.fetch_user_like_videos(sec_user_id, max_cursor, count)
        # print(result)

        # 获取用户收藏作品数据（用户提供自己的Cookie）
        # cookie = "带上你的Cookie/Put your Cookie here"
        # cursor = 0
        # counts = 20
        # result = await self.fetch_user_collection_videos(__cookie, cursor, counts)
        # print(result)

        # 获取用户合辑作品数据
        # https://www.douyin.com/collection/7348687990509553679
        # mix_id = "7348687990509553679"
        # cursor = 0
        # counts = 20
        # result = await self.fetch_user_mix_videos(mix_id, cursor, counts)
        # print(result)

        # 获取用户直播流数据
        # https://live.douyin.com/285520721194
        # webcast_id = "285520721194"
        # result = await self.fetch_user_live_videos(webcast_id)
        # print(result)

        # 获取指定用户的直播流数据
        # # https://live.douyin.com/7318296342189919011
        # room_id = "7318296342189919011"
        # result = await self.fetch_user_live_videos_by_room_id(room_id)
        # print(result)

        # 获取直播间送礼用户排行榜
        # room_id = "7356585666190461731"
        # rank_type = 30
        # result = await self.fetch_live_gift_ranking(room_id, rank_type)
        # print(result)

        # 获取指定用户的信息
        # sec_user_id = "MS4wLjABAAAAW9FWcqS7RdQAWPd2AA5fL_ilmqsIFUCQ_Iym6Yh9_cUa6ZRqVLjVQSUjlHrfXY1Y"
        # result = await self.handler_user_profile(sec_user_id)
        # print(result)

        # 获取单个视频评论数据
        # aweme_id = "7334525738793618688"
        # result = await self.fetch_video_comments(aweme_id)
        # print(result)

        # 获取单个视频评论回复数据
        # item_id = "7344709764531686690"
        # comment_id = "7346856757471953698"
        # result = await self.fetch_video_comments_reply(item_id, comment_id)
        # print(result)

        # 获取指定关键词的综合搜索结果
        # keyword = "中华娘"
        # offset = 0
        # count = 20
        # sort_type = "0"
        # publish_time = "0"
        # filter_duration = "0"
        # result = await self.fetch_general_search_result(keyword, offset, count, sort_type, publish_time, filter_duration)
        # print(result)

        # 获取抖音热榜数据
        # result = await self.fetch_hot_search_result()
        # print(result)

        """-------------------------------------------------------utils接口列表-------------------------------------------------------"""

        # 获取抖音Web的游客Cookie
        # user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
        # result = await self.fetch_douyin_web_guest_cookie(user_agent)
        # print(result)

        # 生成真实msToken
        # result = await self.gen_real_msToken()
        # print(result)

        # 生成ttwid
        # result = await self.gen_ttwid()
        # print(result)

        # 生成verify_fp
        # result = await self.gen_verify_fp()
        # print(result)

        # 生成s_v_web_id
        # result = await self.gen_s_v_web_id()
        # print(result)

        # 使用接口地址生成Xb参数
        # url = "https://www.douyin.com/aweme/v1/web/comment/list/?device_platform=webapp&aid=6383&channel=channel_pc_web&aweme_id=7334525738793618688&cursor=0&count=20&item_type=0&insert_ids=&whale_cut_token=&cut_version=1&rcFT=&pc_client_type=1&version_code=170400&version_name=17.4.0&cookie_enabled=true&screen_width=1344&screen_height=756&browser_language=zh-CN&browser_platform=Win32&browser_name=Firefox&browser_version=124.0&browser_online=true&engine_name=Gecko&engine_version=124.0&os_name=Windows&os_version=10&cpu_core_num=16&device_memory=&platform=PC&webid=7348962975497324070"
        # user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4430.212 Safari/537.36"
        # result = await self.get_x_bogus(url, user_agent)
        # print(result)

        # 提取单个用户id
        # raw_url = "https://www.douyin.com/user/MS4wLjABAAAANXSltcLCzDGmdNFI2Q_QixVTr67NiYzjKOIP5s03CAE?vid=7285950278132616463"
        # result = await self.get_sec_user_id(raw_url)
        # print(result)

        # 提取列表用户id
        # raw_urls = [
        #     "https://www.douyin.com/user/MS4wLjABAAAANXSltcLCzDGmdNFI2Q_QixVTr67NiYzjKOIP5s03CAE?vid=7285950278132616463",
        #     "https://www.douyin.com/user/MS4wLjABAAAAVsneOf144eGDFf8Xp9QNb1VW6ovXnNT5SqJBhJfe8KQBKWKDTWK5Hh-_i9mJzb8C",
        #     "长按复制此条消息，打开抖音搜索，查看TA的更多作品。 https://v.douyin.com/idFqvUms/",
        #     "https://v.douyin.com/idFqvUms/",
        # ]
        # result = await self.get_all_sec_user_id(raw_urls)
        # print(result)

        # 提取单个作品id
        # raw_url = "https://www.douyin.com/video/7298145681699622182?previous_page=web_code_link"
        # result = await self.get_aweme_id(raw_url)
        # print(result)

        # 提取列表作品id
        # raw_urls = [
        #     "0.53 02/26 I@v.sE Fus:/ 你别太帅了郑润泽# 现场版live # 音乐节 # 郑润泽  https://v.douyin.com/iRNBho6u/ 复制此链接，打开Dou音搜索，直接观看视频!",
        #     "https://v.douyin.com/iRNBho6u/",
        #     "https://www.iesdouyin.com/share/video/7298145681699622182/?region=CN&mid=7298145762238565171&u_code=l1j9bkbd&did=MS4wLjABAAAAtqpCx0hpOERbdSzQdjRZw-wFPxaqdbAzsKDmbJMUI3KWlMGQHC-n6dXAqa-dM2EP&iid=MS4wLjABAAAANwkJuWIRFOzg5uCpDRpMj4OX-QryoDgn-yYlXQnRwQQ&with_sec_did=1&titleType=title&share_sign=05kGlqGmR4_IwCX.ZGk6xuL0osNA..5ur7b0jbOx6cc-&share_version=170400&ts=1699262937&from_aid=6383&from_ssr=1&from=web_code_link",
        #     "https://www.douyin.com/video/7298145681699622182?previous_page=web_code_link",
        #     "https://www.douyin.com/video/7298145681699622182",
        # ]
        # result = await self.get_all_aweme_id(raw_urls)
        # print(result)

        # 提取单个直播间号
        # raw_url = "https://live.douyin.com/775841227732"
        # result = await self.get_webcast_id(raw_url)
        # print(result)

        # 提取列表直播间号
        # raw_urls = [
        #     "https://live.douyin.com/775841227732",
        #     "https://live.douyin.com/775841227732?room_id=7318296342189919011&enter_from_merge=web_share_link&enter_method=web_share_link&previous_page=app_code_link",
        #     'https://webcast.amemv.com/douyin/webcast/reflow/7318296342189919011?u_code=l1j9bkbd&did=MS4wLjABAAAAEs86TBQPNwAo-RGrcxWyCdwKhI66AK3Pqf3ieo6HaxI&iid=MS4wLjABAAAA0ptpM-zzoliLEeyvWOCUt-_dQza4uSjlIvbtIazXnCY&with_sec_did=1&use_link_command=1&ecom_share_track_params=&extra_params={"from_request_id":"20231230162057EC005772A8EAA0199906","im_channel_invite_id":"0"}&user_id=3644207898042206&liveId=7318296342189919011&from=share&style=share&enter_method=click_share&roomId=7318296342189919011&activity_info={}',
        #     "6i- Q@x.Sl 03/23 【醒子8ke的直播间】  点击打开👉https://v.douyin.com/i8tBR7hX/  或长按复制此条消息，打开抖音，看TA直播",
        #     "https://v.douyin.com/i8tBR7hX/",
        # ]
        # result = await self.get_all_webcast_id(raw_urls)
        # print(result)

        # 占位
        pass


if __name__ == "__main__":
    # 初始化
    DouyinWebCrawler = DouyinWebCrawler()

    # 开始时间
    start = time.time()

    asyncio.run(DouyinWebCrawler.main())

    # 结束时间
    end = time.time()
    print(f"耗时：{end - start}")
