# -*- coding: utf-8 -*-
"""SM-30 回归：SOP 修订登记、双人审核、发布门禁、发布指针/回退、请求级版本冻结与回放。

覆盖任务卡 SM-30 四条验收：
  A1 无证据、自审、无权、旧 base、审核后被篡改、版本不匹配都拒绝发布；
  A2 并发热加载同一请求仅使用一个版本快照，跨进程读到批准版本，缓存键含版本；
  A3 发布/回退重启可复现，旧请求能定位当时实际注入片段摘要及正文版本，无版本标 unknown；
  A4 只读调试字段权限独立；紧急模板和调用失败路径回归通过。

真实模型效果验证（live）不跑（纪律：真实凭据/在线模型 deferred）；发布门禁对"证据"的
检查行为用离线构造的 validation 记录验证（只验门禁逻辑，不伪造在线评测结论）。

用法: python tests/test_skill_publication.py
环境：skills 与数据目录均指向临时目录，绝不污染真实 skills/ 与 data/。
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["SAFETYMIND_SKILL_EVOLUTION"] = "0"
os.environ["API_HOST"] = "127.0.0.1"
os.environ["AUDIT_ENABLED"] = "0"
os.environ["SAFETYMIND_BGE"] = "0"
os.environ["SAFETYMIND_CRITICAL_TEMPLATE"] = "1"
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"   # 不可达：覆盖调用失败路径
os.environ["PREFERENCE_DB_PATH"] = os.path.join(
    tempfile.mkdtemp(prefix="sk_pub_pref_"), "preference_cards.db")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


SKILL_MD = ("---\nname: 动火作业票办理引导\nkeywords: 动火,办票\nagents: compliance\nenabled: true\n---\n\n"
            "逐步引导用户办理动火作业票：判定等级、气体分析、审批权限、有效期。\n")


def make_env():
    tmp = Path(tempfile.mkdtemp(prefix="sk_pub_"))
    skills = tmp / "skills"
    (skills / "hot_work").mkdir(parents=True)
    (skills / "hot_work" / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
    os.environ["SAFETYMIND_SKILLS_DIR"] = str(skills)
    os.environ["SAFETYMIND_SKILL_EVO_DIR"] = str(tmp / "data")
    os.environ["SAFETYMIND_PUB_DIR"] = str(tmp / "data")
    return tmp, skills


def validation_improved():
    """离线构造的效果验证证据（不调真实模型；门禁只检查证据存在性与判值）。"""
    return {"ok": True, "verdict": "improved", "delta": 0.2, "margin": 0.1,
            "model": "offline-probe",
            "note": "离线假模型证据：仅验证门禁行为，live 效果验证 deferred"}


def propose(evo, ledger, cand_fields, base_rev, validated="improved", with_author=True):
    """候选 → 固定修订。validated: "improved" | "regressed" | None（无验证证据）。

    预置唯一候选 id（append_candidate 的时间戳 id 在同毫秒内会碰撞）。
    with_author=False 构造作者不可指认的修订（no_provenance 用例）。
    """
    import uuid
    cand = evo.append_candidate(dict(cand_fields, id=f"cand-{uuid.uuid4().hex[:10]}"))
    if validated == "improved":
        evo.approve(cand["id"], validation=validation_improved())
    elif validated == "regressed":
        evo.approve(cand["id"], validation={"ok": True, "verdict": "regressed", "delta": -0.3,
                                            "margin": 0.1, "model": "offline-probe"})
    else:
        evo.approve(cand["id"])   # 无证据 approve：validation_verdict=unvalidated（既有语义）
    rec = next(c for c in evo.list_candidates() if c["id"] == cand["id"])
    if with_author:
        rec["author"], rec["author_subject"] = "u-author", "u-author"
    else:
        rec["author"] = rec["author_subject"] = ""
    return cand["id"], ledger.propose_from_candidate(rec, base_revision=base_rev)


def test_core_gate(tmp, skills):
    """A1：发布门禁多种拒绝 + 放行；A2/A3 的 core 语义。"""
    from core.skill_loader import SkillPublicationLedger, SkillManager
    from core.skill_evolution import SkillEvolution

    evo = SkillEvolution(skills_root=str(skills))
    mgr = SkillManager(root_dir=str(skills), max_prompt_chars=5000)
    mgr.load()
    ledger = SkillPublicationLedger(skills_root=str(skills), data_dir=str(tmp / "data"))

    # 起始版本登记（幂等）
    base = ledger.register_baseline()
    record("A1.0a 登记起始版本", len(base) == 1 and base[0]["revision_id"].startswith("base-"), str(base)[:120])
    base2 = ledger.register_baseline()
    record("A1.0b 起始版本登记幂等", [r["revision_id"] for r in base2] == [r["revision_id"] for r in base])
    base_rev = base[0]["revision_id"]

    fields = {"title": "动火审批权限修正", "instruction": "二级动火由设备科审批",
              "when_to_use": "用户办理动火作业票时", "keywords": ["动火", "办票"],
              "action": "merge", "target_skill": "动火作业票办理引导",
              "target_path": str(skills / "hot_work" / "SKILL.md"),
              "provenance": "feedback:t-1"}
    _, rev = propose(evo, ledger, fields, base_rev, validated="improved")
    record("A1.0c 修订 id 由内容摘要派生",
           rev.get("content_sha256") and rev["revision_id"] == "rev-" + rev["content_sha256"][:12],
           str({k: rev.get(k) for k in ("revision_id", "content_sha256")}))
    rid = rev["revision_id"]

    # ① 无证据拒绝（unvalidated / regressed 均不放行）——各用例 instruction 唯一，
    # 否则全集快照相同 → 修订 id 相同 → 幂等复用第一条（同内容=同修订是正确语义）
    _, rev_ne = propose(evo, ledger, dict(fields, title="无证据候选",
                                          instruction="二级动火由设备科审批并留存台账"),
                        base_rev, validated=None)
    r = ledger.publish_revision(rev_ne["revision_id"], base_revision=base_rev,
                                approver="u-pub", approver_subject="u-pub", author="u-author")
    record("A1.1a 无证据（unvalidated）拒绝发布", not r.get("ok") and r.get("code") == "no_evidence", str(r))
    _, rev_rg = propose(evo, ledger, dict(fields, title="退步候选",
                                          instruction="二级动火由设备科审批并公示三日"),
                        base_rev, validated="regressed")
    r = ledger.publish_revision(rev_rg["revision_id"], base_revision=base_rev,
                                approver="u-pub", approver_subject="u-pub", author="u-author")
    record("A1.1b 验证退步拒绝发布", not r.get("ok") and r.get("code") in ("no_evidence", "evidence_regressed"), str(r))

    # 无审批直接发布拒绝
    r = ledger.publish_revision(rid, base_revision=base_rev, approver="u-pub",
                                approver_subject="u-pub", author="u-author")
    record("A1.2 无审批拒绝发布", not r.get("ok") and r.get("code") == "not_approved", str(r))

    # ② 自审拒绝（approve 阶段）
    r = ledger.approve_revision(rid, approver="u-author", approver_subject="u-author", author="u-author")
    record("A1.3a 自审拒绝审批", not r.get("ok") and r.get("code") == "self_approval", str(r))

    # ③ 无权拒绝（审批主体无 admin/publisher 角色）
    r = ledger.approve_revision(rid, approver="u-rand", approver_subject="u-rand",
                                author="u-author", approver_roles={"reporter"})
    record("A1.3b 无权主体拒绝审批", not r.get("ok") and r.get("code") == "not_authorized", str(r))

    # 正常审批（有权、非作者）
    r = ledger.approve_revision(rid, approver="u-pub", approver_subject="u-pub",
                                author="u-author", approver_roles={"admin"})
    record("A1.4 合规审批通过并绑定内容摘要",
           r.get("ok") and r["approval"]["content_sha256"] == rev["content_sha256"], str(r))

    # ④ 审核后被篡改：直接改磁盘修订记录正文（模拟审批后存储被改）
    revs_path = ledger.revisions_path
    rows = [json.loads(l) for l in revs_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = [dict(x, content_sha256="f" * 64) if x["revision_id"] == rid else x for x in rows]
    revs_path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    r = ledger.publish_revision(rid, base_revision=base_rev, approver="u-pub",
                                approver_subject="u-pub", author="u-author")
    record("A1.5 审核后被篡改拒绝发布", not r.get("ok") and r.get("code") == "tampered", str(r))
    # 恢复（重算正确 sha 写回），后续用例继续
    rows = [dict(x, content_sha256=rev["content_sha256"]) if x["revision_id"] == rid else x for x in rows]
    revs_path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")

    # ⑤ 版本不匹配拒绝
    r = ledger.publish_revision("rev-nonexistent", base_revision=base_rev, approver="u-pub",
                                approver_subject="u-pub", author="u-author")
    record("A1.6a 不存在修订拒绝发布", not r.get("ok") and r.get("code") == "revision_mismatch", str(r))

    # A1.6c：修订存在但 id 与内容摘要派生不符 → revision_mismatch。
    # 构造"内容与审批签名同时被篡改"（sha 与签名同步改，tampered 不触发），
    # 验证 id 派生校验（rev-<sha12>）作为纵深防御独立兜底——复核 §六.3 覆盖缺口。
    rows = [json.loads(l) for l in revs_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    forged = "e" * 64
    for x in rows:
        if x["revision_id"] == rid:
            x["content_sha256"] = forged
            for a in x.get("approvals") or []:
                a["content_sha256"] = forged   # 同步伪造签名：绕过 tampered 检查
    revs_path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")
    r = ledger.publish_revision(rid, base_revision=base_rev, approver="u-pub",
                                approver_subject="u-pub", author="u-author")
    record("A1.6c id 派生不符（内容+签名同步篡改）拒绝发布",
           not r.get("ok") and r.get("code") == "revision_mismatch", str(r))
    # 恢复真实修订与签名
    rows = [json.loads(l) for l in revs_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for x in rows:
        if x["revision_id"] == rid:
            x["content_sha256"] = rev["content_sha256"]
            for a in x.get("approvals") or []:
                a["content_sha256"] = rev["content_sha256"]
    revs_path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")

    # ⑥ 旧 base 拒绝 + ⑦ author 不可指认拒绝（无作者修订）
    r = ledger.publish_revision(rid, base_revision="base-stale", approver="u-pub",
                                approver_subject="u-pub", author="u-author")
    record("A1.7 旧 base 拒绝发布", not r.get("ok") and r.get("code") == "stale_base", str(r))
    _, rev_np = propose(evo, ledger, dict(fields, title="无作者候选",
                                          instruction="二级动火由设备科审批并抄送安环部"),
                        base_rev, validated="improved", with_author=False)
    r = ledger.publish_revision(rev_np["revision_id"], base_revision=base_rev,
                                approver="u-pub", approver_subject="u-pub", author="")
    record("A1.8 author 不可指认拒绝发布", not r.get("ok") and r.get("code") == "no_provenance", str(r))

    # 全过 → 发布成功
    r = ledger.publish_revision(rid, base_revision=base_rev, approver="u-pub",
                                approver_subject="u-pub", author="u-author")
    record("A1.9 门禁全过发布成功", r.get("ok") and r["pointer"]["revision_id"] == rid, str(r))
    receipt = r.get("receipt") or {}
    record("A1.9b 发布回执含门禁结果与审批者",
           receipt.get("gate") == "passed" and receipt.get("approver") == "u-pub", str(receipt))

    # A2：跨进程读到批准版本（全新 SkillManager + 新 ledger 实例 = 模拟另一进程）
    mgr2 = SkillManager(root_dir=str(skills), max_prompt_chars=5000)
    mgr2.load()
    ledger2 = SkillPublicationLedger(skills_root=str(skills), data_dir=str(tmp / "data"))
    info = ledger2.apply_published(mgr2)
    target = next(s for s in mgr2.skills if s.name == "动火作业票办理引导")
    record("A2.1 跨进程读到批准版本正文",
           info.get("revision_id") == rid and "设备科审批" in target.content, str(info))

    # A2：同一请求仅一个版本快照（冻结后热加载/回退不影响该请求）
    snap1 = mgr2.snapshot_for_request()
    p1, f1 = mgr2.prompt_for_frozen(snap1, "动火票怎么办理", "compliance")
    mgr2.reload()                              # 模拟并发热加载（目录被其他操作重扫）
    p2, f2 = mgr2.prompt_for_frozen(snap1, "动火票怎么办理", "compliance")
    record("A2.2 同一请求快照在热加载后仍一致", p1 == p2 and f1 == f2)
    record("A2.2b 注入摘要含 revision 与片段 sha",
           bool(f1) and all(x.get("content_sha256") and x.get("revision_id") for x in f1), str(f1))

    # A2：缓存键含版本——指针变化后同 request_id 不得复用旧快照
    s_a = ledger2.freeze_request("req-cache", mgr2)
    s_b = ledger2.freeze_request("req-cache", mgr2)
    record("A2.3 同请求同指针复用同一快照",
           s_a["snapshot_key"] == s_b["snapshot_key"] and s_a["revision_id"] == rid, str(s_a))
    rb = ledger2.rollback(base_rev, approver="u-pub", approver_subject="u-pub")
    record("A3.2a 回退成功且只移指针", rb.get("ok") and rb["pointer"]["revision_id"] == base_rev, str(rb))
    s_c = ledger2.freeze_request("req-cache", mgr2)
    record("A2.3b 指针变化后快照键随版本变化",
           s_c["snapshot_key"] != s_a["snapshot_key"] and s_c["revision_id"] == base_rev, str(s_c))
    # 回退非法目标拒绝
    r = ledger2.rollback("rev-never-published", approver="u-pub", approver_subject="u-pub")
    record("A3.2c 回退到未发布过版本拒绝", not r.get("ok") and r.get("code") == "unknown_revision", str(r))

    # A3：发布/回退重启可复现（再开新实例，指针=回退后版本，审计记录保留）
    ledger3 = SkillPublicationLedger(skills_root=str(skills), data_dir=str(tmp / "data"))
    record("A3.1 重启后指针可复现", ledger3.pointer()["revision_id"] == base_rev, str(ledger3.pointer()))
    kinds = [h["action"] for h in ledger3.history()]
    record("A3.2b 发布审计保留（baseline+候选发布+回退）",
           kinds.count("publish") == 2 and kinds.count("rollback") == 1, str(kinds))

    # A3：回放（有版本 → revision+摘要+正文版本；无 → unknown）
    ledger3.record_injection("req-old", rid, f1)
    rep = ledger3.replay("req-old")
    body_now = ledger3.revision_body(rid)
    import hashlib as _h
    record("A3.3a 旧请求可定位版本与注入摘要",
           rep["version"] == rid and rep["fragments"][0]["content_sha256"] == f1[0]["content_sha256"]
           and rep["fragments"][0]["body_sha256"] == _h.sha256(body_now.encode("utf-8")).hexdigest(),
           str(rep))
    rep_unknown = ledger3.replay("req-never-seen")
    record("A3.3b 无对应版本的历史请求标 unknown",
           rep_unknown["version"] == "unknown" and not rep_unknown["fragments"], str(rep_unknown))

    # A3：正文版本可按修订取回（不可变修订库）
    body = ledger3.revision_body(rid)
    record("A3.3c 按修订取回批准正文", bool(body) and "设备科审批" in body)


def test_api_layer(tmp, skills):
    """A1/A3/A4 的 API 端点行为 + 隔离形态双人 + 紧急模板与失败路径。"""
    from fastapi.testclient import TestClient
    from api.main import app

    with TestClient(app) as client:
        # A4：只读调试字段权限独立——部署形态下 /skills/debug 需管理密钥，/skills 不需要
        os.environ["SAFETYMIND_DEPLOYED"] = "1"
        os.environ["ADMIN_TOKEN"] = "test-admin-token"
        r = client.get("/skills/debug")
        record("A4.1a 调试端点无管理密钥拒绝", r.status_code == 401, f"status={r.status_code}")
        r = client.get("/skills/debug", headers={"X-Admin-Token": "test-admin-token"})
        record("A4.1b 调试端点带密钥可用", r.status_code == 200 and "pointer" in r.json(), f"status={r.status_code}")
        r = client.get("/skills", headers={"X-Admin-Token": "wrong"})
        record("A4.1c 公开摘要端点不要求管理密钥", r.status_code == 200, f"status={r.status_code}")
        del os.environ["SAFETYMIND_DEPLOYED"]

        # A1：API 发布门禁（本地形态：显式 author/approver 字段）
        pointer_rev = client.get("/skills/publish/pointer").json()["revision_id"]
        cand = {"title": "API 门禁候选", "instruction": "高处作业证有效期两年复审每半年",
                "when_to_use": "高处作业咨询", "keywords": ["高处作业"],
                "action": "add", "similarity": 0.1, "author": "u-author"}
        r = client.post("/skills/candidates", json=cand)
        cand_id = r.json()["id"]
        client.post(f"/skills/candidates/{cand_id}/approve")     # 既有审批落盘（作者侧登记）
        r = client.post(f"/skills/candidates/{cand_id}/preview", json={"author": "u-author"})
        pv = r.json()
        record("A1.10a 预览端点固定修订", r.status_code == 200 and pv.get("revision_id") and pv.get("content_sha256"),
               f"{r.status_code} {str(pv)[:140]}")
        rid = pv["revision_id"]
        r = client.post(f"/skills/revisions/{rid}/approve", json={"approver": "u-author", "author": "u-author"})
        record("A1.10b API 自审拒绝", r.status_code == 403 and r.json().get("code") == "self_approval",
               f"{r.status_code} {r.text[:140]}")
        r = client.post(f"/skills/revisions/{rid}/approve", json={"approver": "u-pub", "author": "u-author"})
        record("A1.10c API 审批成功", r.status_code == 200, f"{r.status_code} {r.text[:140]}")
        r = client.post(f"/skills/revisions/{rid}/validation", json=validation_improved())
        record("A1.10c2 API 登记验证证据", r.status_code == 200 and r.json().get("validation_verdict") == "improved",
               f"{r.status_code} {r.text[:140]}")
        r = client.post(f"/skills/revisions/{rid}/publish",
                        json={"base_revision": "base-stale", "approver": "u-pub", "author": "u-author"})
        record("A1.10d API 旧 base 拒绝", r.status_code == 409 and r.json().get("code") == "stale_base",
               f"{r.status_code} {r.text[:140]}")
        r = client.post(f"/skills/revisions/{rid}/publish",
                        json={"base_revision": pointer_rev, "approver": "u-pub", "author": "u-author"})
        record("A1.10e API 门禁全过发布成功",
               r.status_code == 200 and r.json().get("pointer", {}).get("revision_id") == rid,
               f"{r.status_code} {r.text[:200]}")

        # A1：隔离形态——服务端主体绑定作者与审批者（共享管理员凭据不能证明身份）
        os.environ["SAFETYMIND_DEPLOYED"] = "1"
        os.environ["ADMIN_TOKEN"] = "test-admin-token"
        os.environ["SAFETYMIND_USERS"] = json.dumps([
            {"token": "tok-a", "subject_id": "u-iso-author", "roles": ["reporter"], "scopes": ["*"]},
            {"token": "tok-b", "subject_id": "u-iso-pub", "roles": ["admin"], "scopes": ["*"]},
        ])
        pointer_rev = client.get("/skills/publish/pointer",
                                 headers={"X-Admin-Token": "test-admin-token"}).json()["revision_id"]
        r = client.post("/skills/candidates",
                        json={"title": "隔离候选", "instruction": "受限空间作业先通风检测再进入",
                              "when_to_use": "受限空间咨询", "keywords": ["受限空间"],
                              "action": "add", "similarity": 0.1},
                        headers={"X-User-Token": "tok-a", "X-Admin-Token": "test-admin-token"})
        iso_cand = r.json()
        record("A1.11a 隔离形态候选绑定服务端作者",
               iso_cand.get("author_subject") == "u-iso-author", str(iso_cand)[:140])
        client.post(f"/skills/candidates/{iso_cand['id']}/approve",
                    headers={"X-Admin-Token": "test-admin-token"})
        r = client.post(f"/skills/candidates/{iso_cand['id']}/preview", json={},
                        headers={"X-User-Token": "tok-a", "X-Admin-Token": "test-admin-token"})
        iso_rid = r.json()["revision_id"]
        r = client.post(f"/skills/revisions/{iso_rid}/approve", json={},
                        headers={"X-User-Token": "tok-a", "X-Admin-Token": "test-admin-token"})
        record("A1.11b 隔离形态作者本人审批被拒（服务端主体比对）",
               r.status_code == 403 and r.json().get("code") == "self_approval", f"{r.status_code} {r.text[:140]}")
        r = client.post(f"/skills/revisions/{iso_rid}/approve", json={},
                        headers={"X-User-Token": "tok-b", "X-Admin-Token": "test-admin-token"})
        record("A1.11c 隔离形态其他管理员审批通过", r.status_code == 200, f"{r.status_code} {r.text[:140]}")
        r = client.post(f"/skills/revisions/{iso_rid}/validation", json=validation_improved(),
                        headers={"X-User-Token": "tok-b", "X-Admin-Token": "test-admin-token"})
        record("A1.11c2 隔离形态登记验证证据", r.status_code == 200, f"{r.status_code} {r.text[:140]}")
        r = client.post(f"/skills/revisions/{iso_rid}/publish",
                        json={"base_revision": pointer_rev},
                        headers={"X-User-Token": "tok-b", "X-Admin-Token": "test-admin-token"})
        record("A1.11d 隔离形态发布成功（审批者来自服务端主体）",
               r.status_code == 200 and r.json().get("pointer", {}).get("revision_id") == iso_rid,
               f"{r.status_code} {r.text[:200]}")
        del os.environ["SAFETYMIND_USERS"]

        # A3：请求回放端点
        r = client.get("/skills/replay/req-never", headers={"X-Admin-Token": "test-admin-token"})
        record("A3.4a 回放端点未知请求标 unknown",
               r.status_code == 200 and r.json().get("version") == "unknown", f"{r.status_code} {r.text[:140]}")

        # A4：紧急模板与调用失败路径——CRITICAL 请求不因版本层受影响
        r = client.post("/chat", json={"message": "有人在泄漏现场被困了，快救命", "user_id": "t"})
        ok = r.status_code == 200
        body = r.json().get("response", "") if ok else ""
        record("A4.2 紧急模板不受版本层影响", ok and ("撤离" in body or "应急" in body or "被困" in body),
               f"{r.status_code} {body[:80]}")
        # 调用失败路径：不可达端点下普通请求仍返回 200（不 500，不因版本层崩溃）
        r = client.post("/chat", json={"message": "动火票怎么办理", "user_id": "t"})
        record("A4.3 调用失败路径不因版本层崩溃", r.status_code == 200, f"status={r.status_code}")
        del os.environ["SAFETYMIND_DEPLOYED"]
        del os.environ["ADMIN_TOKEN"]


def main():
    tmp, skills = make_env()
    try:
        test_core_gate(tmp, skills)
        test_api_layer(tmp, skills)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
