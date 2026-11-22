# -*- coding: utf-8 -*-
"""
第十轮：遮蔽模式敏感性 —— 第九轮 own6_rr 的提升是否真实？

**为什么必须做这一轮（验证方法的可信度问题）**：
第九轮 own6_rr 把气温 MAE 从 0.866 打到 0.378（改善 56%），极其亮眼。
但它的核心特征是「目标站自己其它 6 个要素当天的距平」。第九轮的遮蔽设计是
**只遮蔽 (目标站, 验证日, 该指标) 这一个格子**，同一站同一天其它 6 个要素还有真实观测。

而真实数据里的坏值形态（前面轮次已实测）：
  - 漠河 1124 2020-12-24：avg_temperature / max / min / avg_humidity / avg_wind_speed
    **五个指标同时是 999999/99999 哨兵值**
  - 2020-07-03：日照 999998 单独坏（全区域同日邻站日照都是 32766）
  - 1129 站：**整段停更 8 个月**（不是零星坏值）

也就是说：真实场景里「同一站同一天其它要素可用」这种情况只占一部分。
如果大部分坏值是整行同时坏，那 own6 的优势就是**虚假的**——它靠的是
在这些坏值场景里根本拿不到的输入。

本轮设计三种遮蔽模式，同一套方法各跑一遍：
  single  只遮蔽目标指标（第九轮做法，对应「单指标坏、其它要素好」的乐观场景）
  row     同日 7 个指标全部遮蔽（对应哨兵值把整行污染的真实场景）
  block   连续 5 天整行遮蔽（对应短时停更）

对照方法：
  clima    邻站同指标加权距平（不依赖本站其它要素）
  own6     目标站自己其它要素的线性修正
  own6_rr  全特征岭回归（自己 6 要素 + 12 邻站 × 7 要素 + 地理）

若 own6_rr 在 row/block 模式下退化为 clima 或更差，说明这一轮的提升
**只在单指标缺失场景成立**，落地时必须按缺失形态分流，不能无条件宣称。

输出 repair_validation10.json / validate_repair10.out.txt
用法：
    python validate_repair10.py
    set SMOKE_ONLY=1 && python validate_repair10.py
"""
import os
import sys
import json
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import validate_repair as V1
import validate_repair8 as V8
import validate_repair9 as V9

METRICS = V1.METRICS
TRAIN_YEARS = V1.TRAIN_YEARS
VAL_YEARS = V1.VAL_YEARS
N_VALID_DAYS = 120
K_NB = 12
SEED = 42
MODES = ["single", "row", "block"]
BLOCK_DAYS = 5
METHODS = ["clima", "own6", "own6_rr"]


def mask_cells_for(mode, days, si, mi_list, rng):
    """按遮蔽模式返回要被遮蔽的 (day, metric) 列表。"""
    out = []
    if mode == "single":
        for di in days:
            out.append((int(di), int(mi_list[0])))
    elif mode == "row":
        for di in days:
            for mj in range(len(METRICS)):
                out.append((int(di), int(mj)))
    elif mode == "block":
        dset = set()
        for di in days:
            for k in range(BLOCK_DAYS):
                dset.add(int(di) + k)
        for di in sorted(dset):
            for mj in range(len(METRICS)):
                out.append((di, int(mj)))
    return out


