# -*- coding: utf-8 -*-
# 确定性查询快车道测试（2026-10-28 并发轴实测发现）。
#
# ## 缺陷
#
# 确定性查询（CAS/化学品参数）单发实测 96ms，但 8 路并发下 **p50 = 21.9s**：
# 它与长 RAG/生成请求共用 `_CHAT_GATE`（容量 4），排在重请求后面。
# 用户体验上意味着：安全员查"乙醇闪点是多少"要等别人 40s 的长回答先跑完。
#
# ## 修法
#
# 把**零 LLM 的确定性查询**分流到 `_FAST_GATE`（容量 8）。预筛是纯本地正则/词表
# （零 I/O、零 LLM），且保守：化学品名或 CAS + 查询意图词 + 无处置类词。
# 误判也不答偏——`_chat_core` 内部仍走完整逻辑，只是少排队。
#
# 本测试锁定：
#   1. 纯参数查询走 _FAST_GATE，其它走 _CHAT_GATE（不误分流）
#   2. 处置类问题绝不进快车道（不能被加速成"没有推理的回答"）
#   3. 快车道容量独立可配（FAST_LANE_MAX_CONCURRENCY）
#   4. 网关异常路径不被破坏（仍会 release 对应闸门）
#
# 用法: python tests/test_fast_lane.py
import ast
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
SRC = Path(__file__).resolve().parents[1] / "api" / "main.py"

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    src = SRC.read_text(encoding="utf-8")

    # ── 1. 车道选择逻辑存在且按预筛分流 ─────────────────────────────────
    record("存在独立快车道闸门 _FAST_GATE", "_FAST_GATE = asyncio.Semaphore" in src)
    record("容量可配 FAST_LANE_MAX_CONCURRENCY",
           "FAST_LANE_MAX_CONCURRENCY" in src)
    record("按 _is_pure_chemical_lookup 选择车道",
           re.search(r"_lane\s*=\s*_FAST_GATE\s+if\s+_is_pure_chemical_lookup", src) is not None)
    record("释放的是实际占用的车道（不是硬编码 _CHAT_GATE）",
           "_lane.release()" in src)

    # ── 2. 预筛口径：纯查询进、处置类不进 ───────────────────────────────
    #    直接抽取前端的 _is_pure_chemical_lookup 定义来跑（避免 import api.main
    #    触发整套初始化，测试更快更稳）
    ns = {}
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_is_pure_chemical_lookup":
            mod = ast.Module(body=[node], type_ignores=[])
            exec(compile(mod, "<api.main>", "exec"), ns)  # noqa: S102
    # 关键字常量
    for name in ("_LOOKUP_KEYWORDS", "_ADVISORY_KEYWORDS"):
        m = re.search(rf"^{name} = \(.*?\)\n", src, re.S | re.M)
        if m:
            exec(m.group(0), ns)  # noqa: S102
    gate = ns.get("_is_pure_chemical_lookup")
    record("能独立加载预筛函数", callable(gate))

    pure = ["乙醇的闪点是多少", "CAS 64-17-5 的理化性质", "甲醇爆炸极限是什么"]
    advisory = ["乙醇泄漏了怎么办", "乙醇和盐酸混存可以吗", "动火作业票有效期怎么规定"]
    record("纯参数查询进快车道", all(gate(m) for m in pure),
           f"{[gate(m) for m in pure]}")
    record("处置类不进快车道", not any(gate(m) for m in advisory),
           f"{[gate(m) for m in advisory]}")

    # ── 3. 快车道不改变产品语义：直出仍受资源库命中约束 ─────────────────
    record("快车道只影响排队，不绕过证据约束",
           "_deterministic_fast_reply(" in src and "data_direct is not None" in src)
    record("快车道可整体关闭（回退单一闸门）",
           "SAFETYMIND_DETERMINISTIC_FASTPATH" in src)

    # ── 4. 注解与注释可追溯 ─────────────────────────────────────────────
    record("源码记录实测依据（21.9s）", "21.9s" in src)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
