# -*- coding: utf-8 -*-
"""
从本地镜像 db_mirror\\mysql\\*.parquet 生成 MySQL 数据浏览页（全量内容，因为总共才 1749 行）。
输出：D:\\Android PDF\\db_mirror\\MySQL数据浏览.html
"""
import os, glob, html, datetime
import pandas as pd
import pyarrow.parquet as pq

HERE = os.path.dirname(os.path.abspath(__file__))
MY_DIR = os.path.join(HERE, "db_mirror", "mysql")
OUT = os.path.join(HERE, "db_mirror", "MySQL数据浏览.html")

esc = lambda x: html.escape("" if x is None else str(x))


def cell(v):
    """把一个值渲染成 HTML 单元格内容"""
    if v is None or (isinstance(v, float) and pd.isna(v)) or (v is pd.NA):
        return "<span class='null'>NULL</span>"
    if isinstance(v, bytes):
        n = len(v)
        head = v[:24].hex(" ")
        return "<code class='bin'>bytes[%d] %s%s</code>" % (n, head, " …" if n > 24 else "")
    s = str(v)
    if len(s) > 160:
        s = s[:160] + " <span class='more'>…(截断)</span>"
    return esc(s)


def main():
    files = sorted(glob.glob(os.path.join(MY_DIR, "*.parquet")))
    total_rows = 0
    parts = []
    toc = []

    for p in files:
        name = os.path.splitext(os.path.basename(p))[0]
        pf = pq.ParquetFile(p)
        nrows = pf.metadata.num_rows
        total_rows += nrows
        cols = [(f.name, str(f.type)) for f in pf.schema_arrow]
        df = pf.read().to_pandas()
        anchor = "t_%s" % name
        toc.append("<a href='#%s'>%s <span class='cnt'>%d</span></a>" % (anchor, esc(name), nrows))

        sec = ["<section id='%s'>" % anchor]
        sec.append("<h2><code>%s</code> <span class='badge'>%d 行 × %d 列</span></h2>"
                   % (esc(name), nrows, len(cols)))
        if nrows == 0:
            sec.append("<p class='empty'>空表（只有结构）。</p>")
            sec.append("<details><summary>列结构（%d 列）</summary><div class='dc'><table><thead><tr><th>列名</th><th>类型</th></tr></thead><tbody>" % len(cols))
            for c, t in cols:
                sec.append("<tr><td><code>%s</code></td><td>%s</td></tr>" % (esc(c), esc(t)))
            sec.append("</tbody></table></div></details>")
        else:
            sec.append("<div style='overflow:auto'><table><thead><tr>")
            for c, t in cols:
                sec.append("<th title='%s'>%s</th>" % (esc(t), esc(c)))
            sec.append("</tr></thead><tbody>")
            for _, row in df.iterrows():
                sec.append("<tr>" + "".join("<td>%s</td>" % cell(v) for v in row) + "</tr>")
            sec.append("</tbody></table></div>")
        sec.append("</section>")
        parts.append("".join(sec))

    H = []
    H.append("""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>MySQL 数据浏览</title><style>
:root{--bg:#f6f7f9;--card:#fff;--ink:#1f2328;--mut:#656d76;--line:#d8dee4;--acc:#0969da}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:13.5px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.top{background:#0d1117;color:#c9d1d9;padding:26px 22px 20px}
.top h1{margin:0 0 6px;font-size:24px;color:#fff}
.top .meta{color:#8b949e;font-size:13px}
.wrap{max-width:1280px;margin:0 auto;padding:20px}
.toc{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:14px 0 26px;display:flex;flex-wrap:wrap;gap:4px 14px}
.toc a{color:var(--acc);text-decoration:none;font-size:13px;white-space:nowrap}
.toc a:hover{text-decoration:underline}
.toc .cnt{color:var(--mut);font-size:12px}
section{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin:18px 0}
h2{font-size:17px;margin:0 0 12px;display:flex;align-items:center;gap:10px}
h2 code{font-size:16px}
.badge{background:#ddf4ff;color:var(--acc);border-radius:99px;padding:2px 10px;font-size:12px;font-weight:600}
table{border-collapse:collapse;font-size:12.5px;min-width:100%}
th,td{border:1px solid var(--line);padding:5px 9px;text-align:left;vertical-align:top;word-break:break-all;white-space:nowrap}
th{background:#eef2f6;position:sticky;top:0}
tr:nth-child(even) td{background:#fafbfc}
code{background:#eef1f4;padding:1px 5px;border-radius:4px;font-size:12px}
code.bin{background:#fff1e5;color:#9a6700}
.null{color:#b0b8c1;font-style:italic}
.more{color:var(--mut)}
.empty{color:var(--mut)}
details{margin-top:10px}summary{cursor:pointer;color:var(--acc);font-weight:600}.dc{padding:8px 0}
</style></head><body>""")
    H.append("<div class='top'><h1>MySQL 数据浏览</h1>"
             "<div class='meta'>来源：本地镜像 db_mirror\\mysql\\*.parquet（抓取自阿里云 RDS，全程只读）｜"
             "31 张表共 %s 行｜生成时间 %s</div></div>"
             % ("{:,}".format(total_rows), datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
    H.append("<div class='wrap'><div class='toc'>%s</div>" % "".join(toc))
    H.append("".join(parts))
    H.append("</div></body></html>")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("".join(H))
    print("OK:", OUT, os.path.getsize(OUT), "bytes,", total_rows, "rows")


if __name__ == "__main__":
    main()
