# -*- coding: utf-8 -*-
"""own6 vs clima 多站真实遮蔽对照（判断 API 默认选型是否正确）

背景：用 make_holdout_table.py 对「新乡 1382」做遮蔽重建，发现 own6 只在气温上
优于 clima（气温 0.54 vs 0.78℃），湿度/风速/日照反而明显更差
（11.07% vs 5.58%、0.73 vs 0.50、1.77 vs 0.78h）。但此前 validate_repair12/13
的 87 站 hold-out 结论是 own6 在 5 个指标上显著优于 clima。两者必须查清 ——
如果 API 的 BEST_METHOD 默认选错，所有插补质量都会受拖累。

做法：对多个站 × 多个指标，同一批遮蔽点分别用 own6 / clima 修，逐点比真值。
不做沙箱改写（避免 876 文件硬链接的 IO 开销），直接把「真值记忆」在内存里，
用 repair_daily 的 metrics 参数只修目标指标 —— 但 repair_daily 读的是镜像文件，
遮蔽必须落盘，所以仍走沙箱。

输出：exports/_own6_audit.json
"""
import glob
import json
import os
import shutil
import sys
import time

import numpy as np
import pandas as pd

OUT = r"D:\Android PDF\exports"
TD_SRC = r"D:\Android PDF\db_mirror\tdengine\weather_data"
MY_SRC = r"D:\Android PDF\db_mirror\mysql"
BOX = r"D:\Android PDF\_audit_box"
BOX_DB = os.path.join(BOX, "db_mirror")

# 覆盖不同气候区与不同站网密度
STATIONS = [
    (1382, "新乡", "河南"), (1204, "乌鲁木齐", "新疆"),
    (1124, "漠河", "黑龙江"), (1213, "吐鲁番", "新疆"),
    (1667, "郑州", "河南"), (1610, "盐源", "四川"),
    (1245, "哈密", "新疆"), (1225, "喀什", "新疆"),
]
MASK_YEARS = (2016, 2019)
N_PER_METRIC = 30
SEED = 20261127
MISSING_VAL = 32766.0
METRICS = ["avg_temperature", "max_temperature", "min_temperature",
           "avg_humidity", "precipitation", "avg_wind_speed", "sunshine_hours"]
CN = {"avg_temperature": "平均气温", "max_temperature": "最高气温",
      "min_temperature": "最低气温", "avg_humidity": "平均湿度",
      "precipitation": "降雨量", "avg_wind_speed": "平均风速",
      "sunshine_hours": "日照时数"}
UNIT = {"avg_temperature": "℃", "max_temperature": "℃", "min_temperature": "℃",
        "avg_humidity": "%", "precipitation": "mm", "avg_wind_speed": "m/s",
        "sunshine_hours": "h"}
STRICT = {
    "avg_temperature": (-60, 55), "max_temperature": (-55, 55),
    "min_temperature": (-65, 45), "avg_humidity": (0, 100),
    "precipitation": (0, 500), "avg_wind_speed": (0, 60),
    "sunshine_hours": (0, 24),
}


def make_box():
    """全量硬链接镜像；调用方负责把「目标站」换成真实副本。"""
    if os.path.isdir(BOX):
        shutil.rmtree(BOX)
    box_td = os.path.join(BOX_DB, "tdengine", "weather_data")
    box_my = os.path.join(BOX_DB, "mysql")
    os.makedirs(box_td)
    os.makedirs(box_my)
    for f in glob.glob(os.path.join(TD_SRC, "*.parquet")):
        try:
            os.link(f, os.path.join(box_td, os.path.basename(f)))
        except OSError:
            shutil.copy2(f, os.path.join(box_td, os.path.basename(f)))
    for f in glob.glob(os.path.join(MY_SRC, "*.parquet")):
        shutil.copy2(f, os.path.join(box_my, os.path.basename(f)))
    return box_td


def true_date_str(ts):
    return "%04d-%02d-%02d" % (ts.year - 100, ts.month, ts.day)


