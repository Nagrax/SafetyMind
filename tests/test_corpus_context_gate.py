# -*- coding: utf-8 -*-
# corpus 注入闸门回归测试（2026-10-29 闭环第 33 轮）。
#
# 修复依据（实测，见 api/main.py 里 _gate_corpus_payload 上方的注释）：
#   生产注入 7.57 条引用/题，corpus 通道 126 条被裁判判定 0 条相关；
#   根因是 RRF 排名分不含绝对相关性、向量通道永远返回最近邻 → 没有闸门就注满噪声。
#   采纳规则：至少 N 个非泛用查询词（≥2 字）逐字出现在片段里才注入；
#   同时让 _corpus_knowledge_context 的 top_k 参数真正生效（旧实现注入 6 条）。
#
# 本测试锁定：
#   1. 噪声块被挡掉（used=False，绝不虚报"已检索到"）
#   2. 有词面证据的块保留，且引用序号从 start_index 续接
#   3. top_k 上限生效（不再固定塞 6 条）
#   4. 目录卡同样受闸门约束，且 catalog_only 语义不变
#   5. SAFETYMIND_CORPUS_MIN_TERM_HITS=1 放宽、=0 完全回退旧行为
#   6. 提供者缺失/返回非 dict/空 payload 时不崩、不注入
#
# 用法: python tests/test_corpus_context_gate.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []

Q = "受限空间作业的气体检测合格判定值是多少"


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def block(text, bid="b1", title="测试条目", score=0.03):
    return {"block_id": bid, "version_id": "v1", "evidence_kind": "body",
            "content": text, "token_count": 30, "score": score, "title": title,
            "category": "测试", "status": "active", "access_scope": ""}


def card(text, cid="c1", title="测试目录卡", score=0.03):
    return {"card_id": cid, "version_id": "v2", "evidence_kind": "catalog",
            "title": title, "category": "测试", "text": text, "score": score}


def payload(blocks, cards=None, build_id="bld-1"):
    return {"query": Q, "blocks": blocks, "cards": cards or [],
            "build": {"build_id": build_id}, "budget": {}}


