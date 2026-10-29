# -*- coding: utf-8 -*-
# K06 切块 / FTS / 目录卡 单元测试（服务 venv 与解析工具链 venv 均可运行）：
# 用法: python tests/test_corpus_chunk_fts.py
# 覆盖：token 计量与 180~220 目标、超长句硬切、表格行组重复表头、locator 继承；
#       中文二字词/专业词/标准号/CAS 词面检索与 search_exact、幂等重导、BM25 排序、
#       rrf_fuse 三路融合截断；目录卡长度受限与 evidence_kind、卡/正文分离存储。
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def tmp_dir(prefix):
    base = Path("D:/claudecode/scratch/sm-corpus-20261019")  # 只在 D 盘落临时产物（铁律 7）
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(base)))


def cleanup(tmp, *objs):
    """先关闭 SQLite 连接再删目录（Windows 上打开的文件句柄会让 rmtree 静默失败）。"""
    for obj in objs:
        try:
            obj.close()
        except Exception:
            pass
    shutil.rmtree(tmp, ignore_errors=True)


def test_chunk_token_budget():
    from corpus.chunking import chunk_blocks
    from corpus.parsers import Block
    sentence = "动火作业前应当进行动火分析并确认安全措施已落实到位。"
    block = Block(content=sentence * 60, locator={"para": 1}, kind="text")
    chunks = chunk_blocks([block], max_tokens=220, target_tokens=180)
    tok_names = {c.tokenizer for c in chunks}
    non_last = chunks[:-1]
    record("切块 token 计量标注 tokenizer",
           tok_names and tok_names <= {"bge", "estimate"}, f"tok_names={tok_names}")
    record("全部块 token ≤ 220", all(c.token_count <= 220 for c in chunks),
           f"max={max((c.token_count for c in chunks), default=0)}")
    record("非末块 token 落在 180~220 目标",
           bool(non_last) and all(180 <= c.token_count <= 220 for c in non_last),
           f"tokens={[c.token_count for c in chunks]}")
    record("切块数合理（长文不只 1 块）", len(chunks) >= 5, f"n={len(chunks)}")
    record("chunk evidence_kind=body 且 kind 继承",
           all(c.evidence_kind == "body" and c.kind == "text" for c in chunks))


def test_chunk_hard_split_long_sentence():
    from corpus.chunking import chunk_blocks
    from corpus.parsers import Block
    text = "甲" * 1500  # 无标点超长句，必须硬切
    chunks = chunk_blocks([Block(content=text, locator={"para": 7}, kind="text")])
    record("超长句硬切为多块", len(chunks) >= 6, f"n={len(chunks)}")
    record("硬切后每块 token ≤ 220", all(c.token_count <= 220 for c in chunks),
           f"max={max(c.token_count for c in chunks)}")
    record("硬切无正文丢失", "".join(c.content for c in chunks) == text)


def test_chunk_table_row_group_repeats_header():
    from corpus.chunking import chunk_blocks
    from corpus.parsers import Block
    header = "序号 | 检查项 | 结论"
    rows = [f"{i} | 动火分析合格确认 | 是" for i in range(1, 61)]
    block = Block(content="\n".join([header] + rows),
                  locator={"sheet": "动火作业检查表", "header_row": 1},
                  kind="table", detail={"headers": ["序号", "检查项", "结论"]})
    chunks = chunk_blocks([block])
    record("表格切成多个行组", len(chunks) >= 2, f"n={len(chunks)}")
    record("每组重复表头且以表头开头",
           all(c.content.startswith(header) and c.header == header for c in chunks))
    record("表头重复次数 == 行组数",
           sum(c.content.count(header) for c in chunks) == len(chunks))
    record("表格块 token ≤ 220 且 kind=table",
           all(c.token_count <= 220 and c.kind == "table" for c in chunks))
    record("表格块 locator 继承 sheet 并带行号范围",
           all(c.locator.get("sheet") == "动火作业检查表" and "row_start" in c.locator
               and "row_end" in c.locator for c in chunks))


def test_chunk_locator_inheritance():
    from corpus.chunking import chunk_text
    text = "受限空间作业前30分钟内取样分析，氧含量19.5%~21%。" * 40
    chunks = chunk_text(text, locator={"page": 3, "para": 12})
    record("chunk 继承源 block 定位（page/para 保留）",
           all(c.locator.get("page") == 3 and c.locator.get("para") == 12 for c in chunks))
    record("chunk 附块内序号便于取邻块",
           [c.locator.get("chunk") for c in chunks] == list(range(len(chunks))))


