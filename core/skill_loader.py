"""
SafetyMind Skill 加载器。

Skill 是一段可热加载的业务能力说明，用来补充 Agent 的 system prompt。
它适合放置企业话术、处理流程、合规边界、排障 SOP 等需要运营侧快速调整的规则。

SM-30 起新增 SkillPublicationLedger：SOP 修订登记（不可变全文快照）、双人审核、
发布门禁（无证据/自审/无权/旧 base/审核后篡改/版本不匹配一律拒绝）、发布指针与
只移指针的回退、请求级版本冻结与回放。修订库（JSONL 追加式）是发布后正文的
事实源；skills/ 目录只是启动登记输入与审计痕迹——发布/回退后生效正文以
apply_published 的覆盖为准（回退只移动指针即可恢复正文，重启可复现）。
"""
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """单个 Skill 的标准化表示，屏蔽 Markdown/JSON 等不同文件格式差异。"""
    name: str
    description: str
    content: str
    path: str
    keywords: List[str] = field(default_factory=list)
    agents: List[str] = field(default_factory=list)
    enabled: bool = True

    def matches(self, message: str, agent_type: Optional[str] = None) -> bool:
        """
        判断当前请求是否应该注入这个 Skill。

        - agents 为空：适用于所有 Agent；否则只匹配指定 Agent。
        - keywords 为空：作为全局 Skill 注入；否则只有命中关键词才注入。
        """
        if not self.enabled:
            return False

        if self.agents and agent_type and agent_type.lower() not in self.agents:
            return False

        if not self.keywords:
            return True

        lowered = (message or "").lower()
        return any(keyword.lower() in lowered for keyword in self.keywords)

    def to_prompt_block(self, max_chars: int = 3200) -> str:
        """格式化为可直接拼入 system prompt 的文本块，并限制单个 Skill 长度。"""
        body = self.content.strip()
        if len(body) > max_chars:
            body = body[:max_chars].rstrip() + "\n..."
        description = f"\n说明: {self.description}" if self.description else ""
        return f"### {self.name}{description}\n{body}"

    def to_summary(self) -> Dict[str, Any]:
        """返回 API 可序列化摘要，避免把完整长文本默认暴露给健康检查。"""
        return {
            "name": self.name,
            "description": self.description,
            "path": self.path,
            "keywords": self.keywords,
            "agents": self.agents,
            "enabled": self.enabled,
            "content_chars": len(self.content),
        }


