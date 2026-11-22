# -*- coding: utf-8 -*-
"""核查镜像里各年份的实际覆盖情况：哪些站、哪些时段真的有数据。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weather_api import data as D

print("=== 抽样站 2020 年数据范围 ===")
print(D._df("""
    SELECT station_id, count(*) AS n_days,
           min(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) AS d1,
           max(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) AS d2
    FROM weather_hist
    WHERE station_id IN (1124,1125,1126,1128,1129,1133)
      AND year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) = 2020
    GROUP BY 1 ORDER BY 1
""").to_string(index=False))

print()
print("=== 全库 2020 年逐月覆盖（有数据的站数）===")
print(D._df("""
    SELECT month(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) AS m,
           count(DISTINCT station_id) AS n_st
    FROM weather_hist
    WHERE year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) = 2020
    GROUP BY 1 ORDER BY 1
""").to_string(index=False))

print()
print("=== 全库逐年：站数 / 行数 ===")
print(D._df("""
    SELECT year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) AS y,
           count(DISTINCT station_id) AS n_st, count(*) AS n_rows
    FROM weather_hist
    WHERE year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) BETWEEN 2015 AND 2025
    GROUP BY 1 ORDER BY 1
""").to_string(index=False))
