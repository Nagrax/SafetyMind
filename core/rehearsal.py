# -*- coding: utf-8 -*-
"""受限的后台预演与待复核提案（SM-41）。

问题（任务卡 SM-41）：SM-40 把"逾期整改 / SOP 漂移"等事实稳定产出（fact_code +
责任范围 + 建议文本），但建议到落地之间**没有预演**——值班人员要自己从零准备审核
材料（核对现状/依据/责任方/文本，准备时间无对照），且直接操作生产写入口（建单/发
通知/发布 SOP），没有预算护栏。

本模块补齐**受限预演机制**：限定一个安全场景，在脱敏副本上**预计算**两类提案——
  1. overdue_rectification_notice  逾期整改提示（事实源：逾期工单）
  2. sop_reference_replacement     过期 SOP 引用替换（事实源：待审批 SOP 候选）
产出**待复核提案**（pending），由人工审批（approve/reject）后才可能进入真实执行——
预演自身**绝不**触碰生产写入口（建单/发通知/发布全是"建议动作"文本，不是执行）。

四条护栏（任务卡 scope"限制并发、时间、请求预算、TTL 和提案数"，decision.md §2.3）：
  - 并发 = 1：同场景恰一个 running run（AuditStore 锁内判定，跨实例/跨进程成立）；
  - 墙钟：run.deadline_ts（max_wall_s），每步检查，超时如实标 aborted/budget_wall；
  - 请求预算：max_steps（每条提案 = 1 步），超限 aborted/budget_steps；
  - 提案数：max_proposals 封顶，超出计 budget_proposals；TTL 到期 reap 置 expired。

只读白名单（artifacts #1，decision.md §2.2）：预演执行期唯一允许的数据读取入口
REHEARSAL_READONLY_TOOLS——写类工具（create_ticket / enqueue_notification / 发布/
重放）请求一律 RehearsalDenied("whitelist")，不执行。治理模式与 core/tool_execution.py
ToolExecutor 同构（白名单 + 双预算 + 拒绝计数），但不调 LLM：预计算是确定性规则，
LLM 参与属真实模型评测（deferred）。

审批重验四项（任务卡 scope，decision.md §2.4）：approve 时逐项校验
  主体（approver 非空且 ≠ 生成者；场景 subject 未漂移）
  策略（提案 policy_version == 场景当前 policy_version）
  修订（payload 重算 sha256 == 库内 payload_sha256，防篡改 fail-closed）
  预算+TTL（run 必须 done（封印 aborted/cancelled/interrupted）；提案未过期）

崩溃可解释（任务卡 A2）：run 状态落库（rehearsal_runs），心跳超 lease_s 被 reap 置
interrupted——重启后新实例 reap 即见，绝不静默复写；已落库提案行保留，计数以行为准。

生产调用点为零（deny 面，decision.md §4）：本模块不被 api/main.py 引用、无后台循环、
无端点——真实接入生产调度需业务批准（deny-decision.md U1-U3）。库组件无自动启动
路径 = 天然 off，对现有行为零影响。

持久化：core/audit_store.py rehearsal_runs / rehearsal_proposals 两表（自有两表，
业务表零接触；ux_rehearsal_dedup 唯一索引 = 数据库级防重，并行重复派工恰一次）。
"""
import hashlib
import json
import time
import uuid
from typing import Any, Dict, List, Optional

# 预演执行期唯一允许的数据读取入口（只读白名单，decision.md §2.2 冻结；
# 全部为既有只读面：AuditStore.list_overdue_tickets / Watchdog SOP 漂移 /
# SkillEvolution.list_candidates）。变动须同步 decision.md 与测试冻结常量。
REHEARSAL_READONLY_TOOLS = (
    "list_overdue_tickets",
    "sop_drift_facts",
    "skill_pending_candidates",
)

# 预演对象（任务卡 scope 两类）→ 白名单事实源
_KIND_SOURCES = {
    "overdue_rectification_notice": "list_overdue_tickets",
    "sop_reference_replacement": "skill_pending_candidates",
}

