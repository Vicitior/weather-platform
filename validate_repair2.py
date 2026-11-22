# -*- coding: utf-8 -*-
"""
异常值修复方法交叉验证 第二轮 —— 回答四个质疑：
  ① 用最近站有什么依据？
  ② 邻站一变结果就变，稳定吗？
  ③ 真的好用吗？
  ④ 能更好用吗？

在第一轮基础上新增：
  - clima_alt  气候距平 IDW + 海拔订正（温度）
  - temporal   同一站前后两日算术平均（时间维插值）
  - temporal_anom  前后两日距平平均 + 本站在月基准
  - blend      0.5*clima + 0.5*temporal_anom
  - k 敏感性：clima 在 k=1,2,3,4,6,8,12 下的误差
  - 稳定性：抽掉最近 1 站，看补给值变化多少（越小越稳）
  - 分季节误差：冬季 vs 夏季 clima / nearest 谁更稳

结果：repair_validation2.json
"""
import os
import sys
import json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_repair as V1

METRICS = V1.METRICS
UNITS = V1.UNITS
RADIUS = V1.RADIUS
N_VALID_DAYS = V1.N_VALID_DAYS

METHODS = ["nearest", "idw", "alt_idw", "clima", "clima_alt",
           "temporal", "temporal_anom", "blend"]
KS = [1, 2, 3, 4, 6, 8, 12]


def build_clim(piv, metrics):
    """每站 × 月 的气候基准（全期平均）。"""
    clim = {}
    for m in metrics:
        p = piv[m]
        mon = pd.Series(p.index).dt.month.values
        for c in p.columns:
            s = pd.Series(p[c].values, index=pd.Index(mon))
            g = s.groupby(level=0).mean()
            for mo, v in g.items():
                clim.setdefault((int(c), int(mo)), {})[m] = float(v) if np.isfinite(v) else None
    return clim


def spatial_pred(row, nbc, nb, meth, m, mo, sid, clim, alt_t, kk):
    """空间插值：nearest/idw/alt_idw/clima/clima_alt。"""
    contrib = []
    for c in nbc:
        v = row[c]
        if pd.isna(v):
            continue
        dist = float(nb.loc[nb["station_id"] == c, "dist"].iloc[0])
        altn = nb.loc[nb["station_id"] == c, "altitude"].iloc[0]
        adj = float(v)
        bt = clim.get((sid, mo), {}).get(m)
        bn = clim.get((int(c), mo), {}).get(m)
        lapse = 0.0
        if alt_t is not None and pd.notna(altn):
            lapse = -0.0065 * (alt_t - float(altn))   # 目标站比邻站高则更冷
        if meth in ("clima", "clima_alt"):
            if m == "precipitation":
                if bt is not None and bn is not None and bn > 0.1:
                    adj = float(v) * min(max(bt / bn, 0.3), 3.0)
            elif bt is not None and bn is not None:
                adj = float(v) + (bt - bn)
            if meth == "clima_alt" and m in ("avg_temperature", "max_temperature",
                                             "min_temperature"):
                adj += lapse
        elif meth == "alt_idw" and m in ("avg_temperature", "max_temperature",
                                         "min_temperature"):
            adj = float(v) + lapse
        w = 1.0 if meth == "nearest" else 1.0 / max(dist, 0.1) ** 2
        contrib.append((adj, w))
        if meth == "nearest" or len(contrib) >= kk:
            break
    if not contrib:
        return np.nan
    tw = sum(w for _, w in contrib)
    return sum(a * w for a, w in contrib) / tw


