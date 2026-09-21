# 充电桩监控机器人 · 项目总结

> 面向代码交接/学习的项目说明文档。本文档不包含任何真实凭据，打包给同学前请按文末《打包与脱敏说明》操作。

---

## 1. 项目简介

一个运行在本地电脑上的 **QQ 机器人 + 充电桩监控 + 数据采集分析** 系统：

- **监控**：对指定充电地点的每个插座做高频轮询，空闲桩被释放或占用时，通过 QQ 私聊主动提醒；
- **采集**：7×24 后台采集插座状态（空闲/充电中/故障、功率、费用、充电时长），存 SQLite；
- **分析**：基于积累数据生成静态可视化面板（占用率曲线、充电/空闲时长、热度图、站点对比等）。

上游数据源为「闪开来电」充电桩小程序的无鉴权 HTTP 接口（`wemp.issks.com`），本项目不依赖官方 SDK。

机器人接入 QQ 官方开放平台（botpy，沙箱环境，不受 C2C 主动消息频控限制）。

---

## 2. 功能特性

| 功能 | 说明 |
|---|---|
| 口语化指令 | 「主楼充电」开始监控 /「结束监控」停止 /「现在有空吗」查状态 /「学子位置」发位置图 /「停止统计」暂停采集 |
| 主动提醒 | 每 45 秒轮询监控中地点，**仅状态变化时**推送：释放（🟢）/占用（🔴） |
| 空闲明细 | 提醒消息附带该地点全部空闲插座完整信息（✅ 标记） |
| 故障标注 | 上游 `iErrorCount` 超过阈值判为故障桩（⚠️），不计入空闲 |
| 充电中 Top5 | 地点全占时，列出已充最久的 5 个插座（最接近释放），附已充时长 |
| 分时段采集 | 白天 3 分钟一轮、凌晨 1–8 点 6 分钟一轮；监控中地点 45 秒 |
| 时间校准 | 用上游接口返回的服务器时间校准本地时钟偏差，记录统一标准北京时间 |
| 数据面板 | 静态 HTML + ECharts，双击打开，`gen_dashboard.py` 一键更新 |

---

## 3. 系统架构

```
┌─────────────┐   WS 长连接    ┌──────────────────────────────┐
│  QQ 开放平台 │◄──────────────►│  ChargeBot (botpy.Client)     │
│  (沙箱)      │   C2C 消息     │  on_c2c_message_create       │
└─────────────┘                │   └─ NLU 解析 → 分发          │
                               └──────────────┬───────────────┘
                                              │ 事件提醒(sender回调)
┌─────────────────┐   HTTP(无鉴权)   ┌────────▼───────────────┐
│ 闪开来电 API     │◄────────────────│  MonitorEngine          │
│ wemp.issks.com  │  轮询 152 插座   │  monitor_loop (45s)     │
└─────────────────┘                 │  collector_loop (3/6min)│
                                    │  timeout_checker        │
                                    └───────────┬─────────────┘
                                                │ 读写
                                    ┌───────────▼─────────────┐
                                    │  SQLite (monitor.db)     │
                                    │  outlet_meta/snapshots/  │
                                    │  events/sessions         │
                                    └───────────┬─────────────┘
                                                │ 聚合
                                    ┌───────────▼─────────────┐
                                    │  dashboard (render.py → │
                                    │  data.js → index.html)  │
                                    └─────────────────────────┘
```

数据流：`轮询上游 → 状态比对 → 事件入库 + 快照入库 → 主动提醒 / 面板聚合`。

---

## 4. 目录结构

