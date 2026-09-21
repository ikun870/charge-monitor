"""元数据种子脚本：从闪开来电 API 拉取三地点全部插座清单并写入数据库。

用法：
    python scripts/seed_meta.py [--config config.json]

种子数据包括：station_id / station_name / place_code / outlet_no / outlet_serial / outlet_name。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.api import FlashApi
from monitor.config import load_config
from monitor.db import Database
from monitor.stations import PLACES


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    args = parser.parse_args()

    cfg = load_config(args.config)
    db = Database(Path(cfg.get("data_dir", "data")) / "monitor.db")
    api = FlashApi(cfg)

    total = 0
    for code, place in PLACES.items():
        for station_id, station_name in place["stations"].items():
            outlets = await api.fetch_station_outlets(station_id)
            for o in outlets:
                outlet_no = o.get("outletNo")
                if not outlet_no:
                    continue
                name = None
                try:
                    st = await api.fetch_outlet_status(outlet_no)
                    name = st.get("outlet", {}).get("vOutletName")
                except Exception:
                    pass
                db.upsert_outlet_meta(station_id, station_name, code, outlet_no,
                                      o.get("outletSerialNo"), name)
                total += 1
        print(f"✓ {place['name']}: {len(db.get_outlet_meta(place_code=code))} 个插座")
    print(f"种子完成，共 {total} 条插座元数据。")


if __name__ == "__main__":
    asyncio.run(main())
