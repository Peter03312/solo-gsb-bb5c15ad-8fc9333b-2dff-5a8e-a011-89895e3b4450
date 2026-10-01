"""断言批次提交预检（只读）。

考古人员在正式提交批次前，可用本预检回答一个问题：在本批新增 / 撤回按**正式
批次语义**模拟应用之后，为消除仍存在的关系冲突，最少还需要额外撤回哪些
**生效中的旧证据**？

口径：
- 预检完全只读：不生成修订、不写变化记录或断言审计、不改动任何状态
  （REPEATABLE READ READ ONLY 快照事务）；
- 修订号过期、证据编号占用、撤回目标无效、引用未登记地层等拒绝语义与
  `POST /batches` 完全一致（顺序也一致：逐条操作模拟，先到先拒）；
- 额外撤回集合只能取自“本批应用后仍生效”的旧证据：
  不含本批新增证据（含新增后又在本批撤回的），也不重复本批已撤回的证据；
- 选取规则：撤回数最少；并列时按证据编号序列字典序最小；
- 本批最终保留的新断言**自身**矛盾时，撤回旧证据不可能修复，单列说明；
- 预算（0~3）内找不到方案时说明预算不足；两种失败都附当前（本批应用后的）
  仍存在的冲突证据路径。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from .db import db_conn
from .derivation import Assertion, Derivation
from .errors import ApiError
from .models import AssertOp, BatchPreflightIn, RetractOp


# ------------------------------------------------------------------ 正式批次的只读模拟
def _simulate(cur, body: BatchPreflightIn) -> Tuple[int, List[Assertion], Set[str]]:
    """在只读事务内按正式批次语义模拟整批操作。

    抛错（STALE_REVISION / INVALID_STRATUM / DUPLICATE_EVIDENCE /
    ASSERTION_NOT_FOUND）与 service.apply_batch 完全同构；成功时返回
    (当前修订号, 本批应用后的生效断言列表, 本批新增证据编号集合)。
    """
    cur.execute("SELECT revision FROM revision_counter WHERE id = TRUE")
    current = cur.fetchone()[0]
    if body.expected_revision != current:
        raise ApiError(
            409,
            "STALE_REVISION",
            f"修订号过期：请求携带 {body.expected_revision}，当前为 {current}",
            {"current_revision": current},
        )

    # 现有断言全量（含已撤回——证据编号永久占用、撤回目标须当前生效）
    cur.execute(
        "SELECT evidence_id, stratum_a, stratum_b, relation, status FROM assertions"
    )
    view: Dict[str, Dict[str, str]] = {
        r[0]: {"a": r[1], "b": r[2], "relation": r[3], "status": r[4]}
        for r in cur.fetchall()
    }
    cur.execute("SELECT stratum_id FROM strata")
    strata = {r[0] for r in cur.fetchall()}

    batch_new: Set[str] = set()
    for op in body.operations:
        if isinstance(op, AssertOp):
            # 与正式批次一致：先校验地层，再校验证据编号占用
            for s in (op.stratum_a, op.stratum_b):
                if s not in strata:
                    raise ApiError(
                        400,
                        "INVALID_STRATUM",
                        f"地层未登记: {s}",
                        {"stratum_id": s},
                    )
            if op.evidence_id in view:
                raise ApiError(
                    409,
                    "DUPLICATE_EVIDENCE",
                    f"证据编号已存在（含已撤回）: {op.evidence_id}",
                    {"evidence_id": op.evidence_id},
                )
            view[op.evidence_id] = {
                "a": op.stratum_a,
                "b": op.stratum_b,
                "relation": op.relation,
                "status": "active",
            }
            batch_new.add(op.evidence_id)
        elif isinstance(op, RetractOp):
            row = view.get(op.evidence_id)
            if row is None:
                raise ApiError(
                    409,
                    "ASSERTION_NOT_FOUND",
                    f"撤回失败：证据编号不存在: {op.evidence_id}",
                    {"evidence_id": op.evidence_id},
                )
            if row["status"] != "active":
                raise ApiError(
                    409,
                    "ASSERTION_NOT_FOUND",
                    f"撤回失败：证据 {op.evidence_id} 已处于撤回状态",
                    {"evidence_id": op.evidence_id},
                )
            row["status"] = "retracted"

    active = [
        Assertion(evidence_id=eid, a=row["a"], b=row["b"], relation=row["relation"])
        for eid, row in view.items()
        if row["status"] == "active"
    ]
    return current, active, batch_new


# ------------------------------------------------------------------ 最少额外撤回搜索
def _search_min_extra(
    by_id: Dict[str, Assertion],
    candidates: List[str],
    budget: int,
) -> Optional[List[str]]:
    """在预算内寻找使关系核验通过的、最少且字典序最小的额外撤回集合。

    按撤回数 k = 1..budget 逐层搜索（先保证数量最少）；同 k 内组合按证据编号
    字典序枚举（首位递增、序列递增），首个可行组合即字典序最小。

    剪枝：对当前状态核验得到的任一冲突，任何可行方案都必须命中其证据路径上
    至少一条证据，因此只需沿“出现在冲突路径上的候选”分支；
    不命中当前冲突路径的组合不可能消除该冲突，可安全跳过。
    """

    def validate_without(removed: Set[str]) -> List[dict]:
        return Derivation([a for eid, a in by_id.items() if eid not in removed]).validate()

    def dfs(removed: Set[str], available: List[str], depth_left: int) -> Optional[List[str]]:
        conflicts = validate_without(removed)
        if not conflicts:
            return sorted(removed)
        if depth_left == 0:
            return None
        path_ids = set(conflicts[0]["evidence_path"])
        useful = sorted(set(available).intersection(path_ids))
        if not useful:
            return None
        for eid in useful:
            nxt = [x for x in available if x > eid]
            found = dfs(removed | {eid}, nxt, depth_left - 1)
            if found is not None:
                return found
        return None

    for k in range(1, budget + 1):
        found = dfs(set(), candidates, k)
        if found is not None:
            return found
    return None


# ------------------------------------------------------------------ 预检入口
def preflight_batch(body: BatchPreflightIn) -> Dict[str, Any]:
    """只读预检：模拟本批并求解最少额外旧证据撤回集合。

    返回 result：
    - already_passes：本批按原语义核验已通过，额外撤回集合为空；
    - solution_found：预算内找到最少额外撤回集合，应用后核验通过；
    - internal_contradiction：本批保留的新断言自身矛盾，无法靠撤回旧证据修复；
    - budget_exceeded：预算内无方案。
    """
    with db_conn() as conn:
        with conn.cursor() as cur:
            # 只读快照事务；SET TRANSACTION 只对当前事务生效，提交/回滚后连接
            # （共享池）的后续事务恢复默认 READ WRITE/READ COMMITTED（同 changes）。
            cur.execute("SET TRANSACTION READ ONLY ISOLATION LEVEL REPEATABLE READ")
            current, active, batch_new = _simulate(cur, body)

    by_id = {a.evidence_id: a for a in active}
    conflicts = Derivation(active).validate()

    base_payload: Dict[str, Any] = {
        "expected_revision": body.expected_revision,
        "current_revision": current,
        "extra_retract_budget": body.extra_retract_budget,
    }

    if not conflicts:
        return {
            **base_payload,
            "result": "already_passes",
            "message": "本批按原批次语义核验已通过，无需额外撤回",
            "extra_retractions": [],
            "extra_retractions_count": 0,
            "validation_passed": True,
            "conflicts": [],
        }

    # 本批最终保留的新断言（仍生效的本批新增）自身核验
    new_only = [a for a in active if a.evidence_id in batch_new]
    if Derivation(new_only).validate():
        return {
            **base_payload,
            "result": "internal_contradiction",
            "message": "本批最终保留的新断言自身存在矛盾，无法靠撤回旧证据修复",
            "extra_retractions": [],
            "extra_retractions_count": 0,
            "validation_passed": False,
            "conflicts": conflicts,
        }

    # 额外撤回只能取自应用本批后仍生效的旧证据（天然排除本批新增 / 本批已撤回）
    candidates = sorted(eid for eid in by_id if eid not in batch_new)
    solution = _search_min_extra(by_id, candidates, body.extra_retract_budget)

    if solution is not None:
        return {
            **base_payload,
            "result": "solution_found",
            "message": "在预算内找到最少额外撤回集合，应用后关系核验通过",
            "extra_retractions": solution,
            "extra_retractions_count": len(solution),
            "validation_passed": True,
            "conflicts": [],
        }

    return {
        **base_payload,
        "result": "budget_exceeded",
        "message": f"预算 {body.extra_retract_budget} 条额外撤回内无法消除全部冲突",
        "extra_retractions": [],
        "extra_retractions_count": 0,
        "validation_passed": False,
        "conflicts": conflicts,
    }
