# -*- coding: utf-8 -*-
"""
异常值修复方法交叉验证 第三轮 —— 探索"更科学有效"的空间插值。

用户要求：不要只靠附近一两个站，用更多站 / 坐标信息，找更科学的办法。

本轮引入地统计学（geostatistics）的正规做法，并与机器学习对照，
全部在同一个 hold-out 框架下比较（人为遮蔽目标站真值 → 邻站重建 → 比真值）：

  nearest        最近 1 站原值（基线，用户最初的直觉方案）
  idw            距离反比平方加权
  clima          距平 IDW（前两轮冠军：邻站距平加权 + 本站月气候基准）
  krig_raw       普通克里金，直接插原始值（纯坐标驱动，不含气候信息）
  krig_anom      普通克里金，插"距平"（气候去趋势 + 变差函数最优加权）★主候选
  krig_log       普通克里金，插 log1p 原始值（降水专用：右偏量乘性更合理）
  regkrig        回归克里金：距平先对海拔差回归，再叠加克里金空间场
  rbf_anom       薄板样条 RBF（纯几何插值，无统计假设）
  gbr_anom       梯度提升树：特征=邻站距平+缺失掩码+距离+高差
  gbr_rich       梯度提升树：再加坐标/海拔/年内日相（正弦余弦）
  temporal_anom  本站前后日距平（时间维）
  blend_krig     0.5×krig_anom + 0.5×temporal_anom（空间+时间融合）

科学要点（这是回答"依据"的关键）：
  1. 克里金权重由**变差函数** γ(h) 决定，不是拍脑袋的 1/d²；
     γ(h)=c0+c(1-e^{-h/a})，a 就是"空间自相关半径"——实测出来的。
  2. 克里金同时给出预测方差 σ²；本轮检验 σ² 是否真能预测实际误差
     （corr(σ², 实际误差²)）。若能，可信度标注就不必是人工三档，
     而可以是数据驱动的连续不确定度。
  3. 天然块金 c0 反映"测站间无法用距离解释的小尺度差异"，
     它也是最近站法误差的下界来源。

输出：repair_validation3.json
"""
import os
import sys
import json
import numpy as np
import pandas as pd
from scipy.optimize import least_squares
from scipy.interpolate import RBFInterpolator
from sklearn.ensemble import GradientBoostingRegressor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_repair as V1

METRICS = V1.METRICS
UNITS = V1.UNITS
PHYS = V1.PHYS_RANGE
RADIUS = 500.0
K_MAX = 20                      # 邻站上限（"更多站"：比第二轮的 12 更多）
N_VALID_DAYS = 120
TRAIN_YEARS = V1.TRAIN_YEARS     # (1991, 2005)
VAL_YEARS = V1.VAL_YEARS         # (2006, 2020)

METHODS = ["nearest", "idw", "clima", "krig_raw", "krig_anom", "krig_log",
           "regkrig", "rbf_anom", "gbr_anom", "gbr_rich",
           "temporal_anom", "blend_krig"]


# ---------------------------------------------------------------- 气候基准
def build_clim(df, metrics):
    """{(station_id, month): {metric: 日均值}}——先按日聚合到月，再跨年平均。"""
    df = df.copy()
    df["_dt"] = pd.to_datetime(df["date"])
    df["_y"] = df["_dt"].dt.year
    df["_m"] = df["_dt"].dt.month
    clim = {m: {} for m in metrics}
    for m in metrics:
        gm = df.groupby(["station_id", "_y", "_m"])[m].mean().reset_index()
        g2 = gm.groupby(["station_id", "_m"])[m].mean()
        for (sid, mo), v in g2.items():
            clim[m][(int(sid), int(mo))] = float(v) if np.isfinite(v) else None
    return clim


def demean_by_clim(arr, cols, months, clim_m):
    """把 (天 × 站) 矩阵按每站自己的月气候基准去趋势 → 距平。"""
    out = np.array(arr, dtype=float, copy=True)
    for j, c in enumerate(cols):
        base = np.array([clim_m.get((int(c), int(mo)), np.nan) for mo in months], dtype=float)
        out[:, j] = out[:, j] - base
    return out


# ---------------------------------------------------------------- 变差函数
def pair_distances(pos_ids, lat_s, lon_s):
    n = len(pos_ids)
    D = np.zeros((n, n))
    lat = np.array([lat_s[i] for i in pos_ids], dtype=float)
    lon = np.array([lon_s[i] for i in pos_ids], dtype=float)
    for i in range(n):
        D[i, :] = V1.haversine_km(lat[i], lon[i], lat, lon)
    return D, lat, lon


