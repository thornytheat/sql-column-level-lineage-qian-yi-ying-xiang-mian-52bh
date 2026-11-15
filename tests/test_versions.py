"""口径版本与切换：按账期选版、断点恢复、重放幂等。

状态约定：seed 给 dwd_order.amount 两个版本（v1 含税现役 / v2 未税 2026-11 起待生效）。
切换生命周期测试在 dwd_order.channel 上另起一对版本，不污染种子数据；
本文件内只读测试与改状态的测试互不影响。
"""
from __future__ import annotations

import pytest

CHANNEL_V1_SQL = None  # v1 直接用基线脚本，不打补丁
CHANNEL_V2_SQL = """-- 渠道列切换为大写口径
INSERT INTO dwd_order (order_id, customer_id, amount, status, channel, created_at)
SELECT
  o.order_id,
  o.customer_id,
  CAST(o.amount AS DECIMAL(18,2)) AS amount,
  o.status,
  UPPER(COALESCE(o.channel, 'unknown')) AS channel,
  o.created_at
FROM ods_orders o;
"""


# --------------------------------------------------------------------------
# 纯函数：版本窗口解析
# --------------------------------------------------------------------------

def test_resolve_version_window_rules():
    from app import versions

    vs = [
        {"version_no": 1, "status": "closed", "effective_from": "2025-01", "effective_to": "2026-11"},
        {"version_no": 2, "status": "active", "effective_from": "2026-11", "effective_to": None},
        {"version_no": 3, "status": "pending", "effective_from": "2027-01", "effective_to": None},
    ]
    assert versions.resolve_version(vs, "2025-01")["version_no"] == 1
    assert versions.resolve_version(vs, "2026-10")["version_no"] == 1  # 窗口右开
    assert versions.resolve_version(vs, "2026-11")["version_no"] == 2
    assert versions.resolve_version(vs, "2027-01")["version_no"] == 2  # pending 对查询不可见
    assert versions.resolve_version(vs, "2024-12") is None
    with pytest.raises(versions.VersionError):
        versions.resolve_version(vs, "2026-13")


def test_prev_period_and_validation():
    from app import versions

    assert versions.prev_period("2026-11") == "2026-10"
    assert versions.prev_period("2026-01") == "2025-12"
    with pytest.raises(versions.VersionError):
        versions.check_period("2026-1")


def test_flatten_steps_stable_keys():
    from app import versions

    plan = {
        "steps": [
            {"action": "freeze", "title": "冻结", "detail": "d", "objects": []},
            {
                "action": "rewrite",
                "title": "改写 L2",
                "detail": "d",
                "objects": [{"object": "a"}, {"object": "b"}],
            },
            {"action": "activate", "title": "生效", "detail": "d", "objects": []},
        ]
    }
    flat = versions.flatten_steps(plan)
    assert [s["step_key"] for s in flat] == ["freeze:all", "rewrite:a", "rewrite:b", "activate:all"]
    # 同一计划重复生成，键完全一致 —— 断点恢复与幂等的基础
    assert versions.flatten_steps(plan) == flat


# --------------------------------------------------------------------------
# 种子版本与按账期查询
# --------------------------------------------------------------------------

def test_seeded_amount_versions(client):
    res = client.get("/api/versions?table=dwd_order&column=amount")
    assert res.status_code == 200
    vs = res.json()["versions"]
    assert [v["version_no"] for v in vs] == [1, 2]
    v1, v2 = vs
    assert v1["status"] == "active" and v1["caliber"] == "含税"
    assert v1["effective_from"] == "2025-01" and v1["effective_to"] is None
    assert v2["status"] == "pending" and v2["caliber"] == "未税"
    assert v2["effective_from"] == "2026-11"
    # 两个版本各挂一份独立解析快照：列定义与血缘都按生效时间保留版本
    assert v1["run_id"] != v2["run_id"]


def test_query_routes_by_period(client):
    # 财务旧账期 → 含税 v1
    res = client.get("/api/query?table=dwd_order&column=amount&period=2026-09")
    assert res.status_code == 200
    body = res.json()
    assert body["version"]["version_no"] == 1
    assert body["version"]["caliber"] == "含税"
    assert body["version"]["expression"] == "CAST(o.amount AS DECIMAL(18,2))"
    assert body["object_count"] == 4  # dws_customer_order / dws_order_wide / v_channel_sales / ads_customer_rank

    # 切换发生前，新账期仍回落到现役旧版本（pending 不可见 = 旧版本继续可查）
    res = client.get("/api/query?table=dwd_order&column=amount&period=2026-11")
    assert res.status_code == 200
    assert res.json()["version"]["version_no"] == 1

    # 没有版本覆盖的账期 → 400，不静默选版
    assert client.get("/api/query?table=dwd_order&column=amount&period=2024-12").status_code == 400
    assert client.get("/api/query?table=dwd_order&column=amount&period=2026-13").status_code == 400


