# -*- coding: utf-8 -*-
"""端到端验证：对 `weather_api/data.py` 里**真实的** repair_daily() 做
「注入坏值 → 修复 → 比对真值」实验。

设计要点
--------
1. 隔离沙箱 `_e2e_sandbox/`：只复制 30 个靶站 + 它们真正的 top-20 邻站 parquet，
   外加全量 weather_station_info.parquet。**只对 30 个靶站注入**，邻站始终保持原始
   干净值 —— 这样「邻站替補」的输入是可信真值，评估无污染。
2. 注入 3 类坏值（超上限哨兵 / 超下限哨兵 / NULL），每站每指标各 1 条。
   `injected.json` 记录 (station_id, date, metric, true_value, kind)。
3. 用**环境变量** + 独立子进程跑真实 `data.repair_daily`（import 时读 env，
   同进程不能切数据集），并在子进程里直接查 `D._df` 证明读到的是沙箱。
4. 评估：按 (station_id, date, metric) 对齐 repaired 与 true_value，
   算 MAE/RMSE/偏差/恢复率，并按 quality 分档；另算「直接取最近站值」对照。
5. 反向对照：同一沙箱的**干净副本**再跑一遍，验证 bad_rows 增量与注入量吻合。

用法（无参数 = 全流程）：
    D:\\anaconda\\python.exe validate_e2e.py
分阶段：
    ... validate_e2e.py plan      # 只打印选站方案
    ... validate_e2e.py prepare   # 建沙箱副本
    ... validate_e2e.py inject    # 注入 + 写 injected.json
    ... validate_e2e.py run --tag injected|control   # 子进程：跑真实 repair_daily
    ... validate_e2e.py eval      # 汇总 → repair_e2e.json
"""
import os
import sys
import json
import math
import shutil
import argparse
import subprocess
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
MIRROR = os.path.join(HERE, "db_mirror")
TD_REAL = os.path.join(MIRROR, "tdengine", "weather_data").replace("\\", "/")
MY_REAL = os.path.join(MIRROR, "mysql").replace("\\", "/")

SB = os.path.join(HERE, "_e2e_sandbox")
SB_TD = os.path.join(SB, "tdengine", "weather_data")          # 注入后的数据集
SB_MY = os.path.join(SB, "mysql")
SB_CTRL_TD = os.path.join(SB, "control", "weather_data")       # 干净对照数据集
SB_PRISTINE = os.path.join(SB, "pristine")                     # 原始副本（只读参照）

INJECTED_JSON = os.path.join(HERE, "injected.json")
RESULT_JSON = os.path.join(HERE, "repair_e2e.json")
PLAN_JSON = os.path.join(SB, "plan.json")

WIN = ("2016-01-01", "2019-12-31")     # 4 年窗口：靶站在此窗口内数据完整
N_TARGETS = 30
N_PER_ZONE = 6
K = 12                                 # repair_daily 默认邻居数
RADIUS_KM = 300.0                      # repair_daily 默认搜索半径
METRICS = ["avg_temperature", "max_temperature", "min_temperature", "avg_humidity",
           "precipitation", "avg_wind_speed", "sunshine_hours"]
# 与 data.PHYS_RANGE 一致（在 run 阶段还会再断言一次）
PHYS = {"avg_temperature": (-100.0, 100.0), "max_temperature": (-100.0, 100.0),
        "min_temperature": (-100.0, 100.0), "avg_humidity": (0.0, 100.0),
        "precipitation": (0.0, 1000.0), "avg_wind_speed": (0.0, 200.0),
        "sunshine_hours": (0.0, 24.0)}
# 与 data._ROUND 一致（run 阶段会断言）
ROUND_DIGITS = {"avg_temperature": 1, "max_temperature": 1, "min_temperature": 1,
                "avg_humidity": 1, "precipitation": 1, "avg_wind_speed": 2,
                "sunshine_hours": 2}
ZONES = ["华南", "长江中下游", "西南山地", "华北/黄土", "北方/高原干旱半干旱"]


# ----------------------------------------------------------------- 基础设施
def _log(msg):
    print(msg, flush=True)


def _duck():
    import duckdb
    c = duckdb.connect()
    c.execute("SET threads TO 8")
    return c


def _zone(province):
    p = str(province)
    if any(x in p for x in ("黑龙江", "吉林", "辽宁", "内蒙古", "新疆", "青海", "西藏", "甘肃")):
        return "北方/高原干旱半干旱"
    if any(x in p for x in ("北京", "天津", "河北", "山西", "山东", "河南", "陕西", "宁夏")):
        return "华北/黄土"
    if any(x in p for x in ("上海", "江苏", "浙江", "安徽", "湖北", "湖南", "江西", "福建", "台湾")):
        return "长江中下游"
    if any(x in p for x in ("广东", "广西", "海南", "香港", "澳门")):
        return "华南"
    return "西南山地"


def _haversine_matrix(lat, lon):
    la = np.radians(np.asarray(lat, dtype=float))
    lo = np.radians(np.asarray(lon, dtype=float))
    dlat = la[:, None] - la[None, :]
    dlon = lo[:, None] - lo[None, :]
    h = np.sin(dlat / 2) ** 2 + np.cos(la)[:, None] * np.cos(la)[None, :] * np.sin(dlon / 2) ** 2
    return 2 * 6371.0 * np.arcsin(np.minimum(1.0, np.sqrt(h)))


def _load_stations():
    c = _duck()
    st = c.execute("SELECT station_id, station_code, province, station_name, latitude, longitude, altitude "
                   "FROM read_parquet('%s/weather_station_info.parquet')" % MY_REAL).df()
    c.close()
    st["sid"] = st["station_id"].astype(int)
    for col in ("latitude", "longitude", "altitude"):
        st[col] = st[col].astype(float)
    return st


def _parquet_ids():
    return {int(f[8:-8]) for f in os.listdir(TD_REAL) if f.startswith("station_") and f.endswith(".parquet")}


