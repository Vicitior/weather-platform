# -*- coding: utf-8 -*-
"""
本地实验工具（完全离线，不连任何数据库）。

用法：
  1) 交互式 SQL
        python local_lab.py
        lab> SELECT * FROM weather LIMIT 5;
        lab> SELECT * FROM stations LIMIT 5;
        lab> .views          # 看有哪些视图
        lab> .cols weather   # 看视图列
        lab> .quit

  2) 单条 SQL
        python local_lab.py -q "SELECT count(*) FROM weather"

  3) 在别的 Python 脚本里当模块用
        import local_lab
        df = local_lab.q("SELECT ...")
        con = local_lab.con()          # 复用同一个 duckdb 连接

内置视图（全部指向本地 parquet，源库已断开也能用）：
  weather    TDengine 全部 876 个气象子表（1669 万行）
  weather_ok 过滤掉哨兵/异常值后的 weather（推荐做统计用这个）
  stations   MySQL weather_station_info（909 个站点维表）
  weather_joined  weather + stations 的关联宽表（带省/站名/经纬度）
  mysql_<表名>     MySQL 的 31 张表，逐表一个视图
"""
import os
import sys
import glob
import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "db_mirror")
TD_GLOB = os.path.join(ROOT, "tdengine", "weather_data", "*.parquet").replace("\\", "/")
MY_DIR = os.path.join(ROOT, "mysql").replace("\\", "/")


def _sql_str(p):
    return "'" + p.replace("'", "''") + "'"


def con():
    c = duckdb.connect()
    c.execute("SET threads TO 8")
    c.execute("PRAGMA memory_limit='4GB'")

    # ---- TDengine 气象数据 ----
    # 注意：子表 SELECT * 不返回 TAG 列，station_id 从文件名 station_<id>.parquet 解析
    c.execute("""
        CREATE OR REPLACE VIEW weather AS
        SELECT CAST(regexp_extract(filename, 'station_(\\d+)\\.parquet', 1) AS INTEGER) AS station_id,
               w.*
        FROM read_parquet(%s, filename=true) AS w
    """ % _sql_str(TD_GLOB))

    # 哨兵值/异常值过滤：32766/32767/999999/9999990 为缺测标记；降水 >1000 的多为天气现象编码
    c.execute("""
        CREATE OR REPLACE VIEW weather_ok AS
        SELECT *
        FROM weather
        WHERE avg_temperature  BETWEEN -100 AND 100
          AND max_temperature  BETWEEN -100 AND 100
          AND min_temperature  BETWEEN -100 AND 100
          AND avg_humidity     BETWEEN 0 AND 100
          AND precipitation    BETWEEN 0 AND 1000
          AND avg_wind_speed   BETWEEN 0 AND 200
          AND sunshine_hours   BETWEEN 0 AND 24
    """)

    # 真实历史时间：源库时间戳被整体 +100 年（2051~2125 → 实际 1951~2025），
    # 依据：漠河月度气温与真实气候吻合；范围正好落在 1951-01-01~2025-12-31；
    # 满配站点 27394 行 = 75 年确切天数。ts_true = 北京时间 - 100 年。
    c.execute("""
        CREATE OR REPLACE VIEW weather_hist AS
        SELECT *, ts_bj - INTERVAL 100 YEAR AS ts_true
        FROM weather
    """)
    c.execute("""
        CREATE OR REPLACE VIEW weather_hist_ok AS
        SELECT *, ts_bj - INTERVAL 100 YEAR AS ts_true
        FROM weather_ok
    """)

    # ---- MySQL 各表 ----
    for p in sorted(glob.glob(os.path.join(MY_DIR, "*.parquet"))):
        name = os.path.splitext(os.path.basename(p))[0]
        c.execute('CREATE OR REPLACE VIEW "mysql_%s" AS SELECT * FROM read_parquet(%s)'
                  % (name, _sql_str(p)))

    # ---- 站点维表 + 关联宽表 ----
    st = os.path.join(MY_DIR, "weather_station_info.parquet")
    if os.path.exists(st):
        c.execute('CREATE OR REPLACE VIEW stations AS SELECT * FROM read_parquet(%s)' % _sql_str(st))
        c.execute("""
            CREATE OR REPLACE VIEW weather_joined AS
            SELECT w.*, s.station_code, s.province, s.station_name,
                   s.latitude, s.longitude, s.altitude
            FROM weather w
            LEFT JOIN stations s ON w.station_id = s.station_id
        """)
    return c


_CACHE = {}


def get_con():
    if "c" not in _CACHE:
        _CACHE["c"] = con()
    return _CACHE["c"]


def q(sql, fetch=True):
    """跑一条 SQL；fetch=True 返回 pandas DataFrame，False 只执行"""
    c = get_con()
    if not fetch:
        c.execute(sql)
        return None
    return c.execute(sql).df()


def show_views():
    return q("SELECT table_name AS view_name, table_type FROM information_schema.tables "
             "WHERE table_schema='main' ORDER BY table_name")


def show_cols(view):
    return q("DESCRIBE %s" % view)


# ---------------- 交互式 ----------------
BANNER = """本地实验环境（离线，不连数据库）
  weather         TDengine 876 个子表全量（含哨兵值）
  weather_ok      已过滤哨兵/异常值（做统计用这个）
  stations        MySQL weather_station_info（909 站点）
  weather_joined  weather + 站点信息宽表
  mysql_<表名>     MySQL 31 张表
命令： .views  .cols <视图>  .count <视图>  .quit"""


def interactive():
    print(Q := BANNER)
    while True:
        try:
            line = input("lab> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in (".quit", ".exit", "quit", "exit"):
            break
        if line == ".views":
            print(show_views().to_string(index=False))
            continue
        if line.startswith(".cols"):
            print(show_cols(line.split(None, 1)[1]).to_string(index=False))
            continue
        if line.startswith(".count"):
            v = line.split(None, 1)[1]
            print(q("SELECT count(*) AS n FROM %s" % v).to_string(index=False))
            continue
        try:
            df = q(line)
            print(df.to_string(max_rows=100))
        except Exception as ex:
            print("[SQL error] %s: %s" % (type(ex).__name__, ex))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("-q", "--query"):
        sql = sys.argv[2] if len(sys.argv) > 2 else sys.stdin.read()
        print(q(sql).to_string(index=False))
    elif len(sys.argv) > 1 and sys.argv[1] == "-v":
        print(show_views().to_string(index=False))
    else:
        interactive()
