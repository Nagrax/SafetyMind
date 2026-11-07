# -*- coding: utf-8 -*-
# SM-01 第三片：只读 smoke 探活（G0 门禁层——服务可达 + 基本链路通）。
# 设计：scratch/sm-loop/handoff/SM-01/slice3-design.md（G0 层："服务起得来、
# 基本链路通"；与破坏性演练 tests/test_destructive_isolated.py 互补：smoke 不做
# 任何破坏性序列，失败单独归因到端点）。
#
# 探活三项（真实 app 入口 with TestClient 触发 lifespan，假模型 = 死 LLM 端点）：
#   1. GET  /health      → 200 且 status=ok（lifespan 真实初始化的编排器在线）
#   2. POST /chat 一次   → 200 且回答非空、带上报编号（假模型下走确定性兜底链路）
#   3. POST /eval/run    → 200 且报告结构完整（评测输入端点接受显式最小输入）
#
# 故障分型（退出码三态，环境故障绝不伪装成业务失败）：
#   exit 2 = 环境不可用：依赖缺失 / app 导入失败 / lifespan 起不来 —— 打 [REJECT]；
#   exit 1 = 端点故障：服务起来了但某端点状态/结构不符 —— 逐端点 [FAIL] 并单独计数；
#   exit 0 = 三项全过。
# 统计行：X/Y PASS（端点故障 N 个）+ 等价的 N/M PASSED 行（runner 统计判据
# parse_counts 只识别 PASSED/passed 形态，两行数值恒一致）。
#
# 铁律：不清理 runner 提供的隔离存储（iso 目录归 runner 丢弃）；smoke 自建目录
# 只进 D 盘 scratch 且 finally 清除；EVAL_BASELINE_PATH/EVAL_LAST_REPORT_PATH
# 强制钉到 smoke 自建目录（产品默认 /app/data/eval 与仓库 data/eval 都不许写）。
import gc
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 共享存储自检 helper

SCRATCH = Path("D:/claudecode/scratch/sm-loop")
TMP = Path(tempfile.mkdtemp(prefix="sm01_smoke_", dir=str(SCRATCH)))

# runner 已提供的存储旋钮一律沿用（setdefault）；评测产物路径产品默认会写
# /app/data/eval（Windows 下落到当前盘 D:\app）或仓库 data/eval——必须钉掉。
STORAGE_DEFAULTS = {
    "AUDIT_DB_PATH": "audit.db",
    "SAFETYMIND_CONVERSATIONS_DIR": "conversations",
    "CHROMA_PERSIST_DIRECTORY": "chroma",
    "CHROMA_KB_PERSIST_DIRECTORY": "kb_chroma",
    "PREFERENCE_DB_PATH": "preference_cards.db",
    "SAFETYMIND_MEMORY_SNAPSHOT": "memory_snapshot.json",
    "SAFETYMIND_SKILL_EVO_DIR": "skill_evo",
}


def pin_env():
    for key, name in STORAGE_DEFAULTS.items():
        os.environ.setdefault(key, str(TMP / name))
    os.environ.setdefault("EVAL_GATE_RECORD_PATH", str(TMP / "eval" / "eval_gate_record.json"))
    # 评测产物路径产品默认会写 /app/data/eval（Windows 落到 D:\app）或仓库 data/eval，
    # 不在 runner 的 STORAGE_ENV_KEYS 守卫清单内（立案见 handoff/SM-01 实测 JSON）——
    # smoke 自身强制钉到自建目录，绝不写这两处。
    for key in ("EVAL_BASELINE_PATH", "EVAL_LAST_REPORT_PATH"):
        os.environ[key] = str(TMP / "eval" / f"{key.lower()}.json")
    os.environ.update({
        "SAFETYMIND_EMBEDDING": "ngram",
        "SAFETYMIND_HAZARD_OVERDUE_CHECK": "0",
        "SAFETYMIND_OUTBOX_ENABLED": "0",
        # 假模型：死 LLM 端点（smoke 验证的是产品在模型不可用时的确定性兜底链路）
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "ANTHROPIC_API_KEY": "dummy",
        "PROMETHEUS_PORT": "0",
    })


