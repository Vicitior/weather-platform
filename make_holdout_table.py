# -*- coding: utf-8 -*-
"""人为遮蔽 → 插补 → 与原始真值对比（hold-out 重建检验）

用途：回答「插补出来的值到底准不准」。做法是把目标站若干天的真实观测
人为挖空（写成源库缺测标记 32766），再让 repair_daily 去补，最后拿补出来
的值和原始真值逐点比对。

关键设计（为什么可信）：
  1) 只遮蔽目标站 —— 邻站数据保持原样，模拟「本站坏了、别人还好」的真实场景；
  2) 遮蔽只落在 2016-2019 年，而插补模型的训练期是 1991-2005（own6 岭回归）
     和 1991-2020 气候基准 —— 训练数据里不含被遮蔽的点，没有标签泄漏；
  3) 每个遮蔽点都记录「原始真值 / 插补值 / 绝对误差 / 用的方法 / 可信度 /
     参与了哪些邻站」，逐条可核对。

沙箱机制：db_mirror 里除目标站外全部用硬链接（不额外占空间），目标站先
删除硬链接再复制成真实副本，绝不影响原始镜像文件。

输出：exports/_holdout_<TAG>.json
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
BOX = r"D:\Android PDF\_holdout_box"
BOX_DB = os.path.join(BOX, "db_mirror")

SID = int(sys.argv[1]) if len(sys.argv) > 1 else 1382
TAG = sys.argv[2] if len(sys.argv) > 2 else ("s%d" % SID)
MASK_YEARS = (2016, 2019)      # 遮蔽落在这些年（数据完整）
N_PER_METRIC = 40              # 每个指标遮蔽多少个点
SEED = 20261126
MISSING_VAL = 32766.0          # 源库通用缺测标记

METRICS = ["avg_temperature", "max_temperature", "min_temperature",
           "avg_humidity", "precipitation", "avg_wind_speed", "sunshine_hours"]
CN = {"avg_temperature": "平均气温", "max_temperature": "最高气温",
      "min_temperature": "最低气温", "avg_humidity": "平均湿度",
      "precipitation": "降雨量", "avg_wind_speed": "平均风速",
      "sunshine_hours": "日照时数"}
UNIT = {"avg_temperature": "℃", "max_temperature": "℃", "min_temperature": "℃",
        "avg_humidity": "%", "precipitation": "mm", "avg_wind_speed": "m/s",
        "sunshine_hours": "h"}


def build_sandbox():
    """硬链接整个镜像到沙箱，目标站换成真实副本（可安全改写）。"""
    print("[1/5] 建沙箱 ...", flush=True)
    if os.path.isdir(BOX):
        shutil.rmtree(BOX)
    box_td = os.path.join(BOX_DB, "tdengine", "weather_data")
    box_my = os.path.join(BOX_DB, "mysql")
    os.makedirs(box_td)
    os.makedirs(box_my)

    n_link = 0
    target = "station_%d.parquet" % SID
    for f in glob.glob(os.path.join(TD_SRC, "*.parquet")):
        base = os.path.basename(f)
        dst = os.path.join(box_td, base)
        if base == target:
            continue                      # 目标站稍后单独复制
        try:
            os.link(f, dst)
            n_link += 1
        except OSError:
            shutil.copy2(f, dst)
            n_link += 1

    # MySQL 表都很小（0.5MB 全量），直接复制
    for f in glob.glob(os.path.join(MY_SRC, "*.parquet")):
        shutil.copy2(f, os.path.join(box_my, os.path.basename(f)))

    # 目标站：先删掉可能的硬链接（绝不改原文件），再复制真实副本
    src = os.path.join(TD_SRC, target)
    dst = os.path.join(box_td, target)
    if os.path.exists(dst):
        os.remove(dst)
    shutil.copy2(src, dst)
    # 安全断言：副本必须与原文件 inode 不同
    assert os.stat(src).st_ino != os.stat(dst).st_ino, "目标站副本仍是硬链接，危险！"
    print("      硬链接 %d 个 + 目标站真实副本（inode 已确认不同）" % n_link, flush=True)
    return src, dst


def true_date_str(ts):
    """parquet 里 ts_bj 是源库原始值（整体 +100 年），换回真实日期。

    与 data.py 的 date_true 口径一致：年份减 100，月日不变。
    （12:00 相位的闰日特例只出现在 2100-02-28，不在本脚本遮蔽年份范围内。）
    """
    return "%04d-%02d-%02d" % (ts.year - 100, ts.month, ts.day)


def pick_mask_points(df):
    """在遮蔽年份里按指标随机挑点，要求该点原本是有效观测。"""
    print("[2/5] 挑遮蔽点 ...", flush=True)
    # 注意：ts_bj 是源库 +100 年的原始值，必须减去才是真实年份
    years = df["ts_bj"].dt.year - 100
    in_range = (years >= MASK_YEARS[0]) & (years <= MASK_YEARS[1])
    idx_pool = np.where(in_range.to_numpy())[0]
    print("      按真实年份筛选：%s~%s 共 %d 行"
          % (MASK_YEARS[0], MASK_YEARS[1], len(idx_pool)), flush=True)
    rng = np.random.default_rng(SEED)
    picks = {}
    for m in METRICS:
        col = df[m].to_numpy(dtype="float64")
        # 只挑有效观测（在严格合理范围内）
        lo, hi = {
            "avg_temperature": (-60, 55), "max_temperature": (-55, 55),
            "min_temperature": (-65, 45), "avg_humidity": (0, 100),
            "precipitation": (0, 500), "avg_wind_speed": (0, 60),
            "sunshine_hours": (0, 24),
        }[m]
        ok = idx_pool[np.isfinite(col[idx_pool])]
        ok = ok[(col[ok] >= lo) & (col[ok] <= hi)]
        if len(ok) == 0:
            picks[m] = np.array([], dtype=int)
            continue
        take = min(N_PER_METRIC, len(ok))
        picks[m] = np.sort(rng.choice(ok, size=take, replace=False))
        print("      %-16s 候选 %5d 个，遮蔽 %d 个" % (m, len(ok), take), flush=True)
    return picks


def main():
    t0 = time.time()
    src, dst = build_sandbox()

    print("[3/5] 读原始数据并写遮蔽版 ...", flush=True)
    df = pd.read_parquet(src)
    truth = df.copy()                       # 保留真值
    picks = pick_mask_points(df)

    masked_rows = {}
    for m, idxs in picks.items():
        for i in idxs:
            masked_rows.setdefault(m, []).append({
                "date": true_date_str(df["ts_bj"].iloc[i]),
                "truth": float(df[m].iloc[i]),
            })
        df.loc[df.index[idxs], m] = MISSING_VAL

    df.to_parquet(dst, index=False)
    n_masked = sum(len(v) for v in masked_rows.values())
    print("      写入遮蔽版：%d 个点" % n_masked, flush=True)

    # 让 data.py 读沙箱（必须在 import 之前设置）
    os.environ["WEATHER_DB_ROOT"] = BOX_DB
    sys.path.insert(0, r"D:\Android PDF")
    import weather_api.data as D            # noqa: E402

    print("[4/5] 跑 repair_daily（auto）...", flush=True)
    t1 = time.time()
    rep = D.repair_daily(
        SID, start="%d-01-01" % MASK_YEARS[0], end="%d-12-31" % MASK_YEARS[1],
        detail_limit=100000,
    )
    print("      耗时 %.1fs，bad_rows=%d repaired=%d by_quality=%s"
          % (time.time() - t1, rep["summary"]["bad_rows"],
             rep["summary"]["repaired_values"], rep["summary"]["by_quality"]), flush=True)

    fix = {}
    for x in rep["rows"]:
        fix[(x["date"][:10], x["metric"])] = x

    # ---- 方法对照：同一批遮蔽点，用不同 method 各修一遍，看谁补得准 ----
    # 整段区间一次调用（不是逐日循环），每个方法约 2s。
    print("[4b/5] 方法对照 ...", flush=True)
    method_compare = {}
    for meth2 in ("own6", "clima", "krig", "blend", "nearest", "idw", "alt_idw"):
        try:
            r2 = D.repair_daily(
                SID, start="%d-01-01" % MASK_YEARS[0], end="%d-12-31" % MASK_YEARS[1],
                method=meth2, detail_limit=100000,
            )
        except Exception as e:                              # noqa: BLE001
            print("      %-8s 失败：%s" % (meth2, e), flush=True)
            continue
        fx = {(x["date"][:10], x["metric"]): x for x in r2["rows"]}
        per = {}
        for m in METRICS:
            errs = []
            for item in masked_rows.get(m, []):
                x = fx.get((item["date"], m))
                if x and x.get("repaired") is not None:
                    errs.append(x["repaired"] - item["truth"])
            if errs:
                e = np.array(errs, dtype=float)
                per[m] = {"n": len(e),
                          "mae": round(float(np.abs(e).mean()), 2),
                          "rmse": round(float(np.sqrt((e ** 2).mean())), 2),
                          "bias": round(float(e.mean()), 2)}
        method_compare[meth2] = per
        print("      %-8s 完成" % meth2, flush=True)

    print("[5/5] 比对真值 ...", flush=True)
    rows = []
    for m in METRICS:
        for item in masked_rows.get(m, []):
            k = (item["date"], m)
            x = fix.get(k)
            r = {
                "date": item["date"], "metric": m, "metric_cn": CN[m], "unit": UNIT[m],
                "truth": round(item["truth"], 2),
                "repaired": (round(x["repaired"], 2) if x and x.get("repaired") is not None else None),
                "method": (x.get("method") if x else None),
                "quality": (x.get("quality") if x else None),
                "n_contrib": (len(x.get("contributors") or []) if x else 0),
                "contrib_names": "、".join([c.get("station_name", "") for c in (x.get("contributors") or [])][:6]) if x else "",
            }
            if r["repaired"] is None:
                r["err"] = None
                r["abs_err"] = None
                r["rel_err"] = None
            else:
                e = r["repaired"] - r["truth"]
                r["err"] = round(e, 2)
                r["abs_err"] = round(abs(e), 2)
                r["rel_err"] = (round(abs(e) / abs(r["truth"]) * 100, 1) if abs(r["truth"]) > 0.1 else None)
            rows.append(r)

    # 分指标统计
    by_metric = {}
    for m in METRICS:
        sub = [r for r in rows if r["metric"] == m and r["abs_err"] is not None]
        n_all = len([r for r in rows if r["metric"] == m])
        if not sub:
            by_metric[m] = {"n_masked": n_all, "n_repaired": 0}
            continue
        errs = np.array([r["err"] for r in sub], dtype=float)
        aerr = np.abs(errs)
        truth_arr = np.array([r["truth"] for r in sub], dtype=float)
        pred_arr = np.array([r["repaired"] for r in sub], dtype=float)
        ss_res = float(np.sum((truth_arr - pred_arr) ** 2))
        ss_tot = float(np.sum((truth_arr - truth_arr.mean()) ** 2))
        by_metric[m] = {
            "metric_cn": CN[m], "unit": UNIT[m],
            "n_masked": n_all, "n_repaired": len(sub),
            "mae": round(float(aerr.mean()), 2),
            "rmse": round(float(np.sqrt((errs ** 2).mean())), 2),
            "bias": round(float(errs.mean()), 2),
            "p50": round(float(np.percentile(aerr, 50)), 2),
            "p90": round(float(np.percentile(aerr, 90)), 2),
            "max_abs": round(float(aerr.max()), 2),
            "r2": (round(1 - ss_res / ss_tot, 3) if ss_tot > 0 else None),
        }

    by_method = {}
    by_quality = {}
    for r in rows:
        if r["repaired"] is None:
            continue
        by_method[r["method"]] = by_method.get(r["method"], 0) + 1
        by_quality[r["quality"]] = by_quality.get(r["quality"], 0) + 1

    payload = {
        "station_id": SID,
        "mask_years": list(MASK_YEARS),
        "n_masked": n_masked,
        "n_repaired": len([r for r in rows if r["repaired"] is not None]),
        "seed": SEED,
        "config": {"method": "auto", "k": 12, "radius_km": 300,
                   "train_period": "1991-2005（own6 岭回归）/ 1991-2020（气候基准）"},
        "by_metric": by_metric,
        "by_method": by_method,
        "by_quality": by_quality,
        "method_compare": method_compare,
        "rows": rows,
    }

    p = os.path.join(OUT, "_holdout_%s.json" % TAG)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    print("DONE -> %s (%.1f KB, %.1fs)" % (p, os.path.getsize(p) / 1024, time.time() - t0), flush=True)
    for m in METRICS:
        b = by_metric[m]
        if b.get("n_repaired"):
            print("  %-16s MAE %.2f%s  RMSE %.2f  bias %+.2f  R2 %s"
                  % (m, b["mae"], UNIT[m], b["rmse"], b["bias"], b["r2"]), flush=True)


if __name__ == "__main__":
    main()
