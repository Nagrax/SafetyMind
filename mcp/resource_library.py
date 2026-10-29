# -*- coding: utf-8 -*-
"""结构化资源库：MSDS 化学品查询 + 安全资料目录。

设计来源：对「安全工程师的百宝箱」小程序的逆向分析（见
D:/claudecode/miniprogram-re/RE_REPORT.md）。该小程序用云数据库存
msds_chemicals（16 段标准 MSDS）与 files（documents/videos/standards 三类
资料），客户端全量缓存 + 增量补传。这里取其功能设计（16 段结构化 schema、
CAS 去重、分批导入、热门统计），数据落地为本地 JSON 文件——与桌面端
"无外部依赖时 JSON 快照持久化"的降级哲学一致，不引入新存储依赖。

与知识库（ChromaDB 向量检索）的分工：MSDS 是高结构化、按段引用的数据，
走精确字段匹配；资料条目是标题+描述的轻量目录，关键词匹配即可；两者
都不需要嵌入。整篇 MSDS 若需参与语义召回，可另行 add_documents 进 RAG。
"""
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

# GB/T 16483 规定的 16 段标准结构（公有章节顺序，非任何厂商内容）
MSDS_SECTIONS = [
    "化学品及企业标识",
    "危险性概述",
    "成分/组成信息",
    "急救措施",
    "消防措施",
    "泄漏应急处理",
    "操作处置与储存",
    "接触控制/个体防护",
    "理化特性",
    "稳定性和反应性",
    "毒理学信息",
    "生态学信息",
    "废弃处置",
    "运输信息",
    "法规信息",
    "其他信息",
]

# 资料分类（与原小程序首页四分类对齐；msds 不入资源目录，走化学品库）
RESOURCE_CATEGORIES = {
    "documents": "安全资料（制度台账、培训课件、检查表和模板）",
    "videos": "案例学习（公开合法的事故案例，便于学习指导）",
    "standards": "安全规范（法律法规、国家标准、行业规范）",
}

_HOT_MAX = 20          # 热门榜单保留条数
_IMPORT_BATCH = 50     # 批量导入分批大小（与原小程序 saveMsdsBatch 一致）


