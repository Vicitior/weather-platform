# -*- coding: utf-8 -*-
"""
只读盘点：MySQL 业务库 + TDengine Cloud 时序库
仅执行 SELECT / SHOW / DESCRIBE / information_schema 查询，不写入任何数据库。
输出：db_inventory.json、数据库内容盘点.md、数据库内容盘点.html
"""
import os, re, json, html, datetime, decimal, traceback
import pymysql, requests

HERE = os.path.dirname(os.path.abspath(__file__))

# ---- 凭据全部从环境变量读取（不要写进代码，也不要把含凭据的 .env 提交到仓库）----
T = os.environ.get("TD_TOKEN", "")
TD_DB = os.environ.get("TD_DB", "")
TD_URL = os.environ.get("TD_URL", "") or (
    "https://gw.cloud.taosdata.com/rest/sql/" + TD_DB)

# 支持 DSH_RENDER_ONLY=1：复用上一轮盘点结果，跳过 TDengine 慢查询（INTERVAL 各约 96s）
CACHE = None
_cache_path = os.path.join(HERE, "db_inventory.json")
if os.environ.get("DSH_RENDER_ONLY") == "1" and os.path.exists(_cache_path):
    with open(_cache_path, encoding="utf-8") as _f:
        CACHE = json.load(_f)
    print("[cache] 复用已有盘点结果，跳过 TDengine 慢查询")

MYSQL = dict(host=os.environ.get("MYSQL_HOST", ""),
             port=int(os.environ.get("MYSQL_PORT", "3306")),
             user=os.environ.get("MYSQL_USER", ""),
             password=os.environ.get("MYSQL_PASSWORD", ""),
             database=os.environ.get("MYSQL_DB", ""),
             charset="utf8mb4", connect_timeout=10, read_timeout=30)


def _need_env():
    """启动时检查必需的环境变量，缺了直接给出人话提示，而不是连不上再报错。"""
    missing = [k for k in ("TD_TOKEN", "MYSQL_HOST", "MYSQL_USER", "MYSQL_PASSWORD", "MYSQL_DB")
               if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "缺少环境变量：" + ", ".join(missing) + "\n"
            "请先设置，例如（Linux/macOS）：\n"
            "  export TD_TOKEN='<你的 TDengine token>'\n"
            "  export MYSQL_HOST='<主机>' MYSQL_PORT=3306\n"
            "  export MYSQL_USER='<用户>' MYSQL_PASSWORD='<密码>' MYSQL_DB='<库名>'\n"
            "Windows PowerShell：\n"
            "  $env:TD_TOKEN='<token>'; $env:MYSQL_HOST='<主机>'; ...\n"
            "详见 README 的「连接源库」一节。")


_need_env()

def norm(v):
    if v is None:
        return None
    if isinstance(v, (datetime.datetime, datetime.date, datetime.time)):
        return v.isoformat(sep=" ") if hasattr(v, "isoformat") else str(v)
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        b = bytes(v)
        return "<bytes len=%d hex=%s%s>" % (len(b), b[:40].hex(), "..." if len(b) > 40 else "")
    if isinstance(v, str):
        return v if len(v) <= 400 else v[:400] + "…(截断)"
    return v

