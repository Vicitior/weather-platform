# -*- coding: utf-8 -*-
"""
第七轮：时空克里金的配置调优（第五轮发现它最有效，这轮把它调到最好）

第五轮结论：krig_st（乘积模型时空克里金）在气温和湿度上都是最好的，
比 clima 好 6-8% 且统计显著。这轮回答三个问题：

Q1 时间维该用多少天？   N_TEMPORAL = 1 / 3 / 5 / 7 / 10
Q2 空间维该用几个站？   K_SPATIAL  = 4 / 8 / 12 / 20
Q3 时间结构怎么估？
    - global : 用目标站全训练期的经验时间变差函数（第五轮做法）
    - monthly: 按月份分别估（冬夏时间自相关强度差别很大）
    - fixed  : 不再拟合，直接用几个月固定值试

同时保留 clima 与 krig_anom 作对照。输出 repair_validation7.json。

用法：
    python validate_repair7.py
    set SMOKE_ONLY=1 && python validate_repair7.py
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

METRICS = V1.METRICS
RADIUS = 500.0
K_MAX = 20
N_VALID_DAYS = 120
N_ANALOG = 20

# ---- 调参网格 ----
NT_LIST = [1, 3, 5, 7, 10]          # 时间邻日跨度
KS_LIST = [4, 8, 12, 20]            # 空间邻站数

METHODS = ["clima", "krig_anom"]
for nt in NT_LIST:
    METHODS.append("st_nt%d" % nt)           # 时间跨度变化（ks=12, 全局时间变差）
    METHODS.append("sta_nt%d" % nt)          # 时间跨度变化（ks=12, 按月估时间变差）
for ks in KS_LIST:
    METHODS.append("st_k%d" % ks)            # 空间邻站数变化（nt=3）

ST_BASE_NT = 3
ST_BASE_KS = 12


def paired(dict_a, dict_b, n_boot=3000):
    keys = sorted(set(dict_a) & set(dict_b))
    if len(keys) < 50:
        return None
    ea = np.array([dict_a[k] for k in keys], dtype=float)
    eb = np.array([dict_b[k] for k in keys], dtype=float)
    mae_a, mae_b = float(np.mean(np.abs(ea))), float(np.mean(np.abs(eb)))
    d = np.abs(eb) - np.abs(ea)
    rng = np.random.default_rng(11)
    boots = [float(np.mean(d[rng.integers(0, len(d), len(d))])) for _ in range(n_boot)]
    lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    return {"n": len(keys), "mae_a": round(mae_a, 4), "mae_b": round(mae_b, 4),
            "delta": round(mae_b - mae_a, 4), "ci95": [round(lo, 4), round(hi, 4)],
            "significant": bool(lo > 0 or hi < 0),
            "b_better_share": round(float(np.mean(d < 0)), 3)}


def temporal_variogram(z, max_lag=40, min_pairs=50):
    lags, gs, ws = [], [], []
    n = len(z)
    for lag in range(1, min(max_lag, n - 1) + 1):
        a, b = z[: n - lag], z[lag:]
        m = np.isfinite(a) & np.isfinite(b)
        cnt = int(m.sum())
        if cnt < min_pairs:
            continue
        d = a[m] - b[m]
        lags.append(float(lag)); gs.append(float(0.5 * np.mean(d * d))); ws.append(float(cnt))
    if len(lags) < 3:
        return None
    return np.array(lags), np.array(gs), np.array(ws)


def fit_temporal_a(z_tr, months=None, by_month=False):
    """估时间相关长度 a_t。by_month=True 时按月分别估，返回 {month: a_t}。"""
    if not by_month:
        tv = temporal_variogram(z_tr)
        if tv is None:
            return 3.0
        p = V3.fit_exponential(tv[0], tv[1], tv[2])
        return float(np.clip(p[2], 0.5, 30.0)) if p is not None else 3.0
    out = {}
    for mo in range(1, 13):
        m = (months == mo)
        zz = z_tr[m]
        zz = zz[np.isfinite(zz)]
        tv = temporal_variogram(zz, max_lag=25)
        if tv is None:
            out[mo] = 3.0
            continue
        p = V3.fit_exponential(tv[0], tv[1], tv[2])
        out[mo] = float(np.clip(p[2], 0.5, 30.0)) if p is not None else 3.0
    return out


def krig_st_predict(A, D_sub, d0, a_s, a_t_of_day, va_idx, n_temporal, k_spatial):
    """
    乘积模型时空简单克里金。
    a_t_of_day: 每个验证日对应的时间相关长度（float 或数组）。
    """
    n_day, n_st = A.shape
    out = np.full(len(va_idx), np.nan)
    for ii, di in enumerate(va_idx):
        at = float(a_t_of_day[ii]) if hasattr(a_t_of_day, "__len__") else float(a_t_of_day)
        ph, pu, pv = [], [], []
        # 当日空间邻站
        row = A[di, 1:]
        okj = np.where(np.isfinite(row))[0]
        if okj.size:
            order = np.argsort(d0[okj])
            for j in order[:k_spatial]:
                ph.append(d0[okj[j]]); pu.append(0.0); pv.append(row[okj[j]])
        # 目标站时间邻日
        for delta in range(1, n_temporal + 1):
            for sgn in (-1, 1):
                dj = di + sgn * delta
                if 0 <= dj < n_day and np.isfinite(A[dj, 0]):
                    ph.append(0.0); pu.append(float(delta)); pv.append(A[dj, 0])
        # 前 4 个近邻站的时间邻日（±1、±2）
        if okj.size:
            for j in np.argsort(d0[okj])[:4]:
                for delta in (1, 2):
                    for sgn in (-1, 1):
                        dj = di + sgn * delta
                        if 0 <= dj < n_day and np.isfinite(A[dj, 1 + okj[j]]):
                            ph.append(d0[okj[j]]); pu.append(float(delta))
                            pv.append(A[dj, 1 + okj[j]])
        m = len(pv)
        if m < 3:
            continue
        h = np.array(ph); u = np.array(pu); v = np.array(pv)
        hh = np.abs(h[:, None] - h[None, :])
        uu = np.abs(u[:, None] - u[None, :])
        C = np.exp(-hh / a_s) * np.exp(-uu / at)
        C[np.diag_indices_from(C)] += 1e-6
        c0 = np.exp(-h / a_s) * np.exp(-u / at)
        try:
            w = np.linalg.solve(C, c0)
        except np.linalg.LinAlgError:
            w = np.linalg.lstsq(C, c0, rcond=None)[0]
        out[ii] = float(np.dot(w, v))
    return out


def main():
    t0 = time.time()
    con = V1._con()
    nreg = int(os.environ.get("SMOKE_N", "3"))
    targets = V1.pick_targets(con, n_per_region=nreg)
    if os.environ.get("SMOKE_ONLY"):
        targets = targets.head(2)
    print("目标站 %d 个 | %d 个方法" % (len(targets), len(METHODS)), flush=True)

    st_path = os.path.join(HERE, "db_mirror", "mysql", "weather_station_info.parquet").replace("\\", "/")
    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s') WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % st_path).df()

    store = {m: {meth: {} for meth in METHODS} for m in METRICS}
    at_log = {m: {"global": [], "monthly": {mo: [] for mo in range(1, 13)}} for m in METRICS}
    skipped = []

    for ti, (_, t) in enumerate(targets.iterrows()):
        sid = int(t["station_id"])
        da = V1.haversine_km(t["latitude"], t["longitude"],
                             all_st["latitude"].values, all_st["longitude"].values)
        nb = all_st.assign(dist=da).query(
            "station_id != %d and dist <= %f" % (sid, RADIUS)).sort_values("dist").head(K_MAX)
        if len(nb) < 5:
            skipped.append((sid, "邻站不足")); continue
        nb_ids = [int(x) for x in nb["station_id"]]
        df = V1.load_station_daily(con, [sid] + nb_ids, V1.TRAIN_YEARS[0], V1.VAL_YEARS[1])
        if df.empty:
            skipped.append((sid, "无数据")); continue

        clim_all = V3.build_clim(df, METRICS)
        lat_s = {sid: float(t["latitude"])}
        lon_s = {sid: float(t["longitude"])}
        for _, r in nb.iterrows():
            lat_s[int(r["station_id"])] = float(r["latitude"])
            lon_s[int(r["station_id"])] = float(r["longitude"])
        pos_ids = [sid] + nb_ids
        D, lat_v, lon_v = V3.pair_distances(pos_ids, lat_s, lon_s)

        for m in METRICS:
            piv = df.pivot_table(index="date", columns="station_id", values=m, aggfunc="first")
            if sid not in piv.columns:
                continue
            p = piv
            dts_m = pd.to_datetime(p.index)
            mo_m = dts_m.month.values
            yr_m = dts_m.year.values
            clim = clim_all[m]

            have = set(int(c) for c in p.columns if p[c].notna().any())
            jkeep = [0] + [j for j in range(1, len(pos_ids)) if pos_ids[j] in have]
            if len(jkeep) < 5:
                continue
            pos_m = [pos_ids[j] for j in jkeep]
            nb_m = pos_m[1:]
            D_m = D[np.ix_(jkeep, jkeep)]
            Dsub, d0 = D_m[1:, 1:], D_m[0, 1:]
            dist_m = np.array([float(nb.loc[nb["station_id"] == c, "dist"].iloc[0]) for c in nb_m])

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

            A = V3.demean_by_clim(M, pos_m, mo_m, clim)
            A_va, A_tr = A[va_idx], A[tr_idx]
            base_va = np.array([clim.get((sid, int(mo_m[i])), np.nan) for i in va_idx])
            keys = ["%d|%s" % (sid, str(dts_m[i])[:10]) for i in va_idx]
            preds = {}

            # ---- clima ----
            w_idw = 1.0 / np.maximum(dist_m, 0.1) ** 2
            if m == "precipitation":
                base_nb = np.array([[clim.get((c, int(mo_m[i])), np.nan) for c in nb_m] for i in va_idx], float)
                Vnb = M[va_idx, 1:]
                ratio = np.clip(np.where(base_nb > 0.1, base_va[:, None] / np.where(base_nb > 0.1, base_nb, np.nan), np.nan), 0.3, 3.0)
                anom_adj = Vnb * np.where(np.isfinite(ratio), ratio, 1.0) - base_va[:, None]
            else:
                anom_adj = A_va[:, 1:]
            preds["clima"] = base_va + V3.masked_wavg(anom_adj, np.tile(w_idw, (len(va_idx), 1)))

            # ---- krig_anom（空间，nt=0）----
            kk0 = min(12, len(nb_m))
            Dk0, d0k0 = Dsub[:kk0, :kk0], d0[:kk0]
            h0, g0, w0 = V3.experimental_variogram(A_tr[:, 1:][:, :kk0], Dk0, min_pairs=30, n_bins=8)
            par0 = V3.fit_exponential(h0, g0, w0) if h0 is not None else None
            if par0 is not None:
                a_s0 = float(min(max(par0[2], 30.0), 1.5 * float(Dk0.max())))
                pa = np.full(len(va_idx), np.nan)
                for ii in range(len(va_idx)):
                    v = A_va[ii, 1:][:kk0]
                    ok = np.isfinite(v)
                    if ok.sum() < 2:
                        continue
                    jj = np.where(ok)[0]
                    try:
                        w, mu, _ = V3.krig_solve(Dk0[np.ix_(jj, jj)], d0k0[jj], par0)
                        pa[ii] = float(np.dot(w, v[jj]))
                    except Exception:
                        pass
                preds["krig_anom"] = base_va + pa
            else:
                preds["krig_anom"] = preds["clima"].copy()

            # ---- 空间相关长度（用满 K 个邻站）----
            kk = min(K_MAX, len(nb_m))
            Dk, d0k = Dsub[:kk, :kk], d0[:kk]
            h, g, wt = V3.experimental_variogram(A_tr[:, 1:][:, :kk], Dk, min_pairs=30, n_bins=8)
            par = V3.fit_exponential(h, g, wt) if h is not None else None
            a_s = float(min(max(par[2], 30.0), 1.5 * float(Dk.max()))) if par is not None else 300.0

            # ---- 时间相关长度：全局 & 按月 ----
            a_t_global = fit_temporal_a(A_tr[:, 0], by_month=False)
            a_t_by_mo = fit_temporal_a(A_tr[:, 0], months=mo_m[tr_idx], by_month=True)
            at_log[m]["global"].append(a_t_global)
            for mo in range(1, 13):
                at_log[m]["monthly"][mo].append(a_t_by_mo[mo])

            a_t_va_global = np.full(len(va_idx), a_t_global)
            a_t_va_monthly = np.array([a_t_by_mo[int(mo_m[i])] for i in va_idx], dtype=float)

            # ---- 时间跨度扫描（全局变差, ks=12）----
            for nt in NT_LIST:
                key = "st_nt%d" % nt
                an = krig_st_predict(A, Dsub, d0, a_s, a_t_va_global, va_idx, nt, ST_BASE_KS)
                preds[key] = base_va + an
                # 按月估时间变差
                ak = "sta_nt%d" % nt
                an2 = krig_st_predict(A, Dsub, d0, a_s, a_t_va_monthly, va_idx, nt, ST_BASE_KS)
                preds[ak] = base_va + an2

            # ---- 空间站数扫描（nt=3, 全局变差）----
            for ks in KS_LIST:
                key = "st_k%d" % ks
                an = krig_st_predict(A, Dsub, d0, a_s, a_t_va_global, va_idx, ST_BASE_NT, ks)
                preds[key] = base_va + an

            lo, hi = V1.PHYS_RANGE[m]
            for meth in METHODS:
                pv = preds.get(meth)
                if pv is None:
                    continue
                pv = np.where(np.isfinite(pv), np.clip(pv, lo, hi), np.nan)
                for k, pr, tr in zip(keys, pv, ytrue):
                    if np.isfinite(pr) and np.isfinite(tr):
                        store[m][meth][k] = float(pr - tr)

        if (ti + 1) % 3 == 0 or ti == len(targets) - 1:
            print("  进度 %d/%d  %.0fs" % (ti + 1, len(targets), time.time() - t0), flush=True)

    con.close()

    report = {"config": {"n_targets": len(targets), "radius_km": RADIUS,
                         "n_valid_days": N_VALID_DAYS, "nt_list": NT_LIST, "ks_list": KS_LIST,
                         "runtime_sec": round(time.time() - t0, 1)},
              "metrics": {}, "paired_vs_clima": {}, "a_t": {}, "skipped": skipped}

    print("\n%-22s" % "指标" + "".join("%10s" % mm for mm in METHODS), flush=True)
    for m in METRICS:
        row, line = {}, "%-22s" % m
        for meth in METHODS:
            d = store[m][meth]
            if len(d) < 50:
                row[meth] = None
                line += "%10s" % "-"
                continue
            errs = np.abs(np.array(list(d.values())))
            row[meth] = {"n": len(d), "mae": round(float(errs.mean()), 4),
                         "rmse": round(float(np.sqrt((errs ** 2).mean())), 4),
                         "bias": round(float(np.mean(list(d.values()))), 4)}
            line += "%10.4f" % row[meth]["mae"]
        report["metrics"][m] = row
        print(line, flush=True)

    print("\n=== 配对检验（相对 clima，负=更好）===", flush=True)
    for m in METRICS:
        base = store[m]["clima"]
        if len(base) < 50:
            continue
        report["paired_vs_clima"][m] = {}
        for meth in METHODS:
            if meth == "clima":
                continue
            r = paired(base, store[m][meth])
            if r is None:
                continue
            report["paired_vs_clima"][m][meth] = r
            if r["significant"]:
                print("%-20s %-10s Δ=%+.4f CI[%+.4f,%+.4f]**" %
                      (m, meth, r["delta"], r["ci95"][0], r["ci95"][1]), flush=True)

    print("\n=== 时间相关长度 a_t（天，中位数）===", flush=True)
    for m in METRICS:
        g = at_log[m]["global"]
        by_mo = {mo: (round(float(np.median(v)), 2) if v else None)
                 for mo, v in at_log[m]["monthly"].items()}
        report["a_t"][m] = {"global_median": round(float(np.median(g)), 2) if g else None,
                            "monthly_median": by_mo}
        print("%-20s 全局 %.2f | 1月 %.2f 4月 %.2f 7月 %.2f 10月 %.2f" % (
            m, np.median(g) if g else float("nan"),
            by_mo[1] or float("nan"), by_mo[4] or float("nan"),
            by_mo[7] or float("nan"), by_mo[10] or float("nan")), flush=True)

    out = os.path.join(HERE, "repair_validation7.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n已写出 %s  总用时 %.0fs" % (out, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