def test_chunk_tokenizer_explicit():
    from corpus.chunking import chunk_blocks, estimate_tokens, Chunk
    from corpus.parsers import Block

    def count_chars(text):
        return len(text)

    text = "湿热天气下受限空间作业应加强通风和气体监测。" * 40
    est = chunk_blocks([Block(content=text, locator={"para": 1}, kind="text")],
                       tokenizer="estimate")
    custom = chunk_blocks([Block(content=text, locator={"para": 1}, kind="text")],
                          tokenizer=count_chars)
    record("显式 estimate tokenizer 标注一致",
           est and all(c.tokenizer == "estimate" for c in est))
    record("自定义 callable tokenizer 按其名称标注",
           custom and all(c.tokenizer == "count_chars" for c in custom))
    record("estimate_tokens 保守计量（中文≈1 token/字）",
           estimate_tokens("动火") == 2 and estimate_tokens("") == 0)
    record("Chunk 默认 evidence_kind=body", Chunk("x", {}, 1, "estimate").evidence_kind == "body")


def _corpus_chunks():
    from corpus.chunking import chunk_text
    c1 = chunk_text("动火作业必须办理动火作业票，动火前30分钟内进行动火分析并确认措施落实。",
                    locator={"page": 1})
    c2 = chunk_text("依据 GB/T 30871-2022 危险化学品企业特殊作业安全规范办理特殊作业票证。",
                    locator={"page": 2})
    c3 = chunk_text("乙醇（CAS 64-17-5）属于易燃液体，储存区应远离火种热源并防静电。",
                    locator={"page": 3})
    return c1 + c2 + c3


def test_fts_chinese_short_word_and_professional_terms():
    from corpus.fts import CorpusFTS
    tmp = tmp_dir("fts_a_")
    try:
        fts = CorpusFTS(db_path=str(tmp / "corpus_fts.sqlite"))
        fts.add_blocks("v1", _corpus_chunks())
        hit1 = fts.search("动火")
        hit2 = fts.search("危险化学品")
        hit3 = fts.search("易燃液体")
        record("中文二字词可检索（动火）",
               any("动火分析" in h["content"] for h in hit1), f"hits={len(hit1)}")
        record("专业长词可检索（危险化学品）",
               any("危险化学品企业特殊作业" in h["content"] for h in hit2), f"hits={len(hit2)}")
        record("专业长词可检索（易燃液体）",
               any("易燃液体" in h["content"] for h in hit3), f"hits={len(hit3)}")
        record("无命中词返回空", fts.search("量子纠缠退相干") == [])
        record("检索结果带 locator 与 evidence_kind",
               hit1 and hit1[0]["locator"].get("page") == 1 and hit1[0]["evidence_kind"] == "body")
    finally:
        cleanup(tmp, fts)


def test_fts_standard_and_cas_exact():
    from corpus.fts import CorpusFTS, extract_cas, extract_standard_nos
    tmp = tmp_dir("fts_b_")
    try:
        fts = CorpusFTS(db_path=str(tmp / "corpus_fts.sqlite"))
        fts.add_blocks("v1", _corpus_chunks())
        std_hits = fts.search("GB/T 30871-2022")
        cas_hits = fts.search("64-17-5")
        ex_std = fts.search_exact("standard", "GB/T 30871-2022")
        ex_std2 = fts.search_exact("standard", "GB/T30871-2022")   # 归一化后等价
        ex_cas = fts.search_exact("cas", "64-17-5")
        record("标准号可词面检索", any("30871" in h["content"] for h in std_hits),
               f"hits={len(std_hits)}")
        record("CAS 可词面检索", any("64-17-5" in h["content"] for h in cas_hits),
               f"hits={len(cas_hits)}")
        record("search_exact 标准号精确命中（唯一）",
               len(ex_std) == 1 and "30871" in ex_std[0]["content"], f"n={len(ex_std)}")
        record("search_exact 标准号归一化等价命中", len(ex_std2) == 1)
        record("search_exact CAS 精确命中（唯一）",
               len(ex_cas) == 1 and "64-17-5" in ex_cas[0]["content"], f"n={len(ex_cas)}")
        record("精确字段抽取正则可复现",
               extract_standard_nos("见 GB/T 30871-2022 与 AQ 3025-2008") ==
               ["GB/T 30871-2022", "AQ 3025-2008"]
               and extract_cas("乙醇 CAS 64-17-5") == ["64-17-5"])
        try:
            fts.search_exact("bogus", "x")
            record("search_exact 非法 kind 抛错", False, "未抛错")
        except ValueError:
            record("search_exact 非法 kind 抛错", True)
    finally:
        cleanup(tmp, fts)


