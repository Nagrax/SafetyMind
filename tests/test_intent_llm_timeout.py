# -*- coding: utf-8 -*-
# 意图 LLM 判定超时保护回归测试（2026-10-29 闭环第 40 轮）。
#
# 依据（真实评测）：端点高峰期单次意图判定实测 10.9~24.3s（上午同一请求 6.4s），
# 而低置信回落覆盖约 43.5% 请求——不设上限时用户会在"识别意图"这一步白等 10~25 秒。
# 实测痛点：eval_intent_llm_fallback 并发 6 时平均 12.4s/条。
#
# 修法：`asyncio.wait_for(..., SAFETYMIND_INTENT_LLM_TIMEOUT_S 默认 6s)`，
# 超时后走既有融合降级（failed=True → _vote 用 BGE/关键词），不影响可用性；
# 设 0 可关闭（回退旧行为）。
#
# 本测试锁定：
#   1. 端点慢于阈值时，recognize 在阈值附近返回（不无限等待）
#   2. 超时不算致命错误：仍返回可用意图（降级），不抛异常
#   3. 端点正常时结果与耗时不受影响（路径不变）
#   4. **默认不设超时**：实测 6s 超时会把低置信样本准确率从 ~95% 砍到 29.7%，
#      所以默认关闭（0）；只有显式设置才启用，属"准确率换延迟"的取舍
#
# 用法: python tests/test_intent_llm_timeout.py
import asyncio
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _Resp:
    stop_reason = "end_turn"

    def __init__(self, text):
        class B:
            type = "text"
            def __init__(self, t):
                self.text = t
        self.content = [B(text)]


class _Messages:
    def __init__(self, delay, payload):
        self.delay = delay
        self.payload = payload

    async def create(self, **kwargs):
        await asyncio.sleep(self.delay)
        return _Resp(self.payload)


class _Client:
    def __init__(self, delay, payload):
        self.messages = _Messages(delay, payload)


def make_rec(delay, payload='{"intent":"work_permit","confidence":0.9,"reasoning":"x"}'):
    from core.intent_recognizer import IntentRecognizer
    from core.intent_bge import BGEProtoClassifier
    rec = IntentRecognizer(api_key="k", base_url="http://127.0.0.1:9/v1", model="m")
    rec.client = _Client(delay, payload)
    rec._bge = None                     # 走 LLM+关键词融合路径，避免本地模型加载
    return rec


def main():
    saved = os.environ.get("SAFETYMIND_INTENT_LLM_TIMEOUT_S")

    def set_to(v):
        if v is None:
            os.environ.pop("SAFETYMIND_INTENT_LLM_TIMEOUT_S", None)
        else:
            os.environ["SAFETYMIND_INTENT_LLM_TIMEOUT_S"] = str(v)

    try:
        # ── 1. 慢端点：超时后快速返回，且仍是可用意图 ─────────────────────
        set_to("1")
        rec = make_rec(delay=10)
        t = time.perf_counter()
        r = asyncio.run(rec.recognize("车间有氨气泄漏了怎么办"))
        ms = (time.perf_counter() - t) * 1000
        record("慢端点被 1s 超时截断（不再白等 10s）", ms < 3000, f"ms={ms:.0f}")
        record("超时后仍返回可用意图（不抛异常、不空结果）",
               r.intent is not None and bool(getattr(r.intent, "value", "")),
               f"intent={r.intent}")
        record("超时被标记为降级（degraded=True）", getattr(r, "degraded", False) is True,
               f"degraded={getattr(r, 'degraded', None)}")

        # ── 2. 快端点：不走超时分支，结果与耗时正常 ───────────────────────
        rec = make_rec(delay=0.05)
        t = time.perf_counter()
        r = asyncio.run(rec.recognize("动火作业票怎么办理"))
        ms = (time.perf_counter() - t) * 1000
        record("快端点不受影响（正常返回且快）", ms < 2000, f"ms={ms:.0f}")
        record("快端点意图正确", getattr(r.intent, "value", "") == "work_permit",
               f"intent={r.intent}")
        record("快端点不标记降级", getattr(r, "degraded", True) is False,
               f"degraded={getattr(r, 'degraded', None)}")

        # ── 3. 默认（未设环境变量）不启用超时 → 等满 ────────────────────
        set_to(None)
        rec = make_rec(delay=1.2)
        t = time.perf_counter()
        r = asyncio.run(rec.recognize("甲醇的闪点是多少"))
        ms = (time.perf_counter() - t) * 1000
        record("默认不启用超时（会等满端点返回）", ms >= 1000, f"ms={ms:.0f}")
        record("默认路径结果仍可用", r.intent is not None)
    finally:
        set_to(saved)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
