# -*- coding: utf-8 -*-
"""K01 目录快照导入与对账（本机当前可做的一半）。

设计依据：docs/资料库建设决策与Agent实施方案_2026-10-19.md
  §2.2/§2.3 目录层只是「能找到资料」，不等于已有正文；downloadUrl 取得成功只记 link_exported。
  §4.1 导出目录快照 → 登记 manifest（受控链接引用、导出时间、来源条目 ID）。
  §4.1 第 5 步 先核验 5~10 个分享（单文件/文件夹/合集、多条复用同一链接）。
  §9   未取得远端完整清单时只能对账目录项，不能声称文件全量。

本模块不做任何下载或网络访问：远端不可达的链路一律进 blocked_register 逐项登记。
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

from corpus import paths
from corpus.acquisition import detect_provider, redact
from corpus.manifest import CorpusManifest

# ── 字段别名：兼容多种目录快照（含 wxapkg 解包报告里的目录 JSON）────────────────
_ITEM_ID_KEYS = ("item_id", "itemId", "id", "_id", "fileId", "file_id", "key")
_TITLE_KEYS = ("title", "name", "fileName", "file_name", "资源名称", "标题")
_URL_KEYS = ("downloadUrl", "download_url", "url", "quarkLink", "link", "资源链接", "补充资源链接")
_CATEGORY_KEYS = ("category", "type", "分类", "类型")
_UPDATED_KEYS = ("updated_at", "updateTime", "update_time", "date", "上传日期", "更新时间")
_SUMMARY_KEYS = ("summary", "desc", "description", "简介", "remark", "备注")
_LIST_KEYS = ("items", "entries", "files", "list", "rows", "data", "adminFiles", "catalog", "resources")

_FILE_EXTS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z",
              ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".gif", ".mp4")

# 凭据赋值形式（"key=value"）——写报告前的断言式纪律；说明文字里提到该词不算凭据
_CRED_ASSIGN_RE = re.compile(
    r"(?i)\b(?:cookie|token|passcode|passwd|password|pwd|secret|signature|sign|"
    r"access[_-]?key|api[_-]?key)\s*[:=]\s*\S")


def _sha1(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def _first(mapping: Mapping, keys: Sequence[str]) -> Any:
    for k in keys:
        if k in mapping and mapping[k] not in (None, ""):
            return mapping[k]
    return ""


def _extract_items(obj: Any, depth: int = 0) -> List[Dict[str, Any]]:
    """从快照对象中抽取条目前缀列表（兼容裸数组与多层包裹）。"""
    if depth > 4 or obj is None:
        return []
    if isinstance(obj, list):
        return [i for i in obj if isinstance(i, dict)]
    if isinstance(obj, Mapping):
        for key in _LIST_KEYS:
            val = obj.get(key)
            if isinstance(val, list):
                return [i for i in val if isinstance(i, dict)]
            if isinstance(val, Mapping):
                inner = _extract_items(val, depth + 1)
                if inner:
                    return inner
        if any(k in obj for k in _TITLE_KEYS):
            return [dict(obj)]
    return []


def _share_key(link: str) -> str:
    """分享标识：去掉查询串与锚点后取哈希（同一分享页带不同参数/锚点仍归为一个分享）。"""
    base = str(link or "").split("#", 1)[0].split("?", 1)[0]
    return _sha1(base)[:16] if base else ""


def _classify_link(link: str) -> str:
    """按链接形态粗判：单文件 / 文件夹 / 合集 / 分享页 / 无。"""
    text = str(link or "")
    if not text:
        return "none"
    path = text.split("?", 1)[0].split("#", 1)[0].lower()
    if path.endswith(_FILE_EXTS):
        return "file"
    if any(k in path for k in ("folder", "/d/", "目录", "dir")):
        return "folder"
    if any(k in path for k in ("collection", "album", "合集", "list")):
        return "collection"
    if "/s/" in path or "share" in path or "pan" in path:
        return "share"
    return "unknown"


def _reports_dir(root: Path, reports_dir: Optional[Union[str, Path]]) -> Path:
    return Path(reports_dir) if reports_dir else paths.subdir("reports", root)


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path.as_posix()


def _redact_deep(obj: Any) -> Any:
    """递归脱敏：raw 条目保留原结构，但字符串值里的凭据一律抹除（不把提取码写进库/报告）。"""
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [_redact_deep(i) for i in obj]
    if isinstance(obj, Mapping):
        return {k: _redact_deep(v) for k, v in obj.items()}
    return obj


def export_snapshot(path_or_obj: Union[str, Path, Mapping, List[Any]], *,
                    provider: str = "toolbox_catalog", exported_at: str = "", note: str = "",
                    manifest: Optional[CorpusManifest] = None,
                    root: Optional[Union[str, Path]] = None,
                    write_reports: bool = True,
                    reports_dir: Optional[Union[str, Path]] = None,
                    expected_total: int = 2640) -> Dict[str, Any]:
    """把已导出的目录快照导入 manifest（每条只记 catalog_registered，可额外置 link_exported）。

    绝不置 file_downloaded_verified / body_parsed —— 目录与链接不等于已取得正文。
    """
    base = paths.ensure_layout(Path(root) if root else None)
    m = manifest or CorpusManifest(paths.manifest_db(base))
    if isinstance(path_or_obj, (str, Path)):
        source_path = str(Path(path_or_obj))
        raw = json.loads(Path(path_or_obj).read_text(encoding="utf-8"))
    else:
        source_path, raw = "", path_or_obj
    items = _extract_items(raw)

    existing = {r["item_id"] for r in m.list_sources(provider=provider) if r["item_id"]}
    created = updated = with_link = without_link = 0
    redacted_items: List[Dict[str, Any]] = []
    for it in items:
        title = str(_first(it, _TITLE_KEYS) or "")
        raw_url = str(_first(it, _URL_KEYS) or "")
        link_ref = redact(raw_url)
        item_id = str(_first(it, _ITEM_ID_KEYS) or "") or _sha1(f"{title}|{link_ref}")[:24]
        fields: Dict[str, Any] = {
            "title": title,
            "category": str(_first(it, _CATEGORY_KEYS) or ""),
            "updated_at": str(_first(it, _UPDATED_KEYS) or ""),
            "summary": str(_first(it, _SUMMARY_KEYS) or ""),
            "netdisk_provider": detect_provider(raw_url),
            "link_ref": link_ref,
            "raw": _redact_deep(it),
        }
        if exported_at:
            fields["exported_at"] = exported_at
        if item_id in existing:
            updated += 1
            m.upsert_source(provider, item_id, **fields)  # 不传状态：避免把已推进的台阶降级
        else:
            created += 1
            m.upsert_source(provider, item_id, acquisition_status="catalog_registered", **fields)
            existing.add(item_id)
        if link_ref:
            with_link += 1
            m.set_status(item_id, ladder=["link_exported"])
        else:
            without_link += 1
        redacted_items.append({"item_id": item_id, "title": title, "link_ref": link_ref,
                               "category": fields["category"], "updated_at": fields["updated_at"],
                               "netdisk_provider": fields["netdisk_provider"]})

    result: Dict[str, Any] = {
        "provider": provider, "exported_at": exported_at, "note": note,
        "snapshot": source_path, "total": len(items), "created": created, "updated": updated,
        "with_link": with_link, "without_link": without_link,
        "highest_status": "catalog_registered",
        "ladder_marked": ["link_exported"] if with_link else [],
        "file_downloaded_verified": 0, "body_parsed": 0,
    }
    if write_reports:
        reports = _reports_dir(base, reports_dir)
        result["report_files"] = [
            _write_json(reports / f"catalog_snapshot_{_safe(provider)}.json",
                        {"provider": provider, "exported_at": exported_at, "note": note,
                         "snapshot_source": source_path, "total": len(items),
                         "with_link": with_link, "without_link": without_link,
                         "items": redacted_items}),
        ]
        recon = reconcile_catalog(expected_total, provider=provider, manifest=m, root=base,
                                  write_report=True, reports_dir=reports)
        result["reconciliation"] = recon
    return result


def _safe(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(text or ""))[:40] or "x"


def reconcile_catalog(expected_total: int = 2640, *, provider: str = "toolbox_catalog",
                      manifest: Optional[CorpusManifest] = None,
                      root: Optional[Union[str, Path]] = None,
                      write_report: bool = False,
                      reports_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """与「2640」对账：catalog_only scope 下的差异 + 为什么不能声称文件全量的说明字段。"""
    base = paths.ensure_layout(Path(root) if root else None)
    m = manifest or CorpusManifest(paths.manifest_db(base))
    recon = m.reconciliation(provider=provider, scope="catalog_only")
    registered = recon["expected_leaf_files"]
    delta = registered - int(expected_total)
    out: Dict[str, Any] = {
        "provider": provider,
        "scope": "catalog_only",
        "expected_total": int(expected_total),
        "registered_total": registered,
        "delta": delta,
        "delta_direction": "超出" if delta > 0 else ("缺口" if delta < 0 else "一致"),
        "remote_file_list_obtained": False,
        "cannot_claim_file_completeness": True,
        "why_not_file_denominator": (
            "未取得远端完整文件清单：目录条目可能指向文件夹或合集，一个条目可含多个文件，"
            "也可能与其它条目重复。因此只能对账目录项，不能声称文件全量。"),
        "acquired_files_verified": recon["acquired_files_verified"],
        "by_ladder": recon["acquired_files"],
        "by_acquisition_status": recon["by_acquisition_status"],
        "by_parse_status": recon["by_parse_status"],
        "buckets": {k: recon[k] for k in ("verified", "duplicate_mapping",
                                          "excluded_with_reason", "todo", "blocked")},
        "note": recon["note"],
    }
    if write_report:
        reports = _reports_dir(base, reports_dir)
        out["report_files"] = [_write_json(reports / "catalog_reconciliation.json", out)]
    return out


def _resolve_items(m: CorpusManifest, items: Sequence[Any]) -> List[Dict[str, Any]]:
    """把条目（item_id 字符串 / 来源行 / 目录条目字典）统一解析为带 link_ref 的记录。

    字符串与「已在库的来源行」按 item_id 反查；其余字典按目录条目字段解析。
    """
    rows = {r["item_id"]: r for r in m.list_sources()}
    out: List[Dict[str, Any]] = []
    for it in items:
        if isinstance(it, str):
            row = rows.get(it)
            if row:
                out.append({"item_id": row["item_id"], "title": row["title"],
                            "link_ref": row["link_ref"], "share_id": ""})
            continue
        if not isinstance(it, Mapping):
            continue
        iid = str(_first(it, _ITEM_ID_KEYS) or "")
        if iid and iid in rows and not it.get("link_ref") and not _first(it, _URL_KEYS):
            row = rows[iid]  # 已是库内来源行 → 直接用其受控引用
            out.append({"item_id": row["item_id"], "title": row["title"],
                        "link_ref": row["link_ref"], "share_id": ""})
            continue
        link = str(it.get("link_ref") or _first(it, _URL_KEYS) or "")
        out.append({"item_id": iid or _sha1(f"{_first(it, _TITLE_KEYS)}|{link}")[:24],
                    "title": str(_first(it, _TITLE_KEYS) or ""),
                    "link_ref": redact(link), "share_id": str(it.get("share_id") or "")})
    return out


def precheck_shares(items: Sequence[Any], *, limit: int = 10,
                    provider: str = "toolbox_catalog",
                    manifest: Optional[CorpusManifest] = None,
                    root: Optional[Union[str, Path]] = None,
                    registry: Any = None,
                    mark_link_exported: bool = True) -> Dict[str, Any]:
    """核验给定条目的分享维度（§4.1 第 5 步：先核验 5~10 个分享）。

    未实际打开分享时只记 `link_exported`，并输出 needs_manual_verify 清单。
    """
    base = paths.ensure_layout(Path(root) if root else None)
    m = manifest or CorpusManifest(paths.manifest_db(base))
    selected = _resolve_items(m, items)[: max(int(limit), 0)]
    by_id = {r["item_id"]: r for r in m.list_sources(provider=provider)}

    per_share: Dict[str, Dict[str, Any]] = {}
    needs: List[Dict[str, str]] = []
    for rec in selected:
        link = rec["link_ref"]
        sid = rec.get("share_id") or _share_key(link)
        looks = _classify_link(link)
        g = per_share.setdefault(sid, {"share_id": sid, "links": [], "items": [], "titles": [],
                                       "looks_like": looks, "multi_title": False})
        if link and link not in g["links"]:
            g["links"].append(link)
        if rec["item_id"] and rec["item_id"] not in g["items"]:
            g["items"].append(rec["item_id"])
        if rec["title"] and rec["title"] not in g["titles"]:
            g["titles"].append(rec["title"])
        row = by_id.get(rec["item_id"])
        if mark_link_exported and link and row is not None:
            if "link_exported" not in list(row["ladder"] or []):
                m.set_status(rec["item_id"], ladder=["link_exported"])
        needs.append({"item_id": rec["item_id"], "share_id": sid,
                      "reason": "未实际打开分享，仅按目录链接预判" if link else "条目无外链"})

    shares = sorted(per_share.values(), key=lambda g: g["share_id"])
    for g in shares:
        g["multi_title"] = len(g["titles"]) > 1
        g["item_count"] = len(g["items"])
    return {
        "provider": provider, "limit": int(limit), "checked": len(selected),
        "unique_shares": len(per_share),
        "shares": shares,
        "reused_shares": [g["share_id"] for g in shares if g["item_count"] > 1],
        "multi_title_shares": [g["share_id"] for g in shares if g["multi_title"]],
        "folder_or_collection": [g["share_id"] for g in shares
                                 if g["looks_like"] in ("folder", "collection")],
        "file_like_links": [g["share_id"] for g in shares if g["looks_like"] == "file"],
        "needs_manual_verify": needs,
        "only_ladder_marked": "link_exported",
        "note": "未实际打开分享：只记 link_exported，不得置 share_accessible/file_downloaded_verified",
    }


def blocked_register(root: Optional[Union[str, Path]] = None, *,
                     reports_dir: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """把当前真实阻塞逐项登记到 reports/blocked_items.json（不写任何真实凭据）。"""
    base = paths.ensure_layout(Path(root) if root else None)
    reports = _reports_dir(base, reports_dir)
    usage = shutil.disk_usage(str(base))
    free_gib = usage.free / float(1 << 30)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    blockers = [
        {
            "blocker": "微信小程序会话与 Storage 导出不可得",
            "why": ("本机仅有缓存包 D:/WeChat/WeChat Files/Applet/<WX_APPID>/9/__APP__.wxapkg"
                    "（411544 字节，SHA-256 50aa789fac02715344d1d0b38734f2b98e29d8450a2afaab1d2fa5222146d19a）；"
                    "该包不含正常会话已读取的目录与 Storage 值，目录与 MSDS 由云端动态读取，"
                    "故无法取得线上完整目录。"),
            "needs": "正常微信客户端可见会话的目录/Storage 导出，或人工导出的目录快照 JSON",
            "what_can_still_run": ("已导出目录快照的导入与对账、分享维度预核验、"
                                   "解析/检索/评测离线链路（K01 本机部分）"),
            "next_action": "取得目录快照 JSON 后调用 catalog_export.export_snapshot 导入并对账",
        },
        {
            "blocker": "夸克网盘登录与实际下载不可得",
            "why": ("本机无网盘账号登录、无提取码/转存条件；不访问夸克，也不使用私有 API；"
                    "真实分享无法实际打开与下载。"),
            "needs": "可达的分享链接，或允许在现有账号正常转存后按官方客户端清单下载",
            "what_can_still_run": ("用本地 HTTP 文件服务当网盘替身，端到端跑通"
                                   "下载→流式 sha256→格式签名校验→内容寻址落盘→台阶推进（已可执行）"),
            "next_action": "先核验 5~10 个分享（precheck_shares），再按唯一分享与实际文件去重下载",
        },
        {
            "blocker": "无法取得远端完整文件清单（因此只能对账目录项）",
            "why": ("目录条目可能指向文件夹或合集，一个条目可含多个文件，也可能与其它条目重复；"
                    "远端叶文件清单未取得，文件级分母不成立。"),
            "needs": "任一分享/文件夹的完整叶文件清单（名称、大小、格式）",
            "what_can_still_run": "目录项对账 reconcile_catalog(scope=catalog_only) 与差异说明",
            "next_action": "在 5~10 个分享上核验实际文件映射后，逐步扩展为文件级分母",
        },
        {
            "blocker": "原件容量预算与 D 盘现状",
            "why": (f"方案 §6 预算：原件 8GiB、总 11GiB、D 盘始终保留 ≥15GiB；"
                    f"实测 D 盘剩余 {free_gib:.2f} GiB。远端总大小未知，无法事先判断是否超预算，"
                    "超限只能逐项登记为容量阻塞并保留待办与大小。"),
            "needs": "远端文件大小清单或分层样本以估算全量磁盘需求",
            "what_can_still_run": "容量守卫（单文件上限/总预算/闲置阈值）与 capacity_blocked 登记",
            "next_action": "取得远端大小后按 8GiB 预算分批；超限项逐项列容量阻塞，不改 done",
        },
        {
            "blocker": "1698 条自有时段无原件",
            "why": ("1698 条是用户自行加入的既有资料记录（NumPy 索引 1698×768 float32），"
                    "属既有基线而非百宝箱成果；本地转换产物不含可归档原件。"),
            "needs": "用户自有的原始文件（若需归档原件），并保持 user_imported 独立来源统计",
            "what_can_still_run": "user_imported 独立来源统计与解析/检索链路（按既有转换产物）",
            "next_action": "原件缺失逐项标记；不得把 1698 计入百宝箱采集分母",
        },
    ]
    payload = {"generated_at": now, "root": base.as_posix(), "count": len(blockers),
               "disk_free_bytes": usage.free, "disk_free_gib": round(free_gib, 2),
               "note": "本文件只登记阻塞项与下一步动作；不含任何账号凭据或临时直链参数。",
               "blockers": blockers}
    # 断言式凭据纪律：不得出现「凭据键=值」形式的赋值（说明文字里提到该词不算凭据）
    assert not _CRED_ASSIGN_RE.search(json.dumps(payload, ensure_ascii=False)), \
        "阻塞登记不得含凭据赋值"
    payload["report_files"] = [_write_json(reports / "blocked_items.json", payload)]
    return payload


class CatalogExporter:
    """目录快照导入/对账/分享预核验/阻塞登记的绑定实例（复用同一 manifest 与根目录）。"""

    def __init__(self, *, manifest: Optional[CorpusManifest] = None,
                 root: Optional[Union[str, Path]] = None,
                 provider: str = "toolbox_catalog",
                 reports_dir: Optional[Union[str, Path]] = None):
        self.root = paths.ensure_layout(Path(root) if root else None)
        self.manifest = manifest or CorpusManifest(paths.manifest_db(self.root))
        self.provider = provider
        self.reports_dir = reports_dir

    def export_snapshot(self, path_or_obj, *, exported_at: str = "", note: str = "",
                        write_reports: bool = True, expected_total: int = 2640) -> Dict[str, Any]:
        return export_snapshot(path_or_obj, provider=self.provider, exported_at=exported_at,
                               note=note, manifest=self.manifest, root=self.root,
                               write_reports=write_reports, reports_dir=self.reports_dir,
                               expected_total=expected_total)

    def reconcile_catalog(self, expected_total: int = 2640, *,
                          write_report: bool = False) -> Dict[str, Any]:
        return reconcile_catalog(expected_total, provider=self.provider, manifest=self.manifest,
                                 root=self.root, write_report=write_report,
                                 reports_dir=self.reports_dir)

    def precheck_shares(self, items, *, limit: int = 10, mark_link_exported: bool = True) -> Dict[str, Any]:
        return precheck_shares(items, limit=limit, provider=self.provider, manifest=self.manifest,
                               root=self.root, mark_link_exported=mark_link_exported)

    def blocked_register(self) -> Dict[str, Any]:
        return blocked_register(self.root, reports_dir=self.reports_dir)
