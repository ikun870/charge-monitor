"""提醒与回复消息模板。

设计原则（按用户要求）：
- 只对状态变化（释放/占用）发消息；
- 若触发站点仍有空闲桩，附上该站全部空闲桩的完整信息（插座编号）；
- 附带全地点空闲汇总，帮助判断值不值得过去。
"""
from __future__ import annotations

from datetime import datetime

from .stations import place_name


def _hm(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


_PREFIXES = ["硕丰十组团", "学子餐厅", "主楼C 区停车场", "主楼C区停车场", "主楼C区", "主楼"]


def _short_station(name: str) -> str:
    """简化站名：「电子科大清水河校区学子餐厅5号充电桩」→「5号充电桩」；无编号站保留「硕丰十组团充电桩」。"""
    n = name.replace("电子科大清水河校区", "")
    for p in _PREFIXES:
        if n.startswith(p):
            n = n[len(p):]
            break
    if not n or n == "充电桩":
        return name.replace("电子科大清水河校区", "")
    return n


def _station_label(st: dict) -> str:
    """站标签：简化名。「5号充电桩」「7号充电桩」。位置信息由地图截图提供，消息内不重复。"""
    return _short_station(st["name"])


def _list_outlets(outlets: list[str]) -> str:
    """['插座2','插座5'] -> 「插座2、插座5」"""
    if not outlets:
        return "（无）"
    return "、".join(outlets)


def _charging_top_block(summary: dict) -> list[str]:
    """无空闲时：充电中 Top5（已充最久 = 最接近释放）。"""
    top = summary.get("charging_top") or []
    if not top:
        return []
    lines = ["充电中 Top5（最接近释放）："]
    for i, t in enumerate(top, 1):
        lines.append(f"  {i}. {_short_station(t['station_name'])} {t['outlet']}（已充 {t['minutes']} 分钟）")
    return lines


def start_message(place_codes: list[str], summaries: dict[str, dict], poll_seconds: int) -> str:
    """监控启动确认 + 当前状态。summaries: place_code -> {'name', 'total', 'available', 'free_list_by_station'}"""
    names = "、".join(place_name(c) for c in place_codes)
    lines = [f"🔋 已开始监控「{names}」", f"每 {poll_seconds} 秒检测一次，有释放/占用会第一时间通知你。"]
    total_free = 0
    total_all = 0
    for code in place_codes:
        s = summaries.get(code)
        if not s:
            continue
        total_all += s["total"]
        total_free += s["available"]
        station_lines = []
        for st in s["stations"]:
            if st["free_count"] == 0:
                station_lines.append(f"  {_station_label(st)}：充电中")
            else:
                station_lines.append(
                    f"  ✅ {_station_label(st)}：空闲 {_list_outlets(st['free_outlets'])}（{st['free_count']}/{st['total']}）")
            if st.get("fault_outlets"):
                station_lines.append(f"  ⚠️ {_station_label(st)} 故障：{_list_outlets(st['fault_outlets'])}")
        lines.append(f"\n📍{s['name']} 当前空闲 {s['available']}/{s['total']}")
        lines.extend(station_lines)
        if s["available"] == 0:
            lines.extend(_charging_top_block(s))
    lines.append(f"\n全部地点合计：空闲 {total_free}/{total_all}")
    lines.append("\n充上后回复「结束监控」或「我充上了」即可停止。")
    return "\n".join(lines)


def _free_summary_block(summary: dict) -> list[str]:
    """渲染该地点全部空闲桩的完整明细（各站分别列出）。"""
    lines: list[str] = []
    if summary["available"] == 0:
        lines.append(f"📍 {summary['name']} 当前空闲：无（0/{summary['total']}）")
        lines.extend(_charging_top_block(summary))
        return lines
    lines.append(f"📍 {summary['name']} 当前空闲（{summary['available']}/{summary['total']}）：")
    for st in summary["stations"]:
        if st["free_count"] == 0:
            continue
        lines.append(f"  ✅ {_station_label(st)}：空闲 {_list_outlets(st['free_outlets'])}（{st['free_count']}/{st['total']}）")
    faults = [st for st in summary["stations"] if st.get("fault_outlets")]
    if faults:
        lines.append("")
        for st in faults:
            lines.append(f"⚠️ {_station_label(st)} 故障：{_list_outlets(st['fault_outlets'])}")
    return lines


def release_message(place_code: str, station_name: str, outlet_name: str, ts: float,
                    summary: dict, charge_minutes: int | None = None) -> str:
    lines = [f"🟢 释放｜{place_name(place_code)}"]
    lines.append(f"「{_short_station(station_name)}」{outlet_name} 已释放（{_hm(ts)}）")
    if charge_minutes is not None:
        lines.append(f"上次充电时长约 {charge_minutes} 分钟")
    lines.append("")
    lines.extend(_free_summary_block(summary))
    return "\n".join(lines)


def occupy_message(place_code: str, station_name: str, outlet_name: str, ts: float,
                   summary: dict) -> str:
    lines = [f"🔴 占用｜{place_name(place_code)}"]
    lines.append(f"「{_short_station(station_name)}」{outlet_name} 刚被占用（{_hm(ts)}）")
    lines.append("")
    lines.extend(_free_summary_block(summary))
    return "\n".join(lines)


def end_message(place_codes: list[str], duration_minutes: int, release_count: int, occupy_count: int) -> str:
    names = "、".join(place_name(c) for c in place_codes)
    return (
        f"✅ 已结束监控「{names}」\n"
        f"本次监控持续约 {duration_minutes} 分钟，期间提醒 {release_count + occupy_count} 次"
        f"（释放 {release_count} / 占用 {occupy_count}）。\n祝充电顺利！"
    )


def status_message(summaries: dict[str, dict]) -> str:
    """查询当前状态。"""
    lines = ["📊 当前状态"]
    total_free = 0
    total_all = 0
    for code, s in summaries.items():
        total_all += s["total"]
        total_free += s["available"]
        station_lines = []
        for st in s["stations"]:
            if st["free_count"] == 0:
                station_lines.append(f"  {_station_label(st)}：充电中")
            else:
                station_lines.append(
                    f"  ✅ {_station_label(st)}：空闲 {_list_outlets(st['free_outlets'])}（{st['free_count']}/{st['total']}）")
            if st.get("fault_outlets"):
                station_lines.append(f"  ⚠️ {_station_label(st)} 故障：{_list_outlets(st['fault_outlets'])}")
        lines.append(f"\n📍{s['name']} 空闲 {s['available']}/{s['total']}")
        lines.extend(station_lines)
        if s["available"] == 0:
            lines.extend(_charging_top_block(s))
    lines.append(f"\n合计：空闲 {total_free}/{total_all}")
    return "\n".join(lines)


HELP_TEXT = (
    "🔌 充电桩监控机器人\n\n"
    "支持三个地点：硕丰十组团 / 主楼C区停车场 / 学子餐厅\n\n"
    "指令示例：\n"
    "· 我要在硕丰十组团充电 → 开始监控\n"
    "· 主楼充电 → 开始监控\n"
    "· 硕丰和主楼都帮我看着 → 同时监控两个地点\n"
    "· 现在有空吗 / 主楼什么情况 → 查询当前空闲\n"
    "· 学子位置 / 主楼在哪 → 发送对应地点的充电桩位置图\n"
    "· 结束监控 / 我充上了 → 停止监控\n"
    "· 停止统计 / 开始统计 → 暂停/恢复后台数据采集\n\n"
    "监控期间，空闲桩被释放或占用时会主动提醒你。"
)


def stats_stop_message() -> str:
    return "📊 已停止数据统计，后台采集暂停。回复「开始统计」可恢复，监控提醒不受影响。"


def stats_start_message() -> str:
    return "📊 已开始数据统计，正在采集三个地点的充电桩数据（硕丰十组团 / 主楼C区停车场 / 学子餐厅）。"


def unknown_message() -> str:
    return "没太听懂～试试说「主楼充电」开始监控，或「现在有空吗」查状态。回复「帮助」看全部指令。"
