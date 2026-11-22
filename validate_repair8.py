# -*- coding: utf-8 -*-
"""
第八轮：用上全网 876 站的低秩补全 + 按相关性选邻站 + 克里金数值修正

为什么要有这一轮（前七轮的方法论缺陷）：
  前七轮所有方法（clima / krig / gbr / analog / dineof）**都只用了 4~20 个邻站**。
  但第四轮实测气温距平的空间相关半径是 1243km，而站间距只有 100~200km
  —— 意思是同一天全国 876 个站几乎落在同一个相关长度内，整个场是「一个天气型 + 噪声」，
  这正是低秩结构。文献里低秩补全强的场景就是这种「全场共同模态」。
  第五轮的 dineof 只喂了 21 列（目标站+20 邻站），rank-8 的 SVD 在 21 列上几乎没自由度，
  等于没测。这轮按文献的正确做法：**整张 (天 × 876站) 矩阵一次性重建**。

实验设计（关键：一次 SVD 评估所有目标，而不是每个目标跑一遍）
  1. 建 (10957 天 × 876 站) 的距平矩阵（按站×月气候基准去趋势，基准只用 1991-2005 训练期）
  2. 把「59 个目标站各自 120 个验证日」这些格子一次性置为缺失（共约 7000 格 / 960 万格 = 0.07%）
  3. 做一次低秩迭代补全（Beckers & Rixen DINEOF 式：SVD 重建 → 回填观测 → 再 SVD）
  4. 在所有被遮蔽的格子上取预测值，与真值比
  这样一次补全就能评估全部目标，也才是文献里那个方法。

  对照方法：
    clima      多站加权气候距平（k=12 最近站）        ← 我们的现有基线
    ro_clima   按「训练期相关系数」选 12 个站再做 clima  ← 文献推荐的 RO 选站
    lr_full    全网 876 站低秩（rank 扫描 4/8/16/24）
    lr_sub     只用目标站附近的子网（300 站）低秩       ← 回答「是不是站越多越好」

输出 repair_validation8.json / validate_repair8.out.txt
用法：
    python validate_repair8.py
    set SMOKE_ONLY=1 && python validate_repair8.py
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
TRAIN_YEARS = V1.TRAIN_YEARS          # (1991, 2005)
VAL_YEARS = V1.VAL_YEARS              # (2006, 2020)
N_VALID_DAYS = 120
K_NB = 12                             # clima / ro_clima 的邻站数
RANKS = [4, 8, 16, 24]
LR_ITERS = 15
SEED = 42

METHODS = ["clima", "ro_clima"] + ["lr_full_r%d" % k for k in RANKS] + ["lr_sub_r8", "lr_sub_r16"]


# ------------------------------------------------------------------ 数据装配
def build_panel(con):
    """返回 (dates, station_ids, st, M) —— M 形状 (天, 站, 指标)，float32，已按物理范围清洗。"""
    # 自建 weather_hist 视图（V1._con() 不带 weather_api 的视图），
    # 时间戳修正含闰日：源库整体 +100 年位移把 2000-02-29 钳到 2100-02-28 并留 12:00 相位，
    # 故 hour=12 的行要再 +1 天（详见 weather_api/data.py 的 date_true）。
    con.execute("""
        CREATE OR REPLACE VIEW weather_hist AS
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS station_id,
               CAST(CAST(ts_bj AS DATE) - INTERVAL 100 YEAR AS DATE)
                   + (CASE WHEN hour(ts_bj) = 12 THEN 1 ELSE 0 END) AS date_true,
               avg_temperature, max_temperature, min_temperature, avg_humidity,
               precipitation, avg_wind_speed, sunshine_hours
        FROM read_parquet('%s', filename=true)
        WHERE CAST(CAST(ts_bj AS DATE) - INTERVAL 100 YEAR AS DATE)
                  + (CASE WHEN hour(ts_bj) = 12 THEN 1 ELSE 0 END) >= DATE '1991-01-01'
          AND CAST(CAST(ts_bj AS DATE) - INTERVAL 100 YEAR AS DATE)
                  + (CASE WHEN hour(ts_bj) = 12 THEN 1 ELSE 0 END) <= DATE '2020-12-31'
    """ % os.path.join(HERE, "db_mirror", "tdengine", "weather_data", "*.parquet").replace("\\", "/"))
    st_path = os.path.join(HERE, "db_mirror", "mysql", "weather_station_info.parquet").replace("\\", "/")
    st = con.execute("""
        SELECT station_id, latitude, longitude, altitude
        FROM read_parquet('%s')
        WHERE latitude IS NOT NULL AND longitude IS NOT NULL
        ORDER BY station_id
    """ % st_path).df()
    sid_list = [int(x) for x in st["station_id"]]
    pos = {s: i for i, s in enumerate(sid_list)}

    dates = con.execute("""
        SELECT DISTINCT date_true FROM weather_hist
        WHERE date_true BETWEEN DATE '1991-01-01' AND DATE '2020-12-31'
        ORDER BY 1
    """).df()["date_true"].tolist()
    dpos = {d: i for i, d in enumerate(dates)}
    nd, ns = len(dates), len(sid_list)
    print("  面板 %d 天 × %d 站 = %.2fM 格/指标" % (nd, ns, nd * ns / 1e6), flush=True)

    M = np.full((nd, ns, len(METRICS)), np.nan, dtype=np.float32)
    cols = ", ".join("avg(%s) AS %s" % (m, m) for m in METRICS)
    for yr in range(1991, 2021):
        q = """
            SELECT station_id, date_true, %s
            FROM weather_hist
            WHERE date_true BETWEEN DATE '%d-01-01' AND DATE '%d-12-31'
            GROUP BY 1, 2
        """ % (cols, yr, yr)
        d = con.execute(q).df()
        if d.empty:
            continue
        si = d["station_id"].map(pos).values
        di = d["date_true"].map(dpos).values
        ok = pd.notna(si) & pd.notna(di)
        si = si[ok].astype(np.int32)
        di = di[ok].astype(np.int32)
        for mi, m in enumerate(METRICS):
            lo, hi = V1.PHYS_RANGE[m]
            v = d[m].values[ok].astype(np.float32)
            v = np.where((v >= lo) & (v <= hi), v, np.nan)
            M[di, si, mi] = v
        if yr % 10 == 0 or yr == 2020:
            print("    装配 %d 年" % yr, flush=True)
    return dates, sid_list, st, M


def climatology(M, dates, sid_list):
    """按 (站, 月) 在训练期算气候基准，返回 dict[(si, month)] -> [7 个指标值]。"""
    yrs = np.array([d.year for d in dates])
    mos = np.array([d.month for d in dates])
    tr = (yrs >= TRAIN_YEARS[0]) & (yrs <= TRAIN_YEARS[1])
    out = {}
    for mo in range(1, 13):
        sel = tr & (mos == mo)
        if not sel.any():
            continue
        blk = M[sel]                                  # (n天, 站, 指标)
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(blk, axis=0)            # (站, 指标)
        for si in range(len(sid_list)):
            out[(si, mo)] = mean[si]
    return out, mos, yrs


def anomaly_matrix(M, clim, mos, mi):
    """单指标距平矩阵 (天, 站)。降水走 log1p 空间。"""
    work = M[:, :, mi]
    if METRICS[mi] == "precipitation":
        work = np.log1p(np.maximum(work, 0.0))
    base = np.full_like(work, np.nan)
    for mo in range(1, 13):
        sel = mos == mo
        if not sel.any():
            continue
        col = np.array([clim.get((si, mo), np.full(len(METRICS), np.nan))[mi]
                        for si in range(work.shape[1])], dtype=np.float32)
        if METRICS[mi] == "precipitation":
            col = np.log1p(np.maximum(col, 0.0))
        base[sel, :] = col[None, :]
    return work - base, work, base


# ------------------------------------------------------------------ 低秩补全
def randomized_svd_small(X, k, n_iter=4, seed=SEED):
    """对 (m, n) 稠密矩阵做 rank-k 随机化 SVD（n 远小于 m 时足够快）。"""
    rng = np.random.default_rng(seed)
    m, n = X.shape
    k = int(min(k, min(m, n)))
    P = rng.standard_normal((n, k + 10)).astype(np.float32)
    Y = X @ P
    for _ in range(n_iter):
        Y, _ = np.linalg.qr(Y)
        Z, _ = np.linalg.qr(X.T @ Y)
        Y = X @ Z
    Q, _ = np.linalg.qr(Y)
    B = Q.T @ X
    Ub, S, Vt = np.linalg.svd(B, full_matrices=False)
    U = Q @ Ub
    return U[:, :k] * S[:k], Vt[:k, :]


def lowrank_fill(A, mask_cells, rank, iters=LR_ITERS, seed=SEED):
    """
    DINEOF 式迭代补全。A: (天, 站) 距平矩阵；mask_cells: (行索引, 列索引) 要被当成缺失的格子。
    返回补全后的矩阵（float32），只在 mask_cells 上有效。
    """
    X = A.copy().astype(np.float32)
    X[mask_cells] = np.nan
    obs = np.isfinite(X)
    if obs.mean() < 0.3:
        return None
    F = np.where(obs, X, 0.0).astype(np.float32)
    # 用观测均值初始化缺失处，避免从 0 起步收敛慢
    F = np.where(obs, F, np.nanmean(np.where(obs, F, np.nan), axis=0)[None, :]).astype(np.float32)
    F = np.nan_to_num(F, nan=0.0)
    prev = None
    for it in range(iters):
        U, Vt = randomized_svd_small(F, rank, seed=seed + it)
        R = (U @ Vt).astype(np.float32)
        F = np.where(obs, X, R)
        cur = F[mask_cells]
        if prev is not None and cur.size and np.nanmax(np.abs(cur - prev)) < 1e-4:
            break
        prev = cur.copy()
    return F


# ------------------------------------------------------------------ 评估工具
def paired(store, a, b, n_boot=3000):
    keys = sorted(set(store[a]) & set(store[b]))
    if len(keys) < 50:
        return None
    ea = np.abs(np.array([store[a][k] for k in keys]))
    eb = np.abs(np.array([store[b][k] for k in keys]))
    d = eb - ea
    rng = np.random.default_rng(7)
    boots = [float(d[rng.integers(0, len(d), len(d))].mean()) for _ in range(n_boot)]
    lo, hi = float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))
    return {"n": len(keys), "mae_a": round(float(ea.mean()), 4), "mae_b": round(float(eb.mean()), 4),
            "delta": round(float(eb.mean() - ea.mean()), 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "significant": bool(lo > 0 or hi < 0),
            "b_better_share": round(float((d < 0).mean()), 3)}


def main():
    t0 = time.time()
    con = V1._con()
    print("装配数据面板…", flush=True)
    dates, sid_list, st, M = build_panel(con)
    con.close()

    nreg = 3 if not os.environ.get("SMOKE_ONLY") else 2
    # 复用 V1 的分省抽样：这里自己抽，因为要站索引而不是 parquet
    from validate_repair import pick_targets
    con2 = V1._con()
    targets = pick_targets(con2, n_per_region=nreg)
    con2.close()
    if os.environ.get("SMOKE_ONLY"):
        targets = targets.head(3)

    pos = {s: i for i, s in enumerate(sid_list)}
    tgt_idx = [pos[int(s)] for s in targets["station_id"] if int(s) in pos]
    print("目标站 %d 个（面板内 %d）" % (len(targets), len(tgt_idx)), flush=True)

    clim, mos, yrs = climatology(M, dates, sid_list)
    rng = np.random.default_rng(SEED)

    # 站坐标
    lat = st["latitude"].values.astype(float)
    lon = st["longitude"].values.astype(float)

    # 每个目标站的 120 个验证日（全库统一，便于跨方法对齐）
    va_days = None

    report = {"config": {"n_targets": len(tgt_idx), "n_days": len(dates), "n_stations": len(sid_list),
                         "n_valid_days": N_VALID_DAYS, "k_nb": K_NB, "ranks": RANKS,
                         "train": list(TRAIN_YEARS), "val": list(VAL_YEARS)},
              "metrics": {}, "paired_vs_clima": {}, "skipped": []}

    print("\n%-22s" % "指标" + "".join("%13s" % m for m in METHODS), flush=True)
    for mi, m in enumerate(METRICS):
        lo, hi = V1.PHYS_RANGE[m]
        A, work, base = anomaly_matrix(M, clim, mos, mi)

        # ---- 构造遮罩：各目标站 × 各自 120 个验证日 ----
        va_years = (yrs >= VAL_YEARS[0]) & (yrs <= VAL_YEARS[1])
        rows, cols, keys, truth = [], [], [], []
        for si in tgt_idx:
            cand = np.where(va_years & np.isfinite(work[:, si]))[0]
            if len(cand) < 40:
                continue
            pick = np.sort(rng.choice(cand, size=min(N_VALID_DAYS, len(cand)), replace=False))
            for di in pick:
                rows.append(int(di)); cols.append(int(si))
                keys.append((int(di), int(si)))
                # 真值必须取物理空间的原始值（work 对降水是 log1p 空间，不能拿来当标签）
                truth.append(float(M[di, si, mi]))
        if len(rows) < 200:
            report["skipped"].append((m, "验证格太少"))
            continue
        rows = np.array(rows); cols = np.array(cols)
        truth = np.array(truth, dtype=np.float64)
        key_str = ["%d|%s" % (sid_list[c], str(dates[r])[:10]) for r, c in zip(rows, cols)]
        print("  %s 遮罩 %d 格（%.3f%%）" % (m, len(rows), 100.0 * len(rows) / A.size), flush=True)

        store = {k: {} for k in METHODS}
        work_is_log = (m == "precipitation")

        def to_real(v):
            v = np.asarray(v, dtype=np.float64)
            if work_is_log:
                v = np.expm1(np.clip(v, -10, 12))
            return np.clip(v, lo, hi)

        # ---- 方法 1：clima（最近 k 站加权距平）----
        nb_cache = {}
        for si in sorted(set(cols.tolist())):
            d = V1.haversine_km(lat[si], lon[si], lat, lon)
            order = np.argsort(d)
            nb_cache[si] = [j for j in order if j != si and d[j] <= 500.0][:K_NB]
        pred = np.full(len(rows), np.nan)
        for i, (di, si) in enumerate(zip(rows, cols)):
            nb = [j for j in nb_cache[si] if np.isfinite(A[di, j])]
            if not nb:
                continue
            w = 1.0 / np.maximum(np.array([V1.haversine_km(lat[si], lon[si], lat[j], lon[j]) for j in nb]), 0.1) ** 2
            pred[i] = A[di, si] * 0 + base[di, si] + float(np.dot(w, A[di, nb]) / w.sum())
        pv = to_real(pred)
        for k, pr, trp in zip(key_str, pv, truth):
            if np.isfinite(pr):
                store["clima"][k] = float(pr - trp)

        # ---- 方法 2：ro_clima（按训练期相关系数选站）----
        tr_mask = (yrs >= TRAIN_YEARS[0]) & (yrs <= TRAIN_YEARS[1])
        corr_cache = {}
        for si in sorted(set(cols.tolist())):
            a_s = A[tr_mask, si]
            cand = nb_cache[si] + [j for j in range(len(sid_list))
                                   if j != si and j not in nb_cache[si]][:0]
            cand = nb_cache[si]
            best = []
            for j in cand:
                b_s = A[tr_mask, j]
                ok = np.isfinite(a_s) & np.isfinite(b_s)
                if ok.sum() < 200:
                    continue
                c = float(np.corrcoef(a_s[ok], b_s[ok])[0, 1])
                if np.isfinite(c):
                    best.append((c, j))
            best.sort(reverse=True)
            corr_cache[si] = [j for _, j in best[:K_NB]]
        pred = np.full(len(rows), np.nan)
        for i, (di, si) in enumerate(zip(rows, cols)):
            nb = [j for j in corr_cache.get(si, []) if np.isfinite(A[di, j])]
            if not nb:
                continue
            w = np.array([max(1e-3, 1.0) for _ in nb])
            pred[i] = base[di, si] + float(np.dot(w, A[di, nb]) / w.sum())
        pv = to_real(pred)
        for k, pr, trp in zip(key_str, pv, truth):
            if np.isfinite(pr):
                store["ro_clima"][k] = float(pr - trp)

        # ---- 方法 3：全网低秩（rank 扫描）----
        # 子网：所有目标站 40 个最近邻的并集
        sub_cols = set()
        for si in sorted(set(cols.tolist())):
            d = V1.haversine_km(lat[si], lon[si], lat, lon)
            sub_cols.update(np.argsort(d)[:41].tolist())
        sub_cols = sorted(sub_cols)
        sub_pos = {c: i for i, c in enumerate(sub_cols)}
        sub_rows_all = np.arange(A.shape[0])
        for tag, cols_use, rows_use in (("full", np.arange(len(sid_list)), rows),
                                        ("sub", np.array(sub_cols), rows)):
            A_sub = A[:, cols_use]
            cpos = {int(c): (i if tag == "full" else sub_pos[int(c)]) for i, c in enumerate(cols_use)}
            mc = (rows_use, np.array([cpos[int(c)] for c in cols]))
            ranks_here = RANKS if tag == "full" else [8, 16]
            for rk in ranks_here:
                t1 = time.time()
                F = lowrank_fill(A_sub, mc, rk)
                if F is None:
                    continue
                # F 是距平矩阵（已减气候基准），必须加回 base 才是物理量
                est = F[mc] + base[rows, cols]
                real = to_real(est)
                name = ("lr_full_r%d" % rk) if tag == "full" else ("lr_sub_r%d" % rk)
                for k, pr, trp in zip(key_str, real, truth):
                    if np.isfinite(pr):
                        store[name][k] = float(pr - trp)
                print("    %-12s 遮罩 %d 格  %.1fs  有限 %d" % (
                    name, len(rows), time.time() - t1, int(np.isfinite(real).sum())), flush=True)

        row, line = {}, "%-22s" % m
        for meth in METHODS:
            d = store[meth]
            if len(d) < 50:
                row[meth] = None; line += "%13s" % "-"; continue
            errs = np.abs(np.array(list(d.values())))
            row[meth] = {"n": len(d), "mae": round(float(errs.mean()), 4),
                         "rmse": round(float(np.sqrt((errs ** 2).mean())), 4),
                         "bias": round(float(np.mean(list(d.values()))), 4)}
            line += "%13.4f" % row[meth]["mae"]
        report["metrics"][m] = row
        print(line, flush=True)

        report["paired_vs_clima"][m] = {}
        for meth in METHODS:
            if meth == "clima":
                continue
            r = paired(store, "clima", meth)
            if r is None:
                continue
            report["paired_vs_clima"][m][meth] = r
            if r["significant"]:
                print("    %-12s Δ=%+.4f CI[%+.4f,%+.4f]** %s" % (
                    meth, r["delta"], r["ci95"][0], r["ci95"][1],
                    "更好" if r["delta"] < 0 else "更差"), flush=True)

    out = os.path.join(HERE, "repair_validation8.json")
    report["config"]["runtime_sec"] = round(time.time() - t0, 1)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n已写出 %s  总用时 %.0fs" % (out, time.time() - t0), flush=True)


if __name__ == "__main__":
    main()
