# 气象数据查询 API · 对接文档

> 面向**接口调用方**：数据由服务端提供，你只需按本文档调用接口、取返回数据。
> 不需要接触任何数据库或数据文件。
>
> 服务地址：`http://<服务器IP>:8900`　　接口总数：34 个（全部 GET）
> 在线文档：`<服务地址>/docs`（Swagger，可直接在页面里试调用）
> 所有示例里把 `127.0.0.1:8900` 换成实际服务器地址。

---

## 1. 通用约定

### 1.1 返回格式

所有 `/api/*` 接口统一返回 JSON，**成功**：

```json
{
  "ok": true,
  "data": ...
}
```

部分接口在 `data` 之外还有附加字段（如站点列表的 `total`、多站查询的 `stations`），与 `ok`、`data` 平级。

**失败**：

```json
{ "ok": false, "error": "错误信息" }
```

配套 HTTP 状态码：`200` 成功 / `400` 参数错误 / `404` 站点不存在 / `422` 参数校验失败 / `500` 服务内部错误 / `503` 数据目录不可达。

### 1.2 关键规则（对接前必读）

| 项 | 说明 |
|---|---|
| **日期口径** | 全部用真实历史年份，格式 `YYYY-MM-DD`，范围 **1951-01-01 ~ 2025-12-31**。源库的时间偏移已在服务端修正，不要自己减 100 年 |
| **站点 ID** | 整数，如漠河 `1124`、吐鲁番 `1213`、齐齐哈尔 `1148`。完整列表见 `GET /api/stations` |
| **省份名** | 已归一化的简称，如 `新疆`、`黑龙江`、`广东`（不带"省/自治区"后缀）。完整列表见 `GET /api/provinces` |
| **指标名** | 固定 7 个，见下表；`metric` 参数只能传这些 key |
| **CORS** | 已全开，浏览器前端可直接 `fetch` 调用 |
| **编码** | 响应 `charset=utf-8`，中文正常 |
| **无鉴权** | 当前接口无鉴权；如需公网暴露，请在网关层加 Basic Auth / token / 限流 |

### 1.3 七个指标（`/api/metrics` 的返回）

| key（参数用这个） | 名称 | 单位 |
|---|---|---|
| `avg_temperature` | 平均气温 | ℃ |
| `max_temperature` | 最高气温 | ℃ |
| `min_temperature` | 最低气温 | ℃ |
| `avg_humidity` | 平均相对湿度 | % |
| `precipitation` | 降水量 | mm |
| `avg_wind_speed` | 平均风速 | m/s |
| `sunshine_hours` | 日照时数 | h |

### 1.4 数据规模

876 个有气象数据的站点 × 1951–2025 逐日 × 7 指标，有效数据约 1442 万行。31 个省级行政区。

---

## 2. 运维 / 基础

### `GET /api/health`
健康检查。返回 `{"ok": true, "version": "0.10.0", "data_root": ..., "parquet_files": 876}`。数据目录不可达时 HTTP 503。

### `GET /api/overview`
数据集概览。

```json
{"ok": true, "data": {
  "rows_total": 14420774,
  "stations": 876,
  "date_from": "1951-01-01 00:00:00",
  "date_to": "2025-12-31 00:00:00"
}}
```

### `GET /api/metrics`
可用指标清单，返回数组 `[{"key": "avg_temperature", "name": "平均气温", "unit": "℃"}, ...]`。

### `GET /api/provinces`
省份列表 + 站点数，返回数组 `[{"province": "新疆", "station_count": 70}, ...]`，按站数降序，共 31 项。

### `GET /api/stations`
站点列表（分页）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `province` | string | — | 按省筛选，如 `新疆` |
| `search` | string | — | 站名/站号/ID 模糊搜索，如 `漠河` |
| `limit` | int | 100 | 1~1000 |
| `offset` | int | 0 | 分页偏移 |

