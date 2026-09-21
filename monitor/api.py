"""「闪开来电」上游 API 客户端（异步 aiohttp）。

接口（实测自 wemp.issks.com，2026-09-11）：
- POST /device/v1/near/station        附近充电站（含 freeNum 站点空闲数）
- GET  /charge/v1/outlet/station/outlets/{stationId}   站点插座列表
      插座是否空闲：currentChargingRecordId == 0
- GET  /charge/v1/charging/outlet/{outletNo}           单个插座详情
      空闲判断：outlet.iCurrentChargingRecordId == 0
      充电信息：chargingBeginTime / usedmin / usedfee / powerFee.billingPower

时间校准：详情接口响应含服务器时间 curTime（毫秒时间戳）。本地电脑时钟可能有偏差，
因此用 curTime 计算 offset，engine 记录数据统一使用校准后的北京时间（now()）。
"""
from __future__ import annotations

import math
import time

import aiohttp

from .config import load_config

DEFAULT_BASE = "https://wemp.issks.com"


class ApiError(Exception):
    pass


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """WGS84 -> GCJ-02（中国国测局坐标偏移），上游接口使用 GCJ-02。"""
    a = 6378245.0
    ee = 0.00669342162296594323

    def _lat(x: float, y: float) -> float:
        ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
        return ret

    def _lng(x: float, y: float) -> float:
        ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
        ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
        return ret

    if lng < 72.004 or lng > 137.8347 or lat < 0.8293 or lat > 55.8271:
        return lat, lng

    d_lat = _lat(lng - 105.0, lat - 35.0)
    d_lng = _lng(lng - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - ee * magic * magic
    sqrt_magic = math.sqrt(magic)
    d_lat = (d_lat * 180.0) / ((a * (1 - ee)) / (magic * sqrt_magic) * math.pi)
    d_lng = (d_lng * 180.0) / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lat + d_lat, lng + d_lng


class FlashApi:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or load_config()
        self.base = cfg.get("api", {}).get("base_url", DEFAULT_BASE)
        self.center_wgs84: tuple[float, float] = tuple(cfg["api"]["center_wgs84"])
        self.timeout = aiohttp.ClientTimeout(total=cfg["api"].get("request_timeout_seconds", 10))
        self._server_offset = 0.0  # 服务器时间 - 本地时间（秒）

    def now(self) -> float:
        """校准后的标准北京时间（Unix 时间戳，秒）。

        本地时钟可能有偏差；每轮请求若上游返回 curTime 会重新校准 offset。
        首次请求前 offset=0，用本地时间。
        """
        return time.time() + self._server_offset

    def _calibrate(self, payload) -> None:
        """从响应 payload 中提取 curTime（毫秒）校准时间偏移。"""
        if not isinstance(payload, dict):
            return
        cur = payload.get("curTime")
        if not isinstance(cur, (int, float)) or cur <= 0:
            return
        server_ts = cur / 1000.0
        # 只接受合理偏移（<1 天），防止脏数据破坏时间基准
        if abs(server_ts - time.time()) < 86400:
            self._server_offset = server_ts - time.time()

    async def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            async with session.request(
                method, url, json=body,
                headers={"Content-Type": "application/json;charset=UTF-8"},
            ) as resp:
                try:
                    data = await resp.json(content_type=None)
                except Exception as e:
                    raise ApiError(f"上游返回非 JSON: {e}") from e
                if resp.status != 200:
                    raise ApiError(f"HTTP {resp.status}: {data}")
                if data.get("code") != "1":
                    raise ApiError(f"上游业务错误 code={data.get('code')} msg={data.get('msg')}")
                payload = data.get("data") or {}
                self._calibrate(payload)
                return payload

    async def fetch_station_outlets(self, station_id: int) -> list[dict]:
        """站点插座列表。返回字段含 outletNo / outletSerialNo / currentChargingRecordId / state。"""
        data = await self._request("GET", f"/charge/v1/outlet/station/outlets/{station_id}")
        return data if isinstance(data, list) else []

    async def fetch_outlet_status(self, outlet_no: str) -> dict:
        """单个插座详情。含 outlet.iCurrentChargingRecordId / chargingBeginTime / usedmin / usedfee / powerFee。"""
        data = await self._request("GET", f"/charge/v1/charging/outlet/{outlet_no}")
        return data if isinstance(data, dict) else {}

    async def fetch_near_stations(self) -> list[dict]:
        """附近充电站（用于核验站点清单，GCJ-02 坐标）。"""
        lat, lng = wgs84_to_gcj02(*self.center_wgs84)
        body = {
            "page": 1,
            "pageSize": 200,
            "scale": 3,
            "latitude": lat,
            "longitude": lng,
            "userLatitude": lat,
            "userLongitude": lng,
        }
        data = await self._request("POST", "/device/v1/near/station", body)
        return data.get("elecStationData") or []
