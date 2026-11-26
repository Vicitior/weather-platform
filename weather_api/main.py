# -*- coding: utf-8 -*-
"""
气象数据 HTTP API（FastAPI）—— 基于本地镜像 parquet，离线可用。

启动：
    python -m weather_api.main            # 默认 http://127.0.0.1:8900
    python -m weather_api.main --port 9000

交互式文档（自动生成）：
    http://127.0.0.1:8900/docs            # Swagger UI，可直接试调用

所有 /api/* 接口返回 {ok, data, ...}；出错返回 {ok:false, error}。
时间均为真实历史年份（1951~2025，已自动修正源库 +100 年偏移）。
"""
import os
import re
import sys
import argparse
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, JSONResponse

from weather_api import data as D


class UTF8JSONResponse(JSONResponse):
    """显式声明 charset=utf-8，避免 PowerShell 5.1 / 部分客户端按 Latin-1 解码中文"""
    media_type = "application/json; charset=utf-8"


app = FastAPI(
    title="气象数据查询 API",
    description="基于本地镜像的 1951–2025 全国 876 站逐日气象数据查询接口。"
                "时间戳已自动修正（源库偏移 +100 年）。",
    version="0.10.0",
    default_response_class=UTF8JSONResponse,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


def ok(data, **extra):
    return UTF8JSONResponse({"ok": True, "data": data, **extra})


def err(msg, code=400):
    return UTF8JSONResponse({"ok": False, "error": msg}, status_code=code)


@app.exception_handler(Exception)
async def unhandled(request, exc):
    """兜底异常处理：把参数类错误映射为 400，避免把客户端输入问题误报为 500。

    上游 duckdb 的日期/数值解析失败（ConversionException / Invalid Input 等）本质是
    用户传了非法日期或数值，属于 400 Bad Request；真正的服务内部错误才用 500。
    """
    traceback.print_exc()
    name = type(exc).__name__
    msg = "%s: %s" % (name, exc)
    # 客户端输入错误 → 400；其余才是服务端 500
    client_errors = ("ConversionException", "InvalidInputException", "ValueError",
                     "TypeError", "ParserException", "BinderException")
    code = 400 if any(k in name for k in client_errors) else 500
    return UTF8JSONResponse({"ok": False, "error": msg}, status_code=code)


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/docs")


# ---------------- 运维 ----------------

@app.get("/api/health", tags=["运维"], summary="健康检查（供反向代理/负载均衡探活）")
def api_health():
    """轻量存活探测：只验证服务进程与数据文件可达，不做重查询，毫秒级返回。

    返回 200 即视为可用。若数据目录丢失会返回 503，便于监控告警。
    """
    import glob as _glob
    ready = os.path.isdir(D.ROOT)
    n_files = 0
    if ready:
        n_files = len(_glob.glob(D.TD_GLOB))
    payload = {"ok": ready, "version": app.version,
               "data_root": D.ROOT, "data_root_exists": ready,
               "parquet_files": n_files}
    return UTF8JSONResponse(payload, status_code=200 if ready else 503)


# ---------------- 基础 ----------------

@app.get("/api/overview", tags=["基础"], summary="数据集概览")
def api_overview():
    """站点数、总行数、时间范围。"""
    return ok(D.overview())


@app.get("/api/provinces", tags=["基础"], summary="省份列表")
def api_provinces():
    """省份列表（名称已归一化）+ 各省站点数。"""
    return ok(D.provinces().to_dict("records"))


@app.get("/api/stations", tags=["基础"], summary="站点列表")
def api_stations(
    province: str = Query(None, description="按省筛选（归一化名，如 新疆）"),
    search: str = Query(None, description="站名/站号模糊搜索，如 漠河"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    rows, total = D.list_stations(province=province, search=search, limit=limit, offset=offset)
    return ok(rows.to_dict("records"), total=total)


# 注意：/api/stations/{station_id} 是动态路由，必须注册在 /api/stations/near、
# /api/stations/bbox 之后，否则 near/bbox 会被当成 station_id 解析成 422。
# 这两个静态路由的实际定义在下方「扩展查询」区（near / bbox）。


@app.get("/api/stations/near", tags=["空间查询"], summary="按经纬度找附近站点")
def api_stations_near(
    lat: float = Query(..., ge=-90, le=90, description="纬度，如 47.34"),
    lng: float = Query(..., ge=-180, le=180, description="经度，如 123.95"),
    radius_km: float = Query(100, ge=1, le=2000, description="半径（仅用于过滤参考）"),
    limit: int = Query(20, ge=1, le=200),
):
    """找离某经纬度最近的 N 个站（返回 distance_km，按近到远）。"""
    df = D.stations_near(lat, lng, radius_km=radius_km, limit=limit)
    df = df[df["distance_km"] <= float(radius_km)]
    return ok(df.to_dict("records"))


@app.get("/api/stations/bbox", tags=["空间查询"], summary="矩形范围内的站点")
def api_stations_bbox(
    lat_min: float = Query(..., ge=-90, le=90),
    lat_max: float = Query(..., ge=-90, le=90),
    lng_min: float = Query(..., ge=-180, le=180),
    lng_max: float = Query(..., ge=-180, le=180),
    limit: int = Query(500, ge=1, le=2000),
):
    """给定经纬度矩形框，返回框内全部站点（地图打点用）。"""
    df = D.stations_in_bbox(lat_min, lat_max, lng_min, lng_max, limit=limit)
    return ok(df.to_dict("records"))


@app.get("/api/stations/{station_id}", tags=["基础"], summary="站点详情")
def api_station_detail(station_id: int):
    d = D.station_detail(station_id)
    if d is None:
        return err("station %d 不存在" % station_id, 404)
    return ok(d)


# ---------------- 日值查询 ----------------

@app.get("/api/weather/daily", tags=["日值"], summary="某站日值")
def api_daily(
    station_id: int = Query(..., description="站点 ID（MySQL station_id）"),
    date: str = Query(None, description="单日，格式 2020-07-12"),
    start: str = Query(None, description="区间开始 2020-07-01"),
    end: str = Query(None, description="区间结束 2020-07-31"),
    limit: int = Query(500, ge=1, le=5000),
):
    """某站某天或某段时间的逐日观测值。原始值 + is_valid（是否通过物理范围校验）。"""
    if not (date or start or end):
        return err("需要 date 或 start/end")
    df = D.daily(station_id, date=date, start=start, end=end, limit=limit)
    if df.empty:
        return ok([])
    df["date"] = df["date"].astype(str)
    return ok(df.to_dict("records"))


@app.get("/api/weather/day_rank", tags=["日值"], summary="某日全国排行")
def api_day_rank(
    date: str = Query(..., description="日期 2020-07-12"),
    metric: str = Query("max_temperature", description="指标，见 /api/metrics"),
    order: str = Query("desc", description="desc 从高到低 / asc 从低到高"),
    limit: int = Query(100, ge=1, le=1000),
    province: str = Query(None, description="只看某省"),
):
    """某一天全部站点的某指标排行。例：2020-07-12 全国最热的 100 个站点。"""
    try:
        df = D.day_all_stations(date, metric=metric, order=order, limit=limit, province=province)
    except ValueError as ex:
        return err(str(ex))
    df["date"] = df["date"].astype(str)
    return ok(df.to_dict("records"))


# ---------------- 极值排行 ----------------

@app.get("/api/records/station", tags=["极值"], summary="某站历史极值 TopN")
def api_station_records(
    station_id: int = Query(...),
    metric: str = Query("max_temperature"),
    order: str = Query("desc", description="desc 极值/asc 极小值"),
    limit: int = Query(10, ge=1, le=100),
):
    """漠河历史最高温 Top10（含出现日期）这类查询。"""
    try:
        df = D.station_records(station_id, metric=metric, order=order, limit=limit)
    except ValueError as ex:
        return err(str(ex))
    df["date"] = df["date"].astype(str)
    return ok(df.to_dict("records"))


@app.get("/api/records/global", tags=["极值"], summary="全数据集极值 TopN")
def api_global_records(
    metric: str = Query("max_temperature"),
    order: str = Query("desc"),
    limit: int = Query(10, ge=1, le=100),
    year: int = Query(None, ge=1951, le=2025, description="限定年份"),
    date_from: str = Query(None, description="限定开始日期"),
    date_to: str = Query(None, description="限定结束日期"),
    province: str = Query(None, description="限定省份"),
):
    """1951-2025 全国最高温 Top10 / 2020 年最热的一天 / 新疆最高温记录……"""
    try:
        df = D.global_records(metric=metric, order=order, limit=limit,
                              year=year, date_from=date_from, date_to=date_to, province=province)
    except ValueError as ex:
        return err(str(ex))
    df["date"] = df["date"].astype(str)
    return ok(df.to_dict("records"))


# ---------------- 聚合 ----------------

@app.get("/api/aggregate/annual", tags=["聚合"], summary="逐年统计")
def api_annual(
    station_id: int = Query(None, description="给=单站；不给=全站平均"),
    province: str = Query(None, description="按省汇总"),
    year_from: int = Query(None, ge=1951, le=2025),
    year_to: int = Query(None, ge=1951, le=2025),
):
    """逐年：均温/极值/降水合计/湿度/日照合计。三种口径：全国、单站、分省。"""
    df = D.annual(station_id=station_id, province=province, year_from=year_from, year_to=year_to)
    return ok(df.to_dict("records"))


@app.get("/api/aggregate/monthly", tags=["聚合"], summary="某站某年逐月")
def api_monthly(
    station_id: int = Query(...),
    year: int = Query(..., ge=1951, le=2025),
    metric: str = Query("avg_temperature"),
):
    try:
        df = D.monthly(station_id, year, metric=metric)
    except ValueError as ex:
        return err(str(ex))
    return ok(df.to_dict("records"))


@app.get("/api/trend", tags=["聚合"], summary="时间序列（画图用）")
def api_trend(
    station_id: int = Query(...),
    metric: str = Query("avg_temperature"),
    agg: str = Query("month", description="day / month / year"),
    date_from: str = Query(None),
    date_to: str = Query(None),
):
    """时间序列聚合，前端画趋势图直接用 period + value。"""
    try:
        df = D.trend(station_id, metric=metric, agg=agg, date_from=date_from, date_to=date_to)
    except ValueError as ex:
        return err(str(ex))
    df["period"] = df["period"].astype(str)
    return ok(df.to_dict("records"))


@app.get("/api/climate/monthly", tags=["聚合"], summary="气候基准（逐月常年值）")
def api_climate(
    station_id: int = Query(...),
    period_from: int = Query(1991, ge=1951, le=2025),
    period_to: int = Query(2020, ge=1951, le=2025),
):
    """某站气候平均期逐月气候值（默认 1991-2020，WMO 标准）。"""
    df = D.monthly_climate(station_id, period_from, period_to)
    return ok(df.to_dict("records"))


# ---------------- 扩展查询（v0.3） ----------------

def _parse_ids(station_ids: str):
    ids = []
    for part in re.split(r"[,\s;，、]+", (station_ids or "").strip()):
        if part and part.isdigit():
            ids.append(int(part))
    if not ids:
        raise HTTPException(400, "station_ids 需要逗号分隔的站点 ID，如 1124,1213")
    return ids


@app.get("/api/weather/daily_multi", tags=["多站对比"], summary="多站日值（一次最多 20 站）")
def api_daily_multi(
    station_ids: str = Query(..., description="逗号分隔，如 1124,1213,1354"),
    date: str = Query(None, description="单日"),
    start: str = Query(None, description="区间开始"),
    end: str = Query(None, description="区间结束"),
    metric: str = Query(None, description="只取这一个指标（可选）"),
    limit: int = Query(5000, ge=1, le=20000),
):
    """多站对比：同一时间段多个站的日值一次拉回，前端叠加折线图直接用。"""
    try:
        ids = _parse_ids(station_ids)
        df = D.daily_multi(ids, date=date, start=start, end=end, metric=metric, limit=limit)
    except ValueError as ex:
        return err(str(ex))
    df["date"] = df["date"].astype(str)
    return ok(df.to_dict("records"), stations=len(ids))


@app.get("/api/compare/periods", tags=["多站对比"], summary="两段时期逐月气候对比")
def api_compare_periods(
    station_id: int = Query(...),
    year_a: int = Query(1961, ge=1951, le=2025),
    year_b: int = Query(1990, ge=1951, le=2025),
    year_c: int = Query(1991, ge=1951, le=2025),
    year_d: int = Query(2020, ge=1951, le=2025),
    month_from: int = Query(1, ge=1, le=12),
    month_to: int = Query(12, ge=1, le=12),
):
    """两个 30 年气候期（默认 1961-1990 vs 1991-2020）逐月对比 → 直观看气候变化；
    也可传任意两年做同年对比。"""
    df = D.compare_station_years(station_id, month_from=month_from, month_to=month_to,
                                 year_a=year_a, year_b=year_b, year_c=year_c, year_d=year_d)
    return ok(df.to_dict("records"))


@app.get("/api/stats/threshold_days", tags=["统计"], summary="阈值天数统计")
def api_threshold_days(
    metric: str = Query("max_temperature"),
    op: str = Query("gte", description="gte/ge/lte/le/gt/lt/eq/ne"),
    threshold: float = Query(35.0),
    station_id: int = Query(None),
    province: str = Query(None),
    year: int = Query(None, ge=1951, le=2025),
    year_from: int = Query(None, ge=1951, le=2025),
    year_to: int = Query(None, ge=1951, le=2025),
):
    """阈值天数：某站/某省/全国每年「≥35℃ 高温日」「≤-20℃ 严寒日」「日降水 ≥50mm 暴雨日」等。
    例：station_id=1213&metric=max_temperature&op=gte&threshold=35 → 吐鲁番历年高温日数。"""
    try:
        df = D.days_count(station_id=station_id, province=province, year=year,
                          year_from=year_from, year_to=year_to,
                          metric=metric, op=op, threshold=threshold)
    except ValueError as ex:
        return err(str(ex))
    return ok(df.to_dict("records"))


@app.get("/api/agri/station", tags=["农业气象"], summary="单站农业气象指标（逐年）")
def api_agri_station(
    station_id: int = Query(...),
    base_temp: float = Query(10.0, description="活动积温起点温度"),
    year_from: int = Query(None, ge=1951, le=2025),
    year_to: int = Query(None, ge=1951, le=2025),
    month_from: int = Query(1, ge=1, le=12),
    month_to: int = Query(12, ge=1, le=12),
):
    """逐年：≥base_temp 有效积温(℃·d)、积温日数、初日/终日、无霜期天数。棉花/作物引种常用。"""
    df = D.agri_stations(station_id, base_temp=base_temp, year_from=year_from,
                         year_to=year_to, month_from=month_from, month_to=month_to)
    return ok(df.to_dict("records"))


@app.get("/api/agri/compare", tags=["农业气象"], summary="多站农业气象对比（多年平均）")
def api_agri_compare(
    station_ids: str = Query(..., description="逗号分隔，如 1124,1213,1178"),
    base_temp: float = Query(10.0),
    year_from: int = Query(1991, ge=1951, le=2025),
    year_to: int = Query(2020, ge=1951, le=2025),
):
    """多站多年平均对比：积温均值/极值、无霜期均值。选种、引种、复种指数参考。"""
    try:
        ids = _parse_ids(station_ids)
    except HTTPException:
        return err("station_ids 需要逗号分隔的站点 ID")
    df = D.agri_compare([{"station_id": i} for i in ids],
                        base_temp=base_temp, year_from=year_from, year_to=year_to)
    return ok(df.to_dict("records"))


@app.get("/api/rank/year", tags=["排行"], summary="某年站点排行")
def api_year_rank(
    year: int = Query(..., ge=1951, le=2025),
    metric: str = Query("avg_temperature"),
    order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=876),
    station_min_days: int = Query(300, ge=1, le=366,
                                  description="该年至少有这么多天数据才参与排行（防小样本）"),
):
    """某年所有站点按某指标排行：如 2024 年均温最高的站、2020 年降水最多的站。"""
    try:
        df = D.year_rank(year, metric=metric, order=order, limit=limit,
                         station_min_days=station_min_days)
    except ValueError as ex:
        return err(str(ex))
    return ok(df.to_dict("records"))


@app.get("/api/on_this_day", tags=["排行"], summary="历史上的今天（同月同日历年排行）")
def api_on_this_day(
    station_id: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    day: int = Query(..., ge=1, le=31),
    metric: str = Query("max_temperature"),
    order: str = Query("desc"),
    limit: int = Query(75, ge=1, le=100),
):
    """某站「7月15日」历年最高温排行这类查询：同一月日、跨 75 年的对比。"""
    try:
        df = D.on_this_day(station_id, month, day, metric=metric, order=order, limit=limit)
    except ValueError as ex:
        return err(str(ex))
    df["date"] = df["date"].astype(str)
    return ok(df.to_dict("records"))


@app.get("/api/debug/values_above", tags=["数据质量"], summary="超阈值记录追查")
def api_values_above(
    metric: str = Query("max_temperature"),
    threshold: float = Query(50.0),
    order: str = Query("desc"),
    limit: int = Query(100, ge=1, le=1000),
):
    """全库 ≥threshold 的记录清单（含站/日期/值）。用于核查 57.7℃、-60℃ 这类可疑极值的上下文。"""
    try:
        df = D.values_above(metric=metric, threshold=threshold, order=order, limit=limit)
    except ValueError as ex:
        return err(str(ex))
    df["date"] = df["date"].astype(str)
    return ok(df.to_dict("records"))


# ---------------- 填报表（站点 × 12 月模板 + Word/Excel 导出） ----------------

@app.get("/api/report/table", tags=["填报表"], summary="气象数据填报表（按模板口径聚合 12 月值+校验规则）")
def api_report_table(
    station_id: int = Query(..., description="站点 ID"),
    year_from: int = Query(1991, description="起年（默认 1991，WMO 气候期）"),
    year_to: int = Query(2020, description="止年（默认 2020）"),
):
    """按用户 Word 模板口径：最高/最低气温=月极端值，平均气温/湿度/风速/日照=月均值，
    降水量=多年平均月值。内置模板校验规则（<40℃、0<RH<100、u<30、n<18、Pmax 7-9 月、干旱区判定），
    flags 列出超限月份。土壤数据/作物数据两行留空待人工填写。"""
    try:
        return ok(D.report_table(station_id, year_from, year_to))
    except ValueError as e:
        return err(str(e))


@app.get("/api/report/export", tags=["填报表"], summary="填报表导出 Word/Excel 成品文件")
def api_report_export(
    station_id: int = Query(..., description="站点 ID"),
    year_from: int = Query(1991, description="起年"),
    year_to: int = Query(2020, description="止年"),
):
    """生成与模板同款的 .docx 和 .xlsx 文件（exports/ 目录），返回文件路径 + 表数据。"""
    from weather_api import export as E
    try:
        r = E.export_report(station_id, year_from, year_to)
    except ValueError as e:
        return err(str(e))
    rep = r["report"]
    return ok({
        "word_file": r["word"],
        "excel_file": r["excel"],
        "station": rep["station"],
        "period": rep["period"],
        "summary": rep["summary"],
        "flags": {row["metric"]: row["flags"] for row in rep["rows"] if row.get("flags")} or None,
    })


# ---------------- 异常值修复（邻站替补） ----------------

@app.get("/api/repair/scan", tags=["异常修复"], summary="缺测/异常扫描（哨兵值、超物理范围、max<min、缺行）")
def api_repair_scan(
    station_id: int = Query(..., description="站点 ID"),
    date: str = Query(None, description="单日 YYYY-MM-DD"),
    start: str = Query(None, description="起始日"),
    end: str = Query(None, description="结束日"),
    year: int = Query(None, description="整年（date/start/end/year 四选一）"),
    detail_limit: int = Query(500, ge=1, le=100000, description="最多返回的坏日明细行数"),
):
    """日历表 LEFT JOIN 目标站原始行：找出缺测日（无记录）、超物理范围（32766/999999 等哨兵值）、
    max<min 不自洽的天。summary 给逐指标 missing/invalid 计数。"""
    try:
        return ok(D.repair_scan(station_id, date=date, start=start, end=end,
                                year=year, detail_limit=detail_limit))
    except ValueError as e:
        return err(str(e))


@app.get("/api/repair/daily", tags=["异常修复"], summary="异常值修复：按坐标找最近邻站替补坏值/缺测")
def api_repair_daily(
    station_id: int = Query(..., description="站点 ID"),
    date: str = Query(None, description="单日 YYYY-MM-DD"),
    start: str = Query(None, description="起始日"),
    end: str = Query(None, description="结束日"),
    year: int = Query(None, description="整年"),
    method: str = Query("auto", description="auto=按指标自动选最优(默认) / own6=门控跨要素岭回归(用本站其它要素+邻站7要素，精度最高但要求该日其它要素够多) / clima=气候异常法 / blend=clima+时间距平 / krig=距平克里金(地统计，降水不适用) / idw=距离反比 / alt_idw=加权+海拔订正 / nearest=最近站原值"),
    k: int = Query(12, ge=1, le=32, description="使用最近 k 个邻站（交叉验证显示 k=24 才饱和，默认 12）"),
    radius_km: float = Query(300, gt=0, le=2000, description="邻站搜索半径 km"),
    metrics: str = Query(None, description="逗号分隔的指标名，默认全部 7 指标"),
    detail_limit: int = Query(500, ge=1, le=100000, description="最多修复的坏日行数"),
    allow_climate_fill: bool = Query(True, description="停更段是否允许用气候平均值填充（quality=climate，气候态非实况）"),
    max_gap_days: int = Query(7, ge=1, le=10000, description="连续缺测超过多少天判为停更段，不再用气候态填充"),
):
    """对扫描出的坏值/缺测日，用邻站同日有效值重建（不是简单复制最近站）。

    默认 method=auto，按 hold-out 交叉验证结果逐指标选最优法：
      气温三项/降水/日照 → clima（邻站值 + 目标站与邻站 1991-2020 月气候之差；降水用 normal-ratio 比值法）
      湿度/风速          → blend（0.5×clima + 0.5×本站前后日距平）
    空间法失效（区域性缺测，邻站当日全是哨兵值）时自动降级：时间维 → 本站该月气候基准。

    method=krig 用普通克里金（变差函数由邻站 1991-2020 长序列拟合）：第四轮 hold-out 验证显示
    它在气温/日照上显著优于 clima（气温 MAE 0.74 vs 0.78℃）、湿度/风速不显著、**降水显著更差**
    （降水是间断非高斯事件，加性插值不适用），故降水会自动退回 clima。因增益仅约 5% 而代价
    是多查一次 30 年长序列，krig 不进 auto 默认，需显式指定。

    每个修复值带 quality 标注：observed（邻站有同日实测，最可信）/ temporal（靠本站前后日）/
    climate（只能用月气候平均值，等同气候态，**不是实况**）。探测到整段停更
    （如本数据集 2020 年有 576 站只记录到 4 月底）时，默认不再用气候态填充并在 warnings 说明。

    验证依据见 异常值修复方法依据说明.md（59 站 hold-out，clima 气温 MAE 0.75℃ vs 最近站 2.91℃；
    克里金 0.74℃；纯坐标法 2.43℃ —— 决定精度的是「邻站实测+气候去趋势」而非算法的复杂度）。
    返回修复值 + 贡献站明细（谁、多远、原值、订正后、权重）；修复后 max<min 会在 warnings 提示。"""
    ms = [m.strip() for m in metrics.split(",") if m.strip()] if metrics else None
    try:
        return ok(D.repair_daily(station_id, date=date, start=start, end=end, year=year,
                                 metrics=ms, method=method, k=k, radius_km=radius_km,
                                 detail_limit=detail_limit,
                                 allow_climate_fill=allow_climate_fill,
                                 max_gap_days=max_gap_days))
    except ValueError as e:
        return err(str(e))


# ---------------- 批量补齐 / 自动巡检 ----------------

@app.get("/api/repair/batches", tags=["批量补齐"],
         summary="已录入批次列表（按上传时间倒序，可发现最新一批新数据）")
def api_repair_batches(limit: int = Query(200, ge=1, le=2000, description="返回最近多少个批次")):
    """数据里每个文件都带上传时间戳（file_path 里 14 位，如 20261106160739）。
    这里按批次聚合，给出每批覆盖几个站、多少行、数据起止日 —— 用来发现「新录入的一批」。
    最新批次在第一位。"""
    return ok({"batches": D.list_ingest_batches(limit=limit),
               "hint": "取 batches[0].batch 作为 /api/repair/new_batch 的 batch 参数即可只补最新一批"})


@app.get("/api/repair/batch", tags=["批量补齐"],
         summary="批量补齐：多站 × 一个区间，一次跑完并汇总")
def api_repair_batch(
    station_ids: str = Query(None, description="逗号分隔的站点 ID（与 province 二选一，都不给=全库）"),
    province: str = Query(None, description="省份（如 黑龙江），批量补齐该省所有站"),
    date: str = Query(None, description="单日 YYYY-MM-DD（等价于 start=end=date）"),
    start: str = Query(None, description="起始日 YYYY-MM-DD"),
    end: str = Query(None, description="结束日"),
    year: int = Query(None, description="整年"),
    metrics: str = Query(None, description="逗号分隔的指标名，默认全部 7 指标"),
    method: str = Query("auto", description="auto / own6 / clima / blend / krig / idw / alt_idw / nearest"),
    k: int = Query(12, ge=1, le=32, description="邻站个数"),
    radius_km: float = Query(300, gt=0, le=2000, description="邻站搜索半径 km"),
    allow_climate_fill: bool = Query(True, description="停更段是否允许气候态填充"),
    max_gap_days: int = Query(7, ge=1, le=10000, description="停更判定天数"),
    detail_limit: int = Query(100000, ge=1, le=1000000, description="每站最多修复明细行数"),
    limit: int = Query(None, ge=1, le=2000, description="最多处理多少个站（调试用）"),
):
    """批量补齐入口：对一批站点逐个跑 repair/daily 的逻辑并合并结果。

    `station_ids=1124,1125` 或 `province=黑龙江` 二选一，都不给则全库 876 站
    （全库 60 年数据量大，建议配 limit 或按年分批）。

    返回合并后的 summary（所有站合计）+ stations[]（逐站 summary）+ rows[]（全部修复明细，
    每行带 station_id/station_name/province，可直接导 CSV 回写）。单站失败不中断批次，
    记入 failed[]。"""
    ids = [s.strip() for s in station_ids.split(",") if s.strip()] if station_ids else None
    ms = [m.strip() for m in metrics.split(",") if m.strip()] if metrics else None
    # date 是单日简写，转成 start/end 传给底层（底层 repair_batch 只认 start/end/year）
    if date:
        start = end = date
    try:
        return ok(D.repair_batch(station_ids=ids, province=province,
                                 start=start, end=end, year=year, metrics=ms,
                                 method=method, k=k, radius_km=radius_km,
                                 allow_climate_fill=allow_climate_fill,
                                 max_gap_days=max_gap_days,
                                 detail_limit=detail_limit, limit=limit))
    except ValueError as e:
        return err(str(e))


@app.get("/api/repair/new_batch", tags=["批量补齐"],
         summary="★ 新数据自动补齐：识别最新录入批次，自动扫出坏值/空白并补齐")
def api_repair_new_batch(
    batch: str = Query(None, description="批次号（file_path 里 14 位时间戳）；留空=自动取最新批次"),
    auto_latest: bool = Query(True, description="batch 为空时是否自动取最新批次"),
    lookback_days: int = Query(0, ge=0, le=365, description="在批次日期区间上向前多补几天（补漏填的窟窿）"),
    metrics: str = Query(None, description="逗号分隔的指标名，默认全部 7 指标"),
    method: str = Query("auto", description="auto / own6 / clima / blend / krig / idw / alt_idw / nearest"),
    k: int = Query(12, ge=1, le=32, description="邻站个数"),
    radius_km: float = Query(300, gt=0, le=2000, description="邻站搜索半径 km"),
    allow_climate_fill: bool = Query(False, description="停更段是否允许气候态填充（默认 False：绝不把气候态当观测写回）"),
    max_gap_days: int = Query(7, ge=1, le=10000, description="停更判定天数"),
    detail_limit: int = Query(100000, ge=1, le=1000000, description="每站最多修复明细行数"),
    write_csv: bool = Query(True, description="是否把修复明细落成 Excel 可直接打开的 CSV"),
):
    """**这就是「今年新录入一批数据后自动补坏值/空白」的入口。**

    工作方式：
      1. 从 file_path 里的上传时间戳识别**最新一批**录入的数据（不传 batch 就自动选最新）；
      2. 取该批次覆盖的站点，以及该批次写入的数据日期区间；
      3. 对这段区间扫描 缺行 / 空值 / 超物理范围哨兵值 / max<min，再用邻站同日有效值重建；
      4. 返回汇总（多少坏日、修了多少值、各可信度档多少）+ 全部明细 + 落盘 CSV 路径。

    **默认 allow_climate_fill=False**：自动补齐场景下，如果某个值只能由「本站该月气候平均值」
    给出（整段停更、邻站与前后日全无数据），返回 null 而不是硬填 —— 气候态不是观测，
    静默写成观测会污染数据。真需要填可显式开启，并在结果里用 quality=climate 标出。

    幂等：同一批次重复调用结果一致（本实现只读数据源，不写回；回写由调用方按 rows 决定）。

    典型用法：每年新数据入库后跑一次，或做成定时任务：
      `GET /api/repair/new_batch?auto_latest=true&lookback_days=7`"""
    ms = [m.strip() for m in metrics.split(",") if m.strip()] if metrics else None
    try:
        return ok(D.repair_new_batch(batch=batch, auto_latest=auto_latest,
                                     lookback_days=lookback_days, metrics=ms,
                                     method=method, k=k, radius_km=radius_km,
                                     allow_climate_fill=allow_climate_fill,
                                     max_gap_days=max_gap_days,
                                     detail_limit=detail_limit, write_csv=write_csv))
    except ValueError as e:
        return err(str(e))


@app.get("/api/repair/overview", tags=["批量补齐"],
         summary="全库质量巡检：只扫描不修复，给出各站缺多少/坏多少的总账")
def api_repair_overview(
    year: int = Query(None, description="整年"),
    start: str = Query(None, description="起始日"),
    end: str = Query(None, description="结束日"),
    province: str = Query(None, description="只巡检某省"),
    station_ids: str = Query(None, description="只巡检指定站（逗号分隔）"),
    limit: int = Query(None, ge=1, le=2000, description="最多巡检多少个站"),
):
    """新数据入库后先跑这个看质量：按站给出 days/rows_present/invalid(超范围)/missing(缺测)，
    以及全库合计与 per_metric 分指标合计；worst_stations 是缺得最多的 50 个站。
    不做插值，速度快（约 0.1s/站年）。"""
    ids = [s.strip() for s in station_ids.split(",") if s.strip()] if station_ids else None
    try:
        return ok(D.repair_overview(year=year, start=start, end=end, province=province,
                                    station_ids=ids, limit=limit))
    except ValueError as e:
        return err(str(e))


# ---------------- 元信息 ----------------

@app.get("/api/metrics", tags=["基础"], summary="可用指标")
def api_metrics():
    return ok([{"key": k, "name": v[0], "unit": v[1]} for k, v in D.METRICS.items()])


def main():
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0（服务器上外部可访问）")
    ap.add_argument("--port", type=int, default=8900)
    a = ap.parse_args()
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")


if __name__ == "__main__":
    main()
