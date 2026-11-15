"""seed：把样例脚本与 schema 写库，并跑一次基线解析。

幂等策略：meta.seed_version 命中且脚本数、schema 数一致时直接跳过；
重复执行不会产生脏数据，也不会重复插入脚本行。

ensure_version_seed 负责「口径版本」演示数据：dwd_order.amount 两个版本
（v1 含税现役 / v2 未税待生效），各自挂独立的解析快照；由 meta.version_seed_version
把关，重复执行跳过。
"""
from __future__ import annotations

import json
from pathlib import Path

from . import pipeline, settings, store, switching

VERSION_SEED_VERSION = "2026-11-15.1"

AMOUNT_TABLE = "dwd_order"
AMOUNT_COLUMN = "amount"
AMOUNT_V1_EXPRESSION = "CAST(o.amount AS DECIMAL(18,2))"
AMOUNT_V2_EXPRESSION = "ROUND(CAST(o.amount AS DECIMAL(18,2)) / 1.13, 2)"
AMOUNT_V1_EFFECTIVE_FROM = "2025-01"
AMOUNT_V2_EFFECTIVE_FROM = "2026-11"

#: v2 未税口径：只改 03_dwd_order.sql 的金额表达式，其余脚本与基线一致
AMOUNT_V2_SQL = """-- 订单落 dwd：金额切换为未税口径（含税金额 / 1.13），空渠道补 unknown
INSERT INTO dwd_order (order_id, customer_id, amount, status, channel, created_at)
SELECT
  o.order_id,
  o.customer_id,
  ROUND(CAST(o.amount AS DECIMAL(18,2)) / 1.13, 2) AS amount,
  o.status,
  COALESCE(o.channel, 'unknown') AS channel,
  o.created_at
FROM ods_orders o;
"""


def ensure_seed(conn, sample_dir: Path | None = None, seed_run: bool = True) -> dict:
    sample_dir = sample_dir or settings.SAMPLE_DIR
    sample_scripts = sorted((sample_dir / "sql").glob("*.sql"))
    broken_scripts = sorted((sample_dir / "sql_broken").glob("*.sql"))
    schema_files = sorted((sample_dir / "schemas").glob("*.json"))

    version = store.get_meta(conn, "seed_version")
    counts = (
        conn.execute("SELECT COUNT(*) FROM scripts").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM schemas").fetchone()[0],
    )
    expected = (len(sample_scripts) + len(broken_scripts), len(schema_files))
    if version == settings.SEED_VERSION and counts == expected:
        return {
            "seeded": False,
            "reason": "seed_version 命中，跳过",
            "scripts": counts[0],
            "schemas": counts[1],
            "baseline_run_id": store.get_meta(conn, "baseline_run_id"),
        }

    with conn:
        conn.execute("DELETE FROM scripts")
        order = 0
        for path in sample_scripts + broken_scripts:
            set_name = "sample" if path.parent.name == "sql" else "broken"
            order += 1
            conn.execute(
                "INSERT INTO scripts(filename, set_name, sql, sort_order) VALUES(?,?,?,?)",
                (path.name, set_name, path.read_text(encoding="utf-8"), order),
            )
        conn.execute("DELETE FROM schemas")
        for path in schema_files:
            data = json.loads(path.read_text(encoding="utf-8"))
            conn.execute(
                "INSERT INTO schemas(table_name, columns_json, comment) VALUES(?,?,?)",
                (
                    data["table"],
                    json.dumps([c["name"] for c in data["columns"]], ensure_ascii=False),
                    data.get("comment", ""),
                ),
            )
        store.set_meta(conn, "seed_version", settings.SEED_VERSION)

    baseline = store.get_meta(conn, "baseline_run_id")
    if seed_run and not baseline:
        run_id, _ = pipeline.run_sample(conn, sample_dir)
        store.set_meta(conn, "baseline_run_id", run_id)
        baseline = run_id

    return {
        "seeded": True,
        "reason": "写入样例脚本与 schema",
        "scripts": len(sample_scripts) + len(broken_scripts),
        "schemas": len(schema_files),
        "baseline_run_id": baseline,
    }


def ensure_version_seed(conn) -> dict:
    """写入口径版本演示数据（幂等）：amount 含税 v1（现役）+ 未税 v2（2026-11 起待生效）。

    v1 直接引用基线解析快照；v2 用打补丁的 03 脚本重放一次解析，得到独立快照 ——
    列定义与血缘都按生效账期保留版本，互不覆盖。
    """
    if store.get_meta(conn, "version_seed_version") == VERSION_SEED_VERSION:
        return {"seeded": False, "reason": "version_seed_version 命中，跳过"}

    if switching.list_versions(conn, AMOUNT_TABLE, AMOUNT_COLUMN):
        # 上次 seed 在写 meta 前中断：版本行已存在，补记 meta 即可，不重复插入
        store.set_meta(conn, "version_seed_version", VERSION_SEED_VERSION)
        conn.commit()
        return {"seeded": False, "reason": "口径版本已存在，补记 version_seed_version 后跳过"}

    baseline = store.get_meta(conn, "baseline_run_id")
    if not baseline:
        return {"seeded": False, "reason": "缺少基线解析，跳过口径版本 seed"}

    with conn:
        conn.execute(
            """INSERT INTO column_def_versions(table_name, column_name, version_no, caliber, expression,
                                               effective_from, effective_to, run_id, status, created_at)
               VALUES(?,?,?,?,?,?,NULL,?,'active',?)""",
            (
                AMOUNT_TABLE,
                AMOUNT_COLUMN,
                1,
                "含税",
                AMOUNT_V1_EXPRESSION,
                AMOUNT_V1_EFFECTIVE_FROM,
                baseline,
                store.now(),
            ),
        )
    v2 = switching.create_version(
        conn,
        AMOUNT_TABLE,
        AMOUNT_COLUMN,
        caliber="未税",
        expression=AMOUNT_V2_EXPRESSION,
        effective_from=AMOUNT_V2_EFFECTIVE_FROM,
        patch={"03_dwd_order.sql": AMOUNT_V2_SQL},
        label="口径版本快照 · dwd_order.amount v2（未税 = 含税 / 1.13）",
    )
    store.set_meta(conn, "version_seed_version", VERSION_SEED_VERSION)
    conn.commit()
    return {
        "seeded": True,
        "reason": "写入 dwd_order.amount 两个口径版本（v1 含税现役 / v2 未税待生效）",
        "v2_version_id": v2["version_id"],
    }
