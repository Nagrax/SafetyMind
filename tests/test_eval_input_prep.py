# 评测输入清洗模块单测（纯 stdlib / 死 LLM 可跑）：python tests/test_eval_input_prep.py
# 覆盖：超长截断标记、全噪声清洗后不静默为空、空串 / None / 代理字符健壮性、
#       三类干扰片段剔除计数、语义对齐（命中/未命中/最长优先）、样本过滤。
# 退出码 0 = 全过；任一 FAIL = 非零（供 CI / 工作流做门禁）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evaluation.input_prep import (
    MAX_INPUT_CHARS,
    align_semantics,
    estimate_tokens,
    filter_samples,
    precheck,
    strip_noise,
)

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── T1 超长截断且带明确标记（绝不静默返回空结果）──────────────────────────────
def t1_oversize_truncation():
    r = precheck("安" * 100, max_chars=50)
    ok = (MAX_INPUT_CHARS >= 1
          and r.truncated is True and r.original_chars == 100
          and r.text.startswith("安" * 50)
          and "[已截断" in r.text and "原长 100 字符" in r.text
          and r.text.strip() != "" and len(r.notes) >= 1)
    record("T1 超长截断且带明确标记", ok, f"{r}")


# ── T2 空串不崩溃 ─────────────────────────────────────────────────────────────
def t2_empty_string():
    r = precheck("")
    ok = (r.text == "" and r.original_chars == 0
          and r.truncated is False and r.noise_removed == 0 and len(r.notes) >= 1)
    record("T2 空串不崩溃且有说明", ok, f"{r}")


# ── T3 None / 代理字符不崩溃 ─────────────────────────────────────────────────
def t3_none_and_surrogate():
    rn = precheck(None)
    rs = precheck("正常\udcff文本")
    ok = (rn.text == "" and rn.original_chars == 0 and len(rn.notes) >= 1
          and "\udcff" not in rs.text and "正常" in rs.text and "文本" in rs.text
          and rs.original_chars == 4 and len(rs.notes) >= 1)
    record("T3 None / 代理字符不崩溃", ok, f"none={rn} sur={rs}")


# ── T4 tool_calls 噪声剔除计数正确 ───────────────────────────────────────────
def t4_tool_calls_noise():
    raw = ('第一段\n```tool_calls\n{"name": "a"}\n```\n'
           '第二段 {"tool_calls": [{"name": "b"}]} 第三段')
    cleaned, n = strip_noise(raw)
    r = precheck(raw)
    ok = (n == 2 and r.noise_removed == 2
          and "tool_calls" not in cleaned and "```" not in cleaned
          and "第一段" in cleaned and "第三段" in cleaned)
    record("T4 tool_calls 噪声剔除计数正确", ok, f"n={n} cleaned={cleaned!r}")


# ── T5 [system] / 内部提示词剔除 ─────────────────────────────────────────────
def t5_internal_prompt_blocks():
    raw = "[system]你是安全专家[/system]\n内部提示词：请保密"
    cleaned, n = strip_noise(raw)
    ok = (n == 2 and "[system]" not in cleaned
          and "内部提示词" not in cleaned and "请保密" not in cleaned)
    record("T5 [system] / 内部提示词剔除", ok, f"n={n} cleaned={cleaned!r}")


# ── T6 并行 Agent 输出块剔除 ─────────────────────────────────────────────────
def t6_parallel_agent_blocks():
    raw = "[equipment - 主处理] 检修建议见下 [compliance - 辅助] 需合规复核"
    cleaned, n = strip_noise(raw)
    ok = (n == 2 and "equipment" not in cleaned and "compliance" not in cleaned
          and "检修建议见下" in cleaned and "需合规复核" in cleaned)
    record("T6 并行 Agent 输出块剔除", ok, f"n={n} cleaned={cleaned!r}")


# ── T7 语义对齐命中 / 未命中 / 最长优先 ──────────────────────────────────────
def t7_align_semantics():
    cats = ["device_repair", "install", "other"]
    aliases = {"报修": "device_repair", "保修": "device_repair", "装空调": "install"}
    hit = align_semantics("我要报修空调", cats, aliases)
    miss = align_semantics("随便聊聊", cats, aliases)
    # 最长优先：应命中更长的 "装空调"，而不是更短的 "空调"
    longest = align_semantics("帮我装空调", ["a_val", "install"],
                              {"空调": "a_val", "装空调": "install"})
    ok = ("device_repair" in hit and miss == "随便聊聊"
          and "install" in longest and "a_val" not in longest)
    record("T7 语义对齐命中 / 未命中 / 最长优先", ok,
           f"hit={hit!r} miss={miss!r} longest={longest!r}")


# ── T8 filter_samples 精确过滤且不改原对象 ───────────────────────────────────
def t8_filter_samples():
    samples = [{"intent": "a", "q": 1}, {"intent": "b", "q": 2}, {"intent": "a", "q": 3}]
    r = filter_samples(samples, intent="a")
    rall = filter_samples(samples)
    rkey = filter_samples([{"c": "x"}, {"c": "y"}], intent="y", key="c")
    ok = (len(r) == 2 and [s["q"] for s in r] == [1, 3]
          and r is not samples and r[0] is samples[0]
          and len(rall) == 3 and rall is not samples
          and len(rkey) == 1 and rkey[0]["c"] == "y"
          and samples[1]["intent"] == "b")
    record("T8 filter_samples 精确过滤且不改原对象", ok, f"r={r}")


# ── T9 estimate_tokens CJK / ASCII 启发式 ───────────────────────────────────
def t9_estimate_tokens():
    ok = (estimate_tokens("") == 0 and estimate_tokens(None) == 0
          and estimate_tokens("abcdefgh") == 2      # 8 / 4
          and estimate_tokens("安全") == 2)          # ceil(2 / 1.5)
    record("T9 estimate_tokens CJK / ASCII 启发式", ok,
           f"ascii={estimate_tokens('abcdefgh')} cjk={estimate_tokens('安全')}")


# ── T10 全噪声超长串清洗后不静默为空（显式留标）───────────────────────────────
def t10_all_noise_not_silent_empty():
    raw = "[system]内部提示词[/system]" * 3000
    r = precheck(raw)
    ok = (r.original_chars > 0 and r.noise_removed >= 3000
          and r.text.strip() != "" and "清洗后无有效内容" in r.text
          and "原长" in r.text and len(r.notes) >= 1)
    record("T10 全噪声清洗后不静默为空", ok,
           f"orig={r.original_chars} noise={r.noise_removed} text={r.text[:48]!r}")


def main():
    t1_oversize_truncation()
    t2_empty_string()
    t3_none_and_surrogate()
    t4_tool_calls_noise()
    t5_internal_prompt_blocks()
    t6_parallel_agent_blocks()
    t7_align_semantics()
    t8_filter_samples()
    t9_estimate_tokens()
    t10_all_noise_not_silent_empty()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASSED"
          + (f"，失败: {[r[0] for r in failed]}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
