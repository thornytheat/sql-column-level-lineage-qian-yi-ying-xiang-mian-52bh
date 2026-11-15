"""FastAPI 入口：路由保持薄，解析 / 血缘 / 图算法 / 迁移清单都在纯函数模块里。"""
from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import graph, migrate, pipeline, report, seed, settings, store, switching, versions

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


def get_conn() -> sqlite3.Connection:
    return store.connect()


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = get_conn()
    try:
        store.init_schema(conn)
        try:
            app.state.seed_info = seed.ensure_seed(conn)
            app.state.version_seed_info = seed.ensure_version_seed(conn)
        except FileNotFoundError:
            app.state.seed_info = {"seeded": False, "reason": "未找到 sample_data，跳过 seed"}
    finally:
        conn.close()
    yield


app = FastAPI(title=settings.APP_NAME, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


# --------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------

def base_context(request: Request, active: str) -> dict:
    conn = get_conn()
    try:
        stats = store.stats(conn)
    finally:
        conn.close()
    return {
        "request": request,
        "app_name": settings.APP_NAME,
        "app_short": settings.APP_SHORT,
        "app_desc": settings.APP_DESC,
        "active": active,
        "page": active,
        "stats": stats,
    }


def _run_graph(conn, run_id: str):
    data = store.load_run(conn, run_id)
    if data is None:
        return None, None
    levels = graph.topo_levels(graph.table_adjacency(data["table_edges"]))
    return data, levels


def _catalog(conn) -> dict[str, list[str]]:
    schemas = store.load_schemas(conn)
    runs = store.list_runs(conn, limit=1)
    if runs:
        run_id = runs[0]["run_id"]
        data = store.load_run(conn, run_id)
        if data:
            for e in data["table_edges"]:
                schemas.setdefault(e["target_table"], [])
            for s in data["scripts"]:
                if s["status"] == "ok":
                    schemas[s["target"]] = s["columns"]
    return schemas


def _errors_payload(analysis: dict) -> list[dict]:
    errors = []
    for s in analysis["scripts"]:
        if s["status"] != "error":
            continue
        errors.append(
            {
                "filename": s["filename"],
                "line": s.get("error_line"),
                "col": s.get("error_col"),
                "message": s.get("message") or "解析失败",
            }
        )
    for d in analysis["diagnostics"]:
        errors.append(
            {
                "filename": d["filename"],
                "line": d["line"],
                "col": d["col"],
                "message": d["message"],
            }
        )
    return errors


def _execute(conn, mode: str, sql: str, filename: str) -> tuple[str | None, dict, str | None]:
    """返回 (run_id, analysis, error_message)。解析 0 条成功 → 不落库并带诊断返回。"""
    if mode in {"sample", "sample_broken"}:
        files = pipeline.load_sample_files(settings.SAMPLE_DIR, include_broken=(mode == "sample_broken"))
        schemas = pipeline.load_schemas(settings.SAMPLE_DIR)
        label = pipeline.SAMPLE_WITH_BROKEN_LABEL if mode == "sample_broken" else pipeline.SAMPLE_LABEL
    else:
        if not sql or not sql.strip():
            return None, {}, "SQL 不能为空：请粘贴 CREATE TABLE/VIEW ... AS SELECT 或 INSERT INTO ... SELECT"
        files = [{"filename": (filename or "自定义脚本.sql").strip() or "自定义脚本.sql", "sql": sql}]
        schemas = pipeline.load_schemas(settings.SAMPLE_DIR)
        if not schemas:
            schemas = store.load_schemas(conn)
        label = f"自定义录入 · {files[0]['filename']}"
    analysis = pipeline.analyze(files, schemas)
    if analysis["parsed_count"] == 0:
        first = next((s for s in analysis["scripts"] if s["status"] == "error"), None)
        msg = first["message"] if first else "全部脚本解析失败"
        return None, analysis, f"全部 {len(files)} 个脚本都未能解析：{msg}"
    run_id = pipeline.persist(conn, analysis, label)
    return run_id, analysis, None


def _workbench_context(conn, ctx: dict) -> dict:
    """工作台右栏只读装配：最近一次解析的诊断 / 血缘 / 环，不重新计算任何算法。"""
    latest = ctx["stats"]["latest_run"]
    failures: list[dict] = []
    column_groups: list[dict] = []
    cycles: list[dict] = []
    if latest:
        run_data, _ = _run_graph(conn, latest["run_id"])
        if run_data:
            failures = report.failed_scripts(run_data["scripts"])
            column_groups = report.grouped_columns(run_data["column_edges"])
            cycles = [c for c in run_data["cycles"] if c["level"] == "table"]
    ctx.update(
        {
            "scripts": store.list_scripts(conn),
            "runs": store.list_runs(conn, limit=8),
            "latest": latest,
            "failures": failures,
            "column_groups": column_groups,
            "cycles": cycles,
        }
    )
    return ctx


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    ctx = base_context(request, "index")
    conn = get_conn()
    try:
        scripts = store.list_scripts(conn)
        latest = ctx["stats"]["latest_run"]
        run_data = None
        cycles = []
        if latest:
            run_data, _ = _run_graph(conn, latest["run_id"])
            cycles = run_data["cycles"] if run_data else []
        ctx.update(
            {
                "scripts": scripts,
                "schemas": store.load_schemas(conn),
                "latest": latest,
                "runs": store.list_runs(conn, limit=6),
                "run_data": run_data,
                "cycles": cycles,
            }
        )
    finally:
        conn.close()
    return templates.TemplateResponse(request, "index.html", ctx)


@app.get("/workbench", response_class=HTMLResponse)
def workbench(request: Request):
    conn = get_conn()
    try:
        ctx = _workbench_context(conn, base_context(request, "workbench"))
    finally:
        conn.close()
    return templates.TemplateResponse(request, "workbench.html", ctx)


@app.post("/runs/submit")
async def submit_run_form(request: Request):
    form = await request.form()
    mode = str(form.get("mode") or "sample")
    sql = str(form.get("sql") or "")
    filename = str(form.get("filename") or "自定义脚本.sql")
    conn = get_conn()
    try:
        run_id, analysis, err = _execute(conn, mode, sql, filename)
        if err or run_id is None:
            ctx = _workbench_context(conn, base_context(request, "workbench"))
            ctx.update(
                {
                    "error": err,
                    "diagnostics": _errors_payload(analysis),
                    "sql": sql,
                    "filename": filename,
                }
            )
            return templates.TemplateResponse(request, "workbench.html", ctx, status_code=422)
    finally:
        conn.close()
    return RedirectResponse(url=f"/result/{run_id}", status_code=303)


@app.get("/result/{run_id}", response_class=HTMLResponse)
def result_page(request: Request, run_id: str):
    conn = get_conn()
    try:
        data, levels = _run_graph(conn, run_id)
        if data is None:
            ctx = base_context(request, "result")
            ctx["error"] = f"找不到运行记录 {run_id}，请回工作台重新解析。"
            return templates.TemplateResponse(request, "result.html", ctx, status_code=404)
        run = data["run"]
        groups = report.grouped_columns(data["column_edges"])
        failures = report.failed_scripts(data["scripts"])
        table_groups: dict[str, list[dict]] = {}
        for e in data["table_edges"]:
            table_groups.setdefault(e["target_table"], []).append(e)
        seed_rows = [
            {"table": e["source_table"], "column": e["source_column"], "script": e["script"], "line": e["line"]}
            for e in data["column_edges"]
        ][:40]
        ctx = base_context(request, "result")
        ctx.update(
            {
                "run": run,
                "run_id": run_id,
                "scripts": data["scripts"],
                "column_groups": groups,
                "table_groups": dict(sorted(table_groups.items())),
                "cycles": [c for c in data["cycles"] if c["level"] == "table"],
                "column_cycles": [c for c in data["cycles"] if c["level"] == "column"],
                "failures": failures,
                "levels": levels,
                "seed_rows": seed_rows,
                "star_edge_count": sum(1 for e in data["column_edges"] if e["star_expanded"]),
                "transform_edge_count": sum(1 for e in data["column_edges"] if e["transforms"]),
                "union_edge_count": sum(1 for e in data["column_edges"] if e["union_branch"]),
            }
        )
        return templates.TemplateResponse(request, "result.html", ctx)
    finally:
        conn.close()


@app.get("/review", response_class=HTMLResponse)
def review_page(
    request: Request,
    run_id: str | None = None,
    table: str | None = None,
    column: str | None = None,
    op: str = "drop",
    new_name: str | None = None,
):
    conn = get_conn()
    try:
        runs = store.list_runs(conn, limit=12)
        if not runs:
            ctx = base_context(request, "review")
            ctx["error"] = "还没有任何解析记录，请先到工作台执行样例解析。"
            return templates.TemplateResponse(request, "review.html", ctx)
        target_run = run_id or runs[0]["run_id"]
        data, levels = _run_graph(conn, target_run)
        if data is None:
            ctx = base_context(request, "review")
            ctx["error"] = f"找不到运行记录 {run_id}，已回退到最近一次解析。"
            target_run = runs[0]["run_id"]
            data, levels = _run_graph(conn, target_run)
        catalog = _catalog(conn)
        table_options = sorted({e["target_table"] for e in data["table_edges"]} | set(store.load_schemas(conn)))
        table_choice = table or "ods_orders"
        columns = catalog.get(table_choice, [])
        column_choice = column if column is not None and column != "" else (columns[2] if len(columns) > 2 else (columns[0] if columns else ""))
        impact = None
        migration = None
        page_error = None
        if table_choice:
            impact = graph.impact(
                data["table_edges"], data["column_edges"], levels, table_choice, column_choice or None
            )
            if not impact["found"]:
                page_error = impact["message"]
            migration = migrate.plan_migration(impact, op, new_name, data["cycles"])
        ctx = base_context(request, "review")
        ctx.update(
            {
                "runs": runs,
                "run_id": target_run,
                "run": data["run"],
                "table_options": table_options,
                "table": table_choice,
                "columns": columns,
                "column": column_choice,
                "op": op,
                "new_name": new_name or "",
                "impact": impact,
                "migration": migration,
                "cycles": data["cycles"],
                "error": page_error,
            }
        )
        return templates.TemplateResponse(request, "review.html", ctx)
    finally:
        conn.close()


# --------------------------------------------------------------------------
# 口径版本与切换（页面 + 表单）
# --------------------------------------------------------------------------

def _versions_context(request: Request, conn, table: str, column: str, period: str | None) -> dict:
    ctx = base_context(request, "versions")
    version_list = switching.list_versions(conn, table, column)
    sw = switching.latest_switch(conn, table, column)
    query = None
    replay_rows: list[dict] = []
    page_error = None
    if period:
        try:
            query = switching.query_column(conn, table, column, period)
        except versions.VersionError as exc:
            page_error = str(exc)
        replay_rows = switching.period_results(conn, table, column, period)
    ctx.update(
        {
            "table": table,
            "column": column,
            "period": period or "",
            "version_list": version_list,
            "pending_versions": [v for v in version_list if v["status"] == "pending"],
            "switch": sw,
            "query": query,
            "replay_rows": replay_rows,
            "error": page_error,
        }
    )
    return ctx


@app.get("/versions", response_class=HTMLResponse)
def versions_page(
    request: Request,
    table: str = "dwd_order",
    column: str = "amount",
    period: str | None = None,
    notice: str | None = None,
):
    conn = get_conn()
    try:
        ctx = _versions_context(request, conn, table, column, period)
        if notice:
            ctx["notice"] = notice
    finally:
        conn.close()
    return templates.TemplateResponse(request, "versions.html", ctx)


def _versions_redirect(table: str, column: str, period: str | None = None, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    params = {"table": table, "column": column}
    if period:
        params["period"] = period
    if notice:
        params["notice"] = notice
    if error:
        params["error"] = error
    return RedirectResponse(url=f"/versions?{urlencode(params)}", status_code=303)


@app.post("/versions/switch/create")
async def create_switch_form(request: Request):
    form = await request.form()
    table = str(form.get("table") or "dwd_order")
    column = str(form.get("column") or "amount")
    period = str(form.get("period") or "") or None
    conn = get_conn()
    try:
        try:
            sw = switching.start_switch(
                conn, table, column, int(form.get("to_version_id")), str(form.get("cutover_period") or "")
            )
        except versions.VersionError as exc:
            return _versions_redirect(table, column, period, error=str(exc))
    finally:
        conn.close()
    created = "已创建" if sw.get("created") else "已存在（重复创建幂等，返回同一切换）"
    return _versions_redirect(table, column, period, notice=f"切换计划{created}：{sw['run']['switch_id']}，共 {len(sw['steps'])} 步")


@app.post("/versions/switch/{switch_id}/execute")
async def execute_switch_form(request: Request, switch_id: str):
    form = await request.form()
    table = str(form.get("table") or "dwd_order")
    column = str(form.get("column") or "amount")
    period = str(form.get("period") or "") or None
    stop_raw = str(form.get("stop_after") or "").strip()
    stop_after = int(stop_raw) if stop_raw.isdigit() else None
    conn = get_conn()
    try:
        try:
            result = switching.execute_switch(conn, switch_id, stop_after=stop_after)
        except versions.VersionError as exc:
            return _versions_redirect(table, column, period, error=str(exc))
    finally:
        conn.close()
    status_text = {"done": "已全部完成", "interrupted": "已中断（旧版本继续可查）", "running": "部分完成"}.get(result["status"], result["status"])
    return _versions_redirect(table, column, period, notice=f"本次执行 {result['executed']} 步 · {status_text}")


@app.post("/versions/switch/{switch_id}/resume")
async def resume_switch_form(request: Request, switch_id: str):
    form = await request.form()
    table = str(form.get("table") or "dwd_order")
    column = str(form.get("column") or "amount")
    period = str(form.get("period") or "") or None
    conn = get_conn()
    try:
        try:
            result = switching.execute_switch(conn, switch_id)
        except versions.VersionError as exc:
            return _versions_redirect(table, column, period, error=str(exc))
    finally:
        conn.close()
    return _versions_redirect(
        table, column, period, notice=f"恢复执行：补跑 {result['executed']} 个未完成步骤 · 状态 {result['status']}"
    )


@app.post("/versions/replay")
async def replay_period_form(request: Request):
    form = await request.form()
    table = str(form.get("table") or "dwd_order")
    column = str(form.get("column") or "amount")
    period = str(form.get("period") or "")
    conn = get_conn()
    try:
        try:
            result = switching.replay_period(conn, table, column, period)
        except versions.VersionError as exc:
            return _versions_redirect(table, column, period or None, error=str(exc))
    finally:
        conn.close()
    return _versions_redirect(
        table,
        column,
        period,
        notice=(
            f"账期 {period} 已按 v{result['version_no']}（{result['caliber']}）重放，"
            f"覆盖 {result['object_count']} 个下游对象；重跑同一账期只更新不新增。"
        ),
    )


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

@app.get("/health")
def health():
    conn = get_conn()
    try:
        stats = store.stats(conn)
    finally:
        conn.close()
    return {
        "status": "ok",
        "app": settings.APP_SHORT,
        "seeded": stats["sample_scripts"] > 0,
        "sample_scripts": stats["sample_scripts"],
        "broken_scripts": stats["broken_scripts"],
        "schema_tables": stats["schema_tables"],
        "runs": stats["runs"],
        "def_versions": stats["def_versions"],
        "switches": stats["switches"],
    }


@app.post("/api/runs")
def api_create_run(payload: dict = Body(default={})):
    mode = str(payload.get("mode") or "sample")
    sql = str(payload.get("sql") or "")
    filename = str(payload.get("filename") or "自定义脚本.sql")
    if mode not in {"sample", "sample_broken", "custom"}:
        raise HTTPException(status_code=400, detail=f"不支持的模式 {mode}；可选 sample / sample_broken / custom")
    conn = get_conn()
    try:
        run_id, analysis, err = _execute(conn, mode, sql, filename)
        if run_id is None:
            raise HTTPException(
                status_code=422,
                detail={"message": err, "errors": _errors_payload(analysis)},
            )
        data = store.load_run(conn, run_id)
        return {
            "run_id": run_id,
            "source_label": data["run"]["source_label"],
            "script_count": data["run"]["script_count"],
            "parsed_count": data["run"]["parsed_count"],
            "failed_count": data["run"]["failed_count"],
            "table_edge_count": data["run"]["table_edge_count"],
            "column_edge_count": data["run"]["column_edge_count"],
            "cycle_count": data["run"]["cycle_count"],
            "diagnostics": _errors_payload(analysis),
        }
    finally:
        conn.close()


@app.get("/api/runs")
def api_list_runs(limit: int = 10):
    conn = get_conn()
    try:
        return {"runs": store.list_runs(conn, limit=max(1, min(limit, 50)))}
    finally:
        conn.close()


@app.get("/api/runs/{run_id}")
def api_get_run(run_id: str):
    conn = get_conn()
    try:
        data, levels = _run_graph(conn, run_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"找不到运行记录 {run_id}")
        return {
            "run": data["run"],
            "scripts": [
                {
                    "filename": s["filename"],
                    "kind": s["kind"],
                    "target": s["target"],
                    "status": s["status"],
                    "columns": s["columns"],
                    "error": (
                        {"line": s.get("error_line"), "col": s.get("error_col"), "message": s.get("message")}
                        if s["status"] == "error"
                        else None
                    ),
                }
                for s in data["scripts"]
            ],
            "table_edges": data["table_edges"],
            "column_edge_count": len(data["column_edges"]),
            "column_groups": report.grouped_columns(data["column_edges"]),
            "cycles": data["cycles"],
            "levels": {k: v for k, v in levels.items()},
        }
    finally:
        conn.close()


@app.get("/api/tables")
def api_tables(run_id: str | None = None):
    conn = get_conn()
    try:
        catalog = _catalog(conn)
        kind_map: dict[str, str] = {}
        if run_id:
            data, _ = _run_graph(conn, run_id)
            if data:
                for e in data["table_edges"]:
                    kind_map[e["target_table"]] = e["target_kind"]
        return {
            "tables": [
                {"name": name, "kind": kind_map.get(name, "table"), "columns": cols}
                for name, cols in sorted(catalog.items())
            ]
        }
    finally:
        conn.close()


def _impact_for(run_id: str, table: str, column: str | None, period: str | None = None) -> tuple[dict, dict, dict, dict | None]:
    """计算影响面；带 period 时按账期解析口径版本，用该版本的快照替代传入的 run_id。"""
    conn = get_conn()
    try:
        version = None
        if period:
            try:
                version = switching.resolve(conn, table, column or "", period)
            except versions.VersionError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            if version is None:
                raise HTTPException(
                    status_code=400, detail=f"{table}.{column or ''} 在账期 {period} 没有已生效的口径版本"
                )
            run_id = version["run_id"]
        data, levels = _run_graph(conn, run_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"找不到运行记录 {run_id}")
        if not table:
            raise HTTPException(status_code=400, detail="缺少 table 参数（例如 ods_orders）")
        impact = graph.impact(data["table_edges"], data["column_edges"], levels, table, column or None)
        return data, levels, impact, version
    finally:
        conn.close()


@app.post("/api/impact")
def api_impact(payload: dict = Body(default={})):
    run_id = str(payload.get("run_id") or "")
    table = str(payload.get("table") or "")
    column = payload.get("column")
    column = str(column) if column not in (None, "") else None
    period = payload.get("period")
    period = str(period) if period not in (None, "") else None
    data, levels, impact, version = _impact_for(run_id, table, column, period)
    if not impact["found"]:
        raise HTTPException(status_code=400, detail=impact["message"])
    if version is not None:
        impact["version"] = version
        impact["run_id"] = version["run_id"]
    return impact


@app.post("/api/migration")
def api_migration(payload: dict = Body(default={})):
    run_id = str(payload.get("run_id") or "")
    table = str(payload.get("table") or "")
    column = payload.get("column")
    column = str(column) if column not in (None, "") else None
    period = payload.get("period")
    period = str(period) if period not in (None, "") else None
    operation = str(payload.get("operation") or "drop")
    new_name = payload.get("new_name")
    new_name = str(new_name) if new_name not in (None, "") else None
    if operation not in {"drop", "rename"}:
        raise HTTPException(status_code=400, detail=f"不支持的操作 {operation}；可选 drop / rename")
    if not column:
        raise HTTPException(status_code=400, detail="迁移清单必须指定列，例如 ods_orders.amount")
    if operation == "rename" and not new_name:
        raise HTTPException(status_code=400, detail="改名操作必须提供 new_name")
    data, levels, impact, version = _impact_for(run_id, table, column, period)
    if not impact["found"]:
        raise HTTPException(status_code=400, detail=impact["message"])
    plan = migrate.plan_migration(impact, operation, new_name, data["cycles"])
    plan["run_id"] = version["run_id"] if version is not None else run_id
    if version is not None:
        plan["version"] = version
    plan["impact"] = impact
    return plan


@app.get("/api/runs/{run_id}/export")
def api_export(run_id: str, format: str = "json"):
    conn = get_conn()
    try:
        data = store.load_run(conn, run_id)
        if data is None:
            raise HTTPException(status_code=404, detail=f"找不到运行记录 {run_id}")
        if format == "csv":
            body = report.export_csv(data["column_edges"])
            return Response(
                content=body,
                media_type="text/csv; charset=utf-8",
                headers={
                    "Content-Disposition": f'attachment; filename="lineagex_{run_id}_column_edges.csv"'
                },
            )
        payload = report.export_payload(
            data["run"], data["scripts"], data["table_edges"], data["column_edges"], data["cycles"]
        )
        return Response(
            content=report.dump_json(payload),
            media_type="application/json; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="lineagex_{run_id}.json"'
            },
        )
    finally:
        conn.close()


@app.get("/api/runs/{run_id}/impact_export")
def api_impact_export(run_id: str, table: str, column: str | None = None, operation: str = "drop", new_name: str | None = None):
    data, levels, impact, _version = _impact_for(run_id, table, column)
    if not impact["found"]:
        raise HTTPException(status_code=400, detail=impact["message"])
    plan = migrate.plan_migration(impact, operation, new_name, data["cycles"])
    return JSONResponse(
        content={"impact": impact, "migration": plan},
        headers={
            "Content-Disposition": f'attachment; filename="lineagex_{table}_{column or "all"}_impact.json"'
        },
    )


# --------------------------------------------------------------------------
# 口径版本 / 切换 / 账期查询与重放 API
# --------------------------------------------------------------------------

@app.get("/api/versions")
def api_list_versions(table: str | None = None, column: str | None = None):
    conn = get_conn()
    try:
        return {"versions": switching.list_versions(conn, table or None, column or None)}
    finally:
        conn.close()


@app.post("/api/versions")
def api_create_version(payload: dict = Body(default={})):
    table = str(payload.get("table") or "").strip()
    column = str(payload.get("column") or "").strip()
    caliber = str(payload.get("caliber") or "")
    expression = str(payload.get("expression") or "")
    effective_from = str(payload.get("effective_from") or "")
    if not table or not column:
        raise HTTPException(status_code=400, detail="缺少 table / column 参数")
    patch = None
    patch_sql = payload.get("patch_sql")
    patch_filename = payload.get("patch_filename")
    if patch_sql:
        if not patch_filename:
            raise HTTPException(status_code=400, detail="patch_sql 需要同时给出 patch_filename（样例脚本名）")
        patch = {str(patch_filename): str(patch_sql)}
    conn = get_conn()
    try:
        try:
            version = switching.create_version(
                conn, table, column, caliber, expression, effective_from, patch=patch
            )
        except versions.VersionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"version": version, "versions": switching.list_versions(conn, table, column)}
    finally:
        conn.close()


