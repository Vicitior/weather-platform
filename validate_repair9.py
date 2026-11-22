# -*- coding: utf-8 -*-
"""
第九轮：多要素联合插补（MICE 思路）——用目标站自己的其它要素

文献综述（D:\Android PDF\文献综述_气象缺测插补方法.md）Top 5 第 2 条指出：
  现有 clima / krig / gbr / analog / dineof 全是**单变量**方法，
  Tmax/Tmin 共线、湿度-气温反相关这些跨变量约束完全没用上。
  综述两次独立指出 missForest/MICE 在相对湿度上优势最大。
  Putra et al. 2026 (Hydrology 13(11):300, 31 法 20 次 MC 交叉验证) 原文:
  "Method complexity therefore mattered for air temperature but not for precipitation."
  MICE RMSE 0.965±0.013℃ 全场最优，IDW/OK 1.383~1.445℃。

关键洞察（前八轮从未利用）：
  某站某天「气温缺了」，它当天的湿度/风速/日照/降水**通常是有的**。
  这正是 MICE 能在单站上起作用的机制——拿同一站同一时刻的其它要素当预测变量。
  前八轮所有方法（含第八轮全网低秩）都只用了「同一指标」的信息。

本轮方法（全部按站×月气候基准去趋势后建模）：
  clima    现有基线（12 邻站同指标加权距平）
  own6     clima + 目标站自己其它 6 要素距平的线性修正（最小自由度的 MICE 变体）
  own6_rr  岭回归：特征 = 目标站自己 6 要素距平 + 12 邻站 × 7 要素距平 + 距离/海拔差/日相位
  pbshade  降水专用：站间比值法 + 训练期协方差求最优权重（P-BSHADE 思路）

输出 repair_validation9.json / validate_repair9.out.txt
用法：
    python validate_repair9.py
    set SMOKE_ONLY=1 && python validate_repair9.py
"""
import os
import sys
import json
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import validate_repair as V1
import validate_repair8 as V8       # 复用 build_panel / climatology

METRICS = V1.METRICS
TRAIN_YEARS = V1.TRAIN_YEARS        # (1991, 2005)
VAL_YEARS = V1.VAL_YEARS            # (2006, 2020)
N_VALID_DAYS = 120
K_NB = 12
SEED = 42
RIDGE_ALPHA = 3.0

METHODS = ["clima", "own6", "own6_rr", "pbshade"]


# ---------------------------------------------------------------- 距平立方
def anomaly_cube(M, clim, mos):
    """返回 (A, work, base)，均为 (天, 站, 指标)。降水走 log1p 空间。"""
    work = M.astype(np.float32, copy=True)
    log_idx = [i for i, m in enumerate(METRICS) if m == "precipitation"]
    for mi in log_idx:
        work[:, :, mi] = np.log1p(np.maximum(work[:, :, mi], 0.0))
    nd, ns, nm = work.shape
    base = np.full_like(work, np.nan)
    for mo in range(1, 13):
        sel = mos == mo
        if not sel.any():
            continue
        # clim[(si, mo)] 是 7 指标值数组
        block = np.full((ns, nm), np.nan, dtype=np.float32)
        for si in range(ns):
            v = clim.get((si, mo))
            if v is not None:
                block[si] = v
        for mi in log_idx:
            block[:, mi] = np.log1p(np.maximum(block[:, mi], 0.0))
        base[sel, :, :] = block[None, :, :]
    return work - base, work, base


def to_real(v, mi, lo, hi):
    v = np.asarray(v, dtype=np.float64)
    if METRICS[mi] == "precipitation":
        v = np.expm1(np.clip(v, -10, 12))
    return np.clip(v, lo, hi)


# ---------------------------------------------------------------- 特征构造
def build_feats(A, di, si, nb, dist, alt, lat, lon, mos, mi, own_other=True):
    """
    为 (行 di, 站 si, 指标 mi) 构造特征向量（距平域）。
    返回 (feat, ok) —— ok=False 表示特征不可用。
    """
    cand = []
    # 目标站自己的其它 6 个要素的距平（MICE 的核心：同一时刻、同一站）
    if own_other:
        vals = []
        miss = 0
        for mj in range(len(METRICS)):
            if mj == mi:
                continue
            v = A[di, si, mj]
            if np.isfinite(v):
                vals.append(float(v))
            else:
                vals.append(0.0)
                miss += 1
        cand.extend(vals)
        cand.append(float(miss))
    # 邻站各要素距平 + 距离 + 海拔差
    for j in nb:
        for mj in range(len(METRICS)):
            v = A[di, j, mj]
            cand.append(float(v) if np.isfinite(v) else 0.0)
        cand.append(float(dist[j]) / 100.0)
    # 目标站坐标 + 日相位
    cand.append(float(lat[si]) / 50.0)
    cand.append(float(lon[si]) / 100.0)
    mo = int(mos[di])
    cand.append(np.sin(2 * np.pi * mo / 12.0))
    cand.append(np.cos(2 * np.pi * mo / 12.0))
    f = np.asarray(cand, dtype=np.float64)
    if not np.all(np.isfinite(f)):
        return None, False
    return f, True


