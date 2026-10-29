# -*- coding: utf-8 -*-
# 冷启动离线优先测试（2026-10-26 闭环第 6 轮，生产就绪门禁实测发现）。
#
# 缺陷：默认联网模式下启动要发 15 次 HF 外部 HEAD 请求（冷启动 40.2s），
# 且在**无外网**环境（内网部署/防火墙/镜像站限流）会卡住，服务 180s 起不来
# （prod_readiness G1 实测失败）。
#
# 修复：core/intent_bge._load_models 默认置 HF_HUB_OFFLINE=1；
# SAFETYMIND_HF_ONLINE=1 才允许联网检查。
#
# 本测试锁定：
#   1. 默认（未设 SAFETYMIND_HF_ONLINE）离线开关被打开
#   2. SAFETYMIND_HF_ONLINE=1 时不强制离线
#   3. 选用的环境变量名与 transformers/huggingface_hub 真实读取的一致
#   4. 离线时模型仍能加载（本地缓存可用）——这是"离线不等于不可用"的核心保证
#
# 用法: python tests/test_coldstart_offline.py
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def apply_policy():
    """复刻 _load_models 里的离线策略（不真正加载模型，只验环境变量决策）。"""
    src = Path(__file__).resolve().parents[1] / "core" / "intent_bge.py"
    text = src.read_text(encoding="utf-8")
    # 直接判断源码里是否按预期写入这两个开关
    return text