# 默认护栏（decision.md §2.3 冻结）
DEFAULT_MAX_WALL_S = 120.0
DEFAULT_MAX_STEPS = 8
DEFAULT_MAX_PROPOSALS = 5
DEFAULT_TTL_S = 86400.0
DEFAULT_LEASE_S = 300.0

# A3 评测预算（离线评测专用，decision.md §2.7；与生产护栏参数解耦——评测经子引擎
# 实际生效本组预算，而不是只写进报告）
EVAL_MAX_PROPOSALS = 8
EVAL_MAX_STEPS = 32
EVAL_MAX_WALL_S = 60.0
# A3 准备时间代理模型（预注册）：对照组每条事实人工核对 4 项（现状/依据/责任方/文本）
# + 全局 3 项（范围/口径/汇总）；实验组每条提案人工终判 1 项 + 全局 3 项——
# 机器可判项由预演预先完成，人工终判项如实保留（不为 0，反"自动化全包"）。
PREPARE_STEPS_PER_FACT = 4
PREPARE_STEPS_GLOBAL = 3
REVIEW_STEPS_PER_PROPOSAL = 1
REVIEW_STEPS_GLOBAL = 3
# A3 评测内驳回分类（预注册冻结表）：每个 should 对的首条提案由"审核者"按轮转分类
# 真实驳回（库内 rejected 状态翻转，报告 rejection_reasons 逐键来自这些真实行）——
# 驳回是评测流程的一部分，不是演示（2026-11-08 独立复核 P3 整改）。
REJECTION_TAXONOMY = (
    "与现场规程不一致",
    "重复建议（已有同类待审提案）",
    "责任方需人工确认后另议",
)
EVAL_REJECT_PER_SHOULD_PAIR = 1


