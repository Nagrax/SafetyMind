# -*- coding: utf-8 -*-
"""提交前密钥守卫的回归测试（离线，零外部依赖）。

## 为什么有这个文件

2026-10-30 GitHub 对本仓库发出 secret scanning 告警，指向
`tests/test_catalog_export.py#L215` 的微信小程序 AppID。排查时又发现**更严重**的
问题：两个 benchmarks 脚本仍把**真实模型 API key 写死在代码里**且已公开在
`origin/main`。根因是守卫本身有四个洞：

  1. **只扫「本次变更的文件」**——已入库的历史密钥根本不在扫描范围内；
  2. **密钥形态覆盖不全**——中继 key（32 位 hex + '.' + 20 位）、`sk-ws-`、
     `glpat-`、`AKIA`、`AIza`、`xox`、微信 AppID 全都没覆盖；
  3. **形态规则写死在脚本里**——加一条规则要改脚本，测试无法覆盖到；
  4. **没有回归测试**——守卫失灵不会被任何人发现。

## 本文件锁住什么

  - 规则文件 `scripts/secret-patterns.json` 存在且非空、能被编译、覆盖上述形态；
  - 每条形态都能被命中（用**运行时拼接**的样例，源码里不留密钥形状字面量）；
  - 已知非凭据的测试夹具（`sk-original-…`）**不**被误伤；
  - 守卫对「已跟踪文件里的历史密钥」也会拦（不只是本次变更）；
  - 仓库当前工作树**不含**任何密钥形态的字面量（端到端兜底）。

## 用法

    <venv python> tests/test_git_guard.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
RULES_FILE = REPO / "scripts" / "secret-patterns.json"
PRIVACY_RULES_FILE = REPO / "scripts" / "privacy-patterns.json"
GUARD = REPO / "scripts" / "git-round.mjs"

RESULTS = []
# 临时产物只落 D 盘（铁律 7）。沙箱下 scratch 可能不可写，退到仓库同级临时目录。
_SCRATCH = Path("D:/claudecode/scratch/sm-git-guard")


def _scratch_base() -> Path:
    """scratch 不可写时退回系统临时目录（只放临时仓库，不进工程目录）。"""
    try:
        _SCRATCH.mkdir(parents=True, exist_ok=True)
        probe = _SCRATCH / ".probe"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        return _SCRATCH
    except Exception:
        return Path(tempfile.gettempdir()) / "sm-git-guard"


def record(name, ok, detail=""):
    ok = bool(ok)
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


# ── 样例：运行时拼接，源码里不留密钥形状的字面量 ────────────────────────────
# 每条都是"形似真 key 但确属占位"的合成串；拼出来只为让正则命中。
SAMPLES = {
    "relay-key-hex32": "a" * 32 + "." + "b" * 20,
    "anthropic-relay-prefix": "tlJIT51" + "z" * 12,
    "anthropic-sk": "sk-" + "c" * 30,
    "dashscope-ws": "sk-ws-" + "d" * 30,
    "github-pat": "ghp_" + "e" * 36,
    "gitlab-pat": "glpat-" + "f" * 22,
    "aws-access-key": "AKIA" + "G" * 16,
    "google-api-key": "AIza" + "h" * 33,
    "slack-token": "xoxb-" + "i" * 12 + "-abcdefghij",
    "wechat-miniprogram-appid": "wx" + "0123456789abcdef",
    # 私钥头也拆开拼接：否则测试文件自己就会被扫描器拦下（自指陷阱）
    "private-key": "-----BEGIN RSA " + "PRIVATE KEY-----",
}

# 确属测试夹具、**不得**被误伤的字面量
ALLOWED_FIXTURES = [
    "sk-original-key-123456",
    "sk-new-key-987654",
]

# 仓库里确实存在、且**不是**密钥的普通字符串（含相似词形）
BENIGN_SAMPLES = [
    "risk-calibration",
    "risk-frozen",
    "wxapkg 解包报告形态",
    "D:/WeChat/WeChat Files/Applet/",
    "SECRET_KEY=change_this_in_production",
]


def load_rules():
    if not RULES_FILE.is_file():
        return None
    try:
        return json.loads(RULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def test_rules_file():
    """规则文件存在、非空、每条都能编译成 JS 正则。"""
    data = load_rules()
    record("规则文件存在且是合法 JSON", data is not None, str(RULES_FILE))
    if not data:
        return None
    pats = data.get("patterns") or []
    record("规则文件非空", len(pats) > 0, f"patterns={len(pats)}")
    ids = [p.get("id") for p in pats]
    record("每条规则有唯一 id", len(ids) == len(set(ids)) and all(ids), f"{ids}")
    bad = []
    for p in pats:
        try:
            re.compile(p["pattern"])
        except Exception as ex:
            bad.append(f"{p.get('id')}: {ex}")
    record("每条规则都是合法正则", not bad, "; ".join(bad))
    # 规则文件本身不得含密钥形状字面量
    text = RULES_FILE.read_text(encoding="utf-8")
    selfhit = []
    for p in pats:
        try:
            rx = re.compile(p["pattern"])
        except Exception:
            continue
        for line in text.split("\n"):
            if rx.search(line) and "pattern" not in line:
                selfhit.append(f"{p.get('id')}: {line.strip()[:60]}")
    record("规则文件自身不含密钥形状字面量", not selfhit, "; ".join(selfhit))
    return data


def test_patterns_hit(data):
    """每种密钥形态都要能被对应规则命中。"""
    if not data:
        return
    by_id = {}
    for p in data["patterns"]:
        try:
            by_id[p["id"]] = re.compile(p["pattern"])
        except Exception:
            continue
    miss = []
    for pid, sample in SAMPLES.items():
        rx = by_id.get(pid)
        if rx is None:
            miss.append(f"{pid}: 无规则")
        elif not rx.search(sample):
            miss.append(f"{pid}: 规则未命中合成样例")
    record("全部密钥形态都能被规则命中", not miss, "; ".join(miss))

    over = []
    for pid, rx in by_id.items():
        sample = SAMPLES.get(pid)
        if sample is None:
            continue
        for other_id, other_rx in by_id.items():
            if other_id == pid:
                continue
            # 形态包含关系不算异常（sk-ws- 天然也被 sk- 命中），只报**互不包含**的交叉
            if other_rx.search(sample) and not rx.search(other_rx.search(sample).group(0)):
                over.append(f"{other_id} 也命中 {pid} 的样例")
    record("规则之间无异常交叉命中", not over, "; ".join(sorted(set(over))))


def test_no_false_positives(data):
    """已知夹具不得被误伤；普通字符串不得被误伤。"""
    if not data:
        return
    rxs = []
    for p in data["patterns"]:
        try:
            rxs.append((p["id"], re.compile(p["pattern"])))
        except Exception:
            continue

    def hits(text):
        return [pid for pid, rx in rxs if rx.search(text)]

    fp = []
    for fx in ALLOWED_FIXTURES:
        h = hits(fx)
        # sk-original-… 由负向先行断言豁免；其余夹具不应命中任何规则
        h = [x for x in h if not (x == "anthropic-sk" and fx.startswith("sk-original-"))]
        if h:
            fp.append(f"{fx} → {h}")
    record("已知测试夹具不被误伤", not fp, "; ".join(fp))

    fp2 = []
    for b in BENIGN_SAMPLES:
        h = hits(b)
        if h:
            fp2.append(f"{b} → {h}")
    record("普通字符串不被误伤", not fp2, "; ".join(fp2))


def test_guard_scans_all_tracked_files():
    """守卫必须扫**全部已跟踪文件**，而不只是本次变更。"""
    src = GUARD.read_text(encoding="utf-8") if GUARD.is_file() else ""
    record("守卫脚本存在", bool(src), str(GUARD))
    if not src:
        return
    record("守卫读取全部已跟踪文件", "ls-files'" in src and "tracked" in src,
           "未发现对全部已跟踪文件的扫描")
    record("守卫从规则文件加载形态", "secret-patterns.json" in src,
           "密钥形态未抽到单一事实源")
    record("守卫不再内嵌密钥样例字面量", "tlJIT51" not in src,
           "守卫内仍内嵌形态字面量")
    record("守卫命中时报样例用打码", "masked" in src or "formatHits" in src,
           "样例未打码，可能把密钥写进 CI 日志")
    # 扫描核心必须与 git 操作解耦，否则无法被测试覆盖
    scan = REPO / "scripts" / "secret-scan.mjs"
    record("扫描核心已抽成无 git 依赖的独立模块", scan.is_file(), str(scan))
    if scan.is_file():
        ssrc = scan.read_text(encoding="utf-8")
        record("扫描核心不调用 git", "execFileSync" not in ssrc and "spawnSync" not in ssrc,
               "扫描核心仍依赖子进程")


def test_worktree_clean_of_secrets(data):
    """端到端兜底：仓库当前工作树不含任何密钥形态字面量。"""
    if not data:
        return
    rxs = []
    for p in data["patterns"]:
        try:
            rxs.append((p["id"], re.compile(p["pattern"])))
        except Exception:
            continue
    skip_files = {"scripts/git-round.mjs", "scripts/secret-patterns.json", ".env"}
    hits = []
    for f in REPO.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(REPO).as_posix()
        if rel in skip_files:
            continue
        if any(part in rel for part in (".git/", "__pycache__/", "node_modules/",
                                        ".venv/", "data/", "logs/", "frontend/dist/")):
            continue
        if rel.startswith("tests/test_git_guard.py"):
            continue      # 本文件含「合成的样例形状」，由上面的规则用例覆盖
        try:
            if f.stat().st_size > 4 * 1024 * 1024:
                continue
            raw = f.read_bytes()
        except Exception:
            continue
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for pid, rx in rxs:
            for m in rx.finditer(text):
                matched = m.group(0)
                if matched in ALLOWED_FIXTURES:
                    continue
                if matched.startswith("sk-original-"):
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                hits.append(f"{rel}:{line_no} [{pid}]")
                break
    record("工作树不含密钥形态字面量", not hits, "; ".join(hits[:10]))


def _run_node(args, cwd):
    """跑 node 子进程：强制 UTF-8 解码（Windows 默认 GBK 会读崩中文输出）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["NODE_OPTIONS"] = env.get("NODE_OPTIONS", "")
    env["LANG"] = "C.UTF-8"
    return subprocess.run(args, capture_output=True, text=True, cwd=str(cwd),
                          env=env, encoding="utf-8", errors="replace")