```
charge-monitor/
├── run.py                  # 主入口：启动机器人 + 后台循环
├── config.json             # 真实配置（凭据，不打包/提交）
├── config.example.json     # 配置模板（占位符，打包用这个）
├── requirements.txt        # 依赖：qq-botpy / aiohttp
├── pip.conf                # 项目级 pip 镜像（USTC）
├── README.md               # 部署文档（QQ 开放平台注册步骤）
├── PROJECT_SUMMARY.md      # 本文档
├── monitor/                # 核心代码（7 个模块）
│   ├── api.py              #   上游 HTTP 客户端 + 时间校准
│   ├── config.py           #   配置加载
│   ├── db.py               #   SQLite 层（自动建表/建目录）
│   ├── engine.py           #   监控引擎（轮询/事件/会话/统计）
│   ├── messages.py         #   消息模板（简化站名/空闲明细/Top5）
│   ├── nlu.py              #   指令解析（关键词表驱动）
│   ├── qqbot.py            #   QQ 机器人接入与分发
│   └── stations.py         #   地点/站点清单 + 位置描述
├── dashboard/              # 可视化面板
│   ├── render.py           #   聚合 SQLite → data.js
│   ├── index.html          #   面板页面（ECharts，双击打开）
│   └── data.js             #   生成的数据（不打包，运行 gen_dashboard 生成）
├── scripts/
│   ├── seed_meta.py        #   重灌插座元数据（站点清单变更后运行）
│   ├── gen_dashboard.py    #   生成面板数据
│   └── smoke_test.py       #   冒烟测试（NLU 用例 + 真实 API 轮询）
└── data/                   # 运行数据（SQLite/地图图，自动创建）
```

---

## 5. 核心模块说明

### monitor/api.py — 上游客户端
- 3 个接口：附近站点（POST `/device/v1/near/station`）、站点插座列表（GET `/charge/v1/outlet/station/outlets/{stationId}`）、插座详情（GET `/charge/v1/charging/outlet/{outletNo}`）；
- **时间校准**：详情接口响应带服务器时间 `curTime`（毫秒），每次请求后计算 offset，`api.now()` 返回校准后的标准北京时间——不依赖本地电脑时钟。

### monitor/engine.py — 监控引擎
- `OutletTracker`：单插座状态机（空闲/占用/故障、充电开始时间、功率费用）；
- `poll_place()`：一轮轮询 = 拉全部站插座列表 → 逐插座详情判实时状态 → 与上轮比对 → 状态翻转写事件（release/occupy）→ 全部写快照；
- `start_session()/end_session()`：监控会话（开始基线轮询不产生事件，结束汇总提醒次数）；
- `collector_loop()`：后台采集，按北京时间时段切换间隔（默认白天 180s / 凌晨 1–8 点 360s）；
- `_summary()`：聚合出地点摘要（含充电中 Top5，供无空闲提示）。

### monitor/nlu.py — 指令解析
关键词表驱动 + 优先级（统计开关 > 停止 > 位置/地图 > 帮助 > 状态/开始）。命中地点关键词（硕丰/主楼/学子）返回对应 code。新增指令只需扩展关键词表。

### monitor/messages.py — 消息模板
- 站名简化：「电子科大清水河校区学子餐厅5号充电桩」→「5号充电桩」；
- 空闲行 `✅ X号充电桩：空闲 插座4（1/8）`、故障行 `⚠️ ... 故障：插座3`、全占行 `充电中`；
- 无空闲时输出「充电中 Top5（最接近释放）」。

### monitor/db.py — SQLite 层
自动建目录/建表；线程锁保证并发安全。表：`outlet_meta`（插座元数据）、`snapshots`（快照）、`events`（状态事件）、`sessions`（会话）。

---

## 6. 上游 API 与关键经验（重点）

1. **无鉴权**：`wemp.issks.com` 接口不带 token 即可调用，但有签名/风控风险，仅供学习；
2. **列表接口的空闲字段不可信**：`currentChargingRecordId` 是**陈旧缓存**（实测空闲插座可能被标为非 0）。空闲判定必须用**详情接口** `outlet.iCurrentChargingRecordId == 0`；
3. **故障判定**：详情接口 `outlet.iErrorCount` 为故障计数（故障桩 25，正常 0~1），阈值取 `api.fault_error_threshold`（默认 5），故障桩不计入空闲并在消息中 ⚠️ 标注；
4. **请求量**：152 个插座每轮逐个调详情接口（并发限 8）。3 分钟一轮 ≈ 7.6 万次/天，注意上游限流；
5. **时间基准**：本地电脑时钟可能不准，一律用上游 `curTime` 校准。

---

## 7. 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| `outlet_meta` | station_id, outlet_no, outlet_name, place_code | 插座元数据（seed 灌入） |
| `snapshots` | ts, station_id, outlet_no, available, charging_begin, power_w, used_min, used_fee | 每轮全量快照 |
| `events` | ts, session_id, place_code, station_id, outlet_no, event_type(release/occupy), charge_minutes, idle_minutes | 状态翻转事件 |
| `sessions` | session_id, places, created_at, ended_at | 监控会话 |