def test_fts_idempotent_reindex():
    from corpus.fts import CorpusFTS
    tmp = tmp_dir("fts_c_")
    try:
        fts = CorpusFTS(db_path=str(tmp / "corpus_fts.sqlite"))
        chunks = _corpus_chunks()
        first = fts.add_blocks("v1", chunks)
        n1 = fts.stats()["blocks"]
        second = fts.add_blocks("v1", chunks)
        n2 = fts.stats()["blocks"]
        fts.add_blocks("v2", chunks)
        n3 = fts.stats()["blocks"]
        st = fts.stats()
        record("首次导入计数正确", first == len(chunks) and n1 == len(chunks),
               f"first={first} n1={n1}")
        record("同 version 重复 add_blocks 不重复", second == 0 and n2 == n1,
               f"second={second} n1={n1} n2={n2}")
        record("不同 version 独立计数", n3 == n1 + len(chunks), f"n3={n3}")
        record("stats 暴露标准号/CAS 计数",
               st["standards"] >= 2 and st["cas"] >= 2, f"stats={st}")
    finally:
        cleanup(tmp, fts)


def test_fts_bm25_ranking():
    from corpus.chunking import chunk_text
    from corpus.fts import CorpusFTS
    tmp = tmp_dir("fts_d_")
    try:
        fts = CorpusFTS(db_path=str(tmp / "corpus_fts.sqlite"))
        many = chunk_text("动火作业 动火分析 动火票 动火前 动火监护人 动火作业票 特殊动火 一级动火 " * 3,
                          locator={"page": 1})
        once = chunk_text("本章说明作业票的通用管理要求，其中提到动火一次。", locator={"page": 2})
        none = chunk_text("受限空间作业前取样分析氧含量，与明火无关。", locator={"page": 3})
        fts.add_blocks("v1", many + once + none)
        hits = fts.search("动火", limit=20)
        record("BM25 多命中词文档排前",
               hits and "动火分析" in hits[0]["content"], f"top={hits[0]['content'][:20] if hits else ''}")
        record("BM25 弱命中文档排后",
               len(hits) >= 2 and hits[0]["locator"]["page"] == 1 and hits[1]["locator"]["page"] == 2,
               f"pages={[h['locator']['page'] for h in hits]}")
        record("不含查询词的文档不被召回",
               all(h["locator"]["page"] != 3 for h in hits))
    finally:
        cleanup(tmp, fts)


def test_rrf_fuse_three_channels_truncate():
    from corpus.fts import rrf_fuse, MAX_FUSED_BLOCKS, RRF_K, CANDIDATES_PER_CHANNEL
    fused = rrf_fuse([["a", "b", "c"], ["b", "d"], ["c", "e"]])
    record("RRF 融合多路（多路命中排最前）", fused[0] == "b", f"fused={fused}")
    record("RRF 融合覆盖全部候选且去重", set(fused) == {"a", "b", "c", "d", "e"} and len(fused) == 5)
    record("RRF 多路候选优先于单路",
           fused.index("c") < fused.index("d") and fused.index("a") < fused.index("e"),
           f"fused={fused}")
    big = rrf_fuse([list(map(str, range(40)))], limit=MAX_FUSED_BLOCKS)
    record("RRF 截断到至多 32 块", len(big) == MAX_FUSED_BLOCKS, f"n={len(big)}")
    consts = rrf_fuse([[{"block_id": "x"}, {"block_id": "y"}], [{"block_id": "y"}]])
    record("RRF 支持 dict 候选并保持预算常量",
           consts == ["y", "x"] and RRF_K == 60 and CANDIDATES_PER_CHANNEL == 20)
    record("RRF 空输入返回空", rrf_fuse([]) == [])


def _catalog_inputs():
    body = ("正文首行：动火作业票有效期特殊动火不超过8小时。\n"
            + "动火作业安全管理细节逐条展开说明。" * 200)
    parsed = {
        "blocks": [
            {"content": "动火作业检查表", "kind": "heading", "locator": {"para": 1}},
            {"content": body, "kind": "table", "locator": {"sheet": "S1"},
             "evidence_kind": "body"},
        ]
    }
    version = {"version_id": "V1", "title": "动火作业安全管理规定", "category": "操作技术",
               "source": "toolbox_catalog", "status": "unknown"}
    return version, parsed