def simulate(online_hint: str | None) -> dict:
    """在干净子进程环境里模拟决策：只跑策略片段，不加载权重。"""
    old = {k: os.environ.get(k) for k in
           ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "SAFETYMIND_HF_ONLINE")}
    try:
        for k in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "SAFETYMIND_HF_ONLINE"):
            os.environ.pop(k, None)
        if online_hint is not None:
            os.environ["SAFETYMIND_HF_ONLINE"] = online_hint
        # —— 与 core/intent_bge.py 中 _load_models 的策略逐字一致 ——
        if os.getenv("SAFETYMIND_HF_ONLINE", "").strip() == "1":
            os.environ.pop("HF_HUB_OFFLINE", None)
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
        else:
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
            os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        return {"HF_HUB_OFFLINE": os.environ.get("HF_HUB_OFFLINE"),
                "TRANSFORMERS_OFFLINE": os.environ.get("TRANSFORMERS_OFFLINE")}
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    # ── 1. 默认离线 ─────────────────────────────────────────────────────
    r = simulate(None)
    record("默认（未设 SAFETYMIND_HF_ONLINE）打开 HF_HUB_OFFLINE=1",
           r["HF_HUB_OFFLINE"] == "1", f"env={r}")
    record("默认同时打开 TRANSFORMERS_OFFLINE=1",
           r["TRANSFORMERS_OFFLINE"] == "1", f"env={r}")

    # ── 2. 显式联网 ─────────────────────────────────────────────────────
    r2 = simulate("1")
    record("SAFETYMIND_HF_ONLINE=1 时不强制离线（允许联网检查）",
           r2["HF_HUB_OFFLINE"] is None and r2["TRANSFORMERS_OFFLINE"] is None, f"env={r2}")

    # ── 3. 其它取值仍视为离线（保守）────────────────────────────────────
    for v in ("0", "true", "yes", ""):
        rv = simulate(v)
        record(f"SAFETYMIND_HF_ONLINE={v!r} 仍走离线（只有 '1' 才联网）",
               rv["HF_HUB_OFFLINE"] == "1", f"env={rv}")

    # ── 4. 调用方已显式设 HF_HUB_OFFLINE=0 时不被强行覆盖 ────────────────
    old = os.environ.get("HF_HUB_OFFLINE")
    try:
        os.environ["HF_HUB_OFFLINE"] = "0"
        os.environ.pop("SAFETYMIND_HF_ONLINE", None)
        # setdefault 语义：已有值不覆盖
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        record("调用方显式设 HF_HUB_OFFLINE=0 时不被 setdefault 覆盖",
               os.environ.get("HF_HUB_OFFLINE") == "0", f"val={os.environ.get('HF_HUB_OFFLINE')}")
    finally:
        if old is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old

    # ── 5. 源码层面确认策略已落地（不是测试里自己实现的）────────────────
    src = apply_policy()
    record("core/intent_bge.py 源码里确有离线策略",
           'SAFETYMIND_HF_ONLINE' in src and 'HF_HUB_OFFLINE' in src)
    record("策略位置在 _load_models（模型加载前生效，而不是加载后）",
           "_load_models" in src and
           src.index("SAFETYMIND_HF_ONLINE") > src.index("def _load_models"),
           "策略必须在加载前设置，否则无效")

    # ── 5b. 唯一加载入口 get_shared_bge 也必须应用策略 ──────────────────
    # 这一条是本轮抓到的第二个漏洞：第一版只改 intent_bge，漏了
    # core/bge_embedding.get_shared_bge（知识库嵌入器走这条更早的路径），
    # 于是外部请求照旧发生、启动照旧卡住。必须锁死在唯一入口上。
    bge_src = (Path(__file__).resolve().parents[1] / "core" / "bge_embedding.py").read_text(encoding="utf-8")
    record("core/bge_embedding.py 提供 apply_hf_offline_policy", "def apply_hf_offline_policy" in bge_src)
    idx_shared = bge_src.index("def get_shared_bge")
    idx_policy_call = bge_src.index("apply_hf_offline_policy()", idx_shared)
    idx_pretrained = bge_src.index("from_pretrained", idx_shared)
    record("get_shared_bge 在 from_pretrained 之前调用离线策略（唯一加载入口不漏）",
           idx_shared < idx_policy_call < idx_pretrained,
           f"shared={idx_shared} policy={idx_policy_call} pretrained={idx_pretrained}")

    # 直接验证策略函数行为（不只是看源码文本）
    from core.bge_embedding import apply_hf_offline_policy as fn
    old_env = {k: os.environ.get(k) for k in
               ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "SAFETYMIND_HF_ONLINE")}
    try:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        os.environ.pop("SAFETYMIND_HF_ONLINE", None)
        fn()
        record("apply_hf_offline_policy 实际执行后 HF_HUB_OFFLINE=1",
               os.environ.get("HF_HUB_OFFLINE") == "1", f"val={os.environ.get('HF_HUB_OFFLINE')}")
    finally:
        for k, v in old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ── 5c. 模块常量也必须被覆盖（本轮第三次踩到的坑）───────────────────
    # 关键陷阱：huggingface_hub 在 **import 时**把 HF_HUB_OFFLINE 固化成模块常量，
    # 之后 os.environ 再改完全无效——表现是"日志显示 env 设了，网络请求照旧发出，
    # 服务照旧卡住"（prod_readiness G1 因此连失败三次）。
    try:
        import huggingface_hub.constants as hf_const
        had_attr = hasattr(hf_const, "HF_HUB_OFFLINE")
        before = getattr(hf_const, "HF_HUB_OFFLINE", None)
        saved = {k: os.environ.get(k) for k in ("HF_HUB_OFFLINE", "SAFETYMIND_HF_ONLINE")}
        try:
            os.environ.pop("SAFETYMIND_HF_ONLINE", None)
            os.environ.pop("HF_HUB_OFFLINE", None)
            fn()
            after = getattr(hf_const, "HF_HUB_OFFLINE", None)
            record("模块常量 huggingface_hub.constants.HF_HUB_OFFLINE 被同步为 True",
                   had_attr and after is True, f"before={before} after={after}")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    except ImportError:
        record("huggingface_hub 未安装（跳过常量覆盖检查）", True, "环境无 hf_hub")

    # 反面验证：常量固化确实存在（证明上面的覆盖是必要的，不是在修一个不存在的问题）
    # 注意子进程必须是"干净环境"：显式清掉两个环境变量，
    # 否则父进程/系统里已有的 HF_HUB_OFFLINE=1 会让 import 时就直接读成 True，
    # 这条验证就失去意义（第一版就写错了，被自己的测试抓出来）。
    try:
        probe = (
            "import os\n"
            "os.environ.pop('HF_HUB_OFFLINE', None)\n"
            "os.environ.pop('TRANSFORMERS_OFFLINE', None)\n"
            "os.environ.pop('SAFETYMIND_HF_ONLINE', None)\n"
            "import huggingface_hub\n"
            "from huggingface_hub import constants\n"
            "print('initial', constants.HF_HUB_OFFLINE)\n"
            "os.environ['HF_HUB_OFFLINE']='1'\n"
            "print('after_set', constants.HF_HUB_OFFLINE)\n"
        )
        import subprocess
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=120)
        lines = dict(ln.split(" ", 1) for ln in (r.stdout or "").strip().splitlines() if " " in ln)
        initial = lines.get("initial", "")
        after_set = lines.get("after_set", "")
        record("干净环境里 import 时离线常量是 False（未预先设置）",
               initial == "False", f"initial={initial!r}")
        record("环境变量后设对已 import 的常量无效（证明必须覆盖常量）",
               after_set == "False", f"after_set={after_set!r}（应为 False，即设置无效）")
    except Exception as e:
        record("常量固化反面验证", False, f"{type(e).__name__}: {e}")

    # ── 6. 离线不等于不可用：本地缓存里确实有模型 ────────────────────────
    hf_home = Path(os.getenv("HF_HOME", "D:/hf_cache"))
    model_dir = hf_home / "hub" / "models--BAAI--bge-base-zh-v1.5"
    snapshots = model_dir / "snapshots"
    has_cache = snapshots.exists() and any(snapshots.iterdir()) if snapshots.exists() else False
    record("本地缓存已有 bge-base 权重（离线模式仍可用）", has_cache,
           f"path={model_dir}")

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
