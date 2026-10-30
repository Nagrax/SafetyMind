# -*- coding: utf-8 -*-
"""工具回路"两轮 vs 一轮"对照实验：延迟收益是否值得质量代价？

## 背景

实测工具类 /chat 中位 16.3s，其中约 9.3s 是**第二轮 LLM 往返**
（把工具结果喂回模型、让它转述成自然语言）。存在"一轮出结果"的设想：
模型发起 tool_use 后，直接把工具结果格式化返回，不再让模型二次生成。

## 为什么必须做对照而不是直接改

一刀切去掉第二轮会让回答质量下降：
  - 工具结果是结构化 JSON，直接返回给用户很难读
  - 模型本来会根据工具结果修正话术（如"库里没有这条，我给你通用参考值"）
  - 所以这是**延迟 vs 可读性**的显式权衡，必须量化

## 实验设计

同一批问句，跑两条路径：
  A 两轮（现状）：tool_use → 执行 → 结果回填 → 模型转述
  B 一轮（设想）：tool_use → 执行 → 工具结果直接模板化返回

指标：总延迟、回答可读性评分（长度/结构/是否含"库内无此数据"这类诚实声明）

用法:
    <venv python> benchmarks/compare_tool_roundtrip.py --live
产物: scratch/sm-loop/runs/tool_roundtrip_compare.json
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

CASES = [
    "帮我查一下甲醇（CAS 67-56-1）的闪点是多少",
    "查一下氨的危险特性",
    "反应釜超温报警了怎么处理",
]


def readability(text: str) -> dict:
    """粗粒度可读性指标（不追求完美，求可比较）。"""
    t = (text or "").strip()
    return {
        "len": len(t),
        "lines": len([ln for ln in t.splitlines() if ln.strip()]),
        "has_structure": any(k in t for k in ("##", "-", "1.", "•", "：")),
        "has_empty": (not t) or len(t) < 20,
        "has_json_smell": t.count("{") >= 2 and t.count('"') >= 6,
        "honest_claim": any(k in t for k in ("库内没有", "无此数据", "未收录", "资料缺口", "暂无")),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args()

    from anthropic import AsyncAnthropic
    from core.llm_params import messages_create_kwargs_base
    from mcp.tool_manager import MCPToolManager, Tool

    base = os.getenv("ANTHROPIC_BASE_URL", "https://open.bigmodel.cn/api/anthropic")
    key = os.getenv("ANTHROPIC_API_KEY", "")
    model = os.getenv("ANTHROPIC_MODEL", "glm-4.6")
    if not key:
        print("缺少 ANTHROPIC_API_KEY")
        return 2

    # 造一个确定性的假工具（避免依赖真实资源库，让对照可复现）
    async def fake_msds(params, context):
        return {
            "found": False,
            "query": params.get("query", ""),
            "note": "MSDS 结构化库中暂无该条目",
            "reference": {"flash_point": "约 11℃（闭杯，行业通用参考值，非库内数据）"},
        }

    tm = MCPToolManager(api_key=key, base_url=base, model=model)
    tm.register(Tool(
        name="msds_lookup", description="查询化学品安全技术说明书",
        handler=fake_msds,
        schema={"type": "object", "properties": {"query": {"type": "string"}},
                "required": ["query"]},
    ))

    tools = [{"name": "msds_lookup", "description": "查询化学品安全技术说明书",
              "input_schema": {"type": "object", "properties": {"query": {"type": "string"}},
                               "required": ["query"]}}]
    client = AsyncAnthropic(api_key=key, base_url=base, timeout=120, max_retries=0)
    system = "你是安全生产助手。需要化学品数据时调用工具，不要凭记忆编造数值。"

    rows = []
    for msg in CASES:
        # ── A 两轮 ────────────────────────────────────────────────────
        t0 = time.monotonic()
        try:
            r1 = await client.messages.create(
                model=model, max_tokens=512, system=system, tools=tools,
                messages=[{"role": "user", "content": msg}],
                **messages_create_kwargs_base())
            tus = [b for b in r1.content if getattr(b, "type", "") == "tool_use"]
            t_round1 = (time.monotonic() - t0) * 1000
            if not tus:
                rows.append({"case": msg, "path": "A_two_round", "note": "第一轮未请求工具",
                             "total_ms": round(t_round1, 1), "text": ""})
                continue
            tu = tus[0]
            res = await tm.call(tu.name, dict(tu.input or {}), None, use_cache=False)
            # ToolResult 是 dataclass/对象，不是 dict：按属性取 data
            raw_data = getattr(res, "data", None)
            if raw_data is None and isinstance(res, dict):
                raw_data = res.get("data", res)
            payload = json.dumps(raw_data, ensure_ascii=False, default=str)
            blocks = [tu.model_dump() if hasattr(tu, "model_dump") else tu]
            r2 = await client.messages.create(
                model=model, max_tokens=1024, system=system, tools=tools,
                messages=[
                    {"role": "user", "content": msg},
                    {"role": "assistant", "content": blocks},
                    {"role": "user", "content": [{"type": "tool_result",
                                                  "tool_use_id": tu.id,
                                                  "content": payload}]},
                ],
                **messages_create_kwargs_base())
            txt_a = "".join(getattr(b, "text", "") for b in r2.content
                            if getattr(b, "type", "") == "text")
            total_a = (time.monotonic() - t0) * 1000
        except Exception as e:
            rows.append({"case": msg, "path": "A_two_round",
                         "error": f"{type(e).__name__}: {str(e)[:150]}"})
            continue

        # ── B 一轮（工具结果直接模板化）────────────────────────────────
        # 关键修正：B 的耗时应该是"第一轮 + 工具执行"，**不含第二轮 LLM 调用**。
        # 第一版写成 total_b = 现在的总耗时，把第二轮也算进去了，得出"0ms 收益"
        # 的错误结论（自相矛盾：如果真没收益，就不该有"第二轮"这个待优化项）。
        t_tool = time.monotonic()
        payload_b = json.dumps(raw_data, ensure_ascii=False, default=str)
        tool_ms = (time.monotonic() - t_tool) * 1000
        txt_b = ("查询结果（原始数据）：\n```json\n" + payload_b + "\n```")
        total_b = t_round1 + tool_ms          # 一轮路径 = 第一轮 + 工具执行，到此为止

        a_ok = bool((txt_a or "").strip())
        if a_ok:
            rows.append({
                "case": msg, "path": "A_two_round",
                "round1_ms": round(t_round1, 1), "total_ms": round(total_a, 1),
                "text": txt_a[:400], **readability(txt_a),
            })
        rows.append({
            "case": msg, "path": "B_one_round",
            "round1_ms": round(t_round1, 1), "tool_ms": round(tool_ms, 1),
            "total_ms": round(total_b, 1),
            "text": txt_b[:400], **readability(txt_b),
        })
        print(f"[{msg[:24]}] A={total_a:.0f}ms(len={len(txt_a)}) vs B={total_b:.0f}ms(len={len(txt_b)})",
              flush=True)

    def agg(path):
        sub = [r for r in rows if r["path"] == path and "total_ms" in r and "len" in r]
        if not sub:
            return {}
        return {
            "n": len(sub),
            "latency_median_ms": round(statistics.median([r["total_ms"] for r in sub]), 1),
            "len_median": round(statistics.median([r["len"] for r in sub]), 1),
            "has_structure": sum(1 for r in sub if r["has_structure"]),
            "has_json_smell": sum(1 for r in sub if r["has_json_smell"]),
            "honest_claim": sum(1 for r in sub if r["honest_claim"]),
            "unreadable": sum(1 for r in sub if r["has_empty"]),
        }

    a, b = agg("A_two_round"), agg("B_one_round")
    print("\n=== 汇总 ===")
    print(f"A 两轮：{json.dumps(a, ensure_ascii=False)}")
    print(f"B 一轮：{json.dumps(b, ensure_ascii=False)}")
    if a and b:
        saved = a["latency_median_ms"] - b["latency_median_ms"]
        print(f"\n延迟收益：{saved:.0f}ms（{(saved / a['latency_median_ms']):.0%}）")
        print(f"可读性代价：JSON 味 {a['has_json_smell']} → {b['has_json_smell']}；"
              f"有结构 {a['has_structure']} → {b['has_structure']}")

    out = Path("D:/claudecode/scratch/sm-loop/runs/tool_roundtrip_compare.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"live": args.live, "summary": {"A": a, "B": b}, "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwritten {out}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