返回 `{"ok": true, "data": [{station_id, station_code, province, province_raw, station_name, latitude, longitude, altitude}, ...], "total": 909}`。
注意 `total=909` 是站点表总数（含 33 个无气象数据的站），有逐日数据的站是 876 个。

### `GET /api/stations/{station_id}`
站点详情 + 数据覆盖。返回：

```json
{"ok": true, "data": {
  "station_id": 1124, "station_code": "50136", "province": "黑龙江",
  "station_name": "漠河", "latitude": 52.9744, "longitude": 122.5131, "altitude": 433.9,
  "data": {
    "rows_total": 24453, "temp_valid": ..., "prcp_valid": ...,
    "date_from": "1957-01-01 00:00:00", "date_to": "2025-12-31 00:00:00",
    "years_with_data": ...
  }
}}
```

站点不存在返回 HTTP 404。

---

## 3. 空间查询

### `GET /api/stations/near`
按经纬度找最近站点（按距离升序）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `lat` | float | 必填 | 纬度 -90~90 |
| `lng` | float | 必填 | 经度 -180~180 |
| `radius_km` | float | 100 | 半径 km，1~2000 |
| `limit` | int | 20 | 1~200 |

返回数组，每项 `{"station_id", "station_code", "province", "station_name", "latitude", "longitude", "altitude", "distance_km"}`。
例：`lat=47.34&lng=123.95` → 齐齐哈尔站 4.5 km。

### `GET /api/stations/bbox`
矩形范围内站点（地图打点用）。

| 参数 | 类型 | 说明 |
|---|---|---|
| `lat_min` / `lat_max` | float | 纬度范围 -90~90 |
| `lng_min` / `lng_max` | float | 经度范围 -180~180 |
| `limit` | int | 默认 500，1~2000 |

返回数组，字段同站点列表（无 `distance_km`）。

---

## 4. 日值查询

### `GET /api/weather/daily` ★ 最常用
某站某天或某段时间的逐日观测值。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | 必填 | 站点 ID |
| `date` | string | — | 单日 `2020-07-12` |
| `start` / `end` | string | — | 区间起止（与 `date` 二选一） |
| `limit` | int | 500 | 1~5000 |

`date` 和 `start/end` 至少给一个。返回数组，每行：

```json
{
  "station_id": 1124, "date": "2015-07-15",
  "avg_temperature": 20.1, "max_temperature": 30.4, "min_temperature": 6.6,
  "avg_humidity": 51, "precipitation": 0.0,
  "avg_wind_speed": 1.3, "sunshine_hours": 13.3,
  "is_valid": true
}
```

> `is_valid=false` 表示该行有超物理范围的可疑值（哨兵值等），前端可按需要 filtering。
> 排行/极值/聚合类接口已自动过滤这类值，只有本接口返回原始值。

### `GET /api/weather/day_rank`
某日全国（或某省）站点排行。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `date` | string | 必填 | `2020-07-12` |
| `metric` | string | `max_temperature` | 7 指标之一 |
| `order` | string | `desc` | `desc` 从高到低 / `asc` 从低到高 |
| `limit` | int | 100 | 1~1000 |
| `province` | string | — | 只看某省 |

返回数组 `[{"station_id", "station_code", "province", "station_name", "date", "value"}, ...]`。
例：`2020-07-12` 全国最热 = 吐鲁番 38.9℃。

