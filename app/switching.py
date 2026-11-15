"""口径切换编排：版本登记 → 切换计划 → 断点执行 → 恢复 → 账期重放。

设计要点（对应需求）：
- 列定义与血缘都按生效账期保留版本：版本行挂一份独立解析快照（run_id），永不原地改；
- 波及对象 / 执行顺序 / 查询都按账期选版：impact 与切换计划取自目标账期版本的快照，
  查询与重放取该账期窗口命中的版本；
- 切换中断旧版本继续可查：activate 是最后一步，此前 pending 版本对查询不可见、
  旧版本窗口未关闭；
- 恢复只补未完成部分：步骤级断点（每步一个事务），恢复时只跑 status='pending' 的步骤；
- 重跑同一账期不新增重复结果：period_results 主键 upsert；切换重复创建 / 重复执行
  也都幂等（确定性 switch_id + 步骤条件更新）。
"""
from __future__ import annotations

import json

from . import graph, pipeline, settings, store, versions


# --------------------------------------------------------------------------
# 版本登记与解析
# --------------------------------------------------------------------------

def list_versions(conn, table: str | None = None, column: str | None = None) -> list[dict]:
    sql = "SELECT * FROM column_def_versions"
    args: list = []
    if table and column:
        sql += " WHERE table_name = ? AND column_name = ?"
        args = [table, column]
    sql += " ORDER BY table_name, column_name, version_no"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def get_version(conn, version_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM column_def_versions WHERE version_id = ?", (version_id,)).fetchone()
    return dict(row) if row else None


def resolve(conn, table: str, column: str, period: str) -> dict | None:
    """账期 → 版本（active / closed 可见；pending 不可见）。"""
    return versions.resolve_version(list_versions(conn, table, column), period)


def create_version(
    conn,
    table: str,
    column: str,
    caliber: str,
    expression: str,
    effective_from: str,
    patch: dict[str, str] | None = None,
    label: str | None = None,
) -> dict:
    """登记新口径版本：用补丁脚本重放一次解析，作为该版本独立的血缘快照。

    patch: {脚本文件名: 新 SQL}，在样例脚本集上整体替换后解析；不传则快照与基线一致。
    首个版本直接 active（窗口至今），后续版本一律 pending，等切换生效。
    """
    caliber = (caliber or "").strip()
    expression = (expression or "").strip()
    if not caliber or not expression:
        raise versions.VersionError("口径名称与列定义表达式都不能为空")
    existing = list_versions(conn, table, column)
    versions.validate_new_window(existing, effective_from)

    files = pipeline.load_sample_files(settings.SAMPLE_DIR)
    if patch:
        missing = sorted(set(patch) - {f["filename"] for f in files})
        if missing:
            raise versions.VersionError(f"补丁脚本不在样例集中：{', '.join(missing)}")
        files = [dict(f, sql=patch[f["filename"]]) if f["filename"] in patch else f for f in files]
    schemas = pipeline.load_schemas(settings.SAMPLE_DIR)
    analysis = pipeline.analyze(files, schemas)
    if analysis["failed_count"]:
        bad = next(s for s in analysis["scripts"] if s["status"] == "error")
        raise versions.VersionError(f"新版本脚本解析失败：{bad['filename']} 第 {bad['error_line']} 行 {bad['message']}")

    version_no = versions.next_version_no(existing)
    run_id = pipeline.persist(
        conn, analysis, label or f"口径版本快照 · {table}.{column} v{version_no}（{caliber}）"
    )
    status = "active" if not existing else "pending"
    cur = conn.execute(
        """INSERT INTO column_def_versions(table_name, column_name, version_no, caliber, expression,
                                           effective_from, effective_to, run_id, status, created_at)
           VALUES(?,?,?,?,?,?,NULL,?,?,?)""",
        (table, column, version_no, caliber, expression, effective_from, run_id, status, store.now()),
    )
    conn.commit()
    return get_version(conn, int(cur.lastrowid))


# --------------------------------------------------------------------------
# 切换计划与执行
# --------------------------------------------------------------------------

def get_switch(conn, switch_id: str) -> dict | None:
    run = conn.execute("SELECT * FROM switch_runs WHERE switch_id = ?", (switch_id,)).fetchone()
    if run is None:
        return None
    steps = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM switch_steps WHERE switch_id = ? ORDER BY step_order", (switch_id,)
        ).fetchall()
    ]
    return {"run": dict(run), "steps": steps}


