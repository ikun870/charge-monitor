"""从 SQLite 聚合三地点的充电数据分析，生成 dashboard/data.js。

用法：
    python scripts/gen_dashboard.py [--days 14] [--config config.json]

输出 dashboard/data.js（window.DASHBOARD_DATA），index.html 双击即可打开查看。

统计口径（重要）：
- 时间锚点取数据库最新快照时间（标准北京时间，已由上游服务器时间校准），
  不使用本地时钟，避免本机时间偏差造成窗口错位。
- 所有按小时分桶均按固定 UTC+8（北京时间）划分，不依赖系统时区。
- 服务停机等"未被记录"的时段不参与统计：
  * 占用率/热力图 = 有快照的采样轮次占比，断档时段无快照、不计入；
  * 日转次/桩 按"该地点实际有快照的小时数/24"计算有效天数，断档小时不算；
  * 平均充电/空闲时长 只统计事件时间窗内快照覆盖跨度 >= 窗长 80% 的事件，
    跨断档事件（充电开始于停机前、释放于重启后等）会被剔除。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.config import load_config
from monitor.db import Database
from monitor.stations import PLACES, place_name

DASHBOARD_DIR = Path(__file__).resolve().parent
_BJ = timezone(timedelta(hours=8))
# 事件时长有效性的快照覆盖阈值（跨度/窗长）
_SPAN_COVER_RATIO = 0.8
# 窗口边界容差（秒）：时长字段按分钟向下取整（最多丢 60s），再加轮询抖动余量
_WINDOW_SLACK = 90.0


def fmt_min(v):
    if v is None:
        return None
    return round(v, 1)


def _span_covered(db, station_id, outlet_no, t0, t1) -> bool:
    """事件时间窗 [t0, t1] 内是否被快照连续覆盖（跨度 >= 窗长*阈值）。

    连续监控时首尾快照跨度接近窗长；跨断档事件（如停机）只覆盖重启后一小段，
    跨度占比会显著低于阈值，从而被排除。
    窗口两侧加 _WINDOW_SLACK 秒容差，以容纳时长字段分钟向下取整（最多 60s）
    与轮询抖动造成的边界误差——否则 1~2 分钟的短空闲会被误判为断档。
    """
    if not t0 or not t1 or t1 <= t0:
        return False
    r = db.query(
        "SELECT MIN(ts) AS mn, MAX(ts) AS mx FROM snapshots "
        "WHERE station_id=? AND outlet_no=? AND ts BETWEEN ? AND ?",
        (station_id, outlet_no, t0 - _WINDOW_SLACK, t1 + _WINDOW_SLACK))[0]
    if not r["mn"]:
        return False
    return (r["mx"] - r["mn"]) >= (t1 - t0) * _SPAN_COVER_RATIO


def build(days: int, cfg: dict) -> dict:
    db = Database(Path(cfg.get("data_dir", "data")) / "monitor.db")

    # 时间锚点：数据库最新快照（标准北京时间），不使用本地时钟
    anchor = db.query("SELECT MAX(ts) AS mx FROM snapshots")[0]["mx"]
    if not anchor:
        anchor = time.time()
    since = anchor - days * 86400

    data: dict = {
        "data_until": datetime.fromtimestamp(anchor, tz=_BJ).strftime("%Y-%m-%d %H:%M"),
        "days": days,
        "places": {},
        "station_table": [],
    }

    # 每地点：插座数（取元数据）
    meta = db.get_outlet_meta()
    by_place_meta: dict[str, int] = {}
    for m in meta:
        by_place_meta[m["place_code"]] = by_place_meta.get(m["place_code"], 0) + 1

    # 有效覆盖：每地点窗口内"有快照的独立(日期,小时)桶数" → 有效天数 = 桶数/24
    eff = db.query(
        """SELECT m.place_code,
                  COUNT(DISTINCT strftime('%Y-%m-%d %H', s.ts, 'unixepoch', '+8 hours')) AS h
           FROM snapshots s JOIN outlet_meta m ON s.station_id=m.station_id AND s.outlet_no=m.outlet_no
           WHERE s.ts >= ? GROUP BY m.place_code""", (since,))
    eff_days = {r["place_code"]: max(r["h"] / 24.0, 1e-6) for r in eff}

    # 最近 24h 监控覆盖（小时数，供前端标注 charge_24h 可信度）
    cov = db.query(
        """SELECT m.place_code,
                  COUNT(DISTINCT strftime('%Y-%m-%d %H', s.ts, 'unixepoch', '+8 hours')) AS h
           FROM snapshots s JOIN outlet_meta m ON s.station_id=m.station_id AND s.outlet_no=m.outlet_no
           WHERE s.ts >= ? GROUP BY m.place_code""", (anchor - 86400,))
    cov_24h = {r["place_code"]: r["h"] for r in cov}

    # 24h 内充电完成次数（release 事件数）
    charge_24h = db.query(
        """SELECT place_code, COUNT(*) AS n FROM events
           WHERE event_type='release' AND ts >= ? GROUP BY place_code""", (anchor - 86400,))
    charge24_map = {r["place_code"]: r["n"] for r in charge_24h}

    # 1) 按小时占用率 + 热力图（采样占比，断档小时无快照自然不计入）
    occ_hour = db.query(
        """SELECT m.place_code,
                  CAST(strftime('%H', s.ts, 'unixepoch', '+8 hours') AS INTEGER) AS hour,
                  COUNT(*) AS n, SUM(CASE WHEN s.available=0 THEN 1 ELSE 0 END) AS occ
           FROM snapshots s JOIN outlet_meta m ON s.station_id=m.station_id AND s.outlet_no=m.outlet_no
           WHERE s.ts >= ? GROUP BY m.place_code, hour""", (since,))
    heat = db.query(
        """SELECT m.place_code,
                  CAST(strftime('%w', s.ts, 'unixepoch', '+8 hours') AS INTEGER) AS wd,
                  CAST(strftime('%H', s.ts, 'unixepoch', '+8 hours') AS INTEGER) AS hour,
                  COUNT(*) AS n, SUM(CASE WHEN s.available=0 THEN 1 ELSE 0 END) AS occ
           FROM snapshots s JOIN outlet_meta m ON s.station_id=m.station_id AND s.outlet_no=m.outlet_no
           WHERE s.ts >= ? GROUP BY m.place_code, wd, hour""", (since,))

    # 2) 事件统计：拉全部事件，Python 侧做"跨断档过滤"再聚合
    evs = db.query(
        """SELECT place_code, event_type, charge_minutes, idle_minutes,
                  station_id, outlet_no, ts
           FROM events WHERE ts >= ?""", (since,))
    ev_by_place: dict[str, dict] = {}
    st_release: dict[int, list] = {}
    for e in evs:
        bp = ev_by_place.setdefault(e["place_code"], {"release": [], "occupy": []})
        if e["event_type"] == "release" and e["charge_minutes"] is not None:
            t0 = e["ts"] - e["charge_minutes"] * 60
            if _span_covered(db, e["station_id"], e["outlet_no"], t0, e["ts"]):
                bp["release"].append(e["charge_minutes"])
                st_release.setdefault(e["station_id"], []).append(e["charge_minutes"])
        elif e["event_type"] == "occupy" and e["idle_minutes"] is not None:
            t0 = e["ts"] - e["idle_minutes"] * 60
            if _span_covered(db, e["station_id"], e["outlet_no"], t0, e["ts"]):
                bp["occupy"].append(e["idle_minutes"])

    # 3) 功率/费用
    pow_fee = db.query(
        """SELECT m.place_code, AVG(s.power_w) AS avg_power, AVG(s.used_fee) AS avg_fee
           FROM snapshots s JOIN outlet_meta m ON s.station_id=m.station_id AND s.outlet_no=m.outlet_no
           WHERE s.ts >= ? AND s.power_w IS NOT NULL GROUP BY m.place_code""", (since,))

    # 4) 站点对比（占用率=采样占比；站点平均充电时长走过滤后事件）
    st_occ = db.query(
        """SELECT m.station_id, m.station_name, m.place_code, COUNT(DISTINCT m.outlet_no) AS outlets,
                  SUM(CASE WHEN s.available=0 THEN 1 ELSE 0 END)*1.0/COUNT(*) AS occ_rate
           FROM snapshots s JOIN outlet_meta m ON s.station_id=m.station_id AND s.outlet_no=m.outlet_no
           WHERE s.ts >= ? GROUP BY m.station_id""", (since,))

    for code in PLACES:
        hour_map = {r["hour"]: r for r in occ_hour if r["place_code"] == code}
        pf = next((r for r in pow_fee if r["place_code"] == code), None)
        heat_rows = [r for r in heat if r["place_code"] == code]
        ev = ev_by_place.get(code, {"release": [], "occupy": []})

        occupancy = []
        for h in range(24):
            r = hour_map.get(h)
            occupancy.append(round(r["occ"] / r["n"], 3) if r and r["n"] else None)

        heatmap = [[0] * 24 for _ in range(7)]
        for r in heat_rows:
            if r["n"]:
                heatmap[r["wd"]][r["hour"]] = round(r["occ"] / r["n"], 3)

        outlets = by_place_meta.get(code, 0)
        releases_n = len(ev["release"])
        data["places"][code] = {
            "name": place_name(code),
            "outlets": outlets,
            "charge_24h": charge24_map.get(code, 0),
            "coverage_24h": cov_24h.get(code, 0),
            "eff_days": round(eff_days.get(code, 0), 2),
            "occupancy_by_hour": occupancy,
            "avg_charge_min": fmt_min(sum(ev["release"]) / len(ev["release"]) if ev["release"] else None),
            "avg_idle_min": fmt_min(sum(ev["occupy"]) / len(ev["occupy"]) if ev["occupy"] else None),
            "release_count": releases_n,
            "occupy_count": len(ev["occupy"]),
            "turnover_per_day": round(releases_n / outlets / eff_days.get(code, days), 2) if outlets else 0,
            "avg_power_w": round(pf["avg_power"]) if pf and pf["avg_power"] else None,
            "avg_fee_yuan": round(pf["avg_fee"], 2) if pf and pf["avg_fee"] else None,
            "heatmap": heatmap,
        }

    for r in st_occ:
        ch = st_release.get(r["station_id"], [])
        data["station_table"].append({
            "place": place_name(r["place_code"]),
            "station": r["station_name"],
            "outlets": r["outlets"],
            "occupancy": round(r["occ_rate"] * 100, 1),
            "avg_charge_min": fmt_min(sum(ch) / len(ch) if ch else None),
            "releases": len(ch),
        })
    data["station_table"].sort(key=lambda x: x["occupancy"], reverse=True)

    data["total_events"] = sum(
        (p["release_count"] + p["occupy_count"]) for p in data["places"].values())
    data["snapshot_count"] = db.query(
        "SELECT COUNT(*) AS n FROM snapshots WHERE ts >= ?", (since,))[0]["n"]
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    data = build(args.days, cfg)
    out = DASHBOARD_DIR / "data.js"
    out.write_text(f"window.DASHBOARD_DATA = {json.dumps(data, ensure_ascii=False, indent=1)};",
                   encoding="utf-8")
    print(f"已生成 {out}（覆盖 {args.days} 天，快照 {data['snapshot_count']:,} 行，事件 {data['total_events']} 条）")
    print(f"数据截至 {data['data_until']}（标准北京时间）")


if __name__ == "__main__":
    main()
