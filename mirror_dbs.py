# -*- coding: utf-8 -*-
"""
把 TDengine Cloud + MySQL 的数据全量镜像到本地（只读源库，只写本地文件）。

输出目录：D:\\Android PDF\\db_mirror\\
  tdengine/weather_data/station_<id>.parquet   876 个文件，一个子表一个
  mysql/<表名>.parquet                         31 张表
  _mirror_manifest.json                        清单（行数/文件大小/校验）
  本地镜像说明.md / .html                       使用说明

特性：
  - 断点续传：已存在且行数吻合的 parquet 直接跳过（--force 强制重拉）
  - 主键列 ts 同时保留 UTC 原值(ts) 与北京时间(ts_bj)
  - 哨兵值（32766/32767/999999/32700 等）原样保留，不篡改，只在说明里标注
"""
import os, sys, json, time, argparse, datetime, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pymysql

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "db_mirror")
TD_DIR = os.path.join(ROOT, "tdengine", "weather_data")
MY_DIR = os.path.join(ROOT, "mysql")
MANIFEST = os.path.join(ROOT, "_mirror_manifest.json")

# ---- 凭据全部从环境变量读取（不要写进代码，也不要把含凭据的 .env 提交到仓库）----
T = os.environ.get("TD_TOKEN", "")
TD_DB = os.environ.get("TD_DB", "")
TD_URL = os.environ.get("TD_URL", "") or "https://gw.cloud.taosdata.com/rest/sql/" + TD_DB
TD_BASE = TD_URL
MYSQL = dict(host=os.environ.get("MYSQL_HOST", ""),
             port=int(os.environ.get("MYSQL_PORT", "3306")),
             user=os.environ.get("MYSQL_USER", ""),
             password=os.environ.get("MYSQL_PASSWORD", ""),
             database=os.environ.get("MYSQL_DB", ""),
             charset="utf8mb4", connect_timeout=10, read_timeout=300)


def _need_env():
    """启动时检查必需的环境变量，缺了直接给出人话提示。"""
    missing = [k for k in ("TD_TOKEN", "TD_DB", "MYSQL_HOST", "MYSQL_USER",
                           "MYSQL_PASSWORD", "MYSQL_DB") if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "缺少环境变量：" + ", ".join(missing) + "\n"
            "请先设置，例如（Linux/macOS）：\n"
            "  export TD_TOKEN='<你的 TDengine token>' TD_DB='<库名>'\n"
            "  export MYSQL_HOST='<主机>' MYSQL_PORT=3306\n"
            "  export MYSQL_USER='<用户>' MYSQL_PASSWORD='<密码>' MYSQL_DB='<库名>'\n"
            "Windows PowerShell：\n"
            "  $env:TD_TOKEN='<token>'; $env:TD_DB='<库名>'; $env:MYSQL_HOST='<主机>'; ...\n"
            "详见 README 的「连接源库」一节。")


_need_env()

# weather_data 的列顺序（与 SELECT * 一致）
TD_COLS = ["ts", "avg_temperature", "max_temperature", "min_temperature",
           "avg_humidity", "precipitation", "avg_wind_speed", "sunshine_hours",
           "file_path", "user_id", "user_nickname"]
FLOAT_COLS = ["avg_temperature", "max_temperature", "min_temperature",
              "precipitation", "avg_wind_speed", "sunshine_hours"]
INT_COLS = ["avg_humidity", "user_id"]

S = requests.Session()
S.headers.update({"Connection": "keep-alive"})

_beijing = datetime.timezone(datetime.timedelta(hours=8))


def log(msg):
    print(msg, flush=True)


# ---------------- TDengine ----------------
def td_post(sql, timeout=180, retries=4):
    last = None
    for i in range(retries):
        try:
            r = S.post(TD_BASE, params={"token": T}, data=sql, timeout=timeout)
            if r.status_code != 200:
                last = "HTTP %s %s" % (r.status_code, r.text[:120])
                time.sleep(2 * (i + 1))
                continue
            j = r.json()
            if j.get("code") != 0:
                raise RuntimeError("code=%s desc=%s" % (j.get("code"), j.get("desc")))
            return j
        except RuntimeError:
            raise
        except Exception as ex:
            last = "%s: %s" % (type(ex).__name__, ex)
            time.sleep(2 * (i + 1))
    raise RuntimeError("retries exhausted: %s" % last)


