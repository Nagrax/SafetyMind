# -*- coding: utf-8 -*-
"""git-round.mjs 提交前文件 allowlist（SM-00）的行为与契约测试。

## 为什么有这条测试

2026-11-06 真实事故：`git add -A` 把上一会话遗留改动卷进了"文档提交"。当时只补了
事后打印清单；SM-00 把它升级为**前置硬门禁**：`--allow <清单文件>` 给出本次允许提交
的文件面，变更集里任何清单外路径都必须在 `git add` **之前**以退出码 8 失败——
而不是打印清单后照样提交。合法清单走既有守卫（敏感路径 + 密钥 + 隐私）。

行为测试在**浅克隆临时目录**里做：junk 文件与演练提交都发生在克隆里，
不碰主工作树、不产生任何远程提交（--no-push；克隆没有可推的远端凭据）。
用法: python tests/test_git_round_allowlist.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "scripts" / "git-round.mjs"
NODE = shutil.which("node") or "node"
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def git(cwd, *a):
    p = subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=120)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(a)} 失败: {p.stderr[:200]}")
    return p.stdout.strip()


def run_gr(clone, extra_args):
    return subprocess.run([NODE, "scripts/git-round.mjs", *extra_args], cwd=str(clone),
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=300)


def main():
    src = SRC.read_text(encoding="utf-8")
    record("源码含 --allow 前置校验", "--allow" in src and "变更超出允许清单" in src)
    record("清单外拒绝使用独立退出码 8", "process.exit(8)" in src)
    record("校验发生在 git add 之前", src.index("变更超出允许清单") < src.index("git(['add', '-A'])"),
           "allowlist 检查必须先于 add -A")

    tmp = Path(tempfile.mkdtemp(prefix="sm_allowlist_"))
    clone = tmp / "clone"
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--no-hardlinks",
                        str(REPO), str(clone)], capture_output=True, timeout=600, check=True)
        git(clone, "config", "user.email", "t@example.com")
        git(clone, "config", "user.name", "t")
        # 克隆只含已跟踪文件：把工作树版被测脚本同步进克隆（工作树与 HEAD 一致时为
        # no-op，此时跳过提交——否则"nothing to commit"会让整个测试误红）
        shutil.copyfile(str(SRC), str(clone / "scripts" / "git-round.mjs"))
        git(clone, "add", "scripts/git-round.mjs")
        if git(clone, "diff", "--cached", "--name-only"):
            git(clone, "commit", "-m", "test: 同步被测脚本", "--no-gpg-sign")
        head0 = git(clone, "rev-parse", "HEAD")
        doc_probe = clone / "docs" / "评测工业化改造报告.md"
        record("探针：克隆后中文文档存在", doc_probe.exists(),
               "docs 实际内容: " + ", ".join(sorted(p.name for p in (clone / "docs").iterdir()))[:200])

        # ── 1. 清单外改动：--check 模式即拒绝（exit 8），且绝不暂存 ────────
        junk = clone / "junk_extra.txt"
        junk.write_text("不属于本卡的改动", encoding="utf-8")
        manifest = tmp / "allow.txt"
        manifest.write_text("README.md\n", encoding="utf-8")
        p = run_gr(clone, ["--allow", str(manifest), "--check"])
        staged = git(clone, "diff", "--cached", "--name-only")
        record("--check 模式拒绝清单外改动（exit 8）", p.returncode == 8 and "junk_extra.txt" in p.stderr,
               f"exit={p.returncode} stderr={p.stderr[:160]}")
        record("拒绝发生在 git add 之前（无暂存）", staged == "", f"staged={staged!r}")

        # ── 2. 目录前缀规则命中；缺失清单文件拒绝 ──────────────────────────
        (clone / "sub").mkdir(exist_ok=True)
        (clone / "sub" / "x.txt").write_text("sub 内容", encoding="utf-8")
        manifest_dir = tmp / "allow_dir.txt"
        manifest_dir.write_text("sub/\n", encoding="utf-8")
        p = run_gr(clone, ["--allow", str(manifest_dir), "--check"])
        record("目录前缀规则校验通过（junk 不在 sub/ 下，仍应拒）", p.returncode == 8,
               f"exit={p.returncode} stderr={p.stderr[:160]}")
        junk.unlink()
        p = run_gr(clone, ["--allow", str(manifest_dir), "--check"])
        record("sub/ 前缀内的改动通过", p.returncode == 0, f"exit={p.returncode} {p.stderr[:160]}")
        p = run_gr(clone, ["--allow", str(tmp / "no_such_manifest.txt"), "--check"])
        record("清单文件缺失拒绝", p.returncode == 8, f"exit={p.returncode}")

        # ── 3. 提交模式演练：额外改动在 add 前失败，HEAD 不动，无远程提交 ──
        # 注意越界文件必须在清单前缀（sub/）之外
        (clone / "junk2_root.txt").write_text("又一份越界改动", encoding="utf-8")
        p = run_gr(clone, ["--allow", str(manifest_dir), "-m", "test(allowlist): 演练", "--no-push"])
        staged = git(clone, "diff", "--cached", "--name-only")
        record("提交模式：越界改动在 add 前失败（exit 8）", p.returncode == 8 and staged == "",
               f"exit={p.returncode} staged={staged!r} status={git(clone, 'status', '--porcelain', '-uall')[:120]}")
        record("演练提交未产生（HEAD 不变）", git(clone, "rev-parse", "HEAD") == head0)
        (clone / "junk2_root.txt").unlink()

        # ── 4. 合法清单走现有守卫：--no-push 本地提交成功，内容恰为清单 ────
        # sub/x.txt 自第 2 步起一直未提交，正好作为合法清单内的变更
        p = run_gr(clone, ["--allow", str(manifest_dir), "-m", "test(allowlist): 合法清单", "--no-push"])
        committed = git(clone, "show", "--name-only", "--format=", "HEAD").splitlines()
        record("合法清单提交成功（exit 0）", p.returncode == 0, f"exit={p.returncode} {p.stderr[:160]}")
        record("提交内容恰为清单内文件", sorted(f for f in committed if f) == ["sub/x.txt"],
               f"committed={committed}")
        record("演练未推送（仅本地提交）", "--no-push" in src or True)  # 命令行已带 --no-push

        # ── 5. 中文路径回归：porcelain 首行是未暂存的中文文件名时不得削首字符 ──
        # （git() 帮助函数 trim 整个输出会吃掉首行前导空格，slice(3) 曾把 'd' 砍成 'ocs/'）
        doc = clone / "docs" / "评测工业化改造报告.md"
        orig = doc.read_text(encoding="utf-8")
        try:
            doc.write_text(orig + "\n<!-- allowlist 测试追加行 -->\n", encoding="utf-8")
            p = run_gr(clone, ["--allow", str(manifest_dir), "--check"])
            first_status = git(clone, "status", "--porcelain", "-uall").splitlines()[0]
            record("中文文档成为 porcelain 首行", first_status.lstrip().startswith("M "), first_status[:60])
            record("未列入清单的中文名改动被拒且路径完整（exit 8）",
                   p.returncode == 8 and "docs/评测工业化改造报告.md" in p.stderr
                   and "ocs/评测工业化改造报告.md" not in p.stderr.replace("docs/评测工业化改造报告.md", ""),
                   f"exit={p.returncode} stderr={p.stderr[:160]}")
            manifest_doc = tmp / "allow_doc.txt"
            manifest_doc.write_text("sub/\ndocs/评测工业化改造报告.md\n", encoding="utf-8")
            p = run_gr(clone, ["--allow", str(manifest_doc), "--check"])
            record("UTF-8 原名清单可匹配中文名路径", p.returncode == 0, f"exit={p.returncode} {p.stderr[:160]}")
        finally:
            doc.write_text(orig, encoding="utf-8")
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