### `GET /api/weather/daily_multi`
多站日值对比（一次最多 20 站，画叠加折线图用）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_ids` | string | 必填 | 逗号分隔，如 `1124,1213,1354` |
| `date` / `start` / `end` | string | — | 单日或区间 |
| `metric` | string | — | 只取某指标（可选） |
| `limit` | int | 5000 | 1~20000 |

返回 `{"ok": true, "data": [...], "stations": 2}`，每行比单站多一个 `station_name` 字段。

---

## 5. 极值 / 排行

### `GET /api/records/station`
某站历史极值 TopN（含出现日期）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `station_id` | int | 必填 |
| `metric` | string | `max_temperature` |
| `order` | string | `desc`（`asc` 取极小值） |
| `limit` | int | 10（1~100） |

返回数组 `[{"station_id", "station_code", "province", "station_name", "date", "value"}, ...]`。
例：漠河历史最高 39.3℃（2010-06-24）。

### `GET /api/records/global`
全数据集极值 TopN。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `metric` | string | `max_temperature` | 7 指标之一 |
| `order` | string | `desc` | `asc` 取极小值 |
| `limit` | int | 10 | 1~100 |
| `year` | int | — | 限定年份 1951~2025 |
| `date_from` / `date_to` | string | — | 限定日期区间 |
| `province` | string | — | 限定省份 |

返回结构同 `/api/records/station`。
> 注意：全国最高 57.7℃（阿图什 2018-06-22）、最低 -60℃（砀山 2019-05-09）等超出官方记录，属源数据口径，展示时建议标注"待核实"。

### `GET /api/rank/year`
某年站点排行（带最少天数限制防小样本）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `year` | int | 必填 | 1951~2025 |
| `metric` | string | `avg_temperature` | 7 指标之一 |
| `order` | string | `desc` | |
| `limit` | int | 100 | 1~876 |
| `station_min_days` | int | 300 | 该年至少这么多天数据才参与排行（1~366） |

返回数组 `[{"station_id", "station_code", "province", "station_name", "year", "value", "days"}, ...]`。

### `GET /api/on_this_day`
历史上的今天（同月同日跨年排行）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | 必填 | |
| `month` | int | 必填 | 1~12 |
| `day` | int | 必填 | 1~31 |
| `metric` | string | `max_temperature` | |
| `order` | string | `desc` | |
| `limit` | int | 75 | 1~100 |

返回数组 `[{"station_id", "station_name", "date", "year", "value"}, ...]`，按该站点每年这一天的指标值排序。

---

## 6. 聚合 / 统计

### `GET /api/aggregate/annual`
逐年统计。三种口径：**全国**（不给 station_id/province）、**单站**（给 station_id）、**分省**（给 province）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `station_id` | int | — |
| `province` | string | — |
| `year_from` / `year_to` | int | 1951~2025 |

返回数组，每行 `{"station_id", "station_code", "province", "station_name", "year", "temp_avg", "temp_max", "temp_min", "prcp_total", "hum_avg", "sun_total", "station_days"}`（全国口径下站名字段为空）。

### `GET /api/aggregate/monthly`
某站某年逐月统计。

| 参数 | 类型 | 默认 |
|---|---|---|
| `station_id` | int | 必填 |
| `year` | int | 必填 1951~2025 |
| `metric` | string | `avg_temperature` |

返回数组 `[{"month": 1, "avg_temperature": -24.87, "days": 31}, ...]`（12 行；`avg_temperature` 键名随 metric 变化）。

### `GET /api/trend`
时间序列（画图用）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | 必填 | |
| `metric` | string | `avg_temperature` | |
| `agg` | string | `month` | `day` / `month` / `year` |
| `date_from` / `date_to` | string | — | 日期范围 |

返回数组 `[{"period": "1957-01-01", "value": 0.84, "days": 184}, ...]`，直接把 `period` 当 x 轴、`value` 当 y 轴画图。

### `GET /api/climate/monthly`
气候基准（常年逐月值，默认 1991–2020 WMO 标准期）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `station_id` | int | 必填 |
| `period_from` / `period_to` | int | 1991 / 2020 |

返回数组 `[{"month": 1, "temp_c": -28.1, "prcp_mm": 0.2, "hum_pct": 69.0, "wind_ms": 1.1, "sun_h": 4.6, "days": 886}, ...]`（12 行）。

### `GET /api/compare/periods`
两段时期逐月气候对比（看气候变化，默认 1961-1990 vs 1991-2020）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `station_id` | int | 必填 |
| `year_a` / `year_b` | int | 1961 / 1990（第一段） |
| `year_c` / `year_d` | int | 1991 / 2020（第二段） |
| `month_from` / `month_to` | int | 1 / 12 |

返回数组，每行 `{"month", "period", "temp_c", "prcp_mm", "tmax_c", "tmin_c", "days"}`；`period` 为 `1961-1990` 或 `1991-2020`，共 24 行。

### `GET /api/stats/threshold_days`
阈值天数统计（高温日/严寒日/暴雨日等）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `metric` | string | `max_temperature` | 7 指标之一 |
| `op` | string | `gte` | `gte`≥ / `gt`> / `lte`≤ / `lt`< / `eq`= / `ne`≠ |
| `threshold` | float | 35.0 | 阈值 |
| `station_id` | int | — | 按站（与 province 二选一） |
| `province` | string | — | 按省 |
| `year` | int | — | 单年 |
| `year_from` / `year_to` | int | — | 年份范围 |

返回数组 `[{"station_id", "station_name", "year", "days"}, ...]`。
例：`station_id=1213&threshold=35` → 吐鲁番历年 ≥35℃ 高温日数。

### `GET /api/agri/station`
单站农业气象指标（逐年）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | 必填 | |
| `base_temp` | float | 10.0 | 活动积温起点温度 |
| `year_from` / `year_to` | int | — | 年份范围 |
| `month_from` / `month_to` | int | 1 / 12 | 月份范围 |

返回数组 `[{"year", "gdd10", "days_ge_10", "gdd_start", "gdd_end", "frost_free_days", "gdd_daily_mean"}, ...]`。

### `GET /api/agri/compare`
多站农业气象对比（多年平均，选种/引种参考）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `station_ids` | string | 必填，逗号分隔 |
| `base_temp` | float | 10.0 |
| `year_from` / `year_to` | int | 1991 / 2020 |

返回数组 `[{"station_id", "station_name", "years", "gdd10_mean", "gdd10_max", "gdd10_min", "frost_free_mean"}, ...]`。

### `GET /api/debug/values_above`
超阈值记录追查（数据质量排查用）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `metric` | string | `max_temperature` |
| `threshold` | float | 50.0 |
| `order` | string | `desc` |
| `limit` | int | 100（1~1000） |

返回数组 `[{"station_id", "station_code", "province", "station_name", "date", "value"}, ...]`。

---

## 7. 填报表

### `GET /api/report/table`
气象数据填报表（按模板口径聚合成 12 个月值 + 内置校验规则）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `station_id` | int | 必填 |
| `year_from` / `year_to` | int | 1991 / 2020 |

返回：

```json
{"ok": true, "data": {
  "station": {"station_id": 1124, "station_code": "50136", "station_name": "漠河", "province": "黑龙江", "latitude": ..., "longitude": ..., "altitude": ...},
  "period": "1991-2020",
  "rows": [
    {"category": "气象数据", "metric": "最高气温", "unit": "℃", "rule": "...", "months": [12 个月的值], "flags": [...]}
  ],
  "summary": {"n_years", "days_total", "pmax_month", "pmax_mm", "pmax_in_jul_sep", "annual_prcp_mm", "arid_zone_lt200mm"},
  "manual_rows": [{"category": "土壤数据", "months": [...]}, {"category": "作物数据", "months": [...]}]
}}
```

`rows` 固定 7 行（7 个气象指标），`manual_rows` 是留空待人工填写的土壤/作物两行。

### `GET /api/report/export`
同上，但额外生成 Word(.docx) 和 Excel(.xlsx) 成品文件，返回文件路径与摘要。参数同 `/api/report/table`。

返回 `data` 含 `word_file`、`excel_file`、`station`、`period`、`summary`、`flags`。
> 文件写入服务端 `exports/` 目录；如前端要下载，需服务端另行提供静态文件映射（当前仅返回路径）。

---

## 8. 异常值修复 / 批量补齐

> 这一组是本平台的特色：**自动识别坏值并用邻站数据重建**。所有修复都是统计推断，不是观测值，每条结果带 `quality` 可信度标注。

### 8.1 可信度三档（重要）

| quality | 含义 | 可信度 |
|---|---|---|
| `observed` | 邻站同日有实测 / 本站其它要素可用，经气候距平或跨要素回归订正 | 最可信 |
| `temporal` | 邻站当日也无有效值，改用本站前后日距平 | 次之 |
| `climate` | 只能用该月气候平均值 —— 是气候态，**不是实况** | 仅供参考，勿当观测 |

### 8.2 修复方法 `method`

| method | 做法 | 实测气温 MAE | 说明 |
|---|---|---|---|
| `auto` | 逐指标自动选最优（默认） | — | 推荐 |
| `own6` | 门控跨要素岭回归（本站其它 6 要素 + 邻站 7 要素距平 + 坐标/月相位） | 0.38℃ | 精度最高，要求该日其它要素够多 |
| `clima` | 邻站值 + 目标站与邻站月气候基准之差 | 0.78℃ | 性价比最高，也是兜底 |
| `blend` | 0.5×clima + 0.5×本站前后日距平 | 0.78℃ | |
| `krig` | 距平克里金（地统计） | 0.74℃ | 显著更优但只快约 5%，降水自动退回 clima |
| `idw` | 距离反比平方加权 | 1.62℃ | |
| `alt_idw` | idw + 海拔订正 | 1.05℃ | |
| `nearest` | 直接取最近站原值 | 2.13℃ | 对照用，实测最差 |

### 8.3 接口清单

#### `GET /api/repair/scan`
扫描缺测/异常（只扫不修）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | 必填 | |
| `date` / `start` / `end` / `year` | — | — | 四选一定区间 |
| `detail_limit` | int | 500 | 最多返回坏日明细行数（1~100000） |

返回 `data`：`{station, period, summary{days, rows_present, inconsistent_maxmin, inconsistent_maxavg, inconsistent_avgmin, inconsistent_total, per_metric}, bad_rows[{date, values, problems}], bad_rows_shown}`。

> **v0.12 判据升级**：除物理范围与哨兵值外，还检查
> ① **温度三列自洽性**：必须满足 `min ≤ avg ≤ max`，违反的记为 `max<min`/`max<avg`/`avg<min`（全库 avg<min 有 1,660 行，旧版只查 max<min 的 8 行，这是原先最大的漏检口）；
> ② **日照超天文上限**：日照时数 > 当天按纬度算出的可照时数 + 0.5h（全库仅 145 行，误报极低）。
> 实测：`repair_scan(1610, year=2019)` 旧版报 0 行，新版报 250 行（该站 avg<min 全部命中）；正常站新乡 `inconsistent_total=0` 不误报。

#### `GET /api/repair/hidden_scan` ★ 隐性异常发现（只报不修）
补 `repair/scan` 的盲区：后者只看物理范围/自洽性，抓不到「数值在合法范围内但错误」的隐性异常（例如某天温度被错记成 5℃ 而不是 15℃）。三条互补规则：

| 规则 | 依据 | 特点 |
|---|---|---|
| `record` | 超过本站该月**滚动 ±15 年**（留一年交叉验证）历史极值 ± `sigma`×稳健标准差（1.4826×MAD） | 零误报（报出来基本就是问题），但只抓超历史记录的 |
| `spatial` | 与同日邻站中位数偏离超 `spatial_sigma`×稳健标准差（降水用 ≥5 倍比值法避免把真暴雨误报） | 能抓「邻站都正常、只有本站离谱」的情况 |
| `implausible` | 超 `PHYS_STRICT` 严格边界（气温 -60~55、最高 -55~55、最低 -65~45、风速 0~60 等） | 兜底，比宽泛 ±100 更早发现 |

参数：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | 必填 | |
| `date` / `start` / `end` / `year` | — | — | 四选一定区间 |
| `metric` | str | 全部 | 只查一个指标（7 选 1） |
| `k` | int | 12 | 参与空间对比的邻站数（1~32） |
| `radius_km` | float | 200 | 邻站搜索半径 km |
| `sigma` | float | 1.0 | 历史极值法的容差（稳健标准差倍数，0~10） |
| `spatial_sigma` | float | 6.0 | 空间偏离阈值（稳健标准差倍数） |
| `baseline` | str | **rolling** | `rolling`=滚动 ±15 年（推荐，抗气候漂移）/ `fixed`=固定 1991-2020（老版本行为） |
| `detail_limit` | int | 500 | 最多返回疑似条数（1~100000） |

> **为什么默认 rolling**：固定 1991-2020 基线会把 1950-70 年代的正常偏冷误判成异常（新乡实测固定基线 156 条 record 命中里 132 条是气候漂移误报；该站 1 月均温 1951-60 为 -1.22℃，2011-20 为 +0.57℃，升了 1.8℃）。改滚动后 record 命中 171→74，1960 年代误报 145→69。实测对比：固定 610 条 → 滚动 498 条。

返回 `data`：`{station, period, baseline, rules, summary{days, suspects, by_metric, by_rule}, suspects[]}`。
每条 `suspect`：`{date, metric, value, rules[], confidence, detail, neighbors[]}`：
- `rules`：命中的规则（`record`/`spatial`/`implausible`，可多个）
- `confidence`：`high`=≥2 条规则都命中（最可信），`medium`=单条命中（需人工确认）
- `detail`：各规则的判定数值（历史极值范围、判定窗、邻站中位、偏离量/倍数）
- `neighbors`：**判断时实际用到的邻站实测值**（站号/站名/距离/值）——让调用方看到依据

> **只报告，绝不自动修复**。极端暴雨、大风本就是真事件（实测 1608 条降水统计极端全是真暴雨），是否当异常处理须由业务方确认。实测有效性：往干净数据注入 210 个「物理范围内但错误」的值，历史极值法 100% 精确、极值∪邻站 62% 检出。

```bash
curl "http://127.0.0.1:8900/api/repair/hidden_scan?station_id=1382&year=1991"
curl "http://127.0.0.1:8900/api/repair/hidden_scan?station_id=1382&start=1951-01-01&end=2025-12-31&baseline=rolling"
```

#### `GET /api/repair/column_swap` ★ 整列互换检测
识别「两列数值被**整体对调**」这类结构性错误 —— 这类错误单看每个值都合理，所有阈值判据都抓不到，但会导致整段数据完全错误。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | — | 单站检测 |
| `province` | str | — | 按省批量 |
| `station_ids` | str | — | 逗号分隔 |
| `min_rows` | int | 10 | 至少多少行矛盾才判 |
| `fix_ratio` | float | 0.95 | 对调后矛盾消除比例阈值 |

返回 `data`：`{scanned_stations, summary{found, failed, criteria}, stations[], failed[]}`。
每条命中项：`{station_id, station_name, province, swap_pair, rows_affected, contradict_rows, fixed_ratio, breakpoint, before_after{before, after}, advice}`：
- `swap_pair`：互换的两列（如 `["avg_temperature", "avg_wind_speed"]`）
- `breakpoint`：从哪一天开始互换（分界日）
- `fixed_ratio`：把这些行两列对调后，满足 `min ≤ 风速 ≤ max` 的比例
- `before_after`：互换前/后的均值与范围对比 —— 用来确认「对调后统计变回正常」

> **修复方式是对调，不是插值**。实测全库 909 站只有 **1610 站（四川盐源，海拔 2516.8m）** 命中：从 **2013-10-01** 起 `avg_temperature` 与 `avg_wind_speed` 两列互换，影响 1,653 行，对调后矛盾消除率 **99.21%**。对调前 a 列均值 2.54、范围 [0.3, 9.4]；对调后 12.52、范围与互换前（12.44 / [-5.4, 24.0]）高度吻合。新乡等正常站 `found=0`。

```bash
curl "http://127.0.0.1:8900/api/repair/column_swap?station_id=1610"
curl "http://127.0.0.1:8900/api/repair/column_swap"          # 全库扫描（约 79 秒）
```

#### `GET /api/repair/daily`
修复某站某区间的坏值。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_id` | int | 必填 | |
| `date` / `start` / `end` / `year` | — | — | 四选一定区间 |
| `method` | string | `auto` | 见 8.2 |
| `k` | int | 12 | 邻站个数 1~32 |
| `radius_km` | float | 300 | 搜索半径 km |
| `metrics` | string | 全部 | 逗号分隔，默认 7 指标全修 |
| `allow_climate_fill` | bool | true | 停更段是否允许气候态填充 |
| `max_gap_days` | int | 7 | 连续缺测超过多少天判停更段（1~10000） |
| `detail_limit` | int | 500 | 最多修复坏日行数 |

