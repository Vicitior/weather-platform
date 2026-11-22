"""
第十三轮：own6（门控跨要素岭回归）在「部分要素可用」中间档的稳健性验证。

第十二轮只覆盖两个端头：
    single —— 6 个其它要素全部可用
    row    —— 6 个其它要素全部不可用（退回 clima）
但真实库里 12-24 那种坏日实测是「5 坏 2 好」这种中间档，从未被验证过。

本轮按「目标站该日除待修指标外还剩几个要素有效」分层（n = 0..6），
逐层比较 own6_rr 与 clima 的 MAE，回答「GATE=1 在中间档会不会放进太多噪声特征」。
clima 只用邻站同日值，不受目标站自身遮蔽影响，故每层共用同一个 clima 误差。

用法：
    python validate_repair13.py                       # 全量（每省 2 站）
    set SMOKE_ONLY=1 & python validate_repair13.py    # 冒烟 3 站
输出：repair_validation13.json / validate_repair13.out.txt
"""
import json
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import validate_repair as V1          # noqa: E402
import validate_repair8 as V8         # noqa: E402
import validate_repair9 as V9         # noqa: E402

METRICS = V1.METRICS
TRAIN_YEARS = V1.TRAIN_YEARS
VAL_YEARS = V1.VAL_YEARS
N_VALID_DAYS = 120
K_NB = 12
SEED = 42
LAYERS = [0, 1, 2, 3, 4, 5, 6]        # 其它要素可用个数
N_MET = len(METRICS)


def mask_layer(A, di, si, mi, n_other, rng):
    """
    遮蔽目标站在 di 天的 mi，并随机再遮蔽其它要素，直到只剩 n_other 个有效。
    直接原地改 A。
    """
    A[di, si, mi] = np.nan
    others = [mj for mj in range(N_MET) if mj != mi]
    live = [mj for mj in others if np.isfinite(A[di, si, mj])]
    k_drop = max(0, len(live) - int(n_other))
    if k_drop <= 0:
        return
    for mj in rng.choice(len(live), size=min(k_drop, len(live)), replace=False):
        A[di, si, int(live[int(mj)])] = np.nan


