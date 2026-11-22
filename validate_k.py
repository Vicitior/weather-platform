# -*- coding: utf-8 -*-
"""
补充实验：k 到底取到多大才饱和（第一/二轮只测到 12）。
只测 2 个代表指标（气温、降水）× 更多 k 值，减少站点数以控时间。
"""
import os
import sys
import json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import validate_repair as V1
import validate_repair2 as V2

KS = [1, 2, 4, 6, 8, 12, 16, 24, 32]
METRICS = ["avg_temperature", "precipitation"]
N_TARGETS_PER_PROV = 1


def main():
    con = V1._con()
    targets = V1.pick_targets(con, n_per_region=N_TARGETS_PER_PROV)
    print("目标站 %d 个，k 上限 %d" % (len(targets), max(KS)), flush=True)

    all_st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s/weather_station_info.parquet')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
    """ % os.path.join(HERE, "db_mirror", "mysql").replace("\\", "/")).df()

    acc = {m: {k: {"err": [], "t": []} for k in KS} for m in METRICS}

    for ti, (_, t) in enumerate(targets.iterrows()):
        sid = int(t["station_id"])
        d = V1.haversine_km(t["latitude"], t["longitude"],
                            all_st["latitude"].values, all_st["longitude"].values)
        nb_all = all_st.assign(dist=d).query("station_id != %d and dist <= %f" % (sid, V1.RADIUS))
        nb_all = nb_all.sort_values("dist")
        if len(nb_all) < 4:
            continue
        nb = nb_all.head(max(KS))
        ids = [sid] + [int(x) for x in nb["station_id"]]
        alt_t = float(t["altitude"]) if pd.notna(t["altitude"]) else None

        df = V1.load_station_daily(con, ids, V1.TRAIN_YEARS[0], V1.VAL_YEARS[1])
        if df.empty:
            continue
        piv = {m: df.pivot_table(index="date", columns="station_id", values=m, aggfunc="first")
               for m in METRICS}
        clim = V2.build_clim(piv, METRICS)

        for m in METRICS:
            p = piv[m]
            if sid not in p.columns:
                continue
            tv = p[sid]
            nbc = [c for c in p.columns if c != sid]
            years = pd.Series(p.index).dt.year.values
            okv = tv.notna() & (p[nbc].notna().sum(axis=1) >= 2)
            va = okv & (years >= V1.VAL_YEARS[0]) & (years <= V1.VAL_YEARS[1])
            va_idx = np.where(va.values)[0]
            if len(va_idx) < 30:
                continue
            if len(va_idx) > V1.N_VALID_DAYS:
                va_idx = np.sort(V1.RNG.choice(va_idx, size=V1.N_VALID_DAYS, replace=False))
            ytrue = tv.iloc[va_idx].values.astype(float)
            for k in KS:
                pred = []
                for ii in va_idx:
                    row = p.iloc[ii]
                    mo = p.index[ii].month
                    pred.append(V2.spatial_pred(row, nbc, nb, "clima", m, mo, sid, clim, alt_t, k))
                arr = np.array(pred, float)
                ok = np.isfinite(arr) & np.isfinite(ytrue)
                if ok.sum():
                    acc[m][k]["err"].extend((arr[ok] - ytrue[ok]).tolist())
                    acc[m][k]["t"].extend(ytrue[ok].tolist())
        print("  [%d/%d] %s" % (ti + 1, len(targets), t["station_name"]), flush=True)

    print("\n===== k 饱和测试（clima，半径 %.0fkm） =====" % V1.RADIUS)
    out = {}
    hdr = "  %-16s " % "指标" + " ".join("k=%-6d" % k for k in KS)
    print(hdr)
    for m in METRICS:
        out[m] = {}
        cells = []
        for k in KS:
            e = np.array(acc[m][k]["err"], float)
            if len(e) == 0:
                ok_ = False
                cells.append("%-8s" % "-")
                continue
            v = float(np.mean(np.abs(e)))
            out[m][k] = {"mae": round(v, 3), "n": int(len(e))}
            cells.append("%-8.3f" % v)
        print("  %-16s %s" % (m, " ".join(cells)))
    with open(os.path.join(HERE, "repair_k_saturation.json"), "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("\n结果已存：repair_k_saturation.json")


if __name__ == "__main__":
    main()