def main():
    pin_env()
    try:
        from fastapi.testclient import TestClient
    except Exception as ex:
        print(f"[REJECT] 环境不可用（fastapi TestClient 缺失）: {ex}")
        shutil.rmtree(str(TMP), ignore_errors=True)
        return 2
    try:
        from api.main import app
    except Exception as ex:
        print(f"[REJECT] 环境不可用（产品 app 导入失败）: {type(ex).__name__}: {ex}")
        shutil.rmtree(str(TMP), ignore_errors=True)
        return 2

    probes = []   # (endpoint, ok, detail)

    def probe(endpoint, ok, detail=""):
        probes.append((endpoint, bool(ok), str(detail)[:220]))
        print(f"[{'PASS' if ok else 'FAIL'}] {endpoint}" + (f" — {detail}" if not ok else ""))

    try:
        try:
            client_ctx = TestClient(app)
            client = client_ctx.__enter__()   # lifespan 真实初始化；起不来 = 环境故障
        except Exception as ex:
            print(f"[REJECT] 环境不可用（app lifespan 启动失败）: {type(ex).__name__}: {ex}")
            shutil.rmtree(str(TMP), ignore_errors=True)
            return 2

        try:
            # ── 探活 1：/health ──────────────────────────────────────────────
            try:
                t0 = time.time()
                r = client.get("/health")
                body = r.json() if r.status_code == 200 else {}
                probe("/health", r.status_code == 200 and body.get("status") == "ok",
                      f"code={r.status_code} body={r.text[:160]} ({time.time()-t0:.1f}s)")
            except Exception as ex:
                probe("/health", False, f"端点异常 {type(ex).__name__}: {ex}")

            # ── 探活 2：/chat 假模型一次（法规查询，不该触发建单/升级）─────────
            try:
                t0 = time.time()
                r = client.post("/chat", json={"message": "灭火器多长时间检查一次？",
                                               "user_id": "smoke", "conv_id": f"smoke-{int(time.time())}"})
                body = r.json() if r.status_code == 200 else {}
                probe("/chat(假模型一次)",
                      r.status_code == 200 and bool((body.get("response") or "").strip())
                      and bool(body.get("request_id")),
                      f"code={r.status_code} response_len={len((body.get('response') or ''))} "
                      f"request_id={body.get('request_id')!r} hazard_action={body.get('hazard_action')!r} "
                      f"({time.time()-t0:.1f}s) body={r.text[:160]}")
            except Exception as ex:
                probe("/chat(假模型一次)", False, f"端点异常 {type(ex).__name__}: {ex}")

            # ── 探活 3：/eval/run 评测输入端点（显式最小输入，不跑内置全量用例）──
            try:
                t0 = time.time()
                r = client.post("/eval/run", json={
                    "intent_cases": [{"message": "动火作业票的有效期是怎么规定的",
                                      "expected_intent": "regulation_query"}],
                    "dialog_cases": [],
                })
                body = r.json() if r.status_code == 200 else {}
                probe("/eval/run(输入端点)",
                      r.status_code == 200 and body.get("total", 0) >= 1
                      and "pass_rate" in body and isinstance(body.get("results"), list),
                      f"code={r.status_code} body={r.text[:160]} ({time.time()-t0:.1f}s)")
            except Exception as ex:
                probe("/eval/run(输入端点)", False, f"端点异常 {type(ex).__name__}: {ex}")
        finally:
            try:
                client_ctx.__exit__(None, None, None)
            except Exception:
                pass
    finally:
        # 显式释放 lifespan 建的知识库/记忆 chroma 句柄再清自建目录（Windows 句柄）
        try:
            import api.main as api_main
            tm_ref = api_main._tool_manager
            tool = tm_ref._tools.get("knowledge_search") if tm_ref is not None else None
            kb = getattr(tool.handler, "__self__", None) if tool is not None else None
            if kb is not None and hasattr(kb, "close"):
                try:
                    kb.close()
                except Exception:
                    pass
            mem = api_main._memory
            mem_cli = next((getattr(c, "_client", None)
                            for c in [getattr(mem, "_episodic", None),
                                      getattr(mem, "_profile", None)] if c is not None), None)
            if mem_cli is not None:
                mem_cli.close()
        except Exception:
            pass
        gc.collect()
        shutil.rmtree(str(TMP), ignore_errors=True)

    total = len(probes)
    passed = sum(1 for _, ok, _ in probes if ok)
    endpoint_failures = total - passed
    # 统计行（ask 口径）+ 等价 PASSED 行（runner parse_counts 识别形态，数值恒一致）
    print(f"\n{passed}/{total} PASS（端点故障 {endpoint_failures} 个）")
    print(f"{passed}/{total} PASSED")
    return 0 if endpoint_failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