返回 `data`：

```json
{
  "station": {...}, "period": {"from": ..., "to": ...},
  "method": "auto", "k": 12, "radius_km": 300.0,
  "metrics": ["avg_temperature", ...],
  "neighbors": [{"station_id", "station_name", "distance_km", "latitude", "longitude", "altitude"}, ...],
  "summary": {
    "bad_rows": 3, "repaired_values": 11, "no_neighbor_data": 0,
    "by_quality": {"observed": 11, "temporal": 0, "climate": 0},
    "warnings": []
  },
  "rows": [
    {
      "date": "2020-12-24", "metric": "avg_temperature",
      "original": 999999, "problem": "out_of_range",
      "repaired": -27.7, "method": "own6",
      "contributors": [{"station_id": ..., "distance_km": ..., "value": ..., "adjusted": ..., "weight": ...}],
      "quality": "observed", "note": "..."
    }
  ]
}
```

> **停更段保护**：数据集中不少站整段停更（2020 年有 576 站只记录到 4 月底）。这类缺口用气候态填充等于编造观测，
> 所以默认拒绝填充（`max_gap_days=7`），只在 `summary.warnings` 告知。要放宽可传 `max_gap_days=400`，但结果里会出现 `quality=climate`，**勿当观测用**。

#### `GET /api/repair/batch`
批量补齐（多站 × 一个区间一次跑完并汇总）。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `station_ids` | string | — | 逗号分隔，与 `province` 二选一，都不给=全库 |
| `province` | string | — | 按省批量 |
| `date` | string | — | 单日（等价 start=end=date） |
| `start` / `end` / `year` | — | — | 区间 |
| `metrics` / `method` / `k` / `radius_km` | — | 同上 | |
| `allow_climate_fill` / `max_gap_days` / `detail_limit` | — | 同上 | |
| `limit` | int | — | 最多处理多少站（调试用） |

