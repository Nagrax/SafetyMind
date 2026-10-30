# -*- coding: utf-8 -*-
# 指标端口不可用时的降级回归测试（2026-10-30 闭环第 56 轮）。
#
# ## 缺陷（真机实测）
#
# 本机 `.env` 曾把 `PROMETHEUS_PORT` 设成 **9091**（Windows 保留端口段 9011–9110），
# `prometheus_client.start_http_server(9091)` 抛 `WinError 10013`，异常一路冒到
# `PerformanceMonitor.__init__` → **应用启动直接失败**，17 个测试文件连带失败。
# 监控是可观测性增强，不该成为业务前提。
#
# ## 修法
#
# 绑定失败 → 记一条 warning、把 `_prom_ok` 置 False、**不禁用 Monitor 本身**
# （采集/告警/建议照常），对话链路完全不受影响。
#
# ## 本测试锁定
#
# 1. 端口绑定抛异常时构造不失败，且 `_prom_ok is False`
# 2. 端口正常时 `_prom_ok is True`
# 3. 未配置端口时 `_prom_ok is False` 且不尝试绑定
# 4. 降级后 Monitor 仍能 start/stop 并产出 summary（功能未被误伤）
#
# 用法: python tests/test_prometheus_port_degrade.py
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


class _FakeOrch:
    def get_stats(self):
        return {"general_0": {"total": 1, "success_rate": 1.0, "avg_ms": 10.0,
                              "monitor_penalty": 0.0, "routing_score": 0.9,
                              "completed": 1, "failed": 0, "in_flight": 0}}

    def update_routing_penalties(self, penalties):
        self._p = penalties


class _FakeTools:
    def get_stats(self):
        return {}


def main():
    import monitor.performance_monitor as pm
    from monitor.performance_monitor import PerformanceMonitor

    orig = pm.start_http_server

    try:
        # ── 1. 绑定失败 → 降级而不是崩 ────────────────────────────────────
        def boom(port):
            raise OSError(10013, "以一种访问权限不允许的方式做了一个访问套接字的尝试")
        pm.start_http_server = boom
        mon = PerformanceMonitor(_FakeOrch(), _FakeTools(), prometheus_port=9091)
        record("端口绑定失败时构造不抛异常", mon is not None)
        record("降级后 _prom_ok 为 False", mon._prom_ok is False, f"got={mon._prom_ok}")
        record("降级后仍创建了指标对象（供进程内 /metrics 使用）",
               "requests_total" in mon._prom)

        # ── 2. 降级后 Monitor 主功能仍可用 ───────────────────────────────
        async def life():
            await mon.start()
            await asyncio.sleep(0.05)
            s = mon.summary()
            await mon.stop()
            return s
        summary = asyncio.run(life())
        record("降级后 start/stop 与 summary 正常",
               isinstance(summary, dict) and "agent_stats" in summary,
               f"keys={list(summary)[:5]}")

        # ── 3. 端口正常 → _prom_ok True ──────────────────────────────────
        pm.start_http_server = lambda port: None
        mon2 = PerformanceMonitor(_FakeOrch(), _FakeTools(), prometheus_port=19090)
        record("端口可用时 _prom_ok 为 True", mon2._prom_ok is True)

        # ── 4. 未配置端口 → 不绑定、_prom_ok False ───────────────────────
        called = {"n": 0}
        pm.start_http_server = lambda port: called.__setitem__("n", called["n"] + 1)
        mon3 = PerformanceMonitor(_FakeOrch(), _FakeTools(), prometheus_port=None)
        record("未配置端口时不尝试绑定", called["n"] == 0, f"calls={called['n']}")
        record("未配置端口时 _prom_ok 为 False", mon3._prom_ok is False)

        # ── 5. 同名指标二次注册 → 复用而不是崩 ──────────────────────────
        # （prometheus 默认 Registry 是进程级单例；测试/reload/多实例都会遇到）
        pm.start_http_server = lambda port: None
        try:
            mon4 = PerformanceMonitor(_FakeOrch(), _FakeTools(), prometheus_port=19091)
            ok = mon4._prom_ok is True and "requests_total" in mon4._prom
            record("第二个实例复用已注册指标（不抛 Duplicated timeseries）", ok)
            # 复用后写入不应报错
            mon4._prom["requests_total"].inc()
            mon._prom["requests_total"].inc()
            record("复用后指标写入正常", True)
        except ValueError as ex:
            record("第二个实例复用已注册指标（不抛 Duplicated timeseries）", False, str(ex))
    finally:
        pm.start_http_server = orig

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
