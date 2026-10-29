# -*- coding: utf-8 -*-
"""生产就绪门禁（Production Readiness Gate）——"没上线怎么证明能用"的可执行答案。

## 面试暴露的问题

"项目没上线，你怎么验证？" 如果答案只有"可观测、可回归"这种话，等于没有验收标准。
本脚本把那句话变成**可执行、可证伪、可复跑**的十条门禁，每条都有明确阈值与实测证据。

## 十条门禁（对齐真实生产关注点）

| # | 门禁 | 阈值 | 为什么这条重要 |
|---|---|---|---|
| G1 | 服务可启动且健康 | /health 200 且 status=ok | 起不来一切都是空谈 |
| G2 | 冷启动就绪时间 | ≤ 120s | 2 核小机器上重启是常态，慢到分钟级会被认为挂了 |
| G3 | 关键端点可用性 | 全部 2xx（公共+管理，按形态判据） | 功能面完整 |
| G4 | 并发不崩 | 200 请求/并发 20，失败率 0 | 单人演示不暴露并发问题，多用户才是生产 |
| G5 | 压测后存活 | 压测后 /health 仍 200 | 内存泄漏/句柄泄漏的快速信号 |
| G6 | 主链路有真实回答 | 非空回答率 100%（≥20 字） | 本轮实战发现过"200 但用户拿不到答案" |
| G7 | 数据落盘可恢复 | 写会话→重启→读回一致 | 内存态服务重启即丢，生产不可接受 |
| G8 | 安全边界 | 网络形态管理端点无令牌必拒 | 部署到服务器就暴露在公网 |
| G9 | 可观测性 | /metrics 有指标且 /monitor 有数据 | 出问题要能看见 |
| G10 | 优雅关闭 | 关闭不抛未捕获异常、不留悬挂 | 每次重启都留脏状态会被运维恨 |

## 纪律

- 每条门禁**必须实测**，不通过就 FAIL，禁止因为"应该没问题"而跳过。
- 服务由本脚本自己拉起并清理（隔离存储，不碰桌面端 data/）。
- dead-LLM 形态也能跑（除 G6 需要真实模型时可开 --live）。

用法:
    <venv python> benchmarks/prod_readiness.py                 # dead-LLM 全跑
    <venv python> benchmarks/prod_readiness.py --live          # 含真实模型主链路
    <venv python> benchmarks/prod_readiness.py --json out.json # 落盘
"""
import argparse
import concurrent.futures as cf
import threading
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO = Path("D:/claudecode/SafetyMind")
PY = REPO / ".venv" / "Scripts" / "python.exe"
ISO_ROOT = Path("D:/claudecode/scratch/sm-loop/iso/prod_readiness")

RESULTS = []


def record(gate: str, ok: bool, detail: str, evidence: str = "", severity: str = "high"):
    RESULTS.append({"gate": gate, "ok": bool(ok), "detail": detail,
                    "evidence": evidence[:600], "severity": severity})
    print(f"[{'PASS' if ok else 'FAIL'}] {gate} — {detail}", flush=True)


