# -*- coding: utf-8 -*-
# 确定性查询前置分流测试（2026-10-26 目标①）。
#
# 背景：工具类问答 ~16.3s 中约 9.3s 是"把工具结果转述成自然语言"的生成往返。
# 在 ToolExecutor 内部跳过第二轮曾实测并回退（破坏 5 条轮次契约）。替代设计：
# **在 /chat 层对"纯化学品参数查询 + 资源库命中"直接模板化直出**，绕过全部 LLM。
#
# 质量门禁（宁可不加速也不能答偏）：
#   命中资源库 + 查询意图词 + 无处置/建议类词 + 长度≤60 + 非 CRITICAL。
#
# 本测试锁定：
#   1. 门禁：纯参数查询过；处置/建议类不过；长问题不过
#   2. 直出格式：逐节格式化、来源标注、"未经模型改写"声明、绝无裸 JSON
#   3. 集成：/chat 真实资源库命中 → 快路径直出（dead-LLM 也能秒回，证明未走模型）
#   4. 集成：处置类问题 → 不走快路径（走模型，dead-LLM 下为降级文案）
#   5. 开关关闭 → 不直出
#   6. 响应契约：tools_used 带 deterministic 标记、routing_reason 说明未调用模型
#
# 用法: python tests/test_deterministic_fastpath.py
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ISO = Path("D:/claudecode/scratch/sm-loop/iso/det_fastpath")
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def setup_env():
    ISO.mkdir(parents=True, exist_ok=True)
    os.environ.update({
        "HF_HOME": "D:/hf_cache", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "CHROMA_PERSIST_DIRECTORY": str(ISO / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(ISO / "kb"),
        "AUDIT_DB_PATH": str(ISO / "audit.db"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(ISO / "conv"),
        "API_HOST": "127.0.0.1",
        "REDIS_URL": "redis://127.0.0.1:9/0",
        "CHROMA_HOST": "127.0.0.1", "CHROMA_PORT": "9",
        "PROMETHEUS_PORT": "0",
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",   # dead-LLM：快路径不该依赖它
        "ANTHROPIC_API_KEY": "dummy", "ANTHROPIC_MODEL": "dummy",
        "SAFETYMIND_EMBEDDING": "ngram",              # 测试用轻嵌入，秒级
    })


def main():
    setup_env()
    from fastapi.testclient import TestClient
    import api.main as m

    # ── 1. 门禁单元测试 ─────────────────────────────────────────────────
    gate = m._is_pure_chemical_lookup
    record("纯参数查询过门禁", gate("乙醇的闪点是多少") is True)
    record("CAS 查询过门禁", gate("CAS 64-17-5 的理化性质") is True)
    for bad in ["乙醇泄漏了怎么办", "乙醇和盐酸混存可以吗", "甲醇着火了如何处置",
                "乙醇储存有什么注意事项", "为什么乙醇要防爆"]:
        record(f"处置/建议类不过门禁：{bad[:14]}…", gate(bad) is False, f"got={gate(bad)}")
    record("长问题不过门禁（>60 字）",
           gate("乙醇 " * 30) is False)
    record("无查询意图词不过门禁", gate("乙醇") is False)

    # ── 2. 格式化单元测试 ───────────────────────────────────────────────
    fmt = m._deterministic_fast_reply
    data = {"name": "乙醇", "cas": "64-17-5", "found": True,
            "sections": {"1": "化学品标识：乙醇，无水乙醇", "2": "危险性概述：极易燃液体",
                         "3": "理化特性：闪点约 13℃（闭杯）"}}
    reply = fmt("乙醇的闪点是多少", data)
    record("直出含来源标注", "资源库查询结果" in reply and "64-17-5" in reply)
    record("直出逐节格式化", "化学品标识" in reply and "理化特性" in reply)
    record("直出带'未经模型改写'声明", "未经模型改写" in reply)
    record("直出绝无裸 JSON（本轮实验 B 路径的教训）",
           "{" not in reply and "}" not in reply)
    record("直出带追问引导", "继续追问" in reply)
    record("found=False 不直出", fmt("x", {"found": False}) == "")
    record("空 sections 不直出", fmt("x", {"found": True, "sections": {}}) == "")

    # ── 3. 集成：真实资源库命中 → 秒回（dead-LLM 下证明未走模型）────────
    from core.llm_utils import llm_client_kwargs  # noqa: F401
    with TestClient(m.app) as c:
        t0 = time.perf_counter()
        r = c.post("/chat", json={"message": "乙醇 CAS 64-17-5 的闪点是多少",
                                  "user_id": "det_user", "conv_id": "det_c1"}, timeout=120)
        elapsed = time.perf_counter() - t0
        body = r.json() if r.status_code == 200 else {}
        resp = body.get("response") or ""
        record("集成：纯参数查询 200", r.status_code == 200, f"code={r.status_code} {r.text[:120]}")
        record("集成：走快路径（routing_reason 声明未调用生成模型）",
               "确定性查询直出" in (body.get("routing_reason") or ""),
               f"reason={body.get('routing_reason')}")
        record("集成：直出内容含资源库数据", "闪点" in resp and "资源库" in resp,
               f"resp={resp[:120]}")
        record("集成：tools_used 带 deterministic 标记",
               any(t.get("deterministic") for t in (body.get("tools_used") or [])),
               f"tools={body.get('tools_used')}")
        record("集成：请求编号带 det 前缀（可审计）",
               str(body.get("request_id", "")).startswith("det-"),
               f"rid={body.get('request_id')}")
        # 延迟证据不放在本测试里：①dead-LLM 下意图层重试 ~30s 淹没快路径收益；
        # ②首请求承担全部预热成本，顺序偏差会让任何时序断言失真（本轮实测
        #   快路径 44.8s vs 处置类 18.6s，纯粹是预热在前面）。live 实测见
        #   scratch/sm-loop/runs/chat_latency_profile_fastpath.json 与
        #   docs/PERF_AND_RAG_DECISIONS.md 的对照表。

        # ── 4. 集成：处置类问题不走快路径 ────────────────────────────────
        r2 = c.post("/chat", json={"message": "乙醇泄漏了怎么办，怎么处理",
                                   "user_id": "det_user", "conv_id": "det_c2"}, timeout=120)
        body2 = r2.json() if r2.status_code == 200 else {}
        resp2 = body2.get("response") or ""
        record("集成：处置类不走快路径（routing_reason 非直出）",
               "确定性查询直出" not in (body2.get("routing_reason") or ""),
               f"reason={body2.get('routing_reason')}")
        record("集成：处置类回答不含直出声明",
               "未经模型改写" not in resp2, f"resp={resp2[:100]}")

        # ── 5. 开关关闭 → 不直出 ─────────────────────────────────────────
        old = os.environ.get("SAFETYMIND_DETERMINISTIC_FASTPATH")
        try:
            os.environ["SAFETYMIND_DETERMINISTIC_FASTPATH"] = "0"
            r3 = c.post("/chat", json={"message": "乙醇的闪点是多少",
                                       "user_id": "det_user", "conv_id": "det_c3"}, timeout=120)
            body3 = r3.json() if r3.status_code == 200 else {}
            record("开关=0 时不直出（走模型，dead-LLM 下为降级文案）",
                   "确定性查询直出" not in (body3.get("routing_reason") or "")
                   and "未经模型改写" not in (body3.get("response") or ""),
                   f"resp={(body3.get('response') or '')[:80]}")
        finally:
            if old is None:
                os.environ.pop("SAFETYMIND_DETERMINISTIC_FASTPATH", None)
            else:
                os.environ["SAFETYMIND_DETERMINISTIC_FASTPATH"] = old

        # ── 6. 会话/审计后处理不缺席（快路径也要留痕）────────────────────
        r4 = c.get("/conversations", params={"user_id": "det_user"}, timeout=30)
        convs = (r4.json() or {}).get("conversations", [])
        record("快路径同样写会话历史（留痕不缺席）",
               any(x.get("conv_id") == "det_c1" for x in convs),
               f"convs={[x.get('conv_id') for x in convs]}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
