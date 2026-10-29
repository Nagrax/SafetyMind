# -*- coding: utf-8 -*-
"""人标 golden 与 Judge 一致性（Cohen's κ）+ 分歧裁决（E-06 / S-06 报告口径）。

解决的问题：LLM-as-Judge 与程序化 rubric 都只是近似；发布结论不能只看"总均分"。
本模块把"人工判读标签（golden）"作为独立口径，与 Judge 逐条对齐，输出混淆矩阵、
Cohen's κ、准确率与逐条分歧清单，并支持对分歧的**留痕裁决**（保留原始标签）。

诚实口径（落成代码，不靠口头约定）：
  1. golden 由 `label_kind` 显式区分：`project_golden`（本项目按 rubric 逐条人工判读，
     **非**领域专家）与将来可扩展的 `domain_expert`（领域专家盲测）；`synthetic`
     表示合成/断言标签。缺 `label_kind`/`labeler` 一律报错（fail-loud）。
  2. `report()` **分别**报告 project_golden 与 synthetic 口径，**不合并成一个数**；
     并给出 κ 的 bootstrap 置信区间与准确率的 Wilson 区间（样本量与不确定性可见）。
  3. 分歧裁决必须留痕：记录保持 `original_label` 不被覆盖，只有显式
     `write_back=True` 的裁决才被视为"可回写 golden"（本模块自身绝不改写 golden 文件）。

零第三方依赖（复用 `evaluation.judge_calibration.cohens_kappa`，纯 stdlib）。
"""
import json
import os
import random
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from evaluation.judge_calibration import cohens_kappa

__all__ = [
    "DEFAULT_GOLDEN_PATH",
    "VALID_LABELS",
    "VALID_LABEL_KINDS",
    "DEFAULT_THRESHOLD",
    "load_golden",
    "compare_with_judge",
    "adjudicate",
    "report",
    "wilson_interval",
    "bootstrap_kappa_ci",
]

DEFAULT_GOLDEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "golden_labels.json")

VALID_LABELS = ("pass", "fail")
VALID_LABEL_KINDS = ("project_golden", "domain_expert", "synthetic")
DEFAULT_THRESHOLD = 0.75

# Judge 二值化的正类取 "fail"（即"检测到质量不合格"）；混淆项均按此定义：
#   tp = golden fail & judge fail（一致检出不合格）
#   fp = golden pass & judge fail（judge 误报：把合格判成不合格）
#   fn = golden fail & judge pass（judge 漏报：把不合格判成合格）
#   tn = golden pass & judge pass
DISAGREE_FALSE_ALARM = "judge_false_alarm"   # golden pass，judge fail（误报不合格）
DISAGREE_MISS = "judge_miss"                 # golden fail，judge pass（漏报不合格）

DISCLAIMER_PREFIX = (
    "标签为项目内按规则人工判读（label_kind=project_golden），不是领域专家盲测；"
    "与程序化判定同源、可能共享盲点；domain_expert 标签仍为未完成项。"
)


# ── 加载与校验 ────────────────────────────────────────────────────────────────

def load_golden(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """读取人标 golden（默认 evaluation/golden_labels.json）。

    校验（fail-loud，任何缺项直接报错）：
      - 顶层必须有 `provenance` 与 `disclaimer`（非空）；
      - 每条必须有 case_id（唯一、非空）、message（非空）、label ∈ {pass,fail}、
        **labeler 与 label_kind**（缺任一直接报错）、note（非空）与响应证据
        （`response_excerpt` 或 `response_fingerprint` 至少一项非空）。
    """
    p = path or os.environ.get("SAFETYMIND_GOLDEN_LABELS_PATH") or DEFAULT_GOLDEN_PATH
    with open(p, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, Mapping):
        raise TypeError(f"golden 文件 {p} 顶层必须是对象")
    for key in ("provenance", "disclaimer"):
        val = data.get(key)
        if not (isinstance(val, str) and val.strip()) and not isinstance(val, Mapping):
            raise ValueError(f"golden 文件 {p} 缺顶层非空字段 `{key}`")
    raw = data.get("labels")
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"golden 文件 {p} 的 `labels` 为空或结构不对")

    out: List[Dict[str, Any]] = []
    seen = set()
    for i, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise TypeError(f"第 {i} 条 golden 必须是映射")
        cid = str(item.get("case_id") or "").strip()
        if not cid:
            raise ValueError(f"第 {i} 条 golden 缺 case_id")
        if cid in seen:
            raise ValueError(f"golden case_id 重复：{cid}")
        seen.add(cid)
        msg = item.get("message")
        if not isinstance(msg, str) or not msg.strip():
            raise ValueError(f"golden case {cid} 缺 message（非空字符串）")
        label = str(item.get("label") or "").strip()
        if label not in VALID_LABELS:
            raise ValueError(f"golden case {cid} 的 label={label!r} 非法，只允许 {VALID_LABELS}")
        labeler = str(item.get("labeler") or "").strip()
        if not labeler:
            raise ValueError(f"golden case {cid} 缺 labeler（谁判读的）")
        kind = str(item.get("label_kind") or "").strip()
        if not kind:
            raise ValueError(f"golden case {cid} 缺 label_kind（project_golden/domain_expert/synthetic）")
        if kind not in VALID_LABEL_KINDS:
            raise ValueError(f"golden case {cid} 的 label_kind={kind!r} 非法，只允许 {VALID_LABEL_KINDS}")
        excerpt = str(item.get("response_excerpt") or "").strip()
        fingerprint = str(item.get("response_fingerprint") or "").strip()
        if not (excerpt or fingerprint):
            raise ValueError(f"golden case {cid} 缺响应证据（response_excerpt 或 response_fingerprint）")
        note = str(item.get("note") or "").strip()
        if not note:
            raise ValueError(f"golden case {cid} 缺 note（须写明响应来源/判读口径）")
        out.append({
            "case_id": cid,
            "message": msg,
            "label": label,
            "labeler": labeler,
            "label_kind": kind,
            "rubric_rule_hit": str(item.get("rubric_rule_hit") or ""),
            "note": note,
            "response_excerpt": excerpt,
            "response_fingerprint": fingerprint,
        })
    return out


