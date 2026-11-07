# -*- coding: utf-8 -*-
# 应用数据 store 落盘隔离回归（SM-01）。
#
# ## 缺口（证据 scratch/sm-loop/handoff/SM-00/run-001/isolation_churn.json）
#
# heavy/core 测试在门禁 runner 下仍把应用数据写进仓库默认 data/：
#   data/eval/last_gate.json / data/memory_snapshot.json /
#   data/preference_cards.db-shm / -wal / data/skill_candidates.jsonl
# 根因：4 个 store 的产品默认路径（相对 data/）被测试触发——
#   memory/preference_cards.py  (PREFERENCE_DB_PATH,           默认 data/preference_cards.db)
#   memory/conversation_memory.py (SAFETYMIND_MEMORY_SNAPSHOT, 默认 data/memory_snapshot.json)
#   core/skill_evolution.py     (SAFETYMIND_SKILL_EVO_DIR,     默认 data)
#   api/main.py                 (EVAL_GATE_RECORD_PATH,        默认 <repo>/data/eval/last_gate.json)
#
# ## 本测试固化的契约
#
#   1) 设置 env 旋钮 → store 落盘到指定位置（写盘走 env 指向，不碰仓库 data/）；
#   2) 未设置 env 旋钮 → 保持现行为（相对 cwd 的 data/；产品在仓库根运行即仓库 data/）。
#      "未设置"分支用子进程 + cwd=临时目录 验证默认解析，绝不写仓库 data/；
#   3) 仓库 data/ 既有业务文件全程零改动（哈希前后一致，且无新增 -shm/-wal）。
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")
os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")

REPO = Path(__file__).resolve().parents[1]
TMP = Path(tempfile.mkdtemp(prefix="store_iso_env_"))
KNOBS = TMP / "knobs"
UNSET = TMP / "unset"                      # "未设置"分支的 cwd：默认 data/ 落在这里
KNOB_NAMES = ("PREFERENCE_DB_PATH", "SAFETYMIND_MEMORY_SNAPSHOT",
              "SAFETYMIND_SKILL_EVO_DIR", "EVAL_GATE_RECORD_PATH")
WATCHED = ("preference_cards.db", "memory_snapshot.json",
           "skill_candidates.jsonl", "eval/last_gate.json")

# 旋钮指向隔离目录（模拟 runner 的 child_env：见 scratch/sm-loop/baseline_run.py）
os.environ["PREFERENCE_DB_PATH"] = str(KNOBS / "preference_cards.db")
os.environ["SAFETYMIND_MEMORY_SNAPSHOT"] = str(KNOBS / "memory_snapshot.json")
os.environ["SAFETYMIND_SKILL_EVO_DIR"] = str(KNOBS / "skill_evo")
os.environ["EVAL_GATE_RECORD_PATH"] = str(KNOBS / "eval" / "last_gate.json")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def _digest(p: Path):
    if not p.exists():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "DIR"


def repo_data_state():
    return {name: _digest(REPO / "data" / name) for name in WATCHED} | {
        "pref_shm": (REPO / "data" / "preference_cards.db-shm").exists(),
        "pref_wal": (REPO / "data" / "preference_cards.db-wal").exists(),
    }


def run_snippet(code: str, cwd: Path, timeout: int = 240):
    """子进程跑一段探针代码（cwd=临时目录），取末尾 ##JSON## 行。env 里剥掉全部旋钮。"""
    env = {k: v for k, v in os.environ.items() if k not in KNOB_NAMES}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    p = subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    payload = {}
    for line in reversed((p.stdout or "").splitlines()):
        if line.startswith("##JSON##"):
            payload = json.loads(line[len("##JSON##"):])
            break
    return p, payload


