"""冒烟测试：NLU 指令解析 + 消息模板 + API 客户端 + 引擎单轮轮询 + 数据生成。

用法：python scripts/smoke_test.py
说明：会真实调用闪开来电 API（约 19 站 × 每站 1 次 + 状态变化补采），并写入 data/smoke_test.db。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.api import FlashApi
from monitor.config import load_config
from monitor.db import Database
from monitor.engine import MonitorEngine
from monitor.nlu import parse


def test_nlu():
    cases = [
        ("我要在硕丰十组团充电", "start", ["shuofeng"]),
        ("主楼充电", "start", ["zhulou"]),
        ("学子餐厅充电", "start", ["xuezi"]),
        ("现在有空吗", "status", []),
        ("主楼现在什么情况", "status", ["zhulou"]),
        ("结束监控", "stop", []),
        ("我充上了", "stop", []),
        ("不用看了", "stop", []),
        ("硕丰和主楼都帮我看着", "start", ["shuofeng", "zhulou"]),
        ("帮我看看主楼", "status", ["zhulou"]),
        ("学子位置", "map", ["xuezi"]),
        ("找学子位置", "map", ["xuezi"]),
        ("主楼在哪", "map", ["zhulou"]),
        ("硕丰地图", "map", ["shuofeng"]),
        ("学子在哪充电", "map", ["xuezi"]),
        ("帮助", "help", []),
        ("停止统计", "stats_stop", []),
        ("暂停统计", "stats_stop", []),
        ("开始统计", "stats_start", []),
        ("恢复统计", "stats_start", []),
        ("今天天气不错", "unknown", []),
    ]
    failed = 0
    for text, want_action, want_places in cases:
        it = parse(text)
        ok = it.action == want_action and set(it.places) == set(want_places)
        print(f"{'✓' if ok else '✗'} {text!r} -> {it.action} {it.places} (期望 {want_action} {want_places})")
        if not ok:
            failed += 1
    return failed


def test_engine_once(cfg: dict) -> int:
    """对全部三个地点各做一轮轮询（snapshot 模式），验证 API/DB/引擎链路。"""
    import time
    from monitor.stations import PLACES

    db = Database(Path(cfg["data_dir"]) / "smoke_test.db")
    api = FlashApi(cfg)
    engine = MonitorEngine(db, api, cfg)

    async def run():
        for code in PLACES:
            await engine.ensure_place_meta(code)
            summary = await engine.poll_place(code, snapshot_only=True)
            if summary is None:
                print(f"✗ {code}: 未取到数据")
                return 1
            print(f"✓ {code}: {summary['available']}/{summary['total']} 空闲, 站数 {len(summary['stations'])}")
        # 再轮询一次（验证重复轮询稳定、事件逻辑不抛错）
        for code in PLACES:
            await engine.poll_place(code, snapshot_only=False, notify=False)
        print("✓ 第二轮轮询（含状态比对）无异常")
        return 0

    try:
        return asyncio.run(run())
    finally:
        db.close()


def main():
    cfg = load_config()
    nlu_failed = test_nlu()
    print(f"\nNLU 用例失败: {nlu_failed}")
    if nlu_failed:
        return 1
    print("\n开始引擎冒烟（真实 API，约需 1-2 分钟）...")
    return test_engine_once(cfg)


if __name__ == "__main__":
    sys.exit(main())
