# -*- coding: utf-8 -*-
"""偏好卡片子系统测试（OCTO 式资产沉淀，挂接三级记忆）。

用法: .venv/Scripts/python.exe tests/test_preference_cards.py
     （须用仓库 venv：系统 Python 缺 chromadb，会在注入段 ModuleNotFoundError）
覆盖：规则抽取（禁忌表述/详略偏好/常问设备/高频SOP/画像沉淀/误抽防护）、注入
     （MemoryContext 渲染 + MemoryManager.get_context 排序上限）、降级
     （dead-LLM 画像失败降级规则抽取、偏好库不可用不阻塞对话、坏卡片渲染
     不炸块）、容量上限（单用户 MAX_CARDS_PER_USER 剪除）、/feedback API 钩子。
环境：全 dead——ANTHROPIC_BASE_URL/Redis/Chroma 均指向 127.0.0.1:9（秒级拒连，
     零外部依赖可跑），LLM 超时 2s 重试 0 次；嵌入走本地 ngram；全部数据落临时
     目录（不碰 data/chroma、data/audit.db，符合 AGENTS.md 铁律 9）。
备注：t6 走完整 lifespan，Prometheus 端口沿用真实 .env（与 test_frontend_endpoints
     同款既有模式）；与桌面端同机同跑时 :9091 可能冲突，错峰运行即可。
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEAD = "http://127.0.0.1:9"
RESULTS = []

# ── 环境先行（必须在 import api.main / MemoryManager 之前）─────────────────────
os.environ["ANTHROPIC_API_KEY"] = "test-key-dummy"
os.environ["ANTHROPIC_BASE_URL"] = DEAD
os.environ["SAFETYMIND_LLM_TIMEOUT_S"] = "2"
os.environ["SAFETYMIND_LLM_MAX_RETRIES"] = "0"
os.environ["SAFETYMIND_SKILL_EVOLUTION"] = "0"
os.environ["SAFETYMIND_BGE"] = "0"
os.environ["SAFETYMIND_EMBEDDING"] = "ngram"       # 跳过 BGE 模型加载（零下载）
os.environ["AUDIT_ENABLED"] = "0"
os.environ["API_HOST"] = "127.0.0.1"
os.environ["REDIS_URL"] = "redis://127.0.0.1:9/0"  # 死 Redis → MemoryManager 走进程内降级

TMP = Path(tempfile.mkdtemp(prefix="pref_cards_"))
os.environ["PREFERENCE_DB_PATH"] = str(TMP / "preference_cards.db")
os.environ["SAFETYMIND_SKILL_EVO_DIR"] = str(TMP)   # /feedback 的 jsonl 也进临时目录，不污染 data/
os.environ["SAFETYMIND_MEMORY_SNAPSHOT"] = str(TMP / "memory_snapshot.json")
os.environ["CHROMA_HOST"] = "127.0.0.1"
os.environ["CHROMA_PORT"] = "9"
os.environ["CHROMA_PERSIST_DIRECTORY"] = str(TMP / "chroma_test")       # 独立目录，铁律 9
os.environ["CHROMA_KB_PERSIST_DIRECTORY"] = str(TMP / "kb_chroma_test")  # 独立目录，铁律 9


def record(name, ok, detail=""):
    ok = bool(ok)   # 防 None/0 等假值被误计为通过（如 and 短路返回 None）
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def find_card(cards, key_part):
    return next((c for c in cards if key_part in c.get("card_key", "")), None)


# ── 1. 规则抽取（无 LLM，确定性）──────────────────────────────────────────────

def t1_rule_extraction():
    from memory.preference_cards import (
        PreferenceCardManager, extract_devices, extract_taboo)
    mgr = PreferenceCardManager(db_path=str(TMP / "preference_cards.db"))

    # 禁忌表述：两次差评同句式 → 屡次纠正成卡，hit_count=2，正/错称都进内容
    for _ in range(2):
        mgr.absorb_feedback("u1", rating="down", comment="液碱不是火碱，应该叫液碱，别再叫错了",
                            message_head="火碱储罐巡检要点", request_id="fb-1")
    cards = mgr.list_cards("u1")
    taboo = find_card(cards, "taboo:火碱")
    record("禁忌表述卡沉淀（屡次纠正 hit_count=2）",
           taboo and taboo["hit_count"] == 2 and "液碱" in taboo["content"] and "火碱" in taboo["content"],
           str(taboo))

    # 详略偏好：太长→简洁；太笼统→详细
    mgr.absorb_feedback("u1", rating="down", comment="太长了，说重点")
    mgr.absorb_feedback("u1", rating="down", comment="太长了，精简点")
    mgr.absorb_feedback("u2", rating="down", comment="太笼统了，展开讲讲")
    mgr.absorb_feedback("u2", rating="down", comment="不够详细，要步骤")
    c1 = find_card(mgr.list_cards("u1"), "style:concise")
    c2 = find_card(mgr.list_cards("u2"), "style:detailed")
    record("详略偏好卡（简洁/详细双向）",
           c1 and c1["hit_count"] == 2 and c2 and c2["hit_count"] == 2,
           f"concise={c1} detailed={c2}")

    # 常问设备：位号+名词，同批去重、跨批强化
    mgr.absorb_feedback("u3", rating="down", message_head="R-101 反应釜又异响了")
    mgr.absorb_feedback("u3", rating="down", message_head="R-101 反应釜这回超温了")
    d = find_card(mgr.list_cards("u3"), "device:r-101")
    d2 = find_card(mgr.list_cards("u3"), "device:反应釜")
    record("常问设备卡（位号+名词，跨批强化）",
           d and d["hit_count"] == 2 and d2 and d2["hit_count"] == 2,
           f"r101={d} reactor={d2}")

    # 高频 SOP 类别
    mgr.absorb_feedback("u4", rating="down", message_head="动火作业票怎么办")
    mgr.absorb_feedback("u4", rating="down", message_head="又是动火审批流程")
    s = find_card(mgr.list_cards("u4"), "sop:动火")
    record("高频 SOP 类别卡", s and s["hit_count"] == 2, str(s))

    # 画像沉淀：岗位/区域 + 实体（设备编号/化学品/风险类型）
    mgr.absorb_profile("u5", {"duty": "操作工", "site": "一车间",
                              "entities": {"设备编号": ["R-101"], "化学品": ["液氯"], "风险类型": ["泄漏"]}})
    p = mgr.list_cards("u5")
    record("画像更新沉淀卡片（岗位区域+实体三类）",
           find_card(p, "role:") and find_card(p, "device:r-101")
           and find_card(p, "chemical:液氯") and find_card(p, "risk:泄漏"),
           json.dumps(p, ensure_ascii=False)[:200])

    # 无关反馈不产生卡片
    before = len(mgr.list_cards("u9"))
    mgr.absorb_feedback("u9", rating="down", comment="回答得不错", message_head="谢谢")
    record("无信号反馈不产生卡片", len(mgr.list_cards("u9")) == before)

    # 误抽防护（宁漏不误，2026-10 评审红灯三例回归）：
    # ①议论修辞不成"纠正"卡（update_profile 降级路径会对最近 10 条消息抽取，
    #   安全培训语料高频出现"X不是Y而是Z"，两轮即达注入门槛污染 prompt）
    record("修辞句不成禁忌卡（安全生产不是负担，而是责任）",
           extract_taboo("安全生产不是负担，而是责任。") is None,
           str(extract_taboo("安全生产不是负担，而是责任。")))
    # ②无术语的纠正句不成卡（"错"是通用词不是术语）
    record("无术语纠正不成卡（别再叫错了）",
           extract_taboo("别再叫错了") is None, str(extract_taboo("别再叫错了")))
    # ③标准号不当设备位号（GB50160/API2023），位号正例不受影响（"泵"来自名词词表，亦为正例）
    d_std = extract_devices("依据 GB50160 和 API2023")
    d_pos = extract_devices("泵R-101异常，另查 P102 与 Q-1000 点检")
    pos_keys = {c["card_key"] for c in d_pos}
    record("标准号排除且真位号保留（GB50160/API2023 vs R-101/P102/Q-1000）",
           d_std == [] and {"device:r-101", "device:p102", "device:q-1000"} <= pos_keys
           and not any("gb" in k or "api" in k for k in pos_keys),
           f"std={d_std} pos={d_pos}")
    # 正例不回退：真纠正（含称呼动词+真术语）依旧成卡
    record("真纠正句仍成卡（液碱/火碱）", extract_taboo("液碱不是火碱，应该叫液碱") is not None)
    return mgr


# ── 2. 注入（排序/门槛/上限）与 MemoryContext 渲染 ─────────────────────────────

def t2_injection_and_rendering(mgr):
    from memory.preference_cards import PreferenceCardManager
    from memory.conversation_memory import MemoryContext, Message, MsgRole

    # MIN_HITS 门槛：单次信号（hit_count=1）不注入
    mgr.absorb_conversation("gate", "R-505 离心机振动排查")
    gate_cards = mgr.list_cards("gate")
    record("单次信号不越过注入门槛（MIN_HITS=2）",
           gate_cards and all(c["hit_count"] == 1 for c in gate_cards)
           and mgr.rank_for("gate", "R-505") == [],
           json.dumps(gate_cards, ensure_ascii=False)[:160])

    # 排序：命中度优先，其次 (-score, -updated_ts)；上限 3-5 张
    # u1 持有：taboo:火碱(keywords=[火碱,液碱]) / style:concise / device:储罐（均 hit=2）
    ranks = mgr.rank_for("u1", "液碱储罐巡检要点")
    ok_sorted = ranks == sorted(ranks, key=lambda c: (-c["score"], -c["updated_ts"]))
    ok_top = bool(ranks) and ranks[0]["card_key"] == "device:储罐"   # 全词命中 > 半命中(1/2)
    ok_cap = len(ranks) <= PreferenceCardManager.INJECT_LIMIT
    record("排序：命中度优先 + (-score,-updated_ts) 稳定序", ok_sorted and ok_top,
           json.dumps([{k: c[k] for k in ('card_key', 'score')} for c in ranks], ensure_ascii=False))
    record("注入数量受 INJECT_LIMIT 约束且硬上限 5",
           ok_cap and len(mgr.rank_for("u1", "", limit=10)) <= 5, f"len={len(ranks)}")

    # MemoryContext 渲染：[偏好卡片] 块 + 其余块不丢 + 最多 5 行
    cards = [{"title": f"卡{i}", "content": "内容"} for i in range(7)]
    ctx = MemoryContext(
        recent_messages=[Message(role=MsgRole.USER, content="最近一条")],
        relevant_history=["历史片段"], user_profile={"duty": "操作工"},
        summary="会话摘要", preference_cards=cards)
    text = ctx.to_prompt_text()
    lines = [ln for ln in text.splitlines() if ln.startswith("- 卡")]
    record("渲染 [偏好卡片] 块且最多 5 张", "[偏好卡片]" in text and len(lines) == 5,
           f"lines={len(lines)}")
    record("渲染保序：摘要/画像/偏好/最近对话并存",
           text.index("[会话摘要]") < text.index("[人员安全画像]")
           < text.index("[偏好卡片]") < text.index("[最近对话]"))

    # 坏卡片降级：非 dict / 缺键条目跳过，其余块完好
    bad = MemoryContext(
        recent_messages=[], relevant_history=["历史片段"], user_profile={},
        summary="摘要在", preference_cards=["garbage", {"title": "好卡", "content": "在"}, {}])
    bad_text = bad.to_prompt_text()
    record("坏卡片条目跳过且其余块不受影响",
           "摘要在" in bad_text and "历史片段" in bad_text and "好卡" in bad_text
           and "garbage" not in bad_text, bad_text)

    # 空偏好不产生块（老链路输出形状不变）
    plain = MemoryContext(recent_messages=[], relevant_history=[], user_profile={}, summary="")
    record("无偏好卡片时不输出 [偏好卡片] 块", "[偏好卡片]" not in plain.to_prompt_text())


# ── 3. MemoryManager 集成（dead-Redis/dead-Chroma/dead-LLM 全链路）────────────

def make_mm(tag):
    from memory.conversation_memory import MemoryManager
    return MemoryManager(
        redis_url="redis://127.0.0.1:9/0", chroma_host="127.0.0.1", chroma_port=9,
        chroma_path=str(TMP / f"chroma_{tag}_test"),     # 每个实例独立目录（铁律 9）
        api_key="dummy", base_url=DEAD, model="dummy")


def t3_manager_integration(mgr):
    from memory.conversation_memory import MsgRole

    async def run():
        mm = make_mm("mm")
        record("MemoryManager 挂载偏好子系统（dead 环境构造成功）", mm._pref is not None)

        # 注入链路：工作记忆 + 两次强化 → get_context 带出偏好卡并渲染
        await mm.add_message("u1", "c1", MsgRole.USER, "R-101 反应釜又超温了")
        mm._pref.absorb_feedback("u1", rating="down", message_head="R-101 反应釜超温", request_id="f1")
        mm._pref.absorb_feedback("u1", rating="down", message_head="R-101 反应釜再超温", request_id="f2")
        ctx = await mm.get_context("u1", "c1", query="R-101 现在怎么样了")
        ok = (bool(ctx.preference_cards)
              and all(c["hit_count"] >= mm._pref.MIN_HITS for c in ctx.preference_cards)
              and "[偏好卡片]" in ctx.to_prompt_text()
              and "[最近对话]" in ctx.to_prompt_text())
        record("get_context 注入排序后偏好卡且渲染进 prompt", ok,
               json.dumps(ctx.preference_cards, ensure_ascii=False)[:200])

        # dead-LLM 画像失败 → 降级规则抽取（对话原文 → 设备/SOP 卡）
        await mm.add_message("u6", "c6", MsgRole.USER, "动火作业票怎么办理")
        await mm.add_message("u6", "c6", MsgRole.ASSISTANT, "请先做动火分析并办票")
        await mm.update_profile("u6", "c6")     # LLM 死端点：必须不抛异常
        rule_cards = mm._pref.list_cards("u6")
        record("dead-LLM update_profile 降级规则抽取（不抛异常）",
               find_card(rule_cards, "sop:动火") is not None,
               json.dumps(rule_cards, ensure_ascii=False)[:200])

        # LLM 画像成功（桩）→ 结构化画像沉淀（无第二次 LLM 调用）
        class FakeMsgs:
            async def create(self, **kw):
                profile = {"duty": "电焊工", "site": "二车间",
                           "entities": {"设备编号": ["T-201"], "风险类型": [], "化学品": []},
                           "safety_history": []}
                return SimpleNamespace(content=[{"type": "text", "text": json.dumps(profile)}])

        class FakeClient:
            def __init__(self):
                self.messages = FakeMsgs()
        mm._client = FakeClient()
        await mm.add_message("u7", "c7", MsgRole.USER, "我平时焊接收管线")
        await mm.update_profile("u7", "c7")
        prof_cards = mm._pref.list_cards("u7")
        record("LLM 画像成功走结构化沉淀（岗位+实体卡）",
               find_card(prof_cards, "role:") and find_card(prof_cards, "device:t-201"),
               json.dumps(prof_cards, ensure_ascii=False)[:200])

        # 反馈钩子（MemoryManager 公共入口）：偏好库正常时落卡
        await mm.absorb_feedback("u8", request_id="f8", rating="down",
                                 comment="不要把乙炔叫电石气", message_head="乙炔瓶存放")
        record("MemoryManager.absorb_feedback 落卡", find_card(mm._pref.list_cards("u8"), "taboo:乙炔"))
        await mm.close()

    asyncio.run(run())


# ── 4. 降级：偏好库不可用 → 无偏好注入，对话/记忆照常 ─────────────────────────────

def t4_degradation_blocked_db():
    from memory.conversation_memory import MemoryManager, MsgRole

    blocker = TMP / "pref_blocked"
    blocker.write_text("占用为文件，令其下的 db 路径不可创建", encoding="utf-8")
    os.environ["PREFERENCE_DB_PATH"] = str(blocker / "pref.db")
    try:
        mm = make_mm("bad")   # 构造期 PreferenceCardManager 失败 → _pref=None
        record("偏好库不可用时 MemoryManager 照常构造（_pref=None）", mm._pref is None)

        async def run():
            await mm.add_message("u1", "cbad", MsgRole.USER, "R-101 异响")  # 记忆读写不受影响
            await mm.absorb_feedback("u1", request_id="fb", rating="down",
                                     comment="液碱不是火碱")                  # 静默 no-op
            ctx = await mm.get_context("u1", "cbad", query="R-101")
            return ctx
        ctx = asyncio.run(run())
        record("偏好库不可用：记忆链路照常、偏好注入为空",
               ctx.preference_cards == [] and "[最近对话]" in ctx.to_prompt_text())
        asyncio.run(mm.close())
    finally:
        os.environ["PREFERENCE_DB_PATH"] = str(TMP / "preference_cards.db")


# ── 5. 容量上限：单用户卡片数封顶，强者留下 ────────────────────────────────────

def t5_capacity_cap(mgr):
    from memory.preference_cards import PreferenceCardManager
    cap = PreferenceCardManager.MAX_CARDS_PER_USER

    mgr.absorb_feedback("cap", rating="down", message_head="反应釜又异响")
    mgr.absorb_feedback("cap", rating="down", message_head="反应釜继续异响")   # hit=2 强卡
    flood = " ".join(f"Q-{1000 + i}检查" for i in range(cap + 10))            # 60 个位号弱卡
    mgr.absorb_conversation("cap", flood, source="rule:flood")
    cards = mgr.list_cards("cap")
    strong = find_card(cards, "device:反应釜")
    record(f"容量上限：卡片数封顶 {cap} 且强卡存活",
           len(cards) == cap and strong and strong["hit_count"] >= 2,
           f"n={len(cards)} strong={strong}")

    # 抽干水后强卡仍排注入队首（弱卡被剪、注入序不受洪水影响）
    ranks = mgr.rank_for("cap", "反应釜")
    record("剪枝后强卡仍可注入且排前", bool(ranks) and ranks[0]["card_key"] == "device:反应釜",
           json.dumps(ranks[:2], ensure_ascii=False)[:160])


# ── 6. /feedback API 钩子（新钩子必须带测试）──────────────────────────────────

def t6_api_feedback_hook():
    from fastapi.testclient import TestClient
    import api.main as api_main   # 动态取模块全局：lifespan 之后才被赋值

    with TestClient(api_main.app) as client:
        mm = api_main._memory
        record("lifespan 后 api._memory 挂载偏好子系统", mm is not None and mm._pref is not None)
        r = client.post("/feedback", json={"request_id": "x", "rating": "bad"})
        record("非法 rating 仍被 422 拒绝（契约不变）", r.status_code == 422)
        for rid in ("p1", "p2"):
            r = client.post("/feedback", json={
                "request_id": rid, "rating": "down",
                "comment": "硫化氢别再叫臭碱了，太长了说重点",
                "user_id": "apiuser", "conv_id": "conv1",
                "message_head": "臭碱储罐区报警仪校准", "answer_head": ""})
            record(f"POST /feedback ({rid}) 200 且响应契约不变",
                   r.status_code == 200 and r.json().get("ok") is True and r.json().get("recorded") is True,
                   r.text[:120])
        cards = mm._pref.list_cards("apiuser")
        taboo = find_card(cards, "taboo:")
        style = find_card(cards, "style:concise")
        device = find_card(cards, "device:")
        record("反馈钩子沉淀禁忌/详略/常问设备卡",
               taboo and taboo["hit_count"] == 2 and style and style["hit_count"] == 2
               and device and device["hit_count"] == 2,
               json.dumps(cards, ensure_ascii=False)[:240])
        # 注入回路闭合：同用户 get_context 可带出刚沉淀的卡
        ctx = asyncio.run(mm.get_context("apiuser", "conv1", query="臭碱报警仪校准"))
        record("API 沉淀 → get_context 注入回路闭合",
               bool(ctx.preference_cards) and "[偏好卡片]" in ctx.to_prompt_text(),
               json.dumps(ctx.preference_cards, ensure_ascii=False)[:160])


def main():
    print(f"临时目录: {TMP}")
    mgr = t1_rule_extraction()
    t2_injection_and_rendering(mgr)
    t3_manager_integration(mgr)
    t4_degradation_blocked_db()
    t5_capacity_cap(mgr)
    t6_api_feedback_hook()
    mgr.close()
    shutil.rmtree(TMP, ignore_errors=True)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