统计口径：
- 平均充电时长 = release 事件 `charge_minutes` 均值；
- 平均空闲时间 = occupy 事件 `idle_minutes` 均值（插座从释放到再被占用）；
- 预期等待时间（规划中）= 地点「全占持续时长」历史均值（从全被占用到出现空闲的间隔）。

**断档排除（重要）**：服务停机等"未被记录"的时段不参与统计——
- 占用率/热力图按"有快照的采样轮次"占比，断档小时无快照、不计入；
- 日转次/桩 按该地点实际有快照的小时数计算有效天数，断档小时不算；
- 平均充电/空闲时长只统计"事件时间窗内快照覆盖跨度 ≥ 窗长 80%"的事件，
  跨断档事件（充电开始于停机前、释放于重启后）会被剔除；
- 时间桶按固定 UTC+8 划分，不依赖系统时区；时间锚点取数据库最新快照时间，不用本地时钟。

---

## 8. 部署运行

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt        # qq-botpy、aiohttp

# 2. 配置
cp config.example.json config.json     # 填入 QQ 机器人 AppID/Secret（见 README）

# 3. 初始化插座元数据（首次/站点清单变更后）
python scripts/seed_meta.py

# 4. 启动
python run.py
```

启动后机器人自动连接 QQ 网关，后台采集立即开始。日志输出在 `botpy.log`。

---

## 9. QQ 机器人配置要点

- 在 [QQ 开放平台](https://q.qq.com) 创建机器人，**必须选沙箱环境**（正式环境 C2C 主动消息每月仅 4 条，沙箱不受频控）；
- `config.json` 填 `appid` / `secret`，`is_sandbox: true`；
- 机器人通过 WS 长连接接收私聊（`on_c2c_message_create`），主动提醒用 `post_c2c_message`；
- 位置图发送：`post_c2c_file` 上传公网图片 URL → `post_c2c_message(msg_type=7)`；
- `allow_openids` 可填白名单（沙箱下留空则接受任意可达用户）。

---

## 10. 数据分析面板

```bash
python scripts/gen_dashboard.py        # 聚合 SQLite → dashboard/data.js
# 然后双击 dashboard/index.html（或刷新已打开页面）
```

面板指标：24h 充电次数、平均占用率、日转（次/桩）、按小时占用率曲线、平均充电/空闲时长、星期×小时热度图、站点对比表。

---

## 11. 已知限制与路线图

- **等待时间**：只能近似为「地点全占持续时长」（无用户到达行为数据），等数据积累 3–5 天后可产出「地点×时段」可信统计，再接入机器人查询；
- **账号识别**：上游不暴露充电账号，无法回答"这个账号上次充了多久"；可用本库历史数据近似（同一插座上次充电时长）；
- **采样精度**：采集间隔内（3/6 分钟）的状态变化时间精度受间隔限制；
- **上游稳定性**：无鉴权接口有被限流/变更风险，请求失败会自动跳过重试。

---

## 12. 打包与脱敏说明（给同学前必读）

**打包时删除以下文件/目录**（含真实凭据或隐私数据）：

| 文件 | 原因 |
|---|---|
| `config.json` | 真实 AppID / Secret / 地图 URL |
| `botpy.log*` | 运行日志（含连接与消息信息） |
| `data/monitor.db`、`data/smoke_test.db` | 真实采集数据（充电行为数据） |
| `data/upload2.txt`、`data/upload_check.png`、`data/dash_shot.png` | 个人临时文件（upload2.txt 含疑似凭据字符串） |
| `data/map_*.png` | 个人充电地点位置图（同学可自行配置自己的图） |

**保留**：`run.py`、`requirements.txt`、`pip.conf`、`config.example.json`、`README.md`、`PROJECT_SUMMARY.md`、`monitor/`（全部）、`dashboard/index.html` + `render.py`（不含 data.js）、`scripts/`（全部）。

同学拿到后：复制 `config.example.json` 为 `config.json` 填入自己的机器人凭据，`python scripts/seed_meta.py` 初始化，`python run.py` 即可运行（data/ 目录自动创建）。