def main():
    t0 = time.time()
    print("第十三轮：own6 按「其它要素可用个数」分层稳健性验证", flush=True)
    con = V1._con()
    print("装配数据面板…", flush=True)
    dates, sid_list, st, P = V8.build_panel(con)
    con.close()

    smoke = os.environ.get("SMOKE_ONLY") == "1"
    con2 = V1._con()
    targets = V1.pick_targets(con2, n_per_region=2 if smoke else 3)
    con2.close()
    if smoke:
        targets = targets.head(3)

    pos = {s: i for i, s in enumerate(sid_list)}
    tgt_idx = [pos[int(s)] for s in targets["station_id"] if int(s) in pos]
    print("目标站 %d 个" % len(tgt_idx), flush=True)

    clim, mos, yrs = V8.climatology(P, dates, sid_list)
    A_full, work, base = V9.anomaly_cube(P, clim, mos)
    lat = st["latitude"].values.astype(float)
    lon = st["longitude"].values.astype(float)
    alt = st["altitude"].values.astype(float) if "altitude" in st else np.zeros(len(sid_list))

    tr_mask = (yrs >= TRAIN_YEARS[0]) & (yrs <= TRAIN_YEARS[1])
    va_years = (yrs >= VAL_YEARS[0]) & (yrs <= VAL_YEARS[1])
    tr_days = np.where(tr_mask)[0]

    report = {"config": {"n_targets": len(tgt_idx), "k_nb": K_NB, "layers": LAYERS,
                         "n_valid_days": N_VALID_DAYS, "ridge_alpha": V9.RIDGE_ALPHA,
                         "train_years": list(TRAIN_YEARS), "val_years": list(VAL_YEARS)},
              "results": {}, "summary": {}}

    print("\n%-18s %5s %9s %9s %9s %6s"
          % ("指标", "可用数", "clima", "own6", "变化", "n"), flush=True)
    for mi, mname in enumerate(METRICS):
        lo, hi = V1.PHYS_RANGE[mname]
        rng = np.random.default_rng(SEED)

        # 抽样：目标站 × 验证期有效日
        rows, cols, truth = [], [], []
        for si in tgt_idx:
            cand = np.where(va_years & np.isfinite(work[:, si, mi]))[0]
            if len(cand) < 40:
                continue
            pick = np.sort(rng.choice(cand, size=min(N_VALID_DAYS, len(cand)), replace=False))
            for di in pick:
                rows.append(int(di)); cols.append(int(si))
                truth.append(float(work[di, si, mi]))
        if len(rows) < 200:
            continue
        rows = np.array(rows); cols = np.array(cols)
        truth = np.asarray(truth, dtype=np.float64)

        # 邻站与距离（不随层变化）
        nb_cache, dist_cache = {}, {}
        for si in sorted(set(cols.tolist())):
            d = V1.haversine_km(lat[si], lon[si], lat, lon)
            order = np.argsort(d)
            nb_cache[si] = [int(j) for j in order if j != si and d[j] <= 500.0][:K_NB]
            dist_cache[si] = d

        # ---- clima 基线：只用邻站同日值 + 气候基准，与目标站自身遮蔽无关 ----
        pred_clima = np.full(len(rows), np.nan)
        for i, (di, si) in enumerate(zip(rows, cols)):
            nb = [j for j in nb_cache[si] if np.isfinite(A_full[di, j, mi])]
            if not nb:
                continue
            w = 1.0 / np.maximum(np.array([dist_cache[si][j] for j in nb]), 0.1) ** 2
            pred_clima[i] = base[di, si, mi] + float(np.dot(w, A_full[di, nb, mi]) / w.sum())
        mae_clima = float(np.nanmean(np.abs(V9.to_real(pred_clima, mi, lo, hi) - truth)))

        # ---- own6 模型：训练期（不遮蔽）逐站训练 ----
        models = {}
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
            models[si] = (wr, mu, sd)

        # ---- 逐层：遮蔽后再预测 ----
        res = {"clima_mae": round(mae_clima, 4), "layers": {}}
        for layer in LAYERS:
            lrng = np.random.default_rng(SEED + layer)
            A = A_full.copy()
            for di, si in zip(rows, cols):
                mask_layer(A, int(di), int(si), mi, layer, lrng)
            err = np.full(len(rows), np.nan)
            for i, (di, si) in enumerate(zip(rows, cols)):
                if si not in models:
                    continue
                wr, mu, sd = models[si]
                f, ok = V9.build_feats(A, int(di), int(si), nb_cache[si], dist_cache[si],
                                       alt, lat, lon, mos, mi)
                if not ok:
                    continue
                p = float(np.dot(wr[1:], (f - mu) / sd) + wr[0]) + base[int(di), int(si), mi]
                err[i] = V9.to_real(np.array([p]), mi, lo, hi)[0] - truth[i]
            n = int(np.isfinite(err).sum())
            if n < 20:
                res["layers"][str(layer)] = None
                continue
            mae_o = float(np.nanmean(np.abs(err)))
            res["layers"][str(layer)] = {
                "n": n, "own6_mae": round(mae_o, 4),
                "delta_pct": round((mae_o - mae_clima) / mae_clima * 100.0, 1)}
            print("%-18s %5d %9.4f %9.4f %+8.1f%% %6d"
                  % (mname, layer, mae_clima, mae_o,
                     (mae_o - mae_clima) / mae_clima * 100.0, n), flush=True)
        report["results"][mname] = res
        report["summary"][mname] = {
            "clima_mae": round(mae_clima, 4),
            **{("n%d" % k): (res["layers"][str(k)] or {}).get("delta_pct")
               for k in LAYERS}}

    report["meta_elapsed_s"] = round(time.time() - t0, 1)
    out = os.path.join(HERE, "repair_validation13.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n已写出 %s  总用时 %.0fs" % (out, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
