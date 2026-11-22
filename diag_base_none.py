# -*- coding: utf-8 -*-
"""查为什么降级到气候基准时仍有 488 个值是 None。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weather_api import data as D

sid = 1129
print("=== 1129 各月气候基准（1991-2020，含有效性）===")
df = D._df("""
    SELECT month(ts_true) AS m,
           count(*) AS n,
           count(avg_temperature) AS n_tavg, count(max_temperature) AS n_tmax,
           count(min_temperature) AS n_tmin, count(avg_humidity) AS n_rh,
           count(precipitation) AS n_prcp, count(avg_wind_speed) AS n_ws,
           count(sunshine_hours) AS n_sun
    FROM weather_hist_ok WHERE station_id = %d GROUP BY 1 ORDER BY 1
""" % sid)
print(df.to_string(index=False))

print()
print("=== weather_hist_ok 里 1129 各月非空计数（原始 weather_hist）===")
df2 = D._df("""
    SELECT month(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) AS m, count(*) AS n,
           count(avg_temperature) AS n1, count(sunshine_hours) AS n7
    FROM weather_hist WHERE station_id = %d GROUP BY 1 ORDER BY 1
""" % sid)
print(df2.to_string(index=False))

print()
print("=== 1129 每年数据天数（看 1991-2020 是否完整）===")
print(D._df("""
    SELECT year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) AS y, count(*) AS n
    FROM weather_hist WHERE station_id = %d
      AND year(CAST(ts_bj - INTERVAL 100 YEAR AS DATE)) BETWEEN 1990 AND 2020
    GROUP BY 1 ORDER BY 1
""" % sid).to_string(index=False))