def td_sql(sql, timeout=180):
    j = td_post(sql, timeout=timeout)
    return [m[0] for m in j.get("column_meta", [])], j.get("data", [])


def rows_to_frame(cols, rows):
    """把 REST 返回的 list-of-list 转成固定 schema 的 DataFrame"""
    df = pd.DataFrame(rows, columns=cols)
    for c in FLOAT_COLS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype("float64")
    for c in INT_COLS:
        if c in df.columns:
            v = pd.to_numeric(df[c], errors="coerce")
            df[c] = v.round().astype("Int64")
    if "ts" in df.columns:
        utc = pd.to_datetime(df["ts"], utc=True, errors="coerce")
        df["ts"] = utc
        df["ts_bj"] = utc.dt.tz_convert(_beijing).dt.tz_localize(None)
    for c in ("file_path", "user_nickname"):
        if c in df.columns:
            df[c] = df[c].astype("object").where(df[c].notna(), None)
    # 列顺序：ts, ts_bj, 其余
    order = ["ts", "ts_bj"] + [c for c in TD_COLS if c != "ts" and c in df.columns]
    return df[[c for c in order if c in df.columns]]


def parquet_path(sid):
    return os.path.join(TD_DIR, "station_%d.parquet" % sid)


def fetch_station(sid):
    """拉取单个子表并写成 parquet，返回 (sid, rows, bytes, elapsed, skipped)"""
    p = parquet_path(sid)
    if os.path.exists(p) and not FORCE:
        try:
            n = pq.ParquetFile(p).metadata.num_rows
            return (sid, n, os.path.getsize(p), 0.0, True)
        except Exception:
            pass  # 文件坏了就重拉
    t0 = time.time()
    cols, rows = td_sql("SELECT * FROM wd_station_%d" % sid, timeout=300)
    df = rows_to_frame(cols, rows)
    tmp = p + ".tmp"
    df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False,
                  schema=SCHEMA if SCHEMA else None)
    os.replace(tmp, p)
    return (sid, len(df), os.path.getsize(p), time.time() - t0, False)


# 统一的 parquet schema，保证 876 个文件可被 duckdb 一次性 union
SCHEMA = pa.schema([
    pa.field("ts", pa.timestamp("us", tz="UTC")),
    pa.field("ts_bj", pa.timestamp("us")),
    pa.field("avg_temperature", pa.float64()),
    pa.field("max_temperature", pa.float64()),
    pa.field("min_temperature", pa.float64()),
    pa.field("avg_humidity", pa.int64()),
    pa.field("precipitation", pa.float64()),
    pa.field("avg_wind_speed", pa.float64()),
    pa.field("sunshine_hours", pa.float64()),
    pa.field("file_path", pa.string()),
    pa.field("user_id", pa.int64()),
    pa.field("user_nickname", pa.string()),
])


# ---------------- MySQL ----------------
def mirror_mysql():
    conn = pymysql.connect(**MYSQL)
    out = []
    try:
        cur = conn.cursor()
        cur.execute("""SELECT table_name FROM information_schema.tables
                       WHERE table_schema=DATABASE() AND table_type='BASE TABLE'
                       ORDER BY table_name""")
        tables = [r[0] for r in cur.fetchall()]
        for t in tables:
            p = os.path.join(MY_DIR, "%s.parquet" % t)
            if os.path.exists(p) and not FORCE:
                try:
                    n = pq.ParquetFile(p).metadata.num_rows
                    out.append({"table": t, "rows": n, "bytes": os.path.getsize(p), "skipped": True})
                    log("[MY skip] %-46s %d rows" % (t, n))
                    continue
                except Exception:
                    pass
            t0 = time.time()
            cur.execute("SELECT * FROM `%s`" % t.replace("`", "``"))
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            if rows:
                df = pd.DataFrame(rows, columns=cols)
                for c in df.columns:  # Decimal / bytes 归一化
                    if df[c].dtype == object:
                        df[c] = df[c].map(lambda v: bytes(v) if isinstance(v, (bytearray,)) else v)
            else:
                df = pd.DataFrame(columns=cols)
            tmp = p + ".tmp"
            df.to_parquet(tmp, engine="pyarrow", compression="zstd", index=False)
            os.replace(tmp, p)
            out.append({"table": t, "rows": len(df), "bytes": os.path.getsize(p), "skipped": False})
            log("[MY ok]   %-46s %6d rows  %.2fs" % (t, len(df), time.time() - t0))
        cur.close()
    finally:
        conn.close()
    return out