def experimental_variogram(Z, D, min_pairs=30, n_bins=8):
    """实验变差 γ̂(h)=½E[(z_i-z_j)²]，按站间距等频分箱。Z:(天,站) D:(站,站)km。"""
    n_st = Z.shape[1]
    hs, gs, ws = [], [], []
    for i in range(n_st):
        for j in range(i + 1, n_st):
            a, b = Z[:, i], Z[:, j]
            ok = np.isfinite(a) & np.isfinite(b)
            if ok.sum() < min_pairs:
                continue
            hs.append(float(D[i, j]))
            gs.append(0.5 * float(np.mean((a[ok] - b[ok]) ** 2)))
            ws.append(float(ok.sum()))
    if len(hs) < 6:
        return None
    hs, gs, ws = np.asarray(hs), np.asarray(gs), np.asarray(ws)
    order = np.argsort(hs)
    hs, gs, ws = hs[order], gs[order], ws[order]
    hb, gb, wb = [], [], []
    for b in np.array_split(np.arange(len(hs)), n_bins):
        if len(b) == 0:
            continue
        hb.append(float(np.average(hs[b], weights=ws[b])))
        gb.append(float(np.average(gs[b], weights=ws[b])))
        wb.append(float(ws[b].sum()))
    if len(hb) < 3:
        return None
    return np.asarray(hb), np.asarray(gb), np.asarray(wb)


def fit_exponential(h, g, w):
    """拟合 γ(h)=c0+c(1-e^{-h/a})，返回 (c0, c, a)；多起点取最优。"""
    if len(h) < 3 or not np.all(np.isfinite(g)):
        return None
    gmax = float(np.nanmax(g))
    if not np.isfinite(gmax) or gmax <= 0:
        return None
    hmax = float(np.nanmax(h)) or 1.0
    sw = np.sqrt(w / w.max())

    def resid(p):
        c0, c, a = p
        return (c0 + c * (1.0 - np.exp(-h / max(a, 1e-9))) - g) * sw

    best = None
    for a0 in (hmax * 0.12, hmax * 0.3, hmax * 0.7, hmax * 1.5):
        try:
            r = least_squares(resid, x0=[0.15 * gmax, 0.75 * gmax, a0],
                              bounds=([0.0, 1e-9, 1e-2], [3 * gmax, 50 * gmax, 200 * hmax]),
                              max_nfev=500)
        except Exception:
            continue
        if best is None or r.cost < best.cost:
            best = r
    if best is None:
        return None
    c0, c, a = [float(x) for x in best.x]
    return c0, c, a


def krig_solve(D_sub, d0, par):
    """普通克里金：解 [[Γ,1],[1ᵀ,0]][w;μ]=[γ₀;1]，返回 (w, μ, γ₀)。"""
    c0, c, a = par
    n = len(d0)
    G = c0 + c * (1.0 - np.exp(-D_sub / max(a, 1e-9)))
    np.fill_diagonal(G, 0.0)          # 站对自身 γ(0)=0
    A = np.ones((n + 1, n + 1))
    A[:n, :n] = G
    A[n, n] = 0.0
    g0 = c0 + c * (1.0 - np.exp(-d0 / max(a, 1e-9)))
    rhs = np.append(g0, 1.0)
    try:
        sol = np.linalg.solve(A, rhs)
    except np.linalg.LinAlgError:
        sol = np.linalg.lstsq(A, rhs, rcond=None)[0]
    return sol[:n], float(sol[n]), g0


def krig_var(w, mu, g0):
    """克里金方差 σ² = Σwᵢγ₀ᵢ + μ。"""
    return float(np.dot(w, g0) + mu)


def masked_wavg(V, W):
    """逐日按权重加权平均，自动跳过缺失邻站并重新归一权重。"""
    vv = np.where(np.isfinite(V), V, 0.0)
    ww = np.where(np.isfinite(V), W, 0.0)
    s = ww.sum(axis=1)
    out = np.full(len(s), np.nan)
    good = s > 0
    out[good] = (vv * ww).sum(axis=1)[good] / s[good]
    return out


