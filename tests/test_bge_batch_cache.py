# -*- coding: utf-8 -*-
# BGE 查询缓存"批量放大"回归 + 嵌入结果一致性（**服务 venv**，需 torch/transformers 与
# D:/hf_cache 的 bge-base-zh-v1.5）：用法: python tests/test_bge_batch_cache.py
# 覆盖：按文本逐条缓存 + 向量数/字节数双限额（回归旧的"按批次计数"放大）、
#       建库入口 embed_documents 绕过在线查询缓存、固定文本向量与关闭缓存路径逐位一致、
#       门禁 tests/test_bge_proto.py 仍全过。
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RESULTS = []


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# 固定 20 条对拍文本（构造时去重，保证"同批只推理一次"的批次切分口径一致）
TEXTS20 = [f"第{i}条：动火作业前应进行动火分析并确认监护人到位，落实作业票安全措施。" for i in range(20)]


def _ef():
    from core.bge_embedding import BgeEmbeddingFunction
    return BgeEmbeddingFunction()


def _set_env(**kv):
    old = {}
    for k, v in kv.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = str(v)
    return old


def _restore_env(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def test_embedding_contract_unchanged():
    from core.bge_embedding import BgeEmbeddingFunction
    ef = BgeEmbeddingFunction()
    vecs = ef.embed_query(["动火作业"])
    n = sum(v * v for v in vecs[0])
    record("768 维且已归一化（契约未变）", len(vecs[0]) == 768 and abs(n - 1.0) < 1e-4,
           f"dim={len(vecs[0])} norm={n}")
    record("max_length 仍为 256", ef._max_length == 256)


def test_cache_vectors_limit_evicts():
    import core.bge_embedding as B
    old = _set_env(SAFETYMIND_BGE_CACHE_MAX_VECTORS=128, SAFETYMIND_BGE_CACHE_MAX_BYTES=None)
    try:
        B.clear_embed_cache()
        ef = _ef()
        for i in range(100):  # 100 次小批量 × 4 条 = 400 条向量请求
            ef.embed_query([f"受限空间作业取样分析第{i}组第{j}条记录，氧含量19.5%~21%。"
                            for j in range(4)])
        st = B.cache_stats()
        record("向量数限额生效（大量小批量调用后不超上限）",
               st["vectors"] <= 128 and st["entries"] <= 128, f"stats={st}")
    finally:
        _restore_env(old)
        B.clear_embed_cache()


def test_cache_bytes_limit_evicts():
    import core.bge_embedding as B
    per_vec = 768 * 4
    limit_bytes = per_vec * 10
    old = _set_env(SAFETYMIND_BGE_CACHE_MAX_VECTORS=100000,
                   SAFETYMIND_BGE_CACHE_MAX_BYTES=limit_bytes)
    try:
        B.clear_embed_cache()
        ef = _ef()
        for i in range(30):
            ef.embed_query([f"高处作业分级与安全带使用第{i}组第{j}条说明文本。" for j in range(3)])
        st = B.cache_stats()
        record("字节数限额生效（approx_bytes 不超上限）",
               st["approx_bytes"] <= limit_bytes and st["vectors"] <= 10
               and st["approx_bytes"] == st["vectors"] * per_vec, f"stats={st}")
    finally:
        _restore_env(old)
        B.clear_embed_cache()


def test_default_limits_bounded_growth():
    import core.bge_embedding as B
    old = _set_env(SAFETYMIND_BGE_CACHE_MAX_VECTORS=None, SAFETYMIND_BGE_CACHE_MAX_BYTES=None)
    try:
        B.clear_embed_cache()
        ef = _ef()
        for i in range(60):  # 60 × 8 = 480 条
            ef.embed_query([f"隐患排查治理闭环流程第{i}组第{j}条说明。" for j in range(8)])
        st = B.cache_stats()
        record("默认双限额为 4096 向量 / 24MiB",
               st["max_vectors"] == 4096 and st["max_bytes"] == 24 * 1024 * 1024, f"stats={st}")
        record("调用后未超默认上限（回归批次放大）",
               st["vectors"] <= st["max_vectors"] and st["approx_bytes"] <= st["max_bytes"],
               f"stats={st}")
        record("缓存按'文本条数'计数而非按批次计数",
               st["entries"] == st["vectors"], f"stats={st}")
    finally:
        _restore_env(old)
        B.clear_embed_cache()


def test_embed_documents_bypasses_query_cache():
    import core.bge_embedding as B
    B.clear_embed_cache()
    ef = _ef()
    ef.embed_documents(TEXTS20)
    after = B.cache_stats()
    record("建库入口 embed_documents 不写查询缓存",
           after["entries"] == 0 and after["approx_bytes"] == 0, f"stats={after}")
    ef.embed_query(TEXTS20)
    after2 = B.cache_stats()
    record("查询入口 embed_query 逐条写入缓存",
           after2["entries"] == len(TEXTS20) and after2["vectors"] == len(TEXTS20),
           f"stats={after2}")
    ef.embed_query(TEXTS20)   # 二次查询全命中，不新增条目
    after3 = B.cache_stats()
    record("重复查询命中缓存不新增条目", after3["entries"] == after2["entries"],
           f"stats={after3}")
    B.clear_embed_cache()


def test_results_bitwise_identical_with_cache_bypassed():
    import core.bge_embedding as B
    ef = _ef()
    B.clear_embed_cache()
    cached = ef.embed_query(TEXTS20)
    B.clear_embed_cache()
    bypassed = ef.embed_documents(TEXTS20)
    reference = ef._run_model(TEXTS20)      # 改造前的同一计算路径
    record("缓存路径与关闭缓存路径逐位一致", cached == bypassed)
    record("与改造前计算路径（_run_model）逐位一致", cached == reference)
    record("对拍文本向量数量与维度正确",
           len(cached) == 20 and all(len(v) == 768 for v in cached))
    B.clear_embed_cache()


def test_gate_bge_proto_still_passes():
    out = subprocess.run([sys.executable, str(ROOT / "tests" / "test_bge_proto.py")],
                         capture_output=True, text=True, cwd=str(ROOT))
    record("门禁 tests/test_bge_proto.py 仍全过（意图原型空间未受影响）",
           out.returncode == 0 and "ALL 5 TESTS PASSED" in out.stdout,
           f"rc={out.returncode} tail={out.stdout.strip().splitlines()[-1:]}")


if __name__ == "__main__":
    for fn in [test_embedding_contract_unchanged, test_cache_vectors_limit_evicts,
               test_cache_bytes_limit_evicts, test_default_limits_bounded_growth,
               test_embed_documents_bypasses_query_cache,
               test_results_bitwise_identical_with_cache_bypassed,
               test_gate_bge_proto_still_passes]:
        try:
            fn()
        except Exception as ex:  # noqa: BLE001
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)
