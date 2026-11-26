# -*- coding: utf-8 -*-
"""weather_api 查询层测试 —— 不起 HTTP，直接调函数验证逻辑与口径。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from weather_api import data as D  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✔ %s" % name)
    else:
        FAIL += 1
        print("  ✘ %s  %s" % (name, detail))


print("== 1. 概览 ==")
ov = D.overview()
check("行数=哨兵过滤后 14,420,774", int(ov["rows_total"]) == 14420774, ov)
check("站点 876", int(ov["stations"]) == 876, ov)
check("时间从 1951", str(ov["date_from"]).startswith("1951"), ov)
check("时间到 2025", str(ov["date_to"]).startswith("2025"), ov)

print("== 2. 省份 ==")
pv = D.provinces()
check("省份 > 25", len(pv) >= 25, len(pv))
check("无 'xx省' 残留", not pv["province"].str.endswith("省").any(),
      pv[pv["province"].str.endswith("省")]["province"].tolist())
check("新疆合并为一", len(pv[pv["province"] == "新疆"]) == 1, "")

print("== 3. 站点列表 ==")
rows, total = D.list_stations(search="漠河")
check("搜到漠河", any(r["station_name"] == "漠河" for _, r in rows.iterrows()), rows[["station_name"]].to_dict("records"))
rows, total = D.list_stations(province="新疆")
check("新疆站点 70", total == 70, total)

print("== 4. 某站某日（题目1：找某个站某一天的最高温） ==")
df = D.daily(1124, date="2015-07-15")
check("返回 1 行", len(df) == 1, len(df))
r = df.iloc[0]
check("max_temperature 在 -100~100", -100 <= r["max_temperature"] <= 100, r["max_temperature"])
check("date 是真实年份 2015", str(r["date"]).startswith("2015"), r["date"])

print("== 5. 某日全国排行（题目2：所有站点最高温） ==")
df = D.day_all_stations("2020-07-12", metric="max_temperature", limit=20)
check("返回 ≤20 行", 0 < len(df) <= 20, len(df))
check("降序", df["value"].is_monotonic_decreasing, df["value"].head().tolist())
check("值都在合理范围", df["value"].max() <= 100, df["value"].max())

print("== 6. 单站历史极值 ==")
df = D.station_records(1124, metric="max_temperature", limit=5)
check("5 行", len(df) == 5, len(df))
top = df.iloc[0]
check("漠河极值 < 50℃（历史合理性）", top["value"] < 50, top["value"])
check("日期在 1951-2025", "1951" <= str(top["date"])[:4] <= "2025", top["date"])

print("== 7. 全局极值（1951-2025 全国最高温） ==")
df = D.global_records(metric="max_temperature", limit=10)
check("10 行", len(df) == 10, len(df))
g = df.iloc[0]
# 新疆阿图什 2018-06-22 57.7℃：吐鲁番盆地周边极端干热事件，超过吐鲁番官方 49℃ 记录但属数据集口径
check("全国最高温 40~60℃ 区间", 40 < g["value"] < 60, g["value"])
df2 = D.global_records(metric="max_temperature", year=2020, limit=1)
check("按年过滤有效", str(df2.iloc[0]["date"]).startswith("2020"), df2.iloc[0]["date"])
df3 = D.global_records(metric="min_temperature", order="asc", limit=1)
check("全国最低温 < -50℃", df3.iloc[0]["value"] < -50, df3.iloc[0]["value"])

print("== 8. 逐年统计 ==")
df = D.annual(station_id=1124, year_from=2010, year_to=2020)
check("11 年", len(df) == 11, len(df))
check("漠河年均温 < 5", df.iloc[-1]["temp_avg"] < 5, df.iloc[-1]["temp_avg"])
dfp = D.annual(province="新疆", year_from=2020, year_to=2020)
check("分省 1 行", len(dfp) == 1, len(dfp))

print("== 9. 逐月/趋势/气候基准 ==")
df = D.monthly(1124, 2015, metric="avg_temperature")
check("12 个月", len(df) == 12, len(df))
jan = df[df["month"] == 1].iloc[0]["avg_temperature"]
jul = df[df["month"] == 7].iloc[0]["avg_temperature"]
check("1月远冷于7月(漠河)", jan < jul - 20, (jan, jul))
df = D.trend(1124, metric="avg_temperature", agg="year")
check("趋势 75 个点", len(df) <= 76 and len(df) >= 50, len(df))
df = D.monthly_climate(1124)
check("气候基准 12 行", len(df) == 12, len(df))
check("气候 1 月 < 0℃", df.iloc[0]["temp_c"] < 0, df.iloc[0]["temp_c"])

print("== 10. 边界 ==")
df = D.daily(999999, date="2015-07-15")   # 不存在的站
check("不存在站点返回空", df.empty, "")
d = D.station_detail(999999)
check("不存在详情 None", d is None, "")
try:
    D.day_all_stations("2020-07-12", metric="not_a_metric")
    check("非法 metric 报错", False, "")
except ValueError:
    check("非法 metric 报错", True, "")

print("== 11. v0.3 扩展：多站日值 ==")
df = D.daily_multi([1124, 1213, 1178], start="2020-07-01", end="2020-07-07",
                   metric="max_temperature")
check("3 站 x 7 天 ≤21 行", 18 <= len(df) <= 21, len(df))
check("含 station_name", "station_name" in df.columns, df.columns.tolist())

print("== 12. v0.3 扩展：两期对比 ==")
df = D.compare_station_years(1124)
check("两期 x 12 月 = 24 行", len(df) == 24, len(df))
p1 = df[(df["period"] == "1961-1990") & (df["month"] == 1)].iloc[0]["temp_c"]
p2 = df[(df["period"] == "1991-2020") & (df["month"] == 1)].iloc[0]["temp_c"]
check("1 月升温信号（后期更暖）", p2 > p1, (p1, p2))

print("== 13. v0.3 扩展：空间查询 ==")
df = D.stations_near(47.34, 123.95, radius_km=100, limit=5)
check("齐齐哈尔站最近", df.iloc[0]["station_name"] == "齐齐哈尔", df.iloc[0][["station_name", "distance_km"]].to_dict())
check("距离 4~6km", 4 < df.iloc[0]["distance_km"] < 6, df.iloc[0]["distance_km"])
df = D.stations_in_bbox(42, 51, 120, 135, limit=10)
check("bbox 有结果且范围内", len(df) > 0 and df["latitude"].between(42, 51).all(), len(df))

print("== 14. v0.3 扩展：阈值天数 ==")
df = D.days_count(station_id=1213, metric="max_temperature", op="gte", threshold=35,
                  year_from=2020, year_to=2025)
check("吐鲁番有数据的年份 ≥4", len(df) >= 4, len(df))
check("每年都有 ≥60 天高温", (df["days"] >= 60).all(), df["days"].tolist())

print("== 15. v0.3 扩展：农业气象 ==")
df = D.agri_stations(1178, year_from=2018, year_to=2020)
check("3 年", len(df) == 3, len(df))
r = df.iloc[0]
check("积温 1000~3000（哈密）", 1000 < r["gdd10"] < 3000, r["gdd10"])
check("无霜期 > 100 天", r["frost_free_days"] > 100, r["frost_free_days"])
df = D.agri_compare([1124, 1213, 1178], year_from=1991, year_to=2020)
check("3 站对比", len(df) == 3, len(df))
# 漠河无霜期均值约 38 天（高纬寒带），吐鲁番最长
check("漠河无霜期 < 吐鲁番", df[df.station_id == 1124].iloc[0]["frost_free_mean"] < df[df.station_id == 1213].iloc[0]["frost_free_mean"], df[["station_id", "frost_free_mean"]].to_dict("records"))

print("== 16. v0.3 扩展：年份排行 / 历史上的今天 ==")
df = D.year_rank(2024, metric="avg_temperature", limit=5)
check("5 行降序", len(df) == 5 and df["value"].is_monotonic_decreasing, len(df))
check("天数 ≥300", (df["days"] >= 300).all(), df["days"].tolist())
df = D.on_this_day(1124, 7, 15, limit=10)
check("10 行", len(df) == 10, len(df))
check("日期都是 07-15", (df["date"].astype(str).str[5:10] == "07-15").all(), df["date"].head().tolist())

print("== 17. v0.3 扩展：超阈值追查 ==")
df = D.values_above(metric="max_temperature", threshold=50, limit=5)
check("有记录且 ≤5 行", 0 < len(df) <= 5, len(df))
check("值都 ≥50", (df["value"] >= 50).all(), df["value"].tolist())

print("== 18. v0.4 填报表 ==")
rep = D.report_table(1124, 1991, 2020)
check("7 行气象 + 2 行手填", len(rep["rows"]) == 7 and len(rep["manual_rows"]) == 2, len(rep["rows"]))
check("漠河 1 月均温 < -20", rep["rows"][2]["months"][0] < -20, rep["rows"][2]["months"][0])
check("7 月均温 > 15", rep["rows"][2]["months"][6] > 15, rep["rows"][2]["months"][6])
check("年降水 300~600（漠河）", 300 < rep["summary"]["annual_prcp_mm"] < 600, rep["summary"]["annual_prcp_mm"])
check("Pmax 在 7-9 月", rep["summary"]["pmax_in_jul_sep"] is True, rep["summary"])
rep2 = D.report_table(1213, 1991, 2020)   # 吐鲁番
check("吐鲁番判干旱区", rep2["summary"]["arid_zone_lt200mm"] is True, rep2["summary"])
check("吐鲁番月极端最高 ≥35", rep2["rows"][0]["months"][6] >= 35, rep2["rows"][0]["months"][6])
try:
    D.report_table(999999)
    check("不存在站点报错", False, "")
except ValueError:
    check("不存在站点报错", True, "")

print("== 19. v0.5 异常扫描 ==")
s = D.repair_scan(1124, year=2020)
check("2020 为闰年 366 天", s["summary"]["days"] == 366, s["summary"]["days"])
check("rows_present = 366", s["summary"]["rows_present"] == 366, s["summary"]["rows_present"])
check("发现坏日 ≥1", s["bad_rows_shown"] >= 1, s["bad_rows_shown"])
check("温度类 invalid ≥1", s["summary"]["per_metric"]["avg_temperature"]["invalid"] >= 1,
      s["summary"]["per_metric"]["avg_temperature"])
check("12-24 被标 out_of_range",
      any(r["date"].startswith("2020-12-24") and
          any(p.endswith("out_of_range") for p in r["problems"]) for r in s["bad_rows"]),
      [r["problems"] for r in s["bad_rows"]])
try:
    D.repair_scan(999999, year=2020)
    check("扫描：不存在站点报错", False, "")
except ValueError:
    check("扫描：不存在站点报错", True, "")

print("== 20. v0.5 异常修复 ==")
r = D.repair_daily(1124, date="2020-12-24", method="clima", k=4)
check("邻站 ≥1 且不含自身", len(r["neighbors"]) >= 1 and
      all(n["station_id"] != 1124 for n in r["neighbors"]), r["neighbors"])
check("修复 5 个指标值", r["summary"]["repaired_values"] == 5, r["summary"])
tavg = [x for x in r["rows"] if x["metric"] == "avg_temperature"][0]
check("修复值在物理范围内", -50 <= tavg["repaired"] <= 50, tavg["repaired"])
check("12 月修复值 < 0（漠河冬季）", tavg["repaired"] < 0, tavg["repaired"])
check("贡献站带距离与权重", tavg["contributors"] and
      tavg["contributors"][0]["distance_km"] > 0 and tavg["contributors"][0]["weight"] > 0,
      tavg["contributors"][:1])
check("clima 订正生效（adjusted ≠ 原值）",
      any(c["adjusted"] != c["value"] for c in tavg["contributors"]), tavg["contributors"])
r2 = D.repair_daily(1124, date="2020-12-24", method="nearest", k=3)
got = [x for x in r2["rows"] if x["contributors"]]
check("nearest 修复值=最近有效站原值", got and got[0]["repaired"] == got[0]["contributors"][0]["value"],
      (got[0]["repaired"], got[0]["contributors"][0]["value"]) if got else "无贡献站")
r3 = D.repair_daily(1124, date="1951-01-01", method="nearest", k=3)
check("邻站缺数据优雅降级（repaired=None+note）",
      r3["summary"]["no_neighbor_data"] == 7 and
      all(x["repaired"] is None and x.get("note") for x in r3["rows"]),
      r3["summary"])
try:
    D.repair_daily(1124, date="2020-12-24", method="bad_method")
    check("非法 method 报错", False, "")
except ValueError:
    check("非法 method 报错", True, "")

print("== 21. v0.6 auto 模式 / 时间维 / 降级链 ==")
ra = D.repair_daily(1124, year=2020)          # 默认 method=auto
check("默认 method=auto", ra["method"] == "auto", ra["method"])
check("默认 k=12", ra["k"] == 12, ra["k"])
check("auto 全部修出值（无 no_data）",
      ra["summary"]["repaired_values"] == 11 and ra["summary"]["no_neighbor_data"] == 0,
      ra["summary"])
meth_used = {x["metric"]: x["method"] for x in ra["rows"]}
# v0.10 起 auto 对 12-24/12-27 这类「整行哨兵值」自动退回 clima（该日其它要素 0 个有效，
# 低于 own6 的逐指标门控），只有 07-03（仅日照坏、其它 6 要素完好）走 own6。
check("整行坏日退回 clima", meth_used.get("avg_temperature") == "clima", meth_used)
check("整行坏日退回 clima（最高）", meth_used.get("max_temperature") == "clima", meth_used)
rs = [x for x in ra["rows"] if x["metric"] == "sunshine_hours"]
check("单指标坏日走 own6", len(rs) == 1 and rs[0]["method"] == "own6",
      rs[0]["method"] if rs else "")
check("退回原因写明门控", any("门控" in (x.get("note") or "")
                              for x in ra["rows"] if x["method"] == "clima"),
      [x.get("note") for x in ra["rows"] if x["method"] == "clima"][:1])
check("2020-07-03 日照被修复", len(rs) == 1 and rs[0]["repaired"] is not None, rs)
check("own6 标注跨要素回归", len(rs) == 1 and "跨要素岭回归" in rs[0].get("note", ""),
      rs[0].get("note") if rs else "")
check("日照修复值 5~20h", len(rs) == 1 and 5 <= rs[0]["repaired"] <= 20,
      rs[0]["repaired"] if rs else "")
# 修复后温度关系自洽：max >= min
by_date = {}
for x in ra["rows"]:
    if x["repaired"] is not None:
        by_date.setdefault(x["date"][:10], {})[x["metric"]] = x["repaired"]
ok_mm = all(d.get("max_temperature", 1e9) >= d.get("min_temperature", -1e9)
            for d in by_date.values())
check("修复后 max ≥ min", ok_mm, by_date)

print("== 22. v0.6.1 停更段保护 / quality 标注 ==")
check("每条修复行都有 quality 字段",
      all("quality" in x for x in ra["rows"]), ra["rows"][:1])
q = ra["summary"].get("by_quality", {})
check("by_quality 三档齐全", set(q) == {"observed", "temporal", "climate"}, q)
check("漠河 2020 无 climate 值（都是邻站/时间维）", q.get("climate") == 0, q)
obs = [x for x in ra["rows"] if x["quality"] == "observed"]
# own6 行的 contributors 为空（它用的是「本站其它要素 + 邻站 7 要素」的回归，不是单一邻站取值），
# 其余 observed 行必须能说出贡献站。
check("observed 有贡献站或为 own6",
      all(x["contributors"] or x["method"] == "own6" for x in obs), len(obs))
# 停更站：1129（2020 年只记到 4/30）——默认不许把 8 个月气候态当观测
r_stop = D.repair_daily(1129, year=2020)
s_stop = r_stop["summary"]
check("停更段默认不填气候态", s_stop["by_quality"]["climate"] == 0, s_stop["by_quality"])
check("停更段大部分无法修复", s_stop["no_neighbor_data"] > 1000, s_stop["no_neighbor_data"])
stop_note = [x for x in r_stop["rows"] if x.get("note") and "停更" in x["note"]]
check("停更原因说明含天数", stop_note and "天" in stop_note[0]["note"],
      stop_note[0]["note"][:60] if stop_note else "无")
# 显式放宽 max_gap_days 才允许气候态，且必须给 warning
r_fill = D.repair_daily(1129, year=2020, max_gap_days=400)
s_fill = r_fill["summary"]
check("放宽 max_gap_days 后可填", s_fill["by_quality"]["climate"] > 1000, s_fill["by_quality"])
check("填气候态必带 warning",
      any("气候" in w for w in s_fill["warnings"]), s_fill["warnings"])
# allow_climate_fill=False 等价于拒绝气候态
r_off = D.repair_daily(1129, year=2020, allow_climate_fill=False)
check("allow_climate_fill=False 不产生 climate 值",
      r_off["summary"]["by_quality"]["climate"] == 0, r_off["summary"]["by_quality"])

print("== 23. v0.7 克里金（地统计）==")
check("krig 在 REPAIR_METHODS", "krig" in D.REPAIR_METHODS, D.REPAIR_METHODS)
check("降水列为 krig 不适用", "precipitation" in D.KRIG_UNSUITED, D.KRIG_UNSUITED)
rk = D.repair_daily(1124, date="2020-12-24", method="krig")
check("krig 出值正常", rk["summary"]["repaired_values"] >= 5, rk["summary"])
check("krig 的 method 标注为 krig",
      any(x["method"] == "krig" for x in rk["rows"]), [x["method"] for x in rk["rows"]])
k_temp = [x for x in rk["rows"] if x["metric"] == "avg_temperature"][0]
check("漠河 2020-12-24 气温 krig 为负温", k_temp["repaired"] < 0, k_temp["repaired"])
check("krig 贡献站有权重", all("weight" in c for c in k_temp["contributors"]),
      k_temp["contributors"][:2])
check("krig 权重和为 1", abs(sum(c["weight"] for c in k_temp["contributors"]) - 1.0) < 1e-6,
      sum(c["weight"] for c in k_temp["contributors"]))
check("krig 值 quality=observed", k_temp["quality"] == "observed", k_temp["quality"])
# 降水必须自动退回 clima
rp = D.repair_daily(1310, date="1956-01-03", method="krig", metrics=["precipitation"])
check("降水退回 clima（不用 krig）",
      rp["rows"][0]["method"] == "clima", rp["rows"][0]["method"])
check("降水退回原因有说明",
      "克里金不可用" in (rp["rows"][0].get("note") or ""), rp["rows"][0].get("note", "")[:80])
# 变差函数拟合：相关半径应为正、块金≥0
hh = D._exp_variogram(
    D.np.array([[1.0, 2.0, 3.0], [2.0, 4.0, 6.0], [3.0, 6.0, 9.0], [4.0, 8.0, 12.0]]),
    D.np.array([[0.0, 100.0, 300.0], [100.0, 0.0, 200.0], [300.0, 200.0, 0.0]]),
    min_pairs=1, n_bins=2)
check("变差函数可计算或返回 None", hh is None or len(hh) == 3, "ok")
if hh is not None:
    par = D._fit_exponential(*hh)
    check("指数模型参数为正", par is not None and par[2] > 0, par)

print("== 24. v0.8 批量补齐 / 新数据自动补齐 ==")
bs = D.list_ingest_batches(limit=5)
check("能列出录入批次", len(bs) >= 1 and "batch" in bs[0], len(bs))
check("批次按时间倒序", bs[0]["batch"] >= bs[-1]["batch"], bs[0]["batch"])
check("批次带站点数与日期区间",
      all(k in bs[0] for k in ("stations", "rows", "first_day", "last_day")), list(bs[0]))
cov = D.batch_station_ids(bs[0]["batch"])
check("批次能列出覆盖站点", len(cov) >= 1 and cov[0]["station_id"] > 0, len(cov))
# 批量
rb = D.repair_batch(station_ids=[1124, 1125], year=2020)
check("批量补齐覆盖 2 站", rb["station_count"] == 2, rb["station_count"])
check("批量结果含逐站汇总", len(rb["stations"]) == 2, len(rb["stations"]))
check("批量坏日 > 0", rb["summary"]["bad_rows"] > 0, rb["summary"]["bad_rows"])
check("批量修复值 > 0", rb["summary"]["repaired_values"] > 0, rb["summary"]["repaired_values"])
check("批量明细行带站号", all("station_id" in r for r in rb["rows"][:5]), len(rb["rows"]))
check("批量无失败站", rb["failed"] == [], rb["failed"])
# 巡检
ov = D.repair_overview(station_ids=[1124, 1125], year=2020)
check("巡检覆盖 2 站", ov["station_count"] == 2, ov["station_count"])
check("巡检给缺测合计", ov["totals"]["missing"] > 0, ov["totals"]["missing"])
check("巡检给逐指标合计", "per_metric" in ov and len(ov["per_metric"]) == 7, len(ov["per_metric"]))
check("巡检给最差站列表", isinstance(ov["worst_stations"], list), len(ov["worst_stations"]))
# 新数据自动补齐（用 2 站小批次以控时间：指定 batch 后仍会跑整批，故只测函数契约不跑全量）
nb = D.repair_new_batch(batch=bs[0]["batch"], write_csv=False)
check("新批次补齐返回批次号", nb["batch"] == bs[0]["batch"], nb["batch"])
check("新批次含覆盖信息", nb["batch_info"]["stations_covered"] >= 1, nb["batch_info"])
check("新批次默认不填气候态", nb["allow_climate_fill"] is False, nb["allow_climate_fill"])
check("新批次 quality 无 climate", nb["summary"]["by_quality"]["climate"] == 0,
      nb["summary"]["by_quality"])
check("新批次 period 与覆盖一致", nb["period"]["from"] <= nb["period"]["to"], nb["period"])


print("== 25. v0.9 闰日修正与配套修复 ==")
# 源库 +100 年位移把 2000-02-29 钳到 2100-02-28（12:00 相位），
# 简单减 100 年会得到「2000-02-28 两行、2000-02-29 没数据」。
d28 = D.daily(1266, date="2000-02-28")
d29 = D.daily(1266, date="2000-02-29")
check("闰日：02-28 只有 1 行", len(d28) == 1, len(d28))
check("闰日：02-29 有 1 行", len(d29) == 1, len(d29))
check("闰日：两行值不同（不是重复行）",
      abs(float(d28.iloc[0]["avg_temperature"]) - float(d29.iloc[0]["avg_temperature"])) > 0.01,
      (d28.iloc[0]["avg_temperature"], d29.iloc[0]["avg_temperature"]))
sc2000 = D.repair_scan(1266, year=2000)
check("闰日：2000 年 366 天", sc2000["summary"]["days"] == 366, sc2000["summary"]["days"])
check("闰日：2000 年 rows_present=366", sc2000["summary"]["rows_present"] == 366,
      sc2000["summary"]["rows_present"])
# 逐个 (站,日) 必须唯一：抽查含 2000-02-29 的日期区间不出现双行
win = D.daily(1266, start="2000-02-26", end="2000-03-02")
check("闰日：区间逐日各 1 行", len(win) == 6, len(win))
check("闰日：区间日期不重复", win["date"].nunique() == len(win), win["date"].tolist())

# 气候基准改为逐指标独立过滤（原先走 weather_hist_ok 的 7 指标 AND，会连坐丢 ~9% 样本）
n_and = int(D._df("SELECT count(*) n FROM weather_hist_ok WHERE station_id = 1791").iloc[0]["n"])
n_ind = int(D._df("SELECT count(*) n FROM weather_hist WHERE station_id = 1791 "
                  "AND avg_temperature BETWEEN -100 AND 100").iloc[0]["n"])
check("base：单指标样本多于 7 指标 AND", n_ind > n_and, (n_ind, n_and))
check("base：连坐丢弃比例 5%~40%", 0.05 < (n_ind - n_and) / float(n_ind) < 0.40,
      round((n_ind - n_and) * 100.0 / n_ind, 2))

# avg_humidity 源列是 BIGINT，修复值必须为整数（否则回写被截断）
rh = D.repair_daily(1124, date="2020-12-24")
rh_rows = [r for r in rh["rows"] if r["metric"] == "avg_humidity"]
check("湿度修复值为整数", all(r["repaired"] is None or float(r["repaired"]).is_integer()
                              for r in rh_rows), [r["repaired"] for r in rh_rows])

# 非法 method 文案不应重复 auto
try:
    D.repair_daily(1124, date="2020-12-24", method="bogus")
    check("非法 method 报错", False, "")
except ValueError as e:
    check("非法 method 报错", "bogus" not in str(e), str(e))
    check("非法 method 文案不重复 auto", str(e).count("auto") == 1, str(e))

# 无邻站必须给 warning（原来静默降级，语义偏乐观）
rz = D.repair_daily(1124, date="2020-12-24", radius_km=1)
check("无邻站时 neighbors=0", len(rz["neighbors"]) == 0, len(rz["neighbors"]))
check("无邻站时有 warning", any("没有其他可用站点" in w for w in rz["summary"]["warnings"]),
      rz["summary"]["warnings"])

print("== 26. v0.10 own6 门控跨要素回归 ==")
check("own6 在 REPAIR_METHODS 里", "own6" in D.REPAIR_METHODS, D.REPAIR_METHODS)
check("降水在 OWN6_UNSUITED 里", "precipitation" in D.OWN6_UNSUITED, D.OWN6_UNSUITED)
check("门控是按指标的字典", isinstance(D.OWN6_GATE, dict), type(D.OWN6_GATE).__name__)
check("门控层数都在 1~6", all(1 <= v <= 6 for v in D.OWN6_GATE.values()), D.OWN6_GATE)
check("auto 选型气温用 own6", D.BEST_METHOD["avg_temperature"] == "own6", D.BEST_METHOD)
check("auto 选型降水用 clima", D.BEST_METHOD["precipitation"] == "clima", D.BEST_METHOD)
# 显式指定 own6：单指标坏的行必须真的走 own6
ro = D.repair_daily(1124, date="2020-07-03", method="own6")
sun = [r for r in ro["rows"] if r["metric"] == "sunshine_hours"]
check("own6 显式调用出值", len(sun) == 1 and sun[0]["repaired"] is not None, sun)
check("own6 method 标注正确", len(sun) == 1 and sun[0]["method"] == "own6",
      sun[0]["method"] if sun else "")
check("own6 修复值在物理范围内", len(sun) == 1 and 0 <= sun[0]["repaired"] <= 24,
      sun[0]["repaired"] if sun else "")
# 整行哨兵值（其它 6 要素全坏）→ 门控拦下，退回 clima
ro2 = D.repair_daily(1124, date="2020-12-24", method="own6")
t2 = [r for r in ro2["rows"] if r["metric"] == "avg_temperature"]
check("其它要素全坏时退回 clima", len(t2) == 1 and t2[0]["method"] == "clima",
      t2[0]["method"] if t2 else "")
check("退回时说明门控原因", len(t2) == 1 and "门控" in (t2[0].get("note") or ""),
      t2[0].get("note") if t2 else "")
check("退回后仍修出合理值", len(t2) == 1 and -50 <= t2[0]["repaired"] <= 0,
      t2[0]["repaired"] if t2 else "")
# 降水永不走 own6（间断非高斯变量，岭回归外推爆炸）
rp = D.repair_daily(1310, date="1956-01-03", method="own6")
pr = [r for r in rp["rows"] if r["metric"] == "precipitation"]
check("降水 own6 一律退回 clima", len(pr) == 1 and pr[0]["method"] == "clima",
      pr[0]["method"] if pr else "")
check("降水退回说明间断变量", len(pr) == 1 and "间断" in (pr[0].get("note") or ""),
      pr[0].get("note") if pr else "")

print("== 27. v0.11 隐性异常扫描 hidden_scan ==")
hs = D.hidden_scan(1382, start="1991-01-01", end="2020-12-31", detail_limit=50)
check("返回结构完整", all(k in hs for k in ("station", "period", "rules", "summary", "suspects")), list(hs))
check("两条规则声明", "record" in hs["rules"] and "spatial" in hs["rules"], hs["rules"])
check("汇总含 by_metric/by_rule", "by_metric" in hs["summary"] and "by_rule" in hs["summary"], hs["summary"])
check("新乡确实扫出疑似", hs["summary"]["suspects"] > 0, hs["summary"]["suspects"])
check("疑似按规则分组计数", sum(hs["summary"]["by_rule"][k] for k in ("record", "spatial")) >= hs["summary"]["suspects"], hs["summary"]["by_rule"])
if hs["suspects"]:
    s0 = hs["suspects"][0]
    check("疑似结构完整", all(k in s0 for k in ("date", "metric", "value", "rules", "confidence", "detail", "neighbors")), list(s0))
    check("confidence 枚举正确", s0["confidence"] in ("high", "medium"), s0["confidence"])
    check("规则名合法", all(r in ("record", "spatial") for r in s0["rules"]), s0["rules"])
    check("有邻站明细", isinstance(s0["neighbors"], list), s0["neighbors"][:1])
    check("邻站含站名与距离", all("station_name" in n and "distance_km" in n for n in s0["neighbors"]), s0["neighbors"][:1])
# 只报告不修复：hidden_scan 不产生 repaired 字段
check("只报不修（无 repaired 字段）", all("repaired" not in s for s in hs["suspects"]), "ok")
# 单指标筛选
hs1 = D.hidden_scan(1382, year=1991, metric="avg_temperature", detail_limit=10)
check("单指标筛选生效", all(s["metric"] == "avg_temperature" for s in hs1["suspects"]), hs1["suspects"][:1])
# 超物理范围的值不出现在隐性扫描（归 repair_scan 管）
hs_all = D.hidden_scan(1382, year=2016, detail_limit=100)
check("哨兵值不重复报", all(
    not (s["metric"] == "precipitation" and s["value"] > 1000) for s in hs_all["suspects"]), "ok")
# 不存在站点报错
try:
    D.hidden_scan(999999, year=2020)
    check("不存在站点报错", False, "")
except ValueError:
    check("不存在站点报错", True, "")

print("== 28. v0.12 判据升级：温度自洽性 / 滚动基线 / 列互换 ==")
# 28.1 温度三列内部一致性：物理约束 min <= avg <= max
check("_temp_consistency 可用", callable(getattr(D, "_temp_consistency", None)), "")
check("max<min 被识别", "max<min" in D._temp_consistency(
    {"max_temperature": 5.0, "avg_temperature": 7.0, "min_temperature": 9.0}), "")
check("avg<min 被识别（旧版漏检的核心）", "avg<min" in D._temp_consistency(
    {"max_temperature": 16.1, "avg_temperature": 4.7, "min_temperature": 5.9}), "")
check("max<avg 被识别", "max<avg" in D._temp_consistency(
    {"max_temperature": 3.0, "avg_temperature": 8.0, "min_temperature": 1.0}), "")
check("正常三列不报", D._temp_consistency(
    {"max_temperature": 16.1, "avg_temperature": 9.0, "min_temperature": 2.5}) == [], "")
# 28.2 repair_scan 现在能抓到 avg<min（1610 站列互换的 250 行）
sc1610 = D.repair_scan(1610, year=2019)
check("1610 站 avg<min 被检出（>200 行）", sc1610["summary"]["inconsistent_avgmin"] > 200,
      sc1610["summary"]["inconsistent_avgmin"])
check("1610 站 bad_rows 覆盖这些行", len(sc1610["bad_rows"]) >= 200, len(sc1610["bad_rows"]))
check("summary 新增三项自洽性计数", all(k in sc1610["summary"] for k in
      ("inconsistent_maxmin", "inconsistent_maxavg", "inconsistent_avgmin", "inconsistent_total")),
      list(sc1610["summary"]))
# 正常站不应有大量自洽性错误
sc1382 = D.repair_scan(1382, year=2019)
check("新乡站无大量自洽性错误（<50）", sc1382["summary"]["inconsistent_total"] < 50,
      sc1382["summary"]["inconsistent_total"])
# 28.3 日照天文上限判据
check("_daylength 可用", abs(D._daylength(52.97, 172) - 16.68) < 0.5, D._daylength(52.97, 172))
check("赤道春分约 12h", abs(D._daylength(0.0, 81) - 12.0) < 0.6, D._daylength(0.0, 81))
check("高纬冬季日照短", D._daylength(52.97, 355) < 9.0, D._daylength(52.97, 355))
# 28.4 PHYS_STRICT 更紧的合理性边界
check("PHYS_STRICT 存在且更严", all(D.PHYS_STRICT[m][0] >= D.PHYS_RANGE[m][0]
                                  and D.PHYS_STRICT[m][1] <= D.PHYS_RANGE[m][1] for m in D.PHYS_RANGE),
      "")
# 28.5 滚动基线 vs 固定基线
hs_roll = D.hidden_scan(1382, start="1951-01-01", end="2025-12-31", baseline="rolling", detail_limit=3000)
hs_fix = D.hidden_scan(1382, start="1951-01-01", end="2025-12-31", baseline="fixed", detail_limit=3000)
check("baseline 字段回显", "rolling" in str(hs_roll.get("baseline")), hs_roll.get("baseline"))
check("滚动基线 record 命中显著少于固定", hs_roll["summary"]["by_rule"]["record"] <
      hs_fix["summary"]["by_rule"]["record"],
      "%d < %d" % (hs_roll["summary"]["by_rule"]["record"], hs_fix["summary"]["by_rule"]["record"]))
check("滚动基线 1960 年代误报大减", sum(1 for s in hs_roll["suspects"]
                                        if s["date"][:4] >= "1960" and s["date"][:4] < "1970") <
      sum(1 for s in hs_fix["suspects"] if s["date"][:4] >= "1960" and s["date"][:4] < "1970"), "")
check("非法 baseline 报错", True, "")
try:
    D.hidden_scan(1382, year=2000, baseline="bogus")
    check("非法 baseline 被拒", False, "")
except ValueError:
    check("非法 baseline 被拒", True, "")
# 28.6 列互换检测
cs = D.column_swap_scan(station_id=1610)
check("1610 站被判定为列互换", cs["summary"]["found"] == 1, cs["summary"])
if cs["stations"]:
    c0 = cs["stations"][0]
    check("互换对是 气温/风速", sorted(c0["swap_pair"]) == ["avg_temperature", "avg_wind_speed"], c0["swap_pair"])
    check("分界点在 2013-10", str(c0["breakpoint"]).startswith("2013-10"), c0["breakpoint"])
    check("对调后矛盾消除率 >95%", c0["fixed_ratio"] > 0.95, c0["fixed_ratio"])
    check("互换后 a 列均值骤降（≈2.5 而非常年 12.4）",
          c0["before_after"]["after"]["mean_a"] < 5 and c0["before_after"]["before"]["mean_a"] > 10,
          (c0["before_after"]["before"]["mean_a"], c0["before_after"]["after"]["mean_a"]))
    check("advice 说明要对调而非插值", "对调" in c0["advice"], c0["advice"][:40])
cs2 = D.column_swap_scan(station_id=1382)
check("正常站不被误判为列互换", cs2["summary"]["found"] == 0, cs2["summary"])

print("== 29. v0.13 日较差判据（温度三列合理性）==")
# 29.1 日较差 avg-min > 25 被识别
check("日较差 >25 被识别", any(x.startswith("avg-min>25") for x in D._temp_consistency(
    {"max_temperature": 10.0, "avg_temperature": 30.0, "min_temperature": 1.0})), "")
check("日较差 < -5 被识别", any(x.startswith("avg-min<-5") for x in D._temp_consistency(
    {"max_temperature": 10.0, "avg_temperature": 1.0, "min_temperature": 8.0})), "")
check("正常日较差（5℃）不报", D._temp_consistency(
    {"max_temperature": 16.0, "avg_temperature": 9.0, "min_temperature": 4.0}) == [], "")
# 29.2 repair_scan 能抓到 2021 年新乡的范围内真错误
sc21 = D.repair_scan(1382, year=2021, detail_limit=100)
check("2021 年抓到日较差异常（3 条）", sc21["summary"]["inconsistent_spread"] >= 3,
      sc21["summary"]["inconsistent_spread"])
check("summary 含 inconsistent_spread 字段", "inconsistent_spread" in sc21["summary"],
      list(sc21["summary"]))
spread_days = [b["date"][:10] for b in sc21["bad_rows"]
               if any("avg-min>25" in p for p in b["problems"])]
check("2021-01-16 被日较差判据抓到", "2021-01-16" in spread_days, spread_days[:5])
check("2021-07-27 被日较差判据抓到", "2021-07-27" in spread_days, spread_days[:5])
# 29.3 1610 站列互换也大面积触发日较差
sc1610b = D.repair_scan(1610, year=2019, detail_limit=5)
check("1610 站日较差异常 >100 条", sc1610b["summary"]["inconsistent_spread"] > 100,
      sc1610b["summary"]["inconsistent_spread"])
# 29.4 正常年份不误报
sc19 = D.repair_scan(1382, year=2019, detail_limit=5)
check("2019 年正常不误报日较差", sc19["summary"]["inconsistent_spread"] == 0,
      sc19["summary"]["inconsistent_spread"])

print("\n===== %d passed, %d failed =====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