def main():
    t0 = time.time()
    print("第十轮：遮蔽模式敏感性（own6_rr 的提升是真的吗）", flush=True)
    con = V1._con()
    print("装配数据面板…", flush=True)
    dates, sid_list, st, M = V8.build_panel(con)
    con.close()

    nreg = 2 if os.environ.get("SMOKE_ONLY") else 3
    from validate_repair import pick_targets
    con2 = V1._con()
    targets = pick_targets(con2, n_per_region=nreg)
    con2.close()
    if os.environ.get("SMOKE_ONLY"):
        targets = targets.head(3)

    pos = {s: i for i, s in enumerate(sid_list)}
    tgt_idx = [pos[int(s)] for s in targets["station_id"] if int(s) in pos]
    print("目标站 %d 个（面板内 %d）" % (len(targets), len(tgt_idx)), flush=True)

    clim, mos, yrs = V8.climatology(M, dates, sid_list)
    A_full, work, base = V9.anomaly_cube(M, clim, mos)
    lat = st["latitude"].values.astype(float)
    lon = st["longitude"].values.astype(float)
    alt = st["altitude"].values.astype(float) if "altitude" in st else np.zeros(len(sid_list))

    tr_mask = (yrs >= TRAIN_YEARS[0]) & (yrs <= TRAIN_YEARS[1])
    va_years = (yrs >= VAL_YEARS[0]) & (yrs <= VAL_YEARS[1])
    tr_days = np.where(tr_mask)[0]

    report = {"config": {"n_targets": len(tgt_idx), "n_stations": len(sid_list),
                         "n_valid_days": N_VALID_DAYS, "k_nb": K_NB,
                         "block_days": BLOCK_DAYS, "modes": MODES},
              "results": {}, "skipped": []}

    print("\n%-8s %-18s" % ("模式", "指标") + "".join("%12s" % m for m in METHODS), flush=True)
    for mode in MODES:
        rng = np.random.default_rng(SEED)
        res = {}
        for mi, mname in enumerate(METRICS):
            lo, hi = V1.PHYS_RANGE[mname]

            # 选定目标站与验证日（该指标必须真实存在）
            rows, cols, truth = [], [], []
            for si in tgt_idx:
                cand = np.where(va_years & np.isfinite(work[:, si, mi]))[0]
                if len(cand) < 40:
                    continue
                pick = np.sort(rng.choice(cand, size=min(N_VALID_DAYS, len(cand)), replace=False))
                for di in pick:
                    rows.append(int(di)); cols.append(int(si))
                    truth.append(float(M[di, si, mi]))
            if len(rows) < 200:
                report["skipped"].append((mode, mname, "验证格太少"))
                continue
            rows = np.array(rows); cols = np.array(cols)
            truth = np.array(truth, dtype=np.float64)
            keys = ["%d|%s" % (sid_list[c], str(dates[r])[:10]) for r, c in zip(rows, cols)]

            # ---- 按模式构造 A（被遮蔽的格子置 NaN）----
            A = A_full.copy()
            masked = set()
            for si in sorted(set(cols.tolist())):
                ds = rows[cols == si]
                for di, mj in mask_cells_for(mode, ds, si, [mi], rng):
                    if 0 <= di < A.shape[0]:
                        A[di, si, mj] = np.nan
                        masked.add((di, si, mj))
            print("  [%s] %s 遮蔽 %d 格（目标验证格 %d）"
                  % (mode, mname, len(masked), len(rows)), flush=True)

            store = {k: {} for k in METHODS}
            nb_cache, dist_cache = {}, {}
            for si in sorted(set(cols.tolist())):
                d = V1.haversine_km(lat[si], lon[si], lat, lon)
                order = np.argsort(d)
                nb_cache[si] = [j for j in order if j != si and d[j] <= 500.0][:K_NB]
                dist_cache[si] = d

            # ---- clima ----
            pred_clima = np.full(len(rows), np.nan)
            for i, (di, si) in enumerate(zip(rows, cols)):
                nb = [j for j in nb_cache[si] if np.isfinite(A[di, j, mi])]
                if not nb:
                    continue
                w = 1.0 / np.maximum(np.array([dist_cache[si][j] for j in nb]), 0.1) ** 2
                pred_clima[i] = base[di, si, mi] + float(np.dot(w, A[di, nb, mi]) / w.sum())
            for k, pr, trp in zip(keys, V9.to_real(pred_clima, mi, lo, hi), truth):
                if np.isfinite(pr):
                    store["clima"][k] = float(pr - trp)

            for si in sorted(set(cols.tolist())):
                nb = nb_cache[si]
                if not nb:
                    continue
                # ---- 训练：own6 系数（clima 残差 ~ 自己其它要素距平）----
                Xs, ys = [], []
                for di in tr_days:
                    if not np.isfinite(A_full[di, si, mi]):
                        continue
                    nbk = [j for j in nb if np.isfinite(A_full[di, j, mi])]
                    if not nbk:
                        continue
                    w = 1.0 / np.maximum(np.array([dist_cache[si][j] for j in nbk]), 0.1) ** 2
                    pc = base[di, si, mi] + float(np.dot(w, A_full[di, nbk, mi]) / w.sum())
                    if not np.isfinite(pc):
                        continue
                    own = [float(A_full[di, si, mj]) if np.isfinite(A_full[di, si, mj]) else 0.0
                           for mj in range(len(METRICS)) if mj != mi]
                    miss = [0.0 if np.isfinite(A_full[di, si, mj]) else 1.0
                            for mj in range(len(METRICS)) if mj != mi]
                    f = np.asarray(own + miss, dtype=float)
                    Xs.append(f)
                    ys.append(float(A_full[di, si, mi] - (pc - base[di, si, mi])))
                if len(ys) >= 300:
                    Xs = np.asarray(Xs); ys = np.asarray(ys)
                    mu6, sd6 = Xs.mean(0), Xs.std(0) + 1e-6
                    w6 = V9.ridge_fit((Xs - mu6) / sd6, ys)
                    for i, (di, si2) in enumerate(zip(rows, cols)):
                        if si2 != si or not np.isfinite(pred_clima[i]):
                            continue
                        own = [float(A[di, si, mj]) if np.isfinite(A[di, si, mj]) else 0.0
                               for mj in range(len(METRICS)) if mj != mi]
                        miss = [0.0 if np.isfinite(A[di, si, mj]) else 1.0
                                for mj in range(len(METRICS)) if mj != mi]
                        f = np.asarray(own + miss, dtype=float)
                        corr = float(np.dot(w6[1:], (f - mu6) / sd6) + w6[0])
                        store["own6"][keys[i]] = float(
                            V9.to_real(np.array([pred_clima[i] + corr]), mi, lo, hi)[0] - truth[i])

                # ---- 训练：own6_rr 全特征岭回归 ----
                Xs2, ys2 = [], []
                for di in tr_days:
                    if not np.isfinite(A_full[di, si, mi]):
                        continue
                    f, ok = V9.build_feats(A_full, di, si, nb, dist_cache[si], alt, lat, lon, mos, mi)
                    if not ok:
                        continue
                    Xs2.append(f)
                    ys2.append(float(A_full[di, si, mi]))
                if len(ys2) >= 300:
                    Xs2 = np.asarray(Xs2); ys2 = np.asarray(ys2)
                    mu2, sd2 = Xs2.mean(0), Xs2.std(0) + 1e-6
                    w2 = V9.ridge_fit((Xs2 - mu2) / sd2, ys2)
                    for i, (di, si2) in enumerate(zip(rows, cols)):
                        if si2 != si:
                            continue
                        f, ok = V9.build_feats(A, di, si, nb, dist_cache[si], alt, lat, lon, mos, mi)
                        if not ok:
                            continue
                        p = float(np.dot(w2[1:], (f - mu2) / sd2) + w2[0]) + base[di, si, mi]
                        store["own6_rr"][keys[i]] = float(
                            V9.to_real(np.array([p]), mi, lo, hi)[0] - truth[i])

            mae = {}
            for k in METHODS:
                e = np.array([store[k][x] for x in keys if x in store[k]], dtype=float)
                mae[k] = float(np.mean(np.abs(e))) if len(e) else float("nan")
            res[mname] = {"mae": mae, "n": {k: len(store[k]) for k in METHODS},
                          "masked_cells": len(masked), "truth_n": int(len(truth))}
            print("%-8s %-18s" % (mode, mname) + "".join("%12.4f" % mae[k] for k in METHODS),
                  flush=True)
        report["results"][mode] = res

    # ---- 结论摘要 ----
    print("\n=== 第九轮提升在各遮蔽模式下的保持情况（own6_rr vs clima，负=更好）===", flush=True)
    summary = {}
    for mname in METRICS:
        row = {}
        for mode in MODES:
            r = report["results"].get(mode, {}).get(mname)
            if not r:
                continue
            a = r["mae"]["clima"]; b = r["mae"]["own6_rr"]
            if np.isfinite(a) and np.isfinite(b):
                row[mode] = {"clima": round(a, 4), "own6_rr": round(b, 4),
                             "delta": round(b - a, 4),
                             "rel": round((b - a) / a * 100, 1) if a else None}
        summary[mname] = row
        cells = "  ".join("%s %+.1f%%" % (mo, row[mo]["rel"]) if mo in row and row[mo]["rel"] is not None
                          else "%s n/a" % mo for mo in MODES)
        print("%-20s %s" % (mname, cells), flush=True)
    report["summary"] = summary

    print("\n总用时 %.0fs" % (time.time() - t0), flush=True)
    out = os.path.join(HERE, "repair_validation10.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("已写出 %s" % out, flush=True)


if __name__ == "__main__":
    main()
