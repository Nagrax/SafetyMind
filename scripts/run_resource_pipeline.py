# -*- coding: utf-8 -*-
"""资源库工作流管线：一条命令完成 种子校验 → 收件箱导入 → 去重 → 覆盖率自检
→ Skill/工具接线自检 → 单元测试 → 报告落盘。

设计为闲时/夜间批处理入口（不调用任何 LLM API，纯本地执行），可反复运行：
    python scripts/run_resource_pipeline.py

收件箱约定：把待导入文件放到 data/resource_library/inbox/ 下，按文件名路由——
    文件名含 msds/chemical/化学品 → 按 MSDS 条目导入（宽表或长表 CSV/JSON）
    其余                         → 按资料条目导入（title,category,description,tags）
处理完成的文件移动到 inbox/done/，失败明细写入报告。
"""
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.resource_library import RESOURCE_CATEGORIES, ResourceLibrary  # noqa: E402

INBOX = ROOT / "data" / "resource_library" / "inbox"
DONE = INBOX / "done"
REPORT_JSON = ROOT / "data" / "resource_library" / "pipeline_report.json"
REPORT_MD = ROOT / "data" / "resource_library" / "pipeline_report.md"


def import_inbox(lib: ResourceLibrary) -> list[dict]:
    results = []
    if not INBOX.exists():
        return results
    DONE.mkdir(parents=True, exist_ok=True)
    for f in sorted(INBOX.iterdir()):
        if f.suffix.lower() not in (".csv", ".json") or not f.is_file():
            continue
        entry = {"file": f.name}
        try:
            rows = read_table(f)
            if re.search(r"msds|chemical|化学品", f.stem, re.I):
                from import_resources import rows_to_msds_items
                r = lib.import_msds(rows_to_msds_items(rows), mode="fill")
                r["dedupe"] = lib.dedupe()
            else:
                added = failed = 0
                for row in rows:
                    tags = [t for t in str(row.get("tags") or "").replace("，", ",").split(",") if t]
                    r = lib.add_resource({
                        "title": row.get("title", ""),
                        "category": row.get("category", ""),
                        "description": row.get("description", ""),
                        "tags": tags,
                        "file_or_url": row.get("file_or_url", ""),
                    })
                    if "error" in r:
                        failed += 1
                        entry.setdefault("errors", []).append(r["error"])
                    else:
                        added += 1
                r = {"added": added, "failed": failed}
            entry["result"] = r
            shutil.move(str(f), str(DONE / f.name))
        except Exception as ex:
            entry["result"] = {"error": str(ex)}
        results.append(entry)
    return results


def read_table(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8-sig"))
    import csv
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return [dict(row) for row in csv.DictReader(fh)]


def ledger_coverage(lib: ResourceLibrary) -> list[dict]:
    """每类台账的支撑文件覆盖率（matched/missing），指导管理员补文件。"""
    out = []
    for meta in lib.list_ledgers():
        cl = lib.checklist(meta["id"])
        s = cl.get("summary", {})
        out.append({"ledger": meta["name"], "total": s.get("total", 0),
                    "matched": s.get("matched", 0), "missing": s.get("missing", 0)})
    return out


def verify_skills() -> dict:
    """离线验证两个新 Skill 能被 SkillManager 解析并按关键词命中。"""
    from core.skill_loader import SkillManager
    sm = SkillManager(str(ROOT / "skills"))
    skills = sm.load()
    names = {s.name for s in skills}
    checks = {
        "skill_count": len(skills),
        "parse_errors": sm.errors,
        "msds_skill_loaded": any("MSDS" in n for n in names),
        "ledger_skill_loaded": any("台账" in n for n in names),
        "msds_keyword_hit": bool(sm.prompt_for("烧碱的MSDS泄漏应急处理", "compliance")),
        "ledger_keyword_hit": bool(sm.prompt_for("危化品台账需要哪些支撑文件", "coordination")),
    }
    return checks


def run_tests() -> dict:
    proc = subprocess.run([sys.executable, str(ROOT / "tests" / "test_resource_library.py")],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=300)
    tail = [l for l in proc.stdout.splitlines() if l.strip()][-1:] or [""]
    return {"exit_code": proc.returncode, "summary": tail[0],
            "ok": proc.returncode == 0}


def main() -> int:
    started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # 锚定到仓库根，保证从任意工作目录调用都写同一份数据
    lib = ResourceLibrary(base_dir=str(ROOT / "data" / "resource_library"))
    inbox_results = import_inbox(lib)
    coverage = ledger_coverage(lib)
    skill_checks = verify_skills()
    test_result = run_tests()
    stats = lib.stats()

    ok = (test_result["ok"] and skill_checks["msds_skill_loaded"]
          and skill_checks["ledger_skill_loaded"]
          and not skill_checks["parse_errors"]
          and skill_checks["msds_keyword_hit"] and skill_checks["ledger_keyword_hit"])
    report = {
        "started_at": started,
        "ok": ok,
        "stats": stats,
        "inbox_imports": inbox_results,
        "ledger_coverage": coverage,
        "skill_checks": {k: v for k, v in skill_checks.items() if k != "parse_errors"}
                        | {"parse_error_count": len(skill_checks["parse_errors"])},
        "tests": test_result,
    }
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [
        f"# 资源库管线报告（{started}）", "",
        f"- 总体状态: {'PASS' if ok else 'FAIL'}",
        f"- 库存: MSDS {stats['msds_total']} 种（{stats['msds_with_sections']} 含分章内容），"
        f"资料 {stats['resources_total']} 条",
        f"- 收件箱导入: {len(inbox_results)} 个文件",
        f"- 单元测试: {test_result['summary']} (exit={test_result['exit_code']})",
        f"- Skill 自检: MSDS={'✓' if skill_checks['msds_keyword_hit'] else '✗'}, "
        f"台账={'✓' if skill_checks['ledger_keyword_hit'] else '✗'}, "
        f"解析错误={len(skill_checks['parse_errors'])}", "",
        "## 台账支撑文件覆盖率（matched/total，missing 是待补文件）", "",
        "| 台账 | 已入库 | 应备 | 缺失 |", "| --- | --- | --- | --- |",
    ]
    for c in coverage:
        lines.append(f"| {c['ledger']} | {c['matched']} | {c['total']} | {c['missing']} |")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
