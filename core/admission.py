"""独立准入层（SM-20，2026-11-07）：普通隐患自动动作与模型升级提示的业务准入。

## 为什么

HIGH+ 自动建单与 model_hint 参与建单在现状是**不可观测的隐式直通**：
  - 没有任何"凭什么建这张单"的授权快照（brief 记录的是"现场是什么"，不是"谁授权的"）；
  - model_hint（模型正文里的升级关键词，**随机**）单独命中也会建应急单——随机成分进了授权链；
  - 终态回退（accept/reject→feedback）零校验：策略换了、规则停了、单子关了都能静默复活。

契约源：scratch/sm-loop/handoff/SM-20/decision.md §8.1（签名先于实现定死，红灯测试
tests/test_admission_shadow.py 逐字钉死）。机制参考 docs/对标笔记_dsh-superagent_2026-11-06.md
P0-A1/P0-A2（按本仓库主体与 SQLite 独立实现，不照搬 TS 原型）。

## 三档开关（SAFETYMIND_ADMISSION_MODE，逐请求读取）

  - off（默认）  ：现行为逐字节不变。adjudicate 恒 passthrough（不裁决不拦截）。
  - observe      ：旁路影子——现行一切业务写入照旧，唯一增量是 hazard_observations
                   裁决数据（行动指南 §2"现行工单继续正常写"；决策单 §7 冲突裁决 2）。
  - auto         ：获准试点。建单条件只认确定性依据（deterministic_basis），
                   model_hint 摘出（仅建单副作用层）；隐患闸门 auto 受主体白名单收窄。

默认安全论证：未设任何新 env 时 admission_mode()="off"、零副作用（唯一新增是本纯函数
模块的 import）；observe/auto 都是显式 opt-in。

## 不可变授权快照（mandate_json，schema_version=1）

  - mandate_snapshot() 构造；create_ticket(mandate=...) 与工单行同事务落库（零半提交）；
  - 人工改判/合并/幂等重放/终态回退都改写不了快照；补授权只追加 mandate_reauth 时间线
    事件（禁止伪造历史依据）；transition_ticket 在 accept/reject→feedback 前调
    mandate_revalidate 校验快照仍成立，不成立拒绝并留痕 mandate_reauth_required。

设计约束：纯函数 + 只读 env；任何异常由调用方 try/except 降级，绝不阻塞对话主链路。
"""
import logging
import os
import time

logger = logging.getLogger(__name__)

# ── 三档开关 ─────────────────────────────────────────────────────────────────
ADMISSION_MODES = ("off", "observe", "auto")

# 建单条件可认的确定性依据（§6）：urgency=CRITICAL 紧急度 / intent=显式转人工 /
# safety_net=紧急安全网。三者全部零 LLM；model_hint 是随机成分，绝不进授权链。
_DETERMINISTIC_BASES = frozenset({"urgency", "intent", "safety_net"})

# 快照 schema（§4.1）：schema_version=1；来源类型与强度白名单（revalidate 用）
_MANDATE_SCHEMA_VERSION = 1
_VALID_SOURCE_TYPES = frozenset({
    "hazard_gate_auto", "hazard_confirm", "emergency_urgency", "emergency_intent",
    "emergency_safety_net", "emergency_fastpath", "admin_manual", "manual_reauth",
})
_VALID_STRENGTHS = frozenset({"explicit", "inferred"})

# 快照有效期旋钮（SAFETYMIND_ADMISSION_MANDATE_MAX_AGE_H）的缺省（小时）
_DEFAULT_MANDATE_MAX_AGE_H = 168.0


def admission_mode() -> str:
    """逐请求读 SAFETYMIND_ADMISSION_MODE；未设/非法 → "off"（fail-safe 默认安全）。

    合法值见 ADMISSION_MODES；测试可进程内切换（逐请求读取，无缓存）。"""
    raw = (os.getenv("SAFETYMIND_ADMISSION_MODE", "") or "").strip().lower()
    if raw in ADMISSION_MODES:
        return raw
    if raw:
        logger.warning("SAFETYMIND_ADMISSION_MODE 非法值 %r（合法: %s），回退 off",
                       raw, "/".join(ADMISSION_MODES))
    return "off"


