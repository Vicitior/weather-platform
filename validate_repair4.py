# -*- coding: utf-8 -*-
"""
异常值修复方法交叉验证 第四轮 —— 回答"更科学有效"必须回答的三个定量问题。

第三轮已经证明：地统计（克里金）确实优于简单距平加权，但只优 3% 左右。
要judge一个方法"更科学"，必须回答：

  Q1 这些差异是真的，还是抽样噪声？→ 配对显著性检验
     （同一批样本上逐对比较，用 Wilcoxon 符号秩 + 配对 t + bootstrap 置信区间）

  Q2 "用更多站"到底值不值？→ 邻站数 k 与"变差函数用多少站估"分别扫描
     A. 局部变差函数：只用目标站周围 k 个邻站估 γ(h)
     B. 全局变差函数：用全国 876 站两两配对（数百万对）估 γ(h)  ← 真正的"更多站"
     C. k 扫描：k=4/8/12/20/32 下 krig_anom 的误差

  Q3 "用坐标"到底带来了什么？→ 消融实验
     - 只用邻站值（无坐标）      = clima/krig（隐式含坐标：距离进权重）
     - 加显式坐标 + 海拔 + 日相  = gbr_rich
     - 纯坐标几何插值（不看邻站值） = rbf（第三轮已证伪：比 krig 差 2.6 倍）

科学要点：
  * 全局变差函数比局部更稳：局部只有 k(k-1)/2 个站对，噪声大；
    全局用数百万对，估出的相关半径才靠得住。
  * 显著性检验避免"0.737 vs 0.763 就说赢了"的伪科学。
  * 消融能回答"到底是坐标有用还是邻站观测值有用"。

输出：repair_validation4.json
"""
import os
import sys
import json
import numpy as np
import pandas as pd
from scipy import stats
from scipy.optimize import least_squares
from sklearn.ensemble import GradientBoostingRegressor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_repair as V1
import validate_repair3 as V3

METRICS = V1.METRICS
UNITS = V1.UNITS
PHYS = V1.PHYS_RANGE
RADIUS = 500.0
KS = [4, 8, 12, 20, 32]
N_VALID_DAYS = 120
TRAIN_YEARS = V1.TRAIN_YEARS
VAL_YEARS = V1.VAL_YEARS
ALL_STATION_CAP = 400        # 全局变差函数用多少站（876 全用太慢）


