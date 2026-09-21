# charge-monitor · 充电桩监控与数据分析

基于「闪开来电」API（`wemp.issks.com`）的个人充电桩监控服务。

- **QQ 机器人提醒**：给机器人说「我要在主楼充电」，即开始按分钟监控该地点的全部充电桩；有空闲桩被释放或占用时，私聊推送提醒；回「结束监控 / 我充上了」即停止。
- **后台数据采集**：7×24 采集三个地点（硕丰十组团 / 主楼C区停车场 / 学子餐厅）共 19 个站、约 150 个插座的状态快照，为可视化面板积累数据。
- **可视化面板**：静态 HTML + ECharts，双击打开即可查看占用率曲线、高峰低峰、平均充电/空闲时长等。

## 目录结构

```
charge-monitor/
├── run.py                  # 主入口（QQ 机器人 + 监控 + 采集）
├── config.example.json     # 配置模板 → 复制为 config.json
├── monitor/
│   ├── api.py              # 闪开来电 API 客户端（异步）
│   ├── db.py               # SQLite 数据层
│   ├── engine.py           # 监控引擎（轮询/状态机/事件）
│   ├── nlu.py              # 指令解析（口语化指令）
│   ├── messages.py         # 提醒消息模板
│   ├── qqbot.py            # QQ 官方机器人接入（botpy WS）
│   └── stations.py         # 三地点站点清单（已实测核验）
├── dashboard/
│   ├── index.html          # 可视化面板（双击打开）
│   └── render.py           # 从 SQLite 聚合生成 data.js
├── scripts/
│   ├── seed_meta.py        # 拉取插座元数据（自动运行，也可手动）
│   └── gen_dashboard.py    # 生成面板数据
└── data/                   # SQLite 数据库目录
```

## 快速开始

### 1. 注册 QQ 开放平台机器人（一次性的，约 20 分钟）

1. 打开 <https://q.qq.com/> ，用你的 QQ 扫码登录，点击「创建机器人」。
   - 开发者资质：个人即可，需「个人身份证认证」（免费，填身份信息 + 人脸验证）。
2. 填写机器人名称/头像/简介（如「充电桩小助手」），创建完成。
3. 进入机器人管理后台 →「开发设置」：
   - 记录 **AppID** 与 **AppSecret**（AppSecret 可点击重置/查看）。
   - 在「消息接收方式」选择 **WebSocket**（无需公网服务器回调地址）。
4. 「开发设置」→「沙箱配置」→ 把你的 QQ 号添加为**沙箱用户**（自用只需沙箱，无需上架审核）。
5. 「事件订阅」→ 确保勾选 **C2C 单聊消息**（群与单聊事件）权限（`GROUP_AND_C2C_EVENT`）。
6. （可选，建议）「开发设置」→「机器人公开信息」保持沙箱状态即可。

> 沙箱环境下机器人发消息不受频控限制，自用完全够用；若上架正式环境，C2C 主动消息每月仅 4 条，不适合本场景。

### 2. 配置

```powershell
copy config.example.json config.json
```

编辑 `config.json`：

```json
{
  "qq": {
    "appid": "你的 AppID",
    "secret": "你的 AppSecret",
    "is_sandbox": true,
    "allow_openids": []
  },
  "monitor": {
    "poll_interval_seconds": 60,
    "collect_interval_seconds": 300,
    "session_timeout_minutes": 360
  }
}
```

- `allow_openids`：你的 QQ 对应的 openid（留空=接受任意沙箱用户；建议首次运行后把日志里打印的 openid 填进去，更安全）。
- `poll_interval_seconds`：监控轮询间隔（默认 60 秒）。
- `collect_interval_seconds`：后台采集间隔（默认 300 秒，即 5 分钟）。

### 3. 安装依赖并启动

```powershell
python -m pip install -r requirements.txt
python run.py
```

保持窗口常开（或参考下方「开机自启」）。启动后：
1. 机器人会自动拉取三地点插座元数据并开始后台采集；
2. 在你的 QQ 里搜索机器人名称，添加好友，私聊「我要在主楼充电」。

### 4. 生成可视化面板

```powershell
python scripts/gen_dashboard.py --days 14
```

然后双击 `dashboard/index.html` 查看。数据积累 1–2 周后指标才有统计意义。

## 机器人指令

| 指令示例 | 行为 |
|---|---|
| 我要在硕丰十组团充电 / 主楼充电 / 学子餐厅充电 | 开始监控对应地点 |
| 硕丰和主楼都帮我看着 | 同时监控多个地点 |
| 现在有空吗 / 主楼什么情况 | 查询当前空闲情况 |
| 结束监控 / 我充上了 / 不用看了 | 停止全部监控 |
| 帮助 | 查看指令说明 |

监控期间只对**状态变化**（释放/占用）推送消息；释放提醒会附带该站全部空闲插座编号与全地点空闲汇总。

## 开机自启（Windows）

1. 在「任务计划程序」中新建任务：
   - 触发器：登录时；
   - 操作：启动程序 `python`，参数 `E:\VibeCodingProjects\charge\charge-monitor\run.py`，起始目录 `E:\VibeCodingProjects\charge\charge-monitor`；
   - 条件：取消「只在交流电源下启动」勾选。
2. 数据库自动保留最近 90 天快照（`config.json` 的 `snapshot_keep_days` 可调）。

## 数据说明

- 状态快照：每轮轮询写入 `snapshots` 表（插座 × 时间 × 空闲/占用 × 充电信息）。
- 状态变化事件：`events` 表记录每次释放/占用及充电/空闲时长，是分析的核心数据。
- 空闲判定：以「插座详情接口」为准（`outlet.iCurrentChargingRecordId == 0`）。注意列表接口的 `currentChargingRecordId` 是陈旧缓存，实测会把已空闲的插座标为占用，不可用于实时判定。
- 接口请求量：后台采集每 5 分钟对全部 152 个插座各查一次详情（约 4.5 万次/天）；监控进行时仅对监控地点每 60 秒轮询（主楼约 2.4 万次/天、学子约 7.2 万次/天）。觉得太多可在 `config.json` 调大 `collect_interval_seconds`（如 600）。

## 常见问题

- **机器人收不到消息**：确认 QQ 机器人已添加好友、沙箱配置包含你的 QQ、事件订阅勾选 C2C。
- **提醒发不出去**：沙箱下一般不受限；若日志出现 `msg limit exceed`，说明触发了频控，降低提醒频率或检查沙箱配置。
- **面板没有数据**：先跑 `python scripts/seed_meta.py` 确认元数据已入库，再等几轮采集后重新 `gen_dashboard.py`。
