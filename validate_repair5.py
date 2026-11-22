# -*- coding: utf-8 -*-
"""
第五轮：文献启发的新方法 hold-out 对比

在第一~四轮基础上，加入文献里的几类主流做法：

1. dineof      -- DINEOF / 低秩时空重建（SVD 截断 + 迭代填缺）
                  Beckers & Rixen 2003；降水走 log1p 空间
2. krig_st     -- 时空克里金（乘积模型协方差 C(h,u)=C_s(h)*C_t(u)）
                  Cressie & Wikle 2011；时间维用目标站自身邻日
3. analog      -- 天气型相似日匹配（analog method）
                  Lorenz 1969；在邻站距平向量上找最相似的 m 个历史日
4. knn_st      -- 时空最近邻（邻站 ±3 天内所有站-日都算候选）
5. krig_anom   -- 距平克里金（第三轮基线，复现）
6. clima       -- 气候距平法（v0.6 起的生产默认，基线）

统一用 V1 的 hold-out 设计：59 站分层抽样，训练 1991-2005 / 验证 2006-2020，
每站每指标随机取 120 个验证日。输出 repair_validation5.json。

用法：
    python validate_repair5.py             # 全量 59 站
    set SMOKE_ONLY=1 && python validate_repair5.py   # 冒烟：2 站
"""
import os
import sys
import json
import time
import datetime as dt

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import validate_repair as V1
import validate_repair3 as V3

METRICS = V1.METRICS
RADIUS = 500.0
K_MAX = 20
K_SPATIAL = 10          # krig_st 用的空间邻站数
N_TEMPORAL = 5          # krig_st 用的时间邻日（±）
K_KRIG = 12             # krig_anom 用的邻站数
N_VALID_DAYS = 120
N_ANALOG = 20           # analog 取最相似的 m 天
N_KNN_ST = 30           # knn_st 取 30 个站-日
DINEOF_KMODES = 8
DINEOF_ITERS = 25

METHODS = ["clima", "krig_anom", "dineof", "krig_st", "analog", "knn_st"]


# ---------------------------------------------------------------- 基础工具
def working_values(v, metric):
    """把原始值变换到「新方法用的工作空间」：降水走 log1p，其余原样。"""
    if metric == "precipitation":
        return np.log1p(np.maximum(v, 0.0))
    return v


def build_clim_working(df, metric):
    """在工作空间里算 (station_id, month) -> 月气候基准。"""
    d = df[["date", "station_id", metric]].copy()
    d[metric] = working_values(d[metric].values.astype(float), metric)
    d["month"] = pd.to_datetime(d["date"]).dt.month
    g = d.groupby(["station_id", "month"], observed=True)[metric].mean()
    out = {}
    for (s, mo), v in g.items():
        if np.isfinite(v):
            out[(int(s), int(mo))] = float(v)
    return out


def svd_rank_k(F, k):
    """截断 SVD 重建。"""
    U, S, Vt = np.linalg.svd(F, full_matrices=False)
    k = max(1, min(int(k), S.size))
    return (U[:, :k] * S[:k]) @ Vt[:k, :]


def temporal_variogram(z, max_lag=30, min_pairs=50):
    """1 维时间序列的经验变差函数 γ(u)=½E[(z_t - z_{t+u})²]。"""
    lags, gs, ws = [], [], []
    n = len(z)
    for lag in range(1, min(max_lag, n - 1) + 1):
        a = z[: n - lag]
        b = z[lag:]
        m = np.isfinite(a) & np.isfinite(b)
        cnt = int(m.sum())
        if cnt < min_pairs:
            continue
        d = a[m] - b[m]
        lags.append(float(lag))
        gs.append(float(0.5 * np.mean(d * d)))
        ws.append(float(cnt))
    if len(lags) < 3:
        return None
    return np.array(lags), np.array(gs), np.array(ws)