# ---------------------------------------------------------------- 全局变差函数
def global_variogram(con, metric, station_cap=ALL_STATION_CAP, max_pairs=400000):
    """
    用全国多站估变差函数 —— 这才是"依据更多站"的正经做法。

    做法：随机抽 station_cap 个站 × 抽样日期，计算每对站同一天的距平差，
    得到 γ̂(h) 并拟合指数模型。站对数量是局部法的数万倍。
    """
    st = con.execute("""
        SELECT station_id, latitude, longitude
        FROM read_parquet('%s/weather_station_info.parquet')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY station_id
    """ % os.path.join(HERE, "db_mirror", "mysql").replace("\\", "/")).df()
    st = st.head(station_cap)
    ids = [int(x) for x in st["station_id"]]
    lat = st["latitude"].values.astype(float)
    lon = st["longitude"].values.astype(float)

    idlist = ",".join(str(i) for i in ids)
    parquet = os.path.join(HERE, "db_mirror", "tdengine", "weather_data", "*.parquet").replace("\\", "/")
    df = con.execute("""
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS station_id,
               CAST(ts_bj - INTERVAL 100 YEAR AS DATE) AS date,
               month(ts_bj - INTERVAL 100 YEAR) AS m,
               %s AS val
        FROM read_parquet('%s', filename=true)
        WHERE CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) IN (%s)
          AND year(ts_bj - INTERVAL 100 YEAR) BETWEEN 1991 AND 2020
          AND %s BETWEEN %f AND %f
    """ % (metric, parquet, idlist, metric, PHYS[metric][0], PHYS[metric][1])).df()
    if df.empty:
        return None

    # 每站每月气候基准
    gm = df.groupby(["station_id", "m"])["val"].mean().rename("clim").reset_index()
    df = df.merge(gm, on=["station_id", "m"], how="left")
    df["anom"] = df["val"] - df["clim"]

    # 转成 (日期 × 站) 距平矩阵
    piv = df.pivot_table(index="date", columns="station_id", values="anom", aggfunc="first")
    cols = [int(c) for c in piv.columns]
    Z = piv.values.astype(float)
    pos = {sid: i for i, sid in enumerate(ids)}
    jj = [pos.get(c) for c in cols]
    ok = [j for j in jj if j is not None]
    col2j = {c: pos[c] for c in cols if c in pos}

    # 站间距离
    n_used = len(cols)
    idx_used = np.array([col2j[c] for c in cols])
    D = np.zeros((n_used, n_used))
    for a in range(n_used):
        D[a, :] = V1.haversine_km(lat[idx_used[a]], lon[idx_used[a]],
                                  lat[idx_used], lon[idx_used])

    # 采样站对（全组合可能上百万，随机抽）
    pairs = [(a, b) for a in range(n_used) for b in range(a + 1, n_used)]
    if len(pairs) > max_pairs:
        sel = V1.RNG.choice(len(pairs), size=max_pairs, replace=False)
        pairs = [pairs[i] for i in sel]

    hs, gs = [], []
    for a, b in pairs:
        x, y = Z[:, a], Z[:, b]
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 30:
            continue
        hs.append(float(D[a, b]))
        gs.append(0.5 * float(np.mean((x[m] - y[m]) ** 2)))
    if len(hs) < 50:
        return None
    hs = np.asarray(hs)
    gs = np.asarray(gs)
    # 等频分箱到 20 箱
    order = np.argsort(hs)
    hs, gs = hs[order], gs[order]
    hb, gb, wb = [], [], []
    for b in np.array_split(np.arange(len(hs)), 20):
        if len(b) == 0:
            continue
        hb.append(float(np.mean(hs[b])))
        gb.append(float(np.mean(gs[b])))
        wb.append(float(len(b)))
    hb = np.asarray(hb)
    gb = np.asarray(gb)
    wb = np.asarray(wb, dtype=float)
    par = V3.fit_exponential(hb, gb, wb)
    return {"par": list(par) if par else None,
            "h": [round(float(x), 1) for x in hb],
            "g": [round(float(x), 4) for x in gb],
            "n_stations": int(n_used), "n_pairs": int(len(hs)),
            "n_obs": int(len(df))}


# ---------------------------------------------------------------- 配对显著性
def paired_tests(dict_a, dict_b, n_boot=4000):
    """
    同一批样本上的两个方法：samples 以 (站,日期) 为键对齐，
    只比较两法都有值的样本 —— 否则长度不同无法配对，且方法覆盖差异会污染结论。
    """
    keys = [k for k in dict_a if k in dict_b]
    if len(keys) < 30:
        return None
    a = np.abs(np.array([dict_a[k] for k in keys], dtype=float))
    b = np.abs(np.array([dict_b[k] for k in keys], dtype=float))
    ok = np.isfinite(a) & np.isfinite(b)
    a, b = a[ok], b[ok]
    n = len(a)
    if n < 30:
        return None
    out = {"n": int(n), "mae_a": round(float(a.mean()), 4),
           "mae_b": round(float(b.mean()), 4),
           "delta": round(float(b.mean() - a.mean()), 4)}
    try:
        out["wilcoxon_p"] = float(stats.wilcoxon(a, b, zero_method="zsplit").pvalue)
    except Exception:
        out["wilcoxon_p"] = None
    try:
        out["ttest_p"] = float(stats.ttest_rel(a, b).pvalue)
    except Exception:
        out["ttest_p"] = None
    boots = []
    for _ in range(n_boot):
        s = V1.RNG.integers(0, n, n)
        boots.append(float(b[s].mean() - a[s].mean()))
    lo, hi = np.percentile(boots, [2.5, 97.5])
    out["boot_ci95"] = [round(float(lo), 4), round(float(hi), 4)]
    out["significant"] = bool(out["boot_ci95"][0] > 0 or out["boot_ci95"][1] < 0)
    # 胜率：b 比 a 更接近真值的比例
    out["b_better_share"] = round(float(np.mean(b < a)), 3)
    # 配对差的分布（用于判断"少数极端样本拖累"还是"整体偏移"）
    out["median_delta_abs"] = round(float(np.median(b - a)), 4)
    out["win_tie_lose"] = [int(np.sum(b < a)), int(np.sum(b == a)), int(np.sum(b > a))]
    return out