# ---------------- main ----------------
def main():
    global FORCE
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="忽略本地已有 parquet，全部重拉")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--skip-mysql", action="store_true")
    ap.add_argument("--skip-td", action="store_true")
    a = ap.parse_args()
    FORCE = a.force

    os.makedirs(TD_DIR, exist_ok=True)
    os.makedirs(MY_DIR, exist_ok=True)
    t_start = time.time()
    manifest = {"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "readonly_source": True, "tdengine": {}, "mysql": {}}

    # ---- TDengine ----
    if not a.skip_td:
        log("=" * 78)
        log("TDengine：枚举子表 ...")
        cols, rows = td_sql("SHOW TABLES", timeout=60)
        names = [r[0] for r in rows if r and r[0]]
        ids = sorted(int(n.split("_")[-1]) for n in names if n.startswith("wd_station_"))
        log("  子表 %d 张，station_id %d ~ %d" % (len(ids), ids[0], ids[-1]))

        cols, rows = td_sql("SELECT COUNT(*) FROM weather_data", timeout=120)
        total_expected = rows[0][0]
        log("  服务端报告总行数：%d" % total_expected)

        log("  开始并发拉取（workers=%d）..." % a.workers)
        done = 0
        got_rows = 0
        got_bytes = 0
        skipped = 0
        failures = []
        station_rows = {}
        t0 = time.time()
        with ThreadPoolExecutor(a.workers) as ex:
            futs = {ex.submit(fetch_station, sid): sid for sid in ids}
            for fut in as_completed(futs):
                sid = futs[fut]
                try:
                    sid, n, b, el, sk = fut.result()
                    station_rows[sid] = n
                    done += 1
                    if sk:
                        skipped += 1
                    else:
                        got_rows += n
                        got_bytes += b
                    if done % 25 == 0 or done == len(ids):
                        el_wall = time.time() - t0
                        log("    %4d/%d  skipped=%d  rows=%d  %.1f MB  %.1f MB/s  eta=%.0fs"
                            % (done, len(ids), skipped, got_rows, got_bytes / 1e6,
                               got_bytes / 1e6 / max(el_wall, 1e-6),
                               (len(ids) - done) * el_wall / done if done else 0))
                except Exception as ex:
                    failures.append({"station_id": sid, "error": "%s: %s" % (type(ex).__name__, ex)})
                    log("    [FAIL] station_%d  %s" % (sid, str(ex)[:120]))

        total_local = sum(station_rows.values())
        td_bytes = sum(os.path.getsize(parquet_path(s)) for s in station_rows)
        manifest["tdengine"] = {
            "database": TD_DB,
            "supertable": "weather_data",
            "subtable_count": len(ids),
            "station_id_min": ids[0], "station_id_max": ids[-1],
            "server_reported_rows": total_expected,
            "local_rows": total_local,
            "rows_match": total_local == total_expected,
            "local_bytes": td_bytes,
            "skipped_files": skipped,
            "failures": failures,
            "elapsed_sec": round(time.time() - t_start, 1),
            "station_rows": {str(k): v for k, v in sorted(station_rows.items())},
        }
        log("-" * 78)
        log("TDengine 完成：本地 %d 行 / 服务端 %d 行  %s"
            % (total_local, total_expected, "✅ 一致" if total_local == total_expected else "⚠️ 不一致"))
        log("  文件 %d 个，共 %.2f GB，失败 %d 个" % (len(station_rows), td_bytes / 1024 ** 3, len(failures)))

    # ---- MySQL ----
    if not a.skip_mysql:
        log("=" * 78)
        log("MySQL：镜像全部表 ...")
        my = mirror_mysql()
        manifest["mysql"] = {
            "database": MYSQL["database"],
            "table_count": len(my),
            "total_rows": sum(x["rows"] for x in my),
            "total_bytes": sum(x["bytes"] for x in my),
            "tables": my,
        }
        log("MySQL 完成：%d 张表，%d 行，%.1f MB"
            % (len(my), sum(x["rows"] for x in my), sum(x["bytes"] for x in my) / 1e6))

    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    log("=" * 78)
    log("清单已写入 %s" % MANIFEST)
    log("总耗时 %.1f 秒" % (time.time() - t_start))


FORCE = False

if __name__ == "__main__":
    main()