返回 `data`：`{period, method, k, radius_km, metrics, station_count, requested_count, elapsed_sec, summary, stations[], failed[], rows[]}`。
`rows` 是全部站修复明细合并（每行带 `station_id`/`station_name`/`province`，可直接导 CSV 回写）；单站失败记入 `failed[]` 不中断整批。

#### `GET /api/repair/new_batch` ★ 新数据自动补齐
自动定位最新录入批次，扫出坏值/空白并补齐。这就是"新录入一批数据后自动补"的入口。

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `batch` | string | — | 批次号（14 位时间戳）；留空自动取最新 |
| `auto_latest` | bool | true | `batch` 为空时是否自动取最新批次 |
| `lookback_days` | int | 0 | 在批次日期区间上向前多补几天（0~365，补漏填） |
| `metrics` / `method` / `k` / `radius_km` | — | 同上 | |
| `allow_climate_fill` | bool | **false** | 默认 false：绝不把气候态当观测写回 |
| `max_gap_days` / `detail_limit` | — | 同上 | |
| `write_csv` | bool | true | 是否落 CSV 到服务端 exports/ |

返回 `data`：`{batch, batch_info{stations_covered, rows_covered, data_from, data_to}, period, lookback_days, method, k, radius_km, allow_climate_fill, summary, stations[], failed[], rows[], elapsed_sec}`。