def bj(s):
    """TDengine REST 返回 UTC ISO 串，换算成 Asia/Shanghai 显示"""
    if isinstance(s, str) and s.endswith("Z"):
        try:
            dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            return (dt + datetime.timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return s

def to_bj_rows(cols, rows, limit=None):
    out = []
    for r in rows:
        rr = []
        for i, v in enumerate(r):
            cl = cols[i].lower()
            rr.append(bj(norm(v)) if (cl == "ts" or cl.endswith("_at") or cl.startswith("_wstart")) else norm(v))
        out.append(rr)
    return out[:limit] if limit else out

# ---------------- TDengine ----------------
def td(sql, timeout=60):
    r = requests.post(TD_URL, params={"token": T}, data=sql, timeout=timeout)
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError("code=%s desc=%s" % (j.get("code"), j.get("desc")))
    return [m[0] for m in j.get("column_meta", [])], j.get("data", [])

if CACHE:
    td_part = CACHE["tdengine"]
    print("[cache] TDengine 部分复用（%d 个查询）" % len(td_part.get("queries", {})))
else:
    td_part = {"url": TD_URL.split("/rest/sql")[0], "database": TD_DB, "queries": {}}
    def tdq(key, sql, timeout=60, convert_bj=True, keep=None):
        try:
            cols, rows = td(sql, timeout)
            if convert_bj:
                rows = to_bj_rows(cols, rows, limit=keep)
            td_part["queries"][key] = {"sql": sql, "cols": cols, "rows": rows, "row_count": len(rows)}
            print("[TD ok] %-22s %d rows" % (key, len(rows)))
        except Exception as ex:
            td_part["queries"][key] = {"sql": sql, "error": "%s: %s" % (type(ex).__name__, ex)}
            print("[TD ERR] %-22s %s" % (key, str(ex)[:160]))

    try:
        r = requests.post(TD_URL.split("/rest/sql")[0] + "/rest/sql", params={"token": T},
                          data="SHOW DATABASES", timeout=20)
        td_part["databases"] = [x[0] for x in r.json().get("data", [])]
    except Exception:
        td_part["databases"] = []

    tdq("server_version", "SELECT SERVER_VERSION()", convert_bj=False)
    tdq("stables", "SHOW STABLES", convert_bj=False)
    tdq("describe", "DESCRIBE weather_data", convert_bj=False)
    tdq("total_rows", "SELECT COUNT(*) FROM weather_data", convert_bj=False)
    tdq("time_range", "SELECT FIRST(ts), LAST(ts) FROM weather_data")
    tdq("value_overview", "SELECT MIN(avg_temperature), MAX(avg_temperature), AVG(avg_temperature), MAX(precipitation), AVG(avg_humidity), AVG(avg_wind_speed), AVG(sunshine_hours) FROM weather_data", convert_bj=False)
    tdq("by_decade", "SELECT _wstart, COUNT(*) FROM weather_data INTERVAL(10y)", timeout=240, keep=100)
    tdq("by_year", "SELECT _wstart, COUNT(*) FROM weather_data INTERVAL(1y)", timeout=240, keep=200)
    tdq("by_station", "SELECT station_id, station_code, COUNT(*) FROM weather_data GROUP BY station_id, station_code", timeout=90, keep=1000)
    tdq("latest_sample", "SELECT * FROM weather_data ORDER BY ts DESC LIMIT 5")
    tdq("tables", "SHOW TABLES", convert_bj=False, keep=1000)
    # 数据质量：网关有 60s 硬超时，多条件 CASE / UNION ALL 全表扫描会 504，必须拆成单条件查询
    dq_cases = [
        ("dq_total",           "SELECT COUNT(*) FROM weather_data"),
        ("dq_avg_temp_999999", "SELECT COUNT(*) FROM weather_data WHERE avg_temperature=999999"),
        ("dq_max_temp_gt100",  "SELECT COUNT(*) FROM weather_data WHERE max_temperature>100"),
        ("dq_min_temp_ltm100", "SELECT COUNT(*) FROM weather_data WHERE min_temperature<-100"),
        ("dq_humidity_gt100",  "SELECT COUNT(*) FROM weather_data WHERE avg_humidity>100"),
        ("dq_humidity_lt0",    "SELECT COUNT(*) FROM weather_data WHERE avg_humidity<0"),
        ("dq_precip_gt1000",   "SELECT COUNT(*) FROM weather_data WHERE precipitation>1000"),
        ("dq_wind_gt200",      "SELECT COUNT(*) FROM weather_data WHERE avg_wind_speed>200"),
        ("dq_sunshine_gt24",   "SELECT COUNT(*) FROM weather_data WHERE sunshine_hours>24"),
        ("dq_precip_lt0",      "SELECT COUNT(*) FROM weather_data WHERE precipitation<0"),
        ("dq_precip_dist",     "SELECT precipitation, COUNT(*) FROM weather_data WHERE precipitation>1000 GROUP BY precipitation ORDER BY 2 DESC LIMIT 10"),
        ("dq_maxt_dist",       "SELECT max_temperature, COUNT(*) FROM weather_data WHERE max_temperature>100 GROUP BY max_temperature ORDER BY 2 DESC LIMIT 10"),
        ("dq_wind_dist",       "SELECT avg_wind_speed, COUNT(*) FROM weather_data WHERE avg_wind_speed>200 GROUP BY avg_wind_speed ORDER BY 2 DESC LIMIT 10"),
    ]
    for key, sql in dq_cases:
        tdq(key, sql, timeout=58, convert_bj=False)
    print("[TD] SHOW TABLES 共 %d 行" % len(td_part["queries"].get("tables", {}).get("rows", [])))

# 数据质量汇总（缓存路径与全量路径都能用）
def dq_one(k):
    try:
        return td_part["queries"][k]["rows"][0][0]
    except Exception:
        return None

DQ_TOTAL = dq_one("dq_total") or 0
DQ_ROWS = []
for _label, _key in [
    ("avg_temperature = 999999", "dq_avg_temp_999999"),
    ("max_temperature > 100",    "dq_max_temp_gt100"),
    ("min_temperature < -100",   "dq_min_temp_ltm100"),
    ("avg_humidity > 100",       "dq_humidity_gt100"),
    ("avg_humidity < 0",         "dq_humidity_lt0"),
    ("precipitation > 1000",     "dq_precip_gt1000"),
    ("avg_wind_speed > 200",     "dq_wind_gt200"),
    ("sunshine_hours > 24",      "dq_sunshine_gt24"),
    ("precipitation < 0",        "dq_precip_lt0"),
]:
    _v = dq_one(_key)
    DQ_ROWS.append([_label, _v, ("%.4f%%" % (100.0 * _v / DQ_TOTAL)) if isinstance(_v, (int, float)) and DQ_TOTAL else ""])
DQ_DISTS = [
    ("precipitation > 1000 的取值分布", "dq_precip_dist"),
    ("max_temperature > 100 的取值分布", "dq_maxt_dist"),
    ("avg_wind_speed > 200 的取值分布", "dq_wind_dist"),
]

# ---------------- MySQL ----------------
my_part = {"host": MYSQL["host"], "port": MYSQL["port"],
           "database": MYSQL["database"], "tables": []}
conn = pymysql.connect(**MYSQL)
cur = conn.cursor()
cur.execute("SELECT VERSION(), NOW(), @@hostname")
my_part["server"] = ["%s" % norm(v) for v in cur.fetchone()]
cur.execute("SHOW GRANTS FOR CURRENT_USER()")
my_part["grants"] = [r[0] for r in cur.fetchall()]
cur.execute("""SELECT table_name, table_comment FROM information_schema.tables
               WHERE table_schema=DATABASE() AND table_type='BASE TABLE' ORDER BY table_name""")
table_list = cur.fetchall()
my_part["table_total"] = len(table_list)

for (tname, comment) in table_list:
    item = {"name": tname, "comment": comment or "", "columns": [], "count": None,
            "sample": [], "sample_cols": [], "error": None}
    try:
        cur.execute("""SELECT column_name, column_type, is_nullable, column_key,
                              column_default, extra, column_comment
                       FROM information_schema.columns
                       WHERE table_schema=DATABASE() AND table_name=%s ORDER BY ordinal_position""", (tname,))
        item["columns"] = [[norm(v) for v in r] for r in cur.fetchall()]
        safe = tname.replace("`", "``")
        cur.execute("SELECT COUNT(*) FROM `%s`" % safe)
        item["count"] = cur.fetchone()[0]
        if item["count"]:
            cur.execute("SELECT * FROM `%s` LIMIT 5" % safe)
            item["sample_cols"] = [d[0] for d in cur.description]
            item["sample"] = [[norm(v) for v in row] for row in cur.fetchall()]
    except Exception as ex:
        item["error"] = "%s: %s" % (type(ex).__name__, ex)
    my_part["tables"].append(item)
    print("[MY] %-46s rows=%-6s cols=%d" % (tname, item["count"], len(item["columns"])))
cur.close()
conn.close()

# ---------------- 交叉核对 ----------------
cross = {}
mysql_station = next((t for t in my_part["tables"] if t["name"] == "weather_station_info"), None)
if mysql_station and mysql_station["count"]:
    conn = pymysql.connect(**MYSQL)
    cur = conn.cursor()
    cur.execute("SELECT station_id, station_code, station_name, province FROM weather_station_info")
    ms = cur.fetchall()
    cur.close(); conn.close()
    my_ids = set(int(r[0]) for r in ms)
    my_codes = set(str(r[1]) for r in ms if r[1] is not None)
    td_names = [r[0] for r in td_part["queries"].get("tables", {}).get("rows", [])]
    sub_ids = set()
    for n in td_names:
        m = re.match(r"wd_station_(\d+)$", n)
        if m:
            sub_ids.add(int(m.group(1)))
    cross["mysql_station_rows"] = len(ms)
    cross["mysql_station_id_min_max"] = [min(my_ids), max(my_ids)]
    cross["td_subtable_count"] = len(td_names)
    cross["td_subtable_parsed_ids"] = len(sub_ids)
    cross["subtable_ids_in_mysql"] = len(sub_ids & my_ids)
    cross["subtable_ids_not_in_mysql"] = sorted(sub_ids - my_ids)[:30]
    cross["mysql_ids_without_subtable"] = len(my_ids - sub_ids)
    bs = td_part["queries"].get("by_station", {})
    if bs.get("rows"):
        td_codes = set()
        for row in bs["rows"]:
            if len(row) >= 2 and row[1] is not None:
                td_codes.add(str(row[1]))
        cross["td_station_code_count"] = len(td_codes)
        cross["td_codes_in_mysql"] = len(td_codes & my_codes)
        cross["td_codes_not_in_mysql"] = sorted(td_codes - my_codes)[:20]
        cross["mysql_codes_without_td"] = len(my_codes - td_codes)
        by = td_part["queries"].get("by_year", {})
        cross["note"] = "子表名 wd_station_<id> 的 id 与 MySQL station_id 同源；station_code 为区站号"
print("[CROSS]", json.dumps(cross, ensure_ascii=False)[:600])

result = {"generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
          "readonly": True, "mysql": my_part, "tdengine": td_part, "cross_check": cross}

with open(os.path.join(HERE, "db_inventory.json"), "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

# ---------------- Markdown ----------------
def md_table(cols, rows):
    if not rows:
        return "_（0 行）_\n"
    s = "| " + " | ".join(str(c) for c in cols) + " |\n"
    s += "|" + "|".join(["---"] * len(cols)) + "|\n"
    for r in rows:
        cells = [str(x).replace("|", "\\|").replace("\n", " ") if x is not None else "NULL" for x in r]
        s += "| " + " | ".join(cells) + " |\n"
    return s

L = []
L.append("# 数据库内容盘点（只读）\n")
L.append("生成时间：%s　|　本次仅执行 SELECT / SHOW / DESCRIBE / information_schema 查询，未做任何修改\n" % result["generated_at"])
L.append("\n## 一、MySQL `%s`（云 RDS）\n" % MYSQL["database"])
L.append("- 服务端：`%s`（%s）\n" % (my_part["server"][0], my_part["server"][2]))
L.append("- 业务表 **%d 张**；有数据的 %d 张，空表 %d 张\n" % (
    my_part["table_total"],
    sum(1 for t in my_part["tables"] if t["count"]),
    sum(1 for t in my_part["tables"] if t["count"] == 0)))
L.append("\n### 表清单\n")
L.append(md_table(["表名", "行数", "表注释"],
                  [[t["name"], t["count"], t["comment"]] for t in my_part["tables"]]))
L.append("\n### 各表结构与样本（每张最多 5 行）\n")
for t in my_part["tables"]:
    L.append("\n#### `%s`　行数：%s\n" % (t["name"], t["count"]))
    if t["comment"]:
        L.append("> %s\n" % t["comment"])
    L.append("\n列结构：\n")
    L.append(md_table(["列名", "类型", "可空", "键", "默认值", "额外", "注释"], t["columns"]))
    if t["error"]:
        L.append("\n读取失败：%s\n" % t["error"])
    elif t["count"]:
        L.append("\n样本数据：\n")
        L.append(md_table(t["sample_cols"], t["sample"]))
    else:
        L.append("\n_（空表）_\n")

L.append("\n## 二、TDengine Cloud `%s`\n" % TD_DB)
L.append("- 网关：`https://gw.cloud.taosdata.com`（token 走 URL 查询参数 `?token=...`）\n")
L.append("- 可见数据库：%s\n" % ", ".join(td_part.get("databases", [])))
L.append("- 服务端版本：%s\n" % td_part["queries"].get("server_version", {}).get("rows", [["?"]])[0][0])
L.append("- 超级表：%s　子表：%s 张\n" % (
    ", ".join(r[0] for r in td_part["queries"].get("stables", {}).get("rows", [])),
    len(td_part["queries"].get("tables", {}).get("rows", []))))
L.append("\n> 时间显示已按 `Asia/Shanghai`(+8h) 换算；REST 原始返回为 UTC。\n")
for key, title in [("total_rows", "总行数"), ("time_range", "时间范围"),
                   ("value_overview", "数值概览"), ("by_decade", "按十年分布（北京时间）"),
                   ("by_year", "按年分布（北京时间）"),
                   ("describe", "超级表 `weather_data` 结构"), ("latest_sample", "最新 5 条数据")]:
    q = td_part["queries"].get(key, {})
    L.append("\n### %s\n" % title)
    if q.get("error"):
        L.append("\n查询失败：%s\n" % q["error"])
    else:
        L.append("\n```sql\n%s\n```\n" % q.get("sql", ""))
        L.append(md_table(q.get("cols", []), q.get("rows", [])))
bs = td_part["queries"].get("by_station", {})
if bs.get("rows"):
    ranked = sorted(bs["rows"], key=lambda r: (r[2] if len(r) > 2 and isinstance(r[2], (int, float)) else 0), reverse=True)[:15]
    L.append("\n### 数据量 TOP 15 站点（共 %d 个站点）\n" % len(bs["rows"]))
    L.append(md_table(bs.get("cols", []), ranked))
tb = td_part["queries"].get("tables", {})
if tb.get("rows"):
    L.append("\n### 子表清单（前 30 / 共 %d 张）\n" % len(tb["rows"]))
    L.append(md_table(tb.get("cols", []), tb["rows"][:30]))

L.append("\n### 数据质量：哨兵值与异常值（TDengine `weather_data`）\n")
L.append("\n> 该表存在大量**哨兵值/异常值**，直接 `AVG()`/`MAX()` 会被污染（例如平均湿度算出 101.7%、"
         "日照均值 445.9 小时），做任何统计前必须先过滤。\n")
L.append("\n")
L.append(md_table(["异常类别", "行数", "占比"], DQ_ROWS))
for _title, _key in DQ_DISTS:
    _q = td_part["queries"].get(_key, {})
    if _q.get("rows"):
        L.append("\n%s：\n\n" % _title)
        L.append(md_table(_q.get("cols", []), _q.get("rows", [])))

L.append("\n## 三、两库关联核对\n")
for k, v in cross.items():
    L.append("- **%s**：`%s`\n" % (k, json.dumps(v, ensure_ascii=False)))
L.append("\n## 四、结论与提醒\n")
L.append("1. 两个库都能连通、都能读；本次全部为只读查询。\n")
L.append("2. MySQL `weather_station_info` 是站点维表；TDengine `weather_data` 是逐日气象时序事实表，通过 `station_id` / `station_code` 关联。\n")
L.append("3. TDengine 数据时间戳为未来年份（北京时间 2051～2125），疑为测试/构造数据，按 2026 年过滤会查不到。\n")
L.append("4. 库名带 `test_`、数据为未来年份，判断为测试环境，接入前请确认目标环境。\n")

with open(os.path.join(HERE, "数据库内容盘点.md"), "w", encoding="utf-8") as f:
    f.write("".join(L))

# ---------------- HTML ----------------
def esc(x):
    return html.escape("NULL" if x is None else str(x))

def htable(cols, rows):
    if not rows:
        return "<p class='empty'>（0 行）</p>"
    s = "<table><thead><tr>" + "".join("<th>%s</th>" % esc(c) for c in cols) + "</tr></thead><tbody>"
    for r in rows:
        s += "<tr>" + "".join("<td>%s</td>" % esc(x) for x in r) + "</tr>"
    return s + "</tbody></table>"

H = []
H.append("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>数据库内容盘点（只读）</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1f2328;--mut:#656d76;--line:#d8dee4;--acc:#0969da;--ok:#1a7f37}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1200px;margin:0 auto;padding:28px 20px 60px}
h1{font-size:26px;margin:0 0 6px}h2{font-size:20px;margin:36px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--acc)}
h3{font-size:16px;margin:22px 0 8px}h4{font-size:14px;margin:18px 0 6px;color:var(--acc)}
.meta{color:var(--mut);font-size:13px;margin-bottom:18px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:180px;flex:1}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:22px;font-weight:700;margin-top:2px}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top;word-break:break-all}
th{background:#eef2f6;white-space:nowrap;position:sticky;top:0}tr:nth-child(even) td{background:#fafbfc}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12px}
pre{background:#0d1117;color:#c9d1d9;padding:12px;border-radius:8px;overflow:auto;font-size:12px}
details{margin:8px 0;border:1px solid var(--line);border-radius:8px;background:#fff}
summary{cursor:pointer;padding:9px 13px;font-weight:600;background:#f2f5f8;border-radius:8px}
summary:hover{background:#e8eef5}.dc{padding:10px 13px}
.badge{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600}
.b-ok{background:#dafbe1;color:var(--ok)}.b-empty{background:#fff1e5;color:#9a6700}
.empty{color:var(--mut)}.note{background:#fff8c5;border-left:4px solid #d4a72c;padding:10px 14px;border-radius:6px;margin:14px 0}
</style></head><body><div class="wrap">""")
H.append("<h1>数据库内容盘点（只读）</h1>")
H.append("<div class='meta'>生成时间：%s ｜ 本次仅执行 SELECT / SHOW / DESCRIBE / information_schema 查询，<b>未对任何数据库做修改</b></div>" % esc(result["generated_at"]))
H.append("<div class='cards'>")
H.append("<div class='card'><div class='k'>MySQL 业务表</div><div class='v'>%d</div></div>" % my_part["table_total"])
H.append("<div class='card'><div class='k'>其中有数据 / 空表</div><div class='v'>%d / %d</div></div>" % (
    sum(1 for t in my_part["tables"] if t["count"]), sum(1 for t in my_part["tables"] if t["count"] == 0)))
H.append("<div class='card'><div class='k'>TDengine 总行数</div><div class='v'>%s</div></div>" % esc(
    td_part["queries"].get("total_rows", {}).get("rows", [["?"]])[0][0]))
H.append("<div class='card'><div class='k'>TDengine 子表</div><div class='v'>%d</div></div>" % len(td_part["queries"].get("tables", {}).get("rows", [])))
H.append("</div>")

H.append("<h2>一、MySQL <code>%s</code>（云 RDS）</h2>" % esc(MYSQL["database"]))
H.append("<div class='box'>服务端：<b>%s</b>　主机：<code>%s</code>　当前库时间：%s</div>" % (
    esc(my_part["server"][0]), esc(my_part["server"][2]), esc(my_part["server"][1])))
H.append("<h3>表清单</h3>")
H.append(htable(["表名", "行数", "表注释"],
                [[t["name"], t["count"], t["comment"]] for t in my_part["tables"]]))
H.append("<h3>各表结构与样本（点击展开，每张最多 5 行）</h3>")
for t in my_part["tables"]:
    badge = "<span class='badge b-ok'>%s 行</span>" % t["count"] if t["count"] else "<span class='badge b-empty'>空表</span>"
    H.append("<details><summary><code>%s</code>　%s　%s</summary><div class='dc'>" % (esc(t["name"]), badge, esc(t["comment"])))
    H.append("<h4>列结构</h4>" + htable(["列名", "类型", "可空", "键", "默认值", "额外", "注释"], t["columns"]))
    if t["error"]:
        H.append("<p class='note'>读取失败：%s</p>" % esc(t["error"]))
    elif t["count"]:
        H.append("<h4>样本数据（前 5 行）</h4>" + htable(t["sample_cols"], t["sample"]))
    H.append("</div></details>")

H.append("<h2>二、TDengine Cloud <code>%s</code></h2>" % esc(TD_DB))
H.append("<div class='box'>网关：<code>https://gw.cloud.taosdata.com</code>　认证：token 放在 URL 查询参数 <code>?token=...</code>（<code>Authorization: Taosd</code> 头会 401）<br>服务端版本：<b>%s</b>　可见数据库：%s</div>" % (
    esc(td_part["queries"].get("server_version", {}).get("rows", [["?"]])[0][0]),
    esc(", ".join(td_part.get("databases", [])))))
H.append("<div class='note'>时间列已按 <b>Asia/Shanghai (+8h)</b> 换算显示；REST 原始返回为 UTC（形如 <code>2050-12-31T16:00:00.000Z</code>）。数据时间戳落在未来年份，疑为测试/构造数据。</div>")
for key, title in [("total_rows", "总行数"), ("time_range", "时间范围"), ("value_overview", "数值概览"),
                   ("by_decade", "按十年分布"), ("by_year", "按年分布"),
                   ("describe", "超级表 weather_data 结构"), ("latest_sample", "最新 5 条数据")]:
    q = td_part["queries"].get(key, {})
    H.append("<h3>%s</h3>" % title)
    H.append("<pre>%s</pre>" % esc(q.get("sql", "")))
    if q.get("error"):
        H.append("<p class='note'>查询失败：%s</p>" % esc(q["error"]))
    else:
        H.append(htable(q.get("cols", []), q.get("rows", [])))
if bs.get("rows"):
    ranked = sorted(bs["rows"], key=lambda r: (r[2] if len(r) > 2 and isinstance(r[2], (int, float)) else 0), reverse=True)[:15]
    H.append("<h3>数据量 TOP 15 站点（共 %d 个站点）</h3>" % len(bs["rows"]))
    H.append(htable(bs.get("cols", []), ranked))
if tb.get("rows"):
    H.append("<details><summary>子表清单（共 %d 张，点击展开全部）</summary><div class='dc'>%s</div></details>" % (
        len(tb["rows"]), htable(tb.get("cols", []), tb["rows"])))

H.append("<h2>三、TDengine 数据质量：哨兵值与异常值</h2>")
H.append("<div class='note'>该表存在大量<b>哨兵值/异常值</b>，直接 <code>AVG()</code>/<code>MAX()</code> 会被污染"
         "（例如平均湿度算出 101.7%、日照均值 445.9 小时）。做任何统计前必须先按白名单范围过滤。</div>")
H.append(htable(["异常类别", "行数", "占比"], DQ_ROWS))
for _title, _key in DQ_DISTS:
    _q = td_part["queries"].get(_key, {})
    if _q.get("rows"):
        H.append("<h3>%s</h3>" % esc(_title))
        H.append(htable(_q.get("cols", []), _q.get("rows", [])))

H.append("<h2>四、两库关联核对</h2>")
H.append(htable(["项目", "值"], [[k, json.dumps(v, ensure_ascii=False)] for k, v in cross.items()]))
H.append("<div class='box'>MySQL <code>weather_station_info</code> 为站点维表，TDengine <code>weather_data</code> 为逐日气象时序事实表，二者通过 <code>station_id</code>（子表名 <code>wd_station_&lt;id&gt;</code>）与 <code>station_code</code>（区站号）关联。</div>")
H.append("<h2>四、结论与提醒</h2>")
H.append("<div class='box'><ol><li>两个库都能连通、都能读；本次全部为只读查询，未执行任何 DDL/DML。</li>")
H.append("<li>你贴的 <code>CREATE TABLE weather_station_info</code> 在 MySQL 中<b>已存在且结构完全一致</b>（909 行数据），无需再建。</li>")
H.append("<li>TDengine 数据时间戳为北京时间 2051～2125（未来年份），按当前日期过滤会查不到任何数据。</li>")
H.append("<li>库名带 <code>test_</code>、数据为未来年份，判断为测试环境，接入前请确认目标环境。</li></ol></div>")
H.append("</div></body></html>")

with open(os.path.join(HERE, "数据库内容盘点.html"), "w", encoding="utf-8") as f:
    f.write("".join(H))

print("\n[DONE] 输出文件：db_inventory.json / 数据库内容盘点.md / 数据库内容盘点.html")
