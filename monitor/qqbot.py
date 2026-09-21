"""QQ 官方开放平台机器人接入（botpy WS 模式）。

- 接收 C2C 私聊消息 → NLU 解析 → 分发（开始/停止/查询/帮助）；
- 监控事件提醒：主动调用 post_c2c_message 推送（沙箱环境不受频控限制）。
"""
from __future__ import annotations

import logging

import botpy
from botpy.message import C2CMessage

from . import messages
from .engine import MonitorEngine, new_session_id
from .nlu import parse
from .stations import place_name

log = logging.getLogger("monitor.qqbot")


class ChargeBot(botpy.Client):
    def __init__(self, engine: MonitorEngine, cfg: dict, **kwargs):
        super().__init__(**kwargs)
        self.engine = engine
        self.cfg = cfg
        self.allow_openids = set(cfg["qq"].get("allow_openids") or [])
        self._user_openid: str | None = None
        engine.sender = self.send_notify
        if not self.allow_openids:
            log.warning("[qqbot] allow_openids 为空：将接受任意私聊用户的指令（沙箱环境下仅沙箱用户可触达，仍建议配置白名单）")

    # ---------- 生命周期 ----------
    async def on_ready(self):
        log.info("[qqbot] 机器人已连接 QQ 网关，启动后台任务...")
        mon = self.cfg["monitor"]
        self.loop.create_task(self.engine.monitor_loop(mon["poll_interval_seconds"]))
        self.loop.create_task(self.engine.collector_loop())
        self.loop.create_task(self.engine.timeout_checker(mon["session_timeout_minutes"]))
        # 预热三地点插座元数据（无则自动种子）
        from .stations import PLACES
        for code in PLACES:
            try:
                await self.engine.ensure_place_meta(code)
            except Exception as e:
                log.warning("[qqbot] 元数据预热 %s 失败: %s", code, e)
        log.info("[qqbot] 就绪，共 %d 个地点", len(PLACES))

    # ---------- 消息处理 ----------
    async def on_c2c_message_create(self, message: C2CMessage):
        openid = message.author.user_openid
        if self.allow_openids and openid not in self.allow_openids:
            log.info("[qqbot] 忽略非白名单用户 %s", openid)
            return
        self._user_openid = openid
        content = (message.content or "").strip()
        log.info("[qqbot] 收到指令: %s", content)

        intent = parse(content)
        try:
            reply = await self._dispatch(intent)
        except Exception as e:
            log.exception("[qqbot] 分发异常: %s", e)
            reply = "处理出错，请稍后再试。"
        if reply is None:
            # 已直接发送富媒体（如位置图）
            return
        try:
            await message.reply(msg_type=0, content=reply)
        except Exception as e:
            log.warning("[qqbot] 被动回复失败（可能超时/超频）: %s", e)
            await self.send_notify(reply)

    async def _dispatch(self, intent) -> str | None:
        action = intent.action
        if action == "stats_stop":
            self.engine.stats_enabled = False
            return messages.stats_stop_message()
        if action == "stats_start":
            self.engine.stats_enabled = True
            return messages.stats_start_message()
        if action == "start":
            return await self._start(intent.places)
        if action == "stop":
            return await self._stop_all()
        if action == "map":
            return await self._reply_map(intent.places)
        if action == "status":
            summaries = await self.engine.summaries(intent.places or None)
            if not summaries:
                return "还没有任何数据，稍等片刻再试。"
            return messages.status_message(summaries)
        if action == "help":
            return messages.HELP_TEXT
        return messages.unknown_message()

    async def _reply_map(self, places: list[str]) -> str | None:
        """「学子位置」「主楼在哪」→ 发送对应地点的充电桩位置图（图片消息）。"""
        if not places:
            return "想看哪个位置的地图？说「学子位置」「主楼位置」「硕丰位置」就行。"
        code = places[0]
        cfg_maps = self.cfg.get("maps") or {}
        url = cfg_maps.get(code)
        if not url:
            return "该地点还没有配置位置图，稍后再试。"
        name = place_name(code)
        try:
            media = await self.api.post_c2c_file(openid=self._user_openid, file_type=1, url=url)
            await self.api.post_c2c_message(
                openid=self._user_openid, msg_type=7, content=f"📍{name} 充电桩位置图", media=media)
            return None
        except Exception as e:
            log.warning("[qqbot] 发送位置图失败: %s", e)
            return f"{name}位置图发送失败（图片地址可能已失效），稍后再试。"

    async def _start(self, places: list[str]) -> str:
        if not places:
            return "要去哪个地点充电呀？支持：硕丰十组团 / 主楼C区停车场 / 学子餐厅（可同时说多个）。"
        sid = new_session_id()
        try:
            return await self.engine.start_session(sid, places)
        except Exception as e:
            log.exception("[qqbot] 启动监控失败: %s", e)
            return "启动监控失败，请确认网络后重试。"

    async def _stop_all(self) -> str:
        msgs = []
        for sid in list(self.engine.sessions.keys()):
            m = await self.engine.end_session(sid)
            if m:
                msgs.append(m)
        if not msgs:
            return "当前没有正在进行的监控。"
        return "\n\n".join(msgs)

    # ---------- 主动推送 ----------
    async def send_notify(self, text: str):
        if not self._user_openid:
            log.warning("[qqbot] 尚无交互用户，主动消息未发送: %s", text[:40])
            return
        try:
            await self.api.post_c2c_message(openid=self._user_openid, msg_type=0, content=text)
        except Exception as e:
            log.warning("[qqbot] 主动消息发送失败: %s", e)
