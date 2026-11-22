# -*- coding: utf-8 -*-
"""核查镜像尾部年份（2021-2025）的站点构成与省份分布。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weather_api import data as D

print("=== 2021-2025 有数据的站点按省份分布 ===")
print(D._df("""
    SELECT s.province_norm AS province, count(DISTINCT w.station_id) AS n_st
    FROM weather_hist w JOIN stations s USING(station_id)
    WHERE year(CAST(w.ts_bj - INTERVAL 100 YEAR AS DATE)) BETWEEN 2021 AND 2025
    GROUP BY 1 ORDER BY n_st DESC
""").to_string(index=False))

print()
print("=== 这些站的 station_id 范围 / 是否集中在某段 ===")
print(D._df("""
    SELECT min(station_id) AS id_min, max(station_id) AS id_max,
           count(DISTINCT station_id) AS n_st
    FROM weather_hist
    WHERE year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) BETWEEN 2021 AND 2025
""").to_string(index=False))

print()
print("=== 每站最后一年的最后一天（尾部 10 站样例）===")
print(D._df("""
    SELECT station_id, max(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) AS last_day,
           count(DISTINCT year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE))) AS n_years
    FROM weather_hist
    GROUP BY 1 ORDER BY last_day DESC LIMIT 10
""").to_string(index=False))

print()
print("=== 各站数据结束年份的分布 ===")
print(D._df("""
    WITH t AS (
      SELECT station_id, max(year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE))) AS last_y
      FROM weather_hist GROUP BY 1
    )
    SELECT last_y, count(*) AS n_st FROM t GROUP BY 1 ORDER BY 1
""").to_string(index=False))