def policy_version() -> str:
    """准入策略版本（SAFETYMIND_ADMISSION_POLICY_VERSION，默认 "adm-1"）。
    规则/策略改版时递增——换版后旧快照续期裁决为 policy_changed。"""
    return (os.getenv("SAFETYMIND_ADMISSION_POLICY_VERSION", "") or "").strip() or "adm-1"


def mandate_max_age_hours() -> float:
    """快照有效期（小时）：SAFETYMIND_ADMISSION_MANDATE_MAX_AGE_H，默认 168h。
    非法/非正值回默认（fail-safe，不放大有效期）。"""
    raw = (os.getenv("SAFETYMIND_ADMISSION_MANDATE_MAX_AGE_H", "") or "").strip()
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
        logger.warning("SAFETYMIND_ADMISSION_MANDATE_MAX_AGE_H 非法值 %r，回退 %gh",
                       raw, _DEFAULT_MANDATE_MAX_AGE_H)
    return _DEFAULT_MANDATE_MAX_AGE_H


def auto_subjects() -> frozenset:
    """auto 档自动建单的主体白名单（SAFETYMIND_ADMISSION_AUTO_SUBJECTS，逗号分隔）。
    空 = 全体主体获批——把 mode 拨到 auto 本身就是显式批准动作，收窄必须显式配置。"""
    raw = (os.getenv("SAFETYMIND_ADMISSION_AUTO_SUBJECTS", "") or "").strip()
    if not raw:
        return frozenset()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def deterministic_basis(basis: str) -> bool:
    """建单条件谓词（§6）：basis ∈ {urgency, intent, safety_net} 为确定性依据。
    escalation_basis() 语义一个字不改（tests/test_escalation_basis.py 锁定）；
    hazard_gate_auto/hazard_gate_suggest/confirm 走闸门分支本就确定性，不经此谓词。"""
    return str(basis or "") in _DETERMINISTIC_BASES


# ── 影子/准入裁决 ────────────────────────────────────────────────────────────
def adjudicate(*, mode: str, basis: str = "", gate_decision=None,
               subject_id: str = "") -> dict:
    """准入裁决（action_proposed 的"会不会自动建单"真值）。

    返回 {"would_accept": bool, "decision": "accept"|"reject"|"observe"|"passthrough",
          "reason": str, "policy_version": str}。

      - off            → passthrough（不裁决不拦截；would_accept=False 无语义）；
      - observe        → 恒旁路（decision="observe"），would_accept 记"新规则下会不会建"：
                         urgency/intent/safety_net/hazard_gate_auto → 1；
                         model_hint → 0（reason=model_hint_random，差异列）；
      - auto           → accept/reject：model_hint 拒（model_hint_random）；
                         hazard_gate_auto 需 gate_decision 且主体在 AUTO_SUBJECTS
                         （空=全体）才 accept，否则 reject/subject_not_approved；
      - 其余（空/未知 basis、缺 gate_decision）→ fail-safe 拒绝（auto）/影子 0（observe）。
    """
    pv = policy_version()
    b = str(basis or "")
    if mode not in ADMISSION_MODES or mode == "off":
        return {"would_accept": False, "decision": "passthrough",
                "reason": "", "policy_version": pv}

    def _out(would: bool) -> dict:
        if mode == "observe":
            return {"would_accept": bool(would), "decision": "observe",
                    "reason": reason, "policy_version": pv}
        return {"would_accept": bool(would),
                "decision": "accept" if would else "reject",
                "reason": reason, "policy_version": pv}

    reason = b
    if b in _DETERMINISTIC_BASES:
        return _out(True)                      # 确定性依据：显式转人工/CRITICAL 零弱化
    if b == "model_hint":
        reason = "model_hint_random"           # 随机升级提示：不进授权链
        return _out(False)
    if b == "hazard_gate_auto" and isinstance(gate_decision, dict) and gate_decision:
        subjects = auto_subjects()
        if subjects and str(subject_id or "") not in subjects:
            reason = "subject_not_approved"    # 主体范围收窄：降级建议/复核
            return _out(False)
        return _out(True)
    # 未知 basis / 缺闸门裁决：拿不出确定性依据 → 拒（影子记 0 供对照）
    return _out(False)


