# -*- coding: utf-8 -*-
"""Skill 自进化一期：反馈采集 → 候选抽取（LLM 后台）→ 相似度 add/merge 决策 → 人工审批落盘。

设计对齐 EvoHarness 自进化链路（D:/claudecode/EvoHarness/自进化链路.md）的一期子集：
- 每条负反馈/带评语反馈最多产生一个候选（不抽取一次性任务内容、密钥、临时参数）；
- 候选与现有 skill 做相似度决策：≥0.55 强制 merge（防 skill 数量爆炸），否则 add；
- 落盘前必须人工审批（评测层是观察哨不是执行器）；merge/新增前存版本快照，全程 provenance 可溯源；
- 抽取走旁路 LLM（不阻塞反馈响应），失败如实记录，可后续人工补录。

存储（JSONL，追加式，进程内全量重读——量级为运营事件，非热路径）：
- data/skill_feedback.jsonl：每次 👍/👎 原始记录
- data/skill_candidates.jsonl：候选与审批状态（pending/approved/rejected/extraction_failed）
- skills_snapshots/<skill_dir>/<ts>.md：approve 修改现有 skill 前的快照
"""
import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

MERGE_THRESHOLD = 0.55   # 相似度高于此值强制 merge（EvoHarness 同值）


def _data_dir() -> Path:
    return Path(os.getenv("SAFETYMIND_SKILL_EVO_DIR", "data"))


def _bigrams(text: str) -> set:
    t = re.sub(r"\s+", "", str(text or ""))
    return {t[i:i + 2] for i in range(len(t) - 1)} if len(t) > 1 else {t} if t else set()


def similarity(candidate: Dict[str, Any], skill_name: str, skill_keywords: List[str],
               skill_content: str) -> float:
    """中文二元分词 Jaccard + 关键词命中加权的相似度，返回 [0,1]。

    决定候选是 merge 进现有 skill 还是 add 新建。确定性纯函数（可单测）。
    """
    cand_text = f"{candidate.get('when_to_use', '')} {candidate.get('instruction', '')}"
    key_hit = sum(1 for k in skill_keywords if k and k in cand_text)
    key_score = min(1.0, key_hit / 3.0) if skill_keywords else 0.0
    ja = _bigrams(cand_text) & _bigrams(f"{skill_name} {skill_content[:600]}")
    union = _bigrams(cand_text) | _bigrams(f"{skill_name} {skill_content[:600]}")
    jac = len(ja) / len(union) if union else 0.0
    return min(1.0, 0.5 * key_score + 0.5 * min(1.0, jac * 6))


