# -*- coding: utf-8 -*-
"""
第十一轮：真实坏值的形态普查 —— 决定 own6（跨要素）到底该不该用

**为什么必须做这一轮**：
第九轮「多要素联合插补」（own6_rr）在 single 遮蔽下把气温 MAE 从 0.866 压到 0.378（-56%），
第十轮立刻证伪：一旦同一天整行都是坏值（row 模式），它反而比 clima 差 85%。
所以「跨要素信息有没有用」完全取决于**真实数据里坏值是什么形态**：
  - 若绝大多数坏值只是单个指标坏、同行其它要素是好的  → own6 值钱，应落地
  - 若绝大多数坏值是整行同时坏（哨兵值灌满一行）        → own6 无用，只有 clima 可用
  - 若是整段停更                                        → 只有气候态可用

本轮不训练任何模型，只对**原始镜像数据**做形态普查：
  1. 逐个 (站, 日, 指标) 判定是否为坏值（超 PHYS_RANGE 或 NULL）
  2. 对每个坏格，数同一 (站, 日) 下其它 6 个指标里有几个是好的
  3. 统计分布：坏格中有「≥1 个好要素」/「≥4 个好要素」的比例
  4. 按指标、按坏值类型（哨兵值 vs NULL vs 缺失行）分别统计
  5. 另外统计「整行缺」（该站该日 7 个指标全无有效值）的占比

输出 `D:\Android PDF\repair_badcell_shapes.json` / `validate_repair11.out.txt`
用法：
    python validate_repair11.py
"""
import os
import sys
import json
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import validate_repair as V1

METRICS = V1.METRICS
SENTINEL_HINT = 32000.0     # 库里常见的哨兵值量级（32700/32001/32766/99999/999999 等）


