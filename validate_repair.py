# -*- coding: utf-8 -*-
"""
异常值修复方法交叉验证 —— 用真实数据回答"取最近站到底准不准"。

做法（hold-out 检验，气象上标准的空间交叉验证）：
  1. 选一批分布在不同气候区的目标站；
  2. 把目标站真值人为遮蔽（假装缺测/异常）；
  3. 用邻站按不同方法重建；
  4. 与真值比较，统计 MAE / RMSE / 相关系数。

对比方法：
  nearest   最近 1 站原值
  idw       距离反比平方加权
  alt_idw   idw + 海拔订正（温度 -6.5℃/km）
  clima     邻站值 + 月气候差（温度加性、降水比值）
  reg       多元线性回归（邻站同日值 → 目标站）
  reg_anom  距平多元回归（邻站距平 → 目标站距平，再加回月基准）

输出：按指标 × 方法 的误差表，用于决定默认方法。
"""
import os
import sys
import json
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from weather_api import data as D

RNG = np.random.default_rng(42)
K = 6                 # 使用几个邻站
RADIUS = 500.0        # 邻站搜索半径 km
N_VALID_DAYS = 120    # 每站每指标的验证天数
TRAIN_YEARS = (1991, 2005)
VAL_YEARS = (2006, 2020)
METRICS = ["avg_temperature", "max_temperature", "min_temperature",
           "avg_humidity", "precipitation", "avg_wind_speed", "sunshine_hours"]
UNITS = {"avg_temperature": "℃", "max_temperature": "℃", "min_temperature": "℃",
         "avg_humidity": "%", "precipitation": "mm", "avg_wind_speed": "m/s",
         "sunshine_hours": "h"}

HERE = os.path.dirname(os.path.abspath(__file__))
PARQUET = os.path.join(HERE, "db_mirror", "tdengine", "weather_data", "*.parquet").replace("\\", "/")

# 物理有效范围（与 weather_api/data.py 的 PHYS_RANGE 一致）
PHYS_RANGE = {
    "avg_temperature": (-100.0, 100.0),
    "max_temperature": (-100.0, 100.0),
    "min_temperature": (-100.0, 100.0),
    "avg_humidity": (0.0, 100.0),
    "precipitation": (0.0, 1000.0),
    "avg_wind_speed": (0.0, 200.0),
    "sunshine_hours": (0.0, 24.0),
}


def _con():
    import duckdb
    c = duckdb.connect()
    c.execute("SET threads TO 8")
    c.execute("PRAGMA memory_limit='6GB'")
    return c


def load_station_daily(c, ids, y1, y2):
    """读若干站在 [y1,y2] 的日值（真实年份通过 -100 年修正），并按物理范围清洗掉哨兵值。"""
    idlist = ",".join(str(int(i)) for i in ids)
    sql = """
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS station_id,
               CAST(ts_bj - INTERVAL 100 YEAR AS DATE) AS date,
               year(ts_bj - INTERVAL 100 YEAR) AS y,
               month(ts_bj - INTERVAL 100 YEAR) AS m,
               avg_temperature, max_temperature, min_temperature, avg_humidity,
               precipitation, avg_wind_speed, sunshine_hours
        FROM read_parquet('%s', filename=true)
        WHERE CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) IN (%s)
          AND year(ts_bj - INTERVAL 100 YEAR) BETWEEN %d AND %d
    """ % (PARQUET, idlist, int(y1), int(y2))
    df = c.execute(sql).df()
    # 关键：清洗哨兵值（999999/32766 等），否则"真值"里混着错值，验证结果无意义
    for m, (lo, hi) in PHYS_RANGE.items():
        if m in df.columns:
            df.loc[(df[m] < lo) | (df[m] > hi), m] = np.nan
    return df