class SkillManager:
    """
    从目录中发现、解析并管理 Skills。

    支持两种常用结构：
      1. skills/refund/SKILL.md
      2. skills/refund.json / skills/refund.md / skills/refund.txt
    """

    SUPPORTED_SUFFIXES = {".md", ".txt", ".json"}

    def __init__(self, root_dir: str, max_prompt_chars: int = 5000):
        self.root_dir = Path(root_dir).expanduser().resolve()
        self.max_prompt_chars = max_prompt_chars
        self._skills: List[Skill] = []
        self._errors: List[str] = []
        self._published_revision: str = ""   # SM-30：当前生效发布修订（apply_published 设置）

    @property
    def skills(self) -> List[Skill]:
        return list(self._skills)

    @property
    def errors(self) -> List[str]:
        return list(self._errors)

    def load(self) -> List[Skill]:
        """重新扫描目录并加载 Skills；单个文件失败不会影响其他 Skill 生效。"""
        loaded: List[Skill] = []
        errors: List[str] = []

        if not self.root_dir.exists():
            logger.info(f"Skill 目录不存在，跳过加载: {self.root_dir}")
            self._skills = []
            self._errors = []
            return []

        for path in self._discover_files(self.root_dir):
            try:
                skill = self._load_file(path)
                if skill is not None:
                    loaded.append(skill)
            except Exception as ex:
                msg = f"{path}: {ex}"
                errors.append(msg)
                logger.warning(f"Skill 加载失败: {msg}")

        self._skills = loaded
        self._errors = errors
        self._log_loaded_skills()
        return self.skills

    def reload(self) -> List[Skill]:
        """运行时热加载入口，供 API 调用。"""
        return self.load()

    def prompt_for(self, message: str, agent_type: Optional[str] = None) -> str:
        """为当前用户请求构建 Skill prompt（实时视图，无版本冻结；测试/兼容入口）。"""
        prompt, _fragments = self._render_prompt(
            [(s, s) for s in self._skills], message, agent_type, revision_id=self._published_revision)
        return prompt

    def apply_published(self, published: Dict[str, Any]) -> Dict[str, Any]:
        """把发布指针指向的修订全集内容覆盖进内存生效视图（SM-30）。

        `published` = {"revision_id": str, "content": {skill_name: {"content": str, ...}}}。
        生效正文以修订库为准（skills/ 目录允许作为审计痕迹保留旧文）。重复应用幂等。
        """
        rid = str(published.get("revision_id") or "")
        content = published.get("content") or {}
        changed: List[str] = []
        for skill in self._skills:
            entry = content.get(skill.name)
            if entry and isinstance(entry.get("content"), str) and entry["content"] != skill.content:
                skill.content = entry["content"]
                changed.append(skill.name)
        self._published_revision = rid
        if changed:
            logger.info(f"Skill 生效视图已按发布指针 {rid} 覆盖: {changed}")
        return {"revision_id": rid, "changed": changed}

    @property
    def published_revision(self) -> str:
        """当前生效的发布修订 id（未应用发布视图时为空）。"""
        return self._published_revision

    def snapshot_for_request(self) -> Dict[str, Any]:
        """请求开始时冻结一次版本快照（SM-30 A2）。

        快照持有 skills 列表的浅拷贝——Skill.dataclass 字段为 str/list（使用中不可变），
        后续 reload/回退原地替换 `self._skills` 不会影响已冻结快照，
        因此同一请求的全部（并行）专家注入内容一致。快照键含 revision_id，
        指针变化后同一 request_id 会得到新快照（缓存/失效条件包含版本）。
        """
        return {
            "revision_id": self._published_revision,
            "skills": list(self._skills),
            "frozen_ts": time.time(),
        }

    def prompt_for_frozen(self, snapshot: Dict[str, Any], message: str,
                          agent_type: Optional[str] = None) -> Tuple[str, List[Dict[str, Any]]]:
        """从请求冻结快照渲染 Skill prompt，返回 (prompt, 注入摘要 fragments)。

        fragments = [{skill_name, revision_id, content_sha256, chars, truncated}]，
        content_sha256 为**实际拼入 prompt 的片段**（截断后）的摘要，供请求级回放比对。
        """
        revision_id = str(snapshot.get("revision_id") or "")
        pairs = [(s, s) for s in snapshot.get("skills") or []]
        return self._render_prompt(pairs, message, agent_type, revision_id=revision_id)

    def _render_prompt(self, pairs, message: str, agent_type: Optional[str],
                       revision_id: str = "") -> Tuple[str, List[Dict[str, Any]]]:
        """从 (候选匹配用 skill 对象) 列表渲染 prompt 与注入摘要（prompt_for/_frozen 共用）。"""
        blocks: List[str] = []
        matched: List[tuple[Skill, List[str]]] = []
        fragments: List[Dict[str, Any]] = []
        remaining = self.max_prompt_chars
        lowered_message = (message or "").lower()

        for skill, _src in pairs:
            if not skill.matches(message, agent_type):
                continue
            matched_keywords = [
                keyword for keyword in skill.keywords
                if keyword.lower() in lowered_message
            ]
            block = skill.to_prompt_block()
            truncated = False
            if len(block) > remaining:
                block = block[:remaining].rstrip() + "\n..."
                truncated = True
            blocks.append(block)
            matched.append((skill, matched_keywords))
            fragments.append({
                "skill_name": skill.name,
                "revision_id": revision_id,
                "content_sha256": hashlib.sha256(block.encode("utf-8")).hexdigest(),
                "chars": len(block),
                "truncated": truncated,
            })
            remaining -= len(block)
            if remaining <= 0:
                break

        if not blocks:
            logger.debug(
                "Skills 未命中: agent=%s message=%r",
                agent_type or "all",
                (message or "")[:80],
            )
            return "", fragments

        detail = "; ".join(
            f"{skill.name}(keywords={', '.join(keywords) if keywords else 'all'})"
            for skill, keywords in matched
        )
        logger.info(
            "Skills 已注入: agent=%s matched=%s message=%r",
            agent_type or "all",
            detail,
            (message or "")[:80],
        )

        return (
            "以下是当前请求可用的 SafetyMind Skills。"
            "请优先遵循这些业务规则；如果与系统角色冲突，以系统角色和安全边界为准。\n\n"
            + "\n\n".join(blocks)
        ), fragments

    def summary(self) -> Dict[str, Any]:
        """返回 Skill 管理器状态，用于 /skills 接口和排障。"""
        return {
            "root_dir": str(self.root_dir),
            "count": len(self._skills),
            "skills": [skill.to_summary() for skill in self._skills],
            "errors": self.errors,
        }

    def _log_loaded_skills(self) -> None:
        """在控制台输出醒目的 Skill 加载结果，方便启动和热加载时确认生效状态。"""
        lines = [
            "",
            "================ SafetyMind Skills Loaded ================",
            f"目录: {self.root_dir}",
            f"数量: {len(self._skills)}",
        ]

        if self._skills:
            for index, skill in enumerate(self._skills, start=1):
                agents = ", ".join(skill.agents) if skill.agents else "all"
                keywords = ", ".join(skill.keywords[:8]) if skill.keywords else "all"
                if len(skill.keywords) > 8:
                    keywords += ", ..."
                lines.extend([
                    f"{index}. {skill.name}",
                    f"   agents: {agents}",
                    f"   keywords: {keywords}",
                    f"   path: {skill.path}",
                ])
        else:
            lines.append("未加载任何 Skill。")

        if self._errors:
            lines.append("解析错误:")
            lines.extend(f"  - {error}" for error in self._errors)

        lines.append("========================================================")
        logger.info("\n".join(lines))

    def _discover_files(self, root_dir: Path) -> Iterable[Path]:
        """发现可加载文件，优先读取目录规范文件 SKILL.md。"""
        skill_md_files = sorted(root_dir.rglob("SKILL.md"))
        yielded = {path.resolve() for path in skill_md_files}
        for path in skill_md_files:
            yield path

        for path in sorted(root_dir.rglob("*")):
            resolved = path.resolve()
            if resolved in yielded or not path.is_file():
                continue
            if path.name.startswith(".") or path.name.upper() == "README.MD":
                continue
            if path.suffix.lower() in self.SUPPORTED_SUFFIXES:
                yield path

    def _load_file(self, path: Path) -> Optional[Skill]:
        if path.suffix.lower() == ".json":
            return self._load_json(path)
        return self._load_text(path)

    def _load_json(self, path: Path) -> Optional[Skill]:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("JSON Skill 必须是对象格式")

        content = str(raw.get("content") or raw.get("instructions") or "").strip()
        if not content:
            raise ValueError("缺少 content 或 instructions")

        return Skill(
            name=str(raw.get("name") or path.stem),
            description=str(raw.get("description") or ""),
            content=content,
            path=str(path),
            keywords=self._as_list(raw.get("keywords")),
            agents=[item.lower() for item in self._as_list(raw.get("agents"))],
            enabled=self._as_bool(raw.get("enabled"), default=True),
        )

    def _load_text(self, path: Path) -> Optional[Skill]:
        raw = path.read_text(encoding="utf-8")
        meta, body = self._split_front_matter(raw)
        body = body.strip()
        if not body:
            return None

        default_name = path.parent.name if path.name == "SKILL.md" else path.stem
        name = str(meta.get("name") or self._first_heading(body) or default_name)

        # 如果首行标题只是 Skill 名称，注入 prompt 时去掉它，减少重复噪音。
        body = self._strip_first_heading(body, name)

        return Skill(
            name=name,
            description=str(meta.get("description") or ""),
            content=body,
            path=str(path),
            keywords=self._as_list(meta.get("keywords")),
            agents=[item.lower() for item in self._as_list(meta.get("agents"))],
            enabled=self._as_bool(meta.get("enabled"), default=True),
        )

    def _split_front_matter(self, raw: str) -> tuple[Dict[str, Any], str]:
        """
        解析 Markdown 顶部的简单 front matter。

        这里刻意不用 PyYAML，避免为一个轻量配置格式新增运行时依赖。
        """
        text = raw.lstrip()
        if not text.startswith("---"):
            return {}, raw

        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}, raw

        meta: Dict[str, Any] = {}
        end_idx: Optional[int] = None
        for idx, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                end_idx = idx
                break
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip().strip("\"'")

        if end_idx is None:
            return {}, raw
        return meta, "\n".join(lines[end_idx + 1:])

    @staticmethod
    def _first_heading(body: str) -> Optional[str]:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip() or None
        return None

    @staticmethod
    def _strip_first_heading(body: str, name: str) -> str:
        lines = body.splitlines()
        if not lines:
            return body
        first = lines[0].strip()
        if first.startswith("#") and first.lstrip("#").strip() == name:
            return "\n".join(lines[1:]).strip()
        return body

    @staticmethod
    def _as_list(value: Any) -> List[str]:
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return [item.strip() for item in str(value).split(",") if item.strip()]

    @staticmethod
    def _as_bool(value: Any, default: bool = False) -> bool:
        if value is None or value == "":
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() not in {"0", "false", "no", "off", "disabled"}


