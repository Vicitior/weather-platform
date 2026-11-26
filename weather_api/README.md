# 气象数据查询 API

基于本地镜像（`db_mirror/`）的 1951–2025 全国 876 站逐日气象数据查询接口。
**离线可用**（不连任何数据库），之后可原样搬上网站（换个数据源即可）。

## 启动

```bash
cd "D:\Android PDF"
python -m weather_api.main            # 默认 0.0.0.0:8900，服务器外部可直接访问
python -m weather_api.main --port 9000
```

- 交互式文档（可直接试调用）：**http://127.0.0.1:8900/docs**
- 健康检查：**`GET /api/health`** → 200 可用 / 503 数据目录不可达
- 所有接口返回 `{"ok": true, "data": ...}`，出错 `{"ok": false, "error": "..."}`
- 响应 `charset=utf-8`，CORS 全开（网站前端可直接调）

> 部署到服务器见 [DEPLOY.md](DEPLOY.md)。

## 时间口径（重要）

源库时间戳被整体 **+100 年**（2051–2125 → 实际 1951–2025），本 API 已自动修正，
**所有传入/返回的日期都是真实历史年份**，直接用 `1951-01-01 ~ 2025-12-31`。

## 数据口径

- 排行/极值/聚合一律过滤哨兵值（32766/32767/999999 等）与超物理范围的值
- 日值接口返回原始值 + `is_valid` 标志（前端可自行决定是否展示可疑值）

## 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | **健康检查**（运维用，200=可用 / 503=数据目录不可达） |
| GET | `/api/overview` | 数据集概览：站点数/行数/时间范围 |
| GET | `/api/provinces` | 省份列表（已归一化）+ 站点数 |
| GET | `/api/stations` | 站点列表，支持 `province` / `search`（站名、站号、ID）/ 分页 |
| GET | `/api/stations/{id}` | 站点详情 + 数据覆盖 |
| GET | `/api/weather/daily` | **某站某天/某段时间的日值**（`station_id` + `date` 或 `start/end`） |
| GET | `/api/weather/day_rank` | **某天全部站点排行**（`date` + `metric` + `order`，可限省） |
| GET | `/api/records/station` | **某站历史极值 TopN**（含出现日期） |
| GET | `/api/records/global` | **全数据集极值 TopN**（可限年份/日期区间/省份） |
| GET | `/api/aggregate/annual` | 逐年统计（全国 / 单站 `station_id` / 分省 `province`） |
| GET | `/api/aggregate/monthly` | 某站某年逐月统计 |
| GET | `/api/trend` | 时间序列（`agg=day/month/year`），画趋势图直接用 |
| GET | `/api/climate/monthly` | 气候基准（默认 1991–2020 WMO 标准气候平均期逐月值） |
| GET | `/api/metrics` | 可用指标清单 |
| GET | `/api/weather/daily_multi` | **多站日值对比**（一次最多 20 站，`station_ids=1,2,3`） |
| GET | `/api/compare/periods` | **两段时期逐月气候对比**（默认 1961-1990 vs 1991-2020，看气候变化） |
| GET | `/api/stations/near` | **按经纬度找附近站点**（haversine 距离排序） |
| GET | `/api/stations/bbox` | **矩形范围内站点**（地图打点用） |
| GET | `/api/stats/threshold_days` | **阈值天数**（高温日/严寒日/暴雨日，按站/省/全国逐年） |
| GET | `/api/agri/station` | **农业气象**：逐年有效积温、≥10℃ 初终日、无霜期 |
| GET | `/api/agri/compare` | **多站农业气象对比**（多年平均，选种/引种参考） |
| GET | `/api/rank/year` | **某年站点排行**（含最少天数限制，防小样本） |
| GET | `/api/on_this_day` | **历史上的今天**（同月同日跨 75 年排行） |
| GET | `/api/debug/values_above` | 超阈值记录追查（数据质量排查用） |
| GET | `/api/report/table` | **气象数据填报表**（站点×12 月模板口径聚合 + 模板校验规则自动核查） |
| GET | `/api/report/export` | **填报表导出**成 Word(.docx)/Excel(.xlsx) 成品文件（exports/ 目录） |
| GET | `/api/repair/scan` | **缺测/异常扫描**：哨兵值（999999/32766）、超物理范围、max<min、缺行 |
| GET | `/api/repair/hidden_scan` | **隐性异常扫描**：物理范围内但可疑（超本站该月历史极值 / 与同日邻站显著偏离），**只报不修**，附判断依据与邻站实测值 |
| GET | `/api/repair/daily` | **异常值修复**：按坐标找最近 k 个邻站，用气候距平法重建（非简单复制最近站），空间法失效自动降级时间维 |
| GET | `/api/repair/batches` | **录入批次列表**（按上传时间戳倒序，用于发现最新一批新数据） |
| GET | `/api/repair/batch` | **批量补齐**：多站 × 一个区间一次跑完并汇总（`station_ids` 或 `province`） |
| GET | `/api/repair/new_batch` | **★ 新数据自动补齐**：自动定位最新录入批次，扫出坏值/空白并补齐，落 CSV 明细 |
| GET | `/api/repair/overview` | **全库质量巡检**：只扫描不修复，给出各站缺多少/坏多少的总账 |