# ── golden（人标）vs Judge ─────────────────────────────────────────────────────

def _judge_value_for(judge_scores: Any, idx: int, case_id: str, missing: List[str]) -> Any:
    """支持 Mapping{case_id→score} 与等长序列两种入参；缺项记入 missing 并返回 None。"""
    if isinstance(judge_scores, Mapping):
        if case_id not in judge_scores:
            missing.append(case_id)
            return None
        return judge_scores[case_id]
    try:
        if idx >= len(judge_scores):
            missing.append(case_id)
            return None
        return judge_scores[idx]
    except TypeError:
        missing.append(case_id)
        return None


def compare_with_judge(golden: Sequence[Mapping[str, Any]],
                       judge_scores: Any,
                       *, threshold: float = DEFAULT_THRESHOLD) -> Dict[str, Any]:
    """把 golden 人标与 Judge 分（按可配阈值二值化）逐条对齐。

    返回 {threshold, n, n_missing, missing, confusion{tp,fp,tn,fn}, kappa, accuracy,
          disagreements[{case_id, message, golden, judge_value, judge, disagreement_type}]}。
    judge_scores 可为 {case_id: 0~1} 或与 golden 等长序列。
    """
    threshold = float(threshold)
    pairs: List[Tuple[str, str, str, float]] = []   # (case_id, golden, judge_bin, judge_val)
    missing: List[str] = []
    for i, g in enumerate(golden):
        cid = str(g["case_id"])
        val = _judge_value_for(judge_scores, i, cid, missing)
        if val is None:
            continue
        try:
            fval = float(val)
        except (TypeError, ValueError):
            missing.append(cid)
            continue
        jbin = "pass" if fval >= threshold else "fail"
        pairs.append((cid, str(g["label"]), jbin, fval))

    tp = fp = tn = fn = 0
    disagreements: List[Dict[str, Any]] = []
    by_id = {str(g["case_id"]): g for g in golden}
    for cid, glab, jbin, fval in pairs:
        if glab == "fail" and jbin == "fail":
            tp += 1
        elif glab == "pass" and jbin == "fail":
            fp += 1
            disagreements.append(_disagreement(cid, glab, fval, jbin, by_id.get(cid, {})))
        elif glab == "fail" and jbin == "pass":
            fn += 1
            disagreements.append(_disagreement(cid, glab, fval, jbin, by_id.get(cid, {})))
        else:
            tn += 1

    n = len(pairs)
    accuracy = (tp + tn) / n if n else 0.0
    kappa = cohens_kappa([1 if glab == "fail" else 0 for _, glab, _, _ in pairs],
                         [1 if jbin == "fail" else 0 for _, _, jbin, _ in pairs]) if n else 0.0

    return {
        "threshold": threshold,
        "n": n,
        "n_missing": len(missing),
        "missing": missing,
        "confusion": {"tp": tp, "fp": fp, "tn": tn, "fn": fn},
        "kappa": kappa,
        "accuracy": accuracy,
        "disagreements": disagreements,
    }


