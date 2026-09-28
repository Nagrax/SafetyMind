# -*- coding: utf-8 -*-
"""Skill 自进化一期测试：相似度决策 / 反馈记录 / 审批落盘（add+merge）/ 热加载闭环 / API 端点。

用法: python tests/test_skill_evolution.py
环境：SAFETYMIND_SKILL_EVOLUTION=0（不触发 LLM 抽取）；skills 与数据目录均指向临时目录，
     approve 落盘不会污染真实 skills/。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ["SAFETYMIND_SKILL_EVOLUTION"] = "0"
os.environ["API_HOST"] = "127.0.0.1"
os.environ["AUDIT_ENABLED"] = "0"
os.environ["SAFETYMIND_BGE"] = "0"          # 跳过模型加载，加速启动
os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="sk_evo_"))
    skills_dir = tmp / "skills"
    data_dir = tmp / "data"
    skills_dir.mkdir()
    (skills_dir / "hot_work").mkdir()
    (skills_dir / "hot_work" / "SKILL.md").write_text(
        "---\nname: 动火作业票办理引导\nkeywords: 动火,办票,焊接审批\nagents: compliance\nenabled: true\n---\n\n"
        "逐步引导用户办理动火作业票：判定等级、气体分析、审批权限、有效期。\n", encoding="utf-8")
    os.environ["SAFETYMIND_SKILLS_DIR"] = str(skills_dir)
    os.environ["SAFETYMIND_SKILL_EVO_DIR"] = str(data_dir)

    from core.skill_evolution import SkillEvolution, similarity, MERGE_THRESHOLD
    evo = SkillEvolution(skills_root=str(skills_dir))

    # ── 1. 相似度与 add/merge 决策 ──
    from core.skill_loader import SkillManager
    mgr = SkillManager(root_dir=str(skills_dir))
    mgr.load()
    skills_meta = [{"name": s.name, "keywords": s.keywords, "content": s.content,
                    "path": s.path} for s in mgr.skills]

    near = {"instruction": "二级动火由设备科审批而非安环部，动火票有效期按等级区分",
            "when_to_use": "用户办理动火作业票时", "keywords": ["动火", "办票"]}
    far = {"instruction": "职业健康档案保存期限不少于三年", "when_to_use": "用户询问职业健康档案时",
           "keywords": ["职业健康"]}
    sim_near = similarity(near, skills_meta[0]["name"], skills_meta[0]["keywords"], skills_meta[0]["content"])
    sim_far = similarity(far, skills_meta[0]["name"], skills_meta[0]["keywords"], skills_meta[0]["content"])
    record("相近候选相似度高于阈值", sim_near >= MERGE_THRESHOLD, f"sim={sim_near:.3f}")
    record("无关候选相似度低于阈值", sim_far < MERGE_THRESHOLD, f"sim={sim_far:.3f}")
    d_near = evo.decide_against_skills(dict(near), skills_meta)
    d_far = evo.decide_against_skills(dict(far), skills_meta)
    record("相近候选判 merge 并指向目标", d_near["action"] == "merge" and d_near["target_skill"] == "动火作业票办理引导")
    record("无关候选判 add", d_far["action"] == "add")

    # ── 2. approve(add)：新建 SKILL.md + 热加载可命中 ──
    d_far["title"] = "职业健康档案要求"
    cand_add = evo.append_candidate(dict(d_far))
    r = evo.approve(cand_add["id"])
    record("approve(add) 成功", bool(r.get("ok")), str(r))
    written = Path(r.get("written", ""))
    record("add 落盘文件存在", written.exists() and written.name == "SKILL.md")
    mgr.reload()
    names = [s.name for s in mgr.skills]
    record("热加载后新 skill 可见", "职业健康档案要求" in names, str(names))
    hit = [s for s in mgr.skills if s.matches("职业健康档案保存多久")]
    record("新 skill 关键词可命中", len(hit) == 1)

    # ── 3. approve(merge)：快照 + 追加修订段 ──
    target_before = (skills_dir / "hot_work" / "SKILL.md").read_text(encoding="utf-8")
    cand_merge = evo.append_candidate(dict(d_near, title="动火审批权限修正"))
    r2 = evo.approve(cand_merge["id"])
    record("approve(merge) 成功", bool(r2.get("ok")), str(r2))
    target_after = (skills_dir / "hot_work" / "SKILL.md").read_text(encoding="utf-8")
    record("merge 追加了修订段", "运营修订" in target_after and len(target_after) > len(target_before))
    snaps = list(Path("skills_snapshots").glob("hot_work/*.md")) if Path("skills_snapshots").exists() else []
    record("merge 前存了版本快照", len(snaps) >= 1 and snaps[-1].read_text(encoding="utf-8") == target_before)

    # ── 4. reject 与重复审批 ──
    cand_x = evo.append_candidate(dict(far, title="待拒绝"))
    record("reject 成功", evo.reject(cand_x["id"]).get("ok"))
    again = evo.approve(cand_x["id"])
    record("已拒绝候选不可再 approve", not again.get("ok"))

    # ── 5. API 端点（loopback 免管理密钥）──
    from fastapi.testclient import TestClient
    from api.main import app
    with TestClient(app) as client:
        cand_count_before = len(evo.list_candidates())
        r = client.post("/feedback", json={"request_id": "t-1", "rating": "bad"})
        record("非法 rating 被 422 拒绝", r.status_code == 422)
        r = client.post("/feedback", json={"request_id": "t-1", "rating": "down",
                                           "comment": "审批人写错了", "message_head": "动火票怎么办",
                                           "answer_head": "二级动火由安环部审批"})
        record("POST /feedback 记录成功", r.status_code == 200 and r.json().get("recorded"))
        fb = evo.latest_feedback()
        record("反馈已落盘（EVOLUTION=0 不产生新候选）",
               fb and fb["rating"] == "down" and len(evo.list_candidates()) == cand_count_before)
        r = client.get("/skills/candidates")
        record("GET /skills/candidates 可用", r.status_code == 200 and "candidates" in r.json())
        # 注入 pending 候选走 API 审批闭环
        cand_api = evo.append_candidate({"title": "API审批用例",
                                         "instruction": "高处作业证有效期两年", "when_to_use": "高处作业咨询",
                                         "keywords": ["高处作业"], "action": "add", "similarity": 0.1})
        r = client.post(f"/skills/candidates/{cand_api['id']}/approve")
        body = r.json()
        record("API approve 落盘+热加载", r.status_code == 200 and body.get("skills_count", 0) >= 2, str(body))
        r = client.post(f"/skills/candidates/{cand_api['id']}/approve")
        record("重复 approve 返回 404", r.status_code == 404)

    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree("skills_snapshots", ignore_errors=True)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