class SkillPublicationLedger:
    """SOP 修订注册表与发布指针（SM-30：双人审核、评测发布、请求回溯）。

    存储（JSONL 追加式，运营事件量级；文件是事实源，进程内无长驻缓存——
    每次读取重放，天然跨进程/重启可复现）：
      - skill_revisions.jsonl        修订（不可变全文快照；approval 以全量重写追加）
      - skill_publications.jsonl     发布/回退审计（只追加，回退=新增记录移动指针）
      - skill_request_snapshots.jsonl 请求级冻结（request_id → revision + 注入片段摘要）

    语义：
      - 修订 = 一次对**生效全集**的全文快照（content: {skill_name: {content, content_sha256}}），
        revision_id 由内容摘要派生（rev-<sha12> / base-<sha12>），内容变 → id 变，篡改可检。
      - 发布门禁按序检查（任一不过即拒，拒绝码机器可读）：revision_mismatch →
        no_evidence/evidence_regressed → no_provenance → not_approved → self_approval →
        not_authorized → tampered → stale_base。
      - 回退只移动指针（新 rollback 记录）；apply_published 把指针修订正文覆盖进
        SkillManager 生效视图，冻结条件（缓存键）包含 revision_id。
    """

    APPROVER_ROLES = frozenset({"admin", "publisher"})
    # 有证据 = 效果验证已执行且未退步（improved/no_change）；regressed 拒绝；
    # unvalidated / extraction_failed / 缺失 = 无证据拒绝。
    PUBLISHABLE_VERDICTS = frozenset({"improved", "no_change"})

    def __init__(self, skills_root: str, data_dir: Optional[str] = None):
        self.skills_root = Path(skills_root)
        base = Path(data_dir) if data_dir else Path("data")
        base.mkdir(parents=True, exist_ok=True)
        self.revisions_path = base / "skill_revisions.jsonl"
        self.publications_path = base / "skill_publications.jsonl"
        self.replays_path = base / "skill_request_snapshots.jsonl"
        self._req_cache: Dict[str, Dict[str, Any]] = {}   # 键 = f"{request_id}:{revision_id}"（含版本）

    # ── 基础设施 ─────────────────────────────────────────
    @staticmethod
    def sha256_text(text: str) -> str:
        return hashlib.sha256((text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
        return rows

    @staticmethod
    def _append_jsonl(path: Path, rec: Dict[str, Any]) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def _read_revisions(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.revisions_path)

    def _read_publications(self) -> List[Dict[str, Any]]:
        return self._read_jsonl(self.publications_path)

    def revision(self, rid: str) -> Optional[Dict[str, Any]]:
        for r in self._read_revisions():
            if r.get("revision_id") == rid:
                return r
        return None

    def _snapshot_from_manager(self) -> Dict[str, Dict[str, Any]]:
        """扫描 skills 目录 → 生效全集快照（name → {content, content_sha256}）。"""
        mgr = SkillManager(root_dir=str(self.skills_root), max_prompt_chars=10 ** 9)
        mgr.load()
        content: Dict[str, Dict[str, Any]] = {}
        for s in mgr.skills:
            content[s.name] = {"content": s.content,
                               "content_sha256": self.sha256_text(s.content)}
        return content

    # ── 登记与预览（固定修订）────────────────────────────
    def register_baseline(self) -> List[Dict[str, Any]]:
        """把当前 skills 目录登记为起始版本（幂等）；首次登记同时落一条 baseline 发布记录，
        使发布指针从"当前已生效内容"开始（发布/回退均以此为祖先）。"""
        content = self._snapshot_from_manager()
        digest = self.sha256_text(json.dumps(content, ensure_ascii=False, sort_keys=True))
        rid = f"base-{digest[:12]}"
        existing = self.revision(rid)
        if existing is None:
            rec = {"revision_id": rid, "kind": "baseline", "skill_name": "",
                   "changed": sorted(content.keys()), "content": content,
                   "content_sha256": digest, "base_revision": "",
                   "author": "system", "author_subject": "system",
                   "source_cand_id": "", "validation_verdict": "",
                   "provenance": "register_baseline", "created_ts": time.time(),
                   "approvals": []}
            self._append_jsonl(self.revisions_path, rec)
            if not self._read_publications():
                self._append_jsonl(self.publications_path, {
                    "action": "publish", "kind": "baseline", "revision_id": rid,
                    "base_revision": "", "approver": "system", "approver_subject": "system",
                    "gate": "baseline_init", "seq": 1, "published_ts": time.time()})
            existing = rec
        return [existing]

    def propose_from_candidate(self, cand: Dict[str, Any], base_revision: str = "") -> Dict[str, Any]:
        """候选 → 固定修订（预览）：以 base（缺省当前指针）全集为底，应用候选变更。

        返回修订记录（含 diff 摘要）。修订一旦生成不可变；发布请求必须引用同一
        revision_id（内容变 → id 变 → 版本不匹配拒绝）。幂等：同 id 已存在直接返回。
        """
        base = base_revision or self.pointer()["revision_id"]
        base_rev = self.revision(base)
        if base_rev is None:
            return {"ok": False, "code": "base_unknown", "error": f"base 修订不存在: {base}"}
        content = {k: dict(v) for k, v in (base_rev.get("content") or {}).items()}

        action = cand.get("action") or "add"
        if action == "merge":
            target = cand.get("target_skill") or ""
            if target not in content:
                return {"ok": False, "code": "target_missing",
                        "error": f"merge 目标 skill 不在生效集: {target}"}
            addition = (f"\n\n## 运营修订\n- 适用：{cand.get('when_to_use', '')}\n"
                        f"- 修正：{cand.get('instruction', '')}\n")
            new_body = content[target]["content"] + addition
            content[target] = {"content": new_body, "content_sha256": self.sha256_text(new_body)}
            changed = [target]
            skill_name = target
        else:
            kws = ",".join(cand.get("keywords", []) or [])
            new_body = (f"---\nname: {cand.get('title', '运营修订')}\n"
                        f"description: 由候选发布（{cand.get('provenance', 'manual')}）\n"
                        f"keywords: {kws}\nenabled: true\n---\n\n"
                        f"## 适用场景\n{cand.get('when_to_use', '')}\n\n"
                        f"## 业务规则\n{cand.get('instruction', '')}\n")
            skill_name = cand.get("title") or "运营修订"
            content[skill_name] = {"content": new_body, "content_sha256": self.sha256_text(new_body)}
            changed = [skill_name]

        digest = self.sha256_text(json.dumps(content, ensure_ascii=False, sort_keys=True))
        rid = f"rev-{digest[:12]}"
        existing = self.revision(rid)
        if existing is not None:
            return existing
        rec = {"revision_id": rid, "kind": "candidate", "skill_name": skill_name,
               "changed": changed, "content": content, "content_sha256": digest,
               "base_revision": base, "author": str(cand.get("author") or ""),
               "author_subject": str(cand.get("author_subject") or ""),
               "source_cand_id": str(cand.get("id") or ""),
               "validation_verdict": str(cand.get("validation_verdict") or ""),
               "provenance": str(cand.get("provenance") or ""),
               "created_ts": time.time(), "approvals": []}
        self._append_jsonl(self.revisions_path, rec)
        rec["diff_summary"] = self._diff_summary(base_rev.get("content") or {}, content)
        return rec

    @staticmethod
    def _diff_summary(old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for name, entry in new.items():
            o = (old.get(name) or {}).get("content")
            n = entry.get("content")
            if o is None:
                out.append({"skill_name": name, "change": "add", "chars": len(n)})
            elif o != n:
                out.append({"skill_name": name, "change": "merge",
                            "added_chars": max(0, len(n) - len(o))})
        return out

    # ── 审批（双人审核第一主体）──────────────────────────
    def approve_revision(self, rid: str, *, approver: str = "", approver_subject: str = "",
                         author: str = "", approver_roles: Optional[frozenset] = None) -> Dict[str, Any]:
        """审批签名：记录审批者主体并绑定当时内容摘要（防审核后篡改）。

        `approver_roles`：隔离形态由服务端主体解析传入；None = 本地形态经 require_admin
        的管理操作（记 local_admin，有权）。自审（审批者==作者）与无权在此拒绝。
        """
        rev = self.revision(rid)
        if rev is None:
            return {"ok": False, "code": "revision_mismatch", "error": f"修订不存在: {rid}"}
        eff_author = author or rev.get("author") or rev.get("author_subject") or ""
        eff_subj_author = rev.get("author_subject") or ""
        if (approver_subject and eff_subj_author and approver_subject == eff_subj_author) or \
           (approver and eff_author and approver == eff_author):
            return {"ok": False, "code": "self_approval",
                    "error": "候选作者与发布审批者必须为不同受信主体"}
        roles = set(approver_roles) if approver_roles else {"admin"}
        # 本地形态（roles=None）= 经 require_admin 闸门的管理操作，语义等价 admin；
        # 隔离形态必须传服务端解析的角色集，无 admin/publisher 角色即拒。
        if not (roles & self.APPROVER_ROLES):
            return {"ok": False, "code": "not_authorized",
                    "error": f"审批主体无发布审批角色（需 {sorted(self.APPROVER_ROLES)}）"}
        approval = {"approver": approver or approver_subject,
                    "approver_subject": approver_subject,
                    "roles": sorted(roles),
                    "basis": "subject_roles" if approver_roles else "local_admin_gate",
                    "content_sha256": rev["content_sha256"],
                    "approved_ts": time.time()}
        rows = self._read_revisions()
        for r in rows:
            if r.get("revision_id") == rid:
                r.setdefault("approvals", []).append(approval)
        self._write_jsonl(self.revisions_path, rows)
        return {"ok": True, "revision_id": rid, "approval": approval}

    # ── 验证证据登记（"审批是人的决定，验证是证据"——两者解耦，不得互相假冒）──
    def register_validation(self, rid: str, validation: Dict[str, Any],
                            registered_by: str = "") -> Dict[str, Any]:
        """把一次已执行的效果验证结果登记到修订上（SM-30"固定修订的验证"）。

        如实登记：verdict 原样存储（unvalidated/regressed 照登，门禁会拒）；
        不在本方法内伪造或推断结论。live 验证（真实模型）由运维在隔离环境执行
        core/skill_effect_validation.py 后登记其产物；离线探针证据须注明来源。
        """
        rev = self.revision(rid)
        if rev is None:
            return {"ok": False, "code": "revision_mismatch", "error": f"修订不存在: {rid}"}
        if not isinstance(validation, dict) or not str(validation.get("verdict") or ""):
            return {"ok": False, "code": "invalid_validation",
                    "error": "验证记录必须是含 verdict 的对象（如实登记，不推断）"}
        rows = self._read_revisions()
        for r in rows:
            if r.get("revision_id") == rid:
                r["validation_verdict"] = str(validation.get("verdict"))
                r["validation"] = {k: validation.get(k)
                                   for k in ("verdict", "delta", "margin", "model", "note")
                                   if k in validation}
                r["validation_registered_by"] = registered_by
                r["validation_registered_ts"] = time.time()
        self._write_jsonl(self.revisions_path, rows)
        return {"ok": True, "revision_id": rid,
                "validation_verdict": str(validation.get("verdict"))}

    # ── 发布门禁（第二道闸）──────────────────────────────
    def publish_revision(self, rid: str, *, base_revision: str = "", approver: str = "",
                         approver_subject: str = "", author: str = "") -> Dict[str, Any]:
        """按 decision.md 固化的门禁序检查并发布（追加 publish 记录、移动指针）。

        检查序：存在性 → base 乐观并发（请求引用的 base 不对即拒，最便宜的前置检查）→
        证据 → 可指认 → 审批（自审/无权/篡改）→ id 派生（版本不匹配）。
        篡改检查先于 id 派生校验：内容被改时 id 必然不再派生，
        但"审批签名与内容不符"是更具体的拒绝原因。
        """
        rev = self.revision(rid)
        if rev is None:
            return {"ok": False, "code": "revision_mismatch",
                    "error": f"修订不存在: {rid}"}

        cur = self.pointer()["revision_id"]
        if not base_revision or base_revision != cur:
            # fail-closed：base_revision 必须显式声明且等于当前指针（乐观并发；空=未声明同样拒绝）
            return {"ok": False, "code": "stale_base",
                    "error": f"base_revision 与当前指针不符: 请求={base_revision or '(空)'} 当前={cur or '(空)'}"}

        verdict = str(rev.get("validation_verdict") or "")
        if verdict == "regressed":
            return {"ok": False, "code": "evidence_regressed",
                    "error": "效果验证判为退步（regressed），不得发布"}
        if verdict not in self.PUBLISHABLE_VERDICTS:
            return {"ok": False, "code": "no_evidence",
                    "error": f"无效果验证证据（verdict={verdict or '缺失'}）"}

        eff_author = author or str(rev.get("author") or "")
        eff_author_subject = str(rev.get("author_subject") or "")
        if not (eff_author or eff_author_subject):
            return {"ok": False, "code": "no_provenance",
                    "error": "候选作者不可指认（隔离环境先验证双人审核）"}

        approvals = rev.get("approvals") or []
        if not approvals:
            return {"ok": False, "code": "not_approved", "error": "修订未经审批"}
        for a in approvals:
            a_subj, a_name = str(a.get("approver_subject") or ""), str(a.get("approver") or "")
            if (eff_author_subject and a_subj == eff_author_subject) or \
               (eff_author and a_name == eff_author):
                return {"ok": False, "code": "self_approval",
                        "error": "审批主体与候选作者相同（自审）"}
            if not (set(a.get("roles") or []) & self.APPROVER_ROLES):
                return {"ok": False, "code": "not_authorized",
                        "error": f"审批主体无发布审批角色: {a.get('approver')}"}
            if a.get("content_sha256") != rev.get("content_sha256"):
                return {"ok": False, "code": "tampered",
                        "error": "审批后修订内容已被篡改（内容摘要与审批签名不符）"}

        if rid != f"rev-{(rev.get('content_sha256') or '')[:12]}":
            return {"ok": False, "code": "revision_mismatch",
                    "error": f"修订 id 与内容摘要不匹配（版本不匹配）: {rid}"}

        seq = len(self._read_publications()) + 1
        self._append_jsonl(self.publications_path, {
            "action": "publish", "kind": "candidate", "revision_id": rid,
            "base_revision": base_revision or cur, "approver": approver or approver_subject,
            "approver_subject": approver_subject, "author": eff_author or eff_author_subject,
            "gate": "passed", "seq": seq, "published_ts": time.time()})
        pointer = {"revision_id": rid, "seq": seq, "updated_ts": time.time()}
        receipt = {"revision_id": rid, "gate": "passed", "approver": approver or approver_subject,
                   "author": eff_author or eff_author_subject, "base_revision": base_revision or cur,
                   "seq": seq, "published_ts": pointer["updated_ts"]}
        return {"ok": True, "pointer": pointer, "receipt": receipt}

    # ── 回退（只移指针）──────────────────────────────────
    def rollback(self, to_revision: str, *, approver: str = "", approver_subject: str = "") -> Dict[str, Any]:
        pub = self._read_publications()
        known = {p.get("revision_id") for p in pub}
        if to_revision not in known:
            return {"ok": False, "code": "unknown_revision",
                    "error": f"回退目标未在发布历史中出现: {to_revision}"}
        cur = self.pointer()["revision_id"]
        if to_revision == cur:
            return {"ok": False, "code": "already_at_revision",
                    "error": f"指针已在该修订: {to_revision}"}
        seq = len(pub) + 1
        self._append_jsonl(self.publications_path, {
            "action": "rollback", "revision_id": to_revision, "from_revision": cur,
            "approver": approver or approver_subject, "approver_subject": approver_subject,
            "gate": "rollback", "seq": seq, "published_ts": time.time()})
        return {"ok": True, "pointer": {"revision_id": to_revision, "seq": seq,
                                        "updated_ts": time.time()},
                "kept_audit": True}

    # ── 指针 / 历史 / 生效视图 ───────────────────────────
    def pointer(self) -> Dict[str, Any]:
        pub = self._read_publications()
        if not pub:
            return {"revision_id": "", "seq": 0, "updated_ts": 0.0}
        last = pub[-1]
        return {"revision_id": str(last.get("revision_id") or ""),
                "seq": int(last.get("seq") or len(pub)),
                "updated_ts": float(last.get("published_ts") or 0.0)}

    def history(self) -> List[Dict[str, Any]]:
        return self._read_publications()

    def sync_manager(self, mgr: "SkillManager") -> Dict[str, Any]:
        """把当前指针修订覆盖进 SkillManager 生效视图（指针变化即失效重应用——
        缓存/生效条件的失效以版本为准）。指针为空时不动作。"""
        rid = self.pointer()["revision_id"]
        if not rid or rid == getattr(mgr, "_published_revision", ""):
            return {"applied": rid, "changed": []}
        rev = self.revision(rid)
        if rev is None:
            return {"applied": "", "changed": [], "error": "指针修订缺失"}
        return mgr.apply_published({"revision_id": rid, "content": rev.get("content") or {}})

    def apply_published(self, mgr: "SkillManager") -> Dict[str, Any]:
        """对外别名：等效 sync_manager（重启/跨进程后恢复批准版本）。"""
        return self.sync_manager(mgr)

    # ── 请求级冻结与回放 ─────────────────────────────────
    def freeze_request(self, request_id: str, mgr: "SkillManager") -> Dict[str, Any]:
        """冻结请求级快照。缓存键 = f"{request_id}:{revision_id}"（含版本——
        指针变化后同一 request_id 得到新键与新快照，绝不复用旧版本内容）。"""
        self.sync_manager(mgr)
        rid = getattr(mgr, "_published_revision", "")
        key = f"{request_id}:{rid}"
        if key not in self._req_cache:
            self._req_cache[key] = mgr.snapshot_for_request()
        return {"snapshot_key": key, "revision_id": rid,
                "skills_count": len(self._req_cache[key].get("skills") or [])}

    def record_injection(self, request_id: str, revision_id: str,
                         fragments: List[Dict[str, Any]]) -> None:
        """请求处理完成后落盘冻结记录（回放事实源；追加式，同 request_id 取最后一条）。"""
        self._append_jsonl(self.replays_path, {
            "request_id": request_id, "revision_id": revision_id,
            "fragments": list(fragments or []), "recorded_ts": time.time()})

    def replay(self, request_id: str) -> Dict[str, Any]:
        """定位历史请求当时实际注入的版本与片段摘要；正文按不可变修订库取回。

        无冻结记录 / 无版本 / 修订缺失 → version="unknown"（不编造历史）。
        """
        rows = [r for r in self._read_jsonl(self.replays_path)
                if r.get("request_id") == request_id]
        if not rows:
            return {"request_id": request_id, "version": "unknown", "fragments": []}
        last = rows[-1]
        rid = str(last.get("revision_id") or "")
        rev = self.revision(rid) if rid else None
        if not rid or rev is None:
            return {"request_id": request_id, "version": "unknown", "fragments": []}
        content = rev.get("content") or {}
        out = []
        for f in last.get("fragments") or []:
            body = (content.get(str(f.get("skill_name"))) or {}).get("content") or ""
            out.append({**f, "body_sha256": self.sha256_text(body) if body else ""})
        return {"request_id": request_id, "version": rid, "fragments": out}

    def revision_body(self, rid: str, skill_name: Optional[str] = None) -> str:
        """按修订取回批准正文（缺省取该修订的 changed 主 skill）。"""
        rev = self.revision(rid)
        if rev is None:
            return ""
        content = rev.get("content") or {}
        name = skill_name or (rev.get("changed") or [None])[0]
        return (content.get(name) or {}).get("content") or ""

    def debug_state(self) -> Dict[str, Any]:
        """只读调试状态（/skills/debug，独立管理权限）：指针、审计计数、修订摘要、回放规模。"""
        revs = self._read_revisions()
        return {
            "pointer": self.pointer(),
            "history_count": len(self._read_publications()),
            "revisions": [{"revision_id": r["revision_id"], "kind": r.get("kind"),
                           "skill_name": r.get("skill_name"), "changed": r.get("changed"),
                           "content_sha256": r.get("content_sha256"),
                           "approvals": len(r.get("approvals") or []),
                           "validation_verdict": r.get("validation_verdict"),
                           "author": r.get("author"), "author_subject": r.get("author_subject")}
                          for r in revs],
            "request_snapshots": len(self._read_jsonl(self.replays_path)),
        }