> **幂等**：接口只读数据源不写回，同批次重复调用结果一致。回写由调用方按 `rows` 决定。
> **性能**：单站 60 年（约 2 万行）约 12 秒，建议异步调用，别卡在页面请求里。

#### `GET /api/repair/batches`
已录入批次列表（按上传时间倒序，用来发现最新一批）。返回 `data`：

```json
{"batches": [{"batch": "20261106173924", "stations": 1, "rows": 20879, "first_day": "1960-02-01", "last_day": "2020-04-30"}, ...],
 "hint": "取 batches[0].batch 作为 /api/repair/new_batch 的 batch 参数即可只补最新一批"}
```

#### `GET /api/repair/overview`
全库质量巡检（只扫不修，速度快，约 0.1s/站年）。

| 参数 | 类型 | 默认 |
|---|---|---|
| `year` / `start` / `end` | — | 区间 |
| `province` / `station_ids` | string | 筛选 |
| `limit` | int | 最多巡检站数 |

返回 `data`：`{period, station_count, requested_count, elapsed_sec, totals{days, rows_present, inconsistent_maxmin, invalid, missing}, per_metric{7 指标各自计数}, worst_stations[], stations[], failed[]}`。

---

## 9. 前端调用示例

```javascript
const BASE = 'http://<服务器IP>:8900';

// 统一请求封装
async function api(path, params = {}) {
  const url = new URL(BASE + path);
  Object.entries(params).forEach(([k, v]) => v != null && url.searchParams.set(k, v));
  const res = await fetch(url).then(r => r.json());
  if (!res.ok) throw new Error(res.error || '请求失败');
  return res.data;            // 附加快照字段如 total 时需用原始 res
}

// 1) 搜站点：拿到 station_id
const stations = await api('/api/stations', { search: '漠河' });    // [{station_id:1124, ...}]

// 2) 查某站某天日值（画单站折线/表格）
const rows = await api('/api/weather/daily', { station_id: 1124, date: '2015-07-15' });
// rows[0].max_temperature === 30.4

// 3) 某日全国最热 10 站（排行榜）
const top = await api('/api/weather/day_rank', { date: '2020-07-12', limit: 10 });

// 4) 画趋势图
const trend = await api('/api/trend', { station_id: 1124, agg: 'year', metric: 'avg_temperature' });
// x = trend.map(r => r.period), y = trend.map(r => r.value)

// 5) 地图打点
const pts = await api('/api/stations/bbox', {
  lat_min: 18, lat_max: 54, lng_min: 73, lng_max: 135, limit: 1000
});

// 6) 气候基准（常年逐月）
const clim = await api('/api/climate/monthly', { station_id: 1124 });

// 7) 新数据入库后自动补齐（异步，结果写入自己的库）
const fixed = await api('/api/repair/new_batch', { auto_latest: true, lookback_days: 7 });
// fixed.rows = 每条修复明细；fixed.summary.by_quality 各档计数
```

---

## 10. 常见注意事项

1. **站点数 909 vs 876**：`/api/stations` 返回 909 个站点（含 33 个在时序库里没有数据的站），`/api/overview` 的 876 是有逐日数据的站。做地图/列表时用 909，做气象查询时注意某些站会返回空数组。
2. **极端值**：数据集里有超官方记录的值（57.7℃、-60℃），`is_valid` 可能仍为 true（它们在物理范围内），展示时建议标注"待核实"。
3. **哨兵值**：日值接口会原样返回 999999/32766 这类缺测标记并置 `is_valid=false`；其它接口（排行/极值/聚合）已自动过滤。
4. **分页**：`stations` 用 `limit`+`offset`，返回体里的 `total` 是总数（不是本次条数）。
5. **日期范围**：年份参数限 1951~2025；用真实年份，不要用 2051~2125。
6. **无鉴权**：接口当前无鉴权，公网部署务必在 Nginx/网关层加鉴权和限流。
7. **CORS 全开**：前端可直接跨域调用；如要走 HTTPS，需在服务端配反代（见部署文档）。
