"""三个关注地点的站点/插座元数据。

站点清单与 stationId 来自对「闪开来电」near/station 接口的实测核验（2026-09-11）。
插座级元数据（outletNo / outletName）首次运行时由 scripts/seed_meta.py 从 API 拉取并写入数据库。
"""

# place_code -> 地点信息与站点清单
PLACES: dict[str, dict] = {
    "shuofeng": {
        "name": "硕丰十组团",
        "stations": {
            135601: "硕丰十组团7号充电桩",
            135646: "硕丰十组团6号充电桩",
            172927: "硕丰十组团1号充电桩",
            172931: "硕丰十组团5号充电桩",
            172973: "硕丰十组团3号充电桩",
            172979: "硕丰十组团2号充电桩",
            73266: "电子科大清水河校区硕丰十组团充电桩",
        },
    },
    "zhulou": {
        "name": "主楼C区停车场",
        "stations": {
            74677: "电子科大清水河校区主楼C 区停车场1号充电桩",
            74719: "电子科大清水河校区主楼C 区停车场3号充电桩",
            74746: "电子科大清水河校区主楼C 区停车场2号充电桩",
        },
    },
    "xuezi": {
        "name": "学子餐厅",
        "stations": {
            40167: "电子科大清水河校区学子餐厅辅道2号电站",
            54468: "电子科大清水河校区学子餐厅辅道4号电站",
            56937: "电子科大清水河校区学子餐厅辅路1号电站",
            67922: "电子科大清水河校区学子餐厅9号充电桩",
            73096: "电子科大清水河校区学子餐厅6号充电桩",
            73262: "电子科大清水河校区学子餐厅7号充电桩",
            73264: "电子科大清水河校区学子餐厅8号充电桩",
            67924: "电子科大清水河校区学子餐厅5号充电桩",
            231328: "电子科大清水河校区学子餐厅辅路3号电站",
        },
    },
}

# 地点 -> 关键词（用于 NLU 模糊匹配，命中任一关键词即认为指向该地点）
PLACE_KEYWORDS: dict[str, list[str]] = {
    "shuofeng": ["硕丰", "十组团", "十组", "shuofeng"],
    "zhulou": ["主楼", "主楼c", "主楼c区", "c区", "c 区", "停车场"],
    "xuezi": ["学子", "学子餐厅", "xuezi"],
}

# 地点关键词的歧义处理：这些关键词过泛，只有同时命中明确地点词时才采用
WEAK_KEYWORDS: set[str] = {"停车场", "主楼"}

# 各充电桩的相对位置描述（基于用户提供的平面图，2026-09-11 初步标注，待用户校正）。
# 消息里会显示为「7号充电桩（北侧）：空闲 ...」。未配置的站不显示位置。
STATION_LOCATION: dict[int, str] = {
    # 主楼C区停车场：图中绿标 1/2/3 对应 1/2/3 号充电桩
    74677: "北侧",
    74746: "西南",
    74719: "东侧",
    # 学子餐厅：图中西侧从上到下标注 7、6、5、*、3、1（* 疑为 8 号桩）
    73262: "西侧·最北",
    73096: "西侧·北",
    67924: "西侧·中北",
    73264: "西侧·中",
    # 硕丰十组团：左下角集中充电区（无编号站）；1~7 号桩分布待用户确认
    73266: "左下集中区",
}


def place_name(code: str) -> str:
    return PLACES[code]["name"]


def all_station_ids() -> list[int]:
    ids: list[int] = []
    for place in PLACES.values():
        ids.extend(place["stations"].keys())
    return ids


def station_place(station_id: int) -> str | None:
    for code, place in PLACES.items():
        if station_id in place["stations"]:
            return code
    return None


def station_name_of(station_id: int) -> str | None:
    for place in PLACES.values():
        if station_id in place["stations"]:
            return place["stations"][station_id]
    return None
