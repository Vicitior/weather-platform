# -*- coding: utf-8 -*-
"""
气象数据查询层 —— 全部基于本地镜像 parquet（db_mirror/），离线可用。

时间口径：源库时间戳被整体 +100 年，本层统一用 ts_true = ts_bj - 100 年（真实年份 1951~2025）。
数据口径：极值/排行/聚合一律用 weather_ok（过滤 32766/999999 等哨兵值）；
          日值明细返回原始值，另附 is_valid 标志。

所有函数返回 pandas.DataFrame 或 dict，供 main.py 与测试脚本共用。
"""
import os
import re
import math
import time
import datetime
import duckdb
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
# 数据根目录可用环境变量覆盖，便于端到端测试在隔离沙箱里跑真实代码：
#   WEATHER_DB_ROOT  指向镜像根目录（默认 <项目>/db_mirror）
_DB_ROOT = os.environ.get("WEATHER_DB_ROOT") or os.path.join(os.path.dirname(HERE), "db_mirror")
ROOT = _DB_ROOT
TD_GLOB = os.environ.get("WEATHER_TD_GLOB") or \
    os.path.join(ROOT, "tdengine", "weather_data", "*.parquet").replace("\\", "/")
MY_DIR = (os.environ.get("WEATHER_MY_DIR") or os.path.join(ROOT, "mysql")).replace("\\", "/")

# 允许查询的气象指标白名单（列名 → 中文名+单位）
METRICS = {
    "avg_temperature": ("平均气温", "℃"),
    "max_temperature": ("最高气温", "℃"),
    "min_temperature": ("最低气温", "℃"),
    "avg_humidity":    ("平均相对湿度", "%"),
    "precipitation":   ("降水量", "mm"),
    "avg_wind_speed":  ("平均风速", "m/s"),
    "sunshine_hours":  ("日照时数", "h"),
}

# 省份归一化 SQL 片段（province 列 → 标准名）
PROVINCE_NORM_SQL = """
CASE
  WHEN province LIKE '新疆%' THEN '新疆'
  WHEN province LIKE '内蒙古%' THEN '内蒙古'
  WHEN province LIKE '广西%' THEN '广西'
  WHEN province LIKE '西藏%' THEN '西藏'
  WHEN province LIKE '宁夏%' THEN '宁夏'
  ELSE regexp_replace(province, '(省|壮族自治区|维吾尔自治区|回族自治区|自治区)$', '')
END
""".strip()

_CACHE = {}


def _con():
    if "c" in _CACHE:
        return _CACHE["c"]
    c = duckdb.connect()
    c.execute("SET threads TO 8")
    c.execute("PRAGMA memory_limit='4GB'")
    # 源库时间戳被整体 +100 年（2051-2125 ↔ 实际 1951-2025）。
    # 但反向减 100 年有个坑：2100 是世纪年（非闰年）而 2000 是闰年，
    # 源库 ETL 做「日历 +100 年」位移时把 2000-02-29 钳到了 2100-02-28，
    # 且留下 12:00 相位（实证：2100-02-28 12:00 − 100 年 + 1 天 = 2000-02-29）。
    # 全库实测只有 684 行带 12:00，全部落在 2100-02-28，涉及 684 个站；
    # 简单减 100 年会让这些站出现「2000-02-28 有两行、2000-02-29 没有数据」。
    # 故这里统一给出逆变换后的 date_true，下游一律用它而不要自己减 100 年。
    c.execute("""
        CREATE OR REPLACE VIEW _weather_raw AS
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS station_id,
               w.*
        FROM read_parquet('%s', filename=true) AS w
    """ % TD_GLOB)
    c.execute("""
        CREATE OR REPLACE VIEW weather AS
        SELECT *,
               (CASE WHEN hour(ts_bj) = 12
                     THEN CAST(CAST(ts_bj AS DATE) - INTERVAL 100 YEAR AS DATE) + INTERVAL 1 DAY
                     ELSE CAST(CAST(ts_bj AS DATE) - INTERVAL 100 YEAR AS DATE)
                END)::DATE AS date_true
        FROM _weather_raw
    """)
    c.execute("""
        CREATE OR REPLACE VIEW weather_ok AS
        SELECT * FROM weather
        WHERE avg_temperature BETWEEN -100 AND 100
          AND max_temperature BETWEEN -100 AND 100
          AND min_temperature BETWEEN -100 AND 100
          AND avg_humidity    BETWEEN 0 AND 100
          -- 降水必须先解码再判有效：32700（微量）与 3xxxx（雨/雪/露霜编码）
          -- 都是有效观测，早期直接 BETWEEN 0 AND 1000 会把它们当哨兵值，
          -- 从而整行丢弃 2,302,000 行（13.57%）。解码口径见 decode_precipitation。
          AND (CASE
                 WHEN precipitation IS NULL THEN NULL
                 WHEN precipitation IN (32766, 999990, 999998, 999999) THEN NULL
                 WHEN precipitation = 32700 THEN 0.05
                 WHEN precipitation >= 30000 AND precipitation < 32700
                      THEN (precipitation % 1000) / 10.0
                 WHEN precipitation >= 0 AND precipitation <= 1000 THEN precipitation
                 ELSE NULL
               END) IS NOT NULL
          AND avg_wind_speed  BETWEEN 0 AND 200
          AND sunshine_hours  BETWEEN 0 AND 24
    """)
    c.execute("""
        CREATE OR REPLACE VIEW weather_hist AS
        SELECT *, CAST(date_true AS TIMESTAMP) AS ts_true FROM weather
    """)
    c.execute("""
        CREATE OR REPLACE VIEW weather_hist_ok AS
        SELECT *, CAST(date_true AS TIMESTAMP) AS ts_true FROM weather_ok
    """)
    c.execute("""CREATE OR REPLACE VIEW stations AS
                 SELECT station_id, station_code,
                        (%s) AS province_norm, province AS province_raw,
                        station_name, latitude, longitude, altitude
                 FROM read_parquet('%s/weather_station_info.parquet')""" % (PROVINCE_NORM_SQL, MY_DIR))
    # 降水解码宏：源库降水列混用「普通量 / 32700 微量码 / 3xxxx 类型+量」三种写法，
    # 所有聚合都必须走 prcp_mm() 换算成 mm，否则 13.57% 的行会被当哨兵值排除。
    c.execute("""
        CREATE OR REPLACE MACRO prcp_mm(x) AS (CASE
            WHEN x IS NULL THEN NULL
            WHEN x IN (32766, 999990, 999998, 999999) THEN NULL
            WHEN x = 32700 THEN 0.05
            WHEN x >= 30000 AND x < 32700 THEN (x % 1000) / 10.0
            WHEN x >= 0 AND x <= 1000 THEN x
            ELSE NULL
        END)
    """)
    _CACHE["c"] = c
    return c


def _df(sql):
    return _con().execute(sql).df()


def metric_ok(metric):
    return metric in METRICS


# ---------------- 基础 ----------------

def overview():
    """数据集概览：站点数、行数、时间范围"""
    r = _df("""
        SELECT count(*) AS rows_total, count(DISTINCT station_id) AS stations,
               min(ts_true)::VARCHAR AS date_from, max(ts_true)::VARCHAR AS date_to
        FROM weather_hist_ok
    """).iloc[0].to_dict()
    return r


def provinces():
    """省份列表（含站点数，名称已归一化）"""
    return _df("""
        SELECT province_norm AS province, count(*) AS station_count
        FROM stations GROUP BY 1 ORDER BY 2 DESC, 1
    """)


def list_stations(province=None, search=None, limit=100, offset=0):
    """站点列表：按省筛选（归一化后）、按名称/站号模糊搜索。
    返回 (DataFrame, 总数)"""
    w = ["1=1"]
    if province:
        w.append("province_norm = '%s'" % province.replace("'", "''"))
    if search:
        s = search.replace("'", "''")
        w.append("(station_name LIKE '%%%s%%' OR station_code LIKE '%%%s%%'"
                 " OR CAST(station_id AS VARCHAR) = '%s')" % (s, s, s))
    where = " AND ".join(w)
    total = int(_df("SELECT count(*) AS n FROM stations WHERE %s" % where).iloc[0]["n"])
    rows = _df("""
        SELECT station_id, station_code, province_norm AS province, province_raw,
               station_name, latitude, longitude, altitude
        FROM stations WHERE %s
        ORDER BY station_id LIMIT %d OFFSET %d
    """ % (where, int(limit), int(offset)))
    return rows, total


def station_detail(station_id):
    """单站详情 + 数据覆盖情况。没有该站返回 None"""
    d = _df("""
        SELECT station_id, station_code, province_norm AS province, province_raw,
               station_name, latitude, longitude, altitude
        FROM stations WHERE station_id = %d
    """ % int(station_id))
    if d.empty:
        return None
    cov = _df("""
        SELECT count(*) AS rows_total,
               count(*) FILTER (WHERE avg_temperature BETWEEN -100 AND 100) AS temp_valid,
               count(*) FILTER (WHERE precipitation BETWEEN 0 AND 1000) AS prcp_valid,
               min(ts_true)::VARCHAR AS date_from, max(ts_true)::VARCHAR AS date_to,
               count(DISTINCT year(ts_true)) AS years_with_data
        FROM weather_hist WHERE station_id = %d
    """ % int(station_id)).iloc[0].to_dict()
    out = d.iloc[0].to_dict()
    out["data"] = cov
    return out


# ---------------- 日值 ----------------

def daily(station_id, date=None, start=None, end=None, limit=500):
    """某站日值：date=单日；start/end=区间。返回原始值 + is_valid（是否通过物理范围校验）"""
    w = ["w.station_id = %d" % int(station_id)]
    if date:
        w.append("w.date_true = DATE '%s'" % str(date)[:10])
    else:
        if start:
            w.append("w.date_true >= DATE '%s'" % str(start)[:10])
        if end:
            w.append("w.date_true <= DATE '%s'" % str(end)[:10])
    return _df("""
        SELECT station_id, date_true AS date,
               avg_temperature, max_temperature, min_temperature,
               avg_humidity, precipitation, avg_wind_speed, sunshine_hours,
               (avg_temperature BETWEEN -100 AND 100
                AND max_temperature BETWEEN -100 AND 100
                AND min_temperature BETWEEN -100 AND 100
                AND avg_humidity BETWEEN 0 AND 100
                AND precipitation BETWEEN 0 AND 1000
                AND avg_wind_speed BETWEEN 0 AND 200
                AND sunshine_hours BETWEEN 0 AND 24) AS is_valid
        FROM weather_hist w WHERE %s
        ORDER BY date LIMIT %d
    """ % (" AND ".join(w), int(limit)))


def day_all_stations(date, metric="max_temperature", order="desc", limit=100, province=None):
    """某一天全部站点的某指标排行。默认：某日全国最高温排行"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    prov = ""
    if province:
        prov = " AND s.province_norm = '%s'" % province.replace("'", "''")
    return _df("""
        SELECT w.station_id, s.station_code, s.province_norm AS province, s.station_name,
               w.date_true AS date,
               w.%s AS value
        FROM weather_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE w.date_true = DATE '%s'%s
        ORDER BY value %s NULLS LAST LIMIT %d
    """ % (metric, str(date)[:10], prov, direction, int(limit)))


# ---------------- 极值排行 ----------------

def station_records(station_id, metric="max_temperature", order="desc", limit=10):
    """某站历史极值 Top N（含出现日期），如：漠河历史最高温出现在哪天"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    return _df("""
        SELECT w.station_id, s.station_code, s.province_norm AS province, s.station_name,
               w.date_true AS date,
               w.%s AS value
        FROM weather_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE w.station_id = %d
        ORDER BY value %s NULLS LAST LIMIT %d
    """ % (metric, int(station_id), direction, int(limit)))


def global_records(metric="max_temperature", order="desc", limit=10,
                   year=None, date_from=None, date_to=None, province=None):
    """全数据集极值排行 Top N。可限定：某年 / 日期区间 / 省份。
    如：1951-2025 全国最高温 Top10、2020 年最热的一天…"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    w = ["1=1"]
    if year:
        w.append("year(w.date_true) = %d" % int(year))
    if date_from:
        w.append("w.date_true >= DATE '%s'" % str(date_from)[:10])
    if date_to:
        w.append("w.date_true <= DATE '%s'" % str(date_to)[:10])
    if province:
        w.append("s.province_norm = '%s'" % province.replace("'", "''"))
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    return _df("""
        SELECT w.station_id, s.station_code, s.province_norm AS province, s.station_name,
               w.date_true AS date,
               w.%s AS value
        FROM weather_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE %s
        ORDER BY value %s NULLS LAST LIMIT %d
    """ % (metric, " AND ".join(w), direction, int(limit)))


# ---------------- 聚合序列 ----------------

def annual(station_id=None, province=None, year_from=None, year_to=None, limit=100000):
    """逐年统计：全国平均 / 单站 / 分省"""
    w = []
    if station_id:
        w.append("w.station_id = %d" % int(station_id))
    if year_from:
        w.append("year(w.ts_true) >= %d" % int(year_from))
    if year_to:
        w.append("year(w.ts_true) <= %d" % int(year_to))
    if province:
        w.append("s.province_norm = '%s'" % province.replace("'", "''"))
    where = " AND ".join(w) if w else "1=1"
    if station_id:
        sel = """w.station_id, s.station_code, s.province_norm AS province, s.station_name,
                 year(w.ts_true) AS year,"""
        gb = "GROUP BY 1,2,3,4,5"
        ob = "ORDER BY year"
    elif province:
        sel = "s.province_norm AS province, year(w.ts_true) AS year,"
        gb = "GROUP BY 1,2"
        ob = "ORDER BY province, year"
    else:
        sel = "year(w.ts_true) AS year,"
        gb = "GROUP BY 1"
        ob = "ORDER BY year"
    return _df("""
        SELECT %s
               round(avg(w.avg_temperature), 2) AS temp_avg,
               round(max(w.max_temperature), 1) AS temp_max,
               round(min(w.min_temperature), 1) AS temp_min,
               round(sum(prcp_mm(w.precipitation)), 1) AS prcp_total,
               round(avg(w.avg_humidity), 1) AS hum_avg,
               round(sum(w.sunshine_hours), 1) AS sun_total,
               count(*) AS station_days
        FROM weather_hist_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE %s
        %s %s LIMIT %d
    """ % (sel, where, gb, ob, int(limit)))


def monthly(station_id, year, metric="avg_temperature"):
    """某站某年逐月统计（12 行）：均值 + 当月有值天数"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    return _df("""
        SELECT month(w.ts_true) AS month,
               round(avg(w.{m}), 2) AS {m},
               count(*) FILTER (WHERE w.{m} IS NOT NULL) AS days
        FROM weather_hist_ok w
        WHERE w.station_id = {sid} AND year(w.ts_true) = {y}
        GROUP BY 1 ORDER BY 1
    """.format(m=metric, sid=int(station_id), y=int(year)))


def trend(station_id, metric="avg_temperature", agg="month", date_from=None, date_to=None):
    """时间序列（画趋势图用）：agg = day | month | year"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    if agg not in ("day", "month", "year"):
        raise ValueError("agg 必须是 day/month/year")
    w = ["w.station_id = %d" % int(station_id)]
    if date_from:
        w.append("w.date_true >= DATE '%s'" % str(date_from)[:10])
    if date_to:
        w.append("w.date_true <= DATE '%s'" % str(date_to)[:10])
    return _df("""
        SELECT date_trunc('{u}', w.ts_true) AS period,
               round(avg(w.{m}), 2) AS value,
               count(*) AS days
        FROM weather_hist_ok w
        WHERE {w}
        GROUP BY 1 ORDER BY 1
    """.format(u=agg, m=metric, w=" AND ".join(w)))


def monthly_climate(station_id, period_from=1991, period_to=2020):
    """气候基准：某站气候期的逐月平均值（默认 1991-2020 WMO 标准气候平均期）"""
    return _df("""
        SELECT month(w.ts_true) AS month,
               round(avg(w.avg_temperature), 1) AS temp_c,
               round(avg(prcp_mm(w.precipitation)), 1) AS prcp_mm,
               round(avg(w.avg_humidity), 0) AS hum_pct,
               round(avg(w.avg_wind_speed), 1) AS wind_ms,
               round(avg(w.sunshine_hours), 1) AS sun_h,
               count(*) AS days
        FROM weather_hist_ok w
        WHERE w.station_id = %d AND year(w.ts_true) BETWEEN %d AND %d
        GROUP BY 1 ORDER BY 1
    """ % (int(station_id), int(period_from), int(period_to)))


# ================= 扩展查询（v0.3） =================

# ---------------- 多站查询 / 对比 ----------------

def daily_multi(station_ids, date=None, start=None, end=None, metric=None, limit=5000):
    """多站日值：一次查多个站（对比图直接用）。
    metric 给定时只返回该指标（行更窄），不给则返回全部 7 指标。"""
    ids = [int(x) for x in station_ids if str(x).strip()]
    if not ids:
        raise ValueError("station_ids 不能为空")
    if len(ids) > 20:
        raise ValueError("一次最多 20 个站")
    idl = ",".join(str(i) for i in ids)
    w = ["w.station_id IN (%s)" % idl]
    if date:
        w.append("w.date_true = DATE '%s'" % str(date)[:10])
    else:
        if start:
            w.append("w.date_true >= DATE '%s'" % str(start)[:10])
        if end:
            w.append("w.date_true <= DATE '%s'" % str(end)[:10])
    if metric:
        if not metric_ok(metric):
            raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
        sel = "w.station_id, s.station_name, w.date_true AS date, w.%s AS value" % metric
    else:
        sel = ("w.station_id, s.station_name, w.date_true AS date, "
               "w.avg_temperature, w.max_temperature, w.min_temperature, "
               "w.avg_humidity, w.precipitation, w.avg_wind_speed, w.sunshine_hours")
    return _df("""
        SELECT %s
        FROM weather_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE %s ORDER BY w.station_id, date LIMIT %d
    """ % (sel, " AND ".join(w), int(limit)))


