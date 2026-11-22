# -*- coding: utf-8 -*-
"""剖析修复失败（no_neighbor_data）的成因：是邻居真的没数据，还是降级链没覆盖到。"""
import sys
import os
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weather_api import data as D

# 一个 2020 年只到 4 月、且周边站也大多停更的站
for sid, yr in [(1126, 2020), (1129, 2020), (1133, 2020)]:
    r = D.repair_daily(int(sid), year=int(yr))
    s = r["summary"]
    print("=" * 72)
    print("站点 %s (%s) %s 年" % (sid, r["station"]["station_name"], yr))
    print("  坏日 %d / 修复 %d / 无数据 %d" % (s["bad_rows"], s["repaired_values"],
                                               s["no_neighbor_data"]))
    notes = collections.Counter(x.get("note", "(正常修复)") for x in r["rows"])
    for k, v in notes.most_common():
        print("   %-40s %d" % (k, v))
    # 看无法修复的日期范围
    bad_dates = sorted({x["date"][:10] for x in r["rows"] if x["repaired"] is None})
    if bad_dates:
        print("   无法修复的日期范围：%s ~ %s（共 %d 天）"
              % (bad_dates[0], bad_dates[-1], len(bad_dates)))
    # 邻居列表
    print("   邻站：%s" % ", ".join("%s(%dkm)" % (n["station_name"], n["distance_km"])
                                    for n in r["neighbors"]))
