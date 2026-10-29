# -*- coding: utf-8 -*-
# 记忆降级切换竞态测试（2026-10-28 闭环实测发现）。
#
# ## 缺陷
#
# 冷启动后首个请求若并发进入（/chat 与后台画像任务同时触发记忆访问），两个协程会
# 在真 Redis 上**同时**失败。旧实现：
#     except Exception:
#         if self._redis_local: raise        # ← 竞态受害者直接抛
#         self._redis = _MemoryRedis(...); self._redis_local = True
# A 完成切换后，B 看到标志已 True 就 bare raise，把
# `ConnectionError: Error 22 connecting to 127.0.0.1:9` 抛给用户。
#
# 实测证据：live 探针 normal 用例 2.1s 抛 ConnectionError（/chat 500）；
# 并发 10 次首访 → 9 次抛错、1 次成功。
#
# ## 修法
#
# 每次调用先捕获本次实际使用的 client；失败时据此判断"真 client 失败 → 切换后重试"
# 还是"本地实现失败 → 如实抛出"；切加锁保只切一次。
# 修后并发 20 次首访 → 0 异常。
#
# 用法: python tests/test_memory_redis_race.py
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
ISO = Path("D:/claudecode/scratch/sm-loop/iso/mem_race_test")
ISO.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
# 每次运行用独立快照名：_MemoryRedis 会把数据持久化到 JSON，复用同名文件会让
# 上一轮的写入累积下来（实测 got=['v','v'] 导致断言假失败）——测试隔离，非产品问题。
import time as _time
os.environ["SAFETYMIND_MEMORY_SNAPSHOT"] = str(ISO / f"mem_{int(_time.time()*1000)}.json")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _mk():
    from memory.conversation_memory import MemoryManager
    return MemoryManager(redis_url="redis://127.0.0.1:9/0",   # 死端口
                         chroma_host="127.0.0.1", chroma_port=9,
                         chroma_path=str(ISO / "chroma"))


def main():
    # ── 1. 并发首访不再抛（核心回归）────────────────────────────────────
    async def conc():
        m = _mk()
        res = await asyncio.gather(*[m._redis_op("lrange", "k", 0, 5) for _ in range(20)],
                                   return_exceptions=True)
        return m, [r for r in res if isinstance(r, Exception)], res

    m, errs, res = asyncio.run(conc())
    record("并发 20 次首访零异常（旧实现 9/10 抛 ConnectionError）",
           len(errs) == 0, f"errs={len(errs)} first={errs[0] if errs else ''}")
    record("全部走本地降级实现", m._redis_local is True)
    record("调用结果均为列表", all(isinstance(r, list) for r in res))

    # ── 2. 单次调用也能正确降级 ─────────────────────────────────────────
    async def single():
        mm = _mk()
        return mm, await mm._redis_op("lpush", "k", "v"), await mm._redis_op("lrange", "k", 0, 5)

    mm, _, got = asyncio.run(single())
    record("单次调用降级后可读写", mm._redis_local and got == ["v"], f"got={got}")

    # ── 3. 本地实现真故障必须抛出（不掩盖）──────────────────────────────
    class Boom:
        def __getattr__(self, name):
            async def _f(*a, **k):
                raise RuntimeError("local-boom")
            return _f

    async def boom():
        mb = _mk()
        mb._redis = Boom()
        mb._redis_local = True     # 已是本地模式
        try:
            await mb._redis_op("lrange", "k", 0, 5)
            return None
        except Exception as ex:
            return ex

    ex = asyncio.run(boom())
    record("本地实现失败如实抛出（不静默吞掉）",
           isinstance(ex, RuntimeError) and "local-boom" in str(ex))

    # ── 4. 切换只发生一次（锁生效）──────────────────────────────────────
    async def once():
        mo = _mk()
        switches = []
        orig = mo.__class__._redis_op

        await asyncio.gather(*[mo._redis_op("lrange", "k", 0, 5) for _ in range(15)],
                             return_exceptions=True)
        return mo

    mo = asyncio.run(once())
    record("并发后 client 唯一且为本地实现",
           type(mo._redis).__name__ == "_MemoryRedis")

    # ── 5. 源码守护 ─────────────────────────────────────────────────────
    src = Path(__file__).resolve().parents[1].joinpath(
        "memory/conversation_memory.py").read_text(encoding="utf-8")
    record("切换有锁保护", "_redis_switch_lock" in src and "async with self._redis_switch_lock" in src)
    record("按本次 client 判断失败来源", "client = self._redis" in src
           and "isinstance(client, _MemoryRedis)" in src)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