def pick_targets(c, n_per_region=3):
    """按省份分层抽样，保证覆盖不同气候区。"""
    st = c.execute("""
        SELECT station_id, station_name, province, latitude, longitude, altitude
        FROM (SELECT station_id, station_name,
                     regexp_replace(province, '(省|市|自治区|壮族自治区|维吾尔自治区|回族自治区)$', '') AS province,
                     latitude, longitude, altitude
              FROM read_parquet('%s/weather_station_info.parquet'))
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % os.path.join(HERE, "db_mirror", "mysql").replace("\\", "/")).df()
    # 站点按数据量过滤（选数据较全的）
    cnt = c.execute("""
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS sid,
               count(*) AS n
        FROM read_parquet('%s', filename=true) GROUP BY 1
    """ % PARQUET).df()
    ok = set(cnt[cnt["n"] >= 20000]["sid"].tolist())
    st = st[st["station_id"].isin(ok)]
    targets = []
    for prov, g in st.groupby("province"):
        g = g.sort_values("station_id")
        k = min(n_per_region, len(g))
        idx = RNG.choice(len(g), size=k, replace=False)
        targets.append(g.iloc[list(idx)])
    out = pd.concat(targets) if targets else st.head(20)
    return out.reset_index(drop=True)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = p2 - p1
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2
    return r * 2 * np.arcsin(np.sqrt(a))


def fit_predict_lin(X, y, Xv):
    """最小二乘线性回归（带截距），返回验证集预测。"""
    A = np.column_stack([np.ones(len(X)), X])
    Av = np.column_stack([np.ones(len(Xv)), Xv])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    return Av @ coef


def main():
    con = _con()
    targets = pick_targets(con, n_per_region=2)
    print("目标站 %d 个：%s" % (
        len(targets),
        ", ".join("%s(%d)" % (r.station_name, r.station_id) for _, r in targets.iterrows())))

    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s/weather_station_info.parquet')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % os.path.join(HERE, "db_mirror", "mysql").replace("\\", "/")).df()

    results = {m: {meth: {"err": [], "true": [], "pred": []} for meth in
                   ["nearest", "idw", "alt_idw", "clima", "reg", "reg_anom"]}
               for m in METRICS}
    per_station = []

    for ti, (_, t) in enumerate(targets.iterrows()):
        sid = int(t["station_id"])
        d = haversine_km(t["latitude"], t["longitude"],
                         all_st["latitude"].values, all_st["longitude"].values)
        nb = all_st.assign(dist=d).query("station_id != %d and dist <= %f" % (sid, RADIUS))
        nb = nb.sort_values("dist").head(K)
        if len(nb) < 2:
            continue
        ids = [sid] + [int(x) for x in nb["station_id"]]
        alt_t = float(t["altitude"]) if pd.notna(t["altitude"]) else None

        df = load_station_daily(con, ids, TRAIN_YEARS[0], VAL_YEARS[1])
        if df.empty:
            continue
        piv = {m: df.pivot_table(index="date", columns="station_id", values=m, aggfunc="first")
               for m in METRICS}
        dts = pd.to_datetime(pd.Series(sorted(df["date"].unique())))
        mmap = {d: (d.year, d.month) for d in dts}

        # 月气候基准（1991-2020，用目标站+邻站全期）
        clim = {}
        for m in METRICS:
            p = piv[m]
            mon = pd.Series(p.index).dt.month.values
            yy = pd.Series(p.index).dt.year.values
            for c in p.columns:
                s = pd.Series(p[c].values, index=pd.MultiIndex.from_arrays([yy, mon]))
                g = s.groupby(level=[0, 1]).mean()
                for (y, mo), v in g.items():
                    clim.setdefault((int(c), mo), {}).setdefault(m, []).append(float(v))
        clim = {k: {m: float(np.nanmean(v)) for m, v in md.items()} for k, md in clim.items()}

        for m in METRICS:
            p = piv[m]
            if sid not in p.columns:
                continue
            tv = p[sid]
            nbc = [c for c in p.columns if c != sid]
            valid = tv.notna()
            # 只保留邻站至少有 2 个有值的日子
            has_nb = p[nbc].notna().sum(axis=1) >= 2
            okv = valid & has_nb
            years = pd.Series(p.index).dt.year.values
            train_mask = okv & (years >= TRAIN_YEARS[0]) & (years <= TRAIN_YEARS[1])
            val_mask = okv & (years >= VAL_YEARS[0]) & (years <= VAL_YEARS[1])
            val_idx = np.where(val_mask.values)[0]
            if len(val_idx) < 30:
                continue
            if len(val_idx) > N_VALID_DAYS:
                val_idx = RNG.choice(val_idx, size=N_VALID_DAYS, replace=False)
            tr_idx = np.where(train_mask.values)[0]
            if len(tr_idx) < 100:
                continue

            ytrue = tv.iloc[val_idx].values.astype(float)
            dates_v = p.index[val_idx]

            # ---- 各方法 ----
            preds = {}
            # nearest / idw / alt_idw / clima
            for meth in ["nearest", "idw", "alt_idw", "clima"]:
                out = []
                for ii in val_idx:
                    row = p.iloc[ii]
                    contrib = []
                    for c in nbc:
                        v = row[c]
                        if pd.isna(v):
                            continue
                        dist = float(nb.loc[nb["station_id"] == c, "dist"].iloc[0])
                        altn = nb.loc[nb["station_id"] == c, "altitude"].iloc[0]
                        adj = float(v)
                        mo = p.index[ii].month
                        if meth == "clima":
                            bt = clim.get((sid, mo), {}).get(m)
                            bn = clim.get((int(c), mo), {}).get(m)
                            if m == "precipitation":
                                if bt is not None and bn is not None and bn > 0.1:
                                    adj = float(v) * min(max(bt / bn, 0.3), 3.0)
                            elif bt is not None and bn is not None:
                                adj = float(v) + (bt - bn)
                        elif meth == "alt_idw" and m in ("avg_temperature", "max_temperature",
                                                         "min_temperature") \
                                and alt_t is not None and pd.notna(altn):
                            # T_h_t = T_h_n - L*(h_t - h_n)，每升高 1000m 降温 6.5℃
                            adj = float(v) - 0.0065 * (alt_t - float(altn))
                        w = 1.0 if meth == "nearest" else 1.0 / max(dist, 0.1) ** 2
                        contrib.append((adj, w))
                        if meth == "nearest":
                            break
                    if not contrib:
                        out.append(np.nan)
                    else:
                        tw = sum(w for _, w in contrib)
                        out.append(sum(a * w for a, w in contrib) / tw)
                preds[meth] = np.array(out, dtype=float)

            # ---- reg / reg_anom ----
            Xtr = p.iloc[tr_idx][nbc].values.astype(float)
            ytr = tv.iloc[tr_idx].values.astype(float)
            Xv = p.iloc[val_idx][nbc].values.astype(float)
            # 缺失值用训练集中位数填补（避免丢行）
            med = np.nanmedian(Xtr, axis=0)
            med = np.where(np.isnan(med), 0.0, med)
            Xtr_f = np.where(np.isnan(Xtr), med, Xtr)
            Xv_f = np.where(np.isnan(Xv), med, Xv)
            try:
                preds["reg"] = fit_predict_lin(Xtr_f, ytr, Xv_f)
            except Exception:
                preds["reg"] = np.full(len(val_idx), np.nan)
            # reg_anom：先减月基准
            def _anom(X, idx):
                out = X.copy()
                for j, ii in enumerate(idx):
                    mo = p.index[ii].month
                    for ci, c in enumerate(nbc):
                        bn = clim.get((int(c), mo), {}).get(m)
                        if bn is not None:
                            out[j, ci] = out[j, ci] - bn
                return out
            ytr_anom = ytr - np.array([clim.get((sid, p.index[ii].month), {}).get(m, np.nan)
                                       for ii in tr_idx], dtype=float)
            ok_tr = ~np.isnan(ytr_anom)
            if ok_tr.sum() >= 100:
                try:
                    pred_anom = fit_predict_lin(_anom(Xtr_f, tr_idx)[ok_tr], ytr_anom[ok_tr],
                                                _anom(Xv_f, val_idx))
                    base = np.array([clim.get((sid, p.index[ii].month), {}).get(m, np.nan)
                                     for ii in val_idx], dtype=float)
                    preds["reg_anom"] = pred_anom + base
                except Exception:
                    preds["reg_anom"] = np.full(len(val_idx), np.nan)
            else:
                preds["reg_anom"] = np.full(len(val_idx), np.nan)

            for meth, pv in preds.items():
                ok = ~np.isnan(pv) & ~np.isnan(ytrue)
                if ok.sum() == 0:
                    continue
                results[m][meth]["err"].extend((pv[ok] - ytrue[ok]).tolist())
                results[m][meth]["true"].extend(ytrue[ok].tolist())
                results[m][meth]["pred"].extend(pv[ok].tolist())
                per_station.append({"station": t["station_name"], "metric": m, "method": meth,
                                    "mae": float(np.mean(np.abs(pv[ok] - ytrue[ok])))})
        print("  [%d/%d] %s 完成" % (ti + 1, len(targets), t["station_name"]))

    # ---- 汇总 ----
    print("\n===== 交叉验证结果（%d~%d 训练 / %d~%d 验证，k=%d，半径%.0fkm） =====" % (
        TRAIN_YEARS[0], TRAIN_YEARS[1], VAL_YEARS[0], VAL_YEARS[1], K, RADIUS))
    out = {}
    for m in METRICS:
        print("\n【%s %s】" % (m, UNITS[m]))
        print("  %-10s %8s %8s %8s %8s" % ("方法", "MAE", "RMSE", "R", "样本"))
        rowout = {}
        for meth in ["nearest", "idw", "alt_idw", "clima", "reg", "reg_anom"]:
            e = np.array(results[m][meth]["err"], dtype=float)
            t_ = np.array(results[m][meth]["true"], dtype=float)
            p_ = np.array(results[m][meth]["pred"], dtype=float)
            if len(e) == 0:
                continue
            mae = float(np.mean(np.abs(e)))
            rmse = float(np.sqrt(np.mean(e ** 2)))
            r = float(np.corrcoef(t_, p_)[0, 1]) if len(e) > 2 else float("nan")
            rowout[meth] = {"mae": round(mae, 3), "rmse": round(rmse, 3),
                            "r": round(r, 3), "n": int(len(e))}
            print("  %-10s %8.3f %8.3f %8.3f %8d" % (meth, mae, rmse, r, len(e)))
        out[m] = rowout
    with open(os.path.join(HERE, "repair_validation.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n结果已存：repair_validation.json")


if __name__ == "__main__":
    main()