**修复方法默认 `method=auto`**（逐指标选交叉验证最优法）：
气温三项/湿度/风速/日照 → `own6`（门控跨要素岭回归）；降水 → `clima`。
验证依据与全部实测数字见 [`异常值修复方法依据说明.md`](../异常值修复方法依据说明.md)
（59 站 hold-out：气温 MAE 0.75℃ vs 最近站 2.91℃；`own6` 门控通过时再降到 **0.38℃**）。

**可选 `method`**：

| method | 做法 | 实测（气温 MAE） | 说明 |
|---|---|---|---|
| `auto` | 逐指标选下表最优（默认） | — | 推荐入口 |
| `own6` | **门控跨要素岭回归**：本站其它 6 要素距平 + 邻站 7 要素距平 + 坐标/月相位，91 维岭回归 | **0.38℃**（其它 6 要素全可用时） | **精度最高**，但要求该日其它要素够多（见下门控表） |
| `clima` | 邻站值 +（目标站月基准 − 邻站月基准）；降水用 normal-ratio | 0.78℃ | **性价比最高**，也是 own6 的兜底 |
| `blend` | 0.5×clima + 0.5×本站前后日距平 | 0.78℃ | 湿度/风速曾经最优，已被 own6 超越 |
| `krig` | **距平克里金**：变差函数由邻站 1991-2020 长序列拟合，解普通克里金权重 | **0.74℃** | 统计上显著更优，但只比 clima 好约 5%，且要多查一次 30 年长序列 —— 故**不进 auto 默认** |
| `idw` | 距离反比平方加权 | 1.62℃ | — |
| `alt_idw` | idw + 温度海拔订正（6.5℃/km） | 1.05℃ | — |
| `nearest` | 直接取最近邻站原值 | 2.13℃ | 对照用，**实测最差** |

> `own6` 的**门控是必须的**：第十三轮 87 站分层验证发现，本站当日其它要素只剩 1~3 个可用时，
> 跨要素特征大量退化成常数，岭回归会系统性外推偏差，**比 clima 差 40%~90%**。
> 故按指标分别定门控（`OWN6_GATE`）：平均气温 ≥5 个、最高/最低气温 ≥6 个、湿度 ≥6、日照 ≥6、风速 ≥6；
> 不足门控自动退回 clima（零劣化）。**降水永不用 own6**（层 0 实测 +3818.9%）。
>
> `krig` 的**降水会自动退回 clima**：降水是间断非高斯事件（大量 0 + 少数暴雨），
> 加性插值天然吃亏，实测显著更差（+0.079mm）。变差函数拟合出的空间相关半径
> （降水 107km、气温 1243km、风速 409km）本身就说明「300km 内找邻站」是合理的。

**每个修复值都带 `quality` 可信度标注**：

| quality | 含义 | 可信度 |
|---|---|---|
| `observed` | 邻站**同日有实测**或本站其它要素可用，经气候距平/跨要素回归订正 | 最可信（漠河 2020 全部 11 个值） |
| `temporal` | 邻站当日也无有效值，改用**本站前后日距平** | 次之（区域性缺测日） |
| `climate` | 只能用**本月气候平均值** → 是气候态，**不是实况** | 仅供参考，勿当观测 |

**停更段保护**：本数据集里不少站是整段停更（2020 年有 576 站只记录到 4 月底）。
这类缺口用气候态填充等于编造观测，所以默认**拒绝填充**（`max_gap_days=7`），
只在 `summary.warnings` 里告知。要让它们出值必须显式放宽：

```bash
# 只修孤立坏值（默认，推荐）
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1129&year=2020"
#   → 坏日 245，只修复 7 个，1708 个无法修复（都是 5-12 月停更段）

# 强行让停更段也出气候态（会返回 climate 计数与警告，勿当观测用）
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1129&year=2020&max_gap_days=400"

# 彻底不要气候态（等价于默认 max_gap_days=7 的效果）
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1129&year=2020&allow_climate_fill=false"
```

