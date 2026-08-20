"""仅保留抖音 Web 爬虫会用到的工具函数，避免引入 browser_cookie3 等 NAS 上难装的依赖。"""
from __future__ import annotations

import datetime
import random
import re
import secrets
from typing import List, Union

from pydantic import BaseModel
from urllib.parse import urlencode

seed_bytes = secrets.token_bytes(16)
random.seed(int.from_bytes(seed_bytes, "big"))


def model_to_query_string(model: BaseModel) -> str:
    return urlencode(model.dict())


def gen_random_str(randomlength: int) -> str:
    base_str = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-"
    return "".join(random.choice(base_str) for _ in range(randomlength))


def get_timestamp(unit: str = "milli"):
    now = datetime.datetime.utcnow() - datetime.datetime(1970, 1, 1)
    if unit == "milli":
        return int(now.total_seconds() * 1000)
    if unit == "sec":
        return int(now.total_seconds())
    if unit == "min":
        return int(now.total_seconds() / 60)
    raise ValueError("Unsupported time unit")


def extract_valid_urls(inputs: Union[str, List[str]]) -> Union[str, List[str], None]:
    url_pattern = re.compile(r"https?://\S+")
    if isinstance(inputs, str):
        match = url_pattern.search(inputs)
        return match.group(0) if match else None
    if isinstance(inputs, list):
        valid_urls = []
        for input_str in inputs:
            valid_urls.extend(url_pattern.findall(input_str))
        return valid_urls
    return None


def split_filename(text: str, os_limit: dict) -> str:
    return (text or "")[: os_limit.get("win32", 200)]
