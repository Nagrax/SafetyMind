# -*- coding: utf-8 -*-
"""K08 通用资料库评测骨架：冻结/开发集 + A / B0 / B80 / B300 四配置对比（离线可跑）。

解决的问题：§8.2 要求"固定模型、提示、题目和指标"比较四个配置，并把**逐组**结果、
阻断项与"新增资料价值"分开披露，而不是用一个总分掩盖空白类。

四个配置（§8.2）：
  A    = 当前版本（既有链路 mcp/knowledge_base）+ 用户既有资料
  B0   = 新链路（corpus/search.py CorpusSearch：FTS + 短目录卡 + 精选正文向量）+ 同一自有资料
  B80  = 同一新链路 + 80 份增量
  B300 = 同一新链路 + 扩展集合
缺增量集时 B80/B300 **如实标 not_applicable**，绝不用 B0 冒充 B80 再说"无差异"。

诚实与纪律：
  - 冻结集只评估不调参：assert_frozen_untouched / select_corpus_thresholds 落成代码守卫；
  - 目录卡命中不得计入正文命中：复用 corpus.catalog 的 assert_not_body_evidence / is_body_evidence；
  - 阻断项（关键阈值 / 错引现行状态）单独成列，不计入普通失分（blocking_failures）；
  - 报告逐组披露 per_group，且把用例集 content_gaps（缺内容题组）一并落盘。

零第三方依赖（corpus / mcp / api 均惰性导入，仅真实链路才需要）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from corpus.catalog import assert_not_body_evidence  # 目录卡守卫（复用，不重写）
from evaluation.e2e_runner import EXPECT_KEYS, _check_expect

logger = logging.getLogger(__name__)

DEFAULT_CASES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cases.json")
DEFAULT_REPORT_DIR = "D:/SafetyMindData/corpus/reports"
DEFAULT_TOP_K = 5

CONFIGS = ("A", "B0", "B80", "B300")
CORPUS_CONFIGS = ("B0", "B80", "B300")
VALID_SPLITS = ("frozen", "dev")
GROUP_HIT_KEY = "单主题定位与解释"

GROUPS = ("单主题定位与解释", "跨资料学习与组织", "版本·数字·引用",
          "复杂格式与结构", "资料发现与原件访问", "缺失·冲突·资料注入")

QUOTA = {
    "frozen": {"单主题定位与解释": 36, "跨资料学习与组织": 24, "版本·数字·引用": 18,
               "复杂格式与结构": 18, "资料发现与原件访问": 12, "缺失·冲突·资料注入": 12},
    "dev": {"单主题定位与解释": 10, "跨资料学习与组织": 6, "版本·数字·引用": 5,
            "复杂格式与结构": 2, "资料发现与原件访问": 2, "缺失·冲突·资料注入": 5},
}

# §8.3：新增资料价值判定阈值
INCREMENT_GAIN_TARGET_PP = 10.0     # B80 相对 B0 任务通过率提升目标（百分点）
PASS_TARGET_WHEN_B0_ABOVE = {"b0_above": 0.85, "b80_target": 0.95}

# 占位/不可答条目所需的"诚实回话"标记
PLACEHOLDER_MARKERS = ("未检索到", "缺", "无相关资料", "无法", "无原件", "仅目录", "未落盘")

# 用例级字段（除 id/group/split/message/expect/label_source 外）
CASE_LIST_FIELDS = ("accept_sources", "must_include", "forbidden", "evidence_set",
                    "expect_any", "blocking_forbidden", "blocking_missing")


# ── 用例集加载与校验 ──────────────────────────────────────────────────────────

def load_document(path: Optional[str] = None) -> Dict[str, Any]:
    """读取用例集文档（cases.json：quotas / content_gaps / cases）。"""
    p = path or os.environ.get("SAFETYMIND_CORPUS_CASES_PATH") or DEFAULT_CASES_PATH
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, Mapping) or not isinstance(doc.get("cases"), list):
        raise ValueError(f"用例集 {p} 结构不对（缺 cases 列表）")
    return dict(doc)


def load_cases(path: Optional[str] = None, *, split: Optional[str] = None,
               validate: bool = True) -> List[Dict[str, Any]]:
    """读取并（默认）校验用例集；split 给定时只返回该 split。"""
    doc = load_document(path)
    cases = [dict(c) for c in doc["cases"]]
    validate_cases(cases, content_gaps=doc.get("content_gaps"),
                   quotas=doc.get("quotas") or QUOTA)
    if split:
        if split not in VALID_SPLITS:
            raise ValueError(f"非法 split={split!r}，只允许 {VALID_SPLITS}")
        cases = [c for c in cases if str(c.get("split")) == split]
    return cases


def _as_list_of_str_or_group(value: Any, cid: str, field: str) -> List[Any]:
    """must_include 的元素允许 str（必需）或 list[str]（任一即可）。"""
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"case {cid} 的 {field} 必须是列表")
    out: List[Any] = []
    for item in value:
        if isinstance(item, str):
            out.append(item)
        elif isinstance(item, (list, tuple)) and all(isinstance(x, str) for x in item):
            out.append(list(item))
        else:
            raise TypeError(f"case {cid} 的 {field} 元素只能是字符串或字符串列表")
    return out


def _as_list_of_str(value: Any, cid: str, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"case {cid} 的 {field} 必须是列表")
    if not all(isinstance(x, str) for x in value):
        raise TypeError(f"case {cid} 的 {field} 元素必须是字符串")
    return list(value)


def validate_cases(cases: Sequence[Mapping[str, Any]], *,
                   content_gaps: Optional[Mapping[str, Any]] = None,
                   quotas: Optional[Mapping[str, Mapping[str, int]]] = None) -> Dict[str, Any]:
    """校验用例集结构与配额（fail-loud）。

    规则：
      - id 唯一非空、group ∈ GROUPS、split ∈ {frozen, dev}、message 非空、label_source 非空；
      - expect 的键必须落在 e2e_runner.EXPECT_KEYS 内（未知键报错）；
      - accept_sources / must_include / forbidden 必须是列表且类型合规；
      - 逐组配额（quotas=None 表示不做配额校验，供注入小样本用例时使用）：
        超出配额报错；**不足配额必须在 content_gaps 里有记录**，否则报错。
    """
    if not isinstance(cases, Sequence) or isinstance(cases, (str, bytes)):
        raise TypeError("cases 必须是序列")
    quotas = dict(quotas) if quotas is not None else None
    gaps = dict(content_gaps or {})
    counted: Dict[str, int] = {}
    seen: set = set()
    out_cases: List[Dict[str, Any]] = []

    for idx, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise TypeError(f"第 {idx} 条 case 必须是映射")
        cid = str(case.get("id") or "").strip()
        if not cid:
            raise ValueError(f"第 {idx} 条 case 缺 id")
        if cid in seen:
            raise ValueError(f"case id 重复：{cid}")
        seen.add(cid)
        group = str(case.get("group") or "").strip()
        if group not in GROUPS:
            raise ValueError(f"case {cid} 的 group={group!r} 非法，只允许 {GROUPS}")
        split = str(case.get("split") or "").strip()
        if split not in VALID_SPLITS:
            raise ValueError(f"case {cid} 的 split={split!r} 非法，只允许 {VALID_SPLITS}")
        message = case.get("message")
        if not isinstance(message, str) or not message.strip():
            raise ValueError(f"case {cid} 缺 message（非空字符串）")
        label_source = str(case.get("label_source") or "").strip()
        if not label_source:
            raise ValueError(f"case {cid} 必须显式标注 label_source")
        expect = case.get("expect") or {}
        if not isinstance(expect, Mapping):
            raise TypeError(f"case {cid} 的 expect 必须是映射")
        unknown = [k for k in expect if k not in EXPECT_KEYS]
        if unknown:
            raise ValueError(f"case {cid} 出现未知 expect 键 {unknown}，只允许 {list(EXPECT_KEYS)}")
        normalized = dict(case)
        normalized["must_include"] = _as_list_of_str_or_group(
            case.get("must_include"), cid, "must_include")
        for field in ("accept_sources", "forbidden", "evidence_set", "expect_any",
                      "blocking_forbidden", "blocking_missing"):
            normalized[field] = _as_list_of_str(case.get(field), cid, field)
        out_cases.append(normalized)
        counted[f"{split}/{group}"] = counted.get(f"{split}/{group}", 0) + 1

    violations: List[str] = []
    undocumented: List[str] = []
    for split, group_quota in (quotas or {}).items():
        for group, quota in group_quota.items():
            key = f"{split}/{group}"
            actual = counted.get(key, 0)
            if actual > quota:
                raise ValueError(f"{key} 实际 {actual} 条 > 配额 {quota} 条（不允许超编）")
            if actual < quota:
                if key not in gaps:
                    undocumented.append(f"{key}（缺 {quota - actual} 条）")
                    continue
                written = gaps[key].get("written")
                if written is not None and int(written) != actual:
                    raise ValueError(
                        f"{key} 的 content_gaps.written={written} 与实际 {actual} 条不一致")
    if undocumented:
        violations.append(
            "以下题组未达配额且 content_gaps 未记录缺口（禁止用编造素材充数）：" + "; ".join(undocumented))
    if violations:
        raise ValueError("; ".join(violations))

    return {"counts": counted, "n": len(out_cases), "content_gaps": sorted(gaps),
            "violations": violations, "cases": out_cases}


# ── 冻结集纪律守卫 ────────────────────────────────────────────────────────────

def assert_frozen_untouched(cases: Optional[Sequence[Mapping[str, Any]]] = None, *,
                            tuning_allowed: bool = False,
                            purpose: str = "evaluate") -> Dict[str, Any]:
    """冻结集只评估不调参：任何"挑资料/回选/改阈值"用途一律拒绝。

    purpose 为 evaluate/compare/report 时放行（只评估）；为 retune/select_sources/select_thresholds
    等调参用途且 tuning_allowed=False 时直接报错（冻结集不得被用于调参）。
    """
    rows = list(cases) if cases is not None else load_cases()
    frozen_ids = [str(c.get("id")) for c in rows if str(c.get("split")) == "frozen"]
    tuning_purposes = {"retune", "tune", "select_sources", "select_thresholds", "pick", "calibrate"}
    if str(purpose) in tuning_purposes and frozen_ids and not tuning_allowed:
        raise ValueError(
            f"拒绝在冻结集上调参（purpose={purpose!r}）：冻结集只评估不调参；"
            "调参请改用 split=dev 的开发集")
    return {"frozen_cases": len(frozen_ids), "purpose": str(purpose),
            "tuning_allowed": bool(tuning_allowed), "untouched": True,
            "note": ("调参在 dev 集上进行；冻结集结果不得回写阈值或回选资料"
                     if not tuning_allowed else "显式允许调参，但仍须单独披露冻结集结论")}


def select_corpus_thresholds(kind: str, report: Mapping[str, Any], *,
                             tuning_allowed: bool = True) -> Dict[str, Any]:
    """阈值/口径只允许在开发集（dev）上选定；冻结集一律拒绝回选（与 §8.2 对应）。"""
    if str(kind) == "frozen":
        raise ValueError("阈值不得在冻结集上回选：冻结集只评估不调参（请用 split=dev）")
    if str(kind) != "dev":
        raise ValueError(f"未知评测集类别 {kind!r}：阈值只允许在 dev 开发集上选定")
    if not tuning_allowed:
        raise ValueError("tuning_allowed=False：本调用被视为调参，已拒绝")
    return {"selected_on": {"kind": "dev", "n": int(len(report.get("cases") or []))},
            "note": "口径只在开发集上选定；冻结集仅用于评估，不得回选"}


# ── 检索通道 ──────────────────────────────────────────────────────────────────

class _CorpusSearcher:
    """corpus/search.py CorpusSearch 的复用包装（缺省 B 系链路）。"""

    def __init__(self, root: Optional[Any] = None):
        self._root = root
        self._cs = None

    def _ensure(self):
        if self._cs is None:
            from corpus.search import CorpusSearch
            self._cs = CorpusSearch(root=self._root)
        return self._cs

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> Dict[str, Any]:
        return self._ensure().search(query, top_k=top_k)

    def close(self) -> None:
        if self._cs is not None:
            try:
                self._cs.close()
            finally:
                self._cs = None


class _KnowledgeBaseSearcher:
    """既有链路（mcp/knowledge_base 单容器父子召回）的离线等价实现。

    仅用于 A 配置的离线对比：把同一批用户自有资料灌进 KnowledgeBase（独立 _test 存储），
    走它自己的 search。语料来源可用 SAFETYMIND_CORPUS_EVAL_BASELINE_SEED 覆盖。
    """

    DEFAULT_SEED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "data", "knowledge_seed", "xlsx_docs.json")

    def __init__(self, seed_path: Optional[str] = None, chroma_path: Optional[str] = None):
        self._seed_path = seed_path or os.environ.get(
            "SAFETYMIND_CORPUS_EVAL_BASELINE_SEED", self.DEFAULT_SEED)
        self._chroma = chroma_path or os.environ.get(
            "SAFETYMIND_CORPUS_EVAL_BASELINE_CHROMA",
            "D:/claudecode/scratch/sm-corpus-eval-baseline/kb_chroma")
        self._kb = None

    def _ensure(self):
        if self._kb is None:
            from mcp.knowledge_base import KnowledgeBase
            kb = KnowledgeBase(chroma_host="127.0.0.1", chroma_port=1,
                               chroma_path=str(self._chroma))
            for col in (kb._collection, kb._parents):
                ids = col.get()["ids"]
                if ids:
                    col.delete(ids=ids)
            docs = json.loads(open(self._seed_path, encoding="utf-8").read())
            kb.add_documents([{"title": str(d.get("title") or ""),
                               "content": str(d.get("content") or "")} for d in docs])
            self._kb = kb
        return self._kb

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> Dict[str, Any]:
        kb = self._ensure()
        items = kb.search(query, top_k=top_k) or []
        blocks = [{"content": str(it.get("content") or it.get("snippet") or ""),
                   "title": str(it.get("title") or ""),
                   "evidence_kind": "body", "legacy": True,
                   "score": it.get("score")} for it in items]
        return {"blocks": blocks, "cards": [], "channel": "knowledge_base"}

    def close(self) -> None:
        self._kb = None


def _unavailable(reason: str) -> Dict[str, Any]:
    return {"blocks": [], "cards": [], "unavailable": reason}


# ── 结果归一化（目录卡与正文分离）────────────────────────────────────────────

def _norm_item_content(item: Mapping[str, Any]) -> str:
    return str(item.get("content") or item.get("snippet") or item.get("text") or "")


def _split_evidence(raw: Any) -> Dict[str, Any]:
    """把各链路返回值统一成 {blocks, cards, invalid}；目录卡绝不混进正文块。"""
    unavailable = ""
    blocks_raw: List[Any] = []
    cards_raw: List[Any] = []
    if raw is None:
        unavailable = "无检索结果"
    elif isinstance(raw, Mapping):
        unavailable = str(raw.get("unavailable") or "")
        if isinstance(raw.get("blocks"), list):
            blocks_raw = list(raw["blocks"])
        elif isinstance(raw.get("results"), list):
            blocks_raw = list(raw["results"])
        if isinstance(raw.get("cards"), list):
            cards_raw = list(raw["cards"])
    elif isinstance(raw, list):
        blocks_raw = list(raw)
    else:
        unavailable = f"未知检索返回类型 {type(raw).__name__}"

    body: List[Dict[str, Any]] = []
    cards: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    for item in cards_raw:
        cards.append(dict(item) if isinstance(item, Mapping) else {"text": str(item)})
    for item in blocks_raw:
        d = dict(item) if isinstance(item, Mapping) else {"content": str(item)}
        kind = d.get("evidence_kind")
        if kind == "catalog":
            # 目录卡被误放进 blocks：按目录卡处理，绝不当作正文证据
            cards.append(d)
            continue
        if kind not in (None, "", "body"):
            invalid.append(d)
            continue
        body.append(d)
    return {"blocks": body, "cards": cards, "invalid": invalid, "unavailable": unavailable}


def _citations_of(blocks: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """正文块 → 引用条目（复用 corpus.catalog 守卫：目录卡不得作为引用）。"""
    cites: List[Dict[str, Any]] = []
    for i, b in enumerate(blocks, start=1):
        assert_not_body_evidence(b if b.get("evidence_kind") else {**b, "evidence_kind": "body"})
        locator = b.get("locator")
        cites.append({
            "index": i,
            "title": str(b.get("title") or ""),
            "version_id": str(b.get("version_id") or ""),
            "locator": locator if isinstance(locator, (Mapping, list)) else {},
            "evidence_kind": "body",
            "block_id": str(b.get("block_id") or ""),
            "build_id": str((b.get("build") or {}).get("build_id") or b.get("build_id") or ""),
            "snippet": _norm_item_content(b),
            "catalog_only": False,
        })
    return cites


# ── 离线可注入的缺省答题器 ────────────────────────────────────────────────────

def _cjk_bigrams(text: str) -> set:
    """CJK 二元组集合（用于离线答题器的相关性判断，确定性、无依赖）。"""
    chars = [c for c in str(text) if "\u4e00" <= c <= "\u9fff"]
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}


STUB_MIN_SHARED_BIGRAMS = 2


def _relevant(query: str, content: str) -> bool:
    """离线答题器的相关性守卫：与问题无实质重合的块不当作证据（避免拿无关正文硬答）。"""
    return len(_cjk_bigrams(query) & _cjk_bigrams(content)) >= STUB_MIN_SHARED_BIGRAMS


def _offline_extractive_stub(message: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
    """缺省答题器：从证据里抽取式作答（不调模型，离线可复跑）。

    只使用正文块（evidence_kind=body）；与问题无实质重合的块不当作证据；
    命中目录卡但无可回取正文时明确"仅目录"；无可用正文证据时如实回"未检索到"，**不编造**。
    """
    cards = [c for c in (evidence.get("cards") or []) if isinstance(c, Mapping)]
    blocks = [b for b in (evidence.get("blocks") or [])
              if isinstance(b, Mapping) and _relevant(message, _norm_item_content(b))]
    body = "\n".join(_norm_item_content(b) for b in blocks[:3])
    if not body.strip():
        note = "现有资料中未检索到与问题相关的内容，无法给出依据，不编造数据。"
        if cards:
            note += "（仅命中目录卡，该版本无可回取正文，属于'仅目录'，不得当作条款引用。）"
        return {"response": note, "citations": [], "knowledge_used": False,
                "escalated": False, "intent": "other", "total_ms": 0.0,
                "request_id": "", "conv_id": "", "hazard_action": "none",
                "emergency_fastpath": False}
    cites = _citations_of(blocks[:3])
    return {"response": body, "citations": cites, "knowledge_used": True,
            "escalated": False, "intent": "other", "total_ms": 0.0,
            "request_id": "", "conv_id": "", "hazard_action": "none",
            "emergency_fastpath": False}


async def _call_chat(chat: Callable[..., Any], message: str, evidence: Mapping[str, Any]) -> Any:
    """调用答题器：支持 (message) 与 (message, evidence) 两种签名，同步/异步均可。"""
    n_pos = 1
    has_kw = False
    try:
        sig = inspect.signature(chat)
        params = list(sig.parameters.values())
        n_pos = len([p for p in params if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)])
        has_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
    except (TypeError, ValueError):
        n_pos, has_kw = 1, False
    res = chat(message, evidence=evidence) if (n_pos >= 2 or has_kw) else chat(message)
    if inspect.isawaitable(res):
        res = await res
    return res


def _get(raw: Any, name: str, default: Any = None) -> Any:
    if isinstance(raw, Mapping):
        return raw.get(name, default)
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        try:
            m = dump()
            if isinstance(m, Mapping) and name in m:
                return m[name]
        except Exception:
            pass
    return getattr(raw, name, default)


# ── 单配置评测 ────────────────────────────────────────────────────────────────

def _answer_blob(trace: Mapping[str, Any]) -> str:
    """答案口径：响应正文 + 其引用片段（必需信息出现在答案或其引用里即可）。"""
    parts = [str(trace.get("response") or "")]
    for c in trace.get("citations") or []:
        if isinstance(c, Mapping):
            parts.append(str(c.get("snippet") or ""))
    return "\n".join(parts)


def _evidence_blob(trace: Mapping[str, Any]) -> str:
    parts = [_norm_item_content(b) for b in (trace.get("retrieval_blocks") or [])]
    for c in trace.get("citations") or []:
        if isinstance(c, Mapping):
            parts.append(str(c.get("snippet") or ""))
    return "\n".join(parts)


def _check_corpus_expect(trace: Mapping[str, Any], case: Mapping[str, Any]) -> List[Dict[str, str]]:
    """corpus 专用断言：must_include / forbidden / evidence_set / 占位诚实回话。"""
    failures: List[Dict[str, str]] = []
    response = str(trace.get("response") or "")
    answer = _answer_blob(trace)
    evidence = _evidence_blob(trace)

    for entry in case.get("must_include") or []:
        if isinstance(entry, list):
            if not any(t in answer for t in entry):
                failures.append({"rule": "must_include", "detail": f"缺少任一必需信息 {entry}"})
        elif entry and entry not in answer:
            failures.append({"rule": "must_include", "detail": f"缺少必需信息 {entry!r}"})

    for token in case.get("forbidden") or []:
        if token and token in response:
            failures.append({"rule": "forbidden", "detail": f"响应出现禁止错误 {token!r}"})

    ev_set = list(case.get("evidence_set") or [])
    if ev_set:
        covered = [t for t in ev_set if t in evidence]
        trace["evidence_recall"] = round(len(covered) / len(ev_set), 4)
        trace["evidence_covered"] = covered
        if str(case.get("probe") or "") == "conflict" and len(covered) < len(ev_set):
            missing = [t for t in ev_set if t not in covered]
            failures.append({"rule": "evidence_set", "detail": f"冲突/多依据题缺基础依据 {missing}"})

    if case.get("placeholder") or case.get("answerable") is False:
        markers = list(case.get("expect_any") or []) or list(PLACEHOLDER_MARKERS)
        if not any(m in response for m in markers):
            failures.append({"rule": "unanswerable_honesty",
                             "detail": "无资料/无原件时应如实回缺口，但响应未体现（疑似编造）"})

    for token in case.get("blocking_forbidden") or []:
        if token and token in response:
            failures.append({"rule": "blocking_forbidden", "blocking": "true",
                             "detail": f"阻断项：响应出现 {token!r}（关键阈值/错引现行状态）"})
    for token in case.get("blocking_missing") or []:
        if token and token not in answer:
            failures.append({"rule": "blocking_missing", "blocking": "true",
                             "detail": f"阻断项：缺少关键数字 {token!r}"})
    return failures


async def _retrieve(ctx: Mapping[str, Any], config: str, query: str) -> Dict[str, Any]:
    if config == "A":
        raw = ctx["baseline"].search(query, DEFAULT_TOP_K)
    else:
        raw = ctx["searchers"][config].search(query, DEFAULT_TOP_K)
    if inspect.isawaitable(raw):
        raw = await raw
    return _split_evidence(raw)


async def _run_config(cases: Sequence[Mapping[str, Any]], *, config: str, chat: Callable,
                      group_by: Callable, ctx: Mapping[str, Any],
                      top_k: int = DEFAULT_TOP_K) -> Dict[str, Any]:
    traces: List[Dict[str, Any]] = []
    blocking: List[Dict[str, Any]] = []
    latencies: List[float] = []
    cat_only_hits = 0

    for case in cases:
        cid = str(case["id"])
        message = str(case["message"])
        t0 = time.perf_counter()
        retr: Dict[str, Any] = {"blocks": [], "cards": [], "unavailable": ""}
        error = ""
        try:
            retr = await _retrieve(ctx, config, message)
        except Exception as ex:                      # 检索失败不中止整轮，如实记错误
            error = f"{type(ex).__name__}: {ex}"
            logger.warning("case %s 检索失败（%s）：%s", cid, config, error)
        retr_ms = round((time.perf_counter() - t0) * 1000, 2)
        latencies.append(retr_ms)

        blocks = retr["blocks"]
        cards = retr["cards"]
        evidence = {"blocks": blocks, "cards": cards, "config": config, "query": message}

        raw: Any = None
        chat_error = ""
        try:
            raw = await _call_chat(chat, message, evidence)
        except Exception as ex:
            chat_error = f"{type(ex).__name__}: {ex}"
            logger.warning("case %s 答题失败：%s", cid, chat_error)

        response = str(_get(raw, "response", "") or "")
        citations = list(_get(raw, "citations", []) or [])
        accept = list(case.get("accept_sources") or [])
        answer = response + "\n" + "\n".join(
            str(c.get("snippet") or "") for c in citations if isinstance(c, Mapping))
        hit_tokens = [t for t in accept if t in answer]
        top5 = blocks[:top_k]

        # 目录卡命中不计入正文命中：仅当正文未命中而卡片命中时记 catalog_only_hits
        top5_has_token = any(
            any(t in _norm_item_content(b) for t in accept) for b in top5) if accept else False
        if accept and not top5_has_token:
            if any(any(t in str(c.get("text") or "") for t in accept) for c in cards):
                cat_only_hits += 1

        trace: Dict[str, Any] = {
            "id": cid, "group": str(group_by(case)), "split": str(case.get("split")),
            "message": message, "config": config,
            "expect": dict(case.get("expect") or {}),
            "expect_intent": "",
            "intent": str(_get(raw, "intent", "") or ""),
            "response": response,
            "citations": citations,
            "tools_used": list(_get(raw, "tools_used", []) or []),
            "knowledge_used": bool(_get(raw, "knowledge_used", False)),
            "escalated": bool(_get(raw, "escalated", False)),
            "hazard_action": str(_get(raw, "hazard_action", "none") or "none"),
            "emergency_fastpath": bool(_get(raw, "emergency_fastpath", False)),
            "total_ms": float(_get(raw, "total_ms", 0.0) or 0.0),
            "retrieval_ms": retr_ms,
            "retrieval_blocks": blocks,
            "retrieval_cards": cards,
            "retrieval_unavailable": retr.get("unavailable") or "",
            "hit_at_5": bool(top5_has_token),
            "citation_tokens_hit": hit_tokens,
            "accept_tokens_in_evidence": bool(
                accept and any(t in _evidence_blob({"retrieval_blocks": blocks}) for t in accept)),
            "probe": str(case.get("probe") or ""),
            "placeholder": bool(case.get("placeholder")),
            "answerable": case.get("answerable") is not False,
            "entry_error": error or chat_error,
        }

        failures: List[Dict[str, str]] = []
        if error or chat_error:
            failures.append({"rule": "entry_error", "detail": error or chat_error})
        failures.extend(_check_expect(trace, trace["expect"]))
        failures.extend(_check_corpus_expect(trace, case))

        trace["blocking_failures"] = [f for f in failures if str(f.get("blocking")) == "true"]
        trace["passed"] = not failures
        trace["failures"] = failures
        traces.append(trace)
        for f in trace["blocking_failures"]:
            blocking.append({"case_id": cid, "group": trace["group"], "rule": f["rule"],
                             "detail": f["detail"]})

    per_group: Dict[str, Dict[str, Any]] = {}
    for t in traces:
        g = t["group"]
        bucket = per_group.setdefault(g, {
            "total": 0, "passed": 0, "failed": 0, "pass_rate": 0.0, "blocking": 0,
            "placeholders": 0, "answerable_total": 0, "answerable_passed": 0,
            "answerable_pass_rate": 0.0, "misses": []})
        bucket["total"] += 1
        if t["passed"]:
            bucket["passed"] += 1
        else:
            bucket["failed"] += 1
            bucket["misses"].append(t["id"])
        if t["blocking_failures"]:
            bucket["blocking"] += 1
        if t["placeholder"]:
            bucket["placeholders"] += 1
        if t["answerable"]:
            bucket["answerable_total"] += 1
            bucket["answerable_passed"] += int(bool(t["passed"]))
    for bucket in per_group.values():
        bucket["pass_rate"] = round(bucket["passed"] / bucket["total"], 4) if bucket["total"] else 0.0
        bucket["answerable_pass_rate"] = (round(bucket["answerable_passed"] / bucket["answerable_total"], 4)
                                          if bucket["answerable_total"] else 0.0)

    hit_cases = [t for t in traces if t["group"] == GROUP_HIT_KEY]
    hit_n = sum(1 for t in hit_cases if t["hit_at_5"])
    recall_cases = [t for t in traces if t.get("evidence_recall") is not None]
    cite_denom = [t for t in traces
                  if t["answerable"] and t["accept_tokens_in_evidence"] and not t["entry_error"]]
    cite_ok = [t for t in cite_denom if t["citation_tokens_hit"]]

    honesty = {"fabrication": 0, "conflict": 0, "injection": 0}
    for t in traces:
        if not t["passed"] and t["probe"] in honesty:
            honesty[t["probe"]] += 1

    return {
        "config": config,
        "applicable": True,
        "total": len(traces),
        "passed": sum(1 for t in traces if t["passed"]),
        "failed": sum(1 for t in traces if not t["passed"]),
        "pass_rate": round(sum(1 for t in traces if t["passed"]) / len(traces), 4) if traces else 0.0,
        "answerable": {
            "total": sum(1 for t in traces if t["answerable"]),
            "passed": sum(1 for t in traces if t["answerable"] and t["passed"]),
            "pass_rate": (round(sum(1 for t in traces if t["answerable"] and t["passed"])
                                / max(1, sum(1 for t in traces if t["answerable"])), 4)),
            "note": "只统计可答条目（排除缺内容导致的占位/不可答条目）",
        },
        "per_group": per_group,
        "hit_at_5": {"group": GROUP_HIT_KEY, "n": len(hit_cases), "hits": hit_n,
                     "rate": round(hit_n / len(hit_cases), 4) if hit_cases else 0.0,
                     "catalog_only_hits": cat_only_hits,
                     "note": "目录卡命中不计入正文命中"},
        "evidence_set_recall": {
            "n_questions": len(recall_cases),
            "avg_recall": round(sum(t["evidence_recall"] for t in recall_cases) / len(recall_cases), 4)
            if recall_cases else 0.0,
            "per_case": [{"id": t["id"], "recall": t["evidence_recall"],
                          "covered": t.get("evidence_covered")} for t in recall_cases],
        },
        "citation_locator_accuracy": {
            "n": len(cite_denom),
            "ok": len(cite_ok),
            "rate": round(len(cite_ok) / len(cite_denom), 4) if cite_denom else 0.0,
            "note": "普通引用定位：答案或引用中包含可接受出处（accept_sources）",
        },
        "blocking_failures": blocking,
        "blocking_failure_count": len(blocking),
        "honesty_failures": honesty,
        "latency": {"excludes_llm": True, **_latency_summary(latencies)},
        "cases": traces,
    }


def _percentile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(max(int(len(sorted_vals) * q / 100.0), 0), len(sorted_vals) - 1)
    return round(float(sorted_vals[idx]), 2)


def _latency_summary(vals: Sequence[float]) -> Dict[str, Any]:
    s = sorted(float(v) for v in vals)
    return {"n": len(s), "mean": round(sum(s) / len(s), 2) if s else 0.0,
            "p50": _percentile(s, 50), "p95": _percentile(s, 95),
            "max": round(max(s), 2) if s else 0.0}


async def _latency_concurrency(ctx: Mapping[str, Any], cases: Sequence[Mapping[str, Any]],
                               config: str, workers: int) -> Dict[str, Any]:
    """并发 workers 的检索延迟探针（只测检索，不含 LLM）。"""
    sem = asyncio.Semaphore(max(1, int(workers)))
    lat: List[float] = []

    async def one(case):
        async with sem:
            t0 = time.perf_counter()
            await _retrieve(ctx, config, str(case["message"]))
            lat.append((time.perf_counter() - t0) * 1000)

    await asyncio.gather(*[one(c) for c in cases])
    return _latency_summary(lat)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def _default_group_by(case: Mapping[str, Any]) -> str:
    return str(case.get("group") or "other")


async def run_ab(cases: Optional[Sequence[Mapping[str, Any]]] = None, *,
                 group_by: Optional[Callable[[Mapping[str, Any]], str]] = None,
                 chat_fn: Optional[Callable[..., Any]] = None,
                 corpus_search_fn: Optional[Callable[..., Any]] = None,
                 configs: Sequence[str] = CONFIGS,
                 out_path: Optional[str] = None,
                 baseline_search_fn: Optional[Callable[..., Any]] = None,
                 incremental_sources: Optional[Mapping[str, Any]] = None,
                 content_gaps: Optional[Mapping[str, Any]] = None,
                 tuning_allowed: bool = False,
                 latency_probe_n: int = 26,
                 top_k: int = DEFAULT_TOP_K) -> Dict[str, Any]:
    """跑 A / B0 / B80 / B300 四配置对比，返回逐组披露的报告（可落盘）。

    cases=None 时从 cases.json 读取（并连带 content_gaps 一起披露）。
    chat_fn 可注入：缺省 = 离线抽取式答题器（不调真实模型）；真实模型入口待额度恢复后
      由调用方注入（同一命令复跑）。
    corpus_search_fn=None 时缺省走 corpus/search.py CorpusSearch（B 系链路）。
    incremental_sources={"B80": <root 或 search_fn>, "B300": ...}：给了才跑 B80/B300，
      否则如实标 not_applicable（不用 B0 冒充）。
    """
    doc = None
    quotas = None
    if cases is None:
        doc = load_document()
        cases = doc["cases"]
        quotas = doc.get("quotas") or QUOTA
        if content_gaps is None:
            content_gaps = doc.get("content_gaps")
    cases = validate_cases(cases, content_gaps=content_gaps, quotas=quotas)["cases"]
    group_by = group_by or _default_group_by
    chat = chat_fn if chat_fn is not None else _offline_extractive_stub
    entry = {
        "chat_fn": ("offline-extractive-stub（不调模型，离线可复跑）" if chat_fn is None
                    else f"injected:{getattr(chat_fn, '__name__', type(chat_fn).__name__)}"),
        "corpus_channel": ("corpus/search.py CorpusSearch（缺省）" if corpus_search_fn is None
                           else f"injected:{getattr(corpus_search_fn, '__name__', 'fn')}"),
        "baseline_channel": ("mcp/knowledge_base（缺省 A 链路）" if baseline_search_fn is None
                             else f"injected:{getattr(baseline_search_fn, '__name__', 'fn')}"),
    }
    if tuning_allowed:
        entry["warning"] = "tuning_allowed=True：调参须只用 dev 集，冻结集结果不得回写阈值"

    # 组装检索上下文
    searchers: Dict[str, Any] = {}
    searcher_kind: Dict[str, str] = {}
    for name in CORPUS_CONFIGS:
        inc = (incremental_sources or {}).get(name)
        if name != "B0" and inc is None:
            continue
        if callable(inc) or (corpus_search_fn is not None and name == "B0"):
            fn = inc if callable(inc) else corpus_search_fn
            searchers[name] = _FnSearcher(fn)
            searcher_kind[name] = "injected"
        elif inc is not None:
            searchers[name] = _CorpusSearcher(root=inc)
            searcher_kind[name] = f"corpus:{inc}"
        else:
            searchers[name] = _CorpusSearcher()
            searcher_kind[name] = "corpus:default"
    baseline = _FnSearcher(baseline_search_fn) if baseline_search_fn is not None \
        else _KnowledgeBaseSearcher()
    ctx = {"searchers": searchers, "baseline": baseline}

    configs_report: Dict[str, Any] = {}
    try:
        for name in configs:
            if name not in CONFIGS:
                raise ValueError(f"未知配置 {name!r}，只允许 {CONFIGS}")
            if name == "B0" or name == "A" or name in searchers:
                report = await _run_config(cases, config=name, chat=chat, group_by=group_by,
                                           ctx=ctx, top_k=top_k)
                if name in searchers or name == "A":
                    report["searcher"] = "knowledge_base" if name == "A" else searcher_kind.get(name, "")
                # 并发 1 与 2 的检索 p95（不含 LLM）
                probe = list(cases)[:max(1, int(latency_probe_n))]
                c1 = await _latency_concurrency(ctx, probe, name, 1)
                c2 = await _latency_concurrency(ctx, probe, name, 2)
                report["latency"] = {"concurrency_1": c1, "concurrency_2": c2,
                                     "excludes_llm": True,
                                     "note": "仅检索通道耗时；整个问答耗时另报"}
                configs_report[name] = report
            else:
                configs_report[name] = {
                    "config": name, "applicable": False,
                    "not_applicable_reason": "增量集未取得（0 份）：不得用 B0 冒充本配置",
                    "total": 0, "passed": 0, "pass_rate": None,
                    "per_group": {}, "blocking_failures": [],
                    "honesty_failures": {"fabrication": 0, "conflict": 0, "injection": 0},
                }
    finally:
        for s in list(searchers.values()):
            try:
                s.close()
            except Exception:
                pass
        try:
            baseline.close()
        except Exception:
            pass

    report: Dict[str, Any] = {
        "generated_ts": datetime.now().isoformat(timespec="seconds"),
        "entry": entry,
        "configs": configs_report,
        "cases_total": len(cases),
        "frozen_cases": sum(1 for c in cases if str(c.get("split")) == "frozen"),
        "dev_cases": sum(1 for c in cases if str(c.get("split")) == "dev"),
        "content_gaps": dict(content_gaps or {}),
        "tuning_allowed": bool(tuning_allowed),
        "notes": [
            "冻结集只评估不调参；调参只用 dev 集。",
            "目录卡命中不计入正文命中（复用 corpus.catalog 守卫）。",
            "阻断项（关键阈值/错引现行状态）单独成列，不并入普通失分。",
            "增量集未取得时 B80/B300 标 not_applicable，不用 B0 冒充。",
            "占位/不可答条目（缺内容题组）的通过率对'检索不到就不作答'的系统天然友好，"
            "不作为效果证据：任务成功率同时给 answerable（仅可答条目）口径。",
            "离线证据：chat_fn 缺省为离线抽取式答题器，不调真实模型；真实模型受账号额度阻塞。",
        ],
    }
    report["comparisons"] = compare_configs(report)
    if out_path:
        report["report_path"] = _write_report(report, out_path)
    return report


class _FnSearcher:
    """把注入的 search 可调用对象包装成统一检索器。"""

    def __init__(self, fn: Callable[..., Any]):
        self._fn = fn

    def search(self, query: str, top_k: int = DEFAULT_TOP_K) -> Any:
        try:
            return self._fn(query, top_k=top_k)
        except TypeError:
            return self._fn(query)

    def close(self) -> None:
        close = getattr(self._fn, "close", None)
        if callable(close):
            close()


# ── 配置对比（§8.3 新增资料价值）───────────────────────────────────────────────

def _cfg_rate(report: Mapping[str, Any], name: str) -> Optional[float]:
    return (report.get("configs") or {}).get(name, {}).get("pass_rate")


def _cfg_metric(report: Mapping[str, Any], name: str, path: Sequence[str]) -> Optional[float]:
    node: Any = (report.get("configs") or {}).get(name)
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node if isinstance(node, (int, float)) else None


def _delta(report: Mapping[str, Any], a: str, b: str) -> Dict[str, Any]:
    pa, pb = _cfg_rate(report, a), _cfg_rate(report, b)
    ha = _cfg_metric(report, a, ("hit_at_5", "rate"))
    hb = _cfg_metric(report, b, ("hit_at_5", "rate"))
    ra = _cfg_metric(report, a, ("evidence_set_recall", "avg_recall"))
    rb = _cfg_metric(report, b, ("evidence_set_recall", "avg_recall"))
    aa = _cfg_metric(report, a, ("answerable", "pass_rate"))
    ab = _cfg_metric(report, b, ("answerable", "pass_rate"))
    out = {"from": a, "to": b, "applicable": pa is not None and pb is not None}
    if out["applicable"]:
        out.update({
            "pass_rate_delta_pp": round((pb - pa) * 100, 2),
            "pass_rate": {"from": pa, "to": pb},
            "answerable_pass_rate": {"from": aa, "to": ab},
            "answerable_delta_pp": None if aa is None or ab is None else round((ab - aa) * 100, 2),
            "hit_at_5_delta_pp": None if ha is None or hb is None else round((hb - ha) * 100, 2),
            "evidence_recall_delta": None if ra is None or rb is None else round(rb - ra, 4),
        })
    return out


def compare_configs(report: Mapping[str, Any]) -> Dict[str, Any]:
    """A→B0（链路改造收益）与 B0→B80→B300（增量资料收益）的差值 + §8.3 判定。"""
    cfgs = report.get("configs") or {}
    link = _delta(report, "A", "B0")
    incr80 = _delta(report, "B0", "B80")
    incr300 = _delta(report, "B80", "B300")
    b0 = _cfg_rate(report, "B0")
    b80 = _cfg_rate(report, "B80")

    if not (cfgs.get("B80") or {}).get("applicable"):
        verdict = {
            "applicable": False,
            "status": "not_applicable",
            "reason": "增量集未取得（B80/B300 未运行）：新增资料价值无法判定",
            "target_gain_pp": INCREMENT_GAIN_TARGET_PP,
            "target_when_b0_above": dict(PASS_TARGET_WHEN_B0_ABOVE),
        }
    else:
        gain_pp = (b80 - b0) * 100 if (b0 is not None and b80 is not None) else None
        met_gain = gain_pp is not None and gain_pp >= INCREMENT_GAIN_TARGET_PP
        blocked = bool((cfgs.get("B80") or {}).get("blocking_failure_count"))
        b0_above = b0 is not None and b0 > PASS_TARGET_WHEN_B0_ABOVE["b0_above"]
        met_high = b0_above and b80 is not None and b80 >= PASS_TARGET_WHEN_B0_ABOVE["b80_target"]
        no_regress = (b80 is not None and b0 is not None and b80 >= b0)
        verdict = {
            "applicable": True,
            "status": "met" if ((met_gain or met_high) and no_regress and not blocked) else "not_met",
            "gain_pp": None if gain_pp is None else round(gain_pp, 2),
            "target_gain_pp": INCREMENT_GAIN_TARGET_PP,
            "b0_above_threshold": b0_above,
            "high_target_met": met_high,
            "no_regression": no_regress,
            "blocking_failure_count": int((cfgs.get("B80") or {}).get("blocking_failure_count") or 0),
        }
    return {"link_upgrade": link, "increment_B80": incr80, "increment_B300": incr300,
            "increment_value_verdict": verdict}


# ── 报告落盘 ──────────────────────────────────────────────────────────────────

def _assert_on_d(path: str) -> str:
    drive = os.path.splitdrive(os.path.abspath(path))[0].upper()
    if drive and drive != "D:":
        raise ValueError(f"评测报告只允许落 D 盘，收到：{path}")
    return path


def _write_report(report: Mapping[str, Any], out_path: str) -> str:
    path = str(out_path)
    if os.path.isdir(path) or not os.path.splitext(path)[1]:
        os.makedirs(path, exist_ok=True)
        path = os.path.join(path, f"corpus_eval_ab_{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
    _assert_on_d(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return path
