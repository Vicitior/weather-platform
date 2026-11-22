# -*- coding: utf-8 -*-
"""
填报表导出 —— 把 data.report_table() 的结果导出为 Word / Excel 成品表。

Word 版式对齐用户给的模板：指标大类竖向合并单元格、指标列 + 1~12 月列、
降水量行 0<P 校验注记等。土壤数据 / 作物数据两行留空待人工填写。
"""
import os

from docx import Document
from docx.shared import Pt, Cm
from docx.enum.table import WD_ALIGN_VERTICAL, WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

from weather_api import data as D

# 12 个月表头
MONTH_HEADERS = ["1 月", "2 月", "3 月", "4 月", "5 月", "6 月",
                 "7 月", "8 月", "9 月", "10 月", "11 月", "12 月"]


def _fmt(v):
    return "" if v is None else ("%g" % v)


def export_word(rep, path):
    """rep = D.report_table(...) 的返回 dict；生成 Word 版填报表。"""
    st = rep["station"]
    doc = Document()

    style = doc.styles["Normal"]
    style.font.name = "宋体"
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    style.font.size = Pt(10.5)

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = title.add_run(
        "气象数据填报表 — %s（%s，%s）%s"
        % (st["station_name"], st["province"], st["station_code"], rep["period"])
    )
    run.bold = True
    run.font.size = Pt(14)

    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    meta.add_run(
        "站号 %s  经度 %.2f  纬度 %.2f  海拔 %.1f m  统计 %d 年（共 %d 天数据）"
        % (st["station_code"], st["longitude"], st["latitude"],
           st["altitude"] or 0.0, rep["summary"]["n_years"], rep["summary"]["days_total"])
    ).font.size = Pt(9)

    n_rows = 1 + len(rep["rows"]) + len(rep["manual_rows"])
    table = doc.add_table(rows=n_rows, cols=14)
    table.style = "Table Grid"

    hdr = table.rows[0].cells
    for j, text in enumerate(["指标大类", "指标"] + MONTH_HEADERS):
        hdr[j].text = text
        hdr[j].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

    for i, row in enumerate(rep["rows"], start=1):
        cells = table.rows[i].cells
        cells[1].text = row["metric"]
        for j, v in enumerate(row["months"]):
            cells[2 + j].text = _fmt(v)
        for c in cells:
            c.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
            c.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
        if row.get("flags"):
            # 校验规则触发：在指标格标注
            cells[1].text = row["metric"] + "（超限: " + "; ".join(row["flags"]) + "）"

    for k, mrow in enumerate(rep["manual_rows"], start=1):
        cells = table.rows[len(rep["rows"]) + k].cells
        cells[0].text = mrow["category"]
        for c in cells[1:]:
            c.text = ""

    # 指标大类竖向合并（气象数据 7 行 + 手填两行各 1 行）
    cat_cell = table.cell(1, 0)
    for i in range(2, 1 + len(rep["rows"])):
        cat_cell = cat_cell.merge(table.cell(i, 0))
    cat_cell.text = "气象数据"
    cat_cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
    for k in range(len(rep["manual_rows"])):
        pass  # 土壤/作物各占一行，无需合并

    # 列宽：大类 1.6cm、指标 2.6cm、月份列均分
    table.columns[0].width = Cm(1.6)
    table.columns[1].width = Cm(2.6)
    for j in range(2, 14):
        table.columns[j].width = Cm(1.35)
    for row in table.rows:
        row.cells[0].width = Cm(1.6)
        row.cells[1].width = Cm(2.6)

    # 表下注记：模板校验规则 + 降水汇总
    s = rep["summary"]
    note_lines = [
        "校验规则：最高气温 <40℃；0<RHmean<100；0<u<30、0<n<18（≥上限时需海拔和站点二测检测）；"
        "0<P，Pmax 在 7-9 月，最大值站点不在干旱区。",
        "降水汇总：Pmax = %s 月（%.1f mm/月，%s 7-9 月）；年均降水 %.1f mm（%s 干旱区 <200mm）。"
        % (s["pmax_month"], s["pmax_mm"] or 0, "在" if s["pmax_in_jul_sep"] else "不在",
           s["annual_prcp_mm"] or 0, "属" if s["arid_zone_lt200mm"] else "不属"),
        "土壤数据 / 作物数据行留空待人工填写。",
    ]
    if rep["rows"][-1].get("note"):
        note_lines.insert(1, "口径：" + rep["rows"][-1]["note"] + "；气温/湿度/风速/日照为多年平均，最高/最低气温为区间月极端值。")
    for line in note_lines:
        p = doc.add_paragraph(line)
        p.runs[0].font.size = Pt(9)

    doc.save(path)
    return path