def _disagreement(case_id: str, golden_label: str, judge_value: float,
                  judge_bin: str, entry: Mapping[str, Any]) -> Dict[str, Any]:
    kind = DISAGREE_FALSE_ALARM if golden_label == "pass" else DISAGREE_MISS
    return {
        "case_id": case_id,
        "message": str(entry.get("message") or ""),
        "label_kind": str(entry.get("label_kind") or ""),
        "golden": golden_label,
        "judge": judge_bin,
        "judge_value": judge_value,
        "disagreement_type": kind,
    }


# ── 分歧裁决（留痕，不覆盖原始标签）──────────────────────────────────────────

def adjudicate(disagreements: Sequence[Mapping[str, Any]],
               decisions: Mapping[str, Mapping[str, Any]]) -> Dict[str, Any]:
    """对分歧逐条裁决并留痕。

    decisions: {case_id: {"decided_by","basis","final_label":"pass"/"fail","write_back":bool}}
    - 每条裁决记录都必须保留 `original_label`（= 分歧时的 golden 标签），**绝不静默覆盖**；
    - 未给出裁决的分歧进入 `pending`，不会被"顺手"改写；
    - `write_back=True` 才表示该裁决可回写 golden 文件（本模块自身不改文件）；
    - 未在分歧清单里的 case_id 出现于 decisions → 视为非法输入直接报错。

    返回 {n, adjudicated, pending, records, original_preserved}。
    """
    d_ids = [str(d["case_id"]) for d in disagreements]
    unknown = [cid for cid in decisions if cid not in set(d_ids)]
    if unknown:
        raise ValueError(f"decisions 含非分歧 case_id：{unknown}（只能裁决分歧清单内的条目）")

    records: List[Dict[str, Any]] = []
    pending: List[str] = []
    for d in disagreements:
        cid = str(d["case_id"])
        dec = decisions.get(cid)
        if dec is None or not isinstance(dec, Mapping):
            pending.append(cid)
            continue
        final_label = str(dec.get("final_label") or "").strip()
        if final_label not in VALID_LABELS:
            raise ValueError(f"裁决 case {cid} 的 final_label={final_label!r} 非法，只允许 {VALID_LABELS}")
        decided_by = str(dec.get("decided_by") or "").strip()
        basis = str(dec.get("basis") or "").strip()
        if not decided_by or not basis:
            raise ValueError(f"裁决 case {cid} 必须给出 decided_by 与 basis（留痕）")
        original = str(d["golden"])
        records.append({
            "case_id": cid,
            "original_label": original,
            "final_label": final_label,
            "changed": final_label != original,
            "judge": str(d.get("judge") or ""),
            "judge_value": d.get("judge_value"),
            "disagreement_type": str(d.get("disagreement_type") or ""),
            "decided_by": decided_by,
            "basis": basis,
            "write_back": bool(dec.get("write_back")),
        })

    golden_by_id = {str(d["case_id"]): str(d["golden"]) for d in disagreements}
    preserved = all(r["original_label"] == golden_by_id[r["case_id"]] for r in records)
    return {
        "n": len(disagreements),
        "adjudicated": len(records),
        "pending": pending,
        "records": records,
        "original_preserved": preserved,
    }


# ── 不确定性 ──────────────────────────────────────────────────────────────────