def test_guard_runs_and_rejects():
    """端到端：在临时目录里种一个假密钥，扫描器必须命中并中止（退出码 3）。

    用 `scripts/secret-scan.mjs`（无 git 依赖）而不是 git-round.mjs：
    受限环境里子进程捕获管道被禁（EPERM），git 路径无法被测试覆盖，
    所以扫描核心被拆成独立模块——**守卫能不能真拦住，必须是可测的**。
    """
    if not shutil.which("node"):
        record("跳过守卫端到端用例（无 node）", True)
        return
    base = _scratch_base()
    base.mkdir(parents=True, exist_ok=True)
    # 自己建目录树：tempfile.mkdtemp 建出来的目录在沙箱下反而不可写
    tmp = base / ("guardrepo_" + next(tempfile._get_candidate_names()))
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    try:
        # 复制扫描器与规则文件
        for name in ("secret-scan.mjs", "secret-patterns.json"):
            src_text = (REPO / "scripts" / name).read_text(encoding="utf-8")
            (tmp / "scripts" / name).write_text(src_text, encoding="utf-8")
        # 种入一个"历史"密钥文件
        leaky = tmp / "leaky.py"
        leaky.write_text('KEY = "' + "a" * 32 + "." + "b" * 20 + '"\n', encoding="utf-8")
        # 另放一个干净文件，确认不是"见谁都报"
        (tmp / "clean.py").write_text("x = 1\n", encoding="utf-8")

        proc = _run_node(
            ["node", str(tmp / "scripts" / "secret-scan.mjs"),
             "--repo-root", str(tmp), "leaky.py", "clean.py"], tmp)
        out = proc.stdout + proc.stderr
        record("扫描器拦下密钥文件", proc.returncode == 3,
               f"exit={proc.returncode} out={proc.stdout[:200]} err={proc.stderr[:300]}")
        record("扫描器报出规则 id 便于定位", "relay-key-hex32" in out, out[:300])
        record("扫描器输出不含密钥明文", ("a" * 32) not in out, "扫描日志里出现了密钥明文")
        record("扫描器对干净文件不误报", "clean.py" not in out, out[:300])

        # 干净目录应当放行（退出码 0）
        (tmp / "clean.py").unlink()
        leaky.unlink()
        (tmp / "ok.py").write_text("x = 1\n", encoding="utf-8")
        proc2 = _run_node(
            ["node", str(tmp / "scripts" / "secret-scan.mjs"),
             "--repo-root", str(tmp), "ok.py"], tmp)
        record("干净目录放行（退出码 0）", proc2.returncode == 0,
               f"exit={proc2.returncode} {proc2.stdout[:200]} {proc2.stderr[:200]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── 隐私维度（2026-10-30 用户要求「建立长期记忆，防止隐私信息泄露」）────────
def load_privacy_rules():
    if not PRIVACY_RULES_FILE.is_file():
        return None
    try:
        return json.loads(PRIVACY_RULES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None


def _py_flags(rule):
    """把规则里的 JS 风格 flags（'i'/'g'/'m'）转成 Python re 的标志位。"""
    f = 0
    for ch in (rule.get("flags") or ""):
        if ch == "i":
            f |= re.IGNORECASE
        elif ch == "m":
            f |= re.MULTILINE
    return f


def test_privacy_rules():
    """隐私规则文件存在、可编译、分级正确。"""
    data = load_privacy_rules()
    record("隐私规则文件存在且是合法 JSON", data is not None, str(PRIVACY_RULES_FILE))
    if not data:
        return None
    pats = data.get("patterns") or []
    record("隐私规则非空", len(pats) > 0, f"patterns={len(pats)}")
    bad = []
    sev_bad = []
    for p in pats:
        try:
            re.compile(p["pattern"], _py_flags(p))
        except Exception as ex:
            bad.append(f"{p.get('id')}: {ex}")
        if p.get("severity") not in ("block", "warn"):
            sev_bad.append(f"{p.get('id')}: severity={p.get('severity')}")
    record("每条隐私规则都是合法正则", not bad, "; ".join(bad))
    record("每条隐私规则都有 block/warn 分级", not sev_bad, "; ".join(sev_bad))
    # 关键：真实身份信息必须是 block 级
    ids = {p.get("id"): p.get("severity") for p in pats}
    must_block = ("windows-user-path", "personal-email", "cn-id-card", "cn-bank-card", "cn-mobile")
    miss = [i for i in must_block if ids.get(i) != "block"]
    record("真实身份信息类规则均为 block 级", not miss, f"未 block: {miss}")
    return data


def test_privacy_patterns_hit(data):
    """隐私形态能被对应规则命中（合成样例，源码不留字面量）。"""
    if not data:
        return
    by_id = {}
    for p in data["patterns"]:
        try:
            by_id[p["id"]] = re.compile(p["pattern"], _py_flags(p))
        except Exception:
            continue
    samples = {
        "windows-user-path": "C:" + chr(92) + "Users" + chr(92) + "someone",
        "personal-email": "someone" + "@" + "163" + ".com",
        "cn-id-card": "11010119900101123X",
        "cn-bank-card": "622202123456789" + "0",
        "cn-mobile": "138" + "0000" + "0000",
        "local-abs-path": "D:" + chr(92) + "claudecode" + chr(92) + "SafetyMind",
    }
    miss = []
    for pid, sample in samples.items():
        rx = by_id.get(pid)
        if rx is None:
            miss.append(f"{pid}: 无规则")
        elif not rx.search(sample):
            miss.append(f"{pid}: 规则未命中合成样例")
    record("隐私形态都能被规则命中", not miss, "; ".join(miss))


def test_privacy_no_false_positives(data):
    """隐私规则不得把普通内容误判成 block 级。"""
    if not data:
        return
    rxs = []
    for p in data["patterns"]:
        try:
            rxs.append((p.get("id"), p.get("severity"),
                        re.compile(p["pattern"], _py_flags(p))))
        except Exception:
            continue
    benign = [
        ("浮点尾数", "0.3838579669298401"),
        ("普通长数字", "1234567890123"),
        ("短数字", "20261030"),
        ("占位邮箱", "your_anthropic_api_key_here"),
        ("占位密码", "SECRET_KEY=change_this_in_production"),
        ("占位密码2", "REDIS_PASSWORD="),
        ("相对路径", "scripts/git-round.mjs"),
        ("英文句子", "the quick brown fox jumps over the lazy dog"),
    ]
    fp = []
    for label, text in benign:
        for pid, sev, rx in rxs:
            if sev != "block":
                continue
            if rx.search(text):
                fp.append(f"{label} → {pid}")
    record("隐私规则不误伤普通内容", not fp, "; ".join(fp))


def test_privacy_scanner_runs(data):
    """端到端：隐私扫描器可运行，且当前工作树无 block 级命中。"""
    if not shutil.which("node"):
        record("跳过隐私扫描端到端用例（无 node）", True)
        return
    base = _scratch_base()
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / ("privacyrepo_" + next(tempfile._get_candidate_names()))
    (tmp / "scripts").mkdir(parents=True, exist_ok=True)
    try:
        for name in ("secret-scan.mjs", "secret-patterns.json", "privacy-patterns.json"):
            (tmp / "scripts" / name).write_text(
                (REPO / "scripts" / name).read_text(encoding="utf-8"), encoding="utf-8")
        # 种入一条真实手机号形态
        (tmp / "leak.py").write_text(
            "PHONE = " + chr(34) + "138" + "0000" + "0000" + chr(34) + chr(10), encoding="utf-8")
        proc = _run_node(
            ["node", str(tmp / "scripts" / "secret-scan.mjs"), "--privacy",
             "--repo-root", str(tmp), "leak.py"], tmp)
        out = proc.stdout + proc.stderr
        record("隐私扫描器拦下 block 级形态", proc.returncode == 7,
               f"exit={proc.returncode} {out[:250]}")
        record("隐私扫描器报出规则 id", "cn-mobile" in out, out[:250])
        record("隐私扫描器输出不含手机号明文", ("138" + "0000" + "0000") not in out,
               "扫描日志里出现了手机号明文")
        # 干净文件应放行
        (tmp / "leak.py").unlink()
        (tmp / "ok.py").write_text("x = 1\n", encoding="utf-8")
        proc2 = _run_node(
            ["node", str(tmp / "scripts" / "secret-scan.mjs"), "--privacy",
             "--repo-root", str(tmp), "ok.py"], tmp)
        record("隐私扫描器对干净文件放行", proc2.returncode == 0,
               f"exit={proc2.returncode} {proc2.stdout[:150]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_worktree_no_block_privacy(data):
    """工作树不含 block 级隐私形态（真实身份信息）。"""
    if not data:
        return
    rxs = []
    for p in data["patterns"]:
        if p.get("severity") != "block":
            continue
        try:
            rxs.append((p.get("id"), re.compile(p["pattern"], _py_flags(p)),
                        set(p.get("allow_paths") or [])))
        except Exception:
            continue
    hits = []
    for f in REPO.rglob("*"):
        if not f.is_file():
            continue
        rel = f.relative_to(REPO).as_posix()
        if rel in ("scripts/git-round.mjs", "scripts/secret-scan.mjs",
                   "scripts/secret-patterns.json", "scripts/privacy-patterns.json",
                   "tests/test_git_guard.py", ".env"):
            continue
        if any(part in rel for part in (".git/", "__pycache__/", "node_modules/",
                                        ".venv/", "data/", "logs/", "frontend/dist/",
                                        "benchmarks/results/")):
            continue
        try:
            if f.stat().st_size > 4 * 1024 * 1024:
                continue
            raw = f.read_bytes()
        except Exception:
            continue
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for pid, rx, allow_paths in rxs:
            if rel in allow_paths:
                continue
            if rx.search(text):
                hits.append(f"{rel} [{pid}]")
                break
    record("工作树不含 block 级隐私形态", not hits, "; ".join(hits[:10]))


if __name__ == "__main__":
    _data = load_rules()
    _priv = load_privacy_rules()
    for fn in (test_rules_file, test_patterns_hit, test_no_false_positives,
               test_guard_scans_all_tracked_files, test_worktree_clean_of_secrets,
               test_guard_runs_and_rejects,
               test_privacy_rules, test_privacy_patterns_hit, test_privacy_no_false_positives,
               test_privacy_scanner_runs, test_worktree_no_block_privacy):
        try:
            if fn.__code__.co_argcount:
                fn(_priv if fn.__name__.startswith("test_privacy")
                   or fn.__name__ == "test_worktree_no_block_privacy" else _data)
            else:
                fn()
        except Exception as ex:
            record(fn.__name__, False, f"异常: {type(ex).__name__}: {ex}")
    passed, total = sum(RESULTS), len(RESULTS)
    print(f"\n{passed}/{total} passed")
    sys.exit(0 if passed == total else 1)
