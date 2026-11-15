"""口径版本：列定义按生效账期保留版本，查询 / 波及面 / 切换都按账期选版。

纯函数、无 IO。约定：
- 账期格式 YYYY-MM，字符串比较即时间比较；
- 版本窗口 [effective_from, effective_to)，effective_to 为空表示「至今」；
- 状态机：pending（已登记未生效）→ active（现役）→ closed（已关闭，仅历史可查）；
- 查询可见性：active 与 closed 都参与解析（closed 正是财务要重放的历史口径），
  pending 对查询不可见 —— 切换中断时旧版本自然继续可查。
"""
from __future__ import annotations

import re

PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

#: 对账期查询可见的状态（pending 不可见）
QUERYABLE_STATUSES = ("active", "closed")


class VersionError(ValueError):
    """口径版本规则冲突：账期格式错、窗口重叠、状态不允许、切换前提不满足。"""


def check_period(period: str) -> str:
    if not PERIOD_RE.match(period or ""):
        raise VersionError(f"账期格式应为 YYYY-MM（如 2026-11），收到 {period!r}")
    return period


def prev_period(period: str) -> str:
    check_period(period)
    year, month = int(period[:4]), int(period[5:7])
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


def window_contains(version: dict, period: str) -> bool:
    """账期是否落在版本窗口 [effective_from, effective_to) 内。"""
    start = version["effective_from"]
    end = version["effective_to"]
    return start <= period and (end is None or period < end)


def resolve_version(versions: list[dict], period: str, statuses: tuple[str, ...] = QUERYABLE_STATUSES) -> dict | None:
    """解析账期对应的版本：窗口包含该账期且状态可见；多个命中时取生效最晚的。"""
    check_period(period)
    candidates = [v for v in versions if v["status"] in statuses and window_contains(v, period)]
    if not candidates:
        return None
    return max(candidates, key=lambda v: (v["effective_from"], v["version_no"]))


def next_version_no(versions: list[dict]) -> int:
    return max((v["version_no"] for v in versions), default=0) + 1


def validate_new_window(versions: list[dict], effective_from: str) -> None:
    """新登记（pending）版本的起点不得落在另一个 pending 版本的窗口内。"""
    check_period(effective_from)
    for v in versions:
        if v["status"] == "pending" and window_contains(v, effective_from):
            raise VersionError(
                f"账期 {effective_from} 已落在待生效版本 v{v['version_no']} 的窗口内"
                f"（{v['effective_from']} 起），请先完成或废弃该版本的切换"
            )


def switch_id_for(table: str, column: str, from_no: int, to_no: int) -> str:
    """切换 run 的确定性主键：同一对版本重复创建只会得到同一个 switch。"""
    return f"sw_{table}_{column}_v{from_no}_v{to_no}"


def plan_caliber_switch(
    impact: dict,
    table: str,
    column: str,
    from_version: dict,
    to_version: dict,
    cutover_period: str,
    cycles: list[dict] | None = None,
) -> dict:
    """口径切换的执行顺序（与迁移清单同构，但语义是「换口径」而非删列/改名）。

    顺序原则：冻结 → 按拓扑层级逐层改下游 → 物理表回填 → 切换列定义 → 最后才关闭旧版本窗口。
    activate 永远是最后一步：它之前任何中断，旧版本对所有账期继续可查。
    """
    items = impact.get("downstream", [])
    cycles = cycles or []
    from_no, to_no = from_version["version_no"], to_version["version_no"]

    steps: list[dict] = [
        {
            "action": "freeze",
            "title": "冻结调度并记录基线",
            "detail": (
                f"暂停写入 {table} 的调度任务，记录 {len(items)} 个下游对象的行数与校验和；"
                f"旧版本 v{from_no}（{from_version['caliber']}）窗口未关闭，此时中断不影响任何账期查询。"
            ),
            "objects": [],
        }
    ]

    levels = sorted({it["level"] for it in items if it["level"] is not None})
    for level in levels:
        objs = [it for it in items if it["level"] == level]
        steps.append(
            {
                "action": "rewrite",
                "title": f"按新口径改写第 {level} 层下游（{len(objs)} 个对象）",
                "detail": (
                    f"把 {table}.{column} 的引用口径从「{from_version['caliber']}」切到「{to_version['caliber']}」"
                    f"（{to_version['expression']}）；本层只依赖更浅层，可单独重跑验证。"
                ),
                "objects": objs,
            }
        )

    cycle_items = [it for it in items if it["level"] is None]
    if cycle_items:
        steps.append(
            {
                "action": "uncycle",
                "title": f"先断开环内下游对象（{len(cycle_items)} 个）",
                "detail": "这些对象位于依赖环中，拓扑层级不确定；需先确认环的合法朝向或临时物化，再继续切换。",
                "objects": cycle_items,
            }
        )

    backfill = [it for it in items if it["kind"] == "table"]
    steps.append(
        {
            "action": "backfill",
            "title": f"回填需重算的物理表（{len(backfill)} 个）",
            "detail": "物理表不会自动跟随口径变化，按依赖层级从浅到深重跑；视图只需重新解析部署，不在此列。",
            "objects": backfill,
        }
    )
    steps.append(
        {
            "action": "ddl",
            "title": "切换列定义并复核",
            "detail": (
                f"确认下游全部改写完成后，把 {table}.{column} 的定义切换为 {to_version['expression']}；"
                "重新解析全部脚本，确认旧口径引用为 0。"
            ),
            "objects": [],
        }
    )
    steps.append(
        {
            "action": "activate",
            "title": f"关闭旧版本窗口，新口径自 {cutover_period} 生效",
            "detail": (
                f"把 v{from_no} 的 effective_to 置为 {cutover_period}（历史账期仍可重放），"
                f"v{to_no} 置为 active；此步骤完成前任何中断，旧版本对所有账期继续可查。"
            ),
            "objects": [],
        }
    )

    warnings: list[str] = []
    if not items:
        warnings.append("该列没有下游引用，切换只影响列定义本身；仍建议保留一次全量解析作为复核。")
    if cycles:
        warnings.append(f"血缘图存在 {len(cycles)} 条环，切换前建议先处理：" + "；".join(c["key"] for c in cycles[:3]))

    return {
        "table": table,
        "column": column,
        "from_version": from_no,
        "to_version": to_no,
        "cutover_period": cutover_period,
        "steps": steps,
        "step_count": len(steps),
        "affected_objects": len(items),
        "warnings": warnings,
    }


def flatten_steps(plan: dict) -> list[dict]:
    """把切换计划拍平成可断点续跑的最小步骤：每个下游对象一步，冻结/DDL/生效各一步。

    step_key 稳定（动作:对象），同一计划重复生成得到同样的键 —— 这是恢复时
    「只补未完成部分」与「重跑不重复」的基础。
    """
    flat: list[dict] = []
    for pstep in plan["steps"]:
        objects = pstep.get("objects") or []
        if not objects:
            flat.append(
                {
                    "step_key": f"{pstep['action']}:all",
                    "action": pstep["action"],
                    "object_name": "all",
                    "title": pstep["title"],
                    "detail": pstep["detail"],
                }
            )
            continue
        for obj in objects:
            flat.append(
                {
                    "step_key": f"{pstep['action']}:{obj['object']}",
                    "action": pstep["action"],
                    "object_name": obj["object"],
                    "title": f"{pstep['title']} · {obj['object']}",
                    "detail": pstep["detail"],
                }
            )
    keys = [s["step_key"] for s in flat]
    if len(keys) != len(set(keys)):
        raise VersionError("切换计划产生了重复步骤键，请检查波及对象是否重复")
    return flat