`metric` 可选值：`avg_temperature` `max_temperature` `min_temperature` `avg_humidity`
`precipitation` `avg_wind_speed` `sunshine_hours`

## 批量补齐 / 新数据自动补齐（v0.8）

**场景**：今年新录入一批数据 → 自动扫出其中的异常值 / 空白，用邻站补上，输出明细表。

**一条命令**（不传 batch 就自动取最新录入的那批）：

```bash
# 只看这批数据的质量，不修复
curl "http://127.0.0.1:8900/api/repair/overview?year=2026"

# ★ 自动补齐最新一批（默认 allow_climate_fill=false：绝不把气候态当观测写回）
curl "http://127.0.0.1:8900/api/repair/new_batch?auto_latest=true&lookback_days=7"

# 知道批次号就点名补
curl "http://127.0.0.1:8900/api/repair/new_batch?batch=20261106173924"

# 先看有哪些批次（按上传时间倒序，最新在第一位）
curl "http://127.0.0.1:8900/api/repair/batches?limit=20"

# 批量补一批站点（按省或多站号）
curl "http://127.0.0.1:8900/api/repair/batch?province=黑龙江&year=2020"
curl "http://127.0.0.1:8900/api/repair/batch?station_ids=1124,1125,1126&year=2020"
```

**批次是怎么识别的**：每条记录带 `file_path`，形如
`gg/weather-station/multi/20261106160739_1_1793952459.xlsx`，前 14 位就是上传时间戳。
库内共 844 个批次，绝大多数是逐站上传（一批一站）。

**`/api/repair/new_batch` 的工作流**：

1. 定位最新批次 → 取它覆盖的站点 与 数据日期区间（`lookback_days` 可往前多补几天，补漏填）
2. 对这段区间扫描：缺行 / 空值 / 超物理范围哨兵值（32766、999999 等）/ `max<min` 不自洽
3. 用邻站同日有效值 + 气候去趋势重建，逐值标 `quality`
4. 返回 `summary` + 全部 `rows` 明细 + 落盘 CSV（`exports/补齐_<batch>.csv`，UTF-8-SIG，Excel 直接打开）

**为什么默认 `allow_climate_fill=false`**：自动补齐最怕的错不是修不准，而是**静默编造**。
如果某段整段停更、邻站和前后日全无数据，填月气候平均值等于把气候态写成观测。
脚本化运行时没人复核，所以默认返回 `null` 并在 `warnings` 说明，宁可留空也不编。

**幂等**：本 API 只读数据源、不写回，同一批次重复调用结果一致。回写由调用方按 `rows` 决定。

**性能实测**：`repair_scan` 0.16s/站年；`repair_batch` 2 站 0.7s；
`repair_new_batch` 单站 60 年（班玛 1567，20879 行）**11.6s**，bad_rows 4887 / repaired 11674 /
by_quality {observed:11663, temporal:11, climate:0}。

**做成定时任务**：新数据入库后跑
`GET /api/repair/new_batch?auto_latest=true&lookback_days=7`，
把返回的 `rows` 回写数据库即可（或用 `csv_file` 人工复核后导入）。

## 调用示例