# ----------------------------------------------------------------- plan
def phase_plan():
    """按气候带分层选 30 个靶站（每带 6 个，带内取最密集的一簇），
    并算出需要复制进沙箱的站集合（靶站 + 每个靶站真正的 top-20 邻站）。"""
    st = _load_stations()
    pids = _parquet_ids()
    st = st[st.sid.isin(pids)].dropna(subset=["latitude", "longitude"]).reset_index(drop=True)
    st["zone"] = st.province.map(_zone)

    # 窗口数据完整度
    c = _duck()
    cw = c.execute("""
      SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS sid, count(*) AS n
      FROM read_parquet('%s/*.parquet', filename=true) w
      WHERE CAST(ts_bj - INTERVAL 100 YEAR AS DATE) BETWEEN DATE '%s' AND DATE '%s'
      GROUP BY 1
    """ % (TD_REAL, WIN[0], WIN[1])).df()
    c.close()
    ndays = (pd.Timestamp(WIN[1]) - pd.Timestamp(WIN[0])).days + 1
    full_ids = set(cw[cw.n >= ndays - 2].sid.astype(int))
    _log("[plan] 镜像 %d 个站有 parquet；窗口 %s~%s 共 %d 天，其中 %d 个站数据完整（>=%d 天）"
         % (len(pids), WIN[0], WIN[1], ndays, len(full_ids), ndays - 2))

    D = _haversine_matrix(st.latitude.values, st.longitude.values)
    np.fill_diagonal(D, 1e9)

    # 每带：取「到自己 12 近邻平均距离最小」的站作为簇心，簇 = 它 + 12 近邻。
    # 注意 Dz 的索引是**带内候选的局部下标**，必须用 loc[] 映射回 st 的全局行号。
    targets, used = [], set()
    for z in ZONES:
        cand_loc = np.array([i for i in range(len(st))
                             if st.zone.iat[i] == z and int(st.sid.iat[i]) in full_ids])
        if len(cand_loc) < N_PER_ZONE:
            _log("[plan] !! 气候带 %s 完整站不足：%d" % (z, len(cand_loc)))
            continue
        Dz = D[np.ix_(cand_loc, cand_loc)]
        o = np.argsort(Dz, axis=1)                       # 局部下标
        dens = Dz[np.arange(len(Dz))[:, None], o[:, :12]].mean(1)
        core_l = int(np.argmin(dens))
        ring_l = [core_l] + [int(k) for k in o[core_l][:29]]   # 簇心 + 29 近邻（局部）
        # 挑 6 个：优先不同省份，再补足
        picks = []
        provs = set()
        for k in ring_l:
            g = int(cand_loc[k])
            if int(st.sid.iat[g]) in used:
                continue
            if st.province.iat[g] not in provs:
                picks.append(g); provs.add(st.province.iat[g])
            if len(picks) >= N_PER_ZONE:
                break
        for k in ring_l:
            g = int(cand_loc[k])
            if len(picks) >= N_PER_ZONE:
                break
            if g not in picks and int(st.sid.iat[g]) not in used:
                picks.append(g)
        if len(picks) < N_PER_ZONE:
            _log("[plan] !! 气候带 %s 可选靶站不足：%d" % (z, len(picks)))
            continue
        used |= {int(st.sid.iat[g]) for g in picks}
        core_g = int(cand_loc[core_l])
        targets += [int(st.sid.iat[g]) for g in picks]
        _log("[plan] %-12s 簇心 %s(%s) 簇内 30 站距簇心中位 %.0f km → 靶站 %s"
             % (z, st.station_name.iat[core_g], st.province.iat[core_g],
                float(np.median(Dz[core_l, o[core_l][:13]])),
                [(int(st.sid.iat[g]), st.province.iat[g], st.station_name.iat[g]) for g in picks]))
    targets = list(dict.fromkeys(targets))
    assert len(targets) == N_TARGETS, "靶站数不足 30：%d" % len(targets)

    # 需要复制的站：靶站 + 每个靶站真正的 top-20 邻站（只算有 parquet 的）
    have_rows = {int(s): i for i, s in enumerate(st.sid)}
    copied, fidelity = set(targets), {}
    for t in targets:
        i = have_rows[t]
        nb20 = [int(st.sid[j]) for j in np.argsort(D[i])[:20] if int(st.sid[j]) in pids]
        copied |= set(nb20)
        real12 = [int(st.sid[j]) for j in np.argsort(D[i])[:K]]
        fidelity[t] = {"real_top12": real12,
                       "real_top12_with_parquet": [s for s in real12 if s in pids]}
    copied = sorted(copied)
    missing = {t: [s for s in v["real_top12_with_parquet"] if s not in copied] for t, v in fidelity.items()}
    missing = {t: v for t, v in missing.items() if v}
    _log("[plan] 靶站 %d 个；需复制 parquet %d 个（靶站 + top-20 邻站并集）" % (len(targets), len(copied)))
    _log("[plan] 邻站保真：real_top12 中「有 parquet 但未复制」的站 = %d 个 → %s"
         % (sum(len(v) for v in missing.values()), "完全一致" if not missing else missing))

    tsub = st[st.sid.isin(targets)]
    _log("[plan] 靶站省份 %s" % tsub.province.value_counts().to_dict())
    _log("[plan] 靶站海拔 %.0f~%.0f m，纬度 %.1f~%.1f，经度 %.1f~%.1f"
         % (tsub.altitude.min(), tsub.altitude.max(), tsub.latitude.min(),
            tsub.latitude.max(), tsub.longitude.min(), tsub.longitude.max()))

    plan = {"targets": targets, "copied": copied, "window": list(WIN), "k": K,
            "radius_km": RADIUS_KM, "ndays": ndays,
            "fidelity": {str(k): v for k, v in fidelity.items()},
            "fidelity_missing": {str(k): v for k, v in missing.items()},
            "target_meta": [{"station_id": int(r.sid), "station_code": r.station_code,
                             "province": r.province, "station_name": r.station_name,
                             "latitude": r.latitude, "longitude": r.longitude,
                             "altitude": r.altitude, "zone": r.zone}
                            for r in tsub.sort_values("sid").itertuples()]}
    os.makedirs(SB, exist_ok=True)
    json.dump(plan, open(PLAN_JSON, "w", encoding="utf-8"), ensure_ascii=False)
    _log("[plan] → %s" % PLAN_JSON)
    return plan


# ----------------------------------------------------------------- prepare
def phase_prepare():
    plan = json.load(open(PLAN_JSON, encoding="utf-8"))
    copied = plan["copied"]
    for d in (SB_TD, SB_MY, SB_CTRL_TD, SB_PRISTINE):
        os.makedirs(d, exist_ok=True)

    # 原始副本（pristine）
    n = 0
    for sid in copied:
        name = "station_%d.parquet" % sid
        src = os.path.join(TD_REAL, name)
        dst = os.path.join(SB_PRISTINE, name)
        if not (os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src)):
            shutil.copy2(src, dst)
            n += 1
    _log("[prepare] 复制 %d 个 parquet 到 pristine（共 %d 个文件，%.1f MB）"
         % (n, len(copied), sum(os.path.getsize(os.path.join(SB_PRISTINE, f))
                                for f in os.listdir(SB_PRISTINE)) / 1e6))

    # 干净对照集
    for sid in copied:
        name = "station_%d.parquet" % sid
        shutil.copy2(os.path.join(SB_PRISTINE, name), os.path.join(SB_CTRL_TD, name))
    # 待注入集（先复制成干净状态，inject 阶段再覆盖坏值）
    for sid in copied:
        name = "station_%d.parquet" % sid
        shutil.copy2(os.path.join(SB_PRISTINE, name), os.path.join(SB_TD, name))
    # 站点表全量
    shutil.copy2(os.path.join(MY_REAL, "weather_station_info.parquet"),
                 os.path.join(SB_MY, "weather_station_info.parquet"))
    _log("[prepare] 沙箱就绪：\n  %s\n  %s\n  %s" % (SB_TD, SB_CTRL_TD, SB_MY))
    return plan


# ----------------------------------------------------------------- inject
# 每站选 4 个「原始整行干净、彼此间隔 >=14 天」的日期，各施加一种坏值；
# 每个坏值日把 **全部 7 个指标** 一起破坏 → 每个 (站, 指标) 都覆盖到全部 4 类。
KIND_SPEC = [
    ("high", 999999),    # 超上限哨兵（999999）
    ("high2", 32766),    # 另一超上限哨兵（32766 缺测码）
    ("low", -999),       # 下限哨兵
    ("null", None),      # 置 NULL（模拟空白）
]
MIN_GAP_DAYS = 14