def ridge_fit(X, y, alpha=RIDGE_ALPHA):
    """带截距的岭回归（闭式解），X 已标准化外部处理。"""
    n, p = X.shape
    Xa = np.hstack([np.ones((n, 1)), X])
    P = np.eye(p + 1) * alpha
    P[0, 0] = 0.0                      # 不惩罚截距
    try:
        w = np.linalg.solve(Xa.T @ Xa + P, Xa.T @ y)
    except np.linalg.LinAlgError:
        w = np.linalg.lstsq(Xa.T @ Xa + P, Xa.T @ y, rcond=None)[0]
    return w


def main():
    t0 = time.time()
    print("第九轮：多要素联合插补（MICE 思路）", flush=True)
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
    print("构造距平立方…", flush=True)
    A, work, base = anomaly_cube(M, clim, mos)
    print("  立方 %s" % (A.shape,), flush=True)

    lat = st["latitude"].values.astype(float)
    lon = st["longitude"].values.astype(float)
    alt = st["altitude"].values.astype(float) if "altitude" in st else np.zeros(len(sid_list))

    rng = np.random.default_rng(SEED)
    tr_mask = (yrs >= TRAIN_YEARS[0]) & (yrs <= TRAIN_YEARS[1])
    va_years = (yrs >= VAL_YEARS[0]) & (yrs <= VAL_YEARS[1])

    report = {"config": {"n_targets": len(tgt_idx), "n_days": len(dates), "n_stations": len(sid_list),
                         "n_valid_days": N_VALID_DAYS, "k_nb": K_NB, "ridge_alpha": RIDGE_ALPHA,
                         "train": list(TRAIN_YEARS), "val": list(VAL_YEARS)},
              "metrics": {}, "paired_vs_clima": {}, "skipped": []}

    print("\n%-20s" % "指标" + "".join("%12s" % m for m in METHODS), flush=True)
    for mi, mname in enumerate(METRICS):
        lo, hi = V1.PHYS_RANGE[mname]
        # 遮罩设计：每目标站 120 个验证日（该指标在该日必须真实存在）
        rows, cols, truth = [], [], []
        for si in tgt_idx:
            cand = np.where(va_years & np.isfinite(work[:, si, mi]))[0]
            if len(cand) < 40:
                continue
            pick = np.sort(rng.choice(cand, size=min(N_VALID_DAYS, len(cand)), replace=False))
            for di in pick:
                rows.append(int(di)); cols.append(int(si))
                truth.append(float(M[di, si, mi]))     # 真值取物理空间原值
        if len(rows) < 200:
            report["skipped"].append((mname, "验证格太少"))
            continue
        rows = np.array(rows); cols = np.array(cols)
        truth = np.array(truth, dtype=np.float64)
        print("  %s 遮罩 %d 格" % (mname, len(rows)), flush=True)

        store = {k: {} for k in METHODS}
        keys = ["%d|%s" % (sid_list[c], str(dates[r])[:10]) for r, c in zip(rows, cols)]

        # 邻站缓存
        nb_cache, dist_cache = {}, {}
        for si in sorted(set(cols.tolist())):
            d = V1.haversine_km(lat[si], lon[si], lat, lon)
            order = np.argsort(d)
            nb = [j for j in order if j != si and d[j] <= 500.0][:K_NB]
            nb_cache[si] = nb
            dist_cache[si] = d

        # ---- clima：邻站同指标加权距平 ----
        pred_clima = np.full(len(rows), np.nan)
        for i, (di, si) in enumerate(zip(rows, cols)):
            nb = [j for j in nb_cache[si] if np.isfinite(A[di, j, mi])]
            if not nb:
                continue
            w = 1.0 / np.maximum(np.array([dist_cache[si][j] for j in nb]), 0.1) ** 2
            pred_clima[i] = base[di, si, mi] + float(np.dot(w, A[di, nb, mi]) / w.sum())
        for k, pr, trp in zip(keys, to_real(pred_clima, mi, lo, hi), truth):
            if np.isfinite(pr):
                store["clima"][k] = float(pr - trp)

        # ---- own6：clima + 用自己其它 6 要素的残差修正 ----
        # 在训练期上拟合「clima 残差 ~ 目标站自己其它 6 要素距平」
        tr_days = np.where(tr_mask)[0]
        for si in sorted(set(cols.tolist())):
            nb = nb_cache[si]
            if not nb:
                continue
            # 训练期：算 clima 预测与残差
            Xs, ys = [], []
            for di in tr_days:
                if not np.isfinite(A[di, si, mi]):
                    continue
                nbk = [j for j in nb if np.isfinite(A[di, j, mi])]
                if not nbk:
                    continue
                w = 1.0 / np.maximum(np.array([dist_cache[si][j] for j in nbk]), 0.1) ** 2
                pc = base[di, si, mi] + float(np.dot(w, A[di, nbk, mi]) / w.sum())
                if not np.isfinite(pc):
                    continue
                own = []
                ok = True
                for mj in range(len(METRICS)):
                    if mj == mi:
                        continue
                    v = A[di, si, mj]
                    if np.isfinite(v):
                        own.append(float(v))
                    else:
                        own.append(0.0)
                f = np.asarray(own + [1.0 if not np.isfinite(A[di, si, mj]) else 0.0
                                      for mj in range(len(METRICS)) if mj != mi], dtype=float)
                if not np.all(np.isfinite(f)):
                    continue
                Xs.append(f)
                ys.append(float(A[di, si, mi] - (pc - base[di, si, mi])))
            if len(ys) < 300:
                continue
            Xs = np.asarray(Xs); ys = np.asarray(ys)
            mu, sd = Xs.mean(0), Xs.std(0) + 1e-6
            w6 = ridge_fit((Xs - mu) / sd, ys)
            for i, (di, si2) in enumerate(zip(rows, cols)):
                if si2 != si:
                    continue
                if not np.isfinite(pred_clima[i]):
                    continue
                own = []
                for mj in range(len(METRICS)):
                    if mj == mi:
                        continue
                    v = A[di, si, mj]
                    own.append(float(v) if np.isfinite(v) else 0.0)
                miss = [0.0 if np.isfinite(A[di, si, mj]) else 1.0
                        for mj in range(len(METRICS)) if mj != mi]
                f = np.asarray(own + miss, dtype=float)
                if not np.all(np.isfinite(f)):
                    continue
                corr = float(np.dot(w6[1:], (f - mu) / sd) + w6[0])
                store["own6"][keys[i]] = float(
                    to_real(np.array([pred_clima[i] + corr]), mi, lo, hi)[0] - truth[i])

        # ---- own6_rr：全特征岭回归（自己 6 要素 + 邻站 7 要素 + 地理/相位）----
        for si in sorted(set(cols.tolist())):
            nb = nb_cache[si]
            if not nb:
                continue
            Xs, ys = [], []
            for di in tr_days:
                if not np.isfinite(A[di, si, mi]):
                    continue
                f, ok = build_feats(A, di, si, nb, dist_cache[si], alt, lat, lon, mos, mi)
                if not ok:
                    continue
                Xs.append(f)
                ys.append(float(A[di, si, mi]))
            if len(ys) < 300:
                continue
            Xs = np.asarray(Xs); ys = np.asarray(ys)
            mu, sd = Xs.mean(0), Xs.std(0) + 1e-6
            wr = ridge_fit((Xs - mu) / sd, ys)
            for i, (di, si2) in enumerate(zip(rows, cols)):
                if si2 != si:
                    continue
                f, ok = build_feats(A, di, si, nb, dist_cache[si], alt, lat, lon, mos, mi)
                if not ok:
                    continue
                p = float(np.dot(wr[1:], (f - mu) / sd) + wr[0]) + base[di, si, mi]
                store["own6_rr"][keys[i]] = float(to_real(np.array([p]), mi, lo, hi)[0] - truth[i])

        # ---- pbshade：降水/非负要素用站间比值 + 训练期比值方差权重 ----
        if mname == "precipitation":
            for si in sorted(set(cols.tolist())):
                nb = nb_cache[si]
                if not nb:
                    continue
                # 训练期：各邻站与目标站的比值均值与方差
                ratio_mu, ratio_var = {}, {}
                for j in nb:
                    rs = []
                    for di in tr_days:
                        a = M[di, si, mi]; b = M[di, j, mi]
                        if np.isfinite(a) and np.isfinite(b) and b > 0.2:
                            rs.append(a / b)
                    if len(rs) >= 100:
                        ratio_mu[j] = float(np.mean(rs))
                        ratio_var[j] = float(np.var(rs)) + 1e-6
                if not ratio_mu:
                    continue
                for i, (di, si2) in enumerate(zip(rows, cols)):
                    if si2 != si:
                        continue
                    num = den = 0.0
                    for j, rm in ratio_mu.items():
                        vb = M[di, j, mi]
                        if not np.isfinite(vb):
                            continue
                        wj = 1.0 / ratio_var[j]
                        num += wj * rm * float(vb)
                        den += wj
                    if den <= 0:
                        continue
                    p = num / den
                    store["pbshade"][keys[i]] = float(np.clip(p, lo, hi) - truth[i])

        # ---- 汇总 MAE ----
        mae = {}
        for k in METHODS:
            e = np.array([store[k][x] for x in keys if x in store[k]], dtype=float)
            mae[k] = float(np.mean(np.abs(e))) if len(e) else float("nan")
        report["metrics"][mname] = {"mae": mae,
                                    "n": {k: len(store[k]) for k in METHODS},
                                    "truth_n": int(len(truth))}
        print("%-20s" % mname + "".join("%12.4f" % mae[k] for k in METHODS), flush=True)

    print("\n总用时 %.0fs" % (time.time() - t0), flush=True)
    out = os.path.join(HERE, "repair_validation9.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("已写出 %s" % out, flush=True)


if __name__ == "__main__":
    main()
