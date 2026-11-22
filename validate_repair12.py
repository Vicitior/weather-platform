# -*- coding: utf-8 -*-
"""
第十二轮：门控混合（own6_rr ↔ clima 自动切换）——能不能既吃跨要素红利又不崩

**前情（两轮的关键矛盾）**：
  第九轮 single 遮蔽：own6_rr 气温 MAE 0.378 vs clima 0.866（**-56%**）→ 跨要素极有价值
  第十轮 row 遮蔽：  own6_rr 气温 MAE 1.603 vs clima 0.866（**+85%**）→ 特征全缺时反而崩
  第十一轮真实普查：真实库里坏格 95.09%~99.72% 同行有 ≥1 个好要素；
                    同行全坏的只占 4.91%(气温)/3.66%(最高温)/3.24%(湿度)/0.53%(风速)/0.28%(日照)

  也就是说：真实场景**大部分**落在 own6_rr 擅长的区间，但**有一小部分**落在它崩的区间。
  直接无条件换 own6_rr 是危险的（那一小部分会被修得比 clima 差一倍）。

**本轮要验证的门控**：
  只用「目标站自己其它要素」中**有效个数 ≥ GATE** 时才走 own6_rr，否则退回 clima。
  GATE 扫描 1/2/3/4/5/6（最大 6 = 要求除目标指标外全部可用）。
  三种遮蔽模式各跑一遍，看门控能不能做到「single 保留大部分红利 + row/block 不劣于 clima」。

方法：
  clima          邻站同指标加权气候距平（永远可用的兜底）
  rr_gated{G}    own6_rr 但要求 ≥G 个自己其它要素有效，否则用 clima

输出 repair_validation12.json / validate_repair12.out.txt
用法：
    python validate_repair12.py
    set SMOKE_ONLY=1 && python validate_repair12.py
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
GATES = [1, 2, 3, 4, 5, 6]
METHODS = ["clima"] + ["rr_g%d" % g for g in GATES]


def mask_cells_for(mode, days, mi):
    out = []
    if mode == "single":
        for di in days:
            out.append((int(di), int(mi)))
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


def n_other_valid(A, di, si, mi):
    """目标站该日除 mi 外还有几个指标有效。"""
    c = 0
    for mj in range(len(METRICS)):
        if mj == mi:
            continue
        if np.isfinite(A[di, si, mj]):
            c += 1
    return c


def main():
    t0 = time.time()
    print("第十二轮：门控混合 own6_rr ↔ clima", flush=True)
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
    print("目标站 %d 个" % len(tgt_idx), flush=True)

    clim, mos, yrs = V8.climatology(M, dates, sid_list)
    A_full, work, base = V9.anomaly_cube(M, clim, mos)
    lat = st["latitude"].values.astype(float)
    lon = st["longitude"].values.astype(float)
    alt = st["altitude"].values.astype(float) if "altitude" in st else np.zeros(len(sid_list))

    tr_mask = (yrs >= TRAIN_YEARS[0]) & (yrs <= VAL_YEARS[1])
    tr_mask = (yrs >= TRAIN_YEARS[0]) & (yrs <= TRAIN_YEARS[1])
    va_years = (yrs >= VAL_YEARS[0]) & (yrs <= VAL_YEARS[1])
    tr_days = np.where(tr_mask)[0]

    report = {"config": {"n_targets": len(tgt_idx), "k_nb": K_NB, "gates": GATES,
                         "modes": MODES, "block_days": BLOCK_DAYS},
              "results": {}, "summary": {}}

    print("\n%-7s %-17s" % ("模式", "指标") + "".join("%10s" % m for m in METHODS), flush=True)
    for mode in MODES:
        rng = np.random.default_rng(SEED)
        res = {}
        for mi, mname in enumerate(METRICS):
            lo, hi = V1.PHYS_RANGE[mname]
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
                continue
            rows = np.array(rows); cols = np.array(cols)
            truth = np.array(truth, dtype=np.float64)
            keys = ["%d|%s" % (sid_list[c], str(dates[r])[:10]) for r, c in zip(rows, cols)]

            A = A_full.copy()
            for si in sorted(set(cols.tolist())):
                ds = rows[cols == si]
                for di, mj in mask_cells_for(mode, ds, mi):
                    if 0 <= di < A.shape[0]:
                        A[di, si, mj] = np.nan

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
            err_clima = np.full(len(rows), np.nan)
            pv = V9.to_real(pred_clima, mi, lo, hi)
            for i in range(len(rows)):
                if np.isfinite(pv[i]):
                    err_clima[i] = pv[i] - truth[i]

            # ---- own6_rr 预测 + 门控 ----
            err_rr = np.full(len(rows), np.nan)
            for si in sorted(set(cols.tolist())):
                nb = nb_cache[si]
                if not nb:
                    continue
                Xs, ys = [], []
                for di in tr_days:
                    if not np.isfinite(A_full[di, si, mi]):
                        continue
                    f, ok = V9.build_feats(A_full, di, si, nb, dist_cache[si], alt, lat, lon, mos, mi)
                    if not ok:
                        continue
                    Xs.append(f); ys.append(float(A_full[di, si, mi]))
                if len(ys) < 300:
                    continue
                Xs = np.asarray(Xs); ys = np.asarray(ys)
                mu, sd = Xs.mean(0), Xs.std(0) + 1e-6
                wr = V9.ridge_fit((Xs - mu) / sd, ys)
                for i, (di, si2) in enumerate(zip(rows, cols)):
                    if si2 != si:
                        continue
                    f, ok = V9.build_feats(A, di, si, nb, dist_cache[si], alt, lat, lon, mos, mi)
                    if not ok:
                        continue
                    p = float(np.dot(wr[1:], (f - mu) / sd) + wr[0]) + base[di, si, mi]
                    err_rr[i] = V9.to_real(np.array([p]), mi, lo, hi)[0] - truth[i]

            mae = {"clima": float(np.nanmean(np.abs(err_clima))) if np.isfinite(err_clima).any() else float("nan")}
            for g in GATES:
                e = np.full(len(rows), np.nan)
                for i, (di, si) in enumerate(zip(rows, cols)):
                    nv = n_other_valid(A, di, si, mi)
                    e[i] = err_rr[i] if (nv >= g and np.isfinite(err_rr[i])) else err_clima[i]
                mae["rr_g%d" % g] = float(np.nanmean(np.abs(e))) if np.isfinite(e).any() else float("nan")
            res[mname] = {"mae": mae, "truth_n": int(len(truth))}
            print("%-7s %-17s" % (mode, mname) + "".join("%10.4f" % mae[k] for k in METHODS),
                  flush=True)
        report["results"][mode] = res

    # ---- 摘要：各门控相对 clima 的变化 ----
    print("\n=== 门控方案相对 clima 的变化（负=更好，百分比）===", flush=True)
    summ = {}
    for mname in METRICS:
        row = {}
        for mode in MODES:
            r = report["results"].get(mode, {}).get(mname)
            if not r:
                continue
            base_ = r["mae"]["clima"]
            row[mode] = {k: (round((r["mae"][k] - base_) / base_ * 100, 1)
                             if base_ and np.isfinite(r["mae"][k]) else None)
                         for k in METHODS}
        summ[mname] = row
    for mname, row in summ.items():
        print("\n%s" % mname, flush=True)
        for mode in MODES:
            if mode not in row:
                continue
            cells = "  ".join("%s=%+.1f%%" % (k, row[mode][k]) if row[mode][k] is not None else "%s=n/a" % k
                              for k in METHODS)
            print("  %-7s %s" % (mode, cells), flush=True)
    report["summary"] = summ

    print("\n总用时 %.0fs" % (time.time() - t0), flush=True)
    out = os.path.join(HERE, "repair_validation12.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("已写出 %s" % out, flush=True)


if __name__ == "__main__":
    main()