class SkillEvolution:
    """反馈与候选的落盘/查询/审批。无内存锁前提：运营操作频率极低，文件追加原子性足够。"""

    def __init__(self, skills_root: str):
        self.skills_root = Path(skills_root)
        _data_dir().mkdir(parents=True, exist_ok=True)
        self.feedback_path = _data_dir() / "skill_feedback.jsonl"
        self.candidates_path = _data_dir() / "skill_candidates.jsonl"

    # ── 反馈 ──────────────────────────────────────────────
    def record_feedback(self, *, request_id: str, rating: str, comment: str = "",
                        user_id: str = "", conv_id: str = "",
                        message_head: str = "", answer_head: str = "") -> Dict[str, Any]:
        rec = {"ts": time.time(), "request_id": request_id, "rating": rating,
               "comment": comment[:500], "user_id": user_id, "conv_id": conv_id,
               "message_head": message_head[:200], "answer_head": answer_head[:300]}
        with open(self.feedback_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def latest_feedback(self) -> Optional[Dict[str, Any]]:
        rows = self._read(self.feedback_path)
        return rows[-1] if rows else None

    # ── 候选 ──────────────────────────────────────────────
    def append_candidate(self, cand: Dict[str, Any]) -> Dict[str, Any]:
        cand.setdefault("id", f"cand-{int(time.time()*1000)%10**10}")
        cand.setdefault("created_ts", time.time())
        cand.setdefault("status", "pending")
        with open(self.candidates_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(cand, ensure_ascii=False) + "\n")
        return cand

    def list_candidates(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = self._read(self.candidates_path)
        return [r for r in rows if not status or r.get("status") == status]

    def _update_candidate(self, cand_id: str, **fields) -> Optional[Dict[str, Any]]:
        rows = self._read(self.candidates_path)
        found = None
        for r in rows:
            if r.get("id") == cand_id:
                r.update(fields)
                found = r
        if found:
            with open(self.candidates_path, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return found

    def decide_against_skills(self, cand: Dict[str, Any],
                              skills: List[Dict[str, Any]]) -> Dict[str, Any]:
        """对现有 skill 全量算相似度，产出 add/merge 决策（纯确定性）。"""
        best, best_sim = None, 0.0
        for s in skills:
            sim = similarity(cand, s.get("name", ""), s.get("keywords", []),
                             s.get("content", ""))
            if sim > best_sim:
                best, best_sim = s, sim
        if best and best_sim >= MERGE_THRESHOLD:
            cand["action"] = "merge"
            cand["target_skill"] = best["name"]
            cand["target_path"] = best.get("path", "")
        else:
            cand["action"] = "add"
            cand["target_skill"] = ""
        cand["similarity"] = round(best_sim, 3)
        return cand

    # ── 审批落盘 ─────────────────────────────────────────
    def approve(self, cand_id: str) -> Dict[str, Any]:
        cand = next((c for c in self.list_candidates()
                     if c.get("id") == cand_id and c.get("status") == "pending"), None)
        if cand is None:
            return {"ok": False, "error": "pending 候选不存在"}
        written = (self._write_new_skill(cand) if cand["action"] == "add"
                   else self._merge_into_skill(cand))
        self._update_candidate(cand_id, status="approved", decided_ts=time.time())
        return {"ok": True, "written": written}

    def reject(self, cand_id: str) -> Dict[str, Any]:
        cand = self._update_candidate(cand_id, status="rejected", decided_ts=time.time())
        return {"ok": cand is not None, "written": str(cand and cand.get("id"))}

    def _write_new_skill(self, cand: Dict[str, Any]) -> str:
        slug = re.sub(r"[^0-9a-zA-Z_\-]+", "_", cand.get("when_to_use", "")[:12]) or "misc"
        d = self.skills_root / f"evolved_{slug}_{int(time.time())%100000}"
        d.mkdir(parents=True, exist_ok=True)
        kws = ",".join(cand.get("keywords", []) or [])
        body = (f"---\nname: {cand['title']}\n"
                f"description: 由用户反馈自动提炼（{cand.get('provenance', '')}）\n"
                f"keywords: {kws}\nenabled: true\n---\n\n"
                f"## 适用场景\n{cand.get('when_to_use', '')}\n\n"
                f"## 业务规则（用户纠正）\n{cand.get('instruction', '')}\n")
        p = d / "SKILL.md"
        p.write_text(body, encoding="utf-8")
        return str(p)

    def _merge_into_skill(self, cand: Dict[str, Any]) -> str:
        target = Path(cand.get("target_path", ""))
        if not target.exists():
            # 目标文件被移动/删除：降级为新建，决策记录保留
            return self._write_new_skill(cand)
        snap_dir = Path("skills_snapshots") / target.parent.name
        snap_dir.mkdir(parents=True, exist_ok=True)
        (snap_dir / f"{int(time.time())}.md").write_text(
            target.read_text(encoding="utf-8"), encoding="utf-8")
        addition = (f"\n\n## 运营修订（auto-merged {time.strftime('%Y-%m-%d')}）\n"
                    f"- 适用：{cand.get('when_to_use', '')}\n"
                    f"- 修正：{cand.get('instruction', '')}\n")
        with open(target, "a", encoding="utf-8") as f:
            f.write(addition)
        return str(target)

    @staticmethod
    def _read(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows


EXTRACT_SYSTEM = (
    "你从用户对安全生产问答系统的反馈中，提炼至多一条可复用的业务规则修正。"
    "输出严格 JSON：{\"title\":\"短名\",\"instruction\":\"应当遵守的规则（对用户的纠正做一般化，不含一次性细节）\","
    "\"when_to_use\":\"适用场景\",\"keywords\":[\"触发关键词\",...]}。"
    "只输出 JSON。不提炼一次性任务内容、密钥、临时参数；无法一般化时输出 {\"none\":true}。"
)


async def extract_candidate_async(evo: "SkillEvolution", feedback: Dict[str, Any],
                                  skills_meta: List[Dict[str, Any]],
                                  api_key: str, base_url: str, model: str) -> None:
    """后台抽取：LLM 提炼候选 → 相似度决策 → 落 pending。失败落 extraction_failed（反馈不丢）。"""
    from anthropic import AsyncAnthropic
    from core.llm_utils import llm_client_kwargs
    try:
        if os.getenv("SAFETYMIND_SKILL_EVOLUTION", "1") != "1":
            return
        client = AsyncAnthropic(api_key=api_key, base_url=base_url or None,
                                **llm_client_kwargs())
        user_prompt = (f"用户消息：{feedback.get('message_head', '')}\n"
                       f"助手回答（节选）：{feedback.get('answer_head', '')}\n"
                       f"用户反馈：{'差评' if feedback.get('rating') == 'down' else '评语'} "
                       f"{feedback.get('comment', '')}")
        resp = await client.messages.create(
            model=model, max_tokens=600,
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = "".join(b.get("text", "") for b in resp.content
                      if getattr(b, "type", "") == "text")
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            raise ValueError(f"抽取输出无 JSON: {raw[:120]}")
        parsed = json.loads(m.group(0))
        if parsed.get("none"):
            return
        cand = {"title": str(parsed.get("title", "用户反馈规则"))[:40],
                "instruction": str(parsed.get("instruction", ""))[:1200],
                "when_to_use": str(parsed.get("when_to_use", ""))[:300],
                "keywords": [str(k)[:20] for k in (parsed.get("keywords") or [])][:8],
                "provenance": f"feedback:{feedback.get('request_id')} {time.strftime('%m-%d')}"}
        if not cand["instruction"]:
            raise ValueError("抽取 instruction 为空")
        cand = evo.decide_against_skills(cand, skills_meta)
        evo.append_candidate(cand)
        logger.info(f"skill 候选已生成: {cand['title']} action={cand['action']} sim={cand['similarity']}")
    except Exception as ex:
        evo.append_candidate({
            "title": "（抽取失败）" + (feedback.get("comment", "")[:30] or feedback.get("request_id", "")),
            "instruction": feedback.get("comment", ""),
            "when_to_use": "人工复核后补录",
            "keywords": [],
            "provenance": f"feedback:{feedback.get('request_id')}",
            "status": "extraction_failed",
            "error": str(ex)[:300],
        })
        logger.warning(f"skill 候选抽取失败（反馈已留存）: {ex}")


def spawn_extraction(evo: "SkillEvolution", feedback: Dict[str, Any],
                     skills_meta: List[Dict[str, Any]],
                     api_key: str, base_url: str, model: str) -> None:
    """同步入口：把抽取挂到事件循环后台（反馈响应不等它）。"""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(extract_candidate_async(evo, feedback, skills_meta,
                                                 api_key, base_url, model))
    except RuntimeError:
        logger.info("无运行事件循环，跳过后台抽取（反馈已记录）")
