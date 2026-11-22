# -*- coding: utf-8 -*-
"""
第六轮：两个更"重"的方法

1. regkrig -- 回归克里金（Regression Kriging / Kriging with External Drift）
   Hengl 2007；Odeh 1995。做法：
     step1 用「邻站距平 + 距离 + 海拔差 + 纬度 + 日相位」拟合回归（梯度提升树）
     step2 对回归残差做普通克里金，把残差插值结果加回去
   文献里普遍报告它优于纯克里金与纯回归，因为它同时吃"趋势"和"空间结构"。

2. multi -- 多指标联合空间插值
   利用物理相关性：气温(avg/max/min) 强相关、湿度与降水相关、日照与湿度负相关。
   做法：把目标指标 + 其余 6 个指标在邻站的距平都作为特征，做梯度提升树。
   这就是 missForest 的空间版（用其他变量补目标变量）。

同时复现 clima / krig_anom 作为对照。输出 repair_validation6.json。

用法：
    python validate_repair6.py
    set SMOKE_ONLY=1 && python validate_repair6.py
"""
import os
import sys
import json
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import validate_repair as V1
import validate_repair3 as V3

from sklearn.ensemble import GradientBoostingRegressor

METRICS = V1.METRICS
RADIUS = 500.0
K_MAX = 20
K_SPATIAL = 12
N_VALID_DAYS = 120
METHODS = ["clima", "krig_anom", "regkrig", "multi"]


def paired(dict_a, dict_b, n_boot=4000):
    keys = sorted(set(dict_a) & set(dict_b))
    if len(keys) < 30:
        return None
    ea = np.array([dict_a[k] for k in keys], dtype=float)
    eb = np.array([dict_b[k] for k in keys], dtype=float)
    mae_a, mae_b = float(np.mean(np.abs(ea))), float(np.mean(np.abs(eb)))
    d = np.abs(eb) - np.abs(ea)
    rng = np.random.default_rng(7)
    boots = [float(np.mean(d[rng.integers(0, len(d), len(d))])) for _ in range(n_boot)]
    lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    return {"n": len(keys), "mae_a": round(mae_a, 4), "mae_b": round(mae_b, 4),
            "delta": round(mae_b - mae_a, 4), "ci95": [round(lo, 4), round(hi, 4)],
            "significant": bool(lo > 0 or hi < 0),
            "b_better_share": round(float(np.mean(d < 0)), 3)}


def make_feats(Anb_t, dist, alt_diff, lat_t, lon_t, doy):
    """
    回归特征矩阵（不会产生 NaN —— 这是关键，任何 NaN 都会让整行被丢掉）。
    Anb_t: (n, kk) 邻站距平，NaN 用 0 填充并单独给出「有效邻站数」特征。
    """
    kk = Anb_t.shape[1]
    valid = np.isfinite(Anb_t)
    n_valid = valid.sum(axis=1).astype(float)
    Anb_f = np.where(valid, Anb_t, 0.0)
    denom = np.maximum(n_valid, 1.0)
    mean_anom = Anb_f.sum(axis=1) / denom
    near_anom = Anb_f[:, 0]
    far_anom = Anb_f[:, -1] if kk > 1 else near_anom
    spread = np.sqrt(np.maximum((Anb_f ** 2).sum(axis=1) / denom - mean_anom ** 2, 0.0))
    # 距离加权均值（权重只在有效邻站上归一，已由 Anb_f 的 0 填充隐式处理）
    w = 1.0 / np.maximum(dist[:kk], 0.1) ** 2
    wsum = (valid * w).sum(axis=1)
    wmean = np.where(wsum > 1e-9, (Anb_f * w).sum(axis=1) / np.maximum(wsum, 1e-9), mean_anom)
    sin_d, cos_d = np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)
    n = len(Anb_t)
    cols = [mean_anom, wmean, near_anom, far_anom, spread, n_valid,
            np.full(n, lat_t), np.full(n, lon_t), sin_d, cos_d,
            np.full(n, float(np.mean(alt_diff)))]
    return np.column_stack(cols + [Anb_f])