def _clean_condition():
    """与 data.PHYS_RANGE / repair_scan 同口径的「该行完全干净」SQL 条件。"""
    cs = ["%s BETWEEN %f AND %f" % (m, PHYS[m][0], PHYS[m][1]) for m in METRICS]
    cs.append("max_temperature >= min_temperature")
    return " AND ".join(cs)


def phase_inject():
    plan = json.load(open(PLAN_JSON, encoding="utf-8"))
    targets = plan["targets"]
    rng = np.random.default_rng(20240117)
    c = _duck()

    # 1) 各站「原始干净日」列表（行存在、7 指标全在物理范围、max>=min）
    src_glob = SB_PRISTINE.replace("\\", "/") + "/*.parquet"
    cl = c.execute("""
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS sid,
               CAST(ts_bj - INTERVAL 100 YEAR AS DATE) AS d, count(*) AS n
        FROM read_parquet('%s', filename=true)
        WHERE CAST(ts_bj - INTERVAL 100 YEAR AS DATE) BETWEEN DATE '%s' AND DATE '%s'
          AND %s
        GROUP BY 1, 2 HAVING count(*) = 1
    """ % (src_glob, WIN[0], WIN[1], _clean_condition())).df()
    cl["d"] = cl["d"].astype(str).str.slice(0, 10)
    clean_days = {int(s): sorted(g["d"]) for s, g in cl.groupby("sid")}

    # 2) 选 4 个间隔 >=14 天的干净日 + 记录真值
    injections, inj_map = [], {}
    for sid in targets:
        pool = clean_days.get(int(sid), [])
        assert len(pool) >= 40, "站 %d 原始干净日太少（%d），无法注入" % (sid, len(pool))
        order = list(range(len(pool)))
        rng.shuffle(order)
        picks, last = [], None
        for i in order:
            t = pd.Timestamp(pool[i])
            if last is not None and abs((t - last).days) < MIN_GAP_DAYS:
                continue
            picks.append(pool[i])
            last = t
            if len(picks) == len(KIND_SPEC):
                break
        gap = MIN_GAP_DAYS
        while len(picks) < len(KIND_SPEC):          # 兜底：逐步放宽间隔
            gap = max(1, gap // 2)
            picks, last = [], None
            for i in order:
                t = pd.Timestamp(pool[i])
                if last is not None and abs((t - last).days) < gap:
                    continue
                picks.append(pool[i])
                last = t
                if len(picks) == len(KIND_SPEC):
                    break
        assert len(picks) == len(KIND_SPEC), "站 %d 选不到 %d 个注入日" % (sid, len(KIND_SPEC))
        picks = sorted(picks)
        inj_map[int(sid)] = picks

        got = c.execute("""
            SELECT CAST(ts_bj - INTERVAL 100 YEAR AS DATE) AS d, %s
            FROM read_parquet('%s/station_%d.parquet')
            WHERE CAST(ts_bj - INTERVAL 100 YEAR AS DATE) IN (%s)
        """ % (", ".join(METRICS), SB_PRISTINE.replace("\\", "/"), int(sid),
               ",".join("DATE '%s'" % x for x in picks))).df()
        got["d"] = got["d"].astype(str).str.slice(0, 10)
        gi = got.set_index("d")
        for d, (kind, sent) in zip(picks, KIND_SPEC):
            row = gi.loc[d]
            for m in METRICS:
                injections.append({"station_id": int(sid), "date": d, "metric": m,
                                   "true_value": round(float(row[m]), 3),
                                   "kind": kind, "injected_value": sent})

    _log("[inject] 计划注入 %d 条 = %d 站 × %d 坏值日 × %d 指标（每 (站,指标) 覆盖 %s）"
         % (len(injections), len(targets), len(KIND_SPEC), len(METRICS),
            [k for k, _ in KIND_SPEC]))

    # 3) 写回沙箱：逐站 COPY，CASE 只改这 4 天，其余列/类型原样
    by_station = {}
    for rec in injections:
        by_station.setdefault(rec["station_id"], {}).setdefault(rec["metric"], []).append(rec)

    for sid in targets:
        src = os.path.join(SB_PRISTINE, "station_%d.parquet" % sid).replace("\\", "/")
        dst = os.path.join(SB_TD, "station_%d.parquet" % sid).replace("\\", "/")
        types = {r[0]: r[1] for r in
                 c.execute("DESCRIBE SELECT * FROM read_parquet('%s')" % src).fetchall()}
        sel = []
        for m in METRICS:
            is_int = "INT" in types[m].upper()          # avg_humidity 是 BIGINT
            whens = []
            for r in by_station[sid][m]:
                if r["injected_value"] is None:
                    lit = "CAST(NULL AS %s)" % ("BIGINT" if is_int else "DOUBLE")
                else:
                    lit = ("%d" % int(r["injected_value"])) if is_int \
                        else ("%.1f" % float(r["injected_value"]))
                whens.append("WHEN _e2e_d = DATE '%s' THEN %s" % (r["date"], lit))
            sel.append("CASE %s ELSE %s END AS %s" % (" ".join(whens), m, m))
        cols = ", ".join(["ts", "ts_bj"] + sel + ["file_path", "user_id", "user_nickname"])
        c.execute("""
            COPY (
              SELECT %s FROM (
                SELECT *, CAST(ts_bj - INTERVAL 100 YEAR AS DATE) AS _e2e_d
                FROM read_parquet('%s')
              )
            ) TO '%s' (FORMAT PARQUET)
        """ % (cols, src, dst))

    # 4) 校验：行数不变 / 列类型不变 / 逐条坏值确实落盘
    n_src = c.execute("SELECT count(*) FROM read_parquet('%s')" % src_glob).fetchone()[0]
    n_dst = c.execute("SELECT count(*) FROM read_parquet('%s/*.parquet')"
                      % SB_TD.replace("\\", "/")).fetchone()[0]
    _log("[inject] 行数 pristine=%d 注入后=%d %s"
         % (n_src, n_dst, "一致" if n_src == n_dst else "!! 不一致"))
    assert n_src == n_dst, "注入前后行数不一致"

    type_bad = []
    for sid in targets[:5]:
        t1 = {r[0]: r[1] for r in c.execute(
            "DESCRIBE SELECT * FROM read_parquet('%s/station_%d.parquet')"
            % (SB_PRISTINE.replace("\\", "/"), sid)).fetchall()}
        t2 = {r[0]: r[1] for r in c.execute(
            "DESCRIBE SELECT * FROM read_parquet('%s/station_%d.parquet')"
            % (SB_TD.replace("\\", "/"), sid)).fetchall()}
        if t1 != t2:
            type_bad.append((sid, {k: (t1.get(k), t2.get(k)) for k in set(t1) | set(t2)
                                   if t1.get(k) != t2.get(k)}))
    _log("[inject] 列类型校验（抽查 5 站）：%s" % ("全部一致" if not type_bad else type_bad))
    assert not type_bad, "注入改变了 parquet 列类型"

    seen = 0
    inj_df = pd.DataFrame(injections)
    for sid, g in inj_df.groupby("station_id"):
        back = c.execute("""
            SELECT CAST(ts_bj - INTERVAL 100 YEAR AS DATE) AS d, %s
            FROM read_parquet('%s/station_%d.parquet')
            WHERE CAST(ts_bj - INTERVAL 100 YEAR AS DATE) IN (%s)
        """ % (", ".join(METRICS), SB_TD.replace("\\", "/"), int(sid),
               ",".join("DATE '%s'" % x for x in g["date"].unique()))).df()
        back["d"] = back["d"].astype(str).str.slice(0, 10)
        gi = back.set_index("d")
        for r in g.itertuples():
            cur = gi.loc[r.date][r.metric]
            if r.kind == "null":
                assert pd.isna(cur), "站 %d %s %s 未置空（= %r）" % (r.station_id, r.date,
                                                                   r.metric, cur)
            else:
                assert abs(float(cur) - float(r.injected_value)) < 1e-6, \
                    "站 %d %s %s 哨兵不符：%r vs %r" % (r.station_id, r.date, r.metric,
                                                       cur, r.injected_value)
            seen += 1
    _log("[inject] 逐条回读校验 %d/%d 通过" % (seen, len(injections)))
    assert seen == len(injections)

    json.dump(injections, open(INJECTED_JSON, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    json.dump({str(k): v for k, v in inj_map.items()},
              open(os.path.join(SB, "injected_dates.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    _log("[inject] 注入 %d 条，覆盖 %d 个坏值日 → %s"
         % (len(injections), sum(len(v) for v in inj_map.values()), INJECTED_JSON))
    return injections

# ----------------------------------------------------------------- run（子进程）
def phase_run(tag):
    """在已经设好 WEATHER_TD_GLOB / WEATHER_MY_DIR 的进程里跑真实 data.repair_daily。"""
    sys.path.insert(0, HERE)
    from weather_api import data as D

    td = D.TD_GLOB.replace("\\", "/")
    my = D.MY_DIR.replace("\\", "/")
    assert td.startswith(SB.replace("\\", "/")), "未指向沙箱：%s" % D.TD_GLOB
    assert my.startswith(SB.replace("\\", "/")), "未指向沙箱：%s" % D.MY_DIR
    assert D.PHYS_RANGE == {m: PHYS[m] for m in METRICS}, "PHYS_RANGE 与脚本不一致"
    assert D._ROUND == ROUND_DIGITS, "_ROUND 与脚本不一致：%s" % D._ROUND
    probe = D._df("SELECT count(*) n, count(DISTINCT station_id) s FROM weather_hist")
    _log("[run:%s] data.py 视图 → %s" % (tag, D.TD_GLOB))
    _log("[run:%s] 沙箱内 weather_hist: %d 行 / %d 站"
         % (tag, int(probe["n"][0]), int(probe["s"][0])))

    plan = json.load(open(PLAN_JSON, encoding="utf-8"))
    inj = {}
    if tag == "injected":
        inj = {r["station_id"]: r for r in json.load(open(INJECTED_JSON, encoding="utf-8"))}
        # 直接查沙箱，确认看到坏值（这是「确实读的是沙箱」的硬证据）
        first = sorted(set(inj))
        hits = 0
        for sid in first[:5]:
            k = D._df("""
                SELECT count(*) n FROM weather_hist
                WHERE station_id = %d AND CAST(ts_bj - INTERVAL 100 YEAR AS DATE)
                      BETWEEN DATE '%s' AND DATE '%s'
                  AND (avg_temperature > 100 OR avg_humidity > 100 OR precipitation > 1000
                       OR sunshine_hours > 24 OR avg_wind_speed > 200
                       OR avg_temperature < -100 OR precipitation < 0 OR sunshine_hours < 0)
            """ % (sid, WIN[0], WIN[1]))
            hits += int(k["n"][0])
        _log("[run:%s] 抽查 5 站的超范围坏值行数（沙箱可见）= %d" % (tag, hits))
        assert hits > 0, "沙箱里看不到注入的坏值！"

    out = {"tag": tag, "td_glob": D.TD_GLOB, "my_dir": D.MY_DIR,
           "sandbox_rows": int(probe["n"][0]), "sandbox_stations": int(probe["s"][0]),
           "stations": []}
    t0 = time.time()
    for i, sid in enumerate(plan["targets"], 1):
        r = D.repair_daily(sid, start=WIN[0], end=WIN[1], method="auto",
                           allow_climate_fill=False)
        out["stations"].append({"station": r["station"], "period": r["period"],
                                "method": r["method"], "k": r["k"],
                                "radius_km": r["radius_km"],
                                "neighbors": r["neighbors"],
                                "summary": r["summary"], "rows": r["rows"]})
        _log("[run:%s] %2d/%d station %d  bad_rows=%d repaired=%d by_quality=%s"
             % (tag, i, len(plan["targets"]), sid, r["summary"]["bad_rows"],
                r["summary"]["repaired_values"], r["summary"]["by_quality"]))
    out["elapsed_sec"] = round(time.time() - t0, 1)
    path = os.path.join(SB, "out_%s.json" % tag)
    json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    _log("[run:%s] → %s（%.1fs）" % (tag, path, time.time() - t0))
    return out


# ----------------------------------------------------------------- eval
def _err_stats(errs):
    if not errs:
        return {"n": 0}
    e = np.asarray(errs, dtype=float)
    return {"n": int(e.size), "mae": round(float(np.mean(np.abs(e))), 3),
            "rmse": round(float(math.sqrt(np.mean(e ** 2))), 3),
            "bias": round(float(np.mean(e)), 3),
            "p50_abs": round(float(np.median(np.abs(e))), 3),
            "p90_abs": round(float(np.percentile(np.abs(e), 90)), 3),
            "max_abs": round(float(np.max(np.abs(e))), 3)}


def phase_eval():
    plan = json.load(open(PLAN_JSON, encoding="utf-8"))
    injections = json.load(open(INJECTED_JSON, encoding="utf-8"))
    out_inj = json.load(open(os.path.join(SB, "out_injected.json"), encoding="utf-8"))
    out_ctl = json.load(open(os.path.join(SB, "out_control.json"), encoding="utf-8"))

    # ---- 靶站元数据 / 邻站
    meta = {int(m["station_id"]): m for m in plan["target_meta"]}
    nb_by_station = {int(s["station"]["station_id"]): s["neighbors"] for s in out_inj["stations"]}

    # ---- 邻站真值（从 pristine 取，邻站从未被注入）
    c = _duck()
    copied = plan["copied"]
    raw = c.execute("""
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS station_id,
               CAST(ts_bj - INTERVAL 100 YEAR AS DATE) AS d,
               avg_temperature, max_temperature, min_temperature, avg_humidity,
               precipitation, avg_wind_speed, sunshine_hours
        FROM read_parquet('%s/*.parquet', filename=true)
        WHERE CAST(ts_bj - INTERVAL 100 YEAR AS DATE) BETWEEN DATE '%s' AND DATE '%s'
    """ % (SB_PRISTINE.replace("\\", "/"), WIN[0], WIN[1])).df()
    raw["d"] = raw["d"].astype(str).str.slice(0, 10)
    # 记录注入前的原始真值（用于核对邻站未被污染）
    truth = {}
    for r in raw.itertuples(index=False):
        truth[(int(r.station_id), r.d)] = {m: (None if pd.isna(getattr(r, m)) else float(getattr(r, m)))
                                           for m in METRICS}
    c.close()

    def _valid(v, m):
        return v is not None and PHYS[m][0] <= v <= PHYS[m][1]

    # ---- 收集 injected 运行的 rows
    rows_inj, summary_inj = {}, {}
    for s in out_inj["stations"]:
        sid = int(s["station"]["station_id"])
        summary_inj[sid] = s["summary"]
        for r in s["rows"]:
            rows_inj[(sid, str(r["date"])[:10], r["metric"])] = r
    rows_ctl = set()
    summary_ctl = {}
    for s in out_ctl["stations"]:
        sid = int(s["station"]["station_id"])
        summary_ctl[sid] = s["summary"]
        for r in s["rows"]:
            rows_ctl.add((sid, str(r["date"])[:10], r["metric"]))

    # ---- 1) 逐条对齐
    per_metric, per_kind, per_quality = {}, {}, {}
    detail, missed = [], []
    for rec in injections:
        key = (int(rec["station_id"]), str(rec["date"])[:10], rec["metric"])
        got = rows_inj.get(key)
        if got is None:
            missed.append({"station_id": key[0], "date": key[1], "metric": key[2],
                           "kind": rec["kind"], "true_value": rec["true_value"]})
            continue
        rep = got.get("repaired")
        # 该 bad 行的 problem 是否与注入方式对得上
        prob_ok = (got.get("problem") == "row_missing") or \
                  (rec["kind"] == "null" and got.get("problem", "").endswith(":missing")) or \
                  (rec["kind"] != "null" and got.get("problem", "").endswith(":out_of_range"))
        item = {"station_id": key[0], "date": key[1], "metric": key[2], "kind": rec["kind"],
                "true_value": rec["true_value"], "original": got.get("original"),
                "repaired": rep, "method": got.get("method"),
                "quality": got.get("quality"), "problem": got.get("problem"),
                "problem_matches_kind": bool(prob_ok),
                "err": (None if rep is None else round(float(rep) - rec["true_value"], 4))}
        detail.append(item)
        m = rec["metric"]
        d = per_metric.setdefault(m, {"n_injected": 0, "n_matched": 0, "n_repaired": 0,
                                      "errs": [], "by_quality": {}, "by_kind": {}})
        d["n_injected"] += 1
        d["n_matched"] += 1
        if rep is not None:
            d["n_repaired"] += 1
            d["errs"].append(item["err"])
        q = got.get("quality") or "none"
        qq = d["by_quality"].setdefault(q, {"n": 0, "n_repaired": 0, "errs": []})
        qq["n"] += 1
        if rep is not None:
            qq["n_repaired"] += 1
            qq["errs"].append(item["err"])
        kk = d["by_kind"].setdefault(rec["kind"], {"n": 0, "n_repaired": 0, "errs": []})
        kk["n"] += 1
        if rep is not None:
            kk["n_repaired"] += 1
            kk["errs"].append(item["err"])
        if rep is not None:
            pk = per_kind.setdefault(rec["kind"], []).append(item["err"])
            pq = per_quality.setdefault(q, []).append(item["err"])

    # ---- 2) 最近站对照（直接用最近站同日原始值，不做任何订正）
    def nearest_baseline(sid, dstr, m):
        for n in sorted(nb_by_station.get(sid, []), key=lambda x: x["distance_km"]):
            v = truth.get((int(n["station_id"]), dstr), {}).get(m)
            if _valid(v, m):
                return v, int(n["station_id"]), float(n["distance_km"])
        return None, None, None

    bl = {m: {"n": 0, "n_have": 0, "errs": []} for m in METRICS}
    bl_detail = []
    for rec in injections:
        key = (int(rec["station_id"]), str(rec["date"])[:10], rec["metric"])
        m = rec["metric"]
        v, nsid, ndist = nearest_baseline(*key)
        bl[m]["n"] += 1
        if v is not None:
            bl[m]["n_have"] += 1
            bl[m]["errs"].append(float(v) - rec["true_value"])
            bl_detail.append({"station_id": key[0], "date": key[1], "metric": m,
                              "nearest_station": nsid, "distance_km": ndist,
                              "nearest_value": v, "true_value": rec["true_value"],
                              "err": round(float(v) - rec["true_value"], 4)})

    # ---- 3) 反向对照：bad_rows 增量 vs 注入量
    ctl_bad = sum(s["bad_rows"] for s in summary_ctl.values())
    inj_bad = sum(s["bad_rows"] for s in summary_inj.values())
    inj_dates = {}
    for rec in injections:
        inj_dates.setdefault(int(rec["station_id"]), set()).add(str(rec["date"])[:10])
    exp_days = sum(len(v) for v in inj_dates.values())          # 若注入日期原本都干净
    delta = inj_bad - ctl_bad
    # 用 control 的 bad 日期集合精确算应增量
    ctl_days = {k[:2] for k in rows_ctl}
    exp_days_exact = sum(1 for sid, ds in inj_dates.items()
                         for d in ds if (sid, d) not in ctl_days)

    # ---- 4) 误报/漏报
    inj_keys = {(int(r["station_id"]), str(r["date"])[:10], r["metric"]) for r in injections}
    false_pos = sorted({k for k in rows_inj if k not in inj_keys
                        and (k[0], k[1], k[2]) not in rows_ctl})
    false_pos_true_bad = [k for k in false_pos
                          if not _valid(truth.get((k[0], k[1]), {}).get(k[2]), k[2])]
    # control 里标记为 bad 的，若在 pristine 里其实是有效值 → 误报
    ctl_false = []
    for k in rows_ctl:
        v = truth.get((k[0], k[1]), {}).get(k[2])
        if _valid(v, k[2]) and (k[0], k[1]) in truth:
            ctl_false.append(k)

    # ---- 5) 汇总
    def _rate(d):
        return round(d["n_repaired"] / d["n_matched"], 3) if d["n_matched"] else None

    res = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sandbox": SB, "window": plan["window"], "k": K, "radius_km": RADIUS_KM,
        "method": "auto", "allow_climate_fill": False,
        "design": {
            "targets": len(plan["targets"]), "parquet_files_copied": len(plan["copied"]),
            "injected_total": len(injections),
            "injection_policy": "只对 30 个靶站注入；邻站保持原始干净值",
            "neighbor_fidelity_missing": plan["fidelity_missing"],
            "data_py_td_glob": out_inj["td_glob"],
        },
        "run": {"injected": {"elapsed_sec": out_inj["elapsed_sec"],
                             "sandbox_rows": out_inj["sandbox_rows"],
                             "sandbox_stations": out_inj["sandbox_stations"]},
                "control": {"elapsed_sec": out_ctl["elapsed_sec"],
                            "sandbox_rows": out_ctl["sandbox_rows"]}},
        "summary": {
            "injected_total": len(injections),
            "found_by_scanner": len(detail),
            "missed_by_scanner": len(missed),
            "repaired_not_none": sum(1 for d in detail if d["repaired"] is not None),
            "restore_rate": round(sum(1 for d in detail if d["repaired"] is not None)
                                  / max(1, len(injections)), 3),
            "problem_matches_kind": sum(1 for d in detail if d["problem_matches_kind"]),
            "by_quality": {q: len(v) for q, v in per_quality.items()},
        },
        "per_metric": {}, "per_kind": {}, "per_quality": {},
        "nearest_baseline": {}, "reverse_control": {}, "anomalies": {},
        "detail": detail, "missed": missed,
        "nearest_baseline_detail": bl_detail,
        "control_summary_by_station": {str(k): v for k, v in summary_ctl.items()},
    }
    for m in METRICS:
        d = per_metric.get(m, {"n_injected": len([r for r in injections if r["metric"] == m]),
                               "n_matched": 0, "n_repaired": 0, "errs": [], "by_quality": {},
                               "by_kind": {}})
        res["per_metric"][m] = {
            "n_injected": d["n_injected"], "n_matched": d["n_matched"],
            "n_repaired": d["n_repaired"], "restore_rate": _rate(d),
            "accuracy": _err_stats(d["errs"]),
            "by_quality": {q: dict(_err_stats(v["errs"]), n=v["n"], n_repaired=v["n_repaired"])
                           for q, v in d["by_quality"].items()},
            "by_kind": {q: dict(_err_stats(v["errs"]), n=v["n"], n_repaired=v["n_repaired"])
                        for q, v in d["by_kind"].items()},
        }
        b = bl.get(m, {"n": 0, "n_have": 0, "errs": []})
        res["nearest_baseline"][m] = {
            "n": b["n"], "n_have": b["n_have"],
            "coverage": round(b["n_have"] / b["n"], 3) if b["n"] else None,
            "accuracy": _err_stats(b["errs"]),
        }
    res["per_kind"] = {k: _err_stats(v) for k, v in per_kind.items()}
    res["per_quality"] = {k: _err_stats(v) for k, v in per_quality.items()}
    res["reverse_control"] = {
        "bad_rows_control": ctl_bad, "bad_rows_injected": inj_bad,
        "bad_rows_delta": delta,
        "distinct_injected_dates": exp_days,
        "expected_delta_exact": exp_days_exact,
        "delta_minus_expected": delta - exp_days_exact,
        "control_repaired_values": sum(s["repaired_values"] for s in summary_ctl.values()),
        "injected_repaired_values": sum(s["repaired_values"] for s in summary_inj.values()),
        "control_by_quality": {q: sum(s["by_quality"].get(q, 0) for s in summary_ctl.values())
                               for q in ("observed", "temporal", "climate")},
        "injected_by_quality": {q: sum(s["by_quality"].get(q, 0) for s in summary_inj.values())
                                for q in ("observed", "temporal", "climate")},
        "rows_control_total": len(rows_ctl), "rows_injected_total": len(rows_inj),
    }
    res["anomalies"] = {
        "missed_by_scanner": len(missed),
        "problem_mismatch": [d for d in detail if not d["problem_matches_kind"]][:20],
        "false_positives_vs_control": len(false_pos),
        "false_positives_all_truly_bad": len(false_pos) == len(false_pos_true_bad),
        "false_positive_examples": [list(k) for k in false_pos[:10]],
        "control_false_positives": len(ctl_false),
        "control_false_positive_examples": [list(k) for k in ctl_false[:10]],
        "max_abs_error_rows": sorted([d for d in detail if d["err"] is not None],
                                     key=lambda x: -abs(x["err"]))[:10],
        "truncated_at_detail_limit": [sid for sid, s in summary_inj.items() if s["bad_rows"] >= 500],
    }

    # ---- 6) 修复方法 / quality 分布，以及「邻站同日也被注入」的污染量化
    meth_dist, qual_dist = {}, {}
    for d in detail:
        meth_dist[d["method"]] = meth_dist.get(d["method"], 0) + 1
        qual_dist[d["quality"]] = qual_dist.get(d["quality"], 0) + 1
    inj_days_by_station = {}
    for sid, ds in inj_dates.items():
        inj_days_by_station[sid] = set(ds)
    n_pairs, n_contam, contam_ex = 0, 0, []
    for sid, ds in inj_days_by_station.items():
        nbs = [int(x["station_id"]) for x in nb_by_station.get(sid, [])]
        for d in ds:
            n_pairs += 1
            hit = [n for n in nbs if d in inj_days_by_station.get(n, ())]
            if hit:
                n_contam += 1
                if len(contam_ex) < 10:
                    contam_ex.append({"station_id": sid, "date": d, "injected_neighbors": hit})
    res["method_distribution"] = meth_dist
    res["quality_distribution"] = qual_dist
    res["neighbor_contamination"] = {
        "policy": "只对 30 个靶站注入坏值；邻站保持原始值",
        "target_day_pairs": n_pairs,
        "pairs_with_injected_neighbor": n_contam,
        "rate": round(n_contam / max(1, n_pairs), 3),
        "examples": contam_ex,
    }

    # ---- 7) 闰日口径检查：源库 ts 整体 +100 年，2100-02-28 而非闰日
    #      真实 2000-02-29 被映射成 2100-02-28 → 与真实 2000-02-28 撞成同日双行；
    #      反向 ts_bj-100Y 得到 2000-02-28 两次，2000-02-29 永远无行 → 整库 684 站
    #      的 2000-02-29 都被 scan 报 row_missing 并被「修复」出虚构值。
    def _leap_probe():
        # eval 阶段父进程尚未 import data.py，先把环境变量指向「注入后的沙箱」
        os.environ["WEATHER_TD_GLOB"] = os.path.join(SB_TD, "*.parquet").replace("\\", "/")
        os.environ["WEATHER_MY_DIR"] = SB_MY.replace("\\", "/")
        sys.path.insert(0, HERE)
        from weather_api import data as DD
        c2 = _duck()
        q = """
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS sid,
               count(*) AS n
        FROM read_parquet('%s/*.parquet', filename=true)
        WHERE CAST(ts_bj AS DATE) = DATE '2100-02-28'
        GROUP BY 1 HAVING count(*) > 1 ORDER BY 1
        """ % SB_TD.replace("\\", "/")
        dup = c2.execute(q).df()
        out = {"source_date_with_double_rows": "2100-02-28",
               "sandbox_stations_affected": int(len(dup)),
               "sandbox_station_ids": [int(x) for x in dup["sid"].tolist()][:10]}
        if len(dup) == 0:
            out["note"] = "沙箱内无此现象的站，改用真镜像统计"
            return out
        sid0 = int(dup["sid"].iloc[0])
        out["probe_station"] = sid0
        out["data_py_td_glob"] = DD.TD_GLOB
        sc = DD.repair_scan(sid0, start="2000-02-27", end="2000-03-01")
        out["natural_days"] = 4
        out["scan_days"] = int(sc["summary"]["days"])          # 自然日应为 4
        out["scan_days_ok"] = bool(int(sc["summary"]["days"]) == 4)
        out["bad_rows"] = [{"date": str(b["date"])[:10], "problems": b["problems"]}
                           for b in sc["bad_rows"]]
        rd = DD.repair_daily(sid0, start="2000-02-28", end="2000-03-01",
                             method="auto", allow_climate_fill=False)
        fab = [r for r in rd["rows"] if str(r["date"])[:10] == "2000-02-29"
               and r["repaired"] is not None]
        out["fabricated_2000_02_29"] = {
            "n_values": len(fab),
            "metrics": [r["metric"] for r in fab],
            "quality": sorted({r["quality"] for r in fab}),
            "sample": {r["metric"]: r["repaired"] for r in fab[:3]},
        }
        out["verdict"] = ("BUG：源库 +100 年位移把 2000-02-29 挤成 2100-02-28 的双行，"
                          "ts_true 下 2000-02-29 无任何源行；scan 报 row_missing 且 "
                          "repair_daily 用时间维给它编了值（quality=temporal，"
                          "看起来像「修复成功」实为凭空生成）")
        # 全库规模（只读真镜像）
        try:
            gm = os.path.join(TD_REAL, "*.parquet")
            full = c2.execute("""
                SELECT count(*) FILTER (WHERE n > 1) AS dup_stations, count(*) AS stations
                FROM (
                  SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS sid,
                         count(*) AS n
                  FROM read_parquet('%s', filename=true)
                  WHERE CAST(ts_bj AS DATE) = DATE '2100-02-28'
                  GROUP BY 1
                )
            """ % gm).fetchone()
            out["mirror_scale"] = {"stations_present_on_2100_02_28": int(full[1]),
                                   "stations_with_double_rows": int(full[0]),
                                   "ts_true_2000_02_29_rows": "该日期在 ts_true 下永远为 0 行（2100 非闰年）"}
        except Exception as e:
            out["mirror_scale"] = {"error": "%s: %s" % (type(e).__name__, e)}
        return out

    try:
        res["leap_day_issue"] = _leap_probe()
    except Exception as e:
        res["leap_day_issue"] = {"error": "%s: %s" % (type(e).__name__, e)}

    # ---- 8) 产出侧合法性审计：修复器自己会不会产出物理不可能的值 / 类型漂移
    def _output_audit():
        o = {"n_repaired_rows": 0, "out_of_phys_range": [], "not_rounded": [],
             "max_lt_min_days": [], "humidity_fractional": {"n": 0, "n_frac": 0}}
        by_date = {}
        for (sid, dstr, m), r in rows_inj.items():
            v = r.get("repaired")
            if v is None:
                continue
            o["n_repaired_rows"] += 1
            lo, hi = PHYS[m]
            if not (lo <= float(v) <= hi):
                o["out_of_phys_range"].append({"station_id": sid, "date": dstr, "metric": m,
                                               "repaired": v, "range": [lo, hi]})
            nd = ROUND_DIGITS.get(m, 2)
            if abs(round(float(v), nd) - float(v)) > 1e-9:
                o["not_rounded"].append({"station_id": sid, "date": dstr, "metric": m,
                                         "repaired": v, "round_to": nd})
            if m == "avg_humidity":
                o["humidity_fractional"]["n"] += 1
                if abs(float(v) - round(float(v))) > 1e-9:
                    o["humidity_fractional"]["n_frac"] += 1
            if m in ("max_temperature", "min_temperature"):
                by_date.setdefault((sid, dstr), {})[m] = float(v)
            if m == "precipitation" and r.get("method") == "krig":
                o.setdefault("precip_via_krig", 0)
                o["precip_via_krig"] += 1
        for (sid, dstr), mm in by_date.items():
            if "max_temperature" in mm and "min_temperature" in mm \
                    and mm["max_temperature"] < mm["min_temperature"]:
                o["max_lt_min_days"].append({"station_id": sid, "date": dstr,
                                             "max": mm["max_temperature"],
                                             "min": mm["min_temperature"]})
        o["out_of_phys_range"] = o["out_of_phys_range"][:10]
        o["not_rounded"] = o["not_rounded"][:10]
        o["max_lt_min_days"] = o["max_lt_min_days"][:10]
        o["note"] = ("avg_humidity 源 parquet 类型是 BIGINT，但 repair 输出按 _ROUND 保留 1 位小数，"
                     "下游若严格按原 schema 回写会截断/报错（属预期内的类型放宽，非数值错误）")
        return o

    try:
        res["output_audit"] = _output_audit()
    except Exception as e:
        res["output_audit"] = {"error": "%s: %s" % (type(e).__name__, e)}

    # ---- 9) weather_ok「全 7 指标 AND」连坐损失（只读沙箱 pristine，无需真镜像）
    def _weather_ok_contagion():
        c3 = _duck()
        conds = ["(%s BETWEEN %s AND %s)" % (m, PHYS[m][0], PHYS[m][1]) for m in METRICS]
        single = ", ".join("count(*) FILTER (WHERE (%s BETWEEN %s AND %s)) AS %s_ok"
                           % (m, PHYS[m][0], PHYS[m][1], m) for m in METRICS)
        q = """
        SELECT count(*) AS total, count(*) FILTER (WHERE %s) AS all_ok, %s
        FROM read_parquet('%s/*.parquet', filename=true)
        WHERE CAST(ts_bj - INTERVAL 100 YEAR AS DATE) BETWEEN DATE '1991-01-01' AND DATE '2020-12-31'
        """ % (" AND ".join(conds), single, SB_PRISTINE.replace("\\", "/"))
        row = c3.execute(q).df().iloc[0].to_dict()
        c3.close()
        tot, allok = int(row["total"]), int(row["all_ok"])
        return {"window": "1991-01-01..2020-12-31（气候基准 period）",
                "rows_total": tot, "rows_all_seven_ok": allok,
                "all_seven_ok_pct": round(100.0 * allok / tot, 2) if tot else None,
                "per_metric_single_ok": {
                    m: {"n_ok": int(row["%s_ok" % m]),
                        "pct_ok": round(100.0 * int(row["%s_ok" % m]) / tot, 2) if tot else None,
                        "n_lost_by_other_metrics": int(row["%s_ok" % m]) - allok,
                        "pct_lost": (round(100.0 * (int(row["%s_ok" % m]) - allok)
                                           / int(row["%s_ok" % m]), 2)
                                     if int(row["%s_ok" % m]) else None)}
                    for m in METRICS},
                "verdict": ("data.py:67-77 的 weather_ok 把 7 个指标用 AND 串起来，"
                            "任一指标越界整行作废 → 只有 precipitation 是哨兵值的日期，"
                            "该日的气温/湿度/风速/日照也被连坐丢弃，"
                            "缩小 1991-2020 气候基准 base（data.py:1063-1070）的样本量")}

    try:
        res["weather_ok_contagion"] = _weather_ok_contagion()
    except Exception as e:
        res["weather_ok_contagion"] = {"error": "%s: %s" % (type(e).__name__, e)}

    json.dump(res, open(RESULT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    _print_summary(res)
    _log("\n[eval] → %s" % RESULT_JSON)
    return res


def _print_summary(res):
    s = res["summary"]
    _log("\n" + "=" * 78)
    _log("端到端验证结果（真实 data.repair_daily，method=auto，allow_climate_fill=False）")
    _log("=" * 78)
    _log("沙箱：%d 站 parquet，%d 靶站，%d 条注入"
         % (res["design"]["parquet_files_copied"], res["design"]["targets"],
            res["design"]["injected_total"]))
    _log("扫描器发现 %d/%d（漏报 %d）；修复出值 %d（恢复率 %.1f%%）；problem 与注入类型吻合 %d"
         % (s["found_by_scanner"], s["injected_total"], s["missed_by_scanner"],
            s["repaired_not_none"], 100.0 * s["restore_rate"], s["problem_matches_kind"]))
    _log("\n%-18s %5s %5s %7s %8s %8s %9s %9s %9s"
         % ("指标", "注入", "恢复", "恢复率", "MAE", "RMSE", "偏差", "最近站MAE", "改进倍数"))
    for m in METRICS:
        d = res["per_metric"][m]
        a = d["accuracy"]
        b = res["nearest_baseline"][m]["accuracy"]
        blr = b.get("mae")
        ratio = ("%.2fx" % (blr / a["mae"])) if (a.get("mae") and blr) else "-"
        _log("%-18s %5d %5d %6.1f%% %8.3f %8.3f %9.3f %9s %9s"
             % (m, d["n_injected"], d["n_repaired"], 100.0 * (d["restore_rate"] or 0),
                a.get("mae", float("nan")), a.get("rmse", float("nan")), a.get("bias", float("nan")),
                ("%.3f" % blr) if blr is not None else "-", ratio))
    _log("\n按 quality 分档：")
    for q, v in sorted(res["per_quality"].items()):
        _log("  %-9s n=%-4d MAE=%.3f RMSE=%.3f 偏差=%+.3f" %
             (q, v["n"], v.get("mae", float("nan")), v.get("rmse", float("nan")),
              v.get("bias", float("nan"))))
    _log("按注入类型：")
    for q, v in sorted(res["per_kind"].items()):
        _log("  %-9s n=%-4d MAE=%.3f RMSE=%.3f 偏差=%+.3f" %
             (q, v["n"], v.get("mae", float("nan")), v.get("rmse", float("nan")),
              v.get("bias", float("nan"))))
    rv = res["reverse_control"]
    _log("\n反向对照：control bad_rows=%d，注入后=%d，增量=%d，注入的不同日期数=%d（应增量 %d，差 %+d）"
         % (rv["bad_rows_control"], rv["bad_rows_injected"], rv["bad_rows_delta"],
            rv["distinct_injected_dates"], rv["expected_delta_exact"],
            rv["delta_minus_expected"]))
    _log("  control 修复出值 %d，注入后 %d；quality 分档 control=%s 注入后=%s"
         % (rv["control_repaired_values"], rv["injected_repaired_values"],
            rv["control_by_quality"], rv["injected_by_quality"]))
    an = res["anomalies"]
    _log("\n异常检查：漏报 %d；problem 类型不符 %d；相对 control 的误报 %d（其中确为原始坏值 %s）；"
         "control 自身误报 %d；detail_limit 截断站 %s"
         % (an["missed_by_scanner"], len(an["problem_mismatch"]),
            an["false_positives_vs_control"], an["false_positives_all_truly_bad"],
            an["control_false_positives"], an["truncated_at_detail_limit"] or "无"))
    if an["max_abs_error_rows"]:
        w = an["max_abs_error_rows"][0]
        _log("最大误差样例：站 %s %s %s 真值 %.2f → 修复 %.2f（method=%s, quality=%s）误差 %.2f"
             % (w["station_id"], w["date"], w["metric"], w["true_value"],
                w["repaired"], w["method"], w["quality"], w["err"]))
    md = res.get("method_distribution") or {}
    _log("\n修复方法分布：%s" % ", ".join("%s=%d" % kv for kv in sorted(md.items())))
    nc = res.get("neighbor_contamination") or {}
    _log("邻站污染：%d 个(靶站,注入日)中 %d 个邻站同日也被注入（%.1f%%）—— 设计上邻站保持原始值"
         % (nc.get("target_day_pairs", 0), nc.get("pairs_with_injected_neighbor", 0),
            100.0 * (nc.get("rate") or 0)))
    lp = res.get("leap_day_issue") or {}
    if "error" in lp:
        _log("\n闰日检查失败：%s" % lp["error"])
    elif lp.get("fabricated_2000_02_29"):
        f = lp["fabricated_2000_02_29"]
        _log("\n!! 闰日口径 BUG：源库 %s 双行；scan 把 2000-02-29 报成 row_missing，"
             "repair_daily 虚构出 %d 个值（quality=%s，样例 %s）"
             % (lp.get("source_date_with_double_rows"), f["n_values"],
                ",".join(f["quality"]), f["sample"]))
        _log("   probe 站 %s：自然日 %d 但 scan.days=%d（%s）"
             % (lp.get("probe_station"), lp.get("natural_days"), lp.get("scan_days"),
                "OK" if lp.get("scan_days_ok") else "不一致"))
    ms = lp.get("mirror_scale") or {}
    if ms.get("stations_with_double_rows"):
        _log("   全镜像规模：%d 站中有 %d 站该日双行（%.1f%%），影响的是每一次默认全区间调用"
             % (ms.get("stations_present_on_2100_02_28", 0), ms.get("stations_with_double_rows", 0),
                100.0 * ms["stations_with_double_rows"]
                / max(1, ms.get("stations_present_on_2100_02_28", 1))))
    oa = res.get("output_audit") or {}
    if "error" not in oa:
        _log("\n产出侧审计：repaired %d 条；越出 PHYS_RANGE %d；未按 _ROUND 取整 %d；"
             "max<min 日期 %d"
             % (oa.get("n_repaired_rows", 0), len(oa.get("out_of_phys_range") or []),
                len(oa.get("not_rounded") or []), len(oa.get("max_lt_min_days") or [])))
        hf = oa.get("humidity_fractional") or {}
        if hf.get("n"):
            _log("  avg_humidity 修复输出 %d 条中 %d 条带小数（源列 BIGINT）—— 回写需放宽类型"
                 % (hf["n"], hf.get("n_frac", 0)))
    wc = res.get("weather_ok_contagion") or {}
    if "error" not in wc:
        _log("\nweather_ok 全 7 指标 AND 的连坐损失（%s，沙箱 %d 行）：7 指标同时有效 %.2f%%；"
             "单指标有效 ~99.9%%" % (wc.get("window"), wc.get("rows_total", 0),
                                      wc.get("all_seven_ok_pct") or 0))
        at = (wc.get("per_metric_single_ok") or {}).get("avg_temperature") or {}
        _log("  → 仅因其他指标越界，avg_temperature 可用样本被连坐丢弃 %d 行（%.2f%%），"
             "直接缩小气候基准 base 的样本量" % (at.get("n_lost_by_other_metrics", 0),
                                              at.get("pct_lost") or 0))


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", nargs="?", default="all",
                    choices=["all", "plan", "prepare", "inject", "run", "eval"])
    ap.add_argument("--tag", default="injected")
    a = ap.parse_args()

    if a.phase == "plan":
        phase_plan(); return
    if a.phase == "prepare":
        phase_prepare(); return
    if a.phase == "inject":
        phase_inject(); return
    if a.phase == "run":
        phase_run(a.tag); return
    if a.phase == "eval":
        phase_eval(); return

    t0 = time.time()
    plan = phase_prepare()
    phase_inject()

    # 两个独立子进程：各自 import data.py 时读不同的 WEATHER_TD_GLOB
    envs = {
        "injected": os.path.join(SB_TD, "*.parquet"),
        "control": os.path.join(SB_CTRL_TD, "*.parquet"),
    }
    for tag, glob in envs.items():
        env = dict(os.environ)
        env["WEATHER_TD_GLOB"] = glob.replace("\\", "/")
        env["WEATHER_MY_DIR"] = SB_MY.replace("\\", "/")
        env["PYTHONIOENCODING"] = "utf-8"
        _log("\n>>> 子进程跑 %s 数据集：%s" % (tag, env["WEATHER_TD_GLOB"]))
        r = subprocess.run([sys.executable, os.path.abspath(__file__), "run", "--tag", tag],
                           env=env, cwd=HERE)
        if r.returncode != 0:
            raise SystemExit("run:%s 失败，退出码 %d" % (tag, r.returncode))
    phase_eval()
    _log("\n总耗时 %.1f 秒" % (time.time() - t0))


if __name__ == "__main__":
    main()