# ---------------------------------------------------------------- 主流程
def main():
    con = V1._con()
    n_per = int(os.environ.get("SMOKE_N", "2"))
    targets = V1.pick_targets(con, n_per_region=n_per)
    only = os.environ.get("SMOKE_ONLY")        # 冒烟：只跑前 N 站
    if only:
        targets = targets.head(int(only))
    print("目标站 %d 个（每省 %d）" % (len(targets), n_per), flush=True)

    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s/weather_station_info.parquet')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % os.path.join(HERE, "db_mirror", "mysql").replace("\\", "/")).df()
    alt_map = {int(k): (None if pd.isna(v) else float(v))
               for k, v in zip(all_st["station_id"], all_st["altitude"])}

    # ============ 先算全局变差函数（"用更多站"的核心证据） ============
    print("\n===== 全局变差函数（全国 %d 站两两配对） =====" % ALL_STATION_CAP, flush=True)
    gvario = {}
    for m in METRICS:
        g = global_variogram(con, m)
        gvario[m] = g
        if g and g["par"]:
            c0, c, a = g["par"]
            print("  %-16s 相关半径 %8.1f km  块金占比 %.3f  站对 %d  样本 %d"
                  % (m, a, c0 / max(c0 + c, 1e-9), g["n_pairs"], g["n_obs"]), flush=True)
        else:
            print("  %-16s 拟合失败" % m, flush=True)

    # ============ hold-out：k 扫描 + 局部/全局变差函数 ============
    # 存储逐样本误差，用于配对检验
    store = {m: {} for m in METRICS}

    for ti, (_, t) in enumerate(targets.iterrows()):
        sid = int(t["station_id"])
        da = V1.haversine_km(t["latitude"], t["longitude"],
                             all_st["latitude"].values, all_st["longitude"].values)
        nb_all = all_st.assign(dist=da).query(
            "station_id != %d and dist <= %f" % (sid, RADIUS)).sort_values("dist").head(max(KS))
        if len(nb_all) < 10:
            print("  跳过 %s：邻站不足" % t["station_name"], flush=True)
            continue
        nb_ids = [int(x) for x in nb_all["station_id"]]
        df = V1.load_station_daily(con, [sid] + nb_ids, TRAIN_YEARS[0], VAL_YEARS[1])
        if df.empty:
            continue
        clim = V3.build_clim(df, METRICS)
        piv = {m: df.pivot_table(index="date", columns="station_id", values=m, aggfunc="first")
               for m in METRICS}
        lat_s = {sid: float(t["latitude"])}
        lon_s = {sid: float(t["longitude"])}
        for _, r in nb_all.iterrows():
            lat_s[int(r["station_id"])] = float(r["latitude"])
            lon_s[int(r["station_id"])] = float(r["longitude"])
        pos_ids = [sid] + nb_ids
        D, lat_v, lon_v = V3.pair_distances(pos_ids, lat_s, lon_s)
        alt_t = alt_map.get(sid)
        alt_n = np.array([alt_map.get(c) if alt_map.get(c) is not None else np.nan
                          for c in nb_ids], dtype=float)
        alt_diff = np.where(np.isnan(alt_n), 0.0, alt_n) - (alt_t if alt_t is not None else 0.0)
        dist_vec = np.array([float(nb_all.loc[nb_all["station_id"] == c, "dist"].iloc[0])
                             for c in nb_ids])

        for m in METRICS:
            p = piv[m]
            if sid not in p.columns:
                continue
            dts_m = pd.to_datetime(p.index)
            mo_m = dts_m.month.values
            yr_m = dts_m.year.values
            have = set(int(c) for c in p.columns)
            jkeep = [0] + [j for j in range(1, len(pos_ids)) if pos_ids[j] in have]
            if len(jkeep) < 6:
                continue
            pos_m = [pos_ids[j] for j in jkeep]
            D_m = D[np.ix_(jkeep, jkeep)]
            dist_m = np.array([float(nb_all.loc[nb_all["station_id"] == c, "dist"].iloc[0])
                               for c in pos_m[1:]])
            ad_m = np.where(np.isnan(np.array(
                [alt_map.get(c) if alt_map.get(c) is not None else np.nan for c in pos_m[1:]],
                dtype=float)), 0.0,
                np.array([alt_map.get(c) if alt_map.get(c) is not None else np.nan
                          for c in pos_m[1:]], dtype=float)) - (alt_t if alt_t is not None else 0.0)
            lat_m, lon_m = lat_v[jkeep], lon_v[jkeep]

            M = p[pos_m].values.astype(float)
            cl_m = clim[m]
            base_full = np.array([cl_m.get((sid, int(mo)), np.nan) for mo in mo_m])
            A = V3.demean_by_clim(M, pos_m, mo_m, cl_m)
            tv = M[:, 0]
            nb_ok = np.isfinite(M[:, 1:]).sum(axis=1) >= 2
            okv = np.isfinite(tv) & nb_ok
            tr_idx = np.where(okv & (yr_m >= TRAIN_YEARS[0]) & (yr_m <= TRAIN_YEARS[1]))[0]
            va_idx = np.where(okv & (yr_m >= VAL_YEARS[0]) & (yr_m <= VAL_YEARS[1]))[0]
            if len(tr_idx) < 200 or len(va_idx) < 40:
                continue
            if len(va_idx) > N_VALID_DAYS:
                va_idx = np.sort(V1.RNG.choice(va_idx, size=N_VALID_DAYS, replace=False))
            ytrue = tv[va_idx]
            base_va = base_full[va_idx]
            base_nb = np.array([[cl_m.get((c, int(mo)), np.nan) for c in pos_m[1:]]
                                for mo in mo_m[va_idx]], dtype=float)
            Vnb = M[va_idx, 1:]
            Anb = A[va_idx, 1:]
            Anb_tr = A[tr_idx, 1:]

            preds = {}
            # --- 基线 ---
            preds["nearest"] = Vnb[:, 0].copy()
            w_all = 1.0 / np.maximum(dist_m, 0.1) ** 2
            def okavg(V, W, keep=None):
                if keep is not None:
                    W = np.tile(W, (len(V), 1)) * keep
                else:
                    W = np.tile(W, (len(V), 1))
                return V3.masked_wavg(V, W)
            preds["idw"] = okavg(Vnb, w_all)
            if m == "precipitation":
                ratio = np.clip(np.where(base_nb > 0.1, base_va[:, None] /
                                         np.where(base_nb > 0.1, base_nb, np.nan), np.nan), 0.3, 3.0)
                anom_adj = Vnb * np.where(np.isfinite(ratio), ratio, 1.0) - base_va[:, None]
            else:
                anom_adj = Anb.copy()
            preds["clima"] = base_va + okavg(anom_adj, w_all)

            # --- krig_anom：局部变差函数 × 不同 k ---
            n_nb = Anb.shape[1]
            for k in KS:
                kk = min(k, n_nb)
                vg = V3.experimental_variogram(Anb_tr[:, :kk], D_m[1:1 + kk, 1:1 + kk])
                par = V3.fit_exponential(*vg) if vg is not None else None
                if par is None:
                    preds["krig_anom_k%d" % k] = np.full(len(va_idx), np.nan)
                    continue
                d0 = D_m[0, 1:1 + kk]
                w, mu, g0 = V3.krig_solve(D_m[1:1 + kk, 1:1 + kk], d0, par)
                wfull = np.zeros(n_nb)
                wfull[:kk] = w
                preds["krig_anom_k%d" % k] = base_va + V3.masked_wavg(
                    Anb, np.tile(wfull, (len(Anb), 1)))

            # --- krig_anom：全局变差函数（全国估的）---
            gpar = (gvario.get(m) or {}).get("par")
            if gpar:
                kk = min(max(KS), n_nb)
                w, mu, g0 = V3.krig_solve(D_m[1:1 + kk, 1:1 + kk], D_m[0, 1:1 + kk], gpar)
                wfull = np.zeros(n_nb)
                wfull[:kk] = w
                preds["krig_glob"] = base_va + V3.masked_wavg(
                    Anb, np.tile(wfull, (len(Anb), 1)))
                # 全局变差 + 前 12 站（与局部法同 k，公平比较）
                k12 = min(12, n_nb)
                w2, mu2, g02 = V3.krig_solve(D_m[1:1 + k12, 1:1 + k12], D_m[0, 1:1 + k12], gpar)
                wf2 = np.zeros(n_nb)
                wf2[:k12] = w2
                preds["krig_glob_k12"] = base_va + V3.masked_wavg(
                    Anb, np.tile(wf2, (len(Anb), 1)))

            # --- 时间维 + 混合 ---
            tmp = np.full(len(va_idx), np.nan)
            for q, ii in enumerate(va_idx):
                bt = cl_m.get((sid, int(mo_m[ii])))
                if bt is None:
                    continue
                ds = []
                for delta in (-1, 1):
                    j = ii + delta
                    if 0 <= j < len(tv):
                        v = tv[j]
                        if np.isfinite(v) and PHYS[m][0] <= v <= PHYS[m][1]:
                            ds.append(v - bt)
                if ds:
                    tmp[q] = bt + float(np.mean(ds))
            preds["temporal_anom"] = tmp
            kbest = preds.get("krig_anom_k20")
            if kbest is None:
                kbest = preds.get("krig_anom_k12")
            preds["blend_krig"] = np.where(
                np.isfinite(kbest) & np.isfinite(tmp), 0.5 * kbest + 0.5 * tmp,
                np.where(np.isfinite(kbest), kbest, tmp))

            # --- GBR（消融：带坐标 vs 不带坐标） ---
            def feats(idx, rich, use_vals=True):
                Z = A[idx][:, 1:]
                n = len(Z)
                cols = []
                if use_vals:
                    cols += [np.nan_to_num(Z, nan=0.0), np.isnan(Z).astype(float)]
                cols += [np.tile(np.log1p(dist_m), (n, 1)), np.tile(ad_m, (n, 1))]
                if rich:
                    doy = dts_m[idx].dayofyear.values.astype(float)
                    cols.append(np.column_stack([
                        np.full(n, float(lat_m[0])), np.full(n, float(lon_m[0])),
                        np.full(n, float(alt_t if alt_t is not None else 0.0)),
                        np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)]))
                return np.column_stack(cols)

            ytr_t = A[tr_idx, 0]
            okg = np.isfinite(ytr_t)
            for tag, rich, usev in (("gbr_rich", True, True),
                                    ("gbr_noCoord", False, True),
                                    ("gbr_onlyCoord", True, False)):
                try:
                    if okg.sum() < 300:
                        preds[tag] = np.full(len(va_idx), np.nan)
                        continue
                    gb = GradientBoostingRegressor(n_estimators=80, max_depth=3,
                                                   learning_rate=0.1, subsample=0.8,
                                                   random_state=0)
                    gb.fit(feats(tr_idx[okg], rich, usev), ytr_t[okg])
                    preds[tag] = base_va + gb.predict(feats(va_idx, rich, usev))
                except Exception:
                    preds[tag] = np.full(len(va_idx), np.nan)

            # 存成 {(站,日期): 误差}，配对检验得以按样本对齐
            daykeys = [str(d)[:10] for d in dts_m[va_idx]]
            for meth, pv in preds.items():
                if pv is None:
                    continue
                pv = np.asarray(pv, dtype=float)
                ok = np.isfinite(pv) & np.isfinite(ytrue)
                if ok.sum() == 0:
                    continue
                d_ = store[m].setdefault(meth, {"samples": {}, "err": [], "true": []})
                d_["err"].extend((pv[ok] - ytrue[ok]).tolist())
                d_["true"].extend(ytrue[ok].tolist())
                for q in np.where(ok)[0]:
                    d_["samples"][(sid, daykeys[q])] = float(pv[q] - ytrue[q])

        print("  [%d/%d] %s" % (ti + 1, len(targets), t["station_name"]), flush=True)

    # ================= 汇总 =================
    out, sig = {}, {}
    print("\n===== 第四轮汇总：MAE 对比 =====")
    for m in METRICS:
        print("\n【%s %s】" % (m, UNITS[m]))
        print("  %-18s %8s %8s %8s" % ("方法", "MAE", "RMSE", "R"))
        row = {}
        for meth, d in sorted(store[m].items()):
            e = np.array(d["err"], dtype=float)
            if len(e) == 0:
                continue
            t_ = np.array(d["true"], dtype=float)
            p_ = t_ - e                          # pred = true - err
            row[meth] = {"mae": round(float(np.mean(np.abs(e))), 3),
                         "rmse": round(float(np.sqrt(np.mean(e ** 2))), 3),
                         "r": round(float(np.corrcoef(t_, p_)[0, 1]), 4) if len(e) > 2 else None,
                         "bias": round(float(np.mean(e)), 3), "n": int(len(e))}
            print("  %-18s %8.3f %8.3f %8.3f" % (
                meth, row[meth]["mae"], row[meth]["rmse"], row[meth]["r"] or 0))
        out[m] = row

        # 配对显著性：核心方法 vs 基线
        ref = "clima" if "clima" in store[m] else None
        base = "nearest" if "nearest" in store[m] else None
        sig[m] = {}
        for cand, label in (("krig_anom_k20", "krig20_vs_clima"),
                            ("krig_glob", "krigglob_vs_clima"),
                            ("krig_glob_k12", "krigglob12_vs_clima"),
                            ("gbr_rich", "gbrrich_vs_clima"),
                            ("blend_krig", "blend_vs_clima"),
                            ("clima", "clima_vs_nearest")):
            a_key, b_key = (base, "clima") if cand == "clima" else (ref, cand)
            if a_key in store[m] and b_key in store[m]:
                r = paired_tests(store[m][a_key]["samples"], store[m][b_key]["samples"])
                if r:
                    sig[m][label] = r
        for lbl, r in sig[m].items():
            print("    %-22s ΔMAE=%+7.3f CI95=[%+.3f,%+.3f] %s"
                  % (lbl, r["delta"], r["boot_ci95"][0], r["boot_ci95"][1],
                     "显著" if r["significant"] else "不显著"))

        # k 扫描
        ks_row = {k: row.get("krig_anom_k%d" % k, {}).get("mae") for k in KS}
        print("    k 扫描(krig_anom): " + "  ".join("k=%d:%.3f" % (k, v)
                                                    for k, v in ks_row.items() if v))

    payload = {"main": out, "significance": sig,
               "global_variogram": {m: gvario[m] for m in METRICS},
               "config": {"k_list": KS, "radius_km": RADIUS, "n_targets": int(len(targets)),
                          "train": list(TRAIN_YEARS), "val": list(VAL_YEARS),
                          "global_station_cap": ALL_STATION_CAP}}
    with open(os.path.join(HERE, "repair_validation4.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n结果已存：repair_validation4.json")


if __name__ == "__main__":
    main()
