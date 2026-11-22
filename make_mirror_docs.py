# -*- coding: utf-8 -*-
"""
根据 db_mirror/_mirror_manifest.json 生成本地镜像说明文档 + 数据体检报告。
完全离线，不连任何数据库。

输出：
  D:\\Android PDF\\db_mirror\\本地镜像说明.md
  D:\\Android PDF\\db_mirror\\本地镜像说明.html
  D:\\Android PDF\\db_mirror\\数据体检报告.md
"""
import os, json, html, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "db_mirror")
MANIFEST = os.path.join(ROOT, "_mirror_manifest.json")

SENTINELS = {
    32766: "缺测/未观测（气象业务常用哨兵）",
    32767: "缺测（FLOAT 上限哨兵）",
    999999: "缺测（业务系统哨兵）",
    9999990: "缺测（放大 10 倍的哨兵）",
    32700: "微量降水/天气现象编码",
}


def gb(n):
    return "%.2f GB" % (n / 1024 ** 3) if n >= 1024 ** 3 else "%.1f MB" % (n / 1e6)


def build():
    with open(MANIFEST, encoding="utf-8") as f:
        m = json.load(f)
    td = m.get("tdengine", {})
    my = m.get("mysql", {})

    rows = []
    rows.append(("TDengine 子表文件", "%d 个 parquet" % len(td.get("station_rows", {}))))
    rows.append(("TDengine 本地行数", "{:,}".format(td.get("local_rows", 0))))
    rows.append(("TDengine 服务端行数", "{:,}".format(td.get("server_reported_rows", 0))))
    rows.append(("TDengine 行数是否一致", "✅ 一致" if td.get("rows_match") else "⚠️ 不一致"))
    rows.append(("TDengine 占用空间", gb(td.get("local_bytes", 0))))
    rows.append(("TDengine 拉取失败", "%d 个" % len(td.get("failures", []))))
    rows.append(("MySQL 表数", "%d 张" % my.get("table_count", 0)))
    rows.append(("MySQL 行数合计", "{:,}".format(my.get("total_rows", 0))))
    rows.append(("MySQL 占用空间", gb(my.get("total_bytes", 0))))
    rows.append(("生成时间", m.get("generated_at", "")))

    L = []
    L.append("# 本地镜像说明\n")
    L.append("> 数据来源：TDengine 库 `%s` + MySQL 库 `%s`（云 RDS）。\n" % (m.get("tdengine", {}).get("database", "-"), m.get("mysql", {}).get("database", "-")))
    L.append("> 抓取过程全程**只读**（仅 SELECT / SHOW / DESCRIBE），**源库没有被修改**。\n")
    L.append("> 镜像完成后**不依赖网络**，断网也能做全部本地实验。\n")
    L.append("\n## 一、镜像概况\n")
    L.append("| 项目 | 值 |\n|---|---|\n")
    for k, v in rows:
        L.append("| %s | %s |\n" % (k, v))

    L.append("\n## 二、目录结构\n")
    L.append("```\nD:\\Android PDF\\\n")
    L.append("├─ db_mirror\\\n")
    L.append("│  ├─ tdengine\\weather_data\\station_<id>.parquet   876 个气象子表，一个站一个文件\n")
    L.append("│  ├─ mysql\\<表名>.parquet                         MySQL 31 张表\n")
    L.append("│  ├─ _mirror_manifest.json                       清单（行数/大小/校验）\n")
    L.append("│  └─ 本地镜像说明.md / .html                      本文档\n")
    L.append("├─ local_lab.py                                   本地查询工具（DuckDB）\n")
    L.append("├─ mirror_dbs.py                                  镜像脚本（可重跑/断点续传）\n")
    L.append("└─ pull_db_inventory.py                           原始盘点脚本\n```\n")

    L.append("\n## 三、parquet 字段说明\n")
    L.append("\n### weather_data（每个 station_<id>.parquet）\n")
    L.append("| 列名 | 类型 | 说明 |\n|---|---|---|\n")
    for c, t, d in [
        ("ts", "timestamp[us, UTC]", "原始时间戳（UTC，与源库一致）"),
        ("ts_bj", "timestamp[us]", "**北京时间**（ts + 8h，日常分析用这个）"),
        ("avg_temperature", "double", "平均气温 ℃（含哨兵值，见体检报告）"),
        ("max_temperature", "double", "最高气温 ℃"),
        ("min_temperature", "double", "最低气温 ℃"),
        ("avg_humidity", "int64", "平均相对湿度 %"),
        ("precipitation", "double", "降水量 mm（含天气现象编码，见体检报告）"),
        ("avg_wind_speed", "double", "平均风速 m/s"),
        ("sunshine_hours", "double", "日照时数 h"),
        ("file_path", "string", "源 Excel 在 OSS 上的路径"),
        ("user_id", "int64", "上传用户 ID"),
        ("user_nickname", "string", "上传用户昵称"),
    ]:
        L.append("| `%s` | %s | %s |\n" % (c, t, d))
    L.append("\n> **注意**：源库子表 `SELECT *` 不返回 TAG 列（`station_id` / `station_code` / `uploaded_at`）。"
             "本地镜像里 `station_id` 由文件名 `station_<id>.parquet` 解析得到，"
             "在 `local_lab.py` 的 `weather` 视图中已自动补上；"
             "站点维信息（省/站名/经纬度/海拔）通过 `weather_joined` 视图关联得到。\n")

    L.append("\n## 四、怎么用\n")
    L.append("\n### 1) 命令行单条查询\n```powershell\n")
    L.append('python "D:\\Android PDF\\local_lab.py" -q "SELECT * FROM weather LIMIT 10"\n```\n')
    L.append("\n### 2) 交互式\n```powershell\n")
    L.append('python "D:\\Android PDF\\local_lab.py"\n')
    L.append("lab> SELECT station_id, count(*) n FROM weather GROUP BY 1 ORDER BY n DESC LIMIT 10;\n")
    L.append("lab> .views\nlab> .cols weather_joined\nlab> .quit\n```\n")
    L.append("\n### 3) Python 里用\n```python\n")
    L.append("import sys; sys.path.insert(0, r'D:\\Android PDF')\n")
    L.append("import local_lab\n")
    L.append("df = local_lab.q('SELECT province, count(*) FROM weather_joined GROUP BY 1')\n")
    L.append("```\n")

    L.append("\n### 4) 内置视图\n")
    L.append("| 视图 | 内容 |\n|---|---|\n")
    for v, d in [
        ("weather", "TDengine 全部 876 个子表（1669 万行），**含哨兵值**，带 station_id"),
        ("weather_ok", "已按物理合理范围过滤哨兵/异常值，**做统计推荐用这个**"),
        ("stations", "MySQL weather_station_info，909 个站点维表"),
        ("weather_joined", "weather + 站点维表关联宽表（带省/站名/经纬度/海拔）"),
        ("mysql_<表名>", "MySQL 的 31 张表，逐表一个视图，如 mysql_raster_asset"),
    ]:
        L.append("| `%s` | %s |\n" % (v, d))

    L.append("\n## 五、重跑 / 更新镜像\n")
    L.append("```powershell\n")
    L.append("# 断点续传（已下载且行数吻合的跳过）\n")
    L.append('python "D:\\Android PDF\\mirror_dbs.py"\n\n')
    L.append("# 强制全部重拉\n")
    L.append('python "D:\\Android PDF\\mirror_dbs.py" --force\n\n')
    L.append("# 只更新 MySQL\n")
    L.append('python "D:\\Android PDF\\mirror_dbs.py" --skip-td\n```\n')

    with open(os.path.join(ROOT, "本地镜像说明.md"), "w", encoding="utf-8") as f:
        f.write("".join(L))

    # ---------------- HTML ----------------
    esc = lambda x: html.escape("" if x is None else str(x))
    H = []
    H.append("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>本地镜像说明</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1f2328;--mut:#656d76;--line:#d8dee4;--acc:#0969da;--ok:#1a7f37}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1100px;margin:0 auto;padding:30px 20px 70px}
h1{font-size:27px;margin:0 0 8px}h2{font-size:20px;margin:34px 0 14px;padding-bottom:8px;border-bottom:2px solid var(--acc)}
h3{font-size:16px;margin:22px 0 8px}
.meta{color:var(--mut);font-size:13px;margin-bottom:18px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:170px;flex:1}
.card .k{color:var(--mut);font-size:12px}.card .v{font-size:20px;font-weight:700;margin-top:2px}
.box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;margin:12px 0;overflow:auto}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th,td{border:1px solid var(--line);padding:6px 9px;text-align:left;vertical-align:top}
th{background:#eef2f6;white-space:nowrap}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12px}
pre{background:#0d1117;color:#c9d1d9;padding:13px;border-radius:8px;overflow:auto;font-size:12.5px;line-height:1.5}
.note{background:#fff8c5;border-left:4px solid #d4a72c;padding:11px 14px;border-radius:6px;margin:14px 0}
.ok{color:var(--ok);font-weight:700}
</style></head><body><div class="wrap">""")
    H.append("<h1>本地镜像说明</h1>")
    H.append("<div class='meta'>数据来源：TDengine 库 <code>%s</code> ＋ MySQL 库 <code>%s</code>　｜　"
             "抓取全程<b>只读</b>，源库未被修改　｜　生成时间 %s</div>" % (esc(m.get("tdengine", {}).get("database", "-")), esc(m.get("mysql", {}).get("database", "-")), esc(m.get("generated_at"))))
    H.append("<div class='cards'>")
    H.append("<div class='card'><div class='k'>TDengine 气象数据</div><div class='v'>%s 行</div></div>"
             % "{:,}".format(td.get("local_rows", 0)))
    H.append("<div class='card'><div class='k'>站点数（子表）</div><div class='v'>%d</div></div>"
             % len(td.get("station_rows", {})))
    H.append("<div class='card'><div class='k'>MySQL 表 / 行</div><div class='v'>%d / %s</div></div>"
             % (my.get("table_count", 0), "{:,}".format(my.get("total_rows", 0))))
    H.append("<div class='card'><div class='k'>本地占用</div><div class='v'>%s</div></div>"
             % gb(td.get("local_bytes", 0) + my.get("total_bytes", 0)))
    H.append("</div>")
    H.append("<div class='box'><b>校验：</b>本地 %s 行 vs 服务端 %s 行 → <span class='ok'>%s</span></div>"
             % ("{:,}".format(td.get("local_rows", 0)), "{:,}".format(td.get("server_reported_rows", 0)),
                "一致 ✅" if td.get("rows_match") else "不一致 ⚠️"))

    H.append("<h2>一、目录结构</h2>")
    H.append("<pre>D:\\Android PDF\\\n├─ db_mirror\\\n│  ├─ tdengine\\weather_data\\station_&lt;id&gt;.parquet  876 个气象子表\n│  ├─ mysql\\&lt;表名&gt;.parquet                        MySQL 31 张表\n│  ├─ _mirror_manifest.json                        清单\n│  └─ 本地镜像说明.md / .html\n├─ local_lab.py                                    本地查询工具（DuckDB）\n├─ mirror_dbs.py                                   镜像脚本（断点续传）\n└─ pull_db_inventory.py                            原始盘点脚本</pre>")

    H.append("<h2>二、parquet 字段说明</h2>")
    H.append("<table><thead><tr><th>列名</th><th>类型</th><th>说明</th></tr></thead><tbody>")
    for c, t, d in [
        ("ts", "timestamp[us, UTC]", "原始时间戳（UTC，与源库一致）"),
        ("ts_bj", "timestamp[us]", "<b>北京时间</b>（ts + 8h，日常分析用这个）"),
        ("avg_temperature", "double", "平均气温 ℃（含哨兵值）"),
        ("max_temperature", "double", "最高气温 ℃"),
        ("min_temperature", "double", "最低气温 ℃"),
        ("avg_humidity", "int64", "平均相对湿度 %"),
        ("precipitation", "double", "降水量 mm（含天气现象编码）"),
        ("avg_wind_speed", "double", "平均风速 m/s"),
        ("sunshine_hours", "double", "日照时数 h"),
        ("file_path", "string", "源 Excel 在 OSS 上的路径"),
        ("user_id", "int64", "上传用户 ID"),
        ("user_nickname", "string", "上传用户昵称"),
    ]:
        H.append("<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>" % (esc(c), esc(t), esc(d)))
    H.append("</tbody></table>")
    H.append("<div class='note'><b>注意</b>：源库子表 <code>SELECT *</code> 不返回 TAG 列"
             "（<code>station_id</code>/<code>station_code</code>/<code>uploaded_at</code>）。"
             "本地 <code>station_id</code> 由文件名 <code>station_&lt;id&gt;.parquet</code> 解析得到，"
             "<code>local_lab.py</code> 的 <code>weather</code> 视图已自动补上；"
             "站点维信息通过 <code>weather_joined</code> 关联得到。</div>")

    H.append("<h2>三、怎么用</h2>")
    H.append("<h3>命令行单条查询</h3><pre>python \"D:\\Android PDF\\local_lab.py\" -q \"SELECT * FROM weather LIMIT 10\"</pre>")
    H.append("<h3>交互式</h3><pre>python \"D:\\Android PDF\\local_lab.py\"\n"
             "lab&gt; SELECT station_id, count(*) n FROM weather GROUP BY 1 ORDER BY n DESC LIMIT 10;\n"
             "lab&gt; .views\nlab&gt; .cols weather_joined\nlab&gt; .quit</pre>")
    H.append("<h3>Python 里用</h3><pre>import sys; sys.path.insert(0, r'D:\\Android PDF')\n"
             "import local_lab\ndf = local_lab.q('SELECT province, count(*) FROM weather_joined GROUP BY 1')</pre>")
    H.append("<h3>内置视图</h3><table><thead><tr><th>视图</th><th>内容</th></tr></thead><tbody>")
    for v, d in [
        ("weather", "TDengine 全部 876 个子表（1669 万行），<b>含哨兵值</b>，带 station_id"),
        ("weather_ok", "已按物理合理范围过滤哨兵/异常值，<b>做统计推荐用这个</b>"),
        ("stations", "MySQL weather_station_info，909 个站点维表"),
        ("weather_joined", "weather + 站点维表关联宽表（带省/站名/经纬度/海拔）"),
        ("mysql_&lt;表名&gt;", "MySQL 31 张表逐表一个视图，如 mysql_raster_asset"),
    ]:
        H.append("<tr><td><code>%s</code></td><td>%s</td></tr>" % (v, d))
    H.append("</tbody></table>")

    H.append("<h2>四、重跑 / 更新镜像</h2>")
    H.append("<pre># 断点续传（已下载且行数吻合的跳过）\npython \"D:\\Android PDF\\mirror_dbs.py\"\n\n"
             "# 强制全部重拉\npython \"D:\\Android PDF\\mirror_dbs.py\" --force\n\n"
             "# 只更新 MySQL\npython \"D:\\Android PDF\\mirror_dbs.py\" --skip-td</pre>")
    H.append("</div></body></html>")

    with open(os.path.join(ROOT, "本地镜像说明.html"), "w", encoding="utf-8") as f:
        f.write("".join(H))
    print("已生成：本地镜像说明.md / 本地镜像说明.html")


# ---------------- 数据体检（离线跑 duckdb） ----------------
def health_report():
    import local_lab
    L = []
    L.append("# 本地镜像数据体检报告\n")
    L.append("生成时间：%s　|　全部在本地 parquet 上跑，不连数据库\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    tot = local_lab.q("SELECT count(*) n, count(DISTINCT station_id) s, min(ts_bj) t0, max(ts_bj) t1 FROM weather")
    t = tot.iloc[0]
    L.append("\n## 一、总览\n")
    L.append("| 项目 | 值 |\n|---|---|\n")
    L.append("| 总行数 | %s |\n" % "{:,}".format(int(t["n"])))
    L.append("| 站点数 | %d |\n" % int(t["s"]))
    L.append("| 时间范围（北京） | %s ~ %s |\n" % (t["t0"], t["t1"]))

    ok = local_lab.q("SELECT count(*) n FROM weather_ok")
    L.append("| 通过物理范围校验 | %s（%.2f%%） |\n" % (
        "{:,}".format(int(ok.iloc[0]["n"])), 100.0 * ok.iloc[0]["n"] / t["n"]))

    L.append("\n## 二、哨兵值 / 异常值统计\n")
    dq = local_lab.q("""
        SELECT
          sum(CASE WHEN avg_temperature >= 32766 OR avg_temperature = 999999 THEN 1 ELSE 0 END) AS avg_temp_sentinel,
          sum(CASE WHEN max_temperature >= 32766 OR max_temperature = 999999 THEN 1 ELSE 0 END) AS max_temp_sentinel,
          sum(CASE WHEN min_temperature >= 32766 OR min_temperature = 999999 THEN 1 ELSE 0 END) AS min_temp_sentinel,
          sum(CASE WHEN sunshine_hours  >= 32766 OR sunshine_hours = 999999 THEN 1 ELSE 0 END) AS sunshine_sentinel,
          sum(CASE WHEN avg_wind_speed  >= 32766 THEN 1 ELSE 0 END) AS wind_sentinel,
          sum(CASE WHEN avg_humidity > 100 THEN 1 ELSE 0 END) AS humidity_gt100,
          sum(CASE WHEN precipitation > 1000 THEN 1 ELSE 0 END) AS precip_gt1000
        FROM weather
    """).iloc[0]
    labels = {
        "avg_temp_sentinel": "avg_temperature 哨兵(≥32766/999999)",
        "max_temp_sentinel": "max_temperature 哨兵",
        "min_temp_sentinel": "min_temperature 哨兵",
        "sunshine_sentinel": "sunshine_hours 哨兵",
        "wind_sentinel": "avg_wind_speed 哨兵",
        "humidity_gt100": "avg_humidity > 100",
        "precip_gt1000": "precipitation > 1000（多为天气现象编码）",
    }
    L.append("| 异常 | 行数 | 占比 |\n|---|---|---|\n")
    for k, lab in labels.items():
        v = int(dq[k])
        L.append("| %s | %s | %.3f%% |\n" % (lab, "{:,}".format(v), 100.0 * v / t["n"]))

    L.append("\n## 三、清理后的真实统计（weather_ok）\n")
    st = local_lab.q("""
        SELECT round(avg(avg_temperature),2) temp_avg, round(min(avg_temperature),1) temp_min,
               round(max(avg_temperature),1) temp_max, round(avg(avg_humidity),1) hum_avg,
               round(avg(precipitation),2) prcp_avg, round(max(precipitation),1) prcp_max,
               round(avg(avg_wind_speed),2) wind_avg, round(avg(sunshine_hours),2) sun_avg
        FROM weather_ok
    """).iloc[0]
    L.append("| 指标 | 值 |\n|---|---|\n")
    for k, lab in [("temp_avg", "平均气温 ℃"), ("temp_min", "最低气温 ℃"), ("temp_max", "最高气温 ℃"),
                   ("hum_avg", "平均湿度 %"), ("prcp_avg", "平均降水 mm"), ("prcp_max", "最大降水 mm"),
                   ("wind_avg", "平均风速 m/s"), ("sun_avg", "平均日照 h")]:
        L.append("| %s | %s |\n" % (lab, st[k]))

    L.append("\n## 四、按省份分布（关联 MySQL 站点维表，已过滤哨兵值）\n")
    L.append("\n> 注：源库 `weather_station_info.province` 存在**命名不统一**（如「新疆」与「新疆维吾尔自治区」、"
             "「河北」与「河北省」、「河南」与「河南省」并存），下表按原样展示，做聚合前建议先归一化。\n\n")
    try:
        pv = local_lab.q("""
            SELECT coalesce(province,'(未知)') AS 省份, count(DISTINCT w.station_id) AS 站点数,
                   count(*) AS 行数, round(avg(w.avg_temperature),2) AS 平均气温
            FROM weather_ok w LEFT JOIN stations s ON w.station_id=s.station_id
            GROUP BY 1 ORDER BY 行数 DESC
        """)
        L.append("| 省份 | 站点数 | 行数 | 平均气温 ℃ |\n|---|---|---|---|\n")
        for _, r in pv.iterrows():
            L.append("| %s | %s | %s | %s |\n" % (r["省份"], r["站点数"], "{:,}".format(int(r["行数"])), r["平均气温"]))
    except Exception as ex:
        L.append("关联统计失败：%s\n" % ex)

    L.append("\n## 五、省份命名归一化核对\n")
    try:
        du = local_lab.q("""
            SELECT province AS 原值, count(*) AS 站点数
            FROM stations GROUP BY 1 ORDER BY 1
        """)
        L.append("| 省份原值 | 站点数 |\n|---|---|\n")
        for _, r in du.iterrows():
            L.append("| %s | %s |\n" % (r["原值"], r["站点数"]))
    except Exception as ex:
        L.append("统计失败：%s\n" % ex)

    L.append("\n## 五、MySQL 各表盘点\n")
    try:
        import glob
        files = sorted(glob.glob(os.path.join(ROOT, "mysql", "*.parquet")))
        L.append("| 表 | 行数 | 列数 |\n|---|---|---|\n")
        import pyarrow.parquet as pq
        for p in files:
            pf = pq.ParquetFile(p)
            L.append("| `%s` | %s | %d |\n" % (
                os.path.splitext(os.path.basename(p))[0],
                "{:,}".format(pf.metadata.num_rows), pf.metadata.num_columns))
    except Exception as ex:
        L.append("统计失败：%s\n" % ex)

    with open(os.path.join(ROOT, "数据体检报告.md"), "w", encoding="utf-8") as f:
        f.write("".join(L))
    print("已生成：数据体检报告.md")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "health":
        health_report()
    else:
        build()
