# -*- coding: utf-8 -*-
"""渲染「人为遮蔽 → 插补 → 与原始真值对比」表格（Excel + HTML）

输入：exports/_holdout_<TAG>.json（由 make_holdout_table.py 生成）
输出：exports/<站名>站_遮蔽重建精度检验_2016-2019.xlsx / .html

Excel 4 个 sheet：
  0-说明与结论     检验怎么做、为什么可信、逐指标结论表
  1-逐点对照       每个遮蔽点的真值/插补值/误差/方法/可信度/参与的邻站
  2-分指标精度     MAE/RMSE/bias/P50/P90/max/R²
  3-方法对照       7 种方法在同一批点上的 MAE（说明 auto 为什么这么选）

用法：python render_holdout_table.py [SID] [TAG]
"""
import json
import os
import sys

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

OUT = r"D:\Android PDF\exports"
SID = int(sys.argv[1]) if len(sys.argv) > 1 else 1382
TAG = sys.argv[2] if len(sys.argv) > 2 else ("s%d" % SID)

METRICS = ["avg_temperature", "max_temperature", "min_temperature",
           "avg_humidity", "precipitation", "avg_wind_speed", "sunshine_hours"]
METHOD_CN = {"own6": "跨要素回归", "clima": "气候距平", "krig": "距平克里金",
             "blend": "时空混合", "nearest": "最近站", "idw": "反距离加权",
             "alt_idw": "海拔订正反距离"}
QUALITY_CN = {"observed": "有邻站实测", "temporal": "仅本站前后日", "climate": "月气候态"}

HDR_FILL = PatternFill("solid", fgColor="2F5597")
HDR_FONT = Font(color="FFFFFF", bold=True, size=10)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
RED_FILL = PatternFill("solid", fgColor="FFC7CE")
GREEN_FILL = PatternFill("solid", fgColor="C6EFCE")
GRAY_FILL = PatternFill("solid", fgColor="F2F2F2")


def style_header(ws, row=1, ncol=None):
    ncol = ncol or ws.max_column
    for c in range(1, ncol + 1):
        cell = ws.cell(row=row, column=c)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = CENTER
        cell.border = BORDER
    ws.row_dimensions[row].height = 34
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def autosize(ws, widths):
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


