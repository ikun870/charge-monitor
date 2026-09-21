"""监控引擎：轮询、状态比对、事件检测、会话管理。

- 每个「地点」维护一组插座跟踪器（OutletTracker），记录上一轮/当前可用状态；
- 每轮拉取该地点全部站点的插座列表（outlets 接口，currentChargingRecordId==0 即空闲）；
- 状态翻转时产生事件：占用（occupy）→ 调详情接口记录充电开始时间；释放（release）→ 计算充电时长；
- 事件始终写入数据库（供分析）；notify=True 时同时通过 sender 回调发 QQ 提醒；
- 监控会话（用户指令驱动）与后台采集（7×24）复用同一轮询核心。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from . import messages
from .stations import PLACES, STATION_LOCATION, place_name

log = logging.getLogger("monitor.engine")

# 北京时间固定时区（用于采集时段判断，不依赖系统时区设置）
_BEIJING_TZ = timezone(timedelta(hours=8))


def _parse_begin(charging_begin: str | None) -> float | None:
    if not charging_begin:
        return None
    try:
        return datetime.strptime(charging_begin, "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


class OutletTracker:
    """单个插座的状态跟踪。"""

    def __init__(self, outlet_no: str, station_id: int, station_name: str, outlet_name: str | None):
        self.outlet_no = outlet_no
        self.station_id = station_id
        self.station_name = station_name
        self.outlet_name = outlet_name
        self.available: bool | None = None
        self.prev_available: bool | None = None
        self.fault: bool = False  # 故障（iErrorCount 超过阈值）
        self.charging_begin: str | None = None
        self.power_w: int | None = None
        self.used_min: int | None = None
        self.used_fee: float | None = None
        self.last_release_ts: float | None = None

    def display_name(self) -> str:
        if self.outlet_name:
            return self.outlet_name
        return self.outlet_no


class PlaceMonitor:
    """一个地点的轮询状态容器。"""

    def __init__(self, place_code: str):
        self.place_code = place_code
        self.trackers: dict[str, OutletTracker] = {}
        self.seeded = False

    def station_trackers(self, station_id: int) -> list[OutletTracker]:
        return [t for t in self.trackers.values() if t.station_id == station_id]


class Session:
    def __init__(self, session_id: str, places: list[str], created_at: float | None = None):
        self.id = session_id
        self.places = places
        self.created_at = created_at if created_at is not None else time.time()


class MonitorEngine:
    def __init__(self, db, api, cfg: dict):
        self.db = db
        self.api = api
        self.cfg = cfg
        self.monitors: dict[str, PlaceMonitor] = {}
        self.sessions: dict[str, Session] = {}
        self.place_sessions: dict[str, set[str]] = {code: set() for code in PLACES}
        self.sender = None  # async callable(text: str)
        self.stats_enabled = True  # 后台数据统计开关（默认开启，可用机器人指令控制）
        self._sem = asyncio.Semaphore(4)

    # ---------- 元数据种子 ----------
    async def ensure_place_meta(self, place_code: str):
        mon = self.monitors.setdefault(place_code, PlaceMonitor(place_code))
        if mon.seeded:
            return
        meta = self.db.get_outlet_meta(place_code=place_code)
        if meta:
            for m in meta:
                mon.trackers[m["outlet_no"]] = OutletTracker(
                    m["outlet_no"], m["station_id"], m["station_name"], m["outlet_name"])
            mon.seeded = True
            return
        # 从 API 种子：每站拉插座，再逐个拉详情拿名称
        log.info("[engine] 地点 %s 元数据缺失，从 API 种子...", place_code)
        for station_id, station_name in PLACES[place_code]["stations"].items():
            try:
                outlets = await self.api.fetch_station_outlets(station_id)
            except Exception as e:
                log.warning("[engine] 种子失败 station=%s: %s", station_id, e)
                continue
            for o in outlets:
                outlet_no = o.get("outletNo")
                if not outlet_no:
                    continue
                name = None
                try:
                    st = await self.api.fetch_outlet_status(outlet_no)
                    name = st.get("outlet", {}).get("vOutletName")
                except Exception:
                    pass
                self.db.upsert_outlet_meta(
                    station_id, station_name, place_code, outlet_no,
                    o.get("outletSerialNo"), name)
                mon.trackers[outlet_no] = OutletTracker(outlet_no, station_id, station_name, name)
        mon.seeded = True
        log.info("[engine] 地点 %s 种子完成，共 %d 个插座", place_code, len(mon.trackers))

    # ---------- 轮询核心 ----------
    async def _fetch_outlets_available(self, place_code: str, mon: PlaceMonitor) -> dict[str, dict]:
        """并行拉取该地点所有站插座实时状态。

        重要：outlets 列表接口的 currentChargingRecordId 是陈旧缓存（实测与实时不一致，
        空闲插座可能被标为非 0），因此空闲判定一律改用「插座详情接口」
        （GET /charge/v1/charging/outlet/{outletNo}，outlet.iCurrentChargingRecordId==0 即空闲）。
        列表接口仅用于取插座清单/序列号。
        返回 outlet_no -> {serial, available, station_id, charging_begin, power_w, used_min, used_fee}
        """
        # 1) 列表接口：拿全站插座清单（outletNo / outletSerialNo）
        station_outlets: dict[int, list[dict]] = {}

        async def one(station_id: int):
            try:
                outlets = await self.api.fetch_station_outlets(station_id)
            except Exception as e:
                log.warning("[engine] outlets 请求失败 station=%s: %s", station_id, e)
                return
            station_outlets[station_id] = outlets

        async with self._sem:
            await asyncio.gather(*(one(sid) for sid in PLACES[place_code]["stations"]))

        # 2) 详情接口：逐插座实时判定（并发限 8）
        result: dict[str, dict] = {}
        tasks = []
        for station_id, outlets in station_outlets.items():
            for o in outlets:
                outlet_no = o.get("outletNo")
                if outlet_no:
                    tasks.append((station_id, outlet_no, o.get("outletSerialNo")))

        sem = asyncio.Semaphore(8)

        async def detail(station_id: int, outlet_no: str, serial):
            try:
                st = await self.api.fetch_outlet_status(outlet_no)
            except Exception as e:
                log.debug("[engine] 详情请求失败 %s: %s", outlet_no, e)
                return
            outlet = st.get("outlet") or {}
            err = outlet.get("iErrorCount") or 0
            fault = err > self.cfg.get("api", {}).get("fault_error_threshold", 5)
            result[outlet_no] = {
                "serial": serial,
                "available": (outlet.get("iCurrentChargingRecordId") == 0) and not fault,
                "fault": fault,
                "station_id": station_id,
                "charging_begin": st.get("chargingBeginTime"),
                "power_w": _parse_power((st.get("powerFee") or {}).get("billingPower")),
                "used_min": st.get("usedmin"),
                "used_fee": st.get("usedfee"),
            }

        async def guarded(station_id: int, outlet_no: str, serial):
            async with sem:
                await detail(station_id, outlet_no, serial)

        if tasks:
            await asyncio.gather(*(guarded(*t) for t in tasks))
        return result

    async def poll_place(self, place_code: str, *, snapshot_only: bool = False,
                         notify: bool = False, session_id: str | None = None,
                         record_events: bool = True) -> dict | None:
        """执行一轮轮询。返回该地点摘要 dict（结构见 _summary）。

        record_events=False：仅同步状态、不产生事件/提醒（用于建立基线）。
        """
        mon = self.monitors.setdefault(place_code, PlaceMonitor(place_code))
        await self.ensure_place_meta(place_code)

        now = self.api.now()
        outlets_map = await self._fetch_outlets_available(place_code, mon)
        if not outlets_map:
            log.warning("[engine] %s 本轮未获取到任何插座，跳过", place_code)
            return None

        # 与上一轮比对，产生事件
        for outlet_no, info in outlets_map.items():
            tracker = mon.trackers.get(outlet_no)
            if tracker is None:
                continue
            cur = info["available"]
            if info.get("fault") is not None:
                tracker.fault = info["fault"]
            # 每轮同步详情接口带回的充电信息（功率/费用/开始时间）
            if info.get("charging_begin"):
                tracker.charging_begin = info["charging_begin"]
            if info.get("power_w") is not None:
                tracker.power_w = info["power_w"]
            if info.get("used_min") is not None:
                tracker.used_min = info["used_min"]
            if info.get("used_fee") is not None:
                tracker.used_fee = info["used_fee"]
            if tracker.available is None:
                tracker.available = cur
                # 首次：若已在充电，确保开始时间已记录（供后续时长统计）
                if not cur and not tracker.charging_begin:
                    await self._capture_charging(tracker)
                continue
            tracker.prev_available = tracker.available
            tracker.available = cur
            if tracker.prev_available == cur:
                continue

            if not record_events:
                # 基线轮询：只同步状态，不产生事件/提醒
                continue

            # 状态翻转
            if cur:
                # 释放：占用 -> 空闲
                charge_minutes = None
                begin_ts = _parse_begin(tracker.charging_begin)
                if begin_ts is not None:
                    charge_minutes = max(0, int((now - begin_ts) / 60))
                tracker.last_release_ts = now
                self.db.insert_event(now, session_id, place_code, tracker.station_id,
                                     tracker.station_name, outlet_no, tracker.display_name(),
                                     "release", charge_minutes=charge_minutes)
                if notify:
                    await self._send_release(place_code, mon, tracker, now, session_id)
            else:
                # 占用：空闲 -> 占用
                idle_minutes = None
                if tracker.last_release_ts is not None:
                    idle_minutes = max(0, int((now - tracker.last_release_ts) / 60))
                if not tracker.charging_begin:
                    await self._capture_charging(tracker)
                self.db.insert_event(now, session_id, place_code, tracker.station_id,
                                     tracker.station_name, outlet_no, tracker.display_name(),
                                     "occupy", idle_minutes=idle_minutes)
                if notify:
                    await self._send_occupy(place_code, mon, tracker, now)

        # 快照入库（全部插座）
        for outlet_no, info in outlets_map.items():
            tracker = mon.trackers.get(outlet_no)
            if tracker is None:
                continue
            self.db.insert_snapshot(now, info["station_id"], outlet_no, tracker.available,
                                    tracker.charging_begin, tracker.power_w,
                                    tracker.used_min, tracker.used_fee)

        return self._summary(place_code, mon)

    async def _capture_charging(self, tracker: OutletTracker, update_only: bool = False):
        """调详情接口，记录充电开始时间/功率/费用。"""
        try:
            st = await self.api.fetch_outlet_status(tracker.outlet_no)
        except Exception as e:
            log.debug("status 请求失败 %s: %s", tracker.outlet_no, e)
            return
        outlet = st.get("outlet") or {}
        if not update_only or not tracker.charging_begin:
            tracker.charging_begin = st.get("chargingBeginTime") or tracker.charging_begin
        power_str = (st.get("powerFee") or {}).get("billingPower")
        tracker.power_w = _parse_power(power_str)
        tracker.used_min = st.get("usedmin")
        tracker.used_fee = st.get("usedfee")

    # ---------- 消息发送 ----------
    async def _send_release(self, place_code, mon, tracker, ts, session_id):
        summary = self._summary(place_code, mon)
        charge_minutes = None
        begin_ts = _parse_begin(tracker.charging_begin)
        if begin_ts is not None:
            charge_minutes = max(0, int((ts - begin_ts) / 60))
        text = messages.release_message(
            place_code, tracker.station_name, tracker.display_name(), ts,
            summary, charge_minutes,
        )
        await self._send(text)

    async def _send_occupy(self, place_code, mon, tracker, ts):
        summary = self._summary(place_code, mon)
        text = messages.occupy_message(
            place_code, tracker.station_name, tracker.display_name(), ts, summary,
        )
        await self._send(text)

    async def _send(self, text: str):
        if self.sender:
            try:
                await self.sender(text)
            except Exception as e:
                log.warning("[engine] 发送消息失败: %s", e)

    # ---------- 摘要 ----------
    def _summary(self, place_code: str, mon: PlaceMonitor | None = None) -> dict:
        mon = mon or self.monitors[place_code]
        by_station: dict[int, list[OutletTracker]] = {}
        for t in mon.trackers.values():
            if t.available is None:
                continue
            by_station.setdefault(t.station_id, []).append(t)
        stations = []
        total_avail = 0
        total = 0
        for station_id in PLACES[place_code]["stations"]:
            trackers = by_station.get(station_id, [])
            if not trackers:
                continue
            free = [t for t in trackers if t.available]
            faults = [t for t in trackers if t.fault]
            free_outlets = [t.display_name() for t in sorted(free, key=lambda x: x.outlet_no)]
            stations.append({
                "station_id": station_id,
                "name": trackers[0].station_name,
                "location": STATION_LOCATION.get(station_id, ""),
                "total": len(trackers),
                "free_count": len(free),
                "free_outlets": free_outlets,
                "fault_count": len(faults),
                "fault_outlets": [t.display_name() for t in sorted(faults, key=lambda x: x.outlet_no)],
            })
            total_avail += len(free)
            total += len(trackers)
        # 地点级：充电中插座按已充时长 Top5（供"无空闲"时的等待提示）
        charging = []
        now_t = self.api.now()
        for t in mon.trackers.values():
            if t.available is not None and not t.available and not t.fault and t.charging_begin:
                b = _parse_begin(t.charging_begin)
                if b is not None:
                    charging.append({"station_name": t.station_name, "outlet": t.display_name(),
                                     "minutes": max(0, int((now_t - b) / 60))})
        charging.sort(key=lambda x: x["minutes"], reverse=True)
        return {"name": place_name(place_code), "total": total, "available": total_avail,
                "stations": stations, "charging_top": charging[:5]}

    async def summaries(self, place_codes: list[str] | None = None) -> dict[str, dict]:
        codes = place_codes or list(PLACES)
        result = {}
        for code in codes:
            mon = self.monitors.setdefault(code, PlaceMonitor(code))
            await self.ensure_place_meta(code)
            # 尚无任何实时状态（如刚重启、首轮采集未跑）时，主动拉一轮，
            # 避免"当前状态"显示 0/0
            if not any(t.available is not None for t in mon.trackers.values()):
                await self.poll_place(code, snapshot_only=True, record_events=False, notify=False)
            if mon.trackers:
                result[code] = self._summary(code, mon)
        return result

    # ---------- 会话 ----------
    async def start_session(self, session_id: str, place_codes: list[str]) -> str:
        summaries = {}
        for code in place_codes:
            await self.ensure_place_meta(code)
            # 建立基线（不产生事件/提醒，确保会话统计只算监控期间的真实变化）
            await self.poll_place(code, snapshot_only=True, record_events=False)
            mon = self.monitors[code]
            summary = self._summary(code, mon)
            if summary["total"] > 0:
                summaries[code] = summary
            self.place_sessions[code].add(session_id)
        self.sessions[session_id] = Session(session_id, place_codes, created_at=self.api.now())
        self.db.insert_session(session_id, ",".join(place_codes))
        return messages.start_message(place_codes, summaries, self.cfg["monitor"]["poll_interval_seconds"])

    async def end_session(self, session_id: str, by_timeout: bool = False) -> str | None:
        sess = self.sessions.pop(session_id, None)
        if not sess:
            return None
        for code in sess.places:
            sids = self.place_sessions.get(code)
            if sids and session_id in sids:
                sids.discard(session_id)
        self.db.end_session(session_id)
        now = self.api.now()
        duration = max(0, int((now - sess.created_at) / 60))
        events = self.db.query(
            "SELECT * FROM events WHERE session_id = ? AND ts >= ? AND ts <= ?",
            (session_id, sess.created_at, now),
        )
        releases = sum(1 for e in events if e["event_type"] == "release")
        occupies = sum(1 for e in events if e["event_type"] == "occupy")
        if by_timeout:
            names = "、".join(place_name(c) for c in sess.places)
            return (f"⏰ 监控「{names}」已超时自动结束（持续 {duration} 分钟）。"
                    f"如需继续，重新说「xx充电」即可。")
        return messages.end_message(sess.places, duration, releases, occupies)

    def active_places(self) -> list[str]:
        return [code for code, sids in self.place_sessions.items() if sids]

    # ---------- 后台循环 ----------
    async def monitor_loop(self, poll_interval: int):
        while True:
            try:
                places = self.active_places()
                for code in places:
                    session_id = next(iter(self.place_sessions[code]), None)
                    await self.poll_place(code, notify=True, session_id=session_id)
            except Exception as e:
                log.exception("[engine] monitor_loop 出错: %s", e)
            await asyncio.sleep(poll_interval)

    async def collector_loop(self):
        while True:
            await asyncio.sleep(self._next_collect_interval())
            if not self.stats_enabled:
                log.info("[engine] 数据统计已暂停，跳过本轮采集")
                continue
            try:
                active = set(self.active_places())
                for code in PLACES:
                    if code in active:
                        continue  # 监控会话正在轮询，避免重复请求
                    await self.poll_place(code, notify=False)
            except Exception as e:
                log.exception("[engine] collector_loop 出错: %s", e)

    def _next_collect_interval(self) -> int:
        """按北京时间时段返回采集间隔：凌晨 1:00–8:00 用夜间间隔（默认 360s），其余用白天间隔（默认 180s）。"""
        mon = self.cfg.get("monitor", {})
        day = int(mon.get("collect_interval_seconds", 180))
        night = int(mon.get("collect_night_interval_seconds", 360))
        start = int(mon.get("collect_night_start_hour", 1))
        end = int(mon.get("collect_night_end_hour", 8))
        h = datetime.fromtimestamp(self.api.now(), tz=_BEIJING_TZ).hour
        return night if start <= h < end else day

    async def timeout_checker(self, timeout_minutes: int):
        while True:
            await asyncio.sleep(60)
            now = self.api.now()
            for sid, sess in list(self.sessions.items()):
                if now - sess.created_at > timeout_minutes * 60:
                    msg = await self.end_session(sid, by_timeout=True)
                    if msg:
                        await self._send(msg)


def _parse_power(power_str: str | None) -> int | None:
    """'180W' / '1.2kW' -> 瓦特数。"""
    if not power_str:
        return None
    s = power_str.strip().lower()
    try:
        if s.endswith("kw"):
            return int(float(s[:-2]) * 1000)
        if s.endswith("w"):
            return int(float(s[:-1]))
    except ValueError:
        return None
    return None


def new_session_id() -> str:
    return f"s{uuid.uuid4().hex[:12]}"