class ResourceLibrary:
    """MSDS 结构化库 + 资料目录，JSON 文件持久化，线程安全。

    目录布局（可用 base_dir 覆盖，测试必须用独立目录，见仓库规则 9）：
        base_dir/msds.json       化学品主数据 {cas: entry}
        base_dir/resources.json  资料条目列表
        base_dir/hot.json        查询热度 {cas: count}
    """

    def __init__(self, base_dir: Optional[str] = None):
        if base_dir is None:
            base_dir = os.path.join("data", "resource_library")
        self.base_dir = base_dir
        self._lock = threading.RLock()
        self._msds: Dict[str, Dict[str, Any]] = {}
        self._resources: List[Dict[str, Any]] = []
        self._hot: Dict[str, int] = {}
        self._ledgers: List[Dict[str, Any]] = []
        self._load()

    # ── 持久化 ────────────────────────────────────────────────────────────

    def _path(self, name: str) -> str:
        return os.path.join(self.base_dir, name).replace("\\", "/")

    def _load(self) -> None:
        os.makedirs(self.base_dir, exist_ok=True)
        self._msds = self._read_json("msds.json", {})
        self._resources = self._read_json("resources.json", [])
        self._hot = self._read_json("hot.json", {})
        # 首次启动时播种内置种子数据（不覆盖已有文件）
        if not self._msds:
            seed = self._read_json("msds_seed.json", None)
            if seed is None:
                seed_path = os.path.join(os.path.dirname(__file__), "msds_seed.json")
                if os.path.exists(seed_path):
                    with open(seed_path, encoding="utf-8") as f:
                        seed = json.load(f)
            if seed:
                for item in seed:
                    self._msds[self._norm_cas(item.get("cas", ""))] = item
                self._save_json("msds.json", self._msds)
        # 台账→支撑文件映射：base_dir 可覆盖，缺省用包内种子
        self._ledgers = self._read_json("ledger_bundles.json", None)
        if self._ledgers is None:
            ledger_path = os.path.join(os.path.dirname(__file__), "ledger_bundles.json")
            if os.path.exists(ledger_path):
                with open(ledger_path, encoding="utf-8") as f:
                    self._ledgers = json.load(f)
        if isinstance(self._ledgers, dict):
            self._ledgers = self._ledgers.get("bundles", [])
        # 资源目录种子：首次初始化时播种公开标准/模板目录元数据（不覆盖已有）
        if not self._resources:
            res_seed = self._read_json("resources_seed.json", None)
            if res_seed is None:
                res_path = os.path.join(os.path.dirname(__file__), "resources_seed.json")
                if os.path.exists(res_path):
                    with open(res_path, encoding="utf-8") as f:
                        res_seed = json.load(f).get("items", [])
            if res_seed:
                self._resources = list(res_seed)
                self._save_json("resources.json", self._resources)

    def _read_json(self, name: str, default: Any) -> Any:
        p = self._path(name)
        if not os.path.exists(p):
            return default
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            # 损坏文件归档重建，不阻塞启动（与桌面端向量库降级策略一致）
            try:
                os.replace(p, p + ".corrupt")
            except OSError:
                pass
            return default

    def _save_json(self, name: str, data: Any) -> None:
        p = self._path(name)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    # ── MSDS 查询 ─────────────────────────────────────────────────────────

    @staticmethod
    def _norm_cas(cas: str) -> str:
        return re.sub(r"\s+", "", str(cas or ""))

    def lookup(self, name: str = "", cas: str = "",
               section: Optional[str] = None) -> Dict[str, Any]:
        """按名称（含别名前缀匹配）或 CAS 精确查化学品。

        section 可传 1-16 的序号或章节名，只返回该段；命中后记录热度。
        """
        name = (name or "").strip()
        cas = self._norm_cas(cas)
        if not name and not cas:
            return {"error": "需要提供 name 或 cas 之一"}
        with self._lock:
            entry = None
            if cas and cas in self._msds:
                entry = self._msds[cas]
            elif name:
                nl = name.lower()
                for item in self._msds.values():
                    aliases = [item.get("name", "")] + list(item.get("aliases", []))
                    if any(a.lower().startswith(nl) or nl == a.lower() for a in aliases if a):
                        entry = item
                        break
            if entry is None:
                return {"found": False, "name": name, "cas": cas,
                        "hint": "库中暂无该化学品，可联系管理员批量导入，或改用知识库检索"}
            e = dict(entry)
            e["found"] = True
            e["sections_available"] = sorted(
                int(k) for k in e.get("sections", {}) if str(k).isdigit())
            if section is not None:
                sec = self._resolve_section(section)
                if sec is None:
                    return {"error": f"无效章节: {section}，可用 1-16 或章节名"}
                e["section"] = sec
                e["content"] = e.get("sections", {}).get(str(sec), "")
                e["section_title"] = MSDS_SECTIONS[sec - 1]
            self._bump_hot(e.get("cas", ""))
            return e

    @staticmethod
    def _resolve_section(section: Any) -> Optional[int]:
        try:
            n = int(section)
            return n if 1 <= n <= 16 else None
        except (TypeError, ValueError):
            s = str(section or "").strip()
            for i, title in enumerate(MSDS_SECTIONS, 1):
                if s and (s in title or title.startswith(s)):
                    return i
        return None

    def _bump_hot(self, cas: str) -> None:
        if not cas:
            return
        self._hot[cas] = self._hot.get(cas, 0) + 1
        # 只在整百时落盘，避免高频查询写盘
        if self._hot[cas] % 10 == 0:
            self._save_json("hot.json", self._hot)

    def hot(self, top_k: int = 10) -> List[Dict[str, Any]]:
        """热门化学品（借鉴原小程序 msds_hot：热度来自查询记录）。"""
        with self._lock:
            ranked = sorted(self._hot.items(), key=lambda kv: -kv[1])[:top_k]
            return [{"cas": cas, "count": count,
                     "name": self._msds.get(cas, {}).get("name", cas)}
                    for cas, count in ranked]

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            filled = sum(1 for e in self._msds.values() if e.get("sections"))
            return {
                "msds_total": len(self._msds),
                "msds_with_sections": filled,
                "resources_total": len(self._resources),
                "categories": {k: sum(1 for r in self._resources
                                      if r.get("category") == k)
                               for k in RESOURCE_CATEGORIES},
            }

    # ── 批量导入（借鉴 saveMsdsBatch + dedupeMsds：CAS 去重、只补缺失） ────

    def import_msds(self, items: List[Dict[str, Any]],
                    mode: str = "upsert") -> Dict[str, Any]:
        """批量导入化学品。mode=upsert 覆盖同 CAS，mode=fill 只补缺失。

        返回每批处理计数，方便前端展示"补传 N 条缺失"式反馈。
        """
        if mode not in ("upsert", "fill"):
            return {"error": "mode 只支持 upsert / fill"}
        added, updated, skipped, batches = 0, 0, 0, 0
        with self._lock:
            for i in range(0, len(items), _IMPORT_BATCH):
                batches += 1
                for raw in items[i:i + _IMPORT_BATCH]:
                    cas = self._norm_cas(raw.get("cas", ""))
                    name = str(raw.get("name", "")).strip()
                    if not cas and not name:
                        skipped += 1
                        continue
                    key = cas or f"name:{name}"
                    if key in self._msds and mode == "fill":
                        skipped += 1
                        continue
                    entry = self._normalize_entry(raw, cas, name)
                    existed = key in self._msds
                    self._msds[key] = entry
                    updated += 1 if existed else 0
                    added += 0 if existed else 1
            self._save_json("msds.json", self._msds)
        return {"added": added, "updated": updated, "skipped": skipped,
                "batches": batches, "total": len(items)}

    @staticmethod
    def _normalize_entry(raw: Dict[str, Any], cas: str, name: str) -> Dict[str, Any]:
        sections = raw.get("sections") or {}
        norm_secs = {}
        for k, v in sections.items():
            idx = ResourceLibrary._resolve_section(k)
            if idx is not None and v:
                norm_secs[str(idx)] = str(v)
        return {
            "name": name,
            "cas": cas,
            "aliases": list(raw.get("aliases", [])),
            "sections": norm_secs,
            "source": str(raw.get("source", "")),
            "updated_at": int(time.time()),
        }

    def dedupe(self) -> Dict[str, Any]:
        """合并重复条目，返回清理数量。

        CAS 直接作字典键，同 CAS upsert 不会产生重复；真正会积累重复的是
        无 CAS 条目（键为 name:xxx）与后续带 CAS 的同物条目。按规范化键
        （CAS 优先，否则名称）分组，字段互补合并，CAS 组重建为 CAS 键。
        """
        with self._lock:
            before = len(self._msds)
            groups: Dict[str, Dict[str, Any]] = {}
            group_of: Dict[str, str] = {}

            def merge_into(target: str, entry: Dict[str, Any]) -> None:
                if target not in groups:
                    groups[target] = dict(entry)
                    return
                merged = dict(groups[target])
                for k, v in entry.items():
                    if not v:
                        continue
                    cur = merged.get(k)
                    if k == "sections" and isinstance(v, dict):
                        base = dict(cur or {})
                        for sk, sv in v.items():
                            if sv and not base.get(sk):
                                base[sk] = sv
                        merged[k] = base
                    elif k == "aliases" and isinstance(v, list):
                        merged[k] = list(dict.fromkeys(list(cur or []) + list(v)))
                    elif not cur:
                        merged[k] = v
                if entry.get("cas") and not merged.get("cas"):
                    merged["cas"] = entry["cas"]
                groups[target] = merged

            for key, entry in self._msds.items():
                name = re.sub(r"\s+", "", entry.get("name", "").lower())
                cas = entry.get("cas", "")
                name_key = f"name:{name}" if name else ""
                target = None
                for probe in (cas, name_key):
                    if probe and probe in group_of:
                        target = group_of[probe]
                        break
                if target is None:
                    target = cas or name_key or key
                merge_into(target, entry)
                group_of[key] = target
                if cas:
                    group_of[cas] = target
                if name_key:
                    group_of[name_key] = target
            rebuilt = {}
            for target, entry in groups.items():
                rebuilt[entry.get("cas") or target] = entry
            self._msds = rebuilt
            self._save_json("msds.json", self._msds)
            return {"removed": before - len(self._msds), "remaining": len(self._msds)}

    # ── 资料目录 ───────────────────────────────────────────────────────────

    def search_resources(self, query: str = "", category: str = "",
                         top_k: int = 8) -> List[Dict[str, Any]]:
        """关键词检索资料目录（标题/描述/标签），可按分类过滤。"""
        query = (query or "").strip().lower()
        terms = [t for t in re.split(r"[\s,，]+", query) if t]
        with self._lock:
            scored = []
            for r in self._resources:
                if category and r.get("category") != category:
                    continue
                hay = " ".join([r.get("title", ""), r.get("description", ""),
                                " ".join(r.get("tags", []))]).lower()
                score = sum(hay.count(t) * (3 if t in r.get("title", "").lower() else 1)
                            for t in terms)
                if terms and score <= 0:
                    continue
                scored.append((score, r))
            scored.sort(key=lambda sr: -sr[0])
            return [{**r, "score": s} for s, r in scored[:top_k]]

    def add_resource(self, item: Dict[str, Any]) -> Dict[str, Any]:
        """新增/更新资料条目（title+category 定位同一 entry）。"""
        title = str(item.get("title", "")).strip()
        category = str(item.get("category", "")).strip()
        if not title or category not in RESOURCE_CATEGORIES:
            return {"error": "需要 title 且 category 必须是 " + "/".join(RESOURCE_CATEGORIES)}
        with self._lock:
            for r in self._resources:
                if r.get("title") == title and r.get("category") == category:
                    r.update(item)
                    self._save_json("resources.json", self._resources)
                    return {"updated": True, "title": title}
            self._resources.append({**item, "title": title, "category": category})
            self._save_json("resources.json", self._resources)
            return {"added": True, "title": title}

    # ── 台账支撑文件清单（痛点2：台账要很多支撑文件，找起来麻烦） ────────────

    @staticmethod
    def _title_core(title: str) -> str:
        """去掉条目标题末尾的归档括注（如「（模板目录）」），得到对齐用主干。"""
        return re.sub(r"（[^（）]*）\s*$", "", title or "").strip()

    @staticmethod
    def _max_common_len(a: str, b: str) -> int:
        """两字符串最长公共连续片段长度（O(len_a*len_b)，目录规模足够小）。"""
        prev = [0] * (len(b) + 1)
        best = 0
        for ca in a:
            cur = [0] * (len(b) + 1)
            for j, cb in enumerate(b, 1):
                if ca == cb:
                    cur[j] = prev[j - 1] + 1
                    best = max(best, cur[j])
            prev = cur
        return best

    def _match_support_entry(self, item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """台账支撑项 → 资源条目 的严格对齐匹配（checklist 专用）。

        不复用 search_resources 的关键词宽匹配：那里任意一个词命中即得分
        （title/desc/tags 的任意子串计数），台账项又普遍带「培训/演练/清单/
        复查」等通用标签，会把签到表挂到培训计划、危化品清单挂到特种设备
        清单上——matched 虚高、真实缺口被掩盖。这里要求同时满足：
          1) category 完全一致：企业自存文件（documents）不拿标准文本充数；
          2) 台账项全部 tags 在条目标签中逐项精确命中（种子条目标签与其
             对应台账项 1:1 对齐，见 resources_seed.json 说明）；
          3) 台账项名称与条目标题主干有 ≥2 字连续公共片段，
             排除仅靠通用标签蹭上的条目。
        满足者取名称重合最长的一条；否则返回 None，清单照实标 missing。
        """
        name = str(item.get("name", "")).strip()
        tags = [t for t in item.get("tags", []) if t]
        cat = item.get("category", "")
        best, best_overlap = None, 1  # 名称重合须 >1（即 ≥2 字）才认
        for r in self._resources:
            if cat and r.get("category") != cat:
                continue
            rtags = set(r.get("tags", []))
            if tags and not all(t in rtags for t in tags):
                continue
            overlap = self._max_common_len(name, self._title_core(r.get("title", "")))
            if overlap > best_overlap:
                best, best_overlap = r, overlap
        return best

    def list_ledgers(self) -> List[Dict[str, Any]]:
        """可用台账类型清单。"""
        return [{"id": b.get("id"), "name": b.get("name"),
                 "aliases": b.get("aliases", []),
                 "support_files": len(b.get("items", []))}
                for b in self._ledgers]

    def checklist(self, ledger: str) -> Dict[str, Any]:
        """给定台账类型（id/名称/别名），返回支撑文件清单。

        每项支撑文件按严格对齐规则在资源库中匹配已有条目：命中则带出
        （matched），未命中标 missing=true，管理员照单补文件即可——这正是
        "找文件麻烦"痛点的直接解法：一张清单说清"这台账还缺哪些件"。
        """
        ledger = (ledger or "").strip()
        if not ledger:
            return {"error": "需要提供台账类型", "available": self.list_ledgers()}
        bundle = None
        ll = ledger.lower()
        # id 只做精确比较：英文 id 之间存在前缀包含（hazard ⊂
        # hazardous_chemicals），双向子串会把危化品台账误路由到隐患台账；
        # 中文名称/别名保留子串匹配，让「危化品台账」这类简称仍能命中。
        for b in self._ledgers:
            if (b.get("id", "") or "").lower() == ll:
                bundle = b
                break
        if bundle is None:
            for b in self._ledgers:
                names = [b.get("name", "")] + list(b.get("aliases", []))
                if any(ll in n.lower() or n.lower() in ll for n in names if n):
                    bundle = b
                    break
        if bundle is None:
            return {"found": False, "ledger": ledger,
                    "available": self.list_ledgers(),
                    "hint": "未收录该台账类型，可在 ledger_bundles.json 中补充映射"}
        items = []
        matched_count = 0
        for it in bundle.get("items", []):
            hit = self._match_support_entry(it)
            matched_count += 1 if hit else 0
            items.append({
                "name": it.get("name"),
                "description": it.get("description"),
                "category": it.get("category"),
                "matched": {"title": hit["title"], "description": hit.get("description", "")}
                           if hit else None,
                "missing": hit is None,
            })
        return {
            "found": True,
            "ledger": bundle.get("name"),
            "items": items,
            "summary": {"total": len(items), "matched": matched_count,
                        "missing": len(items) - matched_count},
            "disclaimer": "清单为常见实践总结，具体以当地监管要求和企业制度为准",
        }

    # ── MCP 工具 handler（与 KnowledgeBase.search_handler 同款签名） ───────

    async def msds_handler(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        action = params.get("action", "lookup")
        if action == "lookup":
            return self.lookup(params.get("name", ""), params.get("cas", ""),
                               params.get("section"))
        if action == "hot":
            return {"items": self.hot(int(params.get("top_k", 10)))}
        if action == "stats":
            return self.stats()
        return {"error": f"未知 action: {action}，可用 lookup/hot/stats"}

    async def resource_handler(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        query = params.get("query", "")
        category = params.get("category", "")
        items = self.search_resources(query, category,
                                      int(params.get("top_k", 8)))
        if not items:
            cats = "、".join(f"{k}({v})" for k, v in RESOURCE_CATEGORIES.items())
            return {"items": [], "hint": f"未命中资料，可用分类: {cats}，或改用知识库检索"}
        return {"items": items}

    async def ledger_handler(self, params: Dict[str, Any], context: Any) -> Dict[str, Any]:
        action = params.get("action", "checklist")
        if action == "checklist":
            return self.checklist(params.get("ledger", ""))
        if action == "list":
            return {"ledgers": self.list_ledgers()}
        return {"error": f"未知 action: {action}，可用 checklist/list"}