def compare_station_years(station_id, month_from=1, month_to=12,
                          year_a=1961, year_b=1990, year_c=1991, year_d=2020):
    """两段时期（各 30 年，默认 1961-1990 vs 1991-2020）逐月气候对比 → 气候变化信号。
    或同指标两年对比：compare_station_years(1124, year_a=2015, year_b=2024, month_from=7, month_to=7)。"""
    def _blk(y1, y2, tag):
        return """
        SELECT month(w.ts_true) AS month, '%s' AS period,
               round(avg(w.avg_temperature), 2) AS temp_c,
               round(sum(prcp_mm(w.precipitation))/count(DISTINCT year(w.ts_true)), 1) AS prcp_mm,
               round(avg(w.max_temperature), 1) AS tmax_c,
               round(avg(w.min_temperature), 1) AS tmin_c,
               count(*) AS days
        FROM weather_hist_ok w
        WHERE w.station_id = %d AND year(w.ts_true) BETWEEN %d AND %d
              AND month(w.ts_true) BETWEEN %d AND %d
        GROUP BY 1, 2""" % (tag, int(station_id), int(y1), int(y2),
                            int(month_from), int(month_to))
    parts = [_blk(year_a, year_b, "%d-%d" % (year_a, year_b))]
    if year_c is not None and year_d is not None:
        parts.append(_blk(year_c, year_d, "%d-%d" % (year_c, year_d)))
    sql = " UNION ALL BY NAME ".join(parts)
    out = _df(sql).sort_values(["period", "month"]).reset_index(drop=True)
    return out


# ---------------- 空间查询 ----------------

def stations_near(lat, lng, radius_km=100, limit=50):
    """按经纬度找附近的气象站（haversine 近似，返回带距离，按近到远）。

    radius_km: 真实生效的半径过滤（None 表示不限，只按距离取前 limit 个）。
    """
    r = "" if radius_km is None else "WHERE d.distance_km <= %f" % float(radius_km)
    return _df("""
        WITH q AS (SELECT CAST(%f AS DOUBLE) AS lat, CAST(%f AS DOUBLE) AS lng)
        SELECT d.station_id, d.station_code, d.province, d.station_name,
               d.latitude, d.longitude, d.altitude, d.distance_km
        FROM (
          SELECT s.station_id, s.station_code, s.province_norm AS province, s.station_name,
                 s.latitude, s.longitude, s.altitude,
                 round(6371.0 * 2 * asin(sqrt(
                   pow(sin(radians(s.latitude - q.lat) / 2), 2) +
                   cos(radians(q.lat)) * cos(radians(s.latitude)) *
                   pow(sin(radians(s.longitude - q.lng) / 2), 2)
                 )), 1) AS distance_km
          FROM stations s, q
          WHERE s.latitude IS NOT NULL AND s.longitude IS NOT NULL
        ) d
        %s
        ORDER BY d.distance_km LIMIT %d
    """ % (float(lat), float(lng), r, int(limit)))


def stations_in_bbox(lat_min, lat_max, lng_min, lng_max, limit=500):
    """矩形范围内的站点（画地图用）"""
    return _df("""
        SELECT station_id, station_code, province_norm AS province, station_name,
               latitude, longitude, altitude
        FROM stations
        WHERE latitude BETWEEN %f AND %f AND longitude BETWEEN %f AND %f
        ORDER BY station_id LIMIT %d
    """ % (float(lat_min), float(lat_max), float(lng_min), float(lng_max), int(limit)))


# ---------------- 天数统计 ----------------

