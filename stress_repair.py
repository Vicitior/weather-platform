# -*- coding: utf-8 -*-
"""批量压力测试：多站多年异常扫描+自动修复，检验「好不好用」。"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weather_api import data as D

N_STATION = int(sys.argv[1]) if len(sys.argv) > 1 else 30
YEARS = [int(x) for x in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["2015"])]

sids = D._df("SELECT station_id FROM stations ORDER BY station_id LIMIT %d" % N_STATION)["station_id"].tolist()
print("批量修复测试：%d 站 × 年份 %s" % (len(sids), YEARS))

ok = fail = 0
tot_bad = tot_rep = tot_nodata = 0
t0 = time.time()
detail = []
fails = []
for sid in sids:
    for yr in YEARS:
        try:
            r = D.repair_daily(int(sid), year=yr)
            s = r["summary"]
            tot_bad += s["bad_rows"]
            tot_rep += s["repaired_values"]
            tot_nodata += s["no_neighbor_data"]
            ok += 1
            if s["bad_rows"]:
                detail.append((int(sid), r["station"]["station_name"], yr,
                               s["bad_rows"], s["repaired_values"], s["no_neighbor_data"]))
        except Exception as e:
            fail += 1
            fails.append((int(sid), yr, type(e).__name__, str(e)[:120]))
            print("  FAIL", sid, yr, type(e).__name__, str(e)[:100])

dt = time.time() - t0
n = max(ok + fail, 1)
print("-" * 70)
print("成功 %d / 失败 %d / 耗时 %.1fs / 平均 %.2fs 每站年" % (ok, fail, dt, dt / n))
print("坏日合计 %d，修复值 %d，无法修复 %d（成功率 %.1f%%）"
      % (tot_bad, tot_rep, tot_nodata, 100.0 * tot_rep / max(tot_rep + tot_nodata, 1)))
print("-" * 70)
print("有坏日的站（%d 个）：" % len(detail))
for d in detail[:30]:
    print("  id=%-5d %-10s %d年  坏日=%-3d 修复=%-3d 无数据=%d" % d)
if fails:
    print("-" * 70)
    print("失败明细：")
    for f in fails[:10]:
        print("  ", f)