# ── 不可变授权快照（§4.1）────────────────────────────────────────────────────
def mandate_snapshot(*, source_type: str, strength: str, subject_id: str, scopes,
                     rule_id: str, basis: str, proposal_id: str, evidence_ids) -> dict:
    """构造授权快照（schema_version=1）。字段↔行动指南清单：
    schema 版本/来源类型/主体与作用域（服务端解析结果）/规则 ID 与策略版本/
    proposal ID/决策时间/证据 ID。decided_ts 由本函数落（构造即决策时刻）。"""
    return {
        "schema_version": _MANDATE_SCHEMA_VERSION,
        "policy_version": policy_version(),
        "source_type": str(source_type or ""),
        "strength": str(strength or ""),
        "subject_id": str(subject_id or ""),
        "scopes": [str(s) for s in (scopes or [])],
        "rule_id": str(rule_id or ""),
        "basis": str(basis or ""),
        "proposal_id": str(proposal_id or ""),
        "decided_ts": time.time(),
        "evidence_ids": [str(e) for e in (evidence_ids or [])],
    }


def _default_subject_check(subject_id) -> bool:
    """撤权校验的默认实现（接 core/auth 主体目录）：目录未配置（本地形态）→ 无从撤权，
    不拒；已配置 → subject_id 仍在目录才放行。测试可注入桩覆盖。"""
    try:
        from core import auth as _auth
        users = _auth.load_users()
    except Exception:                            # 主体解析失败不放大为准入拒绝之外的影响
        return True
    if not users:
        return True
    ids = {str((v or {}).get("subject_id") or "") for v in users.values()}
    return str(subject_id or "") in ids


# 续期裁决原因集（§5.2）
REVALIDATE_REASONS = ("ok", "legacy", "bad_schema", "policy_changed",
                      "revoked", "cross_subject", "expired", "reauthorized")


def mandate_revalidate(mandate, *, current_policy: str,
                       operator_subject: str = "", operator_is_admin: bool = False,
                       subject_check=None, now=None) -> dict:
    """终态回退前的快照续期裁决（§5.2 原因集，先命中先返回）：

      legacy（快照缺失/空）→ bad_schema（版本/strength/source_type 不识别）
      → policy_changed（策略换版）→ revoked（主体被撤权）
      → cross_subject（操作主体≠快照主体且操作者非 admin）→ expired（超有效期）
      → ok。

    reauthorized 不在此裁决（时间线只有 store 层可见）：transition_ticket 在本裁决
    不通过时查未过期 mandate_reauth 事件自行放行（§5.3 补授权路径）。"""
    now = time.time() if now is None else float(now)

    def _no(reason: str) -> dict:
        return {"ok": False, "reason": reason}

    if not isinstance(mandate, dict) or not mandate:
        return _no("legacy")
    if mandate.get("schema_version") != _MANDATE_SCHEMA_VERSION:
        return _no("bad_schema")
    if str(mandate.get("strength") or "") not in _VALID_STRENGTHS:
        return _no("bad_schema")
    if str(mandate.get("source_type") or "") not in _VALID_SOURCE_TYPES:
        return _no("bad_schema")
    if (mandate.get("policy_version") or "") != str(current_policy or ""):
        return _no("policy_changed")
    check = _default_subject_check if subject_check is None else subject_check
    try:
        if not check(mandate.get("subject_id")):
            return _no("revoked")
    except Exception:                            # 校验桩异常按撤权处理（fail-safe）
        return _no("revoked")
    op = str(operator_subject or "")
    sub = str(mandate.get("subject_id") or "")
    if op and sub and op != sub and not operator_is_admin:
        return _no("cross_subject")
    try:
        decided = float(mandate.get("decided_ts") or 0)
    except (TypeError, ValueError):
        decided = 0.0
    if decided <= 0 or now - decided > mandate_max_age_hours() * 3600:
        return _no("expired")
    return {"ok": True, "reason": "ok"}