# ---------------------------------------------------------------- 方法 1：DINEOF
def dineof_predict(M, va_idx):
    """
    低秩时空重建。M: (天, 站)，第 0 列是目标站。
    人为把目标站验证日置为缺失，然后迭代 SVD 截断重建，读回该处。
    """
    X = M.astype(float)
    mu = np.nanmean(X, axis=0)
    sd = np.nanstd(X, axis=0)
    mu = np.where(np.isfinite(mu), mu, 0.0)
    sd = np.where(np.isfinite(sd) & (sd > 1e-9), sd, 1.0)
    Z = (X - mu) / sd
    Zn = Z.copy()
    Zn[va_idx, 0] = np.nan                 # 遮蔽验证日
    obs = np.isfinite(Zn)
    if obs.sum() < 0.3 * Zn.size:
        return np.full(len(va_idx), np.nan)

    F = np.where(obs, Zn, 0.0)
    prev = None
    for _ in range(DINEOF_ITERS):
        R = svd_rank_k(F, DINEOF_KMODES)
        F = np.where(obs, Zn, R)
        cur = F[~obs]
        if prev is not None and cur.size and np.max(np.abs(cur - prev)) < 1e-6:
            break
        prev = cur
    return F[va_idx, 0] * sd[0] + mu[0]


# ---------------------------------------------------------------- 方法 2：时空克里金
def krig_st_predict(A, t_day, D_sub, d0, a_s, a_t, va_idx):
    """
    乘积模型简单克里金（距平均值≈0）：
        C(h,u) = exp(-h/a_s) * exp(-u/a_t)
    观测点 = 当日空间邻站 + 目标站时间邻日 + 邻站时间邻日。
    返回每个验证日的预测距平。
    """
    n_day, n_st = A.shape
    n_nb = n_st - 1
    out = np.full(len(va_idx), np.nan)
    for ii, di in enumerate(va_idx):
        pts_h, pts_u, pts_v = [], [], []
        # 当日空间邻站
        row = A[di, 1:]
        okj = np.where(np.isfinite(row))[0]
        if okj.size:
            order = np.argsort(d0[okj])
            for j in order[:K_SPATIAL]:
                pts_h.append(d0[okj[j]]); pts_u.append(0.0); pts_v.append(row[okj[j]])
        # 目标站的时间邻日
        for delta in range(1, N_TEMPORAL + 1):
            for sgn in (-1, 1):
                dj = di + sgn * delta
                if 0 <= dj < n_day and np.isfinite(A[dj, 0]):
                    pts_h.append(0.0); pts_u.append(float(delta)); pts_v.append(A[dj, 0])
        # 最近 4 个邻站的时间邻日
        if okj.size:
            order = np.argsort(d0[okj])[:4]
            for j in order:
                for sgn in (-1, 1):
                    dj = di + sgn
                    if 0 <= dj < n_day and np.isfinite(A[dj, 1 + okj[j]]):
                        pts_h.append(d0[okj[j]]); pts_u.append(1.0)
                        pts_v.append(A[dj, 1 + okj[j]])
        m = len(pts_v)
        if m < 3:
            continue
        h = np.array(pts_h); u = np.array(pts_u); v = np.array(pts_v)
        # 观测-观测协方差矩阵
        hh = np.abs(h[:, None] - h[None, :])
        uu = np.abs(u[:, None] - u[None, :])
        Cmat = np.exp(-hh / a_s) * np.exp(-uu / a_t)
        Cmat[np.diag_indices_from(Cmat)] += 1e-6
        # 观测-待估点（待估点 h=0, u=0）
        c0 = np.exp(-h / a_s) * np.exp(-u / a_t)
        try:
            w = np.linalg.solve(Cmat, c0)
        except np.linalg.LinAlgError:
            w = np.linalg.lstsq(Cmat, c0, rcond=None)[0]
        out[ii] = float(np.dot(w, v))
    return out