def main():
    before = repo_data_state()

    # ── 0. 源码守护：4 个旋钮仍在产品读取点上（防止被删后测试静默失去意义）──
    guards = [
        ("PREFERENCE_DB_PATH", "memory/preference_cards.py"),
        ("SAFETYMIND_MEMORY_SNAPSHOT", "memory/conversation_memory.py"),
        ("SAFETYMIND_SKILL_EVO_DIR", "core/skill_evolution.py"),
        ("EVAL_GATE_RECORD_PATH", "api/main.py"),
    ]
    for knob, rel in guards:
        src = (REPO / rel).read_text(encoding="utf-8")
        record(f"旋钮契约存在于产品代码: {knob}", knob in src, rel)

    # ── 1. PREFERENCE_DB_PATH 设置 → 偏好卡片 SQLite 落到指定位置 ────────
    from memory.preference_cards import PreferenceCardManager
    mgr = PreferenceCardManager()          # 不传 db_path → 必须走 env
    record("偏好卡片走 env 指向", mgr._path == str(KNOBS / "preference_cards.db"), mgr._path)
    n = mgr.upsert_cards("iso_user", [{
        "card_type": "style", "card_key": "iso_env_probe", "title": "t",
        "content": "c", "keywords": ["k"]}], source="regression")
    cards = mgr.list_cards("iso_user")
    record("偏好卡片写入并读回（env 指向库）",
           n == 1 and any(c["card_key"] == "iso_env_probe" for c in cards),
           f"n={n} cards={len(cards)}")
    mgr._con.close()

    # ── 2. SAFETYMIND_MEMORY_SNAPSHOT 设置 → 记忆降级快照落到指定位置 ────
    from memory.conversation_memory import MemoryManager
    mm = MemoryManager(redis_url="redis://127.0.0.1:9/0",   # 死端口 → 触发降级
                       chroma_host="127.0.0.1", chroma_port=9,
                       chroma_path=str(KNOBS / "chroma_mm"))
    asyncio.run(mm._redis_op("setex", "iso:env:probe", 60, "v"))
    snap_file = KNOBS / "memory_snapshot.json"
    snap = json.loads(snap_file.read_text(encoding="utf-8")) if snap_file.exists() else {}
    record("记忆快照写到 env 指向（降级触发后落盘）",
           snap_file.exists() and snap.get("kv", {}).get("iso:env:probe") == "v",
           f"exists={snap_file.exists()}")

    # ── 3. SAFETYMIND_SKILL_EVO_DIR 设置 → 候选落盘到指定位置 ────────────
    from core.skill_evolution import SkillEvolution
    skills = KNOBS / "skills"
    (skills / "sk1").mkdir(parents=True, exist_ok=True)
    evo = SkillEvolution(skills_root=str(skills))
    cand = evo.append_candidate({"title": "隔离探针", "when_to_use": "w",
                                 "instruction": "i", "keywords": ["k"], "action": "add"})
    cand_file = KNOBS / "skill_evo" / "skill_candidates.jsonl"
    rows = [json.loads(l) for l in cand_file.read_text(encoding="utf-8").splitlines()] \
        if cand_file.exists() else []
    record("技能候选写到 env 指向目录",
           cand_file.exists() and any(r.get("id") == cand["id"] for r in rows),
           f"exists={cand_file.exists()} rows={len(rows)}")

    # ── 4. EVAL_GATE_RECORD_PATH 设置 → 门禁判决落到指定位置 ─────────────
    from evaluation.release_gate import evaluate_report_gate
    gate_path = KNOBS / "eval" / "last_gate.json"
    rec = evaluate_report_gate({"metrics": {}, "red_failures": [], "red_evidence": []},
                               path=str(gate_path))
    gate_lines = gate_path.read_text(encoding="utf-8").splitlines() if gate_path.exists() else []
    record("门禁判决写到 env 指向（JSONL 可解析）",
           bool(gate_lines) and "blocked" in json.loads(gate_lines[-1]),
           f"exists={gate_path.exists()}")
    from api import main as api_main
    record("api 门禁记录路径解析走 env",
           str(api_main._eval_gate_record_path()) == str(gate_path),
           str(api_main._eval_gate_record_path()))

    # ── 5. 未设置 → 现行为（子进程 + cwd=临时目录，默认解析进临时目录）────
    (UNSET / "skills").mkdir(parents=True, exist_ok=True)

    p, out = run_snippet(f"""
import sys, json
sys.path.insert(0, r"{REPO}")
from pathlib import Path
from memory.preference_cards import PreferenceCardManager
mgr = PreferenceCardManager()
print("##JSON##" + json.dumps({{"path": str(Path(mgr._path).resolve()),
                               "exists": Path(mgr._path).exists()}}))
mgr._con.close()
""", cwd=UNSET)
    default_pref = UNSET / "data" / "preference_cards.db"
    record("未设置 PREFERENCE_DB_PATH → 默认相对 data/（现行为保持）",
           p.returncode == 0 and Path(out.get("path", "")) == default_pref and out.get("exists"),
           f"exit={p.returncode} out={out} err={p.stderr[-200:]}")

    p, out = run_snippet(f"""
import sys, json
sys.path.insert(0, r"{REPO}")
from pathlib import Path
from core.skill_evolution import SkillEvolution
evo = SkillEvolution(skills_root="skills")
evo.append_candidate({{"title": "默认路径探针", "action": "add"}})
f = Path("data") / "skill_candidates.jsonl"
print("##JSON##" + json.dumps({{"exists": f.exists(), "n": len(f.read_text(encoding="utf-8").splitlines())}}))
""", cwd=UNSET)
    record("未设置 SAFETYMIND_SKILL_EVO_DIR → 默认相对 data/（现行为保持）",
           p.returncode == 0 and out.get("exists") is True and out.get("n", 0) >= 1,
           f"exit={p.returncode} out={out} err={p.stderr[-200:]}")

    p, out = run_snippet(f"""
import sys, json, asyncio
sys.path.insert(0, r"{REPO}")
from pathlib import Path
from memory.conversation_memory import MemoryManager
mm = MemoryManager(redis_url="redis://127.0.0.1:9/0", chroma_host="127.0.0.1",
                   chroma_port=9, chroma_path="unset_chroma")
asyncio.run(mm._redis_op("setex", "k", 60, "v"))
f = Path("data") / "memory_snapshot.json"
kv = json.loads(f.read_text(encoding="utf-8")).get("kv", {{}}) if f.exists() else {{}}
print("##JSON##" + json.dumps({{"exists": f.exists(), "kv": kv.get("k")}}))
""", cwd=UNSET, timeout=300)
    record("未设置 SAFETYMIND_MEMORY_SNAPSHOT → 默认相对 data/（现行为保持）",
           p.returncode == 0 and out.get("exists") is True and out.get("kv") == "v",
           f"exit={p.returncode} out={out} err={p.stderr[-200:]}")

    p, out = run_snippet(f"""
import sys, json
sys.path.insert(0, r"{REPO}")
from api import main as api_main
print("##JSON##" + json.dumps({{"path": str(api_main._eval_gate_record_path())}}))
""", cwd=UNSET, timeout=300)
    record("未设置 EVAL_GATE_RECORD_PATH → 默认仓库 data/eval/last_gate.json（现行为保持）",
           p.returncode == 0 and Path(out.get("path", "")) == REPO / "data" / "eval" / "last_gate.json",
           f"exit={p.returncode} out={out} err={p.stderr[-200:]}")

    # ── 6. runner 门禁接线（本工作区布局下）：child_env 与守卫都带 4 个旋钮 ─
    runner_py = Path(__file__).resolve().parents[2] / "scratch" / "sm-loop" / "baseline_run.py"
    if runner_py.exists():
        rsrc = runner_py.read_text(encoding="utf-8")
        record("runner child_env 注入 4 个 store 旋钮",
               all(f'"{k}"' in rsrc for k in KNOB_NAMES),
               "scratch/sm-loop/baseline_run.py")
    else:
        record("runner 接线检查（非本工作区布局，跳过）", True, str(runner_py))

    # ── 7. 全程零污染：仓库 data/ 哈希不变、无新增 -shm/-wal ─────────────
    after = repo_data_state()
    unchanged = {k: v for k, v in after.items() if k in before} == before
    record("仓库 data/ 既有文件零改动", unchanged,
           f"before={before} after={after}")
    record("无新增 preference WAL 边车文件",
           not after["pref_shm"] and not after["pref_wal"],
           f"shm={after['pref_shm']} wal={after['pref_wal']}")

    shutil.rmtree(str(TMP), ignore_errors=True)
    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