def env_for(iso: Path, live: bool, port: int) -> dict:
    e = dict(os.environ)
    e.update({
        "HF_HOME": "D:/hf_cache",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "CHROMA_PERSIST_DIRECTORY": str(iso / "chroma"),
        "CHROMA_KB_PERSIST_DIRECTORY": str(iso / "kb_chroma"),
        "AUDIT_DB_PATH": str(iso / "audit.db"),
        "SAFETYMIND_CONVERSATIONS_DIR": str(iso / "conversations"),
        "API_HOST": "127.0.0.1",
        "API_PORT": str(port),
        "REDIS_URL": "redis://127.0.0.1:9/0",
        "CHROMA_HOST": "127.0.0.1",
        "CHROMA_PORT": "9",
        "PROMETHEUS_PORT": str(port + 100),   # 隔离的指标端口，避免撞用户 9091
        "MONITOR_INTERVAL": "2",
        "SAFETYMIND_LLM_TIMEOUT_S": "90",
    })
    if not live:
        e["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:9"
        e.setdefault("ANTHROPIC_API_KEY", "dummy")
        e["ANTHROPIC_MODEL"] = "dummy"
    return e


def wait_health(base: str, timeout: float) -> tuple[bool, float]:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = httpx.get(base + "/health", timeout=3)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True, time.time() - t0
        except Exception:
            pass
        time.sleep(0.5)
    return False, time.time() - t0


def pct(xs, p):
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="含真实模型主链路（G6）")
    ap.add_argument("--port", type=int, default=8801)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    port = args.port
    base = f"http://127.0.0.1:{port}"
    iso = ISO_ROOT / f"run_{int(time.time())}"
    # 目录必须全新（chroma 复用会放大偶发故障）
    if iso.exists():
        shutil.rmtree(str(iso), ignore_errors=True)
    iso.mkdir(parents=True, exist_ok=True)

    print(f"=== 生产就绪门禁开始 port={port} live={args.live} iso={iso} ===\n")
    t_boot = time.time()
    # 启动子进程并**持续抽干输出**。
    #
    # 踩过的坑（务必保留这段注释）：本脚本第一版用 stdout=PIPE 但只在最后才
    # communicate()，服务启动会打 70+ 行日志，管道缓冲区（Windows 约 4KB）写满后
    # 子进程阻塞在 write() 上，永远到不了 "Application startup complete" ——
    # 表现是"服务 180s/362s 都起不来"，而单独跑 diagnose_boot_hang.py 只要 22.9s。
    # 这**不是产品缺陷，是评测脚本自身的死锁**。所以这里用后台线程实时读走输出。
    proc = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env_for(iso, args.live, port),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace", bufsize=1,
    )
    _boot_lines: list[str] = []
    _log_lock = threading.Lock()

    def _drain(stream) -> None:
        try:
            for raw in iter(stream.readline, ""):
                ln = raw.rstrip()
                if ln:
                    with _log_lock:
                        _boot_lines.append(ln)
        except Exception:
            pass

    threading.Thread(target=_drain, args=(proc.stdout,), daemon=True).start()

    def _tail_log(n: int = 40) -> str:
        with _log_lock:
            return "\n".join(_boot_lines[-n:])

    def drain_and_stop() -> str:
        """终止进程并返回已收集的日志（日志由后台线程持续读取，不会漏）。"""
        try:
            proc.terminate()
            proc.wait(timeout=25)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=10)
            except Exception:
                pass
        except Exception:
            pass
        time.sleep(0.3)
        return _tail_log(60)

    try:
        # ── G1 启动与健康 ────────────────────────────────────────────
        ok, secs = wait_health(base, 180)
        record("G1 服务可启动且健康", ok,
               f"/health {'200 status=ok' if ok else '在 180s 内未就绪'}",
               f"启动耗时 {secs:.1f}s")
        if not ok:
            log = drain_and_stop()
            record("G1b 启动失败日志", False, "服务未就绪，后续门禁无法执行", log[-1500:])
            raise SystemExit(_summary(args, iso))

        # ── G2 冷启动就绪时间 ────────────────────────────────────────
        record("G2 冷启动就绪时间 ≤120s", secs <= 120,
               f"实测 {secs:.1f}s（阈值 120s）", f"含知识库/记忆/Skills/BGE 预热")

        c = httpx.Client(base_url=base, timeout=90.0,
                         limits=httpx.Limits(max_connections=64, max_keepalive_connections=32))

        # ── G3 关键端点可用性 ────────────────────────────────────────
        endpoints = ["/health", "/config", "/skills", "/knowledge/stats", "/monitor",
                     "/eval/last", "/", "/api/python/skills", "/api/python/openapi.json"]
        bad = []
        for p in endpoints:
            try:
                r = c.get(p)
                if r.status_code >= 400:
                    bad.append(f"{p}={r.status_code}")
            except Exception as e:
                bad.append(f"{p}={type(e).__name__}")
        record("G3 关键端点可用性", not bad, f"{len(endpoints)} 个端点全部 2xx" if not bad else f"异常: {bad}",
               str(bad)[:400])

        # ── G4 并发不崩 ─────────────────────────────────────────────
        lat, errs = [], []

        def one(_):
            t = time.perf_counter()
            try:
                r = c.get("/health")
                if r.status_code >= 400:
                    errs.append(f"HTTP{r.status_code}")
                    return None
                return (time.perf_counter() - t) * 1000
            except Exception as e:
                errs.append(type(e).__name__)
                return None

        with cf.ThreadPoolExecutor(max_workers=20) as ex:
            lat = [v for v in ex.map(one, range(200)) if v is not None]
        fail_rate = len(errs) / 200
        record("G4 并发不崩（200 请求/并发 20，失败率 0）", len(errs) == 0,
               f"成功 {len(lat)} 失败 {len(errs)}（失败率 {fail_rate:.1%}）p50={statistics.median(lat):.0f}ms "
               f"p95={pct(lat, 95):.0f}ms" if lat else f"全部失败 {errs[:3]}",
               str(errs[:5])[:300])

        # ── G5 压测后存活 ───────────────────────────────────────────
        try:
            r = c.get("/health")
            alive = r.status_code == 200
        except Exception:
            alive = False
        record("G5 压测后服务仍存活", alive, "压测后 /health 200" if alive else "压测后服务失去响应")

        # ── G6 主链路有真实回答 ─────────────────────────────────────
        chat_cases = ["动火作业票的有效期是怎么规定的？", "甲醇的闪点是多少？"]
        replies = []
        for q in chat_cases:
            try:
                r = c.post("/chat", json={"message": q, "user_id": "pr_gate",
                                          "conv_id": f"pr_{int(time.time())}"}, timeout=180)
                b = r.json() if r.status_code == 200 else {}
                txt = (b.get("response") or "").strip()
                replies.append({"q": q, "code": r.status_code, "len": len(txt),
                                "head": txt[:100], "tools": [t.get("name") for t in (b.get("tools_used") or [])]})
            except Exception as e:
                replies.append({"q": q, "error": f"{type(e).__name__}: {e}"})
        # dead 形态下 LLM 不可用，回答会是有界降级文案（也应非空）；live 形态必须是真答案
        min_len = 20
        ok6 = all(r.get("code") == 200 and r.get("len", 0) >= min_len for r in replies)
        record(f"G6 主链路回答非空（≥{min_len} 字）", ok6,
               f"{sum(1 for r in replies if r.get('len', 0) >= min_len)}/{len(replies)} 条达标；"
               + ("live 形态需真实答案" if args.live else "dead 形态为有界降级文案"),
               json.dumps([{k: v for k, v in r.items() if k != 'q'} for r in replies], ensure_ascii=False)[:500])

        # ── G7 数据落盘可恢复（重启后读回一致）─────────────────────
        marker_uid = f"pr_persist_{int(time.time())}"
        marker_conv = f"pr_conv_{int(time.time())}"
        try:
            c.post("/chat", json={"message": "动火作业票有效期多久？", "user_id": marker_uid,
                                  "conv_id": marker_conv}, timeout=180)
            convs_before = c.get("/conversations", params={"user_id": marker_uid}).json().get("conversations", [])
            persist_ok = any(x.get("conv_id") == marker_conv for x in convs_before)
        except Exception as e:
            persist_ok = False
            convs_before = []
        record("G7a 会话可写入", persist_ok, f"写入后列表含该会话={persist_ok}",
               json.dumps(convs_before[:2], ensure_ascii=False)[:300])

        # ── G8 安全边界（网络形态管理端点必须拒绝）─────────────────
        # 这里用同一进程改不了 API_HOST，改为独立起一个网络形态实例验证（见 build_network_check）
        record("G8 安全边界（网络形态管理端点拒绝）", _check_network_form(port + 2),
               "独立起 API_HOST=0.0.0.0 实例（仅绑回环）验证无令牌必拒", "", "high")

        # ── G9 可观测性 ────────────────────────────────────────────
        try:
            mon = c.get("/monitor")
            mon_ok = mon.status_code == 200 and len(mon.json()) > 0
        except Exception:
            mon_ok = False
        metrics_ok = False
        try:
            m = httpx.get(f"http://127.0.0.1:{port + 100}/metrics", timeout=5)
            metrics_ok = m.status_code == 200 and b"safetymind" in m.content.lower() or b"#" in m.content
        except Exception:
            metrics_ok = False
        record("G9 可观测性（/monitor 与 Prometheus /metrics）", mon_ok and metrics_ok,
               f"/monitor={mon_ok} /metrics={metrics_ok}",
               "监控端点与 Prometheus 指标端口均需可用", "medium")

        c.close()

        # ── G10 优雅关闭 ───────────────────────────────────────────
        log = drain_and_stop()
        crashed = "Traceback (most recent call last)" in log
        record("G10 优雅关闭无未捕获异常", not crashed,
               "关闭未出现 Traceback" if not crashed else "关闭过程出现 Traceback",
               log[-800:], "medium")

        # ── G7b 重启后读回（必须在新进程里验证，所以放在关闭之后）───
        # 注意：这里同样必须把 stdout 导到文件而不是 PIPE。
        # 这是本脚本第三处踩到同一个坑——PIPE 不抽干 → 缓冲写满 → 子进程阻塞 →
        # 服务永不就绪 → 被误判成"重启后会话丢失"。产品侧经最小复现
        # （scratch/sm-loop/probe_persist.py）已证明持久化本身完全正常。
        log2 = iso / "boot2.log"
        lf2 = open(log2, "w", encoding="utf-8", errors="replace")
        proc2 = subprocess.Popen(
            [str(PY), "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(REPO), env=env_for(iso, args.live, port),
            stdout=lf2, stderr=subprocess.STDOUT,
        )
        ok2, secs2 = wait_health(base, 240)
        if ok2:
            try:
                r = httpx.get(f"{base}/conversations", params={"user_id": marker_uid}, timeout=30)
                convs_after = r.json().get("conversations", [])
                restored = any(x.get("conv_id") == marker_conv for x in convs_after)
            except Exception as ex:
                restored = False
                print(f"  [G7b] 读回异常：{type(ex).__name__}: {ex}", flush=True)
        else:
            restored = False
            try:
                lf2.flush()
                print(f"  [G7b] 重启未就绪，日志尾：\n"
                      f"{log2.read_text(encoding='utf-8', errors='replace')[-600:]}", flush=True)
            except Exception:
                pass
        record("G7b 重启后会话仍可读回（持久化有效）", restored,
               "新进程重启后仍读回该会话" if restored else "重启后会话丢失",
               f"重启就绪 {secs2:.1f}s；(就绪={ok2})", "high")
        try:
            proc2.terminate()
            proc2.wait(timeout=25)
        except Exception:
            proc2.kill()
        try:
            lf2.close()
        except Exception:
            pass

    except SystemExit:
        raise
    except Exception as e:
        record("门禁执行异常", False, f"{type(e).__name__}: {e}", "", "high")
        try:
            drain_and_stop()
        except Exception:
            pass

    return _summary(args, iso)


def _check_network_form(port: int) -> bool:
    """独立起一个 API_HOST=0.0.0.0（网络形态）实例，验证管理端点无令牌必被拒。

    uvicorn 仍只绑 127.0.0.1，所以不会真的暴露端口；这里验证的是产品内部的形态判定。
    """
    iso = ISO_ROOT / f"net_{int(time.time())}"
    iso.mkdir(parents=True, exist_ok=True)
    env = env_for(iso, False, port)
    env["API_HOST"] = "0.0.0.0"
    base = f"http://127.0.0.1:{port}"
    # 同 G1 的教训：PIPE 必须配后台线程抽干，否则启动日志写满缓冲区 → 子进程阻塞 → 永远不就绪
    log_path = iso / "net_boot.log"
    lf = open(log_path, "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen(
        [str(PY), "-m", "uvicorn", "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO), env=env, stdout=lf, stderr=subprocess.STDOUT,
    )
    try:
        ok, _ = wait_health(base, 240)
        if not ok:
            logger_tail = ""
            try:
                lf.flush()
                logger_tail = log_path.read_text(encoding="utf-8", errors="replace")[-800:]
            except Exception:
                pass
            if logger_tail:
                print(f"  [网络形态] 启动失败日志尾部：\n{logger_tail}", flush=True)
            return False
        denied = 0
        paths = ["/config/admin", "/tickets", "/audit", "/notifications", "/skills/candidates"]
        for path in paths:
            try:
                r = httpx.get(base + path, timeout=15)
                print(f"  [网络形态] {path} -> {r.status_code}", flush=True)
                if r.status_code in (401, 403, 503):
                    denied += 1
            except Exception as ex:
                print(f"  [网络形态] {path} -> EXC {type(ex).__name__}", flush=True)
                return False
        return denied == len(paths)
    finally:
        try:
            p.terminate()
            p.wait(timeout=20)
        except Exception:
            p.kill()
        try:
            lf.close()
        except Exception:
            pass


def _summary(args, iso) -> int:
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["ok"])
    print(f"\n=== 生产就绪门禁：{passed}/{total} 项通过 ===")
    for r in RESULTS:
        if not r["ok"]:
            print(f"  FAIL {r['gate']}: {r['detail'][:160]}")
    out = {"ranAt": time.strftime("%Y-%m-%dT%H:%M:%S"), "live": args.live,
           "iso": str(iso), "total": total, "passed": passed,
           "failed": [r for r in RESULTS if not r["ok"]], "results": RESULTS}
    target = args.json or f"D:/claudecode/scratch/sm-loop/runs/prod_readiness_{int(time.time())}.json"
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"written {target}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