# ---------------------------------------------------------------- 方法 3：analog
def analog_predict(A, va_idx, tr_idx, m_days=N_ANALOG):
    """在邻站距平向量上找最相似的 m 个历史日，取其目标站距平加权平均。"""
    Pat = A[:, 1:]
    tgt = A[:, 0]
    Ptr = Pat[tr_idx]
    Ttr = tgt[tr_idx]
    out = np.full(len(va_idx), np.nan)
    for ii, di in enumerate(va_idx):
        p = Pat[di]
        if not np.isfinite(p).any():
            continue
        diff = Ptr - p
        dd = np.nansum(diff * diff, axis=1)
        dd = np.where(np.isfinite(dd), dd, np.inf)
        order = np.argsort(dd)[:m_days]
        vals = Ttr[order]
        ok = np.isfinite(vals)
        if not ok.any():
            continue
        w = 1.0 / (np.sqrt(dd[order][ok]) + 1.0)
        out[ii] = float(np.average(vals[ok], weights=w))
    return out


# ---------------------------------------------------------------- 方法 4：时空 kNN
def knn_st_predict(A, t_day, D_sub, d0, va_idx, k=N_KNN_ST):
    """邻站 ±3 天内所有站-日作为候选，按 (空间距离, 时间滞后) 加权取最近 k 个。"""
    n_day, n_st = A.shape
    out = np.full(len(va_idx), np.nan)
    for ii, di in enumerate(va_idx):
        cand_d, cand_v = [], []
        for delta in range(-3, 4):
            dj = di + delta
            if dj < 0 or dj >= n_day:
                continue
            w_t = 1.0 / (1.0 + abs(delta))
            row = A[dj, 1:]
            okj = np.isfinite(row)
            if not okj.any():
                continue
            # 时空距离：空间距离(km) / 100 + 时间滞后(天) * 20
            dspace = d0 / 100.0 + abs(delta) * 20.0
            cand_d.extend(dspace[okj].tolist())
            cand_v.extend(row[okj].tolist())
        if len(cand_v) < 5:
            continue
        cd = np.array(cand_d)
        cv = np.array(cand_v)
        order = np.argsort(cd)[:k]
        w = 1.0 / (cd[order] + 1.0)
        out[ii] = float(np.average(cv[order], weights=w))
    return out


# ---------------------------------------------------------------- 配对检验
def paired(dict_a, dict_b, n_boot=4000):
    """按 (station, date) 键对齐；delta = MAE_b - MAE_a（负 = b 更好）。"""
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


