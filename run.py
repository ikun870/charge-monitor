"""充电桩监控机器人入口。

用法：
    python run.py
（首次需先复制 config.example.json 为 config.json 并填写 QQ 机器人 AppID/AppSecret）
"""
from __future__ import annotations

import logging
from pathlib import Path

import botpy

from monitor.api import FlashApi
from monitor.config import load_config
from monitor.db import Database
from monitor.engine import MonitorEngine
from monitor.qqbot import ChargeBot


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    log = logging.getLogger("main")

    cfg = load_config()
    qq_cfg = cfg["qq"]

    if "在这里填" in str(qq_cfg.get("appid", "")) or "在这里填" in str(qq_cfg.get("secret", "")):
        log.error("请先复制 config.example.json 为 config.json，并填入 QQ 开放平台的 AppID / AppSecret（见 README 注册步骤）。")
        raise SystemExit(1)

    data_dir = Path(cfg.get("data_dir", "data"))
    db = Database(data_dir / "monitor.db")
    api = FlashApi(cfg)
    engine = MonitorEngine(db, api, cfg)

    intents = botpy.Intents(public_messages=True)
    client = ChargeBot(
        engine=engine,
        cfg=cfg,
        intents=intents,
        is_sandbox=qq_cfg.get("is_sandbox", True),
    )
    log.info("启动充电桩监控机器人（appid=%s, sandbox=%s）", qq_cfg.get("appid"), qq_cfg.get("is_sandbox"))
    client.run(appid=str(qq_cfg["appid"]), secret=str(qq_cfg["secret"]))


if __name__ == "__main__":
    main()
