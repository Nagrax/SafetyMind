# -*- coding: utf-8 -*-
"""资源库批量导入脚本：CSV/JSON → MSDS 结构化库 + 资料目录。

流程借鉴自「安全百宝箱」小程序的管理后台（parseExcel 云函数 → saveMsdsBatch
每批 50 条 → dedupeMsds 去重），本地实现不依赖云函数：
    1. 解析 CSV/JSON（UTF-8 / UTF-8-SIG，Excel 另存 CSV 即可）
    2. upsert 或 fill（只补缺失，不覆盖已有）两种模式
    3. 导入后按 CAS 去重并输出统计

用法（在 SafetyMind 根目录）：
    python scripts/import_resources.py msds     path/to/chemicals.csv [--mode fill]
    python scripts/import_resources.py resource path/to/materials.csv
    python scripts/import_resources.py stats

MSDS CSV 列（表头任意顺序，section 列可写「急救措施」或数字 4）：
    name,cas,aliases,section,section_content
    也可用宽表：name,cas,1,2,3,...,16（列名为章节号）
资源 CSV 列：
    title,category,description,tags,file_or_url
"""
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mcp.resource_library import MSDS_SECTIONS, RESOURCE_CATEGORIES, ResourceLibrary  # noqa: E402


def read_table(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"文件不存在: {p}")
    if p.suffix.lower() == ".json":
        return json.loads(p.read_text(encoding="utf-8-sig"))
    with open(p, encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def rows_to_msds_items(rows: list[dict]) -> list[dict]:
    """把 CSV 行聚合成 MSDS 条目：同一 (name, cas) 的多行 section 合并；宽表按列号解析。"""
    items: dict[tuple, dict] = {}
    for row in rows:
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items() if k}
        name = row.get("name", "")
        cas = row.get("cas", "")
        key = (name, cas)
        item = items.setdefault(key, {"name": name, "cas": cas, "aliases": [],
                                      "sections": {}, "source": row.get("source", "csv导入")})
        alias = row.get("aliases", "")
        if alias and alias not in item["aliases"]:
            item["aliases"].append(alias)
        # 宽表：列名恰为 1-16 或章节名
        for col, val in row.items():
            if not val or col in ("name", "cas", "aliases", "source"):
                continue
            if col == "section" or col in MSDS_SECTIONS or col.isdigit():
                idx_col = row.get("section", col)
                item["sections"][idx_col] = val
        sc = row.get("section_content", "")
        if sc and row.get("section"):
            item["sections"][row["section"]] = sc
    return [v for v in items.values() if v["name"] or v["cas"]]


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        return
    # 锚定到仓库根，保证从任意工作目录调用都写同一份数据
    lib = ResourceLibrary(base_dir=str(ROOT / "data" / "resource_library"))
    cmd = sys.argv[1]
    if cmd == "stats":
        print(json.dumps(lib.stats(), ensure_ascii=False, indent=1))
        return
    if len(sys.argv) < 3:
        raise SystemExit("需要文件路径，如: python scripts/import_resources.py msds data.csv")
    rows = read_table(sys.argv[2])
    if cmd == "msds":
        mode = "fill"
        if "--mode" in sys.argv:
            mode = sys.argv[sys.argv.index("--mode") + 1]
        result = lib.import_msds(rows_to_msds_items(rows), mode=mode)
        result["dedupe"] = lib.dedupe()
    elif cmd == "resource":
        added = updated = failed = 0
        for row in rows:
            tags = [t for t in (row.get("tags") or "").replace("，", ",").split(",") if t]
            r = lib.add_resource({
                "title": row.get("title", ""),
                "category": row.get("category", ""),
                "description": row.get("description", ""),
                "tags": tags,
                "file_or_url": row.get("file_or_url", row.get("file_or_url ".strip(), "")),
            })
            if "error" in r:
                failed += 1
                print(f"  [跳过] {row.get('title')}: {r['error']}")
            elif r.get("updated"):
                updated += 1
            else:
                added += 1
        result = {"added": added, "updated": updated, "failed": failed,
                  "valid_categories": list(RESOURCE_CATEGORIES)}
    else:
        raise SystemExit(f"未知命令: {cmd}，可用 msds / resource / stats")
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
