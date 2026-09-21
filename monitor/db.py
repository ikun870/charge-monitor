"""SQLite 数据层。

表：
- outlet_meta  插座元数据（由 seed 脚本从 API 拉取）
- snapshots    每轮轮询的状态快照（append-only）
- events       状态变化事件（release 释放 / occupy 占用）
- sessions     监控会话（用户指令驱动的监控周期）
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outlet_meta (
    station_id   INTEGER NOT NULL,
    station_name TEXT NOT NULL,
    place_code   TEXT NOT NULL,
    outlet_no    TEXT NOT NULL,
    outlet_serial INTEGER,
    outlet_name  TEXT,
    PRIMARY KEY (station_id, outlet_no)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,          -- unix 秒
    station_id     INTEGER NOT NULL,
    outlet_no      TEXT NOT NULL,
    available      INTEGER NOT NULL,       -- 1 空闲 0 占用
    charging_begin TEXT,
    power_w        INTEGER,
    used_min       INTEGER,
    used_fee       REAL
);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_snap_outlet ON snapshots(outlet_no, ts);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    session_id   TEXT,
    place_code   TEXT,
    station_id   INTEGER NOT NULL,
    station_name TEXT,
    outlet_no    TEXT NOT NULL,
    outlet_name  TEXT,
    event_type   TEXT NOT NULL,            -- release / occupy
    charge_minutes INTEGER,                -- 本次充电时长（release 时，分钟）
    idle_minutes   INTEGER                 -- 空闲时长（occupy 时，分钟）
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS sessions (
    id         TEXT PRIMARY KEY,
    place_code TEXT NOT NULL,
    status     TEXT NOT NULL,              -- active / ended
    created_at REAL NOT NULL,
    ended_at   REAL
);
"""


class Database:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()

    # ---------- 元数据 ----------
    def upsert_outlet_meta(self, station_id: int, station_name: str, place_code: str,
                           outlet_no: str, outlet_serial: int | None, outlet_name: str | None):
        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO outlet_meta
                   (station_id, station_name, place_code, outlet_no, outlet_serial, outlet_name)
                   VALUES (?,?,?,?,?,?)""",
                (station_id, station_name, place_code, outlet_no, outlet_serial, outlet_name),
            )
            self._conn.commit()

    def get_outlet_meta(self, place_code: str | None = None, station_id: int | None = None) -> list[dict]:
        sql = "SELECT * FROM outlet_meta"
        args: list = []
        conds = []
        if place_code:
            conds.append("place_code = ?")
            args.append(place_code)
        if station_id:
            conds.append("station_id = ?")
            args.append(station_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY station_id, outlet_serial"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---------- 快照 ----------
    def insert_snapshot(self, ts: float, station_id: int, outlet_no: str, available: bool,
                        charging_begin: str | None, power_w: int | None,
                        used_min: int | None, used_fee: float | None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO snapshots (ts, station_id, outlet_no, available, charging_begin, power_w, used_min, used_fee) VALUES (?,?,?,?,?,?,?,?)",
                (ts, station_id, outlet_no, 1 if available else 0, charging_begin, power_w, used_min, used_fee),
            )
            self._conn.commit()

    def purge_snapshots(self, keep_days: int):
        cutoff = time.time() - keep_days * 86400
        with self._lock:
            cur = self._conn.execute("DELETE FROM snapshots WHERE ts < ?", (cutoff,))
            self._conn.commit()
            return cur.rowcount

    # ---------- 事件 ----------
    def insert_event(self, ts: float, session_id: str | None, place_code: str | None,
                     station_id: int, station_name: str | None, outlet_no: str,
                     outlet_name: str | None, event_type: str,
                     charge_minutes: int | None = None, idle_minutes: int | None = None):
        with self._lock:
            self._conn.execute(
                """INSERT INTO events (ts, session_id, place_code, station_id, station_name, outlet_no, outlet_name, event_type, charge_minutes, idle_minutes)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (ts, session_id, place_code, station_id, station_name, outlet_no, outlet_name,
                 event_type, charge_minutes, idle_minutes),
            )
            self._conn.commit()

    # ---------- 会话 ----------
    def insert_session(self, session_id: str, place_code: str):
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (id, place_code, status, created_at) VALUES (?,?, 'active', ?)",
                (session_id, place_code, time.time()),
            )
            self._conn.commit()

    def end_session(self, session_id: str):
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET status='ended', ended_at=? WHERE id=?",
                (time.time(), session_id),
            )
            self._conn.commit()

    def list_sessions(self, status: str | None = None) -> list[dict]:
        sql = "SELECT * FROM sessions"
        args: list = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    # ---------- 查询（dashboard 用） ----------
    def snapshot_range(self, since: float | None = None, until: float | None = None) -> list[dict]:
        sql = "SELECT * FROM snapshots"
        args: list = []
        conds = []
        if since:
            conds.append("ts >= ?")
            args.append(since)
        if until:
            conds.append("ts <= ?")
            args.append(until)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY ts"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def event_range(self, since: float | None = None, until: float | None = None,
                    event_type: str | None = None) -> list[dict]:
        sql = "SELECT * FROM events"
        args: list = []
        conds = []
        if since:
            conds.append("ts >= ?")
            args.append(since)
        if until:
            conds.append("ts <= ?")
            args.append(until)
        if event_type:
            conds.append("event_type = ?")
            args.append(event_type)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY ts"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]

    def query(self, sql: str, args: tuple = ()) -> list[dict]:
        """任意只读 SQL 查询（供聚合脚本使用）。"""
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(r) for r in rows]
