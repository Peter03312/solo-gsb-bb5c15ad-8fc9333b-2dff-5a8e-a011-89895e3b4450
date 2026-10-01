#!/usr/bin/env bash
# 断言批次提交预检（POST /batch-preflight）端到端示例
# 作用：正式提交批次前，只读求解“最少额外撤回哪些生效旧证据才能消除冲突”。
# 前提：服务已启动（docker compose up --build），监听 http://localhost:8000
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pp

echo; echo "== 1. 准备：登记地层，提交一条无冲突批次（修订号 0 -> 1） =="
for s in L1 L2 L3 L4; do
  curl -s -X POST "$BASE/strata" -H 'Content-Type: application/json' \
    -d "{\"stratum_id\": \"$s\", \"site\": \"甲遗址\"}" >/dev/null
done
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-001", "stratum_a": "L1", "stratum_b": "L2", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L2", "stratum_b": "L3", "relation": "earlier_than"}
  ]}' | pp
# L1 早于 L2 早于 L3。

echo; echo "== 2. 预检（预算 0）：新增 EV-009: L3 早于 L1 将成环，预算内无解 =="
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "extra_retract_budget": 0,
  "operations": [
    {"op": "assert", "evidence_id": "EV-009", "stratum_a": "L3", "stratum_b": "L1", "relation": "earlier_than"}
  ]}' | pp
# result=budget_exceeded，conflicts 附环路径 ["EV-001","EV-002","EV-009"]。

echo; echo "== 3. 预检（预算 1）：撤回 EV-001 或 EV-002 均可破环，数量并列取字典序最小 =="
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "extra_retract_budget": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-009", "stratum_a": "L3", "stratum_b": "L1", "relation": "earlier_than"}
  ]}' | pp
# result=solution_found，extra_retractions=["EV-001"]，validation_passed=true。

echo; echo "== 4. 预检只读：修订号仍为 1，EV-009 未落库 =="
curl -s "$BASE/revision"; echo
curl -s "$BASE/assertions?status=active" | pp

echo; echo "== 5. 采纳建议：把额外撤回 EV-001 并入正式批次提交（1 -> 2，成功） =="
curl -s -X POST "$BASE/batches" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-009", "stratum_a": "L3", "stratum_b": "L1", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-001"}
  ]}' | pp

echo; echo "== 6. 原批次已可通过：预检直接返回 already_passes（空集合） =="
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "extra_retract_budget": 2,
  "operations": [
    {"op": "assert", "evidence_id": "EV-010", "stratum_a": "L1", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 7. 本批新断言自身矛盾：撤回旧证据无法修复（internal_contradiction） =="
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "extra_retract_budget": 3,
  "operations": [
    {"op": "assert", "evidence_id": "EV-011", "stratum_a": "L3", "stratum_b": "L4", "relation": "earlier_than"},
    {"op": "assert", "evidence_id": "EV-012", "stratum_a": "L4", "stratum_b": "L3", "relation": "earlier_than"}
  ]}' | pp
# 冲突路径仅由本批新证据 EV-011/EV-012 构成，只能修改本批，不能靠撤旧修复。

echo; echo "== 8. 拒绝语义沿用正式批次：修订号过期（409 STALE_REVISION） =="
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 1,
  "extra_retract_budget": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-013", "stratum_a": "L1", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 9. 拒绝语义沿用：证据编号被占用 / 撤回目标无效 / 地层未登记 =="
echo "-- DUPLICATE_EVIDENCE（EV-002 已在历史中，含已撤回编号） --"
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "extra_retract_budget": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-002", "stratum_a": "L1", "stratum_b": "L4", "relation": "earlier_than"}
  ]}' | pp
echo "-- ASSERTION_NOT_FOUND（撤回不存在的证据） --"
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "extra_retract_budget": 1,
  "operations": [
    {"op": "retract", "evidence_id": "EV-404"}
  ]}' | pp
echo "-- INVALID_STRATUM（HTTP 400） --"
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "extra_retract_budget": 1,
  "operations": [
    {"op": "assert", "evidence_id": "EV-013", "stratum_a": "L1", "stratum_b": "LX", "relation": "earlier_than"}
  ]}' | pp

echo; echo "== 10. 额外撤回不会选到本批新增或本批已撤回的证据 =="
# 本批同时新增 EV-009 成环并撤回 EV-001：环已断，预检 already_passes；
# 即使仍有冲突，建议集合也只能取“本批应用后仍生效的旧证据”。
curl -s -X POST "$BASE/batch-preflight" -H 'Content-Type: application/json' -d '{
  "expected_revision": 2,
  "extra_retract_budget": 2,
  "operations": [
    {"op": "assert", "evidence_id": "EV-020", "stratum_a": "L3", "stratum_b": "L2", "relation": "earlier_than"},
    {"op": "retract", "evidence_id": "EV-002"}
  ]}' | pp
# 当前生效旧证据为 EV-002(L2<L3)、EV-009(L3<L1)（EV-001 已在修订 2 撤回）；
# 本批撤回 EV-002 后仅剩 EV-009(L3<L1) 与新增 EV-020(L3<L2)，不成环 -> already_passes。

echo; echo "预检示例执行完毕（所有预检均未产生修订、变化记录或断言）。"