@app.get("/api/query")
def api_query_by_period(table: str, column: str, period: str):
    conn = get_conn()
    try:
        try:
            query = switching.query_column(conn, table, column, period)
        except versions.VersionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "table": query["table"],
            "column": query["column"],
            "period": query["period"],
            "version": query["version"],
            "downstream": query["impact"]["downstream"],
            "object_count": query["impact"]["object_count"],
            "edges": query["edges"],
        }
    finally:
        conn.close()


@app.post("/api/switch")
def api_start_switch(payload: dict = Body(default={})):
    table = str(payload.get("table") or "").strip()
    column = str(payload.get("column") or "").strip()
    to_version_id = payload.get("to_version_id")
    cutover_period = str(payload.get("cutover_period") or "")
    if not table or not column or to_version_id is None:
        raise HTTPException(status_code=400, detail="缺少 table / column / to_version_id 参数")
    conn = get_conn()
    try:
        try:
            sw = switching.start_switch(conn, table, column, int(to_version_id), cutover_period)
        except versions.VersionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {
            "switch_id": sw["run"]["switch_id"],
            "created": sw.get("created", False),
            "status": sw["run"]["status"],
            "step_count": len(sw["steps"]),
            "steps": sw["steps"],
        }
    finally:
        conn.close()


@app.get("/api/switch/{switch_id}")
def api_get_switch(switch_id: str):
    conn = get_conn()
    try:
        sw = switching.get_switch(conn, switch_id)
        if sw is None:
            raise HTTPException(status_code=404, detail=f"找不到切换 {switch_id}")
        done = sum(1 for s in sw["steps"] if s["status"] == "done")
        return {
            "switch_id": switch_id,
            "run": sw["run"],
            "steps": sw["steps"],
            "done_steps": done,
            "pending_steps": len(sw["steps"]) - done,
        }
    finally:
        conn.close()


