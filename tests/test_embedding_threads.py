# -*- coding: utf-8 -*-
# 嵌入线程数自适应回归测试（2026-10-29 闭环第 46 轮）。
#
# ## 依据（benchmarks/bench_embedding.py 实测，60 条真实文本）
#
#   threads=1 → 2.28 条/秒（439ms/条）      ← 旧默认
#   threads=2 → 4.14 条/秒（+82%）
#   threads=4 → 6.29 条/秒（+176%）
#   threads=8 → 6.99 条/秒（只再 +11%）
#   batch 8/32/64 在 4 线程下 6.69/6.29/5.89（差异在噪声内，故不改 batch）
#
# ## 本测试锁定
#
# 1. 未设置环境变量时按核数自适应：max(1, min(4, cpu//2))
#    （20 核 → 4；2 核 → 1，避免小机被嵌入抢满 CPU）
# 2. SAFETYMIND_TORCH_THREADS 显式设置优先（含非法值回退 1）
# 3. 结果真的作用到 torch.set_num_threads（不是只算了个数）
#
# 用法: python tests/test_embedding_threads.py
import importlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("HF_HOME", "D:/hf_cache")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    import core.bge_embedding as be
    import torch

    saved = os.environ.get("SAFETYMIND_TORCH_THREADS")
    orig_cpu = os.cpu_count

    def setup(cpu, threads_env):
        os.cpu_count = lambda: cpu
        if threads_env is None:
            os.environ.pop("SAFETYMIND_TORCH_THREADS", None)
        else:
            os.environ["SAFETYMIND_TORCH_THREADS"] = str(threads_env)
        be._apply_torch_thread_limit()
        return torch.get_num_threads()

    try:
        got = setup(20, None)
        record("20 核未设置 → 自适应 4 线程", got == 4, f"got={got}")
        got = setup(8, None)
        record("8 核 → 4 线程", got == 4, f"got={got}")
        got = setup(4, None)
        record("4 核 → 2 线程（cpu//2）", got == 2, f"got={got}")
        got = setup(2, None)
        record("2 核小机 → 1 线程（不抢满 CPU）", got == 1, f"got={got}")
        got = setup(1, None)
        record("1 核 → 1 线程", got == 1, f"got={got}")

        got = setup(20, 2)
        record("显式设置 2 线程时优先于自适应", got == 2, f"got={got}")
        got = setup(20, 6)
        record("显式设置 6 线程也尊重（不夹取）", got == 6, f"got={got}")
        got = setup(20, "abc")
        record("非法取值回退 1 线程", got == 1, f"got={got}")
    finally:
        os.cpu_count = orig_cpu
        if saved is None:
            os.environ.pop("SAFETYMIND_TORCH_THREADS", None)
        else:
            os.environ["SAFETYMIND_TORCH_THREADS"] = saved
        be._apply_torch_thread_limit()

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
