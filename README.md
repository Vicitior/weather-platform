# 气象数据平台 · weather-platform

把一套**气象站点时序数据**（876 站 × 1951–2025 逐日 × 7 个要素，约 1700 万行）从云端数据库
镜像到本地、做质量体检、识别并修复异常/缺测，并对外提供一套完整的中文气象查询 HTTP API。

> 数据来源为第三方云数据库（TDengine Cloud + MySQL RDS）。本仓库的 git 历史里**不含数据、不含任何凭据**。
> 想立刻跑起来：直接下 [Release 里的数据包](https://github.com/Vicitior/weather-platform/releases)（367 MB，
> 解压即用）；想自己从源库镜像：用 `mirror_dbs.py`。

---

## 这个项目解决什么问题

原始数据是从业务系统导出的，直接拿来用会遇到四类问题，本仓库逐个处理：

| 问题 | 具体表现 | 处理 |
|---|---|---|
| **时间戳整体偏移** | 源库里所有时间是 2051–2125 年（未来），实际应为 1951–2025 | 镜像时统一 `−100 年`，并修正闰日错位（见下） |
| **哨兵值污染** | 999999 / 32766 这类"错误码"混在数值列里；降水列里塞了天气现象编码（32700 等） | 按物理范围过滤（`PHYS_RANGE`），聚合时逐指标独立过滤 |
| **缺测与异常** | 部分站整段停更（2020 年只有 161 站记满全年），部分站零星坏值 | 扫描 + 多方法修复，并标注每个修复值的可信度 |
| **闰日错位** | `+100 年` 位移把 2000-02-29 钳到了 2100-02-28，684 站全中 | 按 `hour(ts)=12` 的相位修正回 2000-02-29 |

---

## 目录结构

```
weather-platform/
├── weather_api/                  # 对外 HTTP API（v0.10.0，32 个路由）
│   ├── data.py                   # 查询层 + 修复算法（纯函数，不依赖 HTTP）
│   ├── main.py                   # FastAPI 路由
│   ├── export.py                 # 填报表导出 Word / Excel
│   ├── test_api.py               # 155 项自检
│   ├── requirements.txt
│   ├── README.md                 # 接口文档
│   └── DEPLOY.md                 # 部署到服务器的步骤
│
├── 数据获取
│   ├── pull_db_inventory.py      # 盘点源库（只读，输出 md/html 报告）
│   ├── mirror_dbs.py             # 把两库镜像成本地 parquet（断点续传）
│   └── local_lab.py              # duckdb 本地离线查询视图
│
├── 验证实验（论文式 hold-out）
│   ├── validate_repair.py        # 第一轮：4 种基础空间插值
│   ├── validate_repair2.py       # 第二轮：8 方法 + k 敏感性 + 稳定性 + 分季节
│   ├── validate_k.py             # k 饱和测试（到 k=32）
│   ├── validate_repair3.py       # 第三轮：地统计（克里金/回归克里金/RBF）
│   ├── validate_repair4.py       # 第四轮：配对显著性检验 + 全局变差函数 + 消融
│   ├── validate_repair5.py       # 第五轮：DINEOF / analog / 时空克里金
│   ├── validate_repair6.py       # 第六轮：回归克里金 / 多指标联合
│   ├── validate_repair7.py       # 第七轮：时空克里金参数调优
│   ├── validate_repair8.py       # 第八轮：全网低秩矩阵补全
│   ├── validate_repair9.py       # 第九轮：跨要素联合（MICE 思路）
│   ├── validate_repair10.py      # 第十轮：遮蔽模式敏感性（证伪第九轮）
│   ├── validate_repair11.py      # 第十一轮：真实坏值形态全库普查
│   ├── validate_repair12.py      # 第十二轮：门控混合
│   ├── validate_repair13.py      # 第十三轮：分层拆解，定最终门控参数
│   ├── validate_e2e.py           # 端到端：往真实数据注入 840 个坏值
│   └── stress_repair.py          # 压力测试
│
├── 报告生成
│   ├── make_repair_report.py     # 生成《异常值修复方法依据说明》
│   ├── make_mirror_docs.py       # 生成本地镜像说明
│   └── make_mysql_view.py        # 生成 MySQL 数据浏览页
│
└── 诊断脚本
    ├── check_coverage.py         # 逐年站点覆盖
    ├── check_tail_years.py       # 尾部年份站点构成
    ├── diag_repair_fail.py       # 修复失败成因归类
    ├── diag_base_none.py         # 气候基准完整性
    └── diag_names.py             # 省份命名归一
```

---

## 快速开始

### 1. 装依赖

```bash
pip install -r weather_api/requirements.txt
```

主要依赖：duckdb、pandas、numpy、fastapi、uvicorn、pyarrow（导出另需 python-docx / openpyxl）。
Python 3.13 已测。

### 2. 连接源库（只有镜像和盘点需要）

凭据**全部走环境变量**，代码里没有硬编码：

```bash
# Linux / macOS
export TD_TOKEN='<TDengine token>'  TD_DB='<时序库名>'
export MYSQL_HOST='<主机>' MYSQL_PORT=3306
export MYSQL_USER='<用户>' MYSQL_PASSWORD='<密码>' MYSQL_DB='<库名>'

# Windows PowerShell
$env:TD_TOKEN='<token>'; $env:TD_DB='<库名>'
$env:MYSQL_HOST='<主机>'; $env:MYSQL_PORT='3306'
$env:MYSQL_USER='<用户>'; $env:MYSQL_PASSWORD='<密码>'; $env:MYSQL_DB='<库名>'
```

> TDengine Cloud 的 token **必须放在 URL 查询参数**（`?token=...`），
> 放 `Authorization: Taosd` 头会返回 401。脚本已按这个方式处理。

### 3. 镜像数据到本地

```bash
python mirror_dbs.py            # 默认全量，断点续传（已下完的站会跳过）
python mirror_dbs.py --force    # 强制重拉
python mirror_dbs.py --skip-td # 只拉 MySQL
```

产出 `db_mirror/`：876 个 parquet（每站一个）+ MySQL 的 31 张表。
全量约 **392 MB**（zstd 压缩），首次约 20 分钟（受源库网关带宽限制）。

### 4. 起 API

```bash
python -m weather_api.main            # 默认 0.0.0.0:8900
```

- Swagger 文档：`http://127.0.0.1:8900/docs`
- 健康检查：`GET /api/health`
- 自检：`python weather_api/test_api.py`（155 项）

> **注意**：必须在 `weather_api` 的**父目录**执行，且 `db_mirror/` 要和 `weather_api/` 同级
> （或用 `WEATHER_DB_ROOT` 指向数据目录）。

---

## API 概览

32 个路由，完整文档见 [`weather_api/README.md`](weather_api/README.md)。常用的：

| 接口 | 作用 |
|---|---|
| `GET /api/overview` | 数据集总览（876 站 / 1442 万有效行 / 时间范围） |
| `GET /api/stations?province=黑龙江` | 站点列表（支持分省、模糊搜索、分页） |
| `GET /api/weather/daily?station_id=1124&date=2015-07-15` | 某站某日全部 7 个要素 |
| `GET /api/weather/day_rank?date=2020-07-12&metric=max_temperature` | 某日全国排行（谁最热） |
| `GET /api/records/station?station_id=1124&metric=max_temperature` | 某站历史极值 |
| `GET /api/records/global?metric=max_temperature` | 全国极值 |
| `GET /api/trend?station_id=1124&metric=avg_temperature&agg=year` | 时间序列趋势 |
| `GET /api/climate/monthly?station_id=1124` | 1991–2020 气候基准（12 个月） |
| `GET /api/report/table?station_id=1124` | 气象数据填报表（7 指标 × 12 月） |
| `GET /api/report/export?station_id=1124` | 导出 Word / Excel |
| `GET /api/repair/scan?station_id=1124&year=2020` | 扫描某站某年的异常/缺测 |
| `GET /api/repair/daily?station_id=1124&year=2020` | 修复并返回每个修复值 |
| `GET /api/repair/new_batch?auto_latest=true` | 新录入一批数据后自动补齐 |

统一返回格式：

```json
{ "ok": true,  "data": ... }
{ "ok": false, "error": "..." }
```

---

## 修复方法是怎么选的（不是拍脑袋）

这是本项目花时间最多的部分。做法是 **hold-out 空间交叉验证**：人为把目标站的真值遮住，
用邻站重建，再和真值比。59 个站按省分层抽样，**训练期 1991–2005，验证期 2006–2020，不重叠**。

### 结论一：别直接抄最近站

| 方法 | 平均气温 MAE | 说明 |
|---|---|---|
| 最近站（nearest） | **2.91 ℃** | 直觉做法，但最差 |
| 反距离加权（idw） | 1.57 ℃ | |
| 气候距平法（clima） | **0.75 ℃** | 邻站值 + 本站月基准 − 邻站月基准 |

最近站还有 −1.3~−2.3 ℃ 的**系统偏差**（邻站海拔/纬度不同却照搬），clima 只有 −0.03 ℃。
稳定性也差 20–30 倍：抽掉最近的那个站，nearest 的补给值平均变化 1.62 ℃，clima 只变 0.057 ℃。

**为什么 clima 更好**：它不信任单个站，用 12 个邻站加权，并且先把「本地气候背景」扣掉
再插值 —— 相当于只插"距平"，不插"绝对值"。

### 结论二：高级算法只值 5%

试了 10 多种方法（克里金、回归克里金、GBR、RBF、DINEOF 低秩、时空克里金、analog、KNN……），
配对显著性检验后的结论：

- **克里金显著更准，但只准 0.044 ℃**（占 clima 的 5.6%）—— 统计上站得住，实用上感觉不到
- **降水上克里金反而显著更差**（+0.079 mm）—— 降水不满足二阶平稳假设，必须用比值法
- **纯坐标方法全线崩盘**（只给经纬度、不给邻站观测值 → MAE 2.43 ℃，差 3.3 倍）
  ⇒ **起作用的不是"坐标"这个数字，而是"邻站当天实测值 + 气候去趋势"**

### 结论三：真正有效的是"跨要素"

数据里 **95–99% 的坏格是单指标坏**，同一行其它要素还是好的。于是可以让气温缺测时，
用它自己的湿度/风速/日照 + 邻站的 7 个要素一起回归（岭回归，91 维特征）：

| 指标 | clima | 跨要素回归（own6） | 提升 |
|---|---|---|---|
| 平均气温 | 0.83 ℃ | **0.38 ℃** | −54.6% |
| 最高气温 | 1.02 ℃ | **0.67 ℃** | −34.7% |
| 最低气温 | 1.11 ℃ | **0.64 ℃** | −42.0% |
| 相对湿度 | 5.06 % | **3.71 %** | −26.6% |
| 日照时数 | 1.15 h | **1.06 h** | −7.9% |
| 降水 | 1.61 mm | 不用（更差） | — |

但这招**有前提**：必须目标站那一行还有其它要素可用。整行全坏时特征全缺，模型会崩到
比 clima 差 85%。所以最后做成了**逐指标门控**：

```
气温三项：该日其它要素有效数 ≥ 5 才走跨要素回归，否则退回 clima
湿度/风速/日照：≥ 6
降水：永远用 clima
```

### 三次自我纠错

这个项目的实验记录里有三次"我先做错了，后来推翻了"：

1. 第一轮验证没过滤哨兵值 → 降水 MAE 算出 3474 mm 的荒谬数字（真值约 1.8 mm）
2. 曾假设"邻站数 k > 6 就饱和" → k 扫描到 32 才发现在 **k=24 触底**，假设错了
3. 第九轮测出跨要素回归降 56%，**兴奋地以为找到银弹** → 第十轮换遮蔽模式（整行坏）
   发现 +85% 劣化；再上第十一轮做全库坏值形态普查，才定下门控方案

完整推导和所有数字见 [`docs-异常值修复方法依据说明.md`](docs-异常值修复方法依据说明.md)（600+ 行，
含 9 个章节：方法清单 / 验证设计 / 结果 / 为什么结果会变 / k 敏感性 / 地统计与显著性 /
最终选型 / 端到端验收 / 怎么复现）。

---

## 端到端验证

不止做 hold-out，还往真实数据里注入坏值看能不能救回来：

- 30 个靶站 × 4 类坏值（哨兵值大 / 哨兵值小 / 超下限 / 置空）× 7 指标 = **840 条**
- 结果：**840 条全部被扫描到、840 条全部修出、恢复率 100%**，可信度全部是最高档 `observed`
- 反向对照：不注入的干净库 vs 注入库，坏行数差值 **恰好等于坏值日数**（无虚报无漏报）
- 与最近站对比：气温 MAE **0.646 vs 1.622 ℃**（好 2.5 倍）

---

## 数据目录与文件

`mirror_dbs.py` 产出的 `db_mirror/`：

```
db_mirror/
├── tdengine/weather_data/
│   ├── station_1124.parquet      # 每站一个文件，876 个
│   └── ...
├── mysql/
│   ├── weather_station_info.parquet   # 909 个站点的坐标/省份/海拔
│   └── ...（31 张表）
└── _mirror_manifest.json          # 行数校验、抓取时间
```

**每个 parquet 的字段**：

| 字段 | 说明 |
|---|---|
| `ts` | UTC 时间戳 |
| `ts_bj` | 北京时间 |
| `date_true` | **修正后的真实日期**（已减 100 年 + 闰日修正） |
| `avg_temperature` | 平均气温 ℃ |
| `max_temperature` | 最高气温 ℃ |
| `min_temperature` | 最低气温 ℃ |
| `avg_humidity` | 相对湿度 % |
| `precipitation` | 降水量 mm |
| `avg_wind_speed` | 平均风速 m/s |
| `sunshine_hours` | 日照时数 h |

---

## 上线部署

见 [`weather_api/DEPLOY.md`](weather_api/DEPLOY.md)：上传两个目录 → 装依赖 → 一条命令启动 →
systemd/NSSM 守护 → 放行端口 → Nginx HTTPS。

**最容易漏的一步**：云服务器控制台的安全组要单独放行 8900（除了系统防火墙）。

---

## 已知限制

- **只读**：API 不改任何数据，所有查询走本地 parquet
- **无鉴权**：上线前请自己在 Nginx 层加 Basic Auth / 限流
- **单机**：duckdb 直接扫 parquet，没有分布式；876 站规模下 overview 约 1.4 s，其余接口多在 100 ms 内
- **数据不在 git 里**：`db_mirror/` 有 392 MB，放在 Release 附件（367 MB 压缩包）。`git clone` 只得到代码
- **降水不适用跨要素回归/克里金**：这是实测结论，不是没实现

---

## 免责声明

- 本仓库不含任何真实数据、库地址、账号或令牌。用 `mirror_dbs.py` 时请自行通过环境变量提供凭据，
  并确保你有权限访问对应数据库。
- 修复算法输出的值是**统计推断**，不是观测值。`/api/repair/daily` 的每条结果都带 `quality` 字段：

  | quality | 含义 |
  |---|---|
  | `observed` | 由邻站当日实测值推得，最可信 |
  | `temporal` | 只能靠本站前后日推得 |
  | `climate` | 只能用该月气候平均值，**等同气候态而非实况** |

  默认不会把气候态伪装成观测（`max_gap_days=7` 保护），长期停更段请勿直接当观测使用。
- 数据集里有个别极端值超出官方历史记录（如全国最高 57.7 ℃），属**源数据口径问题**，代码照实返回，
  已在接口文档中标注，请自行判断是否采信。

## License

MIT