@app.post("/api/switch/{switch_id}/execute")
def api_execute_switch(switch_id: str, payload: dict = Body(default={})):
    stop_after = payload.get("stop_after")
    stop_after = int(stop_after) if stop_after is not None else None
    conn = get_conn()
    try:
        try:
            result = switching.execute_switch(conn, switch_id, stop_after=stop_after)
        except versions.VersionError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "switch_id": switch_id,
            "executed": result["executed"],
            "status": result["status"],
            "steps": result["switch"]["steps"],
        }
    finally:
        conn.close()


@app.post("/api/switch/{switch_id}/resume")
def api_resume_switch(switch_id: str):
    conn = get_conn()
    try:
        try:
            result = switching.execute_switch(conn, switch_id)
        except versions.VersionError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return {
            "switch_id": switch_id,
            "executed": result["executed"],
            "status": result["status"],
            "steps": result["switch"]["steps"],
        }
    finally:
        conn.close()


@app.post("/api/replay")
def api_replay(payload: dict = Body(default={})):
    table = str(payload.get("table") or "").strip()
    column = str(payload.get("column") or "").strip()
    period = str(payload.get("period") or "")
    if not table or not column:
        raise HTTPException(status_code=400, detail="缺少 table / column 参数")
    conn = get_conn()
    try:
        try:
            result = switching.replay_period(conn, table, column, period)
        except versions.VersionError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        result["stored_rows"] = len(switching.period_results(conn, table, column, period))
        return result
    finally:
        conn.close()


@app.get("/api/replay")
def api_replay_results(table: str, column: str, period: str):
    conn = get_conn()
    try:
        rows = switching.period_results(conn, table, column, period)
        return {"period": period, "table": table, "column": column, "count": len(rows), "results": rows}
    finally:
        conn.close()
