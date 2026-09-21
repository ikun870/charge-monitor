"""指令解析：把用户自然语言转成意图。

支持的口语示例：
- 开始监控：  "我要在硕丰十组团充电" / "主楼充电" / "帮我盯着学子餐厅" / "硕丰和主楼都帮我看看"
- 查询状态：  "现在有空吗" / "主楼现在什么情况" / "看看还有没有空桩"
- 结束监控：  "结束监控" / "我充上了" / "不用看了" / "停止"
- 帮助：      "帮助" / "怎么用"
"""
from __future__ import annotations

import re
import unicodedata

from .stations import PLACE_KEYWORDS

# 动作关键词（命中即判定，按优先级）
STATS_STOP_WORDS = ["停止统计", "暂停统计", "停掉统计", "关闭统计", "关掉统计", "别统计", "不用统计", "统计停了"]
STATS_START_WORDS = ["开始统计", "恢复统计", "继续统计", "开启统计", "打开统计", "重新统计"]
STOP_WORDS = [
    "结束", "停止", "停掉", "终止", "关闭监控", "关闭", "不看了", "不用看了", "不用了",
    "不充了", "算了", "充好了", "充上了", "充完", "完毕", "搞定", "够了", "收工", "撤销",
]
HELP_WORDS = ["帮助", "怎么用", "能做什么", "支持什么", "指令", "说明一下", "有哪些功能", "用法"]
MAP_WORDS = ["位置", "地图", "在哪", "在哪里", "方位", "布局", "怎么走"]
STATUS_WORDS = ["看看", "看下", "看一下", "看一", "现在", "当前", "目前", "有空", "空闲",
                "状态", "情况", "怎么样", "有没有", "还有", "几个", "多少", "查一下", "查查"]
START_WORDS = ["充电", "监控", "盯着", "看着", "留意", "占位", "占个", "帮我抢", "帮我占", "开始"]

_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[，。！？、,.!?;；:：'\"“”‘’\-_～~]+")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = _WS.sub("", text).lower()
    text = _PUNCT.sub("", text)
    return text


def _match_places(text: str) -> list[str]:
    """返回命中的地点 code 列表（按 PLACES 定义顺序）。"""
    hits: list[str] = []
    for code, keywords in PLACE_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                hits.append(code)
                break
    return hits


class Intent:
    def __init__(self, action: str, places: list[str], raw: str):
        self.action = action      # start / stop / status / help / map / stats_stop / stats_start / unknown
        self.places = places      # 命中的地点 code
        self.raw = raw

    def __repr__(self):  # pragma: no cover
        return f"Intent(action={self.action}, places={self.places})"


def parse(text: str) -> Intent:
    norm = _normalize(text)
    places = _match_places(norm)

    # 统计开关优先（"停止统计"同时含"停止"）
    for w in STATS_STOP_WORDS:
        if w in norm:
            return Intent("stats_stop", places, text)
    for w in STATS_START_WORDS:
        if w in norm:
            return Intent("stats_start", places, text)

    # 结束优先（"结束监控"同时含"监控"）
    for w in STOP_WORDS:
        if w in norm:
            return Intent("stop", places, text)

    # 位置/地图优先（"主楼在哪" 倾向要位置图；"学子位置还有空吗" 也先回图）
    for w in MAP_WORDS:
        if w in norm:
            return Intent("map", places, text)

    for w in HELP_WORDS:
        if w in norm:
            return Intent("help", places, text)

    hit_status = any(w in norm for w in STATUS_WORDS)
    hit_start = any(w in norm for w in START_WORDS)

    if hit_status and not hit_start:
        return Intent("status", places, text)
    if hit_status and hit_start:
        # 两者都命中：含"看看/查"倾向查询；否则视为开始监控
        queryish = any(w in norm for w in ["看看", "看下", "看一下", "查", "有没有", "还有", "有空", "空闲", "现在", "当前"])
        if queryish:
            return Intent("status", places, text)
    if hit_start:
        return Intent("start", places, text)

    if places:
        # 只说地点名（如"硕丰"），默认视为查询当前状态
        return Intent("status", places, text)

    return Intent("unknown", places, text)