```bash
# 漠河 2015-07-15 的最高温
curl "http://127.0.0.1:8900/api/weather/daily?station_id=1124&date=2015-07-15"

# 2020-07-12 全国最热的 10 个站
curl "http://127.0.0.1:8900/api/weather/day_rank?date=2020-07-12&limit=10"

# 漠河历史最高温 Top5（含日期）
curl "http://127.0.0.1:8900/api/records/station?station_id=1124&limit=5"

# 1951-2025 全国最高温 Top10
curl "http://127.0.0.1:8900/api/records/global?limit=10"

# 2020 年新疆最高温记录
curl "http://127.0.0.1:8900/api/records/global?year=2020&province=新疆"

# 漠河 1951-2025 年均温趋势（画图）
curl "http://127.0.0.1:8900/api/trend?station_id=1124&agg=year&metric=avg_temperature"

# 漠河 1991-2020 常年逐月气候值
curl "http://127.0.0.1:8900/api/climate/monthly?station_id=1124"

# 多站对比：三站 2020-07 一周的日最高温（叠加折线图）
curl "http://127.0.0.1:8900/api/weather/daily_multi?station_ids=1124,1213,1178&start=2020-07-01&end=2020-07-07&metric=max_temperature"

# 漠河 1961-1990 vs 1991-2020 逐月气候对比（气候变化）
curl "http://127.0.0.1:8900/api/compare/periods?station_id=1124"

# 某经纬度附近 100km 内的站（齐齐哈尔附近 → 齐齐哈尔站 4.5km）
curl "http://127.0.0.1:8900/api/stations/near?lat=47.34&lng=123.95&radius_km=100"

# 吐鲁番历年 ≥35℃ 高温日数
curl "http://127.0.0.1:8900/api/stats/threshold_days?station_id=1213&metric=max_temperature&op=gte&threshold=35"

# 哈密(1178) 逐年 ≥10℃ 积温 / 无霜期（农业引种参考）
curl "http://127.0.0.1:8900/api/agri/station?station_id=1178"

# 2024 年均温最高的站（该年至少 300 天数据）
curl "http://127.0.0.1:8900/api/rank/year?year=2024&metric=avg_temperature&limit=10"

# 漠河「历史上的今天」：7 月 15 日历年最高温排行
curl "http://127.0.0.1:8900/api/on_this_day?station_id=1124&month=7&day=15"

# 漠河填报表（1991-2020 气候期 12 月值，含 <40℃/0<RH<100/u<30/n<18/Pmax 7-9 月校验）
curl "http://127.0.0.1:8900/api/report/table?station_id=1124&year_from=1991&year_to=2020"

# 漠河填报表直接导出 Word/Excel 成品文件
curl "http://127.0.0.1:8900/api/report/export?station_id=1124"

# 扫描漠河 2020 年的缺测/异常（哨兵值、超物理范围、max<min、缺行）
curl "http://127.0.0.1:8900/api/repair/scan?station_id=1124&year=2020"

# 隐性异常（数值合法但可疑，只报不修）：超历史极值 / 与同日邻站偏离
curl "http://127.0.0.1:8900/api/repair/hidden_scan?station_id=1382&year=1991"

# 修复 2020-12-24 的坏值：默认 auto（逐指标自动选最优法），最近 12 站
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1124&date=2020-12-24"

# 整年批量修复，只修温度三兄弟，idw 距离反比加权
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1124&year=2020&metrics=max_temperature,min_temperature,avg_temperature&method=idw"

# 对比用：强制用「最近站直取」（精度最差，仅供对照）
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1124&year=2020&method=nearest"

# 克里金（地统计）：距平克里金，变差函数由邻站 30 年长序列拟合
#   显著优于 clima 约 5%，但需多查一次 1991-2020 长序列，首次调用会慢一些
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1124&date=2020-12-24&method=krig"

# 跨要素岭回归（精度最高：本站其它要素齐全时气温 MAE 可到 0.38℃）
curl "http://127.0.0.1:8900/api/repair/daily?station_id=1124&date=2020-07-03&method=own6"
```

## 代码结构

```
weather_api/
├─ data.py       查询层：纯函数，不依赖 HTTP。网站后端可直接 import
├─ main.py       FastAPI 路由层：参数校验 → 调 data.py → JSON
├─ test_api.py   169 项自检（python weather_api/test_api.py）
└─ README.md     本文档
```

上传网站时：`data.py` 不用动（或把 parquet 换回 TDengine/MySQL 查询实现），
`main.py` 的路由与参数保持不变，前端按本文档对接即可。

## 已知数据质量口径（重要）

1. **哨兵值**：32766/32767/999999 等为缺测标记，已在聚合口径中过滤，日值里保留并给 `is_valid`
2. **可疑极值**：数据中存在物理范围外但非哨兵的值，如砀山(安徽) 2019-05-09 最低温 **-60℃**、
   阿图什(新疆) 2018-06-22 最高温 **57.7℃**——超过官方历史记录，做展示时可加"待核实"标注
3. **覆盖率**：并非每站都从 1951 年开始（如漠河从 1957 年起）；`/api/stations/{id}` 里有覆盖区间
4. 33 个 MySQL 站点在 TDengine 里没有数据（station_id 1124~2032 之外无对应子表）
5. **2000 年闰日已修正**：源库时间戳整体 +100 年时，2000-02-29 被挤到 2100-02-28
   （2100 是世纪年非闰年，pandas 的「日历 +100 年」把它钳掉，并留下 12:00 相位）。
   全库这 684 行分布在 684 个站，简单减 100 年会得到「02-28 两行、02-29 无数据」。
   视图 `weather` 里已给出修正后的 `date_true`（12:00 相位 → 次日），
   `/api/weather/daily?station_id=1266&date=2000-02-29` 现在能正常返回 1 行。
6. **气候基准逐指标独立过滤**：`repair_daily` 的气候基准不再用 7 指标 AND 的 `weather_ok` 视图，
   否则任一指标越界会整行作废（实测 avg_temperature 可用样本被连坐丢弃约 9.4%）。