def main():
    t0 = time.time()
    con = V1._con()
    nreg = int(os.environ.get("SMOKE_N", "3"))
    targets = V1.pick_targets(con, n_per_region=nreg)
    if os.environ.get("SMOKE_ONLY"):
        targets = targets.head(2)
    print("目标站 %d 个 | 方法 %s" % (len(targets), METHODS), flush=True)

    st_path = os.path.join(HERE, "db_mirror", "mysql", "weather_station_info.parquet").replace("\\", "/")
    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s') WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % st_path).df()
    alt_map = {int(k): (None if pd.isna(v) else float(v))
               for k, v in zip(all_st["station_id"], all_st["altitude"])}

    store = {m: {meth: {} for meth in METHODS} for m in METRICS}
    skipped = []

    for ti, (_, t) in enumerate(targets.iterrows()):
        sid = int(t["station_id"])
        da = V1.haversine_km(t["latitude"], t["longitude"],
                             all_st["latitude"].values, all_st["longitude"].values)
        nb = all_st.assign(dist=da).query(
            "station_id != %d and dist <= %f" % (sid, RADIUS)).sort_values("dist").head(K_MAX)
        if len(nb) < 5:
            skipped.append((sid, "邻站不足"))
            continue
        nb_ids = [int(x) for x in nb["station_id"]]
        df = V1.load_station_daily(con, [sid] + nb_ids, V1.TRAIN_YEARS[0], V1.VAL_YEARS[1])
        if df.empty:
            skipped.append((sid, "无数据"))
            continue

        clim_all = V3.build_clim(df, METRICS)
        lat_s = {sid: float(t["latitude"])}
        lon_s = {sid: float(t["longitude"])}
        for _, r in nb.iterrows():
            lat_s[int(r["station_id"])] = float(r["latitude"])
            lon_s[int(r["station_id"])] = float(r["longitude"])
        pos_ids = [sid] + nb_ids
        D, lat_v, lon_v = V3.pair_distances(pos_ids, lat_s, lon_s)
        alt_t = alt_map.get(sid)

        # 所有指标的先 pivot 出来，multi 方法要用
        pivs = {}
        for m in METRICS:
            pv = df.pivot_table(index="date", columns="station_id", values=m, aggfunc="first")
            if sid in pv.columns:
                pivs[m] = pv
        idx_common = None
        for m in pivs:
            idx_common = pivs[m].index if idx_common is None else idx_common.union(pivs[m].index)
        idx_common = idx_common.sort_values()

        for m in METRICS:
            if m not in pivs:
                continue
            p = pivs[m].reindex(idx_common)
            dts_m = pd.to_datetime(p.index)
            mo_m = dts_m.month.values
            yr_m = dts_m.year.values
            doy_m = dts_m.dayofyear.values.astype(float)
            clim = clim_all[m]

            have = set(int(c) for c in pivs[m].columns if pivs[m][c].notna().any())
            jkeep = [0] + [j for j in range(1, len(pos_ids)) if pos_ids[j] in have]
            if len(jkeep) < 5:
                continue
            pos_m = [pos_ids[j] for j in jkeep]
            nb_m = pos_m[1:]
            D_m = D[np.ix_(jkeep, jkeep)]
            Dsub, d0 = D_m[1:, 1:], D_m[0, 1:]
            dist_m = np.array([float(nb.loc[nb["station_id"] == c, "dist"].iloc[0]) for c in nb_m])
            alt_n_m = np.array([alt_map.get(c) if alt_map.get(c) is not None else np.nan for c in nb_m])
            alt_diff = np.where(np.isnan(alt_n_m), 0.0, alt_n_m) - (alt_t if alt_t is not None else 0.0)

            M = p[pos_m].values.astype(float)
            tv = M[:, 0]
            nb_ok = np.isfinite(M[:, 1:]).sum(axis=1) >= 2
            okv = np.isfinite(tv) & nb_ok
            tr_idx = np.where(okv & (yr_m >= V1.TRAIN_YEARS[0]) & (yr_m <= V1.TRAIN_YEARS[1]))[0]
            va_idx = np.where(okv & (yr_m >= V1.VAL_YEARS[0]) & (yr_m <= V1.VAL_YEARS[1]))[0]
            if len(tr_idx) < 200 or len(va_idx) < 40:
                continue
            if len(va_idx) > N_VALID_DAYS:
                va_idx = np.sort(V1.RNG.choice(va_idx, size=N_VALID_DAYS, replace=False))
            ytrue = tv[va_idx]

            # 距平（线性空间）
            A = V3.demean_by_clim(M, pos_m, mo_m, clim)
            A_va, A_tr = A[va_idx], A[tr_idx]
            base_va = np.array([clim.get((sid, int(mo_m[i])), np.nan) for i in va_idx])

            keys = ["%d|%s" % (sid, str(dts_m[i])[:10]) for i in va_idx]
            preds = {}

            # ---------- clima ----------
            w_idw = 1.0 / np.maximum(dist_m, 0.1) ** 2
            if m == "precipitation":
                base_nb = np.array([[clim.get((c, int(mo_m[i])), np.nan) for c in nb_m] for i in va_idx], float)
                Vnb = M[va_idx, 1:]
                ratio = np.clip(np.where(base_nb > 0.1, base_va[:, None] / np.where(base_nb > 0.1, base_nb, np.nan), np.nan), 0.3, 3.0)
                anom_adj = Vnb * np.where(np.isfinite(ratio), ratio, 1.0) - base_va[:, None]
            else:
                anom_adj = A_va[:, 1:]
            preds["clima"] = base_va + V3.masked_wavg(anom_adj, np.tile(w_idw, (len(va_idx), 1)))

            # ---------- krig_anom ----------
            kk = min(K_SPATIAL, len(nb_m))
            Dk, d0k = Dsub[:kk, :kk], d0[:kk]
            h, g, wt = V3.experimental_variogram(A_tr[:, 1:][:, :kk], Dk, min_pairs=30, n_bins=8)
            par = V3.fit_exponential(h, g, wt) if h is not None else None
            if par is not None:
                pa = np.full(len(va_idx), np.nan)
                for ii in range(len(va_idx)):
                    v = A_va[ii, 1:][:kk]
                    ok = np.isfinite(v)
                    if ok.sum() < 2:
                        continue
                    ii2 = np.where(ok)[0]
                    try:
                        w, mu, g0 = V3.krig_solve(Dk[np.ix_(ii2, ii2)], d0k[ii2], par)
                        pa[ii] = float(np.dot(w, v[ii2]))
                    except Exception:
                        pass
                preds["krig_anom"] = base_va + pa
            else:
                preds["krig_anom"] = preds["clima"].copy()

            # ---------- regkrig（回归克里金）----------
            # step1: 用邻站距平等特征拟合 GBR，得到「趋势项」预测
            # step2: 计算邻站自身的残差 r_j = A_j - LOO_IDW(A_j)（留一交叉，避免自证）
            # step3: 把 r_j 按 IDW 插到目标站，加回趋势项
            Xtr = make_feats(A_tr[:, 1:][:, :kk], dist_m, alt_diff,
                             lat_v[0], lon_v[0], doy_m[tr_idx])
            Xva = make_feats(A_va[:, 1:][:, :kk], dist_m, alt_diff,
                             lat_v[0], lon_v[0], doy_m[va_idx])
            ytr = A_tr[:, 0]
            ok_tr = np.isfinite(ytr) & np.isfinite(Xtr).all(axis=1)
            ok_va = np.isfinite(Xva).all(axis=1)
            if ok_tr.sum() > 200 and ok_va.any():
                gbr = GradientBoostingRegressor(n_estimators=100, max_depth=3,
                                                learning_rate=0.08, subsample=0.8,
                                                random_state=0)
                gbr.fit(Xtr[ok_tr], ytr[ok_tr])
                drift_va = np.full(len(va_idx), np.nan)
                drift_va[ok_va] = gbr.predict(Xva[ok_va])

                # 邻站残差：对每个邻站 j，用它自己的 (k-1) 个邻居做 LOO-IDW 得基准，算残差
                # 注意：必须在「全时段」A 上构造 resid_nb，否则 va_idx（全时段下标）对不上
                Anb_all = A[:, 1:][:, :kk]                # (全天, kk)
                resid_nb = np.full_like(Anb_all, np.nan)
                w_all = 1.0 / np.maximum(dist_m[:kk], 0.1) ** 2
                for j in range(kk):
                    others = [q for q in range(kk) if q != j]
                    if not others:
                        continue
                    wj = w_all[others]
                    Vj = Anb_all[:, others]
                    est_j = V3.masked_wavg(Vj, np.tile(wj, (len(Vj), 1)))
                    resid_nb[:, j] = Anb_all[:, j] - est_j
                # 把邻站残差按 IDW 插到目标站（用验证日的邻站残差）
                Rn_va = resid_nb[va_idx]                   # (n_va, kk)
                resid_interp = V3.masked_wavg(Rn_va, np.tile(w_all, (len(va_idx), 1)))
                preds["regkrig"] = base_va + drift_va + resid_interp
            else:
                preds["regkrig"] = preds["clima"].copy()

            # ---------- multi（多指标联合） ----------
            feat_extra_tr, feat_extra_va = [], []
            for m2 in METRICS:
                if m2 == m:
                    continue
                p2 = pivs.get(m2)
                if p2 is None:
                    continue
                p2r = p2.reindex(idx_common)
                cols2 = [pos_ids[j] for j in jkeep if pos_ids[j] in p2r.columns]
                if len(cols2) < 4:
                    continue
                M2 = p2r[cols2].values.astype(float)
                clim2 = clim_all[m2]
                A2 = V3.demean_by_clim(M2, cols2, mo_m, clim2)
                # 取邻站均值作为该指标的"区域信号"
                feat_extra_tr.append(np.nanmean(A2[tr_idx, 1:], axis=1))
                feat_extra_va.append(np.nanmean(A2[va_idx, 1:], axis=1))
            if feat_extra_tr:
                Xtr2 = np.column_stack([Xtr] + feat_extra_tr)
                Xva2 = np.column_stack([Xva] + feat_extra_va)
                ok_tr2 = np.isfinite(ytr) & np.isfinite(Xtr2).all(axis=1)
                ok_va2 = np.isfinite(Xva2).all(axis=1)
                if ok_tr2.sum() > 200 and ok_va2.any():
                    gbr2 = GradientBoostingRegressor(n_estimators=120, max_depth=3,
                                                     learning_rate=0.08, subsample=0.8,
                                                     random_state=0)
                    gbr2.fit(Xtr2[ok_tr2], ytr[ok_tr2])
                    mp = np.full(len(va_idx), np.nan)
                    mp[ok_va2] = gbr2.predict(Xva2[ok_va2])
                    preds["multi"] = base_va + mp
                else:
                    preds["multi"] = preds["clima"].copy()
            else:
                preds["multi"] = preds["clima"].copy()

            lo, hi = V1.PHYS_RANGE[m]
            for meth in METHODS:
                pv = preds.get(meth)
                if pv is None:
                    continue
                pv = np.where(np.isfinite(pv), np.clip(pv, lo, hi), np.nan)
                for k, pr, tr in zip(keys, pv, ytrue):
                    if np.isfinite(pr) and np.isfinite(tr):
                        store[m][meth][k] = float(pr - tr)

        if (ti + 1) % 5 == 0 or ti == len(targets) - 1:
            print("  进度 %d/%d  %.0fs" % (ti + 1, len(targets), time.time() - t0), flush=True)

    con.close()

    report = {"config": {"n_targets": len(targets), "radius_km": RADIUS, "k_spatial": K_SPATIAL,
                         "n_valid_days": N_VALID_DAYS, "runtime_sec": round(time.time() - t0, 1),
                         "train_years": list(V1.TRAIN_YEARS), "val_years": list(V1.VAL_YEARS)},
              "metrics": {}, "paired_vs_clima": {}, "skipped": skipped}

    print("\n%-22s" % "指标" + "".join("%12s" % mm for mm in METHODS), flush=True)
    for m in METRICS:
        row, line = {}, "%-22s" % m
        for meth in METHODS:
            d = store[m][meth]
            if len(d) < 30:
                row[meth] = None
                line += "%12s" % "-"
                continue
            errs = np.abs(np.array(list(d.values())))
            row[meth] = {"n": len(d), "mae": round(float(errs.mean()), 4),
                         "rmse": round(float(np.sqrt((errs ** 2).mean())), 4),
                         "bias": round(float(np.mean(list(d.values()))), 4),
                         "p90": round(float(np.percentile(errs, 90)), 4)}
            line += "%12.4f" % row[meth]["mae"]
        report["metrics"][m] = row
        print(line, flush=True)

    print("\n=== 配对检验（相对 clima，负=更好）===", flush=True)
    for m in METRICS:
        base = store[m]["clima"]
        if len(base) < 30:
            continue
        report["paired_vs_clima"][m] = {}
        for meth in METHODS:
            if meth == "clima":
                continue
            r = paired(base, store[m][meth])
            if r is None:
                continue
            report["paired_vs_clima"][m][meth] = r
            print("%-20s %-10s Δ=%+.4f CI[%+.4f,%+.4f]%s" %
                  (m, meth, r["delta"], r["ci95"][0], r["ci95"][1],
                   "**" if r["significant"] else ""), flush=True)

    out = os.path.join(HERE, "repair_validation6.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n已写出 %s  总用时 %.0fs" % (out, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