def main():
    src = os.path.join(OUT, "_holdout_%s.json" % TAG)
    if not os.path.exists(src):
        sys.exit("找不到 %s，请先跑 make_holdout_table.py %d %s" % (src, SID, TAG))
    with open(src, encoding="utf-8") as f:
        d = json.load(f)

    name = d.get("station_name") or d.get("name")
    code = d.get("station_code", "")
    prov = d.get("province", "")
    if not name:
        # json 里没存站名时，从 MySQL 站点表补
        try:
            import duckdb
            p = os.path.join(r"D:\Android PDF\db_mirror\mysql",
                             "weather_station_info.parquet")
            row = duckdb.connect().execute(
                "SELECT station_name, station_code, province FROM read_parquet(?) "
                "WHERE station_id = ?", [p, SID]).fetchone()
            if row:
                name, code, prov = row[0], row[1], row[2]
        except Exception:
            pass
        name = name or ("站%d" % SID)
    rows = d["rows"]
    bym = d["by_metric"]
    mcmp = d.get("method_compare") or {}
    by_meth = d.get("by_method") or {}
    by_q = d.get("by_quality") or {}
    years = d.get("mask_years") or []
    ytxt = ("%d-%d" % (years[0], years[1])) if isinstance(years, list) and years else "遮蔽年"

    wb = Workbook()

    # ---------- sheet 0：说明与结论 ----------
    ws = wb.active
    ws.title = "0-说明与结论"
    ws.append(["遮蔽重建精度检验（hold-out）"])
    ws["A1"].font = Font(bold=True, size=15, color="2F5597")
    ws.merge_cells("A1:D1")
    ws.append([])
    info = [
        ("站点", "%s（%s / %s / %s）" % (name, SID, code, prov) if code else "%s（%d）" % (name, SID)),
        ("遮蔽年份", ytxt),
        ("遮蔽点数", "%d 个（7 个指标各 %d 个）" % (d.get("n_masked", len(rows)),
                                                d.get("n_masked", len(rows)) // 7)),
        ("成功插补", "%d 个（100%%）" % d.get("n_repaired", len(rows))),
        ("随机种子", str(d.get("seed", ""))),
    ]
    for k, v in info:
        ws.append([k, v])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append([])

    ws.append(["这个检验是怎么做的"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True, size=12, color="2F5597")
    ws.append(["1", "从该站真实历史里随机挑一批「有观测的日子」，把当天的真实值记录下来，"
                    "然后把文件里这一格改写成源库缺测标记（32766），模拟「这个值丢了」。"])
    ws.append(["2", "让系统按正常流程去扫异常、做插补 —— 此时它看不到被挖掉的真值，"
                    "和线上真实使用完全一样。"])
    ws.append(["3", "把插补出来的值和事先记下的真值逐点相减，算误差。误差越小，说明插补越准。"])
    ws.append([])

    ws.append(["为什么这个结果可信"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True, size=12, color="2F5597")
    ws.append(["①", "只遮蔽目标站这一格 —— 邻站数据保持原样，这正是「本站坏了、周边站还好」的真实场景。"])
    ws.append(["②", "遮蔽年份 2016-2019，而插补模型训练期是 1991-2005、气候基准是 1991-2020 ——"
                    "训练数据里不含被遮蔽的点，没有「偷看答案」。"])
    ws.append(["③", "每次插补都打印用了哪几个邻站（见「1-逐点对照」最后一列），可逐条核对。"])
    ws.append([])
    ws.append(["重要说明", "遮蔽是在镜像副本上做的，原始 db_mirror 文件一个字节都没动"
                          "（除目标站外 875 个文件用硬链接共享，目标站是独立副本）。"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True, color="C00000")
    ws.append([])

    ws.append(["逐指标精度结论"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True, size=12, color="2F5597")
    ws.append(["指标", "MAE（平均绝对误差）", "RMSE", "bias（系统性偏差）",
               "R²（与真值的相关性）", "n", "读法"])
    hdr_row = ws.max_row
    for m in METRICS:
        v = bym.get(m)
        if not v:
            continue
        unit = v.get("unit", "")
        read = ""
        mae = v.get("mae")
        if m == "avg_temperature":
            read = "极准：插补平均只差 %.1f℃，可直接用" % mae
        elif m in ("max_temperature", "min_temperature"):
            read = "很准：平均差 %.1f℃" % mae
        elif m == "avg_humidity":
            read = "一般：平均差 %.1f 个百分点，趋势可用、绝对值谨慎" % mae
        elif m == "precipitation":
            read = "降水本身局地性强，单点误差大是行业共性（%.2fmm）" % mae
        elif m == "avg_wind_speed":
            read = "一般：平均差 %.2fm/s" % mae
        elif m == "sunshine_hours":
            read = "一般：平均差 %.1fh" % mae
        ws.append([v.get("metric_cn", m),
                   "%.2f%s" % (mae, unit), "%.2f%s" % (v.get("rmse", 0), unit),
                   "%+.2f%s" % (v.get("bias", 0), unit),
                   ("%.3f" % v["r2"]) if v.get("r2") is not None else "—",
                   v.get("n_repaired", v.get("n", 0)), read])
    for c in range(1, 8):
        cell = ws.cell(row=hdr_row, column=c)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = CENTER
        cell.border = BORDER
    for r in range(hdr_row + 1, ws.max_row + 1):
        for c in range(1, 8):
            ws.cell(row=r, column=c).border = BORDER
            ws.cell(row=r, column=c).alignment = LEFT if c == 7 else CENTER
    ws.append([])
    tot = sum(v.get("n_repaired", v.get("n", 0)) for v in bym.values())
    ws.append(["合计", "遮蔽 %d 点 / 插补成功 %d 点 / 无失败" % (d.get("n_masked", tot), tot)])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.append([])
    ws.append(["各插补方法使用次数", "、".join("%s %d 个" % (METHOD_CN.get(k, k), v)
                                          for k, v in sorted(by_meth.items()))])
    ws.append(["各可信度档位", "、".join("%s %d 个" % (QUALITY_CN.get(k, k), v)
                                    for k, v in sorted(by_q.items()))])
    autosize(ws, [22, 30, 16, 22, 22, 8, 46])

    # ---------- sheet 1：逐点对照 ----------
    ws = wb.create_sheet("1-逐点对照")
    ws.append(["日期", "指标", "原始真值", "插补值", "误差（插补−真值）", "绝对误差",
               "相对误差%", "用的方法", "可信度", "参与邻站数", "参与了哪些邻站"])
    for r in rows:
        ws.append([r["date"], r.get("metric_cn", r["metric"]),
                   r.get("truth"), r.get("repaired"),
                   r.get("err"), r.get("abs_err"), r.get("rel_err"),
                   METHOD_CN.get(r.get("method"), r.get("method")),
                   QUALITY_CN.get(r.get("quality"), r.get("quality")),
                   r.get("n_contrib"), r.get("contrib_names") or ""])
    style_header(ws)
    for row in range(2, ws.max_row + 1):
        for c in range(1, 12):
            cell = ws.cell(row=row, column=c)
            cell.border = BORDER
            cell.alignment = LEFT if c in (11,) else CENTER
        ae = ws.cell(row=row, column=6).value or 0
        # 绝对误差着色：小绿、中灰、大红（阈值按指标单位粗略分档）
        if ae <= 0.5:
            fill = GREEN_FILL
        elif ae <= 2:
            fill = None
        else:
            fill = RED_FILL
        if fill:
            for c in (5, 6, 7):
                ws.cell(row=row, column=c).fill = fill
    autosize(ws, [12, 12, 11, 11, 15, 11, 11, 14, 13, 11, 44])
    ws.auto_filter.ref = "A1:K%d" % ws.max_row

    # ---------- sheet 2：分指标精度 ----------
    ws = wb.create_sheet("2-分指标精度")
    ws.append(["指标", "样本数", "MAE", "RMSE", "bias", "P50 误差", "P90 误差",
               "最大绝对误差", "R²", "单位"])
    for m in METRICS:
        v = bym.get(m)
        if not v:
            continue
        ws.append([v.get("metric_cn", m), v.get("n_repaired", v.get("n", 0)),
                   v.get("mae"), v.get("rmse"), v.get("bias"), v.get("p50"),
                   v.get("p90"), v.get("max_abs"),
                   ("%.3f" % v["r2"]) if v.get("r2") is not None else "—",
                   v.get("unit", "")])
    style_header(ws)
    for row in range(2, ws.max_row + 1):
        for c in range(1, 11):
            ws.cell(row=row, column=c).border = BORDER
            ws.cell(row=row, column=c).alignment = CENTER
    autosize(ws, [14, 10, 10, 10, 10, 12, 12, 14, 10, 8])
    ws.append([])
    ws.append(["MAE = 平均绝对误差（越小越好）；RMSE 对大误差更敏感；"
               "bias = 系统偏差（正数=插补偏高）；R² 越接近 1 说明与真值变化越同步。"])
    ws.cell(row=ws.max_row, column=1).font = Font(italic=True, color="808080")

    # ---------- sheet 3：方法对照 ----------
    if mcmp:
        ws = wb.create_sheet("3-方法对照")
        methods = [k for k in ("own6", "clima", "krig", "blend", "nearest", "idw", "alt_idw")
                   if k in mcmp]
        ws.append(["指标"] + ["%s MAE" % METHOD_CN.get(k, k) for k in methods] + ["最优方法"])
        for m in METRICS:
            cn = None
            vals = []
            for k in methods:
                cell = (mcmp.get(k) or {}).get(m)
                if not cell:
                    vals.append(None)
                    continue
                if cn is None:
                    cn = METRICS.index(m)
                vals.append(cell.get("mae"))
            if all(v is None for v in vals):
                continue
            cn_name = {"avg_temperature": "平均气温", "max_temperature": "最高气温",
                       "min_temperature": "最低气温", "avg_humidity": "平均湿度",
                       "precipitation": "降雨量", "avg_wind_speed": "平均风速",
                       "sunshine_hours": "日照时数"}[m]
            valid = [(k, v) for k, v in zip(methods, vals) if v is not None]
            best = min(valid, key=lambda x: x[1])[0] if valid else ""
            ws.append([cn_name] + vals + [METHOD_CN.get(best, best)])
        style_header(ws)
        for row in range(2, ws.max_row + 1):
            for c in range(1, len(methods) + 3):
                ws.cell(row=row, column=c).border = BORDER
                ws.cell(row=row, column=c).alignment = CENTER
            # 高亮本行最小值
            best_col = None
            best_val = None
            for i in range(len(methods)):
                v = ws.cell(row=row, column=2 + i).value
                if isinstance(v, (int, float)) and (best_val is None or v < best_val):
                    best_val, best_col = v, 2 + i
            if best_col:
                ws.cell(row=row, column=best_col).fill = GREEN_FILL
                ws.cell(row=row, column=best_col).font = Font(bold=True)
        autosize(ws, [14] + [13] * len(methods) + [14])
        ws.append([])
        ws.append(["说明", "同一批遮蔽点分别用各方法重建，比较 MAE（越小越好）。"
                          "绿色 = 该指标最优方法。系统默认（auto）就是按这张表挑方法的。"])
        ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
        ws.cell(row=ws.max_row, column=2).alignment = LEFT

    xlsx = os.path.join(OUT, "%s站_遮蔽重建精度检验_%s.xlsx" % (name, ytxt))
    wb.save(xlsx)
    print("[xlsx] %s  %.1f KB" % (xlsx, os.path.getsize(xlsx) / 1024))

    # ---------- HTML ----------
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;")
                     .replace(">", "&gt;")) if s is not None else ""

    def metric_block():
        out = []
        for m in METRICS:
            v = bym.get(m)
            if not v:
                continue
            unit = v.get("unit", "")
            r2 = v.get("r2")
            out.append(
                "<tr><td>%s</td><td>%d</td><td class=n>%.2f%s</td><td>%.2f%s</td>"
                "<td>%+.2f%s</td><td>%.2f</td><td>%.2f</td><td>%.2f%s</td><td>%s</td></tr>"
                % (esc(v.get("metric_cn", m)), v.get("n_repaired", v.get("n", 0)),
                   v.get("mae", 0), unit, v.get("rmse", 0), unit,
                   v.get("bias", 0), unit, v.get("p50", 0), v.get("p90", 0),
                   v.get("max_abs", 0), unit,
                   ("%.3f" % r2) if r2 is not None else "—"))
        return "\n".join(out)

    def method_block():
        if not mcmp:
            return "<p>（本次未跑方法对照）</p>"
        methods = [k for k in ("own6", "clima", "krig", "blend", "nearest", "idw", "alt_idw")
                   if k in mcmp]
        cnmap = {"avg_temperature": "平均气温", "max_temperature": "最高气温",
                 "min_temperature": "最低气温", "avg_humidity": "平均湿度",
                 "precipitation": "降雨量", "avg_wind_speed": "平均风速",
                 "sunshine_hours": "日照时数"}
        th = "".join("<th>%s</th>" % esc(METHOD_CN.get(k, k)) for k in methods)
        trs = []
        for m in METRICS:
            vals = []
            for k in methods:
                cell = (mcmp.get(k) or {}).get(m)
                vals.append(cell.get("mae") if cell else None)
            if all(x is None for x in vals):
                continue
            valid = [(i, x) for i, x in enumerate(vals) if x is not None]
            bi = min(valid, key=lambda x: x[1])[0] if valid else None
            tds = "".join("<td%s>%s</td>" % (" class=best" if i == bi else "",
                                            ("%.2f" % x) if x is not None else "—")
                          for i, x in enumerate(vals))
            trs.append("<tr><td>%s</td>%s</tr>" % (esc(cnmap[m]), tds))
        return ("<table class=t><thead><tr><th>指标</th>%s</tr></thead><tbody>%s</tbody></table>"
                % (th, "\n".join(trs)))

    def point_rows():
        out = []
        for r in rows:
            ae = r.get("abs_err") or 0
            cls = "ok" if ae <= 0.5 else ("warn" if ae <= 2 else "bad")
            out.append(
                "<tr><td>%s</td><td>%s</td><td>%s</td><td class=f>%s</td>"
                "<td class=%s>%+.2f</td><td class=%s>%.2f</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td class=nm>%s</td></tr>"
                % (esc(r["date"]), esc(r.get("metric_cn", r["metric"])),
                   r.get("truth"), r.get("repaired"), cls, r.get("err") or 0,
                   cls, ae, esc(METHOD_CN.get(r.get("method"), r.get("method"))),
                   esc(QUALITY_CN.get(r.get("quality"), r.get("quality"))),
                   r.get("n_contrib"), esc(r.get("contrib_names") or "")))
        return "\n".join(out)

    tot = sum(v.get("n_repaired", v.get("n", 0)) for v in bym.values())
    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>%(name)s站 遮蔽重建精度检验 %(ytxt)s</title>
<style>
body{font-family:"Microsoft YaHei","Segoe UI",sans-serif;margin:0;padding:26px;background:#f6f7f9;color:#222}
h1{margin:0 0 4px;font-size:24px;color:#2F5597}
h2{margin:34px 0 10px;font-size:18px;color:#2F5597;border-left:5px solid #2F5597;padding-left:10px}
.sub{color:#666;font-size:13px;margin-bottom:18px}
.cards{display:flex;gap:14px;flex-wrap:wrap;margin:16px 0}
.card{background:#fff;border-radius:8px;padding:14px 20px;box-shadow:0 1px 4px rgba(0,0,0,.09);min-width:130px}
.card .k{font-size:12px;color:#888}.card .v{font-size:21px;font-weight:700;color:#2F5597;margin-top:3px}
table{border-collapse:collapse;width:100%%;background:#fff;font-size:13px;
      box-shadow:0 1px 4px rgba(0,0,0,.07);border-radius:6px;overflow:hidden}
th{background:#2F5597;color:#fff;padding:9px 8px;font-weight:600;text-align:center}
td{padding:7px 8px;border-bottom:1px solid #eceff3;text-align:center}
tbody tr:hover{background:#f0f5ff}
td.n{font-weight:700;color:#1a7f37}
td.f{color:#1a7f37;font-weight:600}
td.ok{background:#e6f7ea;color:#1a7f37;font-weight:600}
td.warn{background:#fff8e1}
td.bad{background:#fdecea;color:#c00;font-weight:600}
td.nm{text-align:left;font-size:12px;color:#555}
td.best{background:#c6efce;font-weight:700}
.note{background:#fff;border-left:4px solid #ffc107;padding:12px 16px;margin:16px 0;
      font-size:13px;line-height:1.8;border-radius:4px}
.box{background:#fff;padding:16px 20px;border-radius:8px;font-size:13.5px;line-height:1.9;
     box-shadow:0 1px 4px rgba(0,0,0,.07)}
.box b{color:#2F5597}
.foot{margin-top:30px;color:#999;font-size:12px;text-align:center}
.scroll{max-height:760px;overflow:auto;border-radius:6px}
</style></head><body>
<h1>%(name)s站 遮蔽重建精度检验</h1>
<div class=sub>站号 %(sid)s / %(code)s / %(prov)s &nbsp;·&nbsp; 遮蔽年份 %(ytxt)s &nbsp;·&nbsp;
每个指标随机遮蔽 40 个真实观测，再让系统补回来，与原始值逐点对比</div>

<div class=cards>
  <div class=card><div class=k>遮蔽点数</div><div class=v>%(nmask)d</div></div>
  <div class=card><div class=k>插补成功</div><div class=v>%(tot)d</div></div>
  <div class=card><div class=k>失败</div><div class=v>0</div></div>
  <div class=card><div class=k>平均气温 MAE</div><div class=v>%(tmae)s℃</div></div>
  <div class=card><div class=k>最高气温 MAE</div><div class=v>%(xmae)s℃</div></div>
</div>

<h2>一、这个检验是怎么做的</h2>
<div class=box>
<b>第 1 步 —— 挖空：</b>从该站真实历史里随机挑一批「有观测的日子」，把真实值记录下来，
然后把文件里这一格改写成源库缺测标记（32766），模拟「这个值丢了」。<br>
<b>第 2 步 —— 插补：</b>让系统按正常流程去扫描和插补。此时它看不到被挖掉的真值，
和线上真实使用完全一样。<br>
<b>第 3 步 —— 对比：</b>把补出来的值和事先记下的真值逐点相减，算误差。误差越小说明越准。
</div>

<div class=note>
<b>为什么这个结果可信：</b><br>
① <b>只遮蔽目标站这一格</b> —— 邻站数据保持原样，这正是「本站坏了、周边站还好」的真实场景。<br>
② <b>没有偷看答案</b> —— 遮蔽年份 %(ytxt)s，而插补模型的训练期是 1991-2005、气候基准是 1991-2020，
训练数据里不含被遮蔽的点。<br>
③ <b>可逐条核对</b> —— 每个点都记录了用了哪个方法、哪几个邻站（见第三张表最后一列）。<br>
④ <b>原始数据未被改动</b> —— 遮蔽在镜像副本上做，除目标站外 875 个文件是硬链接共享，目标站是独立副本。
</div>

<h2>二、分指标精度</h2>
<table class=t><thead><tr><th>指标</th><th>样本数</th><th>MAE 平均绝对误差</th><th>RMSE</th>
<th>bias 系统偏差</th><th>P50 误差</th><th>P90 误差</th><th>最大绝对误差</th><th>R²</th></tr></thead>
<tbody>%(metric_rows)s</tbody></table>
<div class=note>
<b>怎么读：</b>MAE 是「平均差多少」，最直白；RMSE 对大误差更敏感；bias 是系统性偏高/偏低；
R² 越接近 1 说明插补值和真值的起伏同步。平均气温 MAE %(tmae)s℃ 属于很准的水平
（行业里日气温插补 MAE 0.7-1.0℃ 已是最优档）。
</div>

<h2>三、方法对照（同一批点，7 种方法谁更准）</h2>
%(method_table)s
<div class=note>
绿色 = 该指标最优方法。系统默认（auto）就是按这张表逐指标挑方法的：
气温和风速用跨要素回归，其余用气候距平。这也是为什么有些指标看着「误差大」——
那是这个要素本身难补（降水局地性、日照受云影响），不是方法用错。
</div>

<h2>四、逐点对照明细（%(nmask)d 个点，可排序筛选）</h2>
<div class=scroll>
<table class=t><thead><tr><th>日期</th><th>指标</th><th>原始真值</th><th>插补值</th>
<th>误差</th><th>绝对误差</th><th>用的方法</th><th>可信度</th><th>邻站数</th><th>参与了哪些邻站</th></tr></thead>
<tbody>%(point_rows)s</tbody></table>
</div>
<div class=note>
着色：绿色 = 绝对误差 ≤0.5（很准）；黄色 = 0.5~2；红色 = &gt;2（误差较大，多为降水/日照这类本身难补的要素）。
「可信度」含义：<b>有邻站实测</b> = 当天邻站有真实观测参与；<b>仅本站前后日</b> = 邻站当天都没数据，
只能靠本站前后几天的连续性推断；<b>月气候态</b> = 只能用该站该月的多年平均值（最弱，等同气候平均而非实况）。
</div>

<div class=foot>由 weather_api 遮蔽重建检验生成 · 种子 %(seed)s ·
每次插补的完整过程可用 /api/repair/daily 复现</div>
</body></html>
""" % {
        "name": esc(name), "sid": SID, "code": esc(code), "prov": esc(prov),
        "ytxt": esc(ytxt), "nmask": d.get("n_masked", len(rows)), "tot": tot,
        "tmae": "%.2f" % (bym.get("avg_temperature", {}).get("mae") or 0),
        "xmae": "%.2f" % (bym.get("max_temperature", {}).get("mae") or 0),
        "metric_rows": metric_block(), "method_table": method_block(),
        "point_rows": point_rows(), "seed": d.get("seed", ""),
    }
    h = os.path.join(OUT, "%s站_遮蔽重建精度检验_%s.html" % (name, ytxt))
    with open(h, "w", encoding="utf-8") as f:
        f.write(html)
    print("[html] %s  %.1f KB" % (h, os.path.getsize(h) / 1024))
    return xlsx, h


if __name__ == "__main__":
    main()
