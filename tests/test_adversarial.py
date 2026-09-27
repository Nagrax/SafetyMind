# 对抗性/降级场景测试（2026-09-26 审计固化）：python tests/test_adversarial.py
# 覆盖：LLM 端点死亡时的安全关键行为、本地引擎加载失败后的级联健壮性、
#       评测降级标记、记忆持久化。全部不依赖真实 LLM（死端点 127.0.0.1:9）。
# 退出码 0 = 全过；任一 FAIL = 非零（供 CI/工作流做门禁）。
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PY = sys.executable
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── T1（子进程隔离：需要无 BGE 环境）CRITICAL 紧急消息不得被澄清追问拦截 ──────
T1_CHILD = r'''
import asyncio, sys
sys.path.insert(0, ".")
DEAD = "http://127.0.0.1:9"
async def main():
    from agents.agent_orchestrator import AgentOrchestrator, Request
    orch = AgentOrchestrator(api_key="dummy", base_url=DEAD, model="dummy")
    # “被困”是 CRITICAL 关键词但不在 pattern 词表：LLM 死时意图=OTHER 低置信，
    # 期望仍返回应急模板（紧急度独立于意图置信度），而不是澄清追问。
    r = await orch.run(Request(message="这边有个人被困在反应釜里了快救命", user_id="t", conv_id="t1"))
    # 注意：澄清话术本身含“事故应急”字样，必须同时断言不是澄清追问
    ok = "紧急事态" in r.response and "不能确定" not in r.response
    print("T1_RESULT:", "OK" if ok else "CLARIFICATION:" + r.response[:60].replace("\n", " "))
    sys.exit(0 if ok else 1)
asyncio.run(main())
'''

# ── T2（子进程隔离：需要 SAFETYMIND_BGE=1 + 坏 device）引擎加载失败后不得崩溃 ──
T2_CHILD = r'''
import asyncio, os, sys
sys.path.insert(0, ".")
os.environ["SAFETYMIND_BGE"] = "1"
os.environ["SAFETYMIND_BGE_DEVICE"] = "no-such-device"
DEAD = "http://127.0.0.1:9"
async def main():
    from core.intent_recognizer import IntentRecognizer
    rec = IntentRecognizer(api_key="dummy", base_url=DEAD, model="dummy")
    msgs = ["动火作业票怎么办理", "职业健康体检多久一次", "推荐几首歌"]
    for m in msgs:
        r = await rec.recognize(m)   # 任何一条抛 TypeError = 崩溃（历史 Bug：部分初始化后 fuse(None)）
        print("  intent:", r.intent.value)
    print("T2_RESULT: OK（连续 3 条无崩溃，静默降级正常）")
asyncio.run(main())
'''


def run_child(code, name):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SAFETYMIND_BGE")}
    p = subprocess.run([PY, "-c", code], capture_output=True, text=True,
                       cwd=str(Path(__file__).resolve().parents[1]), env=env,
                       encoding="utf-8", errors="replace", timeout=180)
    return p.returncode == 0, (p.stdout + p.stderr)[-300:].strip()


async def t3_evaluator_degraded():
    from evaluation.evaluator import IntentEvaluator, IntentTestCase
    from core.intent_recognizer import IntentRecognizer
    rec = IntentRecognizer(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")
    m = await IntentEvaluator(rec).evaluate([
        IntentTestCase("动火作业票怎么办", "work_permit"),
        IntentTestCase("你好", "greeting"),
    ])
    record("T3 评测器降级如实标记", m["degraded"] is True and m["llm_failures"] == 2,
           f"degraded={m['degraded']} llm_failures={m['llm_failures']}")


async def t4_memory_persistence():
    from memory.conversation_memory import _MemoryRedis
    p = "data/_adv_mem.json"
    Path(p).unlink(missing_ok=True)
    m1 = _MemoryRedis(p)
    await m1.lpush("wm:u:c", json.dumps({"role": "user", "content": "x", "ts": "t"}))
    await m1.setex("summary:u:c", 86400, "摘要S")
    await m1.setex("gone", 1, "过期")
    await asyncio.sleep(1.2)
    m2 = _MemoryRedis(p)
    ok = (await m2.lrange("wm:u:c", 0, -1)) and await m2.get("summary:u:c") == "摘要S" and await m2.get("gone") is None
    record("T4 记忆快照重启恢复+TTL跨重启", bool(ok))
    Path(p).unlink(missing_ok=True)


async def t5_dead_llm_fallback():
    from core.intent_recognizer import IntentRecognizer
    rec = IntentRecognizer(api_key="dummy", base_url="http://127.0.0.1:9", model="dummy")
    r = await rec.recognize("动火作业票怎么办理")
    record("T5 死端点回退不崩溃且意图可用", r.intent.value == "work_permit" and r.confidence > 0,
           f"intent={r.intent.value} conf={r.confidence}")


async def main():
    ok1, out1 = run_child(T1_CHILD, "T1")
    record("T1 CRITICAL 紧急消息优先于澄清追问", ok1, out1)
    ok2, out2 = run_child(T2_CHILD, "T2")
    record("T2 本地引擎加载失败后连续调用不崩溃", ok2, out2)
    await t3_evaluator_degraded()
    await t4_memory_persistence()
    await t5_dead_llm_fallback()
    failed = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} PASSED" + (f"，失败: {[r[0] for r in failed]}" if failed else ""))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