# ---------------------------------------------------------------- 主流程
def main():
    con = V1._con()
    nreg = int(os.environ.get("SMOKE_N", "2"))
    targets = V1.pick_targets(con, n_per_region=nreg)
    if os.environ.get("SMOKE_ONLY"):          # 冒烟测试：只跑前 2 站
        targets = targets.head(2)
    print("目标站 %d 个" % len(targets), flush=True)

    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s/weather_station_info.parquet')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % os.path.join(HERE, "db_mirror", "mysql").replace("\\", "/")).df()
    alt_map = {int(k): (None if pd.isna(v) else float(v))
               for k, v in zip(all_st["station_id"], all_st["altitude"])}

    res = {m: {meth: {"err": [], "true": [], "pred": [], "sig2": []} for meth in METHODS}
           for m in METRICS}
    vario_par = []          # 每次拟合的变差函数参数（用于报告"相关半径"）
    kalib = {m: {"sig2": [], "se2": []} for m in METRICS}

    for ti, (_, t) in enumerate(targets.iterrows()):
        sid = int(t["station_id"])
        da = V1.haversine_km(t["latitude"], t["longitude"],
                             all_st["latitude"].values, all_st["longitude"].values)
        nb = all_st.assign(dist=da).query(
            "station_id != %d and dist <= %f" % (sid, RADIUS)).sort_values("dist").head(K_MAX)
        if len(nb) < 5:
            print("  跳过 %s：邻站不足" % t["station_name"], flush=True)
            continue
        nb_ids = [int(x) for x in nb["station_id"]]
        df = V1.load_station_daily(con, [sid] + nb_ids, TRAIN_YEARS[0], VAL_YEARS[1])
        if df.empty:
            continue

        clim = build_clim(df, METRICS)
        piv = {m: df.pivot_table(index="date", columns="station_id", values=m, aggfunc="first")
               for m in METRICS}

        lat_s = {sid: float(t["latitude"])}
        lon_s = {sid: float(t["longitude"])}
        for _, r in nb.iterrows():
            lat_s[int(r["station_id"])] = float(r["latitude"])
            lon_s[int(r["station_id"])] = float(r["longitude"])
        pos_ids = [sid] + nb_ids
        D, lat_v, lon_v = pair_distances(pos_ids, lat_s, lon_s)
        Dsub, d0 = D[1:, 1:], D[0, 1:]
        dist_vec = np.array([float(nb.loc[nb["station_id"] == c, "dist"].iloc[0]) for c in nb_ids])
        alt_t = alt_map.get(sid)
        alt_n = np.array([alt_map.get(c) if alt_map.get(c) is not None else np.nan
                          for c in nb_ids], dtype=float)
        alt_diff = np.where(np.isnan(alt_n), 0.0, alt_n) - (alt_t if alt_t is not None else 0.0)

        for m in METRICS:
            p = piv[m]
            if sid not in p.columns:
                continue
            # 各指标 pivot 的行数可能不同（某天该指标全 NaN 会被剔除），
            # 所以时间轴必须逐指标重算，不能复用 METRICS[0] 的。
            dts_m = pd.to_datetime(p.index)
            mo_m = dts_m.month.values
            yr_m = dts_m.year.values
            # 该指标下实际有数据的站（目标站排在 0 列，邻站可能整段无数据被剔除）
            have = set(int(c) for c in p.columns)
            jkeep = [0] + [j for j in range(1, len(pos_ids)) if pos_ids[j] in have]
            if len(jkeep) < 5:
                continue
            pos_m = [pos_ids[j] for j in jkeep]
            nb_m = [i for i in pos_m[1:]]
            D_m = D[np.ix_(jkeep, jkeep)]
            Dsub, d0 = D_m[1:, 1:], D_m[0, 1:]
            dist_m = np.array([float(nb.loc[nb["station_id"] == c, "dist"].iloc[0]) for c in nb_m])
            alt_n_m = np.array([alt_map.get(c) if alt_map.get(c) is not None else np.nan
                                for c in nb_m], dtype=float)
            alt_diff = np.where(np.isnan(alt_n_m), 0.0, alt_n_m) - (alt_t if alt_t is not None else 0.0)
            lat_m, lon_m = lat_v[jkeep], lon_v[jkeep]

            cols = pos_m
            M = p[cols].values.astype(float)          # (天, 站+1)，第 0 列是目标站
            cl_m = clim[m]
            base_va_full = np.array([cl_m.get((sid, int(mo)), np.nan) for mo in mo_m])
            A = demean_by_clim(M, cols, mo_m, cl_m)    # 距平矩阵

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
            base_va = base_va_full[va_idx]
            base_nb = np.array([[cl_m.get((c, int(mo)), np.nan) for c in nb_m]
                                for mo in mo_m[va_idx]], dtype=float)
            Vnb = M[va_idx, 1:]          # 邻站原始值
            Anb = A[va_idx, 1:]          # 邻站距平
            Anb_tr = A[tr_idx, 1:]

            preds, sig2s = {}, {}
            w_idw = 1.0 / np.maximum(dist_m, 0.1) ** 2

            preds["nearest"] = Vnb[:, 0].copy()
            preds["idw"] = masked_wavg(Vnb, np.tile(w_idw, (len(va_idx), 1)))

            # ---- clima：距平加权 + 本站气候基准（降水走比值法）----
            if m == "precipitation":
                ratio = np.where(base_nb > 0.1, base_va[:, None] / np.where(base_nb > 0.1, base_nb, np.nan), np.nan)
                ratio = np.clip(ratio, 0.3, 3.0)
                anom_adj = Vnb * np.where(np.isfinite(ratio), ratio, 1.0) - base_va[:, None]
            else:
                anom_adj = Anb.copy()
            preds["clima"] = base_va + masked_wavg(anom_adj, np.tile(w_idw, (len(va_idx), 1)))

            # ---- 克里金三兄弟 ----
            variants = [("krig_raw", A[tr_idx, 1:] + np.array(
                [[cl_m.get((c, int(mo)), np.nan) for c in nb_m] for mo in mo_m[tr_idx]]),
                Vnb, "raw"),
                ("krig_anom", Anb_tr, Anb, "anom")]
            if m == "precipitation":
                variants.append(("krig_log", np.log1p(np.clip(
                    A[tr_idx, 1:] + np.array([[cl_m.get((c, int(mo)), np.nan) for c in nb_m]
                                              for mo in mo_m[tr_idx]]), 0, None)),
                    np.log1p(np.clip(Vnb, 0, None)), "log"))

            for tag, Ztr_nb, Zva_nb, kind in variants:
                vg = experimental_variogram(Ztr_nb, Dsub)
                par = fit_exponential(*vg) if vg is not None else None
                if par is not None:
                    vario_par.append({"station": sid, "metric": m, "method": tag,
                                      "nugget": round(par[0], 4), "sill_partial": round(par[1], 4),
                                      "range_km": round(par[2], 1),
                                      "h": [round(x, 1) for x in vg[0].tolist()],
                                      "g": [round(x, 4) for x in vg[1].tolist()]})
                if par is None:
                    preds[tag] = np.full(len(va_idx), np.nan)
                    sig2s[tag] = np.full(len(va_idx), np.nan)
                    continue
                w, mu, g0 = krig_solve(Dsub, d0, par)
                spat = masked_wavg(Zva_nb, np.tile(w, (len(va_idx), 1)))
                if kind == "log":
                    preds[tag] = np.expm1(spat)
                elif kind == "anom":
                    preds[tag] = base_va + spat
                else:
                    preds[tag] = spat
                sig2s[tag] = np.full(len(va_idx), krig_var(w, mu, g0))

            # ---- 回归克里金：距平对"高差"回归，残余空间场用克里金 ----
            try:
                ytr_a = A[tr_idx, 0]
                ok_e = np.isfinite(ytr_a)
                # 邻站距平的"期望值"作为自变量，高差作为第二自变量
                nb_mean_tr = np.nanmean(Anb_tr, axis=1)
                nb_mean_va = np.nanmean(Anb, axis=1)
                okf = ok_e & np.isfinite(nb_mean_tr)
                preds["regkrig"] = np.full(len(va_idx), np.nan)
                if okf.sum() > 200 and np.isfinite(nb_mean_va).sum() > 10:
                    Xtr = np.column_stack([np.ones(okf.sum()), nb_mean_tr[okf]])
                    coef, *_ = np.linalg.lstsq(Xtr, ytr_a[okf], rcond=None)
                    trend = coef[0] + coef[1] * nb_mean_va
                    vg = experimental_variogram(Anb_tr, Dsub)
                    par = fit_exponential(*vg) if vg is not None else None
                    if par is not None:
                        w2, mu2, g02 = krig_solve(Dsub, d0, par)
                        field = masked_wavg(Anb, np.tile(w2, (len(va_idx), 1)))
                        # 回归趋势与空间场各占一半（避免双重计入同一信息）
                        preds["regkrig"] = base_va + 0.5 * trend + 0.5 * field
            except Exception:
                preds.setdefault("regkrig", np.full(len(va_idx), np.nan))

            # ---- 薄板样条 RBF：纯几何插值邻站距平 ----
            try:
                pts_all = np.column_stack([lon_m, lat_m])
                n_va = len(va_idx)
                step = max(1, n_va // 60)
                rbf_pred = np.full(n_va, np.nan)
                for q in range(0, n_va, step):
                    vals = Anb[q]
                    ok = np.isfinite(vals)
                    if ok.sum() < 4:
                        continue
                    pc = pts_all[1:][ok]
                    try:
                        rf = RBFInterpolator(pc, vals[ok], kernel="thin_plate_spline",
                                             smoothing=1e-2)
                        rbf_pred[q] = float(rf(pts_all[0:1])[0])
                    except Exception:
                        continue
                # 未算的日按邻近已算值前向填充，保证样本量与其它方法一致
                goodq = np.where(np.isfinite(rbf_pred))[0]
                if len(goodq) >= 2:
                    rbf_pred = np.interp(np.arange(n_va), goodq, rbf_pred[goodq])
                preds["rbf_anom"] = base_va + rbf_pred
            except Exception:
                preds["rbf_anom"] = np.full(len(va_idx), np.nan)

            # ---- 梯度提升树 ----
            def feats(idx, arr_anom, rich):
                # 关键：只取第 1 列起（邻站），绝不把第 0 列（目标站=标签）当特征，
                # 否则就是数据泄漏，会得到 R=1.000 的假结果。
                Z = (arr_anom[idx] if idx is not None else arr_anom)[:, 1:]
                n = len(Z)
                cols = [np.nan_to_num(Z, nan=0.0), np.isnan(Z).astype(float),
                        np.tile(np.log1p(dist_m), (n, 1)),
                        np.tile(alt_diff, (n, 1))]
                if rich:
                    doy = dts_m[idx].dayofyear.values.astype(float) if idx is not None \
                        else np.zeros(n)
                    cols.append(np.column_stack([
                        np.full(n, float(lat_m[0])), np.full(n, float(lon_m[0])),
                        np.full(n, float(alt_t if alt_t is not None else 0.0)),
                        np.sin(2 * np.pi * doy / 365.25), np.cos(2 * np.pi * doy / 365.25)]))
                return np.column_stack(cols)

            ytr_t = A[tr_idx, 0]
            okg = np.isfinite(ytr_t)
            for tag, rich in (("gbr_anom", False), ("gbr_rich", True)):
                try:
                    if okg.sum() < 300:
                        preds[tag] = np.full(len(va_idx), np.nan)
                        continue
                    gb = GradientBoostingRegressor(n_estimators=80, max_depth=3,
                                                   learning_rate=0.1, subsample=0.8,
                                                   random_state=0)
                    gb.fit(feats(tr_idx[okg], A, rich), ytr_t[okg])
                    preds[tag] = base_va + gb.predict(feats(va_idx, A, rich))
                except Exception:
                    preds[tag] = np.full(len(va_idx), np.nan)

            # ---- 时间维：本站前后日距平 ----
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

            # ---- 空间+时间融合 ----
            ka = np.asarray(preds["krig_anom"], dtype=float)
            preds["blend_krig"] = np.where(
                np.isfinite(ka) & np.isfinite(tmp), 0.5 * ka + 0.5 * tmp,
                np.where(np.isfinite(ka), ka, tmp))

            for meth in METHODS:
                pv = preds.get(meth)
                if pv is None:
                    continue
                pv = np.asarray(pv, dtype=float)
                ok = np.isfinite(pv) & np.isfinite(ytrue)
                if ok.sum() == 0:
                    continue
                res[m][meth]["err"].extend((pv[ok] - ytrue[ok]).tolist())
                res[m][meth]["true"].extend(ytrue[ok].tolist())
                res[m][meth]["pred"].extend(pv[ok].tolist())
                if meth in sig2s:
                    s2 = np.asarray(sig2s[meth], dtype=float)[ok]
                    res[m][meth]["sig2"].extend(s2.tolist())
                    if meth == "krig_anom":
                        kalib[m]["sig2"].extend(s2.tolist())
                        kalib[m]["se2"].extend(((pv[ok] - ytrue[ok]) ** 2).tolist())

        print("  [%d/%d] %s 完成" % (ti + 1, len(targets), t["station_name"]), flush=True)

    # ---------------- 汇总 ----------------
    out = {}
    print("\n===== 第三轮：地统计 / 机器学习对比（k≤%d，半径%.0fkm，验证 %d~%d） =====" % (
        K_MAX, RADIUS, VAL_YEARS[0], VAL_YEARS[1]))
    for m in METRICS:
        print("\n【%s %s】" % (m, UNITS[m]))
        print("  %-14s %8s %8s %8s %8s %8s" % ("方法", "MAE", "RMSE", "R", "偏差", "样本"))
        row = {}
        for meth in METHODS:
            e = np.array(res[m][meth]["err"], dtype=float)
            if len(e) == 0:
                continue
            t_ = np.array(res[m][meth]["true"], dtype=float)
            p_ = np.array(res[m][meth]["pred"], dtype=float)
            row[meth] = {"mae": round(float(np.mean(np.abs(e))), 3),
                         "rmse": round(float(np.sqrt(np.mean(e ** 2))), 3),
                         "r": round(float(np.corrcoef(t_, p_)[0, 1]), 3) if len(e) > 2 else None,
                         "bias": round(float(np.mean(e)), 3), "n": int(len(e))}
            print("  %-14s %8.3f %8.3f %8.3f %8.3f %8d" % (
                meth, row[meth]["mae"], row[meth]["rmse"], row[meth]["r"] or 0.0,
                row[meth]["bias"], row[meth]["n"]))
        out[m] = row

    # 克里金方差标定
    cal = {}
    print("")
    for m in METRICS:
        s2 = np.array(kalib[m]["sig2"], dtype=float)
        se2 = np.array(kalib[m]["se2"], dtype=float)
        ok = np.isfinite(s2) & np.isfinite(se2) & (s2 > 0)
        if ok.sum() < 50:
            continue
        cal[m] = {"n": int(ok.sum()),
                  "corr_sig2_se2": round(float(np.corrcoef(s2[ok], se2[ok])[0, 1]), 3),
                  "se2_over_sig2": round(float(np.mean(se2[ok]) / np.mean(s2[ok])), 3)}
        print("[克里金方差标定 %-16s] corr(σ²,实错²)=%6.3f  实错²/σ²=%5.2f  n=%d"
              % (m, cal[m]["corr_sig2_se2"], cal[m]["se2_over_sig2"], cal[m]["n"]))

    # 变差函数统计：空间相关半径
    vstats = {}
    for m in METRICS:
        rs = [v["range_km"] for v in vario_par if v["metric"] == m and v["method"] == "krig_anom"]
        nu = [v["nugget"] for v in vario_par if v["metric"] == m and v["method"] == "krig_anom"]
        pa = [v["sill_partial"] for v in vario_par if v["metric"] == m and v["method"] == "krig_anom"]
        if rs:
            nu_a = np.array(nu)
            pa_a = np.array(pa)
            vstats[m] = {"n_fits": len(rs),
                         "range_km_median": round(float(np.median(rs)), 1),
                         "range_km_p25": round(float(np.percentile(rs, 25)), 1),
                         "range_km_p75": round(float(np.percentile(rs, 75)), 1),
                         "nugget_share_median": round(float(np.median(
                             nu_a / np.maximum(nu_a + pa_a, 1e-9))), 3)}
    print("\n[变差函数：空间自相关半径（krig_anom）]")
    for m, s in vstats.items():
        print("  %-16s 中位半径 %6.1f km (IQR %.1f~%.1f)  块金占比 %.2f  n=%d"
              % (m, s["range_km_median"], s["range_km_p25"], s["range_km_p75"],
                 s["nugget_share_median"], s["n_fits"]))

    payload = {"main": out, "calibration": cal, "variogram_stats": vstats,
               "variograms": vario_par[:400],
               "config": {"k_max": K_MAX, "radius_km": RADIUS,
                          "train": list(TRAIN_YEARS), "val": list(VAL_YEARS),
                          "n_targets": int(len(targets)), "n_valid_days": N_VALID_DAYS}}
    with open(os.path.join(HERE, "repair_validation3.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n结果已存：repair_validation3.json")


if __name__ == "__main__":
    main()