# ---------------------------------------------------------------- 主流程
def main():
    t_start = time.time()
    con = V1._con()
    nreg = int(os.environ.get("SMOKE_N", "3"))
    targets = V1.pick_targets(con, n_per_region=nreg)
    if os.environ.get("SMOKE_ONLY"):
        targets = targets.head(2)
    print("目标站 %d 个 | 方法 %d 个" % (len(targets), len(METHODS)), flush=True)

    st_path = os.path.join(HERE, "db_mirror", "mysql", "weather_station_info.parquet").replace("\\", "/")
    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % st_path).df()
    alt_map = {int(k): (None if pd.isna(v) else float(v))
               for k, v in zip(all_st["station_id"], all_st["altitude"])}

    # 结果容器：{metric: {method: {key: signed_error}}}
    store = {m: {meth: {} for meth in METHODS} for m in METRICS}
    extra = {m: {"a_s": [], "a_t": []} for m in METRICS}
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

        clim_all = V3.build_clim(df, METRICS)      # {metric: {(station_id, month): 均值}}
        # 降水在 log1p 空间另算一份，其它指标直接复用线性空间那份

        lat_s = {sid: float(t["latitude"])}
        lon_s = {sid: float(t["longitude"])}
        for _, r in nb.iterrows():
            lat_s[int(r["station_id"])] = float(r["latitude"])
            lon_s[int(r["station_id"])] = float(r["longitude"])
        pos_ids = [sid] + nb_ids
        D, lat_v, lon_v = V3.pair_distances(pos_ids, lat_s, lon_s)
        alt_t = alt_map.get(sid)

        for m in METRICS:
            piv = df.pivot_table(index="date", columns="station_id", values=m, aggfunc="first")
            if sid not in piv.columns:
                continue
            p = piv
            dts_m = pd.to_datetime(p.index)
            mo_m = dts_m.month.values
            yr_m = dts_m.year.values
            t_day = np.asarray((dts_m - dts_m[0]).days, dtype=float)

            have = set(int(c) for c in p.columns)
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
            if not np.isfinite(ytrue).all():
                continue
            clim = clim_all[m]      # 当前指标的气候基准 {(station_id, month): 均值}

            # ---------- 工作空间（降水 log1p，其余=线性空间） ----------
            W = working_values(M, m)
            if m == "precipitation":
                # 降水在 log1p 空间重算一份月气候基准（比值法在 log 空间等价于加性）
                sub = df[df["station_id"].isin(pos_m)]
                clim_w = build_clim_working(sub, m)
            else:
                clim_w = clim

            def base_w(stat):
                """该站在工作空间中的逐日月气候基准。"""
                return np.array([clim_w.get((stat, int(mo)), np.nan) for mo in mo_m], dtype=float)

            base_t_all_w = base_w(sid)
            base_t_va_w = base_t_all_w[va_idx]
            A_w = np.column_stack([W[:, j] - base_w(c) for j, c in enumerate(pos_m)])   # (天, 站) 距平
            Aw_va, Aw_tr = A_w[va_idx], A_w[tr_idx]

            keys = ["%d|%s" % (sid, str(dts_m[i])[:10]) for i in va_idx]
            preds = {}

            # ---------- 基线 1：clima（线性空间，降水走比值） ----------
            base_va_lin = np.array([clim.get((sid, int(mo_m[i])), np.nan) for i in va_idx])
            base_nb_lin = np.array([[clim.get((c, int(mo_m[i])), np.nan) for c in nb_m] for i in va_idx], dtype=float)
            Vnb = M[va_idx, 1:]
            w_idw = 1.0 / np.maximum(dist_m, 0.1) ** 2
            if m == "precipitation":
                ratio = np.where(base_nb_lin > 0.1, base_va_lin[:, None] / np.where(base_nb_lin > 0.1, base_nb_lin, np.nan), np.nan)
                ratio = np.clip(ratio, 0.3, 3.0)
                anom_adj = Vnb * np.where(np.isfinite(ratio), ratio, 1.0) - base_va_lin[:, None]
            else:
                anom_adj = Aw_va[:, 1:]
            preds["clima"] = base_va_lin + V3.masked_wavg(anom_adj, np.tile(w_idw, (len(va_idx), 1)))

            # ---------- 基线 2：krig_anom ----------
            kk = min(K_KRIG, len(nb_m))
            Dk, d0k = Dsub[:kk, :kk], d0[:kk]
            Ztr = Aw_tr[:, 1:][:, :kk]
            h, g, wt = V3.experimental_variogram(Ztr, Dk, min_pairs=30, n_bins=8)
            par = V3.fit_exponential(h, g, wt) if h is not None else None
            if par is not None:
                # 局部变差函数在邻站半径内常常看不到变程（气温的空间相关半径 >1000km），
                # 拟合会外推到几千公里外的无意义值。这里把 a_s 钳到"邻站最大间距的 1.5 倍"，
                # 超过这个距离本来就没有观测支撑，钳住后等价于"近线性权重"，比乱外推稳。
                a_s = float(min(max(par[2], 30.0), 1.5 * float(Dk.max())))
                pred_anom = np.full(len(va_idx), np.nan)
                for ii in range(len(va_idx)):
                    v = Aw_va[ii, 1:][:kk]
                    ok = np.isfinite(v)
                    if ok.sum() < 2:
                        continue
                    idx = np.where(ok)[0]
                    try:
                        w, mu, g0 = V3.krig_solve(Dk[np.ix_(idx, idx)], d0k[idx], par)
                        pred_anom[ii] = float(np.dot(w, v[idx]))
                    except Exception:
                        continue
                preds["krig_anom"] = base_va_lin + pred_anom
                extra[m]["a_s"].append(a_s)
            else:
                a_s = 300.0

            # ---------- 新方法 1：DINEOF ----------
            pz = dineof_predict(W, va_idx)
            preds["dineof"] = np.expm1(pz) if m == "precipitation" else pz

            # ---------- 新方法 2：krig_st（时间相关长度） ----------
            tv_t = temporal_variogram(A_w[tr_idx, 0], max_lag=30)
            if tv_t is not None:
                ptt = V3.fit_exponential(tv_t[0], tv_t[1], tv_t[2])
                a_t = float(ptt[2]) if ptt is not None else 3.0
            else:
                a_t = 3.0
            extra[m]["a_t"].append(a_t)
            st_anom = krig_st_predict(A_w, t_day, Dsub, d0, a_s, max(a_t, 0.5), va_idx)
            preds["krig_st"] = base_t_va_w + st_anom
            if m == "precipitation":
                preds["krig_st"] = np.expm1(base_t_va_w + st_anom)

            # ---------- 新方法 3：analog ----------
            an_anom = analog_predict(A_w, va_idx, tr_idx)
            preds["analog"] = base_t_va_w + an_anom
            if m == "precipitation":
                preds["analog"] = np.expm1(base_t_va_w + an_anom)

            # ---------- 新方法 4：knn_st ----------
            kn_anom = knn_st_predict(A_w, t_day, Dsub, d0, va_idx)
            preds["knn_st"] = base_t_va_w + kn_anom
            if m == "precipitation":
                preds["knn_st"] = np.expm1(base_t_va_w + kn_anom)

            # ---------- 存误差 ----------
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
            print("  进度 %d/%d  用时 %.0fs" % (ti + 1, len(targets), time.time() - t_start), flush=True)

    con.close()

    # ---------------- 汇总 ----------------
    report = {"config": {"n_targets": len(targets), "radius_km": RADIUS, "k_max": K_MAX,
                         "k_spatial": K_SPATIAL, "n_temporal": N_TEMPORAL,
                         "n_valid_days": N_VALID_DAYS, "n_analog": N_ANALOG,
                         "n_knn_st": N_KNN_ST, "dineof_k": DINEOF_KMODES,
                         "train_years": list(V1.TRAIN_YEARS), "val_years": list(V1.VAL_YEARS),
                         "runtime_sec": round(time.time() - t_start, 1)},
              "metrics": {}, "paired_vs_clima": {}, "skipped": skipped}

    print("\n%-22s" % "指标" + "".join("%12s" % mm for mm in METHODS), flush=True)
    for m in METRICS:
        row = {}
        line = "%-22s" % m
        for meth in METHODS:
            d = store[m][meth]
            if len(d) < 30:
                row[meth] = None
                line += "%12s" % "-"
                continue
            errs = np.abs(np.array(list(d.values())))
            mae = float(np.mean(errs))
            row[meth] = {"n": len(d), "mae": round(mae, 4),
                         "rmse": round(float(np.sqrt(np.mean(errs ** 2))), 4),
                         "bias": round(float(np.mean(list(d.values()))), 4),
                         "p90": round(float(np.percentile(errs, 90)), 4)}
            line += "%12.4f" % mae
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
            star = "**" if r["significant"] else "  "
            print("%-20s %-12s Δ=%+.4f CI[%+.4f,%+.4f]%s" % (m, meth, r["delta"], r["ci95"][0], r["ci95"][1], star), flush=True)

    report["variogram"] = {m: {"a_s_median": round(float(np.median(extra[m]["a_s"])), 1) if extra[m]["a_s"] else None,
                               "a_t_median": round(float(np.median(extra[m]["a_t"])), 2) if extra[m]["a_t"] else None}
                           for m in METRICS}

    out = os.path.join(HERE, "repair_validation5.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n已写出 %s  总用时 %.0fs" % (out, time.time() - t_start), flush=True)


if __name__ == "__main__":
    main()
