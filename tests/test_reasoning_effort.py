# -*- coding: utf-8 -*-
# 推理开关测试（2026-10-28，本轮单点收益最大的改动）。
#
# ## 实测数据（为什么默认关推理）
#
# 中继上的推理模型会先用大量 token 产出 reasoning_content 再写正文，而**两者共享
# max_tokens 预算**。A/B 四用例（temperature=0.7 避免中继缓存干扰）：
#
#   | 用例       | 有推理                      | reasoning_effort=none      |
#   |-----------|-----------------------------|----------------------------|
#   | 动火票分级 | 13490ms / 303 字 / 推理 3202 |  4962ms / 613 字 / 推理 0   |
#   | 隐患分级   | 36592ms / **正文 0 字**      | 10792ms / 1414 字           |
#   | 受限空间   | 30593ms / 508 字             | 10790ms / 840 字            |
#   | 台账要素   | 41079ms / 111 字 / 命中 1/4  |  8628ms / 1455 字 / 命中 4/4 |
#
# 即：**关推理不仅快 2.4-4.8×，质量还更好**——有推理时正文可能被截成 0 字（真实故障）。
# 这与 2026-10-25 glm-4.6 的"思考块吃光输出预算"是同一类根因。
#
# 用法: python tests/test_reasoning_effort.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def build(**over):
    from core.llm_protocol import _anthropic_to_openai_request
    kw = {"model": "raccoon/sn-deepseek-v4-1-flash-1017", "max_tokens": 4096,
          "messages": [{"role": "user", "content": "q"}]}
    kw.update(over)
    return _anthropic_to_openai_request(kw)


def main():
    old = os.environ.get("SAFETYMIND_OPENAI_REASONING_EFFORT")
    try:
        # ── 1. 默认发送 reasoning_effort=none（可测的开关契约）─────────────
        os.environ.pop("SAFETYMIND_OPENAI_REASONING_EFFORT", None)
        body = build()
        record("默认带 reasoning_effort", "reasoning_effort" in body,
               f"body keys={sorted(body)}")
        record("默认值为 none", body.get("reasoning_effort") == "none",
               f"got={body.get('reasoning_effort')}")

        # ── 2. 可覆盖 ──────────────────────────────────────────────────
        os.environ["SAFETYMIND_OPENAI_REASONING_EFFORT"] = "low"
        record("可覆盖为 low", build().get("reasoning_effort") == "low")

        # ── 3. 可关闭（回退旧行为：不发该字段）─────────────────────────
        os.environ["SAFETYMIND_OPENAI_REASONING_EFFORT"] = "off"
        record("off 时不发送字段", "reasoning_effort" not in build())
        os.environ["SAFETYMIND_OPENAI_REASONING_EFFORT"] = ""
        record("空值时不发送字段", "reasoning_effort" not in build())

        # ── 4. 不影响其它转译（预算下限、tools、system 仍正确）───────────
        os.environ.pop("SAFETYMIND_OPENAI_REASONING_EFFORT", None)
        b2 = build(max_tokens=512, system="sys",
                   tools=[{"name": "t", "description": "d",
                           "input_schema": {"type": "object", "properties": {}}}])
        record("预算下限仍为 4096", b2["max_tokens"] == 4096)
        record("tools 仍转 function 形态", b2["tools"][0]["type"] == "function")
        record("system 仍为首条消息", b2["messages"][0]["role"] == "system")

        # ── 5. 源码记录实测依据（防后人删掉注释后盲目改回）───────────────
        src = Path(__file__).resolve().parents[1].joinpath(
            "core/llm_protocol.py").read_text(encoding="utf-8")
        record("源码记录推理吃预算的实测", "正文 0 字" in src or "推理" in src)
        record("源码给出可覆盖的环境变量",
               "SAFETYMIND_OPENAI_REASONING_EFFORT" in src)
    finally:
        if old is None:
            os.environ.pop("SAFETYMIND_OPENAI_REASONING_EFFORT", None)
        else:
            os.environ["SAFETYMIND_OPENAI_REASONING_EFFORT"] = old

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