def main():
    t0 = time.time()
    print("第十一轮：真实坏值形态普查", flush=True)
    con = V1._con()
    parquet = os.path.join(HERE, "db_mirror", "tdengine", "weather_data", "*.parquet").replace("\\", "/")
    st_path = os.path.join(HERE, "db_mirror", "mysql", "weather_station_info.parquet").replace("\\", "/")

    # 与 validate_repair8 相同的闰日修正视图
    con.execute("""
        CREATE OR REPLACE VIEW wh AS
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS station_id,
               CAST(CAST(ts_bj AS DATE) - INTERVAL 100 YEAR AS DATE)
                   + (CASE WHEN hour(ts_bj) = 12 THEN 1 ELSE 0 END) AS date_true,
               avg_temperature, max_temperature, min_temperature, avg_humidity,
               precipitation, avg_wind_speed, sunshine_hours
        FROM read_parquet('%s', filename=true)
    """ % parquet)
    con.execute("""
        CREATE OR REPLACE TABLE st AS
        SELECT station_id FROM read_parquet('%s')
    """ % st_path)

    cols = ", ".join(m for m in METRICS)
    print("读取全量…", flush=True)
    df = con.execute("""
        SELECT station_id, date_true, %s
        FROM wh
        WHERE CAST(CAST(ts_bj AS DATE) - INTERVAL 100 YEAR AS DATE)
                  + (CASE WHEN hour(ts_bj) = 12 THEN 1 ELSE 0 END) BETWEEN DATE '1951-01-01' AND DATE '2025-12-31'
    """ % cols).df() if False else con.execute("""
        SELECT station_id, date_true, %s FROM wh
    """ % cols).df()
    con.close()
    print("  行数 %d，站点 %d" % (len(df), df["station_id"].nunique()), flush=True)

    # 判 7 指标的有效性；同时区分「哨兵值」与「NULL」
    v = {}
    sent = {}
    for m in METRICS:
        lo, hi = V1.PHYS_RANGE[m]
        s = df[m].to_numpy(dtype=np.float64)
        isnull = df[m].isna().to_numpy()
        inrange = (s >= lo) & (s <= hi) & (~isnull)
        v[m] = inrange
        sent[m] = (~isnull) & (np.abs(s) >= SENTINEL_HINT)
    V = np.column_stack([v[m] for m in METRICS])          # (n行, 7) bool
    n_good = V.sum(axis=1)                                 # 该行有几个指标是好的
    row_has_null = {m: df[m].isna().to_numpy() for m in METRICS}

    print("\n=== 行的整体形态（%d 行）===" % len(df), flush=True)
    row_shape = {}
    for k in range(8):
        cnt = int((n_good == k).sum())
        row_shape[k] = cnt
        print("  该行有 %d 个好指标：%10d 行（%.3f%%）"
              % (k, cnt, 100.0 * cnt / len(df)), flush=True)

    print("\n=== 坏格（某行某指标坏）中，同行其它要素的可用性 ===", flush=True)
    per_metric = {}
    for mi, m in enumerate(METRICS):
        bad = ~V[:, mi]
        nb = int(bad.sum())
        if nb == 0:
            continue
        others_good = np.column_stack([V[:, j] for j in range(len(METRICS)) if j != mi]).sum(axis=1)
        og = others_good[bad]
        rec = {
            "bad_cells": nb,
            "bad_share_all": round(nb / len(df) * 100, 4),
            "null_cells": int(row_has_null[m][bad].sum()),
            "sentinel_cells": int(sent[m][bad].sum()),
            "with_ge1_other_good": int((og >= 1).sum()),
            "with_ge4_other_good": int((og >= 4).sum()),
            "with_all6_other_good": int((og >= 6).sum()),
            "with_zero_other_good": int((og == 0).sum()),
        }
        for key in ("with_ge1_other_good", "with_ge4_other_good", "with_all6_other_good",
                    "with_zero_other_good"):
            rec[key + "_pct"] = round(100.0 * rec[key] / nb, 2)
        rec["mean_other_good"] = round(float(og.mean()), 3)
        per_metric[m] = rec
        print("  %-16s 坏格 %9d | 同行≥1好 %5.2f%% | ≥4好 %5.2f%% | 6好 %5.2f%% | 全坏 %5.2f%% | 均好 %.2f 个"
              % (m, nb, rec["with_ge1_other_good_pct"], rec["with_ge4_other_good_pct"],
                 rec["with_all6_other_good_pct"], rec["with_zero_other_good_pct"],
                 rec["mean_other_good"]), flush=True)

    # 只统计「哨兵值」坏格（这是最典型的坏值形态，也是最该修的）
    print("\n=== 仅看哨兵值坏格（|值|≥32000）===", flush=True)
    sent_only = {}
    for mi, m in enumerate(METRICS):
        bad = sent[m]
        nb = int(bad.sum())
        if nb == 0:
            continue
        others_good = np.column_stack([V[:, j] for j in range(len(METRICS)) if j != mi]).sum(axis=1)
        og = others_good[bad]
        sent_only[m] = {
            "bad_cells": nb,
            "with_ge1_other_good_pct": round(100.0 * float((og >= 1).mean()), 2),
            "with_ge4_other_good_pct": round(100.0 * float((og >= 4).mean()), 2),
            "with_zero_other_good_pct": round(100.0 * float((og == 0).mean()), 2),
            "mean_other_good": round(float(og.mean()), 3),
        }
        print("  %-16s 哨兵 %8d | ≥1好 %6.2f%% | ≥4好 %6.2f%% | 全坏 %6.2f%% | 均好 %.2f"
              % (m, nb, sent_only[m]["with_ge1_other_good_pct"],
                 sent_only[m]["with_ge4_other_good_pct"],
                 sent_only[m]["with_zero_other_good_pct"],
                 sent_only[m]["mean_other_good"]), flush=True)

    # 另一个决定性问题：坏值的「时间聚集性」—— 孤立日 vs 连续段
    print("\n=== 坏格的时间聚集性（孤立 vs 连续）===", flush=True)
    d = df.copy()
    d["_good"] = V.sum(axis=1) >= 6          # 该行基本完好
    agg = {}
    for m in METRICS:
        lo, hi = V1.PHYS_RANGE[m]
        d["_bad"] = ~v[m]
        g = d.groupby("station_id")["_bad"].sum()
        tot = d.groupby("station_id").size()
        agg[m] = {"stations_with_any_bad": int((g > 0).sum()),
                  "stations_total": int(len(tot)),
                  "bad_rows": int(g.sum())}
    for m, a in agg.items():
        print("  %-16s 有坏值的站 %3d/%d，坏行合计 %d"
              % (m, a["stations_with_any_bad"], a["stations_total"], a["bad_rows"]), flush=True)

    report = {
        "total_rows": int(len(df)),
        "stations": int(df["station_id"].nunique()),
        "row_shape_by_n_good": row_shape,
        "per_metric_badcell": per_metric,
        "sentinel_badcell": sent_only,
        "per_metric_station_reach": agg,
        "conclusion_hint": (
            "若 per_metric_badcell[*].with_ge4_other_good_pct 很高 → 跨要素法(own6)有价值；"
            "若 with_zero_other_good_pct 很高 → 只有 clima 可用。"
        ),
    }
    out = os.path.join(HERE, "repair_badcell_shapes.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("\n总用时 %.0fs，已写出 %s" % (time.time() - t0, out), flush=True)


if __name__ == "__main__":
    main()