def latest_switch(conn, table: str, column: str) -> dict | None:
    row = conn.execute(
        """SELECT switch_id FROM switch_runs WHERE table_name = ? AND column_name = ?
           ORDER BY created_at DESC, switch_id DESC LIMIT 1""",
        (table, column),
    ).fetchone()
    return get_switch(conn, row["switch_id"]) if row else None


def _impact_for_run(conn, run_id: str, table: str, column: str) -> tuple[dict, dict]:
    data = store.load_run(conn, run_id)
    if data is None:
        raise versions.VersionError(f"版本快照 {run_id} 不存在，请重新登记版本")
    levels = graph.topo_levels(graph.table_adjacency(data["table_edges"]))
    impact = graph.impact(data["table_edges"], data["column_edges"], levels, table, column)
    return data, impact


def start_switch(conn, table: str, column: str, to_version_id: int, cutover_period: str) -> dict:
    """创建切换计划（幂等）：波及对象与执行顺序取自新版本（切换账期）的血缘快照。"""
    versions.check_period(cutover_period)
    to_v = get_version(conn, to_version_id)
    if to_v is None or to_v["table_name"] != table or to_v["column_name"] != column:
        raise versions.VersionError(f"版本 {to_version_id} 不属于 {table}.{column}")
    if to_v["status"] == "active":
        raise versions.VersionError(f"v{to_v['version_no']} 已是现役版本，无需切换")
    if to_v["status"] != "pending":
        raise versions.VersionError(f"v{to_v['version_no']} 状态为 {to_v['status']}，不能作为切换目标")
    if cutover_period != to_v["effective_from"]:
        raise versions.VersionError(
            f"切换账期 {cutover_period} 必须等于新版本生效账期 {to_v['effective_from']}（窗口起点即切换点）"
        )
    from_v = resolve(conn, table, column, versions.prev_period(cutover_period))
    if from_v is None:
        raise versions.VersionError(f"切换账期 {cutover_period} 之前没有现役版本可接替")

    switch_id = versions.switch_id_for(table, column, from_v["version_no"], to_v["version_no"])
    existing = get_switch(conn, switch_id)
    if existing is not None:
        existing["created"] = False
        return existing

    # 波及对象 / 执行顺序：用目标账期（新版本）的快照计算
    data, impact = _impact_for_run(conn, to_v["run_id"], table, column)
    plan = versions.plan_caliber_switch(impact, table, column, from_v, to_v, cutover_period, data["cycles"])
    flat = versions.flatten_steps(plan)

    now = store.now()
    with conn:
        conn.execute(
            """INSERT INTO switch_runs(switch_id, table_name, column_name, from_version_id, to_version_id,
                                       cutover_period, status, created_at, updated_at)
               VALUES(?,?,?,?,?,?, 'running', ?, ?)""",
            (switch_id, table, column, from_v["version_id"], to_v["version_id"], cutover_period, now, now),
        )
        for order, step in enumerate(flat, start=1):
            conn.execute(
                """INSERT INTO switch_steps(switch_id, step_key, step_order, action, object_name, title, detail)
                   VALUES(?,?,?,?,?,?,?)""",
                (switch_id, step["step_key"], order, step["action"], step["object_name"], step["title"], step["detail"]),
            )
    result = get_switch(conn, switch_id)
    result["created"] = True
    result["plan"] = plan
    return result


def _finalize(conn, switch_run: dict) -> None:
    """关闭旧版本窗口、启用新版本（与 activate 步骤同事务，要么都成要么都不成）。"""
    conn.execute(
        "UPDATE column_def_versions SET effective_to = ?, status = 'closed' WHERE version_id = ?",
        (switch_run["cutover_period"], switch_run["from_version_id"]),
    )
    conn.execute(
        "UPDATE column_def_versions SET status = 'active' WHERE version_id = ?",
        (switch_run["to_version_id"],),
    )