def main():
    from mcp import knowledge_base as kbmod
    import api.main as api

    orig = kbmod.get_corpus_provider()

    def with_provider(fn, **env):
        """注入假 corpus 提供者并设置环境变量，返回清理函数。"""
        saved = {k: os.environ.get(k) for k in env}
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
        kbmod.set_corpus_provider(fn)

        def cleanup():
            kbmod.set_corpus_provider(orig)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        return cleanup

    # ── 1. 锚点词抽取 ────────────────────────────────────────────────────
    terms = api._corpus_query_terms(Q)
    record("锚点词 ≥2 字且去掉泛用词",
           all(len(t) >= 2 for t in terms) and "安全" not in terms and "作业" not in terms,
           f"terms={terms}")
    record("锚点词去重", len(terms) == len(set(terms)), f"terms={terms}")
    record("锚点词至少抽到 2 个信号词",
           len(terms) >= 2, f"terms={terms}")

    # ── 2. 纯噪声 payload → 不注入 ───────────────────────────────────────
    noise = [block("检查项类别：消防系统 / 气体灭火系统。喷口前1.0m内不应存放设备。", f"n{i}")
             for i in range(6)]
    clean = with_provider(lambda q, s: payload(noise), SAFETYMIND_CORPUS_MIN_TERM_HITS="2")
    try:
        text, used, cits = api._corpus_knowledge_context(Q, top_k=2)
        record("纯噪声 corpus 命中不注入", used is False and text == "" and cits == [],
               f"used={used} cits={len(cits)}")
    finally:
        clean()

    # ── 3. 有词面证据的块保留，序号从 start_index 续接 ────────────────────
    good = payload(
        [block("受限空间作业：作业前30分钟内取样分析，氧含量19.5%~21%；气体检测合格判定值见下表。", "g1"),
         block("动火作业票有效期：特殊动火不超过8小时。", "g2")],
        [card("受限空间气体检测：氧含量19.5%~21%，可燃气体不超过爆炸下限10%。", "c1")])
    clean = with_provider(lambda q, s: good, SAFETYMIND_CORPUS_MIN_TERM_HITS="2")
    try:
        text, used, cits = api._corpus_knowledge_context(Q, top_k=2, start_index=3)
        record("有锚点证据的正文块被保留", used is True and len(cits) >= 1,
               f"used={used} cits={len(cits)}")
        record("无关正文块被挡掉（只留命中块）",
               all("动火作业票" not in (c.get("snippet") or "") for c in cits),
               f"snippets={[c.get('snippet', '')[:12] for c in cits]}")
        record("引用序号从 start_index 续接",
               bool(cits) and cits[0]["index"] == 4, f"index={cits[0]['index'] if cits else None}")
        record("build_id 透传进引用",
               all(c.get("build_id") == "bld-1" for c in cits),
               f"build={[c.get('build_id') for c in cits]}")
        record("注入文本带资料库标记", "资料库检索结果" in text, text[:40])
    finally:
        clean()

    # ── 4. top_k 上限生效 ───────────────────────────────────────────────
    many = payload([block(f"受限空间气体检测要求第{i}条：氧含量19.5%~21%。", f"m{i}")
                    for i in range(6)])
    clean = with_provider(lambda q, s: many, SAFETYMIND_CORPUS_MIN_TERM_HITS="2")
    try:
        _, used, cits = api._corpus_knowledge_context(Q, top_k=2)
        record("top_k=2 时最多注入 2 条", used is True and len(cits) == 2,
               f"cits={len(cits)}")
        _, _, cits1 = api._corpus_knowledge_context(Q, top_k=1)
        record("top_k=1 时最多注入 1 条", len(cits1) == 1, f"cits={len(cits1)}")
    finally:
        clean()

    # ── 4b. 默认口径：启用闸门（>=2 个锚点词），噪声注不进来 ──────────────────
    clean = with_provider(lambda q, s: payload(noise), SAFETYMIND_CORPUS_MIN_TERM_HITS=None)
    try:
        _, used_def, cits_def = api._corpus_knowledge_context(Q, top_k=2)
        record("默认（未设开关）启用词面闸门：纯噪声不注入",
               used_def is False and cits_def == [], f"used={used_def} cits={len(cits_def)}")
    finally:
        clean()

    # ── 5. 目录卡受同一闸门约束 ─────────────────────────────────────────
    cards_only = payload([], [card("受限空间气体检测：氧含量19.5%~21%。", "c1"),
                              card("机床操作面间距不应低于1.5m。", "c2")])
    clean = with_provider(lambda q, s: cards_only, SAFETYMIND_CORPUS_MIN_TERM_HITS="2")
    try:
        _, used, cits = api._corpus_knowledge_context(Q, top_k=3)
        record("目录卡同样只放行有证据的条目",
               used is True and len(cits) == 1 and "机床" not in (cits[0].get("snippet") or ""),
               f"cits={[c.get('snippet', '')[:10] for c in cits]}")
        record("目录卡 catalog_only 语义不变",
               bool(cits) and cits[0].get("catalog_only") is True,
               f"catalog_only={cits[0].get('catalog_only') if cits else None}")
    finally:
        clean()

    # ── 6. 开关：=1 放宽、=0 完全回退 ────────────────────────────────────
    # 该块只含 1 个锚点词（气体），不含"受限空间/检测/合格"等其余锚点 → 恰好用于测阈值
    single = payload([block("气体灭火系统应定期检查喷头。", "t1")])
    clean = with_provider(lambda q, s: single, SAFETYMIND_CORPUS_MIN_TERM_HITS="1")
    try:
        _, used1, _ = api._corpus_knowledge_context(Q, top_k=2)
        record("MIN_TERM_HITS=1 时单锚点命中即注入", used1 is True, f"used={used1}")
    finally:
        clean()
    clean = with_provider(lambda q, s: single, SAFETYMIND_CORPUS_MIN_TERM_HITS="2")
    try:
        _, used2, _ = api._corpus_knowledge_context(Q, top_k=2)
        record("MIN_TERM_HITS=2（默认）时单锚点块被拒", used2 is False, f"used={used2}")
    finally:
        clean()
    one_term = payload([block("动火作业票有效期：特殊动火不超过8小时。", "t2")])
    clean = with_provider(lambda q, s: one_term, SAFETYMIND_CORPUS_MIN_TERM_HITS="0")
    try:
        _, used0, cits0 = api._corpus_knowledge_context(Q, top_k=2)
        record("MIN_TERM_HITS=0 完全回退旧行为（不按词面过滤）",
               used0 is True and len(cits0) == 1, f"used={used0} cits={len(cits0)}")
    finally:
        clean()

    # ── 7. 异常/边界 ────────────────────────────────────────────────────
    clean = with_provider(lambda q, s: None, SAFETYMIND_CORPUS_MIN_TERM_HITS=None)
    try:
        record("提供者返回 None 不注入且不崩",
               api._corpus_knowledge_context(Q, top_k=2) == ("", False, []))
    finally:
        clean()
    clean = with_provider(lambda q, s: {"blocks": [], "cards": []},
                          SAFETYMIND_CORPUS_MIN_TERM_HITS=None)
    try:
        record("空 payload 不注入", api._corpus_knowledge_context(Q, top_k=2) == ("", False, []))
    finally:
        clean()
    kbmod.set_corpus_provider(None)
    try:
        record("未注册提供者时静默跳过",
               api._corpus_knowledge_context(Q, top_k=2) == ("", False, []))
    finally:
        kbmod.set_corpus_provider(orig)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