def wilson_interval(successes: int, n: int, z: float = 1.96) -> Dict[str, float]:
    """Wilson 区间（准确率的二项置信区间，小样本下优于正态近似）。"""
    if n <= 0:
        return {"low": 0.0, "high": 0.0}
    p = successes / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = (z * (p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return {"low": max(0.0, centre - margin), "high": min(1.0, centre + margin)}


def bootstrap_kappa_ci(pairs: Sequence[Tuple[str, str]], *,
                       n_resamples: int = 2000, seed: int = 20261019,
                       alpha: float = 0.05) -> Dict[str, Any]:
    """对 (golden, judge) 二值配对做 percentile bootstrap，给出 κ 的置信区间。

    样本不足（< 2）或重采样退化时区间为 [0,0] 并标记 degenerate=True。
    """
    n = len(pairs)
    if n < 2:
        return {"low": 0.0, "high": 0.0, "n_resamples": 0, "degenerate": True}
    rng = random.Random(seed)
    vals: List[float] = []
    for _ in range(int(n_resamples)):
        samp = [pairs[rng.randrange(n)] for _ in range(n)]
        vals.append(cohens_kappa([1 if g == "fail" else 0 for g, _ in samp],
                                 [1 if j == "fail" else 0 for _, j in samp]))
    vals.sort()
    lo_idx = max(0, int(len(vals) * (alpha / 2)) - 1)
    hi_idx = min(len(vals) - 1, int(len(vals) * (1 - alpha / 2)))
    return {"low": vals[lo_idx], "high": vals[hi_idx],
            "n_resamples": int(n_resamples), "degenerate": False}


def _scope_stats(golden_subset: Sequence[Mapping[str, Any]], judge_scores: Any,
                 threshold: float, bootstrap_n: int, seed: int) -> Dict[str, Any]:
    """单个 label_kind 口径的完整统计（含不确定性），供 report 分口径输出。"""
    cmp_res = compare_with_judge(golden_subset, judge_scores, threshold=threshold)
    n = cmp_res["n"]
    conf = cmp_res["confusion"]
    correct = conf["tp"] + conf["tn"]
    all_pairs = _all_pairs(golden_subset, judge_scores, threshold)
    ci = bootstrap_kappa_ci(all_pairs, n_resamples=bootstrap_n, seed=seed)
    return {
        "n": n,
        "n_missing": cmp_res["n_missing"],
        "missing": cmp_res["missing"],
        "label_kinds": sorted({str(g["label_kind"]) for g in golden_subset}),
        "confusion": conf,
        "kappa": cmp_res["kappa"],
        "kappa_ci95": {"low": ci["low"], "high": ci["high"],
                       "method": "percentile_bootstrap",
                       "n_resamples": ci["n_resamples"], "degenerate": ci["degenerate"]},
        "accuracy": cmp_res["accuracy"],
        "accuracy_ci95": wilson_interval(correct, n),
        "disagreements": cmp_res["disagreements"],
        "disagreement_count": len(cmp_res["disagreements"]),
        "threshold": threshold,
    }


def _all_pairs(golden_subset: Sequence[Mapping[str, Any]], judge_scores: Any,
               threshold: float) -> List[Tuple[str, str]]:
    pairs: List[Tuple[str, str]] = []
    for i, g in enumerate(golden_subset):
        miss: List[str] = []
        val = _judge_value_for(judge_scores, i, str(g["case_id"]), miss)
        if val is None:
            continue
        jbin = "pass" if float(val) >= threshold else "fail"
        pairs.append((str(g["label"]), jbin))
    return pairs


# ── 汇总报告（分口径，不合并）─────────────────────────────────────────────────

def report(golden: Sequence[Mapping[str, Any]],
           judge_scores: Any,
           *, threshold: float = DEFAULT_THRESHOLD,
           adjudications: Optional[Mapping[str, Any]] = None,
           synthetic: Optional[Sequence[Mapping[str, Any]]] = None,
           bootstrap_n: int = 2000,
           seed: int = 20261019,
           provenance: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """按 label_kind **分口径**汇总，给出样本来源与不确定性；绝不输出混合单值。

    - `scopes`：golden 里每个 label_kind 一个独立统计块（project_golden / domain_expert）；
    - `synthetic`：可选传入合成/断言标签记录（各含 case_id/label），**单独**成块报告；
    - `adjudications`：adjudicate() 的返回（若给出则原样附带）。
    """
    by_kind: Dict[str, List[Mapping[str, Any]]] = {}
    for g in golden:
        by_kind.setdefault(str(g["label_kind"]), []).append(g)

    scopes: Dict[str, Any] = {}
    for kind, subset in sorted(by_kind.items()):
        scopes[kind] = _scope_stats(subset, judge_scores, threshold, bootstrap_n, seed)

    synthetic_block: Optional[Dict[str, Any]] = None
    if synthetic:
        synth_subset = [{"case_id": str(s.get("case_id")), "message": str(s.get("message") or ""),
                         "label": str(s.get("label")), "labeler": str(s.get("labeler") or "synthetic"),
                         "label_kind": "synthetic",
                         "rubric_rule_hit": str(s.get("rubric_rule_hit") or ""),
                         "note": str(s.get("note") or "合成/断言标签"),
                         "response_excerpt": "", "response_fingerprint": "synthetic"}
                        for s in synthetic]
        synthetic_block = _scope_stats(synth_subset, judge_scores, threshold, bootstrap_n, seed)
        synthetic_block["label_source"] = sorted({str(s.get("label_source") or "synthetic")
                                                  for s in synthetic})

    sample_sources = sorted({str(g["note"]) for g in golden}) if len(golden) <= 40 else []
    return {
        "threshold": float(threshold),
        "n_golden": len(golden),
        "sample_sources": sample_sources,
        "label_kinds_present": sorted(by_kind),
        "scopes": scopes,
        "synthetic": synthetic_block,
        "adjudications": dict(adjudications) if adjudications is not None else None,
        "provenance": dict(provenance or {}),
        "disclaimer": DISCLAIMER_PREFIX,
        "note": "scopes 与 synthetic 是**彼此独立的口径**，不得合并为单一指标；"
                "domain_expert 尚未产出（阻塞项）。",
    }