def export_excel(rep, path):
    """rep = D.report_table(...) 的返回 dict；生成 Excel 版填报表。"""
    import openpyxl
    from openpyxl.styles import Alignment, Font, Border, Side

    st = rep["station"]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "填报表"

    thin = Side(style="thin", color="000000")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)

    ws.cell(row=1, column=1, value="气象数据填报表 — %s（%s，%s）%s"
            % (st["station_name"], st["province"], st["station_code"], rep["period"])).font = Font(bold=True, size=14)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=14)
    ws.cell(row=1, column=1).alignment = center

    headers = ["指标大类", "指标"] + MONTH_HEADERS
    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=2, column=j, value=h)
        c.font = Font(bold=True)
        c.alignment = center
        c.border = border

    r = 3
    for row in rep["rows"]:
        label = row["metric"] + ("（超限: %s）" % "; ".join(row["flags"]) if row.get("flags") else "")
        ws.cell(row=r, column=2, value=label).border = border
        ws.cell(row=r, column=2).alignment = center
        for j, v in enumerate(row["months"], start=3):
            c = ws.cell(row=r, column=j, value=v)
            c.border = border
            c.alignment = center
            if v is not None:
                c.number_format = "0.0" if row["unit"] != "m/s" else "0.00"
        r += 1

    first_weather, last_weather = 3, r - 1
    for mrow in rep["manual_rows"]:
        ws.cell(row=r, column=1, value=mrow["category"]).border = border
        ws.cell(row=r, column=1).alignment = center
        for j in range(1, 15):
            ws.cell(row=r, column=j).border = border
        r += 1

    ws.merge_cells(start_row=first_weather, start_column=1, end_row=last_weather, end_column=1)
    ws.cell(row=first_weather, column=1, value="气象数据").alignment = center

    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 26
    for col in "CDEFGHIJKLMNO":
        ws.column_dimensions[col].width = 9

    s = rep["summary"]
    notes = [
        "校验规则：最高气温 <40℃；0<RHmean<100；0<u<30、0<n<18（≥上限时需海拔和站点二测检测）；0<P，Pmax 在 7-9 月，最大值站点不在干旱区。",
        "降水汇总：Pmax = %s 月（%.1f mm/月，%s 7-9 月）；年均降水 %.1f mm（%s 干旱区 <200mm）。"
        % (s["pmax_month"], s["pmax_mm"] or 0, "在" if s["pmax_in_jul_sep"] else "不在",
           s["annual_prcp_mm"] or 0, "属" if s["arid_zone_lt200mm"] else "不属"),
        "土壤数据 / 作物数据行留空待人工填写。",
    ]
    for line in notes:
        ws.cell(row=r, column=1, value=line).font = Font(size=9)
        ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=14)
        r += 1

    wb.save(path)
    return path


def export_report(station_id, year_from=1991, year_to=2020, out_dir=None):
    """一站式：算表 + 导出 Word/Excel，返回 {word, excel, report}。"""
    rep = D.report_table(station_id, year_from, year_to)
    out_dir = out_dir or os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "exports")
    os.makedirs(out_dir, exist_ok=True)
    st = rep["station"]
    base = "%s_%s_%s" % (st["station_code"], rep["period"].replace("-", "_"), st["station_name"])
    word = export_word(rep, os.path.join(out_dir, base + ".docx"))
    excel = export_excel(rep, os.path.join(out_dir, base + ".xlsx"))
    return {"word": word, "excel": excel, "report": rep}