def main():
    t0 = time.time()
    box_td = make_box()
    print("[box] 硬链接完成 %.1fs" % (time.time() - t0), flush=True)

    os.environ["WEATHER_DB_ROOT"] = BOX_DB
    sys.path.insert(0, r"D:\Android PDF")
    import weather_api.data as D                         # noqa: E402

    results = {}
    for si, (sid, sname, prov) in enumerate(STATIONS):
        src = os.path.join(TD_SRC, "station_%d.parquet" % sid)
        dst = os.path.join(box_td, "station_%d.parquet" % sid)
        if not os.path.exists(src):
            print("跳过 %s（无文件）" % sname, flush=True)
            continue
        if os.path.exists(dst):
            os.remove(dst)
        shutil.copy2(src, dst)
        assert os.stat(src).st_ino != os.stat(dst).st_ino, "目标站仍是硬链接！"

        df = pd.read_parquet(src)
        years = df["ts_bj"].dt.year - 100
        in_rng = np.where(((years >= MASK_YEARS[0]) & (years <= MASK_YEARS[1])).to_numpy())[0]
        rng = np.random.default_rng(SEED + si)
        picks, truth = {}, {}
        for m in METRICS:
            col = df[m].to_numpy(dtype="float64")
            lo, hi = STRICT[m]
            ok = in_rng[np.isfinite(col[in_rng])]
            ok = ok[(col[ok] >= lo) & (col[ok] <= hi)]
            if len(ok) == 0:
                picks[m] = []
                continue
            take = min(N_PER_METRIC, len(ok))
            idxs = np.sort(rng.choice(ok, size=take, replace=False))
            picks[m] = [(true_date_str(df["ts_bj"].iloc[i]), float(col[i])) for i in idxs]
            df.loc[df.index[idxs], m] = MISSING_VAL
        df.to_parquet(dst, index=False)

        row_out = {}
        for m in METRICS:
            pts = picks[m]
            if not pts:
                continue
            per = {}
            for meth in ("own6", "clima"):
                r = D.repair_daily(sid, start="%d-01-01" % MASK_YEARS[0],
                                   end="%d-12-31" % MASK_YEARS[1],
                                   metrics=[m], method=meth, detail_limit=100000)
                fx = {(x["date"][:10]): x for x in r["rows"]}
                errs = []
                for dstr, tv in pts:
                    x = fx.get(dstr)
                    if x and x.get("repaired") is not None:
                        errs.append(x["repaired"] - tv)
                if errs:
                    e = np.array(errs, dtype=float)
                    per[meth] = {"n": len(e), "mae": round(float(np.abs(e).mean()), 2),
                                 "rmse": round(float(np.sqrt((e ** 2).mean())), 2),
                                 "bias": round(float(e.mean()), 2)}
            if "own6" in per and "clima" in per:
                per["delta"] = round(per["own6"]["mae"] - per["clima"]["mae"], 2)
                per["winner"] = "own6" if per["delta"] < 0 else "clima"
            row_out[m] = per
        results[sid] = {"name": sname, "province": prov, "n_masked": sum(len(v) for v in picks.values()),
                        "metrics": row_out}
        print("[%d/%d] %s 完成（%d 点）  %.0fs"
              % (si + 1, len(STATIONS), sname, sum(len(v) for v in picks.values()),
                 time.time() - t0), flush=True)
        for m in METRICS:
            x = row_out.get(m, {})
            if "delta" in x:
                print("        %-16s own6 %.2f vs clima %.2f  Δ%+.2f → %s"
                      % (m, x["own6"]["mae"], x["clima"]["mae"], x["delta"], x["winner"]),
                      flush=True)

    # 汇总：每个指标多少站 own6 赢
    summary = {}
    for m in METRICS:
        wins = [sid for sid, v in results.items()
                if v["metrics"].get(m, {}).get("winner") == "own6"]
        lose = [sid for sid, v in results.items()
                if v["metrics"].get(m, {}).get("winner") == "clima"]
        d = [v["metrics"][m]["delta"] for v in results.values() if "delta" in v["metrics"].get(m, {})]
        summary[m] = {
            "metric_cn": CN[m], "unit": UNIT[m],
            "n_station": len(d),
            "own6_wins": len(wins), "clima_wins": len(lose),
            "mean_delta_mae": (round(float(np.mean(d)), 2) if d else None),
            "verdict": ("own6" if d and np.mean(d) < 0 else "clima"),
        }

    payload = {"seed": SEED, "mask_years": list(MASK_YEARS),
               "n_per_metric": N_PER_METRIC, "stations": results, "summary": summary}
    p = os.path.join(OUT, "_own6_audit.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("\n===== 汇总（own6 相对 clima 的 MAE 差，负=own6 更好）=====", flush=True)
    for m in METRICS:
        s = summary[m]
        print("%-16s own6 赢 %d 站 / clima 赢 %d 站，平均 Δ%+.2f%s → 建议 %s"
              % (m, s["own6_wins"], s["clima_wins"], s["mean_delta_mae"] or 0,
                 UNIT[m], s["verdict"]), flush=True)
    print("DONE -> %s (%.1f KB, %.0fs)" % (p, os.path.getsize(p) / 1024, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