class RehearsalDenied(Exception):
    """预演护栏拒绝（code 见各抛点；测试按 code 断言）。"""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def _sha(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _clip(text: Any, limit: int = 40) -> str:
    return " ".join(str(text or "").split())[:limit]


class RehearsalEngine:
    """受限预演执行器：start_run（登记并发位）→ execute_run（确定性执行）→
    cancel_run / reap / approve / reject。构造参数见 decision.md §2.3。
    audit：AuditStore（唯一持久化面）；scenarios：{scenario_id: 场景注册表}，
    场景结构 {"subject_id","policy_version","operators","sources","kinds"}，
    sources 为只读事实源（注入函数，测试用脱敏副本；生产接入时绑定真实只读面）。
    """

    def __init__(self, audit: Any, scenarios: Dict[str, Dict[str, Any]], *,
                 clock=time.time, max_wall_s: float = DEFAULT_MAX_WALL_S,
                 max_steps: int = DEFAULT_MAX_STEPS,
                 max_proposals: int = DEFAULT_MAX_PROPOSALS,
                 proposal_ttl_s: float = DEFAULT_TTL_S,
                 lease_s: float = DEFAULT_LEASE_S):
        self._audit = audit
        self._scen = {str(k): dict(v) for k, v in (scenarios or {}).items()}
        self._clock = clock or time.time
        self._max_wall_s = float(max_wall_s)
        self._max_steps = max(1, int(max_steps))
        self._max_proposals = max(1, int(max_proposals))
        self._ttl_s = float(proposal_ttl_s)
        self._lease_s = float(lease_s)
        self._current: Optional[Dict[str, Any]] = None   # 当前 run 上下文（取消旗标/场景绑定）

    # ── 只读白名单执行点（A1"诱导工具写入被拦截"的实现点）────────────────────
    def _invoke_tool(self, name: str, params: Optional[Dict[str, Any]] = None):
        """经白名单调用数据读取入口；写类/未注册请求一律拒绝且不执行。"""
        if name not in REHEARSAL_READONLY_TOOLS:
            raise RehearsalDenied("whitelist",
                                  f"工具 {name} 不在预演只读白名单内，已拒绝执行")
        scenario_id = (self._current or {}).get("scenario_id")
        if scenario_id is None:
            if len(self._scen) == 1:
                scenario_id = next(iter(self._scen))
            else:
                raise RehearsalDenied("no_source", "无当前预演上下文，无法定位事实源")
        sources = dict(self._scen[scenario_id].get("sources") or {})
        fn = sources.get(name)
        if fn is None:
            raise RehearsalDenied("no_source", f"场景 {scenario_id} 未注册事实源 {name}")
        return fn(**(params or {}))

    # ── run 生命周期 ──────────────────────────────────────────────────────────
    def start_run(self, scenario_id: str, actor: str) -> Dict[str, Any]:
        """登记 running（并发位在此判定）；返回 run 行。后台化（异步执行）属真实
        接入面，本组件只提供确定性 execute_run。"""
        scen = self._scen.get(str(scenario_id or ""))
        if scen is None:
            raise RehearsalDenied("unknown_scenario", f"场景未注册: {scenario_id}")
        if str(actor or "") not in (scen.get("operators") or ()):
            raise RehearsalDenied("authz", f"operator {actor!r} 未注册于场景 {scenario_id}")
        run_id = "RH-" + uuid.uuid4().hex[:12]
        now = self._clock()
        res = self._audit.rehearsal_run_start({
            "run_id": run_id, "scenario_id": str(scenario_id),
            "subject_id": str(scen.get("subject_id") or ""),
            "actor": str(actor or ""), "policy_version": str(scen.get("policy_version") or ""),
            "max_steps": self._max_steps, "max_proposals": self._max_proposals,
            "deadline_ts": now + self._max_wall_s,
            "started_ts": now, "heartbeat_ts": now,
        })
        if not res.get("ok"):
            raise RehearsalDenied("concurrency",
                                  f"场景 {scenario_id} 已有运行中预演 {res.get('conflict_run_id')}"
                                  "（并发=1）")
        self._current = {"run_id": run_id, "scenario_id": str(scenario_id),
                         "actor": str(actor or "")}
        return self._audit.rehearsal_run_get(run_id)

    def execute_run(self, run_id: str) -> Dict[str, Any]:
        """确定性执行预演（同步）：读事实 → 生成提案（预算/取消旗标每步生效）→ 终态。"""
        run = self._audit.rehearsal_run_get(run_id)
        if run is None:
            raise RehearsalDenied("unknown_run", f"run 不存在: {run_id}")
        if run["status"] != "running":
            raise RehearsalDenied("invalid_state", f"run {run_id} 状态为 {run['status']}")
        scen = self._scen[run["scenario_id"]]
        self._current = {"run_id": run_id, "scenario_id": run["scenario_id"],
                         "actor": run["actor"]}
        steps = 0
        created = 0
        duplicated = 0
        end_reason = ""
        status = "done"
        kinds = [k for k in (scen.get("kinds") or ()) if k in _KIND_SOURCES]
        for kind in kinds:
            facts = self._invoke_tool(_KIND_SOURCES[kind]) or []
            for fact in facts:
                cur = self._audit.rehearsal_run_get(run_id)
                if cur["status"] != "running":           # 取消旗标（cancel_run 已置终态）
                    return cur
                now = self._clock()
                if now >= float(cur["deadline_ts"]):
                    status, end_reason = "aborted", "budget_wall"
                    break
                if steps >= self._max_steps:
                    status, end_reason = "aborted", "budget_steps"
                    break
                if created >= self._max_proposals:
                    status, end_reason = "aborted", "budget_proposals"
                    break
                draft = self._draft(kind, fact, run["scenario_id"])
                if draft is not None:
                    out = self.propose(draft)
                    if out[0] == "created":
                        created += 1
                    else:
                        duplicated += 1
                    steps += 1
                    self._audit.rehearsal_run_heartbeat(run_id, self._clock())
            if status != "done":
                break
        if status == "done":
            if created == 0 and duplicated > 0:
                end_reason = "all_duplicates"            # 重试去重：全部命中既有提案
            elif created == 0:
                end_reason = "no_facts"                  # 无符合事实（如实记录，非失败）
        self._audit.rehearsal_run_finish(
            run_id, status=status, end_reason=end_reason, ended_ts=self._clock(),
            steps_used=steps, proposals_created=created, proposals_duplicated=duplicated)
        self._current = None
        return self._audit.rehearsal_run_get(run_id)

    def cancel_run(self, run_id: str, actor: str) -> Dict[str, Any]:
        """取消运行中 run（A2）；已结束 run 拒绝取消（invalid_state）。"""
        run = self._audit.rehearsal_run_get(run_id)
        if run is None:
            raise RehearsalDenied("unknown_run", f"run 不存在: {run_id}")
        if run["status"] != "running":
            raise RehearsalDenied("invalid_state",
                                  f"run {run_id} 已为 {run['status']}，不可取消")
        self._audit.rehearsal_run_finish(
            run_id, status="cancelled", end_reason=f"cancelled by {actor}",
            ended_ts=self._clock(), steps_used=int(run["steps_used"]),
            proposals_created=int(run["proposals_created"]),
            proposals_duplicated=int(run["proposals_duplicated"]))
        if (self._current or {}).get("run_id") == run_id:
            self._current = None
        return self._audit.rehearsal_run_get(run_id)

    def reap(self) -> Dict[str, int]:
        """过期回收（A2/rollback）：过期提案置 expired（行保留只读可查）；
        心跳超时 run 置 interrupted（崩溃可解释状态）。"""
        return self._audit.rehearsal_reap(self._clock(), self._lease_s)

    # ── 提案生成（脱敏：payload 只含白名单字段，敏感/诱导原文绝不带入）─────────
    def _draft(self, kind: str, fact: Dict[str, Any], scenario_id: str) -> Optional[Dict[str, Any]]:
        now = self._clock()
        if kind == "overdue_rectification_notice":
            tid = str(fact.get("id") or "")
            if not tid:
                return None
            due = fact.get("due_ts")
            hours = max(0.0, (now - float(due)) / 3600.0) if due else None
            payload = {
                "ticket_id": tid,
                "title": _clip(fact.get("title")),
                "responsible_dept": _clip(fact.get("responsible_dept"), 20),
                "hours_overdue": round(hours, 1) if hours is not None else None,
                "suggested_action": "复核后由人工派发整改提示（预演不发送）",
            }
            ref = f"ticket:{tid}"
        elif kind == "sop_reference_replacement":
            cid = str(fact.get("candidate_id") or "")
            if not cid:
                return None
            payload = {
                "candidate_id": cid,
                "title": _clip(fact.get("title")),
                "current_ref": _clip(fact.get("current_ref"), 60),
                "proposed_ref": _clip(fact.get("proposed_ref"), 60),
                "suggested_action": "复核后由人工走 SOP 发布流程（预演不发布）",
            }
            ref = f"candidate:{cid}"
        else:
            return None
        payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return {
            "id": "RP-" + uuid.uuid4().hex[:12],
            "run_id": (self._current or {}).get("run_id") or "",
            "scenario_id": scenario_id,
            "kind": kind,
            "dedup_key": _sha(f"{scenario_id}|{kind}|{ref}")[:16],
            "subject_id": str((self._scen.get(scenario_id) or {}).get("subject_id") or ""),
            "source_ref": ref,
            "payload": payload_text,
            "payload_sha256": _sha(payload_text),
            "policy_version": str((self._scen.get(scenario_id) or {}).get("policy_version") or ""),
            "created_ts": now,
            "expires_ts": now + self._ttl_s,
        }

    def propose(self, row: Dict[str, Any]) -> tuple:
        """提案落库（dedup 锁内判 + 唯一索引兜底）：('created', id) | ('duplicate', id)。"""
        return self._audit.rehearsal_propose(row)

    # ── 审批（重验四项，decision.md §2.4）────────────────────────────────────
    def approve(self, proposal_id: str, approver: str) -> Dict[str, Any]:
        return self._decide(proposal_id, status="approved", actor=approver, reason="")

    def reject(self, proposal_id: str, reason: str, actor: str) -> Dict[str, Any]:
        return self._decide(proposal_id, status="rejected", actor=actor, reason=str(reason or ""))

    def _decide(self, proposal_id: str, *, status: str, actor: str, reason: str) -> Dict[str, Any]:
        row = self._audit.rehearsal_proposals(status="pending")
        row = next((r for r in row if r["id"] == str(proposal_id)), None)
        if row is None:
            # 非 pending：区分 expired（TTL 回收）与已决（approved/rejected）
            all_rows = self._audit.rehearsal_proposals()
            old = next((r for r in all_rows if r["id"] == str(proposal_id)), None)
            if old is None:
                raise RehearsalDenied("unknown_proposal", f"提案不存在: {proposal_id}")
            raise RehearsalDenied("expired" if old["status"] == "expired" else "decided",
                                  f"提案 {proposal_id} 状态为 {old['status']}")
        if not str(actor or "").strip():
            raise RehearsalDenied("authz", "审批/驳回主体为空")
        run = self._audit.rehearsal_run_get(row["run_id"])
        if run is None:
            raise RehearsalDenied("authz", f"来源 run {row['run_id']} 不可考，拒绝决定")
        if status == "approved" and str(actor) == str(run.get("actor") or ""):
            raise RehearsalDenied("authz", "生成者不得自批（生成者 != 审批者）")
        if status == "approved":
            scen = self._scen.get(row["scenario_id"])
            if scen is None:
                raise RehearsalDenied("subject", f"场景 {row['scenario_id']} 未注册")
            if str(row["subject_id"]) != str(scen.get("subject_id") or ""):
                raise RehearsalDenied("subject", "场景主体漂移（subject drift），拒绝审批")
            if str(row["policy_version"]) != str(scen.get("policy_version") or ""):
                raise RehearsalDenied("policy", "策略版本不匹配（提案产生于旧策略），拒绝审批")
            if str(run.get("status") or "") != "done":
                raise RehearsalDenied("sealed",
                                      f"来源 run 状态为 {run['status']}（非 done），提案已封印")
            if _sha(str(row["payload"])) != str(row["payload_sha256"]):
                raise RehearsalDenied("tampered", "payload 与登记 sha256 不符（疑似篡改），拒绝审批")
            if self._clock() >= float(row["expires_ts"]):
                raise RehearsalDenied("expired", "提案已过 TTL（未及 reap），拒绝审批")
        res = self._audit.rehearsal_proposal_decide(
            proposal_id, status=status, decided_by=str(actor), reason=reason,
            decided_ts=self._clock())
        if res is None:                                  # 并行决定竞态：后到者拒绝
            raise RehearsalDenied("decided", f"提案 {proposal_id} 已被决定（并行竞态）")
        return res

    # ── 只读视图 ─────────────────────────────────────────────────────────────
    def runs(self, scenario_id: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._audit.rehearsal_runs(scenario_id)

    def proposals(self, scenario_id: Optional[str] = None) -> List[Dict[str, Any]]:
        out = []
        for r in self._audit.rehearsal_proposals(scenario_id):
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except (TypeError, ValueError):
                pass
            out.append(d)
        return out

    # ── A3 收益/成本评测（G5，离线确定性，decision.md §2.7）──────────────────
    def evaluate_pairs(self, pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """20 对固定场景评测：对照组（无预演，审核者从零准备）vs 实验组（复核预演提案）。

        准备时间用**确定性代理**（步骤计数，非真人秒表——真实审核者收益验证属
        真实试点，deferred，decision.md §4 D2）。评测预算独立冻结（EVAL_*）——经
        子引擎实际生效（不随构造护栏漂移），评测全程走同一套护栏与白名单，红线
        零退化由调用方断言（双零计数）。
        """
        now = self._clock()
        rows = []
        for p in (pairs or []):
            pid = str(p.get("pair_id") or "")
            n_overdue = int(p.get("overdue") or 0)
            n_sop = int(p.get("sop") or 0)
            label = str(p.get("label") or "should")
            sid = f"eval-{pid}"
            self._scen[sid] = {
                "subject_id": f"eval-subj-{pid}",
                "policy_version": "pv-eval",
                "operators": ("eval-ops",),
                "sources": {
                    "list_overdue_tickets": (lambda n=n_overdue, q=pid: [
                        {"id": 1000 + i, "title": f"评测整改-{q}-{i}",
                         "responsible_dept": "评测部门", "due_ts": now - 3600 * (i + 1),
                         "status": "discuss"} for i in range(n)]),
                    "skill_pending_candidates": (lambda n=n_sop, q=pid: [
                        {"candidate_id": f"ec-{q}-{i}", "title": f"评测SOP-{q}-{i}",
                         "current_ref": "SOP-EVAL-2023 rev1（已过期）",
                         "proposed_ref": "SOP-EVAL-2026 rev1"} for i in range(n)]),
                },
                "kinds": ("overdue_rectification_notice", "sop_reference_replacement"),
            }
            # 对照组：无预演 → 审核者逐项核对（代理模型见模块头 PREPARE_STEPS_*）
            n_facts = n_overdue + n_sop
            prepare = PREPARE_STEPS_PER_FACT * n_facts + PREPARE_STEPS_GLOBAL
            sub = RehearsalEngine(                       # 循环内构造：拷贝含本对场景
                self._audit, self._scen, clock=self._clock,
                max_wall_s=EVAL_MAX_WALL_S, max_steps=EVAL_MAX_STEPS,
                max_proposals=EVAL_MAX_PROPOSALS,
                proposal_ttl_s=self._ttl_s, lease_s=self._lease_s)
            run = sub.start_run(sid, "eval-ops")
            run = sub.execute_run(run["run_id"])
            created = int(run["proposals_created"])
            rejections = 0
            if label == "should" and created >= EVAL_REJECT_PER_SHOULD_PAIR:
                # 审核者驳回（真实库内状态翻转）：首条提案按冻结分类表轮转驳回，
                # 其余提案留待审批——驳回的提案人工终判项仍计入 review（语义不变）
                pend = [p for p in self.proposals(sid)
                        if p["status"] == "pending" and p["run_id"] == run["run_id"]]
                if pend:
                    reason = REJECTION_TAXONOMY[len(rows) % len(REJECTION_TAXONOMY)]
                    self.reject(pend[0]["id"], reason, "eval-approver")
                    rejections = 1
            if label == "none":
                # none 对：本不该有提案；无收益如实计（review == prepare，不虚增下降率）
                review = prepare
            else:
                review = REVIEW_STEPS_PER_PROPOSAL * created + REVIEW_STEPS_GLOBAL
            reduction = (1.0 - review / prepare) if prepare > 0 else 0.0
            rows.append({
                "pair_id": pid, "label": label, "overdue": n_overdue, "sop": n_sop,
                "prepare_steps": prepare, "review_steps": review,
                "reduction": round(reduction, 4),
                "proposals_created": created, "rejections": rejections,
                "cost_steps": int(run["steps_used"]),
                "run_end_reason": run["end_reason"],
            })
        reductions = [r["reduction"] for r in rows]
        rejected = self._audit.rehearsal_proposals(status="rejected")
        reasons: Dict[str, int] = {}
        for r in rejected:
            key = str(r.get("decision_reason") or "(未注明)")
            reasons[key] = reasons.get(key, 0) + 1
        return {
            "pairs": rows,
            "median_reduction": round(statistics_median(reductions), 4),
            "false_suggestions": sum(1 for r in rows
                                     if r["label"] == "none" and r["proposals_created"] > 0),
            "rejection_reasons": reasons,
            "proxy": "deterministic-steps（确定性代理，非真人秒表；真实试点另测）",
            "eval_budget": {"max_proposals": EVAL_MAX_PROPOSALS,
                            "max_steps": EVAL_MAX_STEPS, "max_wall_s": EVAL_MAX_WALL_S},
            "generated_ts": self._clock(),
        }


def statistics_median(values: List[float]) -> float:
    """中位数（stdlib statistics.median 的确定性内联；空集返回 0.0）。"""
    if not values:
        return 0.0
    xs = sorted(values)
    n = len(xs)
    mid = n // 2
    return float(xs[mid]) if n % 2 else (xs[mid - 1] + xs[mid]) / 2.0