def test_impact_and_migration_select_version_by_period(client):
    res = client.post("/api/impact", json={"table": "dwd_order", "column": "amount", "period": "2026-09"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["version"]["caliber"] == "含税"
    assert body["run_id"] == body["version"]["run_id"]
    assert body["object_count"] == 4

    res = client.post(
        "/api/migration",
        json={"table": "dwd_order", "column": "amount", "period": "2026-09", "operation": "drop"},
    )
    assert res.status_code == 200, res.text
    plan = res.json()
    assert plan["version"]["version_no"] == 1
    assert plan["run_id"] == plan["version"]["run_id"]
    assert plan["step_count"] >= 3  # 冻结 → 逐层改写 → 回填 → DDL


def test_version_registration_validation(client):
    # 账期格式非法
    res = client.post(
        "/api/versions",
        json={"table": "dwd_order", "column": "status", "caliber": "x", "expression": "o.status", "effective_from": "2026-13"},
    )
    assert res.status_code == 400
    # 与待生效版本（v2 未税，2026-11 起）窗口重叠
    res = client.post(
        "/api/versions",
        json={
            "table": "dwd_order",
            "column": "amount",
            "caliber": "x",
            "expression": "o.amount",
            "effective_from": "2026-12",
        },
    )
    assert res.status_code == 400
    assert "窗口" in res.json()["detail"]


# --------------------------------------------------------------------------
# 账期重放：幂等，不新增重复结果
# --------------------------------------------------------------------------

def test_replay_same_period_is_idempotent(client):
    payload = {"table": "dwd_order", "column": "amount", "period": "2026-09"}
    r1 = client.post("/api/replay", json=payload)
    assert r1.status_code == 200, r1.text
    first = r1.json()
    assert first["version_no"] == 1 and first["caliber"] == "含税"
    assert first["object_count"] == 4
    assert first["stored_rows"] == 4

    r2 = client.post("/api/replay", json=payload)
    assert r2.status_code == 200
    second = r2.json()
    assert second["stored_rows"] == first["stored_rows"]  # 重跑同一账期不新增重复结果

    rows = client.get("/api/replay?table=dwd_order&column=amount&period=2026-09").json()
    assert rows["count"] == 4
    objects = {r["object_name"] for r in rows["results"]}
    assert objects == {"dws_customer_order", "dws_order_wide", "v_channel_sales", "ads_customer_rank"}
    assert all(r["version_no"] == 1 for r in rows["results"])  # 旧账期永远重放旧口径


def test_replay_period_without_version_is_400(client):
    res = client.post("/api/replay", json={"table": "dwd_order", "column": "amount", "period": "2024-12"})
    assert res.status_code == 400


# --------------------------------------------------------------------------
# 切换生命周期：中断 → 旧版本可查 → 恢复只补未完成 → 生效后按账期分流
# --------------------------------------------------------------------------

def _create_channel_versions(client) -> tuple[int, int]:
    """在 dwd_order.channel 上登记 v1（现役）/ v2（待生效），返回两个 version_id。"""
    res = client.post(
        "/api/versions",
        json={
            "table": "dwd_order",
            "column": "channel",
            "caliber": "原始渠道",
            "expression": "COALESCE(o.channel, 'unknown')",
            "effective_from": "2025-01",
        },
    )
    assert res.status_code == 200, res.text
    v1 = res.json()["version"]
    assert v1["status"] == "active"  # 首个版本直接生效

    res = client.post(
        "/api/versions",
        json={
            "table": "dwd_order",
            "column": "channel",
            "caliber": "大写渠道",
            "expression": "UPPER(COALESCE(o.channel, 'unknown'))",
            "effective_from": "2026-11",
            "patch_filename": "03_dwd_order.sql",
            "patch_sql": CHANNEL_V2_SQL,
        },
    )
    assert res.status_code == 200, res.text
    v2 = res.json()["version"]
    assert v2["status"] == "pending"
    return v1["version_id"], v2["version_id"]


def test_switch_lifecycle_interrupt_resume(client):
    _v1_id, v2_id = _create_channel_versions(client)

    # 切换账期必须等于新版本生效账期
    bad = client.post(
        "/api/switch",
        json={"table": "dwd_order", "column": "channel", "to_version_id": v2_id, "cutover_period": "2026-12"},
    )
    assert bad.status_code == 400

    # 创建切换计划（波及对象 / 执行顺序取自新版本快照）；重复创建幂等
    payload = {"table": "dwd_order", "column": "channel", "to_version_id": v2_id, "cutover_period": "2026-11"}
    s1 = client.post("/api/switch", json=payload)
    assert s1.status_code == 200, s1.text
    plan = s1.json()
    assert plan["created"] is True
    assert plan["step_count"] == 6  # 冻结 + 改写×2 + 回填×1 + DDL + 生效
    assert plan["steps"][0]["action"] == "freeze"
    assert plan["steps"][-1]["action"] == "activate"  # 生效永远最后：此前中断旧版本都可查

    s2 = client.post("/api/switch", json=payload)
    assert s2.json()["created"] is False
    assert s2.json()["switch_id"] == plan["switch_id"]
    assert s2.json()["step_count"] == plan["step_count"]  # 不重复插步骤
    switch_id = plan["switch_id"]

    # 跑 2 步后中断：旧版本对所有账期继续可查
    res = client.post(f"/api/switch/{switch_id}/execute", json={"stop_after": 2})
    assert res.status_code == 200
    assert res.json()["status"] == "interrupted"
    assert res.json()["executed"] == 2
    done_keys = [s["step_key"] for s in res.json()["steps"] if s["status"] == "done"]
    assert done_keys == ["freeze:all", "rewrite:dws_order_wide"]

    q = client.get("/api/query?table=dwd_order&column=channel&period=2026-11")
    assert q.status_code == 200
    assert q.json()["version"]["version_no"] == 1  # 中断时旧版本继续可查
    assert q.json()["version"]["caliber"] == "原始渠道"

    # 恢复：只补未完成部分（6 - 2 = 4 步），已完成的步骤不重跑
    res = client.post(f"/api/switch/{switch_id}/resume")
    assert res.status_code == 200
    assert res.json()["executed"] == 4
    assert res.json()["status"] == "done"
    assert all(s["status"] == "done" for s in res.json()["steps"])

    # 已完成的切换再执行是空操作
    res = client.post(f"/api/switch/{switch_id}/execute", json={})
    assert res.json()["executed"] == 0
    assert res.json()["status"] == "done"

    # 生效后按账期分流：旧账期 → v1（已关闭但可查），新账期 → v2
    q_old = client.get("/api/query?table=dwd_order&column=channel&period=2026-10").json()
    assert q_old["version"]["version_no"] == 1
    assert q_old["version"]["status"] == "closed"
    q_new = client.get("/api/query?table=dwd_order&column=channel&period=2026-11").json()
    assert q_new["version"]["version_no"] == 2
    assert q_new["version"]["caliber"] == "大写渠道"

    # 财务重放旧账期：仍拿到旧口径结果；运营新账期：新口径
    r_old = client.post("/api/replay", json={"table": "dwd_order", "column": "channel", "period": "2026-10"}).json()
    assert r_old["version_no"] == 1 and r_old["caliber"] == "原始渠道"
    r_new = client.post("/api/replay", json={"table": "dwd_order", "column": "channel", "period": "2026-11"}).json()
    assert r_new["version_no"] == 2 and r_new["caliber"] == "大写渠道"

    # 已生效的版本不能再作为切换目标
    again = client.post("/api/switch", json=payload)
    assert again.status_code == 400


def test_switch_not_found(client):
    assert client.get("/api/switch/sw_nope").status_code == 404
    assert client.post("/api/switch/sw_nope/execute", json={}).status_code == 404


# --------------------------------------------------------------------------
# 页面
# --------------------------------------------------------------------------

def test_versions_page_renders(client):
    res = client.get("/versions")
    assert res.status_code == 200
    assert "口径版本与切换" in res.text
    assert "含税" in res.text and "未税" in res.text
    assert "2026-11" in res.text

    res = client.get("/versions?period=2026-09")
    assert res.status_code == 200
    assert "v1" in res.text and "含税" in res.text

    res = client.get("/versions?period=2024-12")
    assert res.status_code == 200
    assert "没有已生效的口径版本" in res.text