def test_catalog_card_short_and_kind():
    from corpus.catalog import make_catalog_card, CATALOG_KIND, CATALOG_MAX_TOKENS
    version, parsed = _catalog_inputs()
    card = make_catalog_card(version, parsed)
    record("目录卡 evidence_kind=catalog", card["evidence_kind"] == CATALOG_KIND)
    record("目录卡长度受限（≤400 token）", card["token_count"] <= CATALOG_MAX_TOKENS,
           f"tokens={card['token_count']}")
    record("目录卡不含正文全文",
           "动火作业安全管理细节逐条展开说明。" * 5 not in card["text"]
           and len(card["text"]) < 600, f"len={len(card['text'])}")
    record("目录卡确定性抽取标题/章节",
           "动火作业安全管理规定" in card["text"] and "动火作业检查表" in card["text"])
    record("目录卡卡片：同输入 → 同卡（确定性）",
           make_catalog_card(version, parsed)["card_id"] == card["card_id"])
    v2, p2 = _catalog_inputs()
    p2["blocks"][1]["content"] = "依据 GB/T 30871-2022 与 CAS 64-17-5 组织动火作业票审批。"
    card2 = make_catalog_card(v2, p2)
    record("目录卡抽取标准号/CAS",
           bool(card2["standard_nos"]) and bool(card2["cas"]),
           f"std={card2['standard_nos']} cas={card2['cas']}")


def test_catalog_assert_not_body_evidence():
    from corpus.catalog import (make_catalog_card, is_body_evidence,
                                assert_not_body_evidence)
    from corpus.chunking import chunk_text
    version, parsed = _catalog_inputs()
    card = make_catalog_card(version, parsed)
    record("目录卡不是正文证据", is_body_evidence(card) is False)
    raised = False
    try:
        assert_not_body_evidence(card)
    except AssertionError:
        raised = True
    record("assert_not_body_evidence 对目录卡抛错", raised)
    body_chunk = chunk_text("动火作业前办理动火票并落实监护人。", locator={"page": 1})[0]
    record("assert_not_body_evidence 对正文块通过",
           assert_not_body_evidence(body_chunk) is True and is_body_evidence(body_chunk) is True)
    raised_missing = False
    try:
        assert_not_body_evidence({"content": "x"})
    except AssertionError:
        raised_missing = True
    record("assert_not_body_evidence 对缺类型块 fail-closed 抛错", raised_missing)


def test_catalog_body_separation_storage():
    from corpus.catalog import (make_catalog_card, write_catalog_card, write_body_blocks,
                                read_catalog_cards, read_body_blocks)
    from corpus.chunking import chunk_text
    tmp = tmp_dir("catalog_")
    try:
        db = tmp / "catalog.sqlite"
        version, parsed = _catalog_inputs()
        card = make_catalog_card(version, parsed)
        chunks = chunk_text("动火作业安全管理规定：动火票有效期与监护人要求。", locator={"page": 1}) \
            + chunk_text("受限空间作业须先通风、再检测、后作业。", locator={"page": 2})
        cid = write_catalog_card(db, card)
        n = write_body_blocks(db, "V1", chunks)
        cards = read_catalog_cards(db)
        bodies = read_body_blocks(db)
        record("卡与正文分表写入成功", cid == card["card_id"] and n == len(chunks),
               f"cid={cid} n={n}")
        record("目录卡表只含 catalog 类型",
               cards and all(c["evidence_kind"] == "catalog" for c in cards)
               and any(c["card_id"] == card["card_id"] for c in cards))
        record("正文表只含 body 类型",
               bodies and all(b["evidence_kind"] == "body" for b in bodies))
        record("正文表不含目录卡",
               all(b.get("card_id") != card["card_id"] for b in bodies)
               and all(b["block_id"] != card["card_id"] for b in bodies))
        try:
            write_body_blocks(db, "V1", [card])
            record("正文表拒收目录卡", False, "未抛错")
        except AssertionError:
            record("正文表拒收目录卡", True)
        try:
            write_catalog_card(db, {"content": "x", "evidence_kind": "body"})
            record("卡表拒收正文块", False, "未抛错")
        except AssertionError:
            record("卡表拒收正文块", True)
        import sqlite3
        conn = sqlite3.connect(str(db))
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        conn.close()
        record("卡与正文是两个独立表", {"catalog_cards", "body_blocks"} <= names, f"tables={names}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    for fn in [test_chunk_token_budget, test_chunk_hard_split_long_sentence,
               test_chunk_table_row_group_repeats_header, test_chunk_locator_inheritance,
               test_chunk_tokenizer_explicit, test_fts_chinese_short_word_and_professional_terms,
               test_fts_standard_and_cas_exact, test_fts_idempotent_reindex,
               test_fts_bm25_ranking, test_rrf_fuse_three_channels_truncate,
               test_catalog_card_short_and_kind, test_catalog_assert_not_body_evidence,
               test_catalog_body_separation_storage]:
        try:
            fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)