def main():
    con = V1._con()
    targets = V1.pick_targets(con, n_per_region=2)
    print("目标站 %d 个" % len(targets), flush=True)

    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s/weather_station_info.parquet')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % os.path.join(HERE, "db_mirror", "mysql").replace("\\", "/")).df()

    res = {m: {me: {"err": [], "true": [], "pred": []} for me in METHODS} for m in METRICS}
    res_k = {m: {k: {"err": [], "true": []} for k in KS} for m in METRICS}
    stab = {m: {me: [] for me in ["nearest", "clima", "clima_alt"]} for m in METRICS}
    season = {m: {me: {"winter": [], "summer": []} for me in ["nearest", "clima"]}
              for m in METRICS}

    for ti, (_, t) in enumerate(targets.iterrows()):
        sid = int(t["station_id"])
        d = V1.haversine_km(t["latitude"], t["longitude"],
                            all_st["latitude"].values, all_st["longitude"].values)
        nb_all = all_st.assign(dist=d).query("station_id != %d and dist <= %f" % (sid, RADIUS))
        nb_all = nb_all.sort_values("dist")
        if len(nb_all) < 3:
            continue
        nb = nb_all.head(12)                      # 取 12 个候选，供 k 敏感性用
        ids = [sid] + [int(x) for x in nb["station_id"]]
        alt_t = float(t["altitude"]) if pd.notna(t["altitude"]) else None

        df = V1.load_station_daily(con, ids, V1.TRAIN_YEARS[0], V1.VAL_YEARS[1])
        if df.empty:
            continue
        piv = {m: df.pivot_table(index="date", columns="station_id", values=m,
                                 aggfunc="first") for m in METRICS}
        clim = build_clim(piv, METRICS)

        for m in METRICS:
            p = piv[m]
            if sid not in p.columns:
                continue
            tv = p[sid]
            nbc = [c for c in p.columns if c != sid]
            has_nb = p[nbc].notna().sum(axis=1) >= 2
            years = pd.Series(p.index).dt.year.values
            okv = tv.notna() & has_nb
            tr_mask = okv & (years >= V1.TRAIN_YEARS[0]) & (years <= V1.TRAIN_YEARS[1])
            va_mask = okv & (years >= V1.VAL_YEARS[0]) & (years <= V1.VAL_YEARS[1])
            tr_idx = np.where(tr_mask.values)[0]
            va_idx = np.where(va_mask.values)[0]
            if len(va_idx) < 30 or len(tr_idx) < 100:
                continue
            if len(va_idx) > N_VALID_DAYS:
                va_idx = V1.RNG.choice(va_idx, size=N_VALID_DAYS, replace=False)
                va_idx = np.sort(va_idx)

            ytrue = tv.iloc[va_idx].values.astype(float)
            preds = {me: [] for me in METHODS}
            preds_k = {k: [] for k in KS}
            stab_pred = {me: {"full": [], "drop1": []}
                         for me in ["nearest", "clima", "clima_alt"]}

            for ii in va_idx:
                row = p.iloc[ii]
                mo = p.index[ii].month
                for me in ["nearest", "idw", "alt_idw", "clima", "clima_alt"]:
                    kk = 1 if me == "nearest" else 12
                    preds[me].append(spatial_pred(row, nbc, nb, me, m, mo, sid, clim, alt_t, kk))
                for k in KS:
                    preds_k[k].append(spatial_pred(row, nbc, nb, "clima", m, mo, sid, clim,
                                                   alt_t, k))

                # 时间维：本站前一日 / 后一日（用全序列位置前后 1）
                pos = p.index.get_loc(p.index[ii])
                prev_v = p[sid].iloc[pos - 1] if pos > 0 else np.nan
                next_v = p[sid].iloc[pos + 1] if pos + 1 < len(p) else np.nan
                tvals = [x for x in [prev_v, next_v] if pd.notna(x)]
                clim_t = clim.get((sid, mo), {}).get(m)
                # temporal：前后两日均值
                preds["temporal"].append(float(np.mean(tvals)) if tvals else np.nan)
                # temporal_anom：前后两日距平均值 + 月基准
                if tvals and clim_t is not None:
                    prev_mo = p.index[pos - 1].month if pos > 0 else mo
                    next_mo = p.index[pos + 1].month if pos + 1 < len(p) else mo
                    anoms = []
                    if pd.notna(prev_v):
                        c1 = clim.get((sid, prev_mo), {}).get(m)
                        if c1 is not None:
                            anoms.append(float(prev_v) - c1)
                    if pd.notna(next_v):
                        c2 = clim.get((sid, next_mo), {}).get(m)
                        if c2 is not None:
                            anoms.append(float(next_v) - c2)
                    preds["temporal_anom"].append(
                        clim_t + float(np.mean(anoms)) if anoms else np.nan)
                else:
                    preds["temporal_anom"].append(np.nan)

                # 稳定性：抽掉最近 1 站
                nbc_d = nbc[1:] if len(nbc) > 1 else nbc
                nb_d = nb[nb["station_id"] != nbc[0]] if len(nb) > 1 else nb
                for me in ["nearest", "clima", "clima_alt"]:
                    kk = 1 if me == "nearest" else 12
                    stab_pred[me]["full"].append(
                        spatial_pred(row, nbc, nb, me, m, mo, sid, clim, alt_t, kk))
                    stab_pred[me]["drop1"].append(
                        spatial_pred(row, nbc_d, nb_d, me, m, mo, sid, clim, alt_t, kk))

            # blend：气候距平 + 时间距平 各半（先算好再统一评估）
            a_ = np.array(preds["clima"], dtype=float)
            b_ = np.array(preds["temporal_anom"], dtype=float)
            preds["blend"] = np.where(np.isnan(a_), b_,
                                      np.where(np.isnan(b_), a_, 0.5 * (a_ + b_))).tolist()

            for me in METHODS:
                arr = np.array(preds[me], dtype=float)
                if arr.shape[0] != ytrue.shape[0]:
                    continue
                ok = np.isfinite(arr) & np.isfinite(ytrue)
                if ok.sum() == 0:
                    continue
                res[m][me]["err"].extend((arr[ok] - ytrue[ok]).tolist())
                res[m][me]["true"].extend(ytrue[ok].tolist())
                res[m][me]["pred"].extend(arr[ok].tolist())
                # 分季节（12,1,2 冬 / 6,7,8 夏）
                mos = np.array([p.index[i].month for i in va_idx])[ok]
                for tag, sel in [("winter", np.isin(mos, [12, 1, 2])),
                                 ("summer", np.isin(mos, [6, 7, 8]))]:
                    if me in season[m]:
                        season[m][me][tag].extend((arr[ok] - ytrue[ok])[sel].tolist())

            for k in KS:
                arr = np.array(preds_k[k], dtype=float)
                ok = np.isfinite(arr) & np.isfinite(ytrue)
                if ok.sum():
                    res_k[m][k]["err"].extend((arr[ok] - ytrue[ok]).tolist())
                    res_k[m][k]["true"].extend(ytrue[ok].tolist())

            for me in stab_pred:
                a = np.array(stab_pred[me]["full"], dtype=float)
                b = np.array(stab_pred[me]["drop1"], dtype=float)
                ok = np.isfinite(a) & np.isfinite(b)
                if ok.sum():
                    stab[m][me].extend(np.abs(a[ok] - b[ok]).tolist())
        print("  [%d/%d] %s" % (ti + 1, len(targets), t["station_name"]), flush=True)

    # ---- 汇总 ----
    def stats(e, t_, p_):
        e = np.asarray(e, float)
        if len(e) == 0:
            return None
        r = float(np.corrcoef(t_, p_)[0, 1]) if len(e) > 2 else float("nan")
        return {"mae": round(float(np.mean(np.abs(e))), 3),
                "rmse": round(float(np.sqrt(np.mean(e ** 2))), 3),
                "bias": round(float(np.mean(e)), 3),
                "r": round(r, 3), "n": int(len(e))}

    out = {"main": {}, "k_sensitivity": {}, "stability": {}, "season": {}}
    print("\n===== 第二轮：10 方法对比 =====", flush=True)
    for m in METRICS:
        print("\n【%s %s】" % (m, UNITS[m]))
        print("  %-14s %8s %8s %8s %8s" % ("方法", "MAE", "RMSE", "bias", "R"))
        out["main"][m] = {}
        for me in METHODS:
            d = res[m][me]
            s = stats(d["err"], d["true"], d["pred"])
            if s is None:
                continue
            out["main"][m][me] = s
            print("  %-14s %8.3f %8.3f %8.3f %8.3f" % (me, s["mae"], s["rmse"], s["bias"], s["r"]))

    print("\n===== k 敏感性（clima）MAE =====")
    print("  %-16s %s" % ("指标", " ".join("k=%-6d" % k for k in KS)))
    for m in METRICS:
        vals = []
        out["k_sensitivity"][m] = {}
        for k in KS:
            s = stats(res_k[m][k]["err"], res_k[m][k]["true"], [0] * len(res_k[m][k]["true"]))
            mae = round(float(np.mean(np.abs(res_k[m][k]["err"]))), 3) if res_k[m][k]["err"] else None
            out["k_sensitivity"][m][k] = mae
            vals.append("%-8s" % (mae if mae is not None else "-"))
        print("  %-16s %s" % (m, " ".join(vals)))

    print("\n===== 稳定性：抽掉最近 1 站后补给值变化（MAE 绝对值，越小越稳） =====")
    print("  %-16s %10s %10s %10s" % ("指标", "nearest", "clima", "clima_alt"))
    for m in METRICS:
        out["stability"][m] = {}
        row = []
        for me in ["nearest", "clima", "clima_alt"]:
            v = float(np.mean(stab[m][me])) if stab[m][me] else None
            out["stability"][m][me] = round(v, 3) if v is not None else None
            row.append("%-10s" % (round(v, 3) if v is not None else "-"))
        print("  %-16s %s" % (m, " ".join(row)))

    print("\n===== 分季节 MAE（nearest vs clima） =====")
    print("  %-16s %10s %10s %10s %10s" % ("指标", "nearest冬", "clima冬", "nearest夏", "clima夏"))
    for m in METRICS:
        out["season"][m] = {}
        row = []
        for tag in ["winter", "summer"]:
            for me in ["nearest", "clima"]:
                arr = season[m][me][tag]
                v = float(np.mean(np.abs(arr))) if arr else None
                out["season"][m].setdefault(tag, {})[me] = round(v, 3) if v is not None else None
        for tag in ["winter", "summer"]:
            for me in ["nearest", "clima"]:
                v = out["season"][m][tag].get(me)
                row.append("%-10s" % (v if v is not None else "-"))
        print("  %-16s %s" % (m, " ".join(row)))

    with open(os.path.join(HERE, "repair_validation2.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n结果已存：repair_validation2.json", flush=True)


if __name__ == "__main__":
    main()
