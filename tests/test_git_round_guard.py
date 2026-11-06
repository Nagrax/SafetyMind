# -*- coding: utf-8 -*-
# git 自动化脚本的静态回归测试（2026-11-06 闭环第 62 轮）。
#
# ## 为什么有这条测试
#
# 2026-11-06 的真实事故：`git-round.mjs` 的 `git add -A` 把上一个会话遗留的未提交改动
# （intent/tool_execution 的修复 + 新测试）一起卷进了"docs: 新增接手手册"这个提交，
# 提交信息与实际内容不符，事后无法从历史还原"哪次提交改了什么"。
#
# 修法：提交前**打印本次会进提交的文件清单**，并在"信息前缀与文件类型不匹配"时给出提示。
# 本测试锁定这些行为存在（脚本是 node，行为测试成本高，这里做源码级断言 + 语法检查）。
#
# 用法: python tests/test_git_round_guard.py
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "scripts" / "git-round.mjs"
RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def main():
    record("git-round.mjs 存在", SRC.exists())
    if not SRC.exists():
        print("0/1 PASSED")
        sys.exit(1)

    src = SRC.read_text(encoding="utf-8")
    record("提交前打印本次文件清单", "本次提交 ${files.length} 个文件" in src)
    record("清单超过 15 个时折叠", "还有 ${files.length - 15} 个" in src)
    record("提交信息与文件类型不匹配时提示",
           "suspicious.length && !/^feat|^fix|^test|^refactor|^perf/" in src)
    record("敏感路径守卫仍在（FORBIDDEN）", "FORBIDDEN" in src)
    record("密钥扫描仍在（secret-scan）", "secret-scan.mjs" in src)
    record("推送失败重试仍在", "第 ${attempt} 次推送失败" in src)

    # 语法检查（node --check）
    node = shutil.which("node") or "node"
    try:
        r = subprocess.run([node, "--check", str(SRC)], capture_output=True, text=True, timeout=60)
        record("node --check 通过", r.returncode == 0, (r.stderr or "")[:120])
    except Exception as ex:
        record("node --check 通过", False, str(ex)[:120])

    # --check 模式在干净仓库应退 0（不提交、不推送）
    try:
        r = subprocess.run([node, str(SRC), "--check"], cwd=str(REPO),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="ignore", timeout=300)
        record("--check 模式退出码 0（守卫通过）", r.returncode == 0,
               f"code={r.returncode} {(r.stderr or '')[:120]}")
        record("--check 输出含扫描文件数", "扫描" in (r.stdout or ""))
    except Exception as ex:
        record("--check 模式退出码 0（守卫通过）", False, str(ex)[:120])

    failed = RESULTS.count(False)
    print(f"\n{len(RESULTS) - failed}/{len(RESULTS)} PASSED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