def execute_switch(conn, switch_id: str, stop_after: int | None = None) -> dict:
    """按序执行未完成的步骤；每步一个事务即一个断点。

    stop_after：跑够 N 步后主动中断（演示 / 演练用），状态置 interrupted。
    真实崩溃的效果与此相同：已提交的步骤保持 done，恢复时只补 pending。
    重复执行已完成的切换是空操作（executed = 0），不会产生重复结果。
    """
    sw = get_switch(conn, switch_id)
    if sw is None:
        raise versions.VersionError(f"找不到切换 {switch_id}")
    run = sw["run"]
    if run["status"] == "done":
        return {"switch": sw, "executed": 0, "status": "done"}

    conn.execute("UPDATE switch_runs SET status = 'running', updated_at = ? WHERE switch_id = ?", (store.now(), switch_id))
    conn.commit()

    executed = 0
    interrupted = False
    for step in [s for s in sw["steps"] if s["status"] == "pending"]:
        if stop_after is not None and executed >= stop_after:
            interrupted = True
            break
        if step["action"] == "activate":
            _finalize(conn, run)
        # 条件更新：只有仍是 pending 的步骤才会被置 done —— 断点恢复 / 重试都幂等
        cur = conn.execute(
            """UPDATE switch_steps SET status = 'done', done_at = ?
               WHERE switch_id = ? AND step_key = ? AND status = 'pending'""",
            (store.now(), switch_id, step["step_key"]),
        )
        if cur.rowcount:
            executed += 1
        conn.commit()  # 每步一个断点：中断时已完成的步骤不会丢

    remaining = conn.execute(
        "SELECT COUNT(*) FROM switch_steps WHERE switch_id = ? AND status = 'pending'", (switch_id,)
    ).fetchone()[0]
    status = "interrupted" if interrupted and remaining else ("done" if remaining == 0 else "running")
    conn.execute(
        "UPDATE switch_runs SET status = ?, updated_at = ? WHERE switch_id = ?",
        (status, store.now(), switch_id),
    )
    conn.commit()
    return {"switch": get_switch(conn, switch_id), "executed": executed, "status": status}


# --------------------------------------------------------------------------
# 账期查询与重放
# --------------------------------------------------------------------------

def query_column(conn, table: str, column: str, period: str) -> dict:
    """按账期选版查询：列定义、口径与波及对象全部来自该账期命中的版本快照。"""
    version = resolve(conn, table, column, period)
    if version is None:
        raise versions.VersionError(f"{table}.{column} 在账期 {period} 没有已生效的口径版本")
    data, impact = _impact_for_run(conn, version["run_id"], table, column)
    edges = [
        e
        for e in data["column_edges"]
        if (e["source_table"] == table and e["source_column"] == column)
        or (e["target_table"] == table and e["target_column"] == column)
    ]
    return {
        "table": table,
        "column": column,
        "period": period,
        "version": version,
        "impact": impact,
        "edges": edges,
    }


def replay_period(conn, table: str, column: str, period: str) -> dict:
    """按账期重放：用该账期版本的快照重算下游结果，主键 upsert —— 重跑不新增重复行。"""
    query = query_column(conn, table, column, period)
    version = query["version"]
    now = store.now()
    objects = query["impact"]["downstream"]
    with conn:
        for it in objects:
            payload = {
                "object": it["object"],
                "kind": it["kind"],
                "level": it["level"],
                "relation": it["relation"],
                "columns": it["columns"],
                "caliber": version["caliber"],
                "expression": version["expression"],
                "run_id": version["run_id"],
            }
            conn.execute(
                """INSERT INTO period_results(period, table_name, column_name, version_no, object_name, payload_json, replayed_at)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(period, table_name, column_name, object_name)
                   DO UPDATE SET version_no = excluded.version_no,
                                 payload_json = excluded.payload_json,
                                 replayed_at = excluded.replayed_at""",
                (period, table, column, version["version_no"], it["object"], json.dumps(payload, ensure_ascii=False), now),
            )
    return {
        "period": period,
        "table": table,
        "column": column,
        "version_no": version["version_no"],
        "caliber": version["caliber"],
        "object_count": len(objects),
    }


def period_results(conn, table: str, column: str, period: str) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM period_results WHERE period = ? AND table_name = ? AND column_name = ?
           ORDER BY object_name""",
        (period, table, column),
    ).fetchall()
    out = []
    for r in rows:
        row = dict(r)
        row["payload"] = json.loads(row["payload_json"])
        out.append(row)
    return out