def days_count(station_id=None, province=None, year=None, year_from=None, year_to=None,
               metric="max_temperature", op="gte", threshold=35.0):
    """阈值天数统计：如某站每年 ≥35℃ 的高温日数（回去几年逐年给）。
    op: gte/ge/lte/le/gt/lt/eq/ne"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    OPS = {"gte": ">=", "ge": ">=", "lte": "<=", "le": "<=",
           "gt": ">", "lt": "<", "eq": "=", "ne": "<>"}
    if op not in OPS:
        raise ValueError("op 必须是 %s" % ", ".join(OPS))
    cond = "(w.%s %s %f)" % (metric, OPS[op], float(threshold))
    w = []
    if station_id:
        w.append("w.station_id = %d" % int(station_id))
    if province:
        w.append("s.province_norm = '%s'" % province.replace("'", "''"))
    if year:
        w.append("year(w.ts_true) = %d" % int(year))
    if year_from:
        w.append("year(w.ts_true) >= %d" % int(year_from))
    if year_to:
        w.append("year(w.ts_true) <= %d" % int(year_to))
    where = " AND ".join(w) if w else "1=1"
    if station_id:
        sel, gb, ob = ("w.station_id, s.station_name, year(w.ts_true) AS year,",
                       "GROUP BY 1, 2, 3", "ORDER BY year")
    elif province:
        sel, gb, ob = ("s.province_norm AS province, year(w.ts_true) AS year,",
                       "GROUP BY 1, 2", "ORDER BY province, year")
    else:
        sel, gb, ob = ("year(w.ts_true) AS year,", "GROUP BY 1", "ORDER BY year")
    return _df("""
        SELECT %s count(*) AS days
        FROM weather_hist_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE %s AND %s
        %s %s
    """ % (sel, where, cond, gb, ob))


# ---------------- 农业气象 ----------------

def agri_stations(station_id, base_temp=10.0, year_from=None, year_to=None,
                  month_from=3, month_to=10, station=None):
    """农业气象指标：逐年 ≥base_temp 有效积温(℃·d)、≥10℃ 初日/终日、无霜期长度（min_temperature>0 最长连续天数）。"""
    sid = int(station_id)
    w = ["w.station_id = %d" % sid]
    if year_from:
        w.append("year(w.ts_true) >= %d" % int(year_from))
    if year_to:
        w.append("year(w.ts_true) <= %d" % int(year_to))
    where = " AND ".join(w)
    return _df("""
        WITH d AS (
            SELECT date_true AS date,
                   year(date_true) AS yr,
                   avg_temperature, min_temperature
            FROM weather_ok WHERE station_id = %d
        ),
        dd AS (
            SELECT * FROM d
            WHERE month(date) BETWEEN %d AND %d %s %s
        ),
        gdd AS (
            SELECT yr,
                   sum(greatest(avg_temperature - %f, 0)) AS gdd_sum,
                   count(*) FILTER (WHERE avg_temperature >= %f) AS gdd_days,
                   min(date) FILTER (WHERE avg_temperature >= %f) AS gdd_start,
                   max(date) FILTER (WHERE avg_temperature >= %f) AS gdd_end
            FROM dd GROUP BY yr
        ),
        seq AS (
            SELECT yr, date, min_temperature,
                   date - row_number() OVER (PARTITION BY yr ORDER BY date)::INT AS grp
            FROM d WHERE min_temperature > 0 AND min_temperature IS NOT NULL
        ),
        frost AS (
            SELECT yr, count(*) AS frost_free_days,
                   min(date) AS first_frost_free, max(date) AS last_frost_free
            FROM seq GROUP BY yr, grp
        ),
        frost_best AS (
            SELECT yr, max(frost_free_days) AS frost_free_days
            FROM frost GROUP BY yr
        )
        SELECT g.yr AS year,
               round(g.gdd_sum, 1) AS gdd10,
               g.gdd_days AS days_ge_%d,
               g.gdd_start::VARCHAR AS gdd_start,
               g.gdd_end::VARCHAR AS gdd_end,
               f.frost_free_days,
               round(coalesce(g.gdd_sum, 0) / nullif(g.gdd_days, 0), 1) AS gdd_daily_mean
        FROM gdd g LEFT JOIN frost_best f ON g.yr = f.yr
        ORDER BY g.yr
    """ % (sid, int(month_from), int(month_to),
           (" AND year(date) >= %d" % int(year_from)) if year_from else "",
           (" AND year(date) <= %d" % int(year_to)) if year_to else "",
           float(base_temp), float(base_temp), float(base_temp), float(base_temp),
           int(base_temp)))


def agri_compare(stations, base_temp=10.0, year_from=1991, year_to=2020):
    """多站农业气象多年平均对比（选种/引种参考）：积温、生长季、无霜期。
    stations: [{"station_id":1124,"name":"漠河"}, ...] 或 [1124, 1213]"""
    import numpy as np
    rows = []
    for it in stations:
        sid = int(it["station_id"] if isinstance(it, dict) else it)
        name = it.get("name") if isinstance(it, dict) else None
        try:
            df = agri_stations(sid, base_temp=base_temp, year_from=year_from, year_to=year_to)
        except Exception:
            continue
        if df.empty:
            continue
        g = df["gdd10"].dropna()
        rows.append({
            "station_id": sid,
            "station_name": name or (D_stations_name(sid) or ("station %d" % sid)),
            "years": len(g),
            "gdd10_mean": round(float(g.mean()), 1) if len(g) else None,
            "gdd10_max": round(float(g.max()), 1) if len(g) else None,
            "gdd10_min": round(float(g.min()), 1) if len(g) else None,
            "frost_free_mean": round(float(df["frost_free_days"].dropna().mean()), 1)
                                if df["frost_free_days"].dropna().size else None,
        })
    return pd.DataFrame(rows)


def D_stations_name(sid):
    r = _df("SELECT station_name FROM stations WHERE station_id = %d" % int(sid))
    return None if r.empty else r.iloc[0]["station_name"]


# ---------------- 年份排行 / 历史上的今天 ----------------

def year_rank(year, metric="avg_temperature", order="desc", limit=100, station_min_days=300):
    """某年站点排行：如 2024 年均温最高的站（要求该年有 ≥station_min_days 天数据，防小样本）。"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    return _df("""
        SELECT w.station_id, s.station_code, s.province_norm AS province, s.station_name,
               year(w.ts_true) AS year,
               round(avg(w.{m}), 2) AS value,
               count(*) AS days
        FROM weather_hist_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE year(w.ts_true) = {y}
        GROUP BY 1, 2, 3, 4, 5
        HAVING count(*) >= {md}
        ORDER BY value {d} NULLS LAST LIMIT {n}
    """.format(m=metric, y=int(year), md=int(station_min_days), d=direction, n=int(limit)))


def on_this_day(station_id, month, day, metric="max_temperature", order="desc", limit=75):
    """历史上的今天：某站所有年份里「同月同日」该指标排行（含出现年份）。
    如：漠河 7 月 15 日历年最高温排行。"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    return _df("""
        SELECT w.station_id, s.station_name,
               w.date_true AS date,
               year(w.date_true) AS year,
               w.{m} AS value
        FROM weather_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE w.station_id = {sid}
          AND month(w.date_true) = {mo}
          AND day(w.date_true) = {dy}
        ORDER BY value {d} NULLS LAST LIMIT {n}
    """.format(m=metric, sid=int(station_id), mo=int(month), dy=int(day),
               d=direction, n=int(limit)))


# ---------------- 极值追查辅助 ----------------

def values_above(metric="max_temperature", threshold=50.0, order="desc", limit=100):
    """全库超阈值记录追查：如所有 ≥50℃ 的记录（含站点/日期/数值），用于数据质量排查。"""
    if not metric_ok(metric):
        raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    direction = "DESC" if str(order).lower() == "desc" else "ASC"
    return _df("""
        SELECT w.station_id, s.station_code, s.province_norm AS province, s.station_name,
               w.date_true AS date,
               w.{m} AS value
        FROM weather_ok w LEFT JOIN stations s ON w.station_id = s.station_id
        WHERE w.{m} {cmp} {th}
        ORDER BY value {d} NULLS LAST LIMIT {n}
    """.format(m=metric, cmp=">=" if direction == "DESC" else "<=",
               th=float(threshold), d=direction, n=int(limit)))


def search_station_by_name(name, limit=50):
    """站名搜索（独立函数：给自然语言入口用）"""
    s = str(name).replace("'", "''")
    return _df("""
        SELECT station_id, station_code, province_norm AS province, station_name,
               latitude, longitude, altitude
        FROM stations WHERE station_name LIKE '%%%s%%' LIMIT %d
    """ % (s, int(limit)))


# ---------------- 填报表（站点 × 12 月模板） ----------------

def report_table(station_id, year_from=1991, year_to=2020):
    """按「气象数据填报表」模板口径，一站聚合 12 个月的值，并内置模板校验规则。

    行口径（多年区间时取多年平均/极端）：
      最高气温=月极端最高 / 最低气温=月极端最低 / 平均气温·相对湿度·平均风速·日照时数=月均值 /
      降水量=月合计（mm）。校验规则来自用户给的 Word 模板：
      最高气温 <40℃；0<RHmean<100；0<u<30（≥30 需海拔和站点二测检测）；
      0<n<18（≥18 需检测）；Pmax 在 7-9 月且站点不在干旱区（年降水 <200mm 视为干旱区）。
    """
    sid = int(station_id)
    y1, y2 = int(year_from), int(year_to)
    if y1 > y2:
        raise ValueError("year_from 不能大于 year_to")
    info = station_detail(sid)
    if info is None:
        raise ValueError("站点 %d 不存在" % sid)
    df = _df("""
        SELECT month(w.ts_true) AS month, count(*) AS days,
               max(w.max_temperature) AS tmax, min(w.min_temperature) AS tmin,
               avg(w.avg_temperature) AS tavg, avg(w.avg_humidity) AS rh,
               avg(w.avg_wind_speed) AS wind, avg(w.sunshine_hours) AS sun,
               sum(prcp_mm(w.precipitation)) AS prcp_total
        FROM weather_hist_ok w
        WHERE w.station_id = %d AND year(w.ts_true) BETWEEN %d AND %d
        GROUP BY 1 ORDER BY 1
    """ % (sid, y1, y2))
    n_years = y2 - y1 + 1
    months = {int(r["month"]): r for _, r in df.iterrows()} if not df.empty else {}
    mv = {}   # 每行 12 个月值
    def _g(m, k, nd):
        r = months.get(m)
        if r is None or r["days"] in (None, 0):
            return None
        return round(float(r[k]), nd)
    mv["tmax"] = [_g(m, "tmax", 1) for m in range(1, 13)]
    mv["tmin"] = [_g(m, "tmin", 1) for m in range(1, 13)]
    mv["tavg"] = [_g(m, "tavg", 1) for m in range(1, 13)]
    mv["rh"] = [_g(m, "rh", 1) for m in range(1, 13)]
    mv["wind"] = [_g(m, "wind", 2) for m in range(1, 13)]
    mv["sun"] = [_g(m, "sun", 2) for m in range(1, 13)]
    # 降水量：月合计 ÷ 年数 = 多年平均月降水量
    mv["prcp"] = []
    for m in range(1, 13):
        r = months.get(m)
        mv["prcp"].append(round(float(r["prcp_total"]) / n_years, 1)
                          if r is not None and int(r["days"]) > 0 else None)

    def _flags(key, rule_fn):
        out = []
        for i, v in enumerate(mv[key]):
            if v is not None and rule_fn(v):
                out.append("%d月 %.1f" % (i + 1, v))
        return out

    # 降水量汇总：Pmax 月份 / 年合计 / 干旱区判定
    pmax_month, pmax_v = None, None
    if any(v is not None for v in mv["prcp"]):
        pmax_v = max(v for v in mv["prcp"] if v is not None)
        pmax_month = mv["prcp"].index(pmax_v) + 1
        annual = round(sum(v for v in mv["prcp"] if v is not None), 1)  # 各月均值之和=年均
    else:
        annual = None
    arid = annual is not None and annual < 200.0

    rows = [
        {"category": "气象数据", "metric": "最高气温", "unit": "℃", "rule": "<40℃",
         "months": mv["tmax"], "flags": _flags("tmax", lambda v: v >= 40.0)},
        {"category": "气象数据", "metric": "最低气温", "unit": "℃", "rule": "",
         "months": mv["tmin"], "flags": []},
        {"category": "气象数据", "metric": "平均气温", "unit": "℃", "rule": "",
         "months": mv["tavg"], "flags": []},
        {"category": "气象数据", "metric": "相对湿度", "unit": "%", "rule": "0<RHmean<100",
         "months": mv["rh"], "flags": _flags("rh", lambda v: v <= 0.0 or v >= 100.0)},
        {"category": "气象数据", "metric": "平均风速", "unit": "m/s", "rule": "0<u<30，≥30 时需海拔和站点二测检测",
         "months": mv["wind"], "flags": _flags("wind", lambda v: v >= 30.0)},
        {"category": "气象数据", "metric": "日照时数", "unit": "h", "rule": "0<n<18，≥18 时需海拔和站点二测检测",
         "months": mv["sun"], "flags": _flags("sun", lambda v: v >= 18.0)},
        {"category": "气象数据", "metric": "降雨量", "unit": "mm", "rule": "0<P，Pmax 在 7-9 月，最大值所在的站点不在干旱区",
         "months": mv["prcp"], "flags": _flags("prcp", lambda v: v <= 0.0), "months_are_totals": False,
         "note": "多年平均月降水量（月合计÷年数）"},
    ]
    summary = {
        "n_years": n_years, "days_total": int(df["days"].sum()) if not df.empty else 0,
        "pmax_month": pmax_month, "pmax_mm": pmax_v,
        "pmax_in_jul_sep": pmax_month in (7, 8, 9) if pmax_month else None,
        "annual_prcp_mm": annual, "arid_zone_lt200mm": arid,
    }
    return {
        "station": {"station_id": sid, "station_code": info.get("station_code"),
                    "station_name": info.get("station_name"), "province": info.get("province"),
                    "latitude": float(info["latitude"]) if info.get("latitude") is not None else None,
                    "longitude": float(info["longitude"]) if info.get("longitude") is not None else None,
                    "altitude": float(info["altitude"]) if info.get("altitude") is not None else None},
        "period": "%d-%d" % (y1, y2),
        "rows": rows,
        "summary": summary,
        "manual_rows": [{"category": "土壤数据", "months": [None] * 12},
                        {"category": "作物数据", "months": [None] * 12}],
    }


# ---------------- 异常值修复（邻站替补） ----------------

# 物理有效范围（与 weather_ok 视图一致）
PHYS_RANGE = {
    "avg_temperature": (-100.0, 100.0),
    "max_temperature": (-100.0, 100.0),
    "min_temperature": (-100.0, 100.0),
    "avg_humidity": (0.0, 100.0),
    "precipitation": (0.0, 1000.0),
    "avg_wind_speed": (0.0, 200.0),
    "sunshine_hours": (0.0, 24.0),
}

# 判据「合理性边界」——只用于异常检测时判定「数值可信度」，不参与 weather_ok 视图过滤。
# 依据：全库 1696 万行剔除旧边界外的值后，真实极值为
#   平均气温 [-46.7, 50.0]、最高气温 [-40.3, 53.0]、最低气温 [-60.0, 39.2]、
#   降水 [0, 298.8]、风速 [0, 40.0]、日照 [0, 22.8]（见 _tmp_range_impact.py）。
# 这里留出安全余量（气温 ±5~6℃、降水 1100mm/日 已是世界纪录量级、风速 60m/s）。
# 注意：把 PHYS_RANGE 直接收窄到这些值几乎无收益（全库只多判 9 行无效），
# 所以保留 PHYS_RANGE 不动以免改变既有统计口径，另设 PHYS_STRICT 供检测使用。
PHYS_STRICT = {
    "avg_temperature": (-60.0, 55.0),
    "max_temperature": (-55.0, 55.0),
    "min_temperature": (-65.0, 45.0),
    "avg_humidity": (0.0, 100.0),
    "precipitation": (0.0, 1000.0),
    "avg_wind_speed": (0.0, 60.0),
    "sunshine_hours": (0.0, 24.0),
}
_ROUND = {"avg_temperature": 1, "max_temperature": 1, "min_temperature": 1,
          # avg_humidity 源列是 BIGINT（整型），修复值若带小数回写会被截断，故取整
          "avg_humidity": 0, "precipitation": 1, "avg_wind_speed": 2, "sunshine_hours": 2}

# ---------------- 降水源库编码解码（3xxxx / 32700） ----------------
#
# 源库降水列混用了三种写法，必须解码后再当数值用：
#   1) 普通量            0 ~ 1000（mm，直接可用）
#   2) 32700            微量降水 <0.1mm 的专用码（全库 1,923,044 行 / 871 站）
#   3) 3xxxx 复合编码    3 + [类型1位] + [降水量3位, 单位 0.1mm]
#                       30xxx = 雨、31xxx = 雪、32xxx = 露/霜（其他凝结）
#      例：31021 → 雪 2.1mm；30056 → 雨 5.6mm；32001 → 露/霜 0.1mm
#
# 判据（见 ../../make_full_daily_table.py 的推导）：末3位分布呈幂律递减（0.1mm 85,968 /
# 0.2mm 29,690 / 0.3mm 6,441 …），符合降水量而非天气现象码；31xxx 均温 -7.1℃、
# 零下占比 81.5%、冬季占 80%（正是雪），30xxx 均温 +2.5℃、均量 6.29mm（正是雨），
# 32xxx 均量仅 0.16mm、秋季峰、均温 13℃（正是露/霜）；且解码后与邻站降水的相关系数
# 达 0.705（30xxx）/ 0.814（31xxx），若是现象码不可能有这种量级相关。
# 反证：若把 32700 按 3+2+700 解成 70.0mm，新乡年均降水会从 585mm 变成 3,034mm。
#
# ⚠ 重要：这套解码必须同时用在「有效性判断」上。早期 weather_ok 视图用
#   `precipitation BETWEEN 0 AND 1000` 一刀切，会把 32700（1,923,044 行）
#   与 3xxxx（369,966 行）判为无效并把【整行】丢弃 —— 全库 16,966,797 行里
#   2,302,000 行（13.57%）因此从概览/排行/聚合里消失，而这些行的其余 6 个
#   指标其实是好的。修复办法见 PRCP_SQL / PRCP_VALID_SQL：把编码换算成 mm
#   再做范围判断，只有真缺测（32766/999990/999998/999999）才判无效。
PRECIP_SENTINELS = (32766.0, 999990.0, 999998.0, 999999.0)
PRECIP_TYPE = {30: "雨", 31: "雪", 32: "露/霜"}
PRECIP_MICRO = 32700.0          # 微量降水专用码，按 0.05mm 计


def decode_precipitation(v):
    """把源库降水值解码成 (mm, 说明)。

    mm=None 表示缺测/无法解码；说明是给表里「天气电码」列看的中文解释。
    普通数值原样返回（说明为 None）。
    """
    if v is None:
        return None, None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None, None
    if not (fv == fv) or fv in (float("inf"), float("-inf")):   # NaN / inf
        return None, None
    if fv in PRECIP_SENTINELS:
        return None, "缺测（源库占位 %d）" % int(fv)
    if fv == PRECIP_MICRO:
        return 0.05, "微量降水（源库码 32700，<0.1mm，按 0.05mm 计）"
    if 30000.0 <= fv < 32700.0:
        typ = int(fv) // 1000
        amt = round((int(fv) % 1000) / 10.0, 1)
        return amt, "%s %.1fmm（源库码 %d）" % (PRECIP_TYPE.get(typ, "未知"), amt, int(fv))
    if 0.0 <= fv <= 1000.0:
        return fv, None
    return None, "超出合理范围（%g）" % fv


# SQL 版解码（逐行等价于 decode_precipitation，供视图与聚合使用）。
# 只用 CASE + 算术，duckdb 可向量化，性能与直接读列同量级。
#   32766/999990/999998/999999 → NULL（真缺测）
#   32700                      → 0.05（微量降水，<0.1mm）
#   30000~32699                → (值 % 1000) / 10.0（3+类型+降水量×0.1mm）
#   0~1000                     → 原值
#   其它                        → NULL（超出可解码范围，按无效处理）
PRCP_SQL = """(CASE
    WHEN {c} IS NULL THEN NULL
    WHEN {c} IN (32766, 999990, 999998, 999999) THEN NULL
    WHEN {c} = 32700 THEN 0.05
    WHEN {c} >= 30000 AND {c} < 32700 THEN ({c} % 1000) / 10.0
    WHEN {c} >= 0 AND {c} <= 1000 THEN {c}
    ELSE NULL
  END)"""


def prcp_sql(col="precipitation"):
    """返回把源库降水列解码成 mm 的 SQL 表达式（NULL = 缺测/无法解码）。"""
    return PRCP_SQL.format(c=col)


def prcp_valid_sql(col="precipitation"):
    """返回「该降水值可用」的 SQL 布尔表达式（有编码值的行不再被判无效）。"""
    return "%s IS NOT NULL" % prcp_sql(col)


# 温度三列的源库缺测占位：这些不是观测值，不参与「谁把温度关系搞乱」的推理。
TEMP_SENTINELS = (32766.0, 999998.0, 999999.0, 999990.0, 32700.0)


def _locate_temp_culprit(v):
    """在 max/avg/min 三列里定位最可疑的那一列（温度矛盾时决定改谁）。

    为什么需要它：温度自洽性判据（max<min / max<avg / avg<min / 日较差过大）
    说的是一组关系不成立，但**不能把参与的三列全当坏值**。例如
    avg=60℃、min=0.6℃、max=32766（缺测）：矛盾来自 avg 被写坏，
    若连 min 一起改会把 0.6℃ 这个好观测毁掉（实测曾被改成 -2.4℃）。

    判定顺序：
      0) 先剔除源库缺测占位（32766 等），它们已由 out_of_range 单独报出；
      1) 若恰好只有一列越过 PHYS_STRICT 合理性边界 → 就是它；
      2) 否则用三角形关系（正常日 avg ≈ (max+min)/2）取偏差最大的一列。
    """
    mx = v.get("max_temperature")
    av = v.get("avg_temperature")
    mn = v.get("min_temperature")
    clean = {}
    for k, val in (("max_temperature", mx), ("avg_temperature", av),
                   ("min_temperature", mn)):
        try:
            clean[k] = None if (val is not None and float(val) in TEMP_SENTINELS) else val
        except (TypeError, ValueError):
            clean[k] = None
    mx, av, mn = clean["max_temperature"], clean["avg_temperature"], clean["min_temperature"]

    # 优先级 1：恰好一列越过 PHYS_STRICT 合理性边界 → 一定是它坏（即使三列关系恰好自洽，
    # 例如 max=80℃ 配 avg=5/min=-6 关系成立但 80℃ 本身不可能）。
    over = [k for k, val in (("max_temperature", mx), ("avg_temperature", av),
                             ("min_temperature", mn))
            if val is not None and not (PHYS_STRICT[k][0] <= val <= PHYS_STRICT[k][1])]
    if len(over) == 1:
        return over[0]

    # 三列本来就自洽 → 没有坏列可归因，返回 None（调用方不会把整行都当坏值）
    def _triple_fine():
        bits = []
        if mx is not None and mn is not None:
            bits.append(mx >= mn)
        if mx is not None and av is not None:
            bits.append(av <= mx + 0.5)
        if av is not None and mn is not None:
            bits.append(-0.5 <= (av - mn) <= 25.0)
        return all(bits) if bits else False

    if _triple_fine():
        return None

    # 优先级 2：留一法自洽检验 —— 去掉哪一列后，剩下两列的关系成立，哪一列就是坏列。
    # 这比「三角形偏差最大」可靠：max=-2 / avg=30 / min=-8 时三角偏差 max 与 min 并列
    # 最大（都是 70），会误选 max；但去掉 avg 后 max(-2) > min(-8)、日较差 6℃ 完全自洽。
    def _pair_ok(ka, va, kb, vb):
        pair = {ka: va, kb: vb}
        if "max_temperature" in pair and "min_temperature" in pair and len(pair) == 2:
            return pair["max_temperature"] >= pair["min_temperature"]
        if "max_temperature" in pair and "avg_temperature" in pair:
            return pair["avg_temperature"] <= pair["max_temperature"] + 0.5
        if "avg_temperature" in pair and "min_temperature" in pair:
            sp = pair["avg_temperature"] - pair["min_temperature"]
            return -0.5 <= sp <= 25.0
        return True

    vals3 = {"max_temperature": mx, "avg_temperature": av, "min_temperature": mn}
    solos = []
    for drop in ("max_temperature", "avg_temperature", "min_temperature"):
        rest = [(k, v) for k, v in vals3.items() if k != drop and v is not None]
        if len(rest) == 2 and _pair_ok(rest[0][0], rest[0][1], rest[1][0], rest[1][1]):
            solos.append(drop)
    if len(solos) == 1:
        return solos[0]

    # 优先级 3：三角形关系兜底（正常日 avg ≈ (max+min)/2）
    cand = []
    if mx is not None and av is not None and mn is not None:
        cand = [("max_temperature", abs(mx - (2 * av - mn))),
                ("min_temperature", abs(mn - (2 * av - mx))),
                ("avg_temperature", abs(av - (mx + mn) / 2.0))]
    elif av is not None and mn is not None:
        # 没有 max 时无法三角定位；日较差类矛盾绝大多数是 avg 被写坏
        # （新乡 2021 那 7 行全部是 avg 被填入了乱码值）
        cand = [("avg_temperature", abs(av - mn))]
    elif mx is not None and av is not None:
        cand = [("avg_temperature", abs(mx - av))]
    if not cand:
        return None
    if over:
        cand = [c for c in cand if c[0] in over]
    if solos:
        cand = [c for c in cand if c[0] in solos]
    cand.sort(key=lambda x: -x[1])
    return cand[0][0]

REPAIR_METHODS = ("auto", "own6", "clima", "blend", "krig", "idw", "alt_idw", "nearest")
LAPSE_RATE = 0.0065  # 温度海拔直减率 ℃/m

# 跨要素门控法（own6）的启用条件：目标站该日除待修指标外，至少还有几个指标有效。
#
# 第十三轮 87 站全量分层验证（validate_repair13.py / repair_validation13.json）修正了
# 第十二轮的乐观结论：第十二轮只测了「单格坏」与「整行全坏」两个极端，漏掉中间层。
# 按「目标站当日其它要素可用个数 0~6」逐层拆开后（MAE 相对 clima 的变化）：
#   层0 +110.0% / 层1 +82.7% / 层2 +56.1% / 层3 +29.9% / 层4 +1.9% / 层5 -27.5% / 层6 -54.6%（气温）
# 即：其它要素只剩 1~3 个时，跨要素特征大多是噪声，岭回归外推会显著劣化。
# 故门控必须按指标分别定在「增益首次稳定为负」的层数上，宁可不修也不能改坏。
OWN6_GATE = {
    "avg_temperature": 5,   # 层5 -27.5%（真实库覆盖 14.5%），层6 虽 -54.6% 但只覆盖 1.9%
    "max_temperature": 6,   # 层5 仅 -1.5%（边际），层6 -34.7%
    "min_temperature": 6,   # 层5 仅 -3.3%，层6 -41.9%
    "avg_humidity": 6,      # 层5 -5.0% 但层6 -26.6%，层6 的总误差削减更大
    "precipitation": 6,     # 见 OWN6_UNSUITED，降水实际永不走 own6
    "avg_wind_speed": 6,    # 层5 -0.1%，层6 -4.8%
    "sunshine_hours": 6,    # 层5 +4.9% 反而更差，层6 才 -7.9%
}
# 降水不能用 own6：log1p 空间 + 岭回归在间断非高斯变量上严重外推，
# 第十三轮实测层0 MAE +3818.9%、层6 仍 +4.1%，与克里金同理。
OWN6_UNSUITED = ("precipitation",)
# 岭回归正则强度（闭式解，不惩罚截距；数值来自第九/十二轮验证脚本）
OWN6_ALPHA = 3.0

# 各指标经 hold-out 交叉验证选出的最优方法（见 异常值修复方法依据说明.md）
#   own6  = 门控跨要素岭回归（本站其它要素距平 + 全部邻站 7 要素距平 + 坐标/月相位）。
#           第十三轮 87 站全量分层：气温在「其它 6 要素全好」时 MAE -54.6%、
#           5 个可用时 -27.5%；不足门控则自动退回 clima（零劣化）。
#           降水实测全线劣化（层0 +3818.9%）故完全不用，见 OWN6_UNSUITED。
#   clima = 邻站值 + 月气候差（加性）/ 降水用 normal-ratio 比值法
#   blend = 0.5*clima + 0.5*前后日距平（引入时间连续性）
#   krig  = 距平克里金（第四轮：气温/日照/风速显著优于 clima 但仅约 5%，
#           代价是多查一次 30 年长序列，故不进 auto 默认，需显式指定）
#
#   ⚠ 2026-11-26 八站真实遮蔽审计（exports/_own6_audit.json、_own6_audit_clean.json）：
#     8 个不同气候区站点（新乡/乌鲁木齐/漠河/吐鲁番/郑州/盐源/哈密/喀什）
#     2016-2019 年各 30 点/指标真实遮蔽后重建，own6 与 clima 逐点对照。
#     ⚠ 必须先剔除数据污染站：盐源 1610 在 2014-2020 年 avg_temperature 与
#       avg_wind_speed 整列互换（ta 年均 2~3℃、ws 年均 12~13 m/s，正常应是 12℃/2m/s），
#       用它算出来的 own6 误差（最高气温 +9.86℃）完全是脏数据造成的假象。
#     剔除盐源后剩 7 站（exports/_own6_audit_clean.json）：
#        avg_temperature  own6 赢 7/7，平均 ΔMAE −0.70℃  → own6
#        max_temperature  own6 赢 5/7，平均 Δ−0.10℃       → own6
#        min_temperature  own6 赢 6/7，平均 Δ−0.52℃       → own6
#        avg_humidity     own6 赢 2/7，平均 Δ+1.34%       → clima
#        precipitation    两者完全相同（own6 本就不适用）  → clima
#        avg_wind_speed   own6 赢 3/7，平均 Δ−0.04m/s     → own6（边际）
#        sunshine_hours   own6 赢 1/7，平均 Δ+0.59h       → clima
#     结论：气温三件套 + 风速用 own6，湿度与日照回落 clima。
BEST_METHOD = {
    "avg_temperature": "own6", "max_temperature": "own6", "min_temperature": "own6",
    "avg_humidity": "clima", "precipitation": "clima",
    "avg_wind_speed": "own6", "sunshine_hours": "clima",
}
# krig 不适用的指标（降水为间断非高斯事件，加性克里金实测显著更差 → 自动退回 clima）
KRIG_UNSUITED = ("precipitation",)


def _daylength(lat, doy):
    """天文可照时数（小时）—— 日出到日落的几何上限，不考虑地形遮挡与大气折射。

    cos(H) = -tan(lat)·tan(dec)，dec = 23.44° · sin(360/365 · (doy-81))
    日照时数在物理上不可能超过这个值（超过即为不可能的观测）。
    极昼/极夜时 cos(H) 超出 [-1,1]，分别按 24h / 0h 处理。
    """
    if lat is None or not np.isfinite(lat):
        return None
    dec = math.radians(23.44) * math.sin(2.0 * math.pi * (doy - 81) / 365.0)
    x = -math.tan(math.radians(float(lat))) * math.tan(dec)
    if x <= -1.0:
        return 24.0
    if x >= 1.0:
        return 0.0
    return 24.0 * math.acos(x) / math.pi


def _temp_consistency(vals):
    """温度三列内部一致性检查：返回违反的规则名列表。

    物理约束：min_temperature ≤ avg_temperature ≤ max_temperature。
    这是**最硬**的判据 —— 不依赖任何历史统计，只要三列同时有值就必定成立。
    2026-11-26 全库量化（16,966,797 行）：
      max<avg  11 行、avg<min 1,660 行、max<min  8 行，合计 1,671 行。
    其中 avg<min 的 1,660 行里有 1,640 行集中在站点 1610（列互换），
    旧版 repair_scan 只检查 max<min（8 行），漏掉了 99.5% 的自洽性错误。

    另加「日较差」约束：avg − min 正常在 0~25℃ 之间（日最高与最低的差的一半左右）。
    avg − min > 25 意味着平均气温比当日最低温高了 25℃ 以上，不可能（一天之中
    ≥一半时间接近日最低，均值不可能被拉到远高于它）；avg − min < −5 意味着
    平均气温低于当日最低温，直接违反 min ≤ avg。
    2026-11-26 全库扫描（剔除哨兵值后）：共 1,154 行命中，其中 1,132 行集中在
    1610 站（列互换），其余散在 9 站共 22 行 —— 都是真错误（如新乡 2021 年
    avg=47/49/50 而当日 min≈0℃，实为 avg 与 avg_humidity 两列对调）。
    """
    out = []
    mx, av, mn = vals.get("max_temperature"), vals.get("avg_temperature"), vals.get("min_temperature")
    if mx is not None and mn is not None and mx < mn:
        out.append("max<min")
    if mx is not None and av is not None and mx < av:
        out.append("max<avg")
    if av is not None and mn is not None and av < mn:
        out.append("avg<min")
    if av is not None and mn is not None:
        sp = av - mn
        if sp > 25.0:
            out.append("avg-min>25（日较差不可能）")
        elif sp < -5.0:
            out.append("avg-min<-5（均值远低于日最低）")
    return out


def _haversine_km(lat1, lon1, lat2, lon2):
    """两点大圆距离（km）。"""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _exp_variogram(Z, D, min_pairs=30, n_bins=8):
    """实验变差 γ̂(h)=½E[(z_i-z_j)²]，按站间距等频分箱。Z:(天×站) D:(站×站)km。"""
    n = Z.shape[1]
    hs, gs, ws = [], [], []
    for i in range(n):
        for j in range(i + 1, n):
            a, b = Z[:, i], Z[:, j]
            ok = np.isfinite(a) & np.isfinite(b)
            if ok.sum() < min_pairs:
                continue
            hs.append(float(D[i, j]))
            gs.append(0.5 * float(np.mean((a[ok] - b[ok]) ** 2)))
            ws.append(float(ok.sum()))
    if len(hs) < 6:
        return None
    hs, gs, ws = np.asarray(hs), np.asarray(gs), np.asarray(ws)
    o = np.argsort(hs)
    hs, gs, ws = hs[o], gs[o], ws[o]
    hb, gb, wb = [], [], []
    for b in np.array_split(np.arange(len(hs)), n_bins):
        if len(b) == 0:
            continue
        hb.append(float(np.average(hs[b], weights=ws[b])))
        gb.append(float(np.average(gs[b], weights=ws[b])))
        wb.append(float(ws[b].sum()))
    if len(hb) < 3:
        return None
    return np.asarray(hb), np.asarray(gb), np.asarray(wb)


def _fit_exponential(h, g, w):
    """拟合 γ(h)=c₀+c(1-e^{-h/a})。

    纯 numpy：固定 a 时 c₀、c 是线性最小二乘，故对 a 做对数网格搜索即可，
    无需 scipy 多起点非线性拟合（更稳、更快、无额外依赖）。
    """
    if len(h) < 3 or not np.all(np.isfinite(g)):
        return None
    gmax = float(np.nanmax(g))
    if not np.isfinite(gmax) or gmax <= 0:
        return None
    sw = np.sqrt(w / w.max())
    e0 = 1.0 - np.exp(-h / max(float(h.max()), 1e-9))
    best = None
    for a in np.geomspace(max(float(h[h > 0].min()) if np.any(h > 0) else 1.0, 1.0),
                          float(h.max()) * 3.0, 40):
        e = 1.0 - np.exp(-h / a)
        A = np.column_stack([np.ones_like(h), e]) * sw[:, None]
        y = g * sw
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        c0, c = float(coef[0]), float(coef[1])
        if c0 < 0 or c < 0:
            # 约束失败 → 退化为「纯块金」或「纯结构」两种边界解，取残差小者
            for cand in ((max(c0, 0.0), 0.0), (0.0, max(c, 0.0))):
                r = np.sum(((cand[0] + cand[1] * e0) - g) ** 2 * w)
                if best is None or r < best[0]:
                    best = (r, (cand[0], cand[1], a))
            continue
        r = np.sum(((c0 + c * e) - g) ** 2 * w)
        if best is None or r < best[0]:
            best = (r, (c0, c, a))
    if best is None:
        return None
    return best[1]


def _krig_weights(D_sub, d0, par):
    """普通克里金：解 [[Γ,1],[1ᵀ,0]][w;μ]=[γ₀;1]，返回权重数组 w。"""
    c0, c, a = par
    a = max(a, 1e-9)
    n = len(d0)
    G = c0 + c * (1.0 - np.exp(-D_sub / a))
    np.fill_diagonal(G, 0.0)
    A = np.ones((n + 1, n + 1))
    A[:n, :n] = G
    A[n, n] = 0.0
    g0 = c0 + c * (1.0 - np.exp(-d0 / a))
    rhs = np.append(g0, 1.0)
    try:
        sol = np.linalg.solve(A, rhs)
    except np.linalg.LinAlgError:
        sol = np.linalg.lstsq(A, rhs, rcond=None)[0]
    return sol[:n]


def _clamp_round(v, m):
    lo, hi = PHYS_RANGE[m]
    return round(min(max(float(v), lo), hi), _ROUND[m])


def _resolve_range(date=None, start=None, end=None, year=None):
    """把 date/start/end/year 解析成具体日期区间 [d1, d2]。

    优先级：date > start/end > year。
    早期版本把 year 放在 start/end 之前，导致「同时传 year 与 start/end」时
    year 静默覆盖区间（实测 _resolve_range(None,'2013-10-01','2013-10-05',2013)
    返回整年）。现在区间参数优先，且 year 与 start/end 同时给时会校验一致性。
    """
    if date:
        return str(date)[:10], str(date)[:10]
    if start or end:
        d1 = str(start)[:10] if start else "1951-01-01"
        d2 = str(end)[:10] if end else "2025-12-31"
        if year:
            y = int(year)
            inside = d1 >= "%d-01-01" % y and d2 <= "%d-12-31" % y
            if inside:
                # year 只是重复约束，忽略即可
                pass
            else:
                raise ValueError(
                    "year=%d 与 start/end=%s~%s 冲突，请只给其中一种" % (y, d1, d2))
        return d1, d2
    if year:
        return "%d-01-01" % int(year), "%d-12-31" % int(year)
    raise ValueError("请指定 date 或 start/end 或 year")


def repair_scan(station_id, date=None, start=None, end=None, year=None, detail_limit=500):
    """缺测/异常扫描：日历表 LEFT JOIN 目标站原始行，找出
    缺测（该日无记录）、超物理范围（32766/999999 等哨兵值）、max<min 不自洽的天。
    返回 {station, period, summary{days, rows_present, per_metric, inconsistent_maxmin}, bad_rows[]}。"""
    sid = int(station_id)
    info = station_detail(sid)
    if info is None:
        raise ValueError("站点 %d 不存在" % sid)
    d1, d2 = _resolve_range(date, start, end, year)
    dc = "date_true"
    cond = "%s BETWEEN DATE '%s' AND DATE '%s' AND station_id = %d" % (dc, d1, d2, sid)

    aggs = []
    for m, (lo, hi) in PHYS_RANGE.items():
        aggs.append("count(t.%s) AS %s__have" % (m, m))
        if m == "precipitation":
            # 降水先解码再判有效：32700（微量）与 3xxxx（雨/雪/露霜编码）都是有效观测，
            # 按原始值判 BETWEEN 0 AND 1000 会把它们误报成 out_of_range 并去「修复」。
            aggs.append("count(*) FILTER (WHERE prcp_mm(t.precipitation) BETWEEN %f AND %f)"
                        " AS %s__valid" % (lo, hi, m))
        else:
            aggs.append("count(*) FILTER (WHERE t.%s BETWEEN %f AND %f) AS %s__valid"
                        % (m, lo, hi, m))
    s = _df("""
        WITH cal AS (SELECT unnest(generate_series(DATE '%s', DATE '%s', INTERVAL 1 DAY))::DATE AS date),
             t AS (SELECT %s AS date, * FROM weather_hist WHERE %s)
        SELECT count(DISTINCT cal.date) AS days, count(t.station_id) AS rows_present, %s,
               count(*) FILTER (WHERE t.max_temperature IS NOT NULL AND t.min_temperature IS NOT NULL
                                AND t.max_temperature < t.min_temperature) AS inconsistent_maxmin,
               count(*) FILTER (WHERE t.max_temperature IS NOT NULL AND t.avg_temperature IS NOT NULL
                                AND t.max_temperature < t.avg_temperature) AS inconsistent_maxavg,
               count(*) FILTER (WHERE t.avg_temperature IS NOT NULL AND t.min_temperature IS NOT NULL
                                AND t.avg_temperature < t.min_temperature) AS inconsistent_avgmin,
               count(*) FILTER (WHERE t.avg_temperature IS NOT NULL AND t.min_temperature IS NOT NULL
                                AND t.avg_temperature BETWEEN -60 AND 55 AND t.min_temperature BETWEEN -65 AND 45
                                AND (t.avg_temperature - t.min_temperature > 25
                                     OR t.avg_temperature - t.min_temperature < -5)) AS inconsistent_spread
        FROM cal LEFT JOIN t ON cal.date = t.date
    """ % (d1, d2, dc, cond, ", ".join(aggs))).iloc[0].to_dict()

    per_metric = {}
    for m in PHYS_RANGE:
        have = int(s["%s__have" % m])
        valid = int(s["%s__valid" % m])
        per_metric[m] = {"have": have, "valid": valid,
                         "invalid": have - valid, "missing": int(s["days"]) - have}

    detail = _df("""
        WITH cal AS (SELECT unnest(generate_series(DATE '%s', DATE '%s', INTERVAL 1 DAY))::DATE AS date),
             t AS (SELECT %s AS date, * FROM weather_hist WHERE %s)
        SELECT cal.date, t.station_id, t.avg_temperature, t.max_temperature, t.min_temperature,
               t.avg_humidity, t.precipitation, t.avg_wind_speed, t.sunshine_hours
        FROM cal LEFT JOIN t ON cal.date = t.date
        WHERE t.station_id IS NULL
           OR %s
           OR (t.max_temperature IS NOT NULL AND t.min_temperature IS NOT NULL
               AND t.max_temperature < t.min_temperature)
           OR (t.max_temperature IS NOT NULL AND t.avg_temperature IS NOT NULL
               AND t.max_temperature < t.avg_temperature)
           OR (t.avg_temperature IS NOT NULL AND t.min_temperature IS NOT NULL
               AND t.avg_temperature < t.min_temperature)
           OR (t.avg_temperature IS NOT NULL AND t.min_temperature IS NOT NULL
               AND t.avg_temperature BETWEEN -60 AND 55 AND t.min_temperature BETWEEN -65 AND 45
               AND (t.avg_temperature - t.min_temperature > 25
                    OR t.avg_temperature - t.min_temperature < -5))
        ORDER BY cal.date LIMIT %d
    """ % (d1, d2, dc, cond,
           " OR ".join(
               ("(prcp_mm(t.precipitation) IS NULL OR prcp_mm(t.precipitation) NOT BETWEEN %f AND %f)"
                % (lo, hi)) if m == "precipitation"
               else "(t.%s IS NULL OR t.%s NOT BETWEEN %f AND %f)" % (m, m, lo, hi)
               for m, (lo, hi) in PHYS_RANGE.items()),
           int(detail_limit)))
    rows = []
    for _, r in detail.iterrows():
        vals = {m: (None if pd.isna(r[m]) else float(r[m])) for m in PHYS_RANGE}
        if r["station_id"] is None or pd.isna(r["station_id"]):
            problems = ["row_missing"]
        else:
            problems = []
            for m, (lo, hi) in PHYS_RANGE.items():
                if vals[m] is None:
                    problems.append(m + ":missing")
                elif m == "precipitation":
                    # 降水原始值是编码：32700/3xxxx 解码后才是有效数值，
                    # 只有解码后仍越界（或无法解码）才算 out_of_range。
                    dec, _why = decode_precipitation(vals[m])
                    if dec is None:
                        problems.append(m + ":out_of_range")
                    elif dec < lo or dec > hi:
                        problems.append(m + ":out_of_range")
                elif vals[m] < lo or vals[m] > hi:
                    problems.append(m + ":out_of_range")
            # 温度三列内部一致性（物理约束 min ≤ avg ≤ max，不依赖历史统计）
            # 旧版只查 max<min（全库 8 行），漏掉 avg<min 的 1,660 行（99.5%）
            problems.extend(_temp_consistency(vals))
            # 日照不可能超过天文可照时数
            dl = _daylength(info.get("latitude"), pd.Timestamp(r["date"]).dayofyear)
            if vals["sunshine_hours"] is not None and dl is not None \
                    and vals["sunshine_hours"] > dl + 0.5:
                problems.append("sunshine>daylength")
        rows.append({"date": str(r["date"]), "values": vals, "problems": sorted(set(problems))})
    return {
        "station": {"station_id": sid, "station_name": info.get("station_name"),
                    "province": info.get("province")},
        "period": {"from": d1, "to": d2},
        "summary": {"days": int(s["days"]), "rows_present": int(s["rows_present"]),
                    "inconsistent_maxmin": int(s["inconsistent_maxmin"]),
                    "inconsistent_maxavg": int(s["inconsistent_maxavg"]),
                    "inconsistent_avgmin": int(s["inconsistent_avgmin"]),
                    "inconsistent_spread": int(s["inconsistent_spread"]),
                    "inconsistent_total": int(s["inconsistent_maxmin"]) + int(s["inconsistent_maxavg"])
                                          + int(s["inconsistent_avgmin"]),
                    "per_metric": per_metric},
        "bad_rows": rows,
        "bad_rows_shown": len(rows),
    }


def hidden_scan(station_id, date=None, start=None, end=None, year=None,
                metric=None, k=12, radius_km=200, sigma=1.0, spatial_sigma=6.0,
                baseline="rolling", detail_limit=500):
    """隐性异常扫描：找「物理范围内、但数值可疑」的值 —— 只报告，绝不自动修复。

    与 repair_scan 的区别：repair_scan 只抓哨兵值（32766/999999/超物理范围/缺测/温度自洽性），
    本函数抓的是「看着合理但其实错了」的隐性异常，用三条互补规则（2026-11-26 注入实验：
    210 个隐性异常，历史极值法 100% 精确/50% 检出，邻站对比 62% 精确/51% 检出，
    两者取并集 67% 精确/62% 检出）：

      record  超过本站该月历史极值 ± sigma 个稳健标准差（1.4826×MAD）。
              这是「知道一个地方最高值是多少再筛查」的思路 —— 零误报，但只抓超历史记录的。
              baseline="rolling"（默认）用该月前后各 15 年的滚动极值；baseline="fixed" 用
              1991-2020 固定期。**滚动是必须的**：新乡站实测固定期基线在 1950-70 年代产生
              132 条误报（占全部 156 条的 85%），根因是气候漂移（该站 1 月均温 1951-60 为
              -1.22℃、2011-20 为 +0.57℃，升温 1.8℃），拿现在的极值去判 70 年前的数据必然误报。
      spatial 当日值与同日邻站中位数的偏离超过 spatial_sigma 个稳健标准差（距平域，
              已扣各自月气候基准，消除海拔/纬度系统差）—— 能抓邻站都正常、只有本站离谱的。

    降水为偏态分布（气候中位数常为 0，一场暴雨 z 就爆表），故 spatial 阈值对降水更保守：
    只有偏离超过 spatial_sigma 倍且倍数达到 5× 才报，避免把真暴雨误报。

    超 PHYS_STRICT（更紧的合理性边界）的值也会被报出来，标 rule="implausible"。

    返回 {station, period, rules, baseline, summary, suspects[]}，每条 suspect 含
    {date, metric, value, rules[], confidence, detail{...}}，confidence=high 表示两条以上规则
    命中（最可信），medium 表示单条命中（需人工确认）。"""
    sid = int(station_id)
    info = station_detail(sid)
    if info is None:
        raise ValueError("站点 %d 不存在" % sid)
    d1, d2 = _resolve_range(date, start, end, year)
    ms = [metric] if metric else list(PHYS_RANGE.keys())
    for m in ms:
        if not metric_ok(m):
            raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    if baseline not in ("rolling", "fixed"):
        raise ValueError("baseline 只能是 rolling 或 fixed")

    cols = ", ".join(ms)
    # 1) 目标站区间原始值
    tgt = _df(f"""SELECT strftime(date_true, '%Y-%m-%d') AS d, {cols}
                 FROM weather_hist
                 WHERE station_id = {sid} AND date_true BETWEEN DATE '{d1}' AND DATE '{d2}'
                 ORDER BY date_true""")
    if tgt.empty:
        return {"station": {"station_id": sid, "station_name": info.get("station_name")},
                "period": {"from": d1, "to": d2}, "rules": ["record", "spatial"],
                "summary": {"days": 0, "suspects": 0, "by_metric": {}, "by_rule": {}},
                "suspects": []}
    tgt["d"] = tgt["d"].astype(str).str.slice(0, 10)
    tdt = pd.to_datetime(tgt["d"])
    tmo = tdt.dt.month.to_numpy()

    # 2) 逐月历史极值 + 稳健标准差（1.4826×MAD）
    #    baseline="rolling" 时按年滚动：目标区间的每一年，用该年 ±15 年的窗口拟合，
    #    这样 1950 年代的值不会被 1991-2020 的暖化极值误判（新乡实测减少 85% 误报）。
    tr_years = sorted({int(x) for x in tdt.dt.year.unique()})
    rolling = (baseline == "rolling")
    win = 15
    if rolling:
        y_lo = min(tr_years) - win
        y_hi = max(tr_years) + win
    else:
        y_lo, y_hi = 1991, 2020
    tr = _df(f"""SELECT year(date_true) AS yr, month(date_true) AS mo, {cols} FROM weather_hist
                 WHERE station_id = {sid} AND year(date_true) BETWEEN {y_lo} AND {y_hi}""")
    # 先算 (year, month) 粒度的极值与 MAD，滚动窗口再按年合并
    # 注意：滚动窗口必须**排除目标年本身**（leave-one-year-out）。若把目标年算进基线，
    # 该年的值就成了自己的极值，永远不可能超 —— 实测会退化成 record 规则 0 命中。
    clim_by_year = {}
    for yr in tr_years:
        if rolling:
            sub = tr[((tr["yr"] >= yr - win) & (tr["yr"] <= yr - 1))
                     | ((tr["yr"] >= yr + 1) & (tr["yr"] <= yr + win))]
        else:
            sub = tr[(tr["yr"] >= 1991) & (tr["yr"] <= 2020)]
        if sub.empty:
            sub = tr
        cdict = {}
        for m in ms:
            v = pd.to_numeric(sub[m], errors="coerce")
            lo, hi = PHYS_RANGE[m]
            ok = sub.loc[(v >= lo) & (v <= hi), ["mo"]].copy()
            ok[m] = v[(v >= lo) & (v <= hi)]
            if ok.empty:
                cdict[m] = None
                continue
            g = ok.groupby("mo")[m]
            med = g.median()
            mad = g.apply(lambda x: (x - x.median()).abs().median())
            sd = (1.4826 * mad).replace(0, np.nan)
            gmed, gmad = ok[m].median(), 1.4826 * (ok[m] - ok[m].median()).abs().median()
            med = med.reindex(range(1, 13)).fillna(gmed)
            sd = sd.reindex(range(1, 13)).fillna(gmad if gmad > 0 else 1.0)
            cdict[m] = {"lo": g.min().reindex(range(1, 13)).fillna(ok[m].min()),
                        "hi": g.max().reindex(range(1, 13)).fillna(ok[m].max()),
                        "med": med, "sd": sd}
        clim_by_year[yr] = cdict
    # 兼容：clim 供后续按 (year) 取
    clim = clim_by_year

    # 3) 邻站同日值（距平域对比用）
    nb = stations_near(info["latitude"], info["longitude"], radius_km=radius_km, limit=int(k) + 4)
    nb = nb[nb["station_id"] != sid].head(int(k))
    nbmeta = {int(r["station_id"]): {"name": str(r["station_name"]),
                                     "km": float(r["distance_km"])} for _, r in nb.iterrows()}
    nb_ids = list(nbmeta)
    nbm = {}
    if nb_ids:
        nq = _df(f"""SELECT station_id, strftime(date_true, '%Y-%m-%d') AS d, {cols}
                     FROM weather_hist
                     WHERE station_id IN ({','.join(map(str, nb_ids))})
                       AND date_true BETWEEN DATE '{d1}' AND DATE '{d2}'""")
        nq["d"] = nq["d"].astype(str).str.slice(0, 10)
        for m in ms:
            v = pd.to_numeric(nq[m], errors="coerce")
            lo, hi = PHYS_RANGE[m]
            v[(v < lo) | (v > hi)] = np.nan
            nq[m] = v
            nbm[m] = nq.groupby("d")[m].median()

    # 4) 逐日逐指标套用各条规则
    suspects = []
    by_metric = {m: 0 for m in ms}
    by_rule = {"record": 0, "spatial": 0, "implausible": 0, "both": 0}
    tgt_year = tdt.dt.year.to_numpy()
    for i, row in tgt.iterrows():
        d = row["d"]
        mo = int(tmo[i])
        yr = int(tgt_year[i])
        cy = clim.get(yr) or clim_by_year.get(min(clim_by_year), {})
        for m in ms:
            v = row[m]
            if pd.isna(v):
                continue
            v = float(v)
            lo, hi = PHYS_RANGE[m]
            if v < lo or v > hi:      # 超物理范围的归 repair_scan 管，这里不重复
                continue
            c = cy.get(m)
            rules, detail = [], {}
            # 规则0：超出「合理性边界」PHYS_STRICT（比 PHYS_RANGE 紧，但仍属硬判据）
            slo, shi = PHYS_STRICT[m]
            if v < slo or v > shi:
                rules.append("implausible")
                detail["implausible"] = {"strict_range": [slo, shi]}
            if c is None:
                if rules:
                    by_metric[m] += 1
                    for r in rules:
                        by_rule[r] += 1
                    suspects.append({"date": d, "metric": m, "value": round(v, 2),
                                     "rules": rules, "confidence": "medium",
                                     "detail": detail, "neighbors": []})
                continue
            sd = float(c["sd"].get(mo, 1.0))
            if not np.isfinite(sd) or sd <= 0:
                sd = 1.0
            # 规则1：超本站该月历史极值 ± sigma×稳健标准差（滚动基线）
            rec_lo = float(c["lo"].get(mo)) - sigma * sd
            rec_hi = float(c["hi"].get(mo)) + sigma * sd
            if v > rec_hi or v < rec_lo:
                rules.append("record")
                detail["record"] = {"month": mo, "hist_min": round(float(c["lo"].get(mo)), 2),
                                    "hist_max": round(float(c["hi"].get(mo)), 2),
                                    "limit": [round(rec_lo, 2), round(rec_hi, 2)],
                                    "baseline": ("rolling±%d年" % win) if rolling else "1991-2020"}
            # 规则2：与同日邻站中位偏离（降水用倍数法更保守）
            if m in nbm and d in nbm[m].index:
                nmed = float(nbm[m].loc[d])
                if np.isfinite(nmed):
                    if m == "precipitation":
                        dev = abs(v - nmed)
                        if dev >= spatial_sigma * sd and max(v, nmed) / max(min(v, nmed), 0.1) >= 5:
                            rules.append("spatial")
                            detail["spatial"] = {"neighbor_median": round(nmed, 2),
                                                 "ratio": round(max(v, nmed) / max(min(v, nmed), 0.1), 2)}
                    else:
                        if abs(v - nmed) >= spatial_sigma * sd:
                            rules.append("spatial")
                            detail["spatial"] = {"neighbor_median": round(nmed, 2),
                                                 "deviation": round(v - nmed, 2)}
            if not rules:
                continue
            confidence = "high" if len(rules) >= 2 else "medium"
            for r in rules:
                by_rule[r] += 1
            if len(rules) >= 2:
                by_rule["both"] += 1
            by_metric[m] += 1
            # 邻站明细（用了哪些站的数据判断）
            used = []
            if nb_ids:
                nq2 = _df(f"""SELECT station_id, {m} AS v FROM weather_hist
                              WHERE station_id IN ({','.join(map(str, nb_ids))})
                                AND strftime(date_true, '%Y-%m-%d') = '{d}'""")
                for _, nr in nq2.iterrows():
                    nv = nr["v"]
                    if pd.notna(nv) and PHYS_RANGE[m][0] <= nv <= PHYS_RANGE[m][1]:
                        s = int(nr["station_id"])
                        used.append({"station_id": s, "station_name": nbmeta[s]["name"],
                                     "distance_km": nbmeta[s]["km"], "value": round(float(nv), 2)})
                used.sort(key=lambda x: x["distance_km"])
            suspects.append({"date": d, "metric": m, "value": round(v, 2),
                             "rules": rules, "confidence": confidence,
                             "detail": detail, "neighbors": used})
        if len(suspects) >= int(detail_limit):
            break
    suspects.sort(key=lambda x: (x["date"], x["metric"]))
    return {
        "station": {"station_id": sid, "station_name": info.get("station_name"),
                    "province": info.get("province")},
        "period": {"from": d1, "to": d2},
        "rules": ["record", "spatial", "implausible"],
        "baseline": ("rolling±%d年" % win) if rolling else "1991-2020",
        "summary": {"days": len(tgt), "suspects": len(suspects),
                    "by_metric": by_metric, "by_rule": by_rule},
        "suspects": suspects[:int(detail_limit)],
    }


def repair_daily(station_id, date=None, start=None, end=None, year=None,
                 metrics=None, method="auto", k=12, radius_km=300, detail_limit=500,
                 allow_climate_fill=True, max_gap_days=7):
    """异常值修复：扫描坏值/缺测日，用邻站同日有效值插补。

    method:
      auto     按指标自动选最优法（默认，依据 hold-out 交叉验证，见 BEST_METHOD）
      clima    气候距平法：邻站值 +（目标站月基准 − 邻站月基准）；
               降水用 normal-ratio 比值法（比值限 0.3~3）
      blend    clima 与「本站前后日距平平均」各半（湿度/风速最优）
      krig     距平克里金：变差函数由邻站 1991-2020 长序列拟合，解普通克里金权重
               加权邻站距平后加回本站月基准。气温/日照显著优于 clima（约 5%），
               降水显著更差 → KRIG_UNSUITED 自动退回 clima
      idw      距离反比平方加权（1/d²）
      alt_idw  idw + 温度海拔订正（T_t = T_n − L·(h_t − h_n)，L=6.5℃/km）
      nearest  直接取最近邻站值（对照用，实测误差最大）

    k/radius_km：邻站个数与搜索半径。交叉验证显示 clima 的 k 一直增加到 24 才饱和，
    但每增站都会拉入气候条件更远的站点，默认取 k=12、半径 300km 兼顾精度与稳健。

    allow_climate_fill / max_gap_days：某些站是**整段停更**（数据集里 2020 年有 576 站
    只记录到 4 月底），此时邻站与本站前后日都没有观测，唯一能给出的是月气候平均值 ——
    那是气候态，不是实况。默认仍给出（quality=climate）但会在 warnings 汇总提示；
    置 allow_climate_fill=False 则这类值返回 None，绝不把气候态混进观测序列。
    max_gap_days 控制「连续缺测多少天以上算停更」，超过则不再用气候态填充（默认 7 天）。

    修复值裁剪回物理范围；返回贡献站明细（谁、多远、原值、订正后、权重）。"""
    if method != "auto" and method not in REPAIR_METHODS:
        raise ValueError("method 必须是 %s 之一" % ", ".join(REPAIR_METHODS))
    sid = int(station_id)
    info = station_detail(sid)
    if info is None:
        raise ValueError("站点 %d 不存在" % sid)
    lat, lng = info.get("latitude"), info.get("longitude")
    if lat is None or lng is None:
        raise ValueError("站点 %d 缺少经纬度，无法找邻站" % sid)
    alt_t = float(info["altitude"]) if info.get("altitude") is not None else None
    ms = list(metrics) if metrics else list(PHYS_RANGE.keys())
    for m in ms:
        if not metric_ok(m):
            raise ValueError("metric 必须是 %s" % ", ".join(METRICS))

    # 1) 扫描坏日（detail_limit 放大以拿全，输出时再截断）
    scan = repair_scan(sid, date=date, start=start, end=end, year=year,
                       detail_limit=max(int(detail_limit), 100000))
    d1, d2 = scan["period"]["from"], scan["period"]["to"]
    # 非「指标:问题」形式的问题 → 受影响的指标。
    # 温度三列自洽性与日较差、日照天文上限这三类判据输出的是不带指标前缀的短语
    # （如 "avg-min>25（日较差不可能）"、"max<avg"、"sunshine>daylength"），
    # 旧版只认 "指标:问题" 的写法，导致这些日子的 hit 恒为空、永远修不了 ——
    # 典型受害者是新乡 2021-01-16（avg=60℃），扫描能报出来却不会插补。
    #
    # ⚠ 但绝不能把「参与矛盾的所有列」都当坏值：例如 avg=60℃ / min=0.6℃ 时日较差
    #    矛盾是 avg 的问题，若连 min 一起改会把好观测（0.6℃）毁掉。
    #    定位逻辑统一放在模块级 _locate_temp_culprit()，这里直接复用。
    def _culprit_temp(v):
        return _locate_temp_culprit(v)

    _TEMPK = ("max_temperature", "avg_temperature", "min_temperature")

    def _problems_to_metrics(problems, allowed, values):
        got = set()
        tv = {k: values.get(k) for k in _TEMPK}
        for p in problems:
            if ":" in p:
                m = p.split(":", 1)[0]
                if m in allowed:
                    got.add(m)
            elif p.startswith("sunshine>daylength"):
                if "sunshine_hours" in allowed:
                    got.add("sunshine_hours")
            elif p.startswith(("max<min", "max<avg", "avg<min", "avg-min>25", "avg-min<-5")):
                c = _culprit_temp(tv)
                if c in allowed:
                    got.add(c)
        return sorted(got)

    # 不带指标前缀的判据 → 中文短语（写进 problem 列给人看）
    _PLAIN = (("max<min", "最高气温低于最低气温（不可能）"),
              ("max<avg", "最高气温低于平均气温（不可能）"),
              ("avg<min", "平均气温低于最低气温（不可能）"),
              ("avg-min>25", "日较差过大（>25℃），平均气温与最低气温不可能差这么多"),
              ("avg-min<-5", "日较差为负（平均气温低于最低气温），数据颠倒"),
              ("sunshine>daylength", "日照时数超过天文可照时数（不可能）"))

    def _first_plain_problem(probs, m):
        """为某指标挑一条最能解释它的中文判据（只用于 problem 列的可读展示）。"""
        for key, txt in _PLAIN:
            if any(p.startswith(key) for p in probs):
                return txt
        return "异常（判据未给出具体说明）"

    bad = []
    for row in scan["bad_rows"]:
        if "row_missing" in row["problems"]:
            hit = list(ms)
        else:
            hit = _problems_to_metrics(row["problems"], set(ms), row["values"])
        if hit:
            # 统一成 YYYY-MM-DD（pandas 读出的 date 列可能是 'YYYY-MM-DD HH:MM:SS' 字符串，
            # 而 _shift()/vals 的 key 是短格式，不一致会导致时间维永远匹配不上）
            bad.append((str(row["date"])[:10], hit, row["values"], row["problems"]))
        if len(bad) >= int(detail_limit):
            break

    # 2) 邻站（按距离近→远，取 k 个）
    neighbors = stations_near(lat, lng, radius_km=radius_km, limit=int(k) + 8)
    neighbors = neighbors[neighbors["station_id"] != sid].head(int(k))
    nmeta = [{"station_id": int(r["station_id"]), "station_name": str(r["station_name"]),
              "distance_km": float(r["distance_km"]),
              "latitude": float(r["latitude"]), "longitude": float(r["longitude"]),
              "altitude": None if pd.isna(r["altitude"]) else float(r["altitude"])}
             for _, r in neighbors.iterrows()]

    # 3) 目标站+邻站的原始日值（weather_hist 原始行，逐指标判有效性）
    ids = [sid] + [n["station_id"] for n in nmeta]
    raw = _df("""
        SELECT station_id, date_true AS date,
               avg_temperature, max_temperature, min_temperature, avg_humidity,
               precipitation, avg_wind_speed, sunshine_hours
        FROM weather_hist
        WHERE station_id IN (%s)
          AND date_true BETWEEN DATE '%s' AND DATE '%s'
    """ % (",".join(str(i) for i in ids), d1, d2))
    vals = {}
    for _, r in raw.iterrows():
        vals[(int(r["station_id"]), str(r["date"])[:10])] = {
            m: (None if pd.isna(r[m]) else float(r[m])) for m in PHYS_RANGE}

    # 4) 气候基准：各站 1991-2020 逐月气候值
    #    降水=月合计÷年数（即多年平均月降水），其余为月均值
    #    任何方法都可能因邻站区域缺测而需要降级到时间维/气候基准，故一律预先算好
    #    ⚠ 这里必须逐指标独立取有效值（per-metric FILTER），不能用 weather_hist_ok：
    #      该视图把 7 个指标用 AND 串起来，任一指标越界整行作废，实测会让
    #      avg_temperature 的可用样本被其他指标「连坐」丢掉约 9.4%（降水哨兵值最多），
    #      气候基准的样本量因此缩水，直接拉低所有降级路径的精度。
    base = {}
    if bad:
        cal = {"avg_temperature": "avg_temperature", "max_temperature": "max_temperature",
               "min_temperature": "min_temperature", "avg_humidity": "avg_humidity",
               "avg_wind_speed": "avg_wind_speed", "sunshine_hours": "sunshine_hours"}
        parts = []
        for m, (lo, hi) in PHYS_RANGE.items():
            if m == "precipitation":
                # 降水先解码再过滤：32700/3xxxx 解码成 mm 后才是可比数值
                f = ("FILTER (WHERE prcp_mm(precipitation) BETWEEN %f AND %f)"
                     % (lo, hi))
                parts.append("sum(prcp_mm(precipitation)) %s"
                             " / NULLIF(count(DISTINCT year(ts_true)) %s, 0)"
                             " AS precipitation" % (f, f))
            else:
                f = "FILTER (WHERE %s BETWEEN %f AND %f)" % (m, lo, hi)
                parts.append("avg(%s) %s AS %s" % (cal[m], f, m))
        b = _df("""
            SELECT station_id, month(ts_true) AS month, %s
            FROM weather_hist WHERE station_id IN (%s) GROUP BY 1, 2
        """ % (", ".join(parts), ",".join(str(i) for i in ids)))
        for _, r in b.iterrows():
            base[(int(r["station_id"]), int(r["month"]))] = {
                m: (None if pd.isna(r[m]) else float(r[m])) for m in PHYS_RANGE}

    # 4.5) 连续缺测段长度：识别「整段停更」与「孤立坏值」。
    #      本数据集里不少站点在某个年份中途永久停更（例如 2020 年有 576 站只记录到 4/5 月），
    #      这类缺口用气候态填补得到的是气候平均，不是实况，必须让调用方看得见。
    gap_len = {}
    if bad:
        d0 = datetime.date(int(d1[:4]), int(d1[5:7]), int(d1[8:10]))
        d9 = datetime.date(int(d2[:4]), int(d2[5:7]), int(d2[8:10]))
        absent = []
        cur = d0
        while cur <= d9:
            ds = cur.isoformat()
            if (sid, ds) not in vals:          # 该日压根没有记录行
                absent.append(ds)
            cur += datetime.timedelta(days=1)
        absent_set = set(absent)
        for ds in absent:                      # 洪水填充每个缺测段的首日，整段赋值
            if gap_len.get(ds) is not None:
                continue
            lo = ds
            while (datetime.date(int(lo[:4]), int(lo[5:7]), int(lo[8:10]))
                   - datetime.timedelta(days=1)).isoformat() in absent_set:
                lo = (datetime.date(int(lo[:4]), int(lo[5:7]), int(lo[8:10]))
                      - datetime.timedelta(days=1)).isoformat()
            hi = ds
            while (datetime.date(int(hi[:4]), int(hi[5:7]), int(hi[8:10]))
                   + datetime.timedelta(days=1)).isoformat() in absent_set:
                hi = (datetime.date(int(hi[:4]), int(hi[5:7]), int(hi[8:10]))
                      + datetime.timedelta(days=1)).isoformat()
            n = (datetime.date(int(hi[:4]), int(hi[5:7]), int(hi[8:10]))
                 - datetime.date(int(lo[:4]), int(lo[5:7]), int(lo[8:10]))).days + 1
            x = lo
            for _ in range(n):
                gap_len[x] = n
                x = (datetime.date(int(x[:4]), int(x[5:7]), int(x[8:10]))
                     + datetime.timedelta(days=1)).isoformat()

    def _climate_fill(m, month, dstr):
        """最后兜底：本站该月气候平均值。长期停更段默认拒绝（除非显式允许）。"""
        bt = base.get((sid, month), {}).get(m)
        if bt is None:
            return None, "邻站、前后日、气候基准均无可用值"
        g = gap_len.get(dstr, 0)
        if not allow_climate_fill:
            return None, ("连续缺测 %d 天，未用气候态填充（allow_climate_fill=false）" % g
                          if g else "未用气候态填充（allow_climate_fill=false）")
        if g > int(max_gap_days):
            return None, ("连续缺测 %d 天（>%d 天判为停更段），未用气候态填充；"
                          "如需气候态请设 max_gap_days=%d" % (g, int(max_gap_days), g))
        return _clamp_round(bt, m), "邻站与前后日均无有效值，降级用本站该月气候基准"

    def resolve(m):
        """auto 模式下逐指标选最优法（若该方法所需数据缺失，逐步降级）。"""
        return BEST_METHOD.get(m, "clima") if method == "auto" else method

    # 4.6) 克里金所需的长序列（1991-2020 距平）与变差函数，按指标惰性计算并缓存。
    #      第四轮 hold-out 验证：距平克里金在气温/日照/风速上显著优于 clima（但仅好 5%），
    #      湿度不显著、降水显著更差（间断非高斯，加性插值不适用）→ 降水退回 clima。
    #      代价是要多查一次 30 年长序列，故 krig 不进 auto 默认，需显式指定。
    _krig_cache = {}

    def _krig_prep(m):
        """为指标 m 估计邻站间变差函数，返回 (nb_ids, D_sub, d0, par) 或 (None, 原因)。"""
        if m in _krig_cache:
            return _krig_cache[m]
        if m in KRIG_UNSUITED:
            _krig_cache[m] = (None, "%s 属间断非高斯变量，克里金实测更差，自动退回 clima" % m)
            return _krig_cache[m]
        ids_all = [sid] + [n["station_id"] for n in nmeta]
        if len(ids_all) < 3:
            _krig_cache[m] = (None, "邻站不足 2 个，无法估计变差函数")
            return _krig_cache[m]
        try:
            # 同 base：逐指标独立过滤，避免被 weather_hist_ok 的 AND 连坐丢样本
            lg = _df("""
                SELECT station_id, date_true AS date,
                       month(date_true) AS month, %s AS v
                FROM weather_hist
                WHERE station_id IN (%s)
                  AND year(date_true) BETWEEN 1991 AND 2020
                  AND %s BETWEEN %f AND %f
            """ % (m, ",".join(str(i) for i in ids_all), m, *PHYS_RANGE[m]))
        except Exception as e:                                # noqa: BLE001
            _krig_cache[m] = (None, "长序列查询失败：%s" % e)
            return _krig_cache[m]
        if lg.empty:
            _krig_cache[m] = (None, "邻站无 1991-2020 长序列，无法估计变差函数")
            return _krig_cache[m]
        lg["date"] = lg["date"].astype(str).str.slice(0, 10)
        # 月气候基准（用于去趋势）——直接用长序列自身均值，避免依赖外部 base
        cl = lg.groupby(["station_id", "month"])["v"].mean()
        lg["anom"] = lg["v"] - [cl.get((int(s), int(mo)), np.nan)
                                for s, mo in zip(lg["station_id"], lg["month"])]
        piv = lg.pivot_table(index="date", columns="station_id", values="anom", aggfunc="mean")
        keep = [i for i in ids_all[1:] if i in piv.columns]
        if len(keep) < 2:
            _krig_cache[m] = (None, "邻站长序列覆盖不足（有效站 <2）")
            return _krig_cache[m]
        Z = piv[keep].to_numpy(dtype=float)
        pos = {n["station_id"]: n for n in nmeta}
        n = len(keep)
        D = np.zeros((n, n))
        for a in range(n):
            for b in range(a + 1, n):
                pa, pb = pos[keep[a]], pos[keep[b]]
                d = _haversine_km(pa["latitude"], pa["longitude"],
                                  pb["latitude"], pb["longitude"])
                D[a, b] = D[b, a] = d
        d0 = np.array([_haversine_km(lat, lng, pos[i]["latitude"], pos[i]["longitude"])
                       for i in keep])
        vg = _exp_variogram(Z, D)
        if vg is None:
            _krig_cache[m] = (None, "变差函数样本不足，无法拟合")
            return _krig_cache[m]
        par = _fit_exponential(*vg)
        if par is None:
            _krig_cache[m] = (None, "变差函数拟合失败")
            return _krig_cache[m]
        _krig_cache[m] = (keep, D, d0, par)
        return _krig_cache[m]

    def krig_predict(m, dstr):
        """距平克里金：权重解自邻站间距矩阵，预测目标站当日距平，再加回本站月基准。"""
        prep = _krig_prep(m)
        if len(prep) != 4:
            return None, None, prep[1]
        keep, D, d0, par = prep
        month = int(dstr[5:7])
        bt = base.get((sid, month), {}).get(m)
        if bt is None:
            return None, None, "本站该月无气候基准，克里金无法加回"
        pos = {n["station_id"]: n for n in nmeta}
        idx, anoms = [], []
        for j, nid in enumerate(keep):
            nv = _ok_val(nid, dstr, m)
            bn = base.get((nid, month), {}).get(m)
            if nv is None or bn is None:
                continue
            idx.append(j)
            anoms.append(nv - bn)
        if len(idx) < 2:
            return None, None, "当日有效邻站不足 2 个，克里金退化"
        sub = np.ix_(idx, idx)
        w = _krig_weights(np.asarray(D)[sub], np.asarray(d0)[idx], par)
        pred = bt + float(np.dot(w, np.asarray(anoms)))
        contribs = []
        for kk, j in enumerate(idx):
            nid = keep[j]
            contribs.append({"station_id": nid, "station_name": pos[nid]["station_name"],
                             "distance_km": round(float(np.asarray(d0)[j]), 3),
                             "value": _ok_val(nid, dstr, m),
                             "adjusted": round(float(anoms[kk]) + float(bt), 3),
                             "weight": round(float(w[kk]), 6)})
        return _clamp_round(pred, m), contribs, None

    def _shift(dstr, delta):
        y, mo, dd = int(dstr[:4]), int(dstr[5:7]), int(dstr[8:10])
        return (datetime.date(y, mo, dd) + datetime.timedelta(days=delta)).isoformat()

    def _ok_val(st, dstr, m):
        """取某站某日某指标的有效值（不在物理范围内视为无效）。"""
        v = vals.get((st, dstr), {}).get(m)
        if v is None or v < PHYS_RANGE[m][0] or v > PHYS_RANGE[m][1]:
            return None
        return v

    def temporal_anom(m, dstr):
        """时间维：本站前后日的距平平均 + 本站该月基准（前后日都可缺，有一即可）。"""
        bt = base.get((sid, int(dstr[5:7])), {}).get(m)
        if bt is None:
            return None
        anoms = []
        for delta in (-1, 1):
            ds = _shift(dstr, delta)
            v = _ok_val(sid, ds, m)
            if v is None:
                continue
            b2 = base.get((sid, int(ds[5:7])), {}).get(m)
            if b2 is None:
                continue
            if m == "precipitation":
                if b2 > 0.1:
                    anoms.append(v * min(max(bt / b2, 0.3), 3.0))
            else:
                anoms.append(v - b2)
        if not anoms:
            return None
        if m == "precipitation":
            return sum(anoms) / len(anoms)
        return bt + sum(anoms) / len(anoms)

    # 4.7) 跨要素门控回归（own6）所需的岭回归模型，按指标惰性训练并缓存。
    #      依据（第十三轮及之前十一/十二轮完整验证链，见「异常值修复方法依据说明.md」）：
    #        · 真实库里 95.09%(气温)~99.72%(日照) 的坏格，同行其它要素仍然是好的
    #        · 用「目标站自己其它 6 要素距平 + 邻站 7 要素距平」做岭回归，
    #          单指标坏时气温 MAE 0.8294→0.3764（-54.6%）、湿度 -27.0%、最低温 -41.9%
    #        · 但整行全坏时它会崩到比 clima 差一倍（+85%）→ 必须门控：
    #          目标站该日其它要素有效个数 < OWN6_GATE 时直接退回 clima
    #        · 门控后：单指标坏保留全部红利，整行坏/整段坏时精确等于 clima（+0.0%，零劣化）
    #      代价是要查邻站 1991-2005 训练期长序列 + 解一次岭回归，故不进 auto 默认。
    _own6_cache = {}
    _own6_base = {}
    _OWN6_TRAIN_FROM, _OWN6_TRAIN_TO = 1991, 2005
    _MET_IDX = {mm: i for i, mm in enumerate(PHYS_RANGE)}
    _NAN7 = np.full(len(PHYS_RANGE), np.nan)

    def _own6_shared():
        """
        加载并预处理训练期长序列中与「待修指标」无关的部分，只做一次：
          tr      逐行原始（已按 PHYS_RANGE 清洗、降水转 log1p、带 month）
          cl      groupby(station_id, month) 的月气候基准
          nb_cols {指标: DataFrame(index=date, columns=邻站)} 邻站距平
          nb_ids  邻站顺序
        返回 True/False（False 时 _own6_base["why"] 是原因）。
        """
        if _own6_base:
            return _own6_base.get("ok", False)
        ids_all = [sid] + [n["station_id"] for n in nmeta]
        if len(ids_all) < 2:
            _own6_base.update(ok=False, why="无邻站，跨要素模型无法构造邻站特征")
            return False
        try:
            tr = _df("""
                SELECT station_id, date_true AS date,
                       avg_temperature, max_temperature, min_temperature, avg_humidity,
                       precipitation, avg_wind_speed, sunshine_hours
                FROM weather_hist
                WHERE station_id IN (%s)
                  AND year(date_true) BETWEEN %d AND %d
            """ % (",".join(str(i) for i in ids_all), _OWN6_TRAIN_FROM, _OWN6_TRAIN_TO))
        except Exception as e:                                # noqa: BLE001
            _own6_base.update(ok=False, why="训练期长序列查询失败：%s" % e)
            return False
        if tr.empty:
            _own6_base.update(ok=False, why="邻站无 1991-2005 训练期数据")
            return False
        tr["date"] = tr["date"].astype(str).str.slice(0, 10)
        tr["month"] = tr["date"].str.slice(5, 7).astype(int)
        # 距平化：减各站各指标各自的月气候基准（降水走 log1p 空间，与验证脚本一致）
        for mm in PHYS_RANGE:
            lo, hi = PHYS_RANGE[mm]
            v = tr[mm].astype(float)
            tr[mm] = v.where((v >= lo) & (v <= hi), np.nan)
            if mm == "precipitation":
                tr[mm] = np.log1p(tr[mm])
        cl = tr.groupby(["station_id", "month"])[list(PHYS_RANGE)].mean()
        # 气候基准转成纯 dict 查表：原样保留 DataFrame 的话，cl[mm].get((站,月)) 走的是
        # DataFrame.get（列查找），38 万次调用要 30s；换成 dict 后是毫秒级。
        met_idx, nan7 = _MET_IDX, _NAN7
        clv = {}
        for (s_id, mo), row in cl.iterrows():
            clv[(int(s_id), int(mo))] = row.to_numpy(dtype=float)
        nb_ids = [n["station_id"] for n in nmeta]
        # 邻站同时刻各要素距平：按 (date × 邻站) 矩阵整表向量化对齐，避免逐行 .loc
        nb_cols = {}
        for mm in PHYS_RANGE:
            p = tr.pivot_table(index="date", columns="station_id", values=mm, aggfunc="mean")
            p = p.reindex(columns=nb_ids)
            mo_of_day = p.index.str.slice(5, 7).astype(int).to_numpy()
            bl = np.empty(p.shape, dtype=float)
            mi = met_idx[mm]
            for j, nid in enumerate(nb_ids):
                row = np.array([clv.get((nid, mo), nan7)[mi] for mo in range(1, 13)])
                bl[:, j] = row[mo_of_day - 1]
            nb_cols[mm] = pd.DataFrame(p.to_numpy(dtype=float) - bl,
                                       index=p.index, columns=nb_ids)
        _own6_base.update(ok=True, tr=tr, cl=clv, nb_cols=nb_cols, nb_ids=nb_ids)
        return True

    def _own6_prep(m):
        """为指标 m 训练跨要素岭回归，返回 (w, mu, sd, cl, nb_cols, nb_ids) 或 (None, 原因)。"""
        if m in _own6_cache:
            return _own6_cache[m]
        if not _own6_shared():
            _own6_cache[m] = (None, _own6_base.get("why", "训练数据不可用"))
            return _own6_cache[m]
        tr, cl = _own6_base["tr"], _own6_base["cl"]
        nb_cols, nb_ids = _own6_base["nb_cols"], _own6_base["nb_ids"]
        # 训练期样本：只取目标站该指标有效的日子
        tw = tr[tr["station_id"] == sid].dropna(subset=[m])
        if len(tw) < 300:
            _own6_cache[m] = (None, "目标站训练期该指标有效样本不足 300 天")
            return _own6_cache[m]
        feat_ms = [mm for mm in PHYS_RANGE if mm != m]
        met_idx, nan7 = _MET_IDX, _NAN7
        # 训练行：目标站该指标有效日，自身 6 要素距平（直接取自 tw，已过滤/去趋势）
        mo_tr = tw["month"].to_numpy(dtype=int)
        own_parts = []
        for mm in feat_ms:
            v = tw[mm].to_numpy(dtype=float)
            mi = met_idx[mm]
            ph = np.array([cl.get((sid, int(mo)), nan7)[mi] for mo in range(1, 13)])
            b = ph[mo_tr - 1]
            a = v - b
            a[~np.isfinite(a)] = 0.0
            own_parts.append(a.reshape(-1, 1))
        nb_parts = []
        for mm in feat_ms:
            sub = nb_cols[mm].reindex(tw["date"].to_numpy()).to_numpy(dtype=float)
            sub = np.where(np.isfinite(sub), sub, 0.0)
            nb_parts.append(sub)
        X = np.hstack(own_parts + nb_parts + [
            np.full((len(tw), 1), float(lat) / 50.0),
            np.full((len(tw), 1), float(lng) / 100.0),
            np.sin(2 * np.pi * mo_tr / 12.0).reshape(-1, 1),
            np.cos(2 * np.pi * mo_tr / 12.0).reshape(-1, 1)])
        mi = met_idx[m]
        y = tw[m].to_numpy(dtype=float) - np.array(
            [cl.get((sid, int(mo)), nan7)[mi] for mo in range(1, 13)])[mo_tr - 1]
        good = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
        X, y = X[good], y[good]
        if len(y) < 300:
            _own6_cache[m] = (None, "可用训练样本不足 300 天")
            return _own6_cache[m]
        mu, sd = X.mean(0), X.std(0) + 1e-6
        Xs_ = (X - mu) / sd
        p_ = X.shape[1]
        Xa = np.hstack([np.ones((len(y), 1)), Xs_])
        P = np.eye(p_ + 1) * OWN6_ALPHA
        P[0, 0] = 0.0
        try:
            w = np.linalg.solve(Xa.T @ Xa + P, Xa.T @ y)
        except np.linalg.LinAlgError:
            w = np.linalg.lstsq(Xa.T @ Xa + P, Xa.T @ y, rcond=None)[0]
        _own6_cache[m] = (w, mu, sd, cl, nb_cols, nb_ids)
        return _own6_cache[m]

    def own6_predict(m, dstr, bad_set=None):
        """门控跨要素回归：其它要素足够多才启用，否则返回 None（由调用方退回 clima）。

        bad_set = 该日已被判为「有错、待修」的指标集合。这些列虽然可能落在
        PHYS_RANGE 宽边界内（例如 avg_temperature=78℃），但**不能当正常观测喂进特征**
        —— 否则模型会把错值的影响传出去。实测踩过：新乡 2021-07-27 修最高气温时，
        avg=78℃ 被当成有效特征，预测出 57.7℃ 这种不可能的高温；排除后恢复正常。
        """
        bad_set = bad_set or set()
        month = int(dstr[5:7])
        if m in OWN6_UNSUITED:
            return None, None, ("%s 属间断非高斯变量，跨要素岭回归实测更差"
                                "（第十三轮层0 +3818.9%%/层6 +4.1%%），自动退回 clima" % m)
        cur = vals.get((sid, dstr))
        if cur is None:
            return None, None, "目标站当日无记录行，跨要素特征不可得"
        n_other = 0
        for mm in PHYS_RANGE:
            if mm == m or mm in bad_set:
                continue
            v = cur.get(mm)
            if v is not None and PHYS_RANGE[mm][0] <= v <= PHYS_RANGE[mm][1]:
                n_other += 1
        gate = OWN6_GATE.get(m, 6)
        if n_other < gate:
            return None, None, ("目标站当日其它要素仅 %d 个有效（<门控 %d），"
                                "跨要素特征不足会显著劣化，退回 clima" % (n_other, gate))
        prep = _own6_prep(m)
        if len(prep) != 6:
            return None, None, prep[1]
        w, mu, sd, cl, nb_cols, nb_ids = prep
        met_idx = _MET_IDX
        # 训练期 y = 目标指标 − 该站该月气候基准（降水两者都在 log1p 空间），
        # 故预测也必须加回同一个 cl 基准，而不是物理空间的 base。
        row_t = cl.get((sid, month))
        mi = met_idx[m]
        bt = float(row_t[mi]) if row_t is not None else np.nan
        if pd.isna(bt):
            return None, None, "本站该月无气候基准"
        f = []
        for mm in PHYS_RANGE:
            if mm == m:
                continue
            v = cur.get(mm)
            # bad_set 里的列一律当「缺」处理（与训练期缺失时的 0.0 距平同处理）
            if mm in bad_set or v is None or not (PHYS_RANGE[mm][0] <= v <= PHYS_RANGE[mm][1]):
                f.append(0.0)
                continue
            if mm == "precipitation":                   # 与训练期一致：log1p 空间
                v = math.log1p(max(v, 0.0))
            b = float(row_t[met_idx[mm]]) if row_t is not None else np.nan
            f.append(float(v - b) if not pd.isna(b) else 0.0)
        for mm in PHYS_RANGE:                           # 与训练期同序：按要素、按邻站
            if mm == m:
                continue
            # ⚠ 历史 bug（2026-11-26 发现并修复）：原先查 _own6_shared() 构造的 nb_cols[mm]，
            # 那张表的索引只有**训练期 1991-2005** 的日期；修 2016 年数据时 dstr 不在索引里，
            # 邻站特征整段被填成 0.0 → own6 退化成「只用本站其它要素 + 常数坐标」。
            # 实测新乡 40 个遮蔽点气温 MAE 1.75℃（比 clima 的 0.78℃ 差一倍多）。
            # 这里改为按预测日现算：邻站当日值 − 该站该月气候基准（用训练期同一套 cl，
            # 保证与训练特征同尺度），训练期外也能拿到真实邻站信号。
            mi_nb = met_idx[mm]
            for nid in nb_ids:
                nv = vals.get((nid, dstr), {}).get(mm)
                if nv is None or not (PHYS_RANGE[mm][0] <= nv <= PHYS_RANGE[mm][1]):
                    f.append(0.0)
                    continue
                if mm == "precipitation":                # 与训练期一致：log1p 空间
                    nv = math.log1p(max(nv, 0.0))
                row_c = cl.get((int(nid), month))
                b_nb = float(row_c[mi_nb]) if row_c is not None else np.nan
                f.append(float(nv - b_nb) if np.isfinite(b_nb) else 0.0)
        f.extend([float(lat) / 50.0, float(lng) / 100.0,
                  math.sin(2 * math.pi * month / 12.0), math.cos(2 * math.pi * month / 12.0)])
        f = np.asarray(f, dtype=float)
        if not np.all(np.isfinite(f)):
            return None, None, "跨要素特征含非有限值"
        pred = float(np.dot(w[1:], (f - mu) / sd) + w[0]) + bt
        if m == "precipitation":
            pred = math.expm1(min(max(pred, -10.0), 12.0))
        return _clamp_round(pred, m), n_other, None

    # 5) 逐坏日逐指标修复

    out_rows, warnings = [], []
    rep_by_date = {}
    kwhy_note = None
    for dstr, hit_ms, orig, probs in bad:
        month = int(dstr[5:7])
        for m in hit_ms:
            tv = orig.get(m)
            meth = resolve(m)
            kwhy_note = None
            kwhy_from = None        # 降级来源方法名，仅用于把原因写进 note
            contrib = []
            for n in nmeta:
                nv = _ok_val(n["station_id"], dstr, m)
                if nv is None:
                    continue
                adj = nv
                if meth in ("clima", "blend"):
                    bt = base.get((sid, month), {}).get(m)
                    bn = base.get((n["station_id"], month), {}).get(m)
                    if m == "precipitation":
                        if bt is not None and bn is not None and bn > 0.1:
                            adj = nv * min(max(bt / bn, 0.3), 3.0)
                    elif bt is not None and bn is not None:
                        adj = nv + (bt - bn)
                elif meth == "alt_idw" and m in ("avg_temperature", "max_temperature",
                                                 "min_temperature") \
                        and alt_t is not None and n["altitude"] is not None:
                    # 气温直减率订正：邻站海拔 h_n → 目标站海拔 h_t 时，
                    # 每升高 1000m 降温 6.5℃，故 T_h_t = T_h_n - L·(h_t - h_n)
                    adj = nv - LAPSE_RATE * (alt_t - n["altitude"])
                wgt = 1.0 if meth == "nearest" else 1.0 / (max(n["distance_km"], 0.1) ** 2)
                contrib.append({"station_id": n["station_id"], "station_name": n["station_name"],
                                "distance_km": n["distance_km"], "value": nv,
                                "adjusted": round(adj, 3), "weight": round(wgt, 6)})
                if meth == "nearest":
                    break
            # problem 文案：优先用「指标:问题」原样，否则（自洽性/日较差/日照上限这类
            # 不带指标前缀的判据）取第一条相关判据的中文短语
            if "row_missing" in probs:
                prob = "row_missing"
            else:
                _p = next((p.split(":", 1)[1] for p in probs if p.startswith(m + ":")), None)
                if _p is None:
                    _p = _first_plain_problem(probs, m)
                prob = m + ":" + _p

            # --- 出值 ---
            if meth == "own6":
                # 把「这一行里已知有错的其它指标」也排除掉，否则错值会污染跨要素特征
                _badset = set(hit_ms) - {m}
                ov, n_other, owhy = own6_predict(m, dstr, _badset)
                if ov is not None:
                    # 跨要素回归用到了目标站自身的其它要素 + 全部邻站的 7 要素，
                    # contributors 只作可读性展示（模型权重不可直接换算成单站贡献）。
                    ocontrib = []
                    for n in nmeta:
                        nv = _ok_val(n["station_id"], dstr, m)
                        if nv is None:
                            continue
                        ocontrib.append({"station_id": n["station_id"],
                                         "station_name": n["station_name"],
                                         "distance_km": n["distance_km"], "value": nv,
                                         "adjusted": nv, "weight": None})
                    out_rows.append({"date": dstr, "metric": m, "original": tv, "problem": prob,
                                     "repaired": ov, "method": "own6",
                                     "contributors": ocontrib,
                                     "quality": "observed",
                                     "note": "跨要素岭回归（本站其它 %d 个要素距平 + %d 邻站×7 要素距平"
                                             " + 经纬度/月相位，训练期 1991-2005）"
                                             % (n_other, len(nmeta))})
                    rep_by_date.setdefault(dstr, {})[m] = ov
                    continue
                # 门控未通过（整行坏）或样本不足 → 退回 clima
                meth = "clima"
                kwhy_note, kwhy_from = owhy, "own6"
                contrib = []
                for n in nmeta:
                    nv = _ok_val(n["station_id"], dstr, m)
                    if nv is None:
                        continue
                    adj = nv
                    bt = base.get((sid, month), {}).get(m)
                    bn = base.get((n["station_id"], month), {}).get(m)
                    if m == "precipitation":
                        if bt is not None and bn is not None and bn > 0.1:
                            adj = nv * min(max(bt / bn, 0.3), 3.0)
                    elif bt is not None and bn is not None:
                        adj = nv + (bt - bn)
                    contrib.append({"station_id": n["station_id"],
                                    "station_name": n["station_name"],
                                    "distance_km": n["distance_km"], "value": nv,
                                    "adjusted": round(adj, 3),
                                    "weight": round(1.0 / (max(n["distance_km"], 0.1) ** 2), 6)})
            if meth == "krig":
                kv, kcontrib, kwhy = krig_predict(m, dstr)
                if kv is not None:
                    out_rows.append({"date": dstr, "metric": m, "original": tv, "problem": prob,
                                     "repaired": kv, "method": meth,
                                     "contributors": kcontrib,
                                     "quality": "observed",
                                     "note": "距平克里金（普通克里金权重，变差函数由邻站长序列拟合）"})
                    rep_by_date.setdefault(dstr, {})[m] = kv
                    continue
                # 克里金不可用 → 退回 clima（降水本来就走这条）
                meth = "clima"
                kwhy_note, kwhy_from = kwhy, "krig"
                contrib = []
                for n in nmeta:
                    nv = _ok_val(n["station_id"], dstr, m)
                    if nv is None:
                        continue
                    adj = nv
                    bt = base.get((sid, month), {}).get(m)
                    bn = base.get((n["station_id"], month), {}).get(m)
                    if m == "precipitation":
                        if bt is not None and bn is not None and bn > 0.1:
                            adj = nv * min(max(bt / bn, 0.3), 3.0)
                    elif bt is not None and bn is not None:
                        adj = nv + (bt - bn)
                    contrib.append({"station_id": n["station_id"],
                                    "station_name": n["station_name"],
                                    "distance_km": n["distance_km"], "value": nv,
                                    "adjusted": round(adj, 3),
                                    "weight": round(1.0 / (max(n["distance_km"], 0.1) ** 2), 6)})
            if meth == "nearest":
                if not contrib:
                    out_rows.append({"date": dstr, "metric": m, "original": tv,
                                     "repaired": None, "method": meth, "contributors": [],
                                     "note": "邻站当日无有效值"})
                    continue
                repaired = _clamp_round(contrib[0]["value"], m)
                note = None
            else:
                spatial = None
                if contrib:
                    tw = sum(c["weight"] for c in contrib)
                    spatial = sum((c["adjusted"] if c["adjusted"] is not None else c["value"])
                                  * c["weight"] for c in contrib) / tw
                if meth == "blend":
                    tval = temporal_anom(m, dstr)
                    if spatial is not None and tval is not None:
                        repaired = _clamp_round(0.5 * spatial + 0.5 * tval, m)
                        note = "0.5×空间(clima) + 0.5×时间(前后日距平)"
                    elif spatial is not None:
                        repaired = _clamp_round(spatial, m)
                        note = "时间维缺失（前后日无有效值），退化为 clima"
                    elif tval is not None:
                        repaired = _clamp_round(tval, m)
                        note = "邻站全缺，仅用时间维（前后日距平）"
                    else:
                        # 区域性长期缺测（本站与邻站整段停更）→ 最后兜底本站该月气候基准
                        repaired, note = _climate_fill(m, month, dstr)
                else:
                    if spatial is not None:
                        repaired, note = _clamp_round(spatial, m), None
                    else:
                        # 空间法失效（邻站当日全为哨兵/缺测，常见于区域性缺测日）
                        # 依次降级：① 本站前后日距平（时间连续性）② 本站该月气候基准
                        tval = temporal_anom(m, dstr)
                        if tval is not None:
                            repaired = _clamp_round(tval, m)
                            note = "邻站当日无有效值，降级用时间维（本站前后日距平）"
                        else:
                            repaired, note = _climate_fill(m, month, dstr)
            rec = {"date": dstr, "metric": m, "original": tv, "problem": prob,
                   "repaired": repaired, "method": meth, "contributors": contrib,
                   # quality 标注本次修复的可信度，供上层决定是否采纳：
                   #   observed = 邻近站有同日实测（最可信）
                   #   temporal = 仅靠本站前后日连续性（可信）
                   #   climate  = 只能用月气候平均值（等同气候态，不是实况，慎用）
                   "quality": ("none" if repaired is None else
                               "observed" if contrib else
                               "temporal" if note and "时间维" in note else
                               "climate")}
            if kwhy_note:
                label = "跨要素回归" if kwhy_from == "own6" else "克里金"
                note = ((note + "；") if note else "") + label + "不可用：" + kwhy_note
                kwhy_note = None
                kwhy_from = None
            if note:
                rec["note"] = note
            out_rows.append(rec)
            if repaired is not None:
                rep_by_date.setdefault(dstr, {})[m] = repaired
    for dstr, mm in sorted(rep_by_date.items()):
        if "max_temperature" in mm and "min_temperature" in mm \
                and mm["max_temperature"] < mm["min_temperature"]:
            warnings.append("%s 修复后 max(%s) < min(%s)，邻站气候差异过大，建议人工复核"
                            % (dstr, mm["max_temperature"], mm["min_temperature"]))
    n_repaired = sum(1 for r in out_rows if r["repaired"] is not None)
    n_climate = sum(1 for r in out_rows if r.get("quality") == "climate")
    if n_climate:
        warnings.append("其中 %d 个值只能由「本站该月气候平均值」给出（quality=climate），"
                        "相当于气候态而非实况，长期缺测段请勿直接当观测使用" % n_climate)
    if not nmeta:
        warnings.append("目标站 %.0fkm 内没有其他可用站点（radius_km 太小或周边无站），"
                        "只能退回本站时间维/气候基准，请谨慎采信" % float(radius_km))
    elif n_repaired == 0:
        warnings.append("%d 个邻站当日与前后日均无有效观测，本次未修出任何值" % len(nmeta))
    # 整站停更提示：区间内后半段完全没有记录时，停更段保护会拒绝用气候态填充，
    # 用户会看到「大量值修不出来」却不知道为什么 —— 必须明确告诉他数据止于哪天。
    if bad:
        gaps = [b[0] for b in bad]
        last_bad = max(gaps)
        try:
            _cov = _df("""
                SELECT max(t.ts_true)::VARCHAR AS last_ts
                FROM weather_hist t WHERE t.station_id = %d
                  AND t.date_true BETWEEN DATE '%s' AND DATE '%s'
            """ % (sid, d1, d2))
            last_ts = _cov.iloc[0]["last_ts"] if not _cov.empty else None
        except Exception:
            last_ts = None
        if last_ts:
            last_day = str(last_ts)[:10]
            if last_day < last_bad:
                warnings.append(
                    "该站在区间内最后一条记录是 %s，之后到 %s 完全停更（%d 天无任何记录）；"
                    "停更段默认不填气候态，所以这部分值会显示为无法修复。"
                    "如需强制填充，把 max_gap_days 调大（但填出来的是气候态不是实况）。"
                    % (last_day, last_bad, len([d for d in gaps if d > last_day])))
    return {
        "station": {"station_id": sid, "station_code": info.get("station_code"),
                    "station_name": info.get("station_name"), "province": info.get("province"),
                    "latitude": float(lat), "longitude": float(lng), "altitude": alt_t},
        "period": {"from": d1, "to": d2},
        "method": method, "k": int(k), "radius_km": float(radius_km),
        "metrics": ms,
        "neighbors": nmeta,
        "summary": {"bad_rows": len(bad), "repaired_values": n_repaired,
                    "no_neighbor_data": len(out_rows) - n_repaired,
                    "by_quality": {
                        "observed": sum(1 for r in out_rows if r.get("quality") == "observed"),
                        "temporal": sum(1 for r in out_rows if r.get("quality") == "temporal"),
                        "climate": n_climate,
                    },
                    "warnings": warnings},
        "rows": out_rows,
    }


# ================= 批量补齐 / 自动巡检 =================
#
# 使用场景：每年（或每批）新录入数据后，希望「自动把新数据里异常/空白的数据补齐」。
#
# 两层接口：
#   1) repair_batch()      —— 多站、多区间批量补齐。适合「一批新数据刚进来，全跑一遍」。
#   2) repair_new_batch()  —— 增量自动补齐。按 file_path 里的上传批次号（前 14 位时间戳）
#                             识别「新录入批次」，只对受影响站点×区间跑，并输出变更清单。
#
# 两者都不写回数据源（当前实现只读 parquet，补齐结果以 JSON/CSV 报告形式落地），
# 是否采纳、如何回写由调用方决定 —— 这是刻意的：修复值带 quality 可信度，
# 停更段的气候态不该被静默当成观测。

def _batch_stations(province=None, station_ids=None, limit=None):
    """挑出参与批量的站点。station_ids 优先；否则按省筛选（全库）。"""
    if station_ids:
        ids = [int(s) for s in station_ids]
        ph = ",".join(str(i) for i in ids)
        df = _df("SELECT station_id FROM stations WHERE station_id IN (%s) ORDER BY station_id" % ph)
        return [int(v) for v in df["station_id"].tolist()]
    if province:
        p = str(province).replace("'", "''")
        df = _df("SELECT station_id FROM stations WHERE province_norm = '%s' ORDER BY station_id" % p)
    else:
        df = _df("SELECT station_id FROM stations ORDER BY station_id")
    ids = [int(v) for v in df["station_id"].tolist()]
    return ids[:int(limit)] if limit else ids


def repair_batch(station_ids=None, province=None, start=None, end=None, year=None,
                 metrics=None, method="auto", k=12, radius_km=300,
                 allow_climate_fill=True, max_gap_days=7, detail_limit=100000,
                 limit=None, max_workers=4, progress=False):
    """批量补齐：对一批站点 × 一个区间逐个跑 repair_daily，汇总成一张总表。

    返回 {period, method, station_count, summary{...合计...}, stations[{station,summary}], rows[]}
    —— rows 是「所有站的修复明细」合并后的列表（每行带 station_id，便于直接导 CSV/回写）。
    失败站点不中断整个批次，记入 failed[] 并说明原因。

    注意 detail_limit 默认放到 10 万：单站跨 60 年可能产生上万条坏值明细，
    用小值会在扫描阶段就截断，导致 repaired_values 明显少于应有的指标数。
    """
    ids = _batch_stations(province=province, station_ids=station_ids, limit=limit)
    if not ids:
        raise ValueError("没有匹配到任何站点")
    ms = list(metrics) if metrics else list(PHYS_RANGE.keys())
    for m in ms:
        if not metric_ok(m):
            raise ValueError("metric 必须是 %s" % ", ".join(METRICS))
    t0 = time.time()
    per_station, rows, failed = [], [], []
    agg = {"bad_rows": 0, "repaired_values": 0, "no_neighbor_data": 0,
           "by_quality": {"observed": 0, "temporal": 0, "climate": 0}, "warnings": 0}
    for n, sid in enumerate(ids, 1):
        if progress:
            print("[%d/%d] station %d ..." % (n, len(ids), sid), flush=True)
        try:
            r = repair_daily(sid, start=start, end=end, year=year, metrics=ms,
                             method=method, k=k, radius_km=radius_km,
                             detail_limit=int(detail_limit),
                             allow_climate_fill=allow_climate_fill,
                             max_gap_days=max_gap_days)
        except Exception as e:
            failed.append({"station_id": sid, "error": "%s: %s" % (type(e).__name__, e)})
            continue
        s = r["summary"]
        per_station.append({
            "station": r["station"], "period": r["period"], "summary": s,
            "neighbors": len(r["neighbors"]),
        })
        agg["bad_rows"] += s["bad_rows"]
        agg["repaired_values"] += s["repaired_values"]
        agg["no_neighbor_data"] += s["no_neighbor_data"]
        for kk in agg["by_quality"]:
            agg["by_quality"][kk] += s["by_quality"].get(kk, 0)
        agg["warnings"] += len(s.get("warnings") or [])
        for row in r["rows"]:
            row = dict(row)
            row["station_id"] = sid
            row["station_name"] = r["station"].get("station_name")
            row["province"] = r["station"].get("province")
            rows.append(row)
    return {
        "period": {"from": per_station[0]["period"]["from"] if per_station else None,
                   "to": per_station[0]["period"]["to"] if per_station else None},
        "method": method, "k": int(k), "radius_km": float(radius_km),
        "metrics": ms,
        "station_count": len(per_station),
        "requested_count": len(ids),
        "elapsed_sec": round(time.time() - t0, 1),
        "summary": agg,
        "stations": per_station,
        "failed": failed,
        "rows": rows,
    }


def list_ingest_batches(limit=200):
    """列出数据里出现过的「上传批次」。批次号取自 file_path 里的 14 位时间戳
    （形如 gg/weather-station/multi/20261106160739_1_1793952459.xlsx → 20261106160739），
    这是平台写入时的时间，天然可以当作「录入批次」标识。

    返回 [{batch, stations, rows, first_day, last_day, station_ids[]}]，按批次时间倒序。"""
    glob = TD_GLOB
    df = _df("""
        SELECT regexp_extract(file_path, '(\\d{14})', 1) AS batch,
               count(DISTINCT station_id) AS stations,
               count(*) AS rows,
               min(date_true)::VARCHAR AS first_day,
               max(date_true)::VARCHAR AS last_day
        FROM weather
        WHERE file_path IS NOT NULL
        GROUP BY 1
        ORDER BY 1 DESC
        LIMIT %d
    """ % int(limit))
    out = []
    for _, r in df.iterrows():
        out.append({"batch": str(r["batch"]), "stations": int(r["stations"]),
                    "rows": int(r["rows"]), "first_day": r["first_day"],
                    "last_day": r["last_day"]})
    return out


def batch_station_ids(batch):
    """某批次覆盖了哪些站点，以及该批次实际写入的日期区间（用于只补齐新进来的那段）"""
    b = str(batch)
    if not re.fullmatch(r"\d{8,14}", b):
        raise ValueError("batch 必须是 file_path 里的 8~14 位数字时间戳，例如 20261106160739")
    df = _df("""
        SELECT station_id,
               count(*) AS rows,
               min(date_true)::VARCHAR AS first_day,
               max(date_true)::VARCHAR AS last_day
        FROM weather
        WHERE file_path LIKE '%%%s%%'
        GROUP BY 1 ORDER BY 1
    """ % b)
    return [{"station_id": int(r["station_id"]), "rows": int(r["rows"]),
             "first_day": r["first_day"], "last_day": r["last_day"]}
            for _, r in df.iterrows()]


def repair_new_batch(batch=None, auto_latest=True, lookback_days=0,
                     metrics=None, method="auto", k=12, radius_km=300,
                     allow_climate_fill=False, max_gap_days=7,
                     detail_limit=100000, write_csv=True, out_dir=None):
    """增量自动补齐：识别「新录入批次」→ 只对这批数据涉及的站点×日期区间跑补齐。

    参数：
      batch          指定批次号（file_path 里的 14 位时间戳）。为空且 auto_latest=True 时
                     自动取**最新的批次**（=最近一次录入）。
      lookback_days  在批次日期区间上向前多补几天（默认 0）。用于「录入时把月初几天
                     忘了填」这类情况，设 7 就能覆盖到上周的窟窿。
      allow_climate_fill 默认 **False**：自动补齐场景下绝不把气候态当观测写回。
      write_csv      把修复明细落成 CSV（UTF-8-SIG，Excel 可直接打开），路径见返回。

    返回 {batch, batch_info, period, stations[{station_id, summary}], summary, rows[], csv_file}
    设计为幂等：同一批次重复跑，结果一致（因为没有写回数据源）。
    """
    if not batch:
        if not auto_latest:
            raise ValueError("未指定 batch 且 auto_latest=False")
        bs = list_ingest_batches(limit=1)
        if not bs:
            raise ValueError("数据里找不到任何上传批次（file_path 无时间戳）")
        batch = bs[0]["batch"]
    covered = batch_station_ids(batch)
    if not covered:
        raise ValueError("批次 %s 没有覆盖任何站点" % batch)
    d1 = min(c["first_day"] for c in covered)
    d2 = max(c["last_day"] for c in covered)
    if lookback_days:
        d1 = (datetime.date.fromisoformat(d1) - datetime.timedelta(days=int(lookback_days))).isoformat()
    ids = [c["station_id"] for c in covered]
    res = repair_batch(station_ids=ids, start=d1, end=d2, metrics=metrics,
                       method=method, k=k, radius_km=radius_km,
                       allow_climate_fill=allow_climate_fill,
                       max_gap_days=max_gap_days,
                       detail_limit=int(detail_limit))
    out = {
        "batch": batch,
        "batch_info": {"stations_covered": len(covered), "rows_covered":
                       sum(c["rows"] for c in covered),
                       "data_from": min(c["first_day"] for c in covered),
                       "data_to": max(c["last_day"] for c in covered)},
        "period": {"from": d1, "to": d2},
        "lookback_days": int(lookback_days),
        "method": method, "k": int(k), "radius_km": float(radius_km),
        "allow_climate_fill": bool(allow_climate_fill),
        "summary": res["summary"],
        "stations": [{"station_id": s["station"]["station_id"],
                      "station_name": s["station"].get("station_name"),
                      "province": s["station"].get("province"),
                      "summary": s["summary"]} for s in res["stations"]],
        "failed": res["failed"],
        "rows": res["rows"],
        "elapsed_sec": res["elapsed_sec"],
    }
    if write_csv and out["rows"]:
        d = out_dir or os.path.join(os.path.dirname(HERE), "exports")
        os.makedirs(d, exist_ok=True)
        f = os.path.join(d, "补齐_%s.csv" % batch)
        pd.DataFrame(out["rows"]).to_csv(f, index=False, encoding="utf-8-sig")
        out["csv_file"] = f
    return out


def column_swap_scan(station_id=None, province=None, station_ids=None,
                     min_rows=10, fix_ratio=0.95):
    """列互换检测：找出「整列数据被写错列」的站点 —— 这类错误用插值修不了，必须整列还原。

    **为什么需要单独的检测**：数据里最隐蔽的一类错误不是「某个值异常」，而是「整列被换走」。
    此时每个值单独看都在合理范围内（1.1℃ 是合法气温、13.6m/s 是合法风速），任何基于
    取值范围的判据都抓不到。唯一可靠的线索是**跨列的内部矛盾**。

    判据（纯物理约束，不依赖任何历史统计）：
      物理上平均气温不可能低于当日最低气温（avg ≥ min）。若某站出现大量 avg < min，
      把 avg_temperature 与 avg_wind_speed 两列对调后这些矛盾消失，即判定为两列互换。

    2026-11-26 全库实测：876 站中**仅站点 1610（四川盐源，56565，海拔 2516.8m）**命中，
    自 2013-10-01 起 a/w 两列互换（此前 2013-09-30 为 a=13.8/w=1.2，次日变为 a=1.1/w=13.6）。
    该站 2013-10 之后 2,404 行全部受影响，其中 1,640 行 avg<min；
    对调后 1,640/1,640 = 100% 满足 min ≤ avg ≤ max。
    其余站点 avg<min 最多 2 行（个别真实录入错误），不会被误判。

    参数：
      min_rows   至少要有多少行 avg<min 才纳入判定（默认 10，远高于噪声水平）
      fix_ratio  对调后矛盾消除的最低比例（默认 0.95）

    返回 {stations: [...], summary}，每站含
    {station_id, station_name, province, swap_pair, rows_affected, contradict_rows,
     fixed_ratio, breakpoint, before_after}
    其中 breakpoint 是互换起始日（该日前后分别统计），before_after 给出互换前后的列均值，
    便于人工核对。**本函数只检测，不修改任何数据**。"""
    if station_id is not None:
        ids = [int(station_id)]
    else:
        ids = _batch_stations(province=province, station_ids=station_ids, limit=None)
    GLOB = TD_GLOB
    out, failed = [], []
    for sid in ids:
        try:
            info = station_detail(sid)
            if info is None:
                continue
            q = _df(f"""
                WITH t AS (
                    SELECT date_true,
                           avg_temperature AS a, max_temperature AS mx, min_temperature AS mn,
                           avg_wind_speed AS w
                    FROM weather_hist WHERE station_id = {sid}
                )
                SELECT count(*) AS n,
                       count(*) FILTER (WHERE a IS NOT NULL AND mn IS NOT NULL AND a < mn) AS bad,
                       count(*) FILTER (WHERE a IS NOT NULL AND mn IS NOT NULL AND a < mn
                                          AND w IS NOT NULL AND w >= mn AND w <= mx) AS bad_fixed_by_swap,
                       round(avg(a), 2) AS mean_a, round(avg(w), 2) AS mean_w,
                       round(min(a), 1) AS min_a, round(max(a), 1) AS max_a,
                       round(min(w), 1) AS min_w, round(max(w), 1) AS max_w,
                       sum(CASE WHEN a BETWEEN -60 AND 55 THEN 1 ELSE 0 END) AS a_in_temp,
                       sum(CASE WHEN w BETWEEN 0 AND 60 THEN 1 ELSE 0 END) AS w_in_wind
                FROM t
            """).iloc[0].to_dict()
            n_bad = int(q["bad"])
            if n_bad < int(min_rows):
                continue
            ratio = int(q["bad_fixed_by_swap"]) / float(n_bad)
            if ratio < float(fix_ratio):
                continue
            # 找分界日：列互换是**持续**错误，故取「最长连续年份段」的起始年。
            # 不能用第一个出现矛盾的年份（1610 站 1968-1970 另有 12 行零星真实录入错误，
            # 会误判成 1968 起始）；也不能用累计占比（2013-2020 各年数量接近，会选到 2019）。
            bd = _df(f"""
                SELECT strftime(date_true, '%Y') AS y, count(*) AS c
                FROM weather_hist
                WHERE station_id = {sid}
                  AND avg_temperature IS NOT NULL AND min_temperature IS NOT NULL
                  AND avg_temperature < min_temperature
                GROUP BY 1 ORDER BY 1
            """)
            breakpoint = None
            if not bd.empty:
                ys = [int(x) for x in bd["y"].tolist()]
                # 切分连续年份段，取覆盖矛盾行最多的那一段（并列时取更早的）
                runs, cur = [], [ys[0]]
                for y in ys[1:]:
                    if y == cur[-1] + 1:
                        cur.append(y)
                    else:
                        runs.append(cur)
                        cur = [y]
                runs.append(cur)
                cnt = {int(r["y"]): int(r["c"]) for _, r in bd.iterrows()}
                best = max(runs, key=lambda rr: (sum(cnt[y] for y in rr), -rr[0]))
                edge_year = best[0]
                bm = _df(f"""
                    SELECT strftime(date_true, '%Y-%m') AS ym, count(*) AS c
                    FROM weather_hist
                    WHERE station_id = {sid}
                      AND avg_temperature IS NOT NULL AND min_temperature IS NOT NULL
                      AND avg_temperature < min_temperature
                      AND year(date_true) = {int(edge_year)}
                    GROUP BY 1 ORDER BY 1
                """)
                if not bm.empty:
                    breakpoint = str(bm.iloc[0]["ym"]) + "-01"
            before_after = {}
            if breakpoint:
                for tag, op in (("before", "<"), ("after", ">=")):
                    r = _df(f"""SELECT round(avg(avg_temperature),2) AS mean_a,
                                       round(avg(avg_wind_speed),2) AS mean_w,
                                       round(min(avg_temperature),1) AS min_a,
                                       round(max(avg_temperature),1) AS max_a,
                                       count(*) AS n
                                FROM weather_hist
                                WHERE station_id = {sid} AND date_true {op} DATE '{breakpoint}'
                                  AND avg_temperature BETWEEN -100 AND 100
                                  AND avg_wind_speed BETWEEN 0 AND 200""").iloc[0].to_dict()
                    before_after[tag] = {k: (None if pd.isna(v) else v) for k, v in r.items()}
            out.append({
                "station_id": sid, "station_name": info.get("station_name"),
                "province": info.get("province"), "swap_pair": ["avg_temperature", "avg_wind_speed"],
                "rows_affected": n_bad, "contradict_rows": n_bad,
                "fixed_ratio": round(ratio, 4), "breakpoint": breakpoint,
                "before_after": before_after,
                "advice": ("该站 %s 起 avg_temperature 与 avg_wind_speed 两列被互换，"
                           "修复方式是把这两列的值对调回来（不是插值）。"
                           "受影响行数约 %d。" % (breakpoint or "某日", n_bad)),
            })
        except Exception as e:
            failed.append({"station_id": sid, "error": "%s: %s" % (type(e).__name__, e)})
    return {
        "scanned_stations": len(ids),
        "summary": {"found": len(out), "failed": len(failed),
                    "criteria": {"min_rows": int(min_rows), "fix_ratio": float(fix_ratio)}},
        "stations": out, "failed": failed,
    }


def repair_overview(year=None, start=None, end=None, province=None,
                    station_ids=None, limit=None, allow_climate_fill=False,
                    max_gap_days=7):
    """全库巡检：只扫描不修复，给出「哪些站、哪年、缺多少」的总账。

    用于「新数据录入后先看一眼质量」：返回按站汇总的 bad_rows / 各指标 invalid+missing，
    以及全库合计。不做插值，几十秒内可跑完几百站。"""
    ids = _batch_stations(province=province, station_ids=station_ids, limit=limit)
    t0 = time.time()
    per_station, failed = [], []
    tot = {"days": 0, "rows_present": 0, "inconsistent_maxmin": 0,
           "invalid": 0, "missing": 0}
    per_metric_tot = {m: {"have": 0, "valid": 0, "invalid": 0, "missing": 0} for m in PHYS_RANGE}
    for sid in ids:
        try:
            sc = repair_scan(sid, start=start, end=end, year=year, detail_limit=1)
        except Exception as e:
            failed.append({"station_id": sid, "error": "%s: %s" % (type(e).__name__, e)})
            continue
        s = sc["summary"]
        bad = s["invalid"] if "invalid" in s else 0
        pm = s["per_metric"]
        n_invalid = sum(v["invalid"] for v in pm.values())
        n_missing = sum(v["missing"] for v in pm.values())
        per_station.append({
            "station_id": sid, "days": s["days"], "rows_present": s["rows_present"],
            "inconsistent_maxmin": s["inconsistent_maxmin"],
            "invalid": n_invalid, "missing": n_missing,
            "bad_total": n_invalid + n_missing,
            "per_metric": pm,
        })
        tot["days"] += s["days"]
        tot["rows_present"] += s["rows_present"]
        tot["inconsistent_maxmin"] += s["inconsistent_maxmin"]
        tot["invalid"] += n_invalid
        tot["missing"] += n_missing
        for m, v in pm.items():
            for kk in ("have", "valid", "invalid", "missing"):
                per_metric_tot[m][kk] += v[kk]
    per_station.sort(key=lambda x: -x["bad_total"])
    return {
        "period": {"from": (start or ("%d-01-01" % int(year)) if (start or year) else None),
                   "to": (end or ("%d-12-31" % int(year)) if (end or year) else None)},
        "station_count": len(per_station), "requested_count": len(ids),
        "elapsed_sec": round(time.time() - t0, 1),
        "totals": tot, "per_metric": per_metric_tot,
        "worst_stations": per_station[:50],
        "stations": per_station,
        "failed": failed,
    }
