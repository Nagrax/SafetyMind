# -*- coding: utf-8 -*-
"""K04 外链唯一获取路线：下载 → 校验 → 内容寻址落盘 → 台阶推进。

设计依据：docs/资料库建设决策与Agent实施方案_2026-10-19.md
  §2.3 台阶纪律：link_exported → share_accessible → file_downloaded_verified → body_parsed
       「链接可取」不等于「文件已取得」；只有实际落盘并校验通过才可记 file_downloaded_verified。
  §4.1 导出目录 → 分批取原件 → 校验大小/格式/完整 SHA-256 → 登记资产；同一分享只遍历一次。
  §6   容量预算：原件 8GiB、下载并发 2、D 盘始终保留 ≥15GiB；触发即暂停扩大落盘。
  §9   状态与分母：容量阻塞保留待办与大小，不把剩余项改 done。

边界：
  - 本模块只做「取到原件并校验」这一段；正文解析在 corpus.parsers（K05）。
  - 不访问夸克/微信：真实下载由注入的 transport 承担（默认 urllib；测试注入本地 HTTP 服务）。
  - 一切下载/缓存/临时只落 D 盘（corpus.paths 强制校验，铁律 7）。
  - 凭据纪律：cookie/token/提取码/签名参数在写库或写报告前一律先经 redact()。
"""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from corpus import parsers, paths
from corpus.manifest import LADDER_STEPS, CorpusManifest, _SECRET_RE

# ── 容量默认值（方案 §6）──────────────────────────────────────────────────────
GIB = 1 << 30
DEFAULT_MAX_BYTES = 1 * GIB             # 单文件上限（可覆盖）
DEFAULT_TOTAL_BUDGET_BYTES = 8 * GIB    # 原件总预算 §6
DEFAULT_DISK_FREE_GUARD_BYTES = 15 * GIB  # D 盘始终保留 ≥15GiB §6
CHUNK = 1 << 16

# ── 凭据脱敏 ─────────────────────────────────────────────────────────────────
_SENSITIVE_KEYS = (r"cookie|token|passcode|passwd|password|pwd|secret|signature|sign|"
                   r"access[_-]?key|api[_-]?key|code")
_URL_PARAM_RE = re.compile(rf"(?i)([?&;])(?:{_SENSITIVE_KEYS})=[^&#;\s]*")
_KV_RE = re.compile(rf"(?i)\b(?:{_SENSITIVE_KEYS})\s*[:=]\s*[^\s&#;]*")
_CN_RE = re.compile(r"(提取码|访问码)\s*[:：=]?\s*[^\s,，;；]*")
_KEY_SYNTAX_RE = re.compile(rf"(?i)\b(?:{_SENSITIVE_KEYS})\s*[:=]")
_CN_KEY_RE = re.compile(r"(提取码|访问码)")

# 二进制容器格式：无文件签名却声称该格式 → 判为「内容与 mime/扩展名不符」
_BINARY_KINDS = frozenset({"pdf", "zip", "docx", "xlsx", "pptx", "ppt_legacy"})
_MAGIC_ACCEPTS = {
    "pdf": frozenset({"pdf"}),
    "zip": frozenset({"zip", "docx", "xlsx", "pptx"}),
    "ole": frozenset({"ppt_legacy", "other", "docx", "xlsx"}),
    "image": frozenset({"image_scan"}),
}


def redact(text: Any) -> str:
    """抹掉 cookie/token/提取码/签名临时直链参数。

    先删 URL 查询参数对（保留分隔符），再删 header/kv 形式，再删中文提取码，
    最后兜底到「不再触发 manifest 的凭据禁写规则」。幂等。
    """
    s = str(text or "")
    s = _URL_PARAM_RE.sub(lambda m: m.group(1), s)
    s = _KV_RE.sub("REDACTED", s)
    s = _CN_RE.sub("REDACTED", s)
    if "://" in s:  # 仅对 URL 清理悬空分隔符（避免误伤普通文本末尾的问号）
        s = re.sub(r"([?&;])[&;]+", r"\1", s)
        s = re.sub(r"[?&;]+(?=#|$)", "", s)
    if _SECRET_RE.search(s):  # 兜底：保证 link_ref 写库不被拒
        s = _KEY_SYNTAX_RE.sub("REDACTED", s)
        s = _CN_KEY_RE.sub("REDACTED", s)
    return s


def detect_provider(url: str) -> str:
    """按外链域名归一网盘供应商（未知保留 unknown）。"""
    host = (urllib.parse.urlparse(str(url or "")).netloc or "").lower()
    if "quark" in host:
        return "quark"
    if "baidu" in host:
        return "baidu_netdisk"
    if "aliyun" in host or "alipan" in host:
        return "aliyun_drive"
    if "weiyun" in host or "qq.com" in host:
        return "weiyun"
    return "unknown"


def _sha1(text: str) -> str:
    return hashlib.sha1(str(text).encode("utf-8")).hexdigest()


def _slug(text: Any, n: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(text or ""))[:n] or "item"


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _manifest_for(root: Optional[Union[str, Path]], manifest: Optional[CorpusManifest]) -> CorpusManifest:
    if manifest is not None:
        return manifest
    return CorpusManifest(paths.manifest_db(Path(root) if root else None))


# ── 传输层 ───────────────────────────────────────────────────────────────────
class HttpResponse:
    """统一响应对象：status / headers（小写键）/ read(n) / close()。"""

    def __init__(self, status: Any, headers: Any, raw: Any):
        self.status = int(status or 0)
        try:
            items = headers.items()
        except AttributeError:  # pragma: no cover - 退化输入
            items = dict(headers or {}).items()
        self.headers = {str(k).lower(): str(v) for k, v in items}
        self._raw = raw

    def read(self, n: int) -> bytes:
        return self._raw.read(n)

    def close(self) -> None:
        try:
            self._raw.close()
        except Exception:  # pragma: no cover - 关闭失败不影响结果
            pass


def urllib_transport(url: str, *, method: str = "GET", headers: Optional[Dict[str, str]] = None,
                     timeout: float = 60.0) -> HttpResponse:
    """默认传输层（stdlib）：GET/HEAD + Range 透传；HTTP 错误码也作为正常响应返回。"""
    req = urllib.request.Request(str(url), method=method, headers=dict(headers or {}))
    try:
        raw = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as ex:
        raw = ex
    status = getattr(raw, "status", None) or getattr(raw, "code", 0)
    return HttpResponse(status, raw.headers, raw)


class _HttpStatus(Exception):
    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = int(status)


# ── 内容寻址落盘 ─────────────────────────────────────────────────────────────
def save_original(content_or_path: Union[str, Path, bytes, bytearray], *, mime: str = "",
                  staging_dir: Optional[Union[str, Path]] = None,
                  manifest: Optional[CorpusManifest] = None,
                  root: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """内容寻址落盘：originals/sha256/<前2位>/<sha256>，流式写 + 边写边算 sha256。

    同内容不重复落盘（复用 manifest.register_asset 的 sha 复用语义）。
    返回 {asset_id, sha256, bytes, path, mime, deduped}。
    """
    m = _manifest_for(root, manifest)
    base = paths.ensure_layout(Path(root) if root else None)

    if isinstance(content_or_path, (bytes, bytearray)):
        data = bytes(content_or_path)
        asset_id = m.register_asset(data, mime=mime)
        rec = m.get_asset(asset_id) or {}
        return {"asset_id": asset_id, "sha256": asset_id, "bytes": len(data),
                "path": rec.get("path", ""), "mime": rec.get("mime", mime), "deduped": False}

    src = Path(content_or_path)
    if not src.is_file():
        raise FileNotFoundError(f"save_original 需要文件路径或内容：{src}")

    stage = Path(staging_dir) if staging_dir else paths.staging_dir("save", base)
    if stage.drive and stage.drive.upper() != "D:":
        raise ValueError(f"staging_dir 只允许 D 盘（铁律 7）：{stage}")
    stage.mkdir(parents=True, exist_ok=True)
    tmp = stage / f"{_slug(src.name)}.{os.getpid()}.{int(time.time() * 1000)}.part"

    hasher = hashlib.sha256()
    total = 0
    with src.open("rb") as f, tmp.open("wb") as out:
        for buf in iter(lambda: f.read(CHUNK), b""):
            hasher.update(buf)
            out.write(buf)
            total += len(buf)
    sha = hasher.hexdigest()

    dest = paths.subdir("originals/sha256", base) / sha[:2] / sha
    deduped = dest.exists()
    if deduped:
        tmp.unlink(missing_ok=True)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(tmp), str(dest))

    asset_id = m.register_asset(dest, mime=mime)
    return {"asset_id": asset_id, "sha256": asset_id, "bytes": total, "path": dest.as_posix(),
            "mime": mime or (mimetypes.guess_type(src.name)[0] or ""), "deduped": deduped}


# ── 分享登记簿（条目 → 分享 → 文件）───────────────────────────────────────────
class ShareRegistry:
    """条目→分享→实际文件映射登记簿（基于 manifest.entry_asset_mapping）。

    同一 share_id 只下载一次：多个条目指向同一分享/文件时复用同一 asset，
    但每个条目各留一条映射（谱系与分母不合并）。
    """

    def __init__(self, manifest: CorpusManifest):
        self.manifest = manifest
        self._downloaded: Dict[str, str] = {}

    def record(self, share_id: str, asset_id: str, share_file_id: str = "") -> None:
        if share_id and asset_id:
            self._downloaded.setdefault(str(share_id), str(asset_id))

    def asset_for(self, share_id: str) -> str:
        """该分享是否已有已落盘资产（本会话优先，其次查库）→ asset_id；否则空串。"""
        sid = str(share_id or "")
        if not sid:
            return ""
        if sid in self._downloaded:
            return self._downloaded[sid]
        for mp in self.manifest.list_mappings():
            if mp["share_id"] == sid and mp["asset_id"]:
                return str(mp["asset_id"])
        return ""

    def unique_shares(self) -> List[Dict[str, Any]]:
        """唯一分享清单：同分享的多条目合并为一条，不重复计数。"""
        groups: Dict[str, Dict[str, Any]] = {}
        for mp in self.manifest.list_mappings():
            sid = str(mp["share_id"] or "")
            g = groups.setdefault(sid, {"share_id": sid, "items": [], "assets": set(),
                                        "mappings": 0, "statuses": set()})
            g["mappings"] += 1
            if mp["asset_id"]:
                g["assets"].add(str(mp["asset_id"]))
            if mp["item_id"] not in g["items"]:
                g["items"].append(str(mp["item_id"]))
            g["statuses"].add(str(mp["mapping_status"]))
        out = []
        for sid, g in groups.items():
            out.append({"share_id": sid, "items": g["items"], "assets": sorted(g["assets"]),
                        "mappings": g["mappings"], "statuses": sorted(g["statuses"]),
                        "downloaded_this_session": sid in self._downloaded})
        return out

    def entry_asset_plan(self, item_ids: Sequence[str]) -> Dict[str, Any]:
        """条目→资产计划：唯一分享、可复用（已有资产）、待下载（尚无资产）。"""
        wanted = {str(i) for i in item_ids}
        plan: Dict[str, Any] = {"items": {}, "unique_shares": [], "reusable": [], "to_download": []}
        shares: Dict[str, str] = {}
        for mp in self.manifest.list_mappings():
            iid, sid, aid = str(mp["item_id"]), str(mp["share_id"]), str(mp["asset_id"])
            if iid in wanted:
                plan["items"].setdefault(iid, []).append(
                    {"share_id": sid, "asset_id": aid, "mapping_status": mp["mapping_status"]})
            if sid and sid not in shares:
                shares[sid] = aid
        for sid, aid in shares.items():
            plan["unique_shares"].append({"share_id": sid, "asset_id": aid})
            (plan["reusable"] if aid else plan["to_download"]).append(sid)
        return plan


# ── 下载器 ───────────────────────────────────────────────────────────────────
class Downloader:
    """分批取原件：下载 / 断点续传 / 校验 / 容量守卫 / 台阶推进 / 分享去重。

    传输层可注入（`transport`，默认 urllib.get）：测试注入本地 HTTP 服务或假函数。
    """

    def __init__(self, *, transport=None, max_parallel: int = 2,
                 staging_dir: Optional[Union[str, Path]] = None,
                 max_bytes: int = DEFAULT_MAX_BYTES,
                 total_budget_bytes: int = DEFAULT_TOTAL_BUDGET_BYTES,
                 disk_free_guard_bytes: int = DEFAULT_DISK_FREE_GUARD_BYTES,
                 manifest: Optional[CorpusManifest] = None,
                 root: Optional[Union[str, Path]] = None,
                 provider: str = "toolbox_catalog",
                 registry: Optional[ShareRegistry] = None,
                 timeout: float = 60.0):
        self._root = paths.ensure_layout(Path(root) if root else None)
        self.manifest = _manifest_for(self._root, manifest)
        self._transport = transport or urllib_transport
        self.max_parallel = max(1, int(max_parallel))
        self._sem = threading.Semaphore(self.max_parallel)
        self.provider = provider
        self.timeout = float(timeout)
        self.max_bytes = int(max_bytes)
        self.total_budget_bytes = int(total_budget_bytes)
        self.disk_free_guard_bytes = int(disk_free_guard_bytes)
        self.staging = Path(staging_dir) if staging_dir else paths.staging_dir("downloads", self._root)
        self.registry = registry or ShareRegistry(self.manifest)
        # 计数（供测试与报告核对：并发/字节）
        self.downloads_started = 0
        self.probes = 0
        self.transferred_bytes = 0
        self.last_transferred_bytes = 0

    # ── 状态与台阶 ──────────────────────────────────────────────────────────
    def _find_source(self, item_id: str) -> Optional[Dict[str, Any]]:
        item_id = str(item_id or "")
        rows = [r for r in self.manifest.list_sources(provider=self.provider) if r["item_id"] == item_id]
        if not rows:
            rows = [r for r in self.manifest.list_sources() if r["item_id"] == item_id]
        return rows[-1] if rows else None

    def mark_ladder(self, item_id: str, step: str, **evidence) -> List[str]:
        """台阶状态机：只允许逐级前进（跳级报错）；file_downloaded_verified 需已登记资产。"""
        if step not in LADDER_STEPS:
            raise ValueError(f"未知台阶 {step!r}，只允许 {LADDER_STEPS}")
        row = self._find_source(item_id)
        if row is None:
            raise ValueError(f"mark_ladder 需要已登记的来源条目：{item_id}")
        current = list(row.get("ladder") or [])
        if step in current:
            return current
        idx = LADDER_STEPS.index(step)
        max_idx = max((LADDER_STEPS.index(s) for s in current), default=-1)
        if idx != max_idx + 1:
            raise ValueError(f"台阶必须逐级前进：当前 {current}，不能直接记 {step}")
        updates: Dict[str, Any] = {"ladder": [step]}
        if step == "file_downloaded_verified":
            asset_id = str(evidence.get("asset_id") or "")
            if not asset_id or self.manifest.get_asset(asset_id) is None:
                raise ValueError("file_downloaded_verified 需要已落盘并登记的 asset_id（链接不得冒充已取得文件）")
            updates["acquisition_status"] = "acquired_verified"
        elif step == "body_parsed":
            updates["parse_status"] = "parsed"
            updates["acquisition_status"] = "parsed"
        self.manifest.set_status(item_id, **updates)
        return current + [step]

    def _set_status(self, item_id: str, acquisition_status: str) -> None:
        if self._find_source(item_id) is None:
            return
        self.manifest.set_status(item_id, acquisition_status=acquisition_status)

    # ── 容量 ────────────────────────────────────────────────────────────────
    def _usage_bytes(self) -> int:
        base = paths.subdir("originals/sha256", self._root)
        return sum(p.stat().st_size for p in base.rglob("*") if p.is_file())

    def _disk_free(self) -> int:
        return shutil.disk_usage(str(self._root)).free

    def _block_capacity(self, item_id: str, ref: str, share_id: str,
                        reason: str, need_bytes: Optional[int]) -> Dict[str, Any]:
        """容量阻塞：保留待办与大小，绝不改 done（方案 §6/§9）。"""
        self._set_status(item_id, "capacity_blocked")
        self.manifest.upsert_source(
            self.provider, item_id,
            pending_bytes="" if need_bytes is None else int(need_bytes),
            pending_reason=reason)
        return {"status": "capacity_blocked", "item_id": str(item_id), "url_ref": ref,
                "share_id": share_id, "reason": reason,
                "pending_bytes": need_bytes, "kept_todo": True,
                "transferred_bytes": self.last_transferred_bytes}

    # ── 探测与校验 ──────────────────────────────────────────────────────────
    def _probe(self, url: str) -> Dict[str, Any]:
        self.probes += 1
        resp = self._transport(url, method="GET", headers={"Range": "bytes=0-0"}, timeout=self.timeout)
        try:
            size = _to_int(resp.headers.get("content-length"))
            m = re.match(r"bytes\s+\d+-\d+/(\d+)", resp.headers.get("content-range") or "")
            if m:
                size = int(m.group(1))
            return {"status": int(resp.status), "size": size,
                    "etag": resp.headers.get("etag", ""),
                    "last_modified": resp.headers.get("last-modified", ""),
                    "accept_ranges": resp.headers.get("accept-ranges", "")}
        finally:
            resp.close()

    @staticmethod
    def _magic_kind(head: bytes) -> str:
        if head.startswith(parsers._OLE_MAGIC):
            return "ole"
        if head.startswith(parsers._PDF_MAGIC):
            return "pdf"
        if head.startswith(parsers._ZIP_MAGIC):
            return "zip"
        if any(head.startswith(m) for m in parsers._IMAGE_MAGIC):
            return "image"
        return ""

    @staticmethod
    def _expected_kinds(mime_hint: str, name: str) -> set:
        kinds = set()
        mt = (mime_hint or "").split(";")[0].strip().lower()
        if mt in parsers._MIME_KIND:
            kinds.add(parsers._MIME_KIND[mt])
        ext = Path(str(name or "")).suffix.lower()
        if ext in parsers._EXT_KIND:
            kinds.add(parsers._EXT_KIND[ext])
        return kinds

    def _check_signature(self, part: Path, name: str, mime_hint: str) -> Dict[str, Any]:
        """真实文件签名校验（区分 OLE/PDF/zip/Office），不看扩展名兜底。"""
        with part.open("rb") as f:
            head = f.read(16)
        magic = self._magic_kind(head)
        expected = self._expected_kinds(mime_hint, name)
        parsed_kind = parsers._detect_kind(Path(str(name or part.name)), head, mime_hint)
        if not expected:
            return {"ok": True, "magic": magic, "kind": parsed_kind, "expected": []}
        if not magic:
            if expected & _BINARY_KINDS:
                return {"ok": False, "magic": "none", "kind": parsed_kind,
                        "expected": sorted(expected),
                        "why": "声称二进制容器格式但内容无文件签名（内容与 mime/扩展名不符）"}
            return {"ok": True, "magic": "none", "kind": parsed_kind, "expected": sorted(expected)}
        if expected & _MAGIC_ACCEPTS.get(magic, frozenset()):
            return {"ok": True, "magic": magic, "kind": parsed_kind, "expected": sorted(expected)}
        return {"ok": False, "magic": magic, "kind": parsed_kind, "expected": sorted(expected),
                "why": f"文件签名 {magic} 与声明的格式 {sorted(expected)} 不符"}

    # ── 传输（含断点续传）───────────────────────────────────────────────────
    def _transfer(self, url: str, part: Path, meta: Optional[Dict[str, Any]], probe: Dict[str, Any],
                  resume: bool) -> Dict[str, Any]:
        offset = 0
        if resume and part.exists() and meta and not meta.get("invalid"):
            offset = part.stat().st_size
            if meta.get("etag") and probe.get("etag") and meta["etag"] != probe["etag"]:
                offset = 0
            elif (meta.get("last_modified") and probe.get("last_modified")
                  and meta["last_modified"] != probe["last_modified"]):
                offset = 0
        hasher = hashlib.sha256()
        if offset > 0:  # 续传：先对流式重算已下部分的哈希（仍不吃内存）
            with part.open("rb") as f:
                for buf in iter(lambda: f.read(CHUNK), b""):
                    hasher.update(buf)

        headers = {"Range": f"bytes={offset}-"} if offset else {}
        resp = self._transport(url, method="GET", headers=headers, timeout=self.timeout)
        self.last_transferred_bytes = 0
        self.downloads_started += 1
        try:
            status = int(resp.status)
            if status >= 400:
                raise _HttpStatus(status)
            if offset > 0 and status != 206:  # 服务端忽略 Range → 从头下
                offset = 0
                hasher = hashlib.sha256()
            mode = "ab" if offset else "wb"
            with part.open(mode) as out:
                while True:
                    buf = resp.read(CHUNK)
                    if not buf:
                        break
                    out.write(buf)
                    hasher.update(buf)
                    offset += len(buf)
                    self.transferred_bytes += len(buf)
                    self.last_transferred_bytes += len(buf)
        finally:
            resp.close()
        return {"bytes": offset, "sha256": hasher.hexdigest()}

    def _save_meta(self, meta_path: Path, ref: str, part: Path, probe: Dict[str, Any],
                   target_name: str, mime_hint: str, item_id: str, offset: int,
                   invalid: bool = False) -> None:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps({
            "url_ref": ref, "target_name": target_name, "mime_hint": mime_hint,
            "item_id": str(item_id), "offset": int(offset),
            "expected_size": probe.get("size"), "etag": probe.get("etag", ""),
            "last_modified": probe.get("last_modified", ""), "invalid": bool(invalid),
            "updated_at": time.time(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 下载主流程 ──────────────────────────────────────────────────────────
    def download(self, url_or_share: Union[str, Dict[str, Any]], *, item_id: str,
                 target_name: str = "", mime_hint: str = "", resume: bool = True,
                 expected_sha256: str = "", title: str = "", share_id: str = "",
                 share_file_id: str = "", exported_at: str = "") -> Dict[str, Any]:
        """下载单个条目并校验落盘；返回状态字典（status 见下）。

        status: verified / reused / pending / capacity_blocked /
                invalid_access_blocked / parse_failed
        """
        spec = dict(url_or_share) if isinstance(url_or_share, dict) else {"url": str(url_or_share or "")}
        url = str(spec.get("url") or spec.get("downloadUrl") or "")
        if not url:
            raise ValueError("download 需要 url 或含 url 的分享描述")
        item_id = str(item_id)
        share_id = str(share_id or spec.get("share_id") or "") or _sha1(redact(url))[:16]
        share_file_id = str(share_file_id or spec.get("share_file_id") or "")
        target_name = str(target_name or spec.get("name")
                          or Path(urllib.parse.urlparse(url).path).name or share_file_id or "download")
        ref = redact(url)                      # 受控引用：凭据一律脱敏后入库
        self.last_transferred_bytes = 0
        self.staging.mkdir(parents=True, exist_ok=True)
        part = self.staging / f"{_slug(share_id, 32)}.{_slug(target_name, 40)}.part"
        meta_path = self.staging / f"{_slug(share_id, 32)}.{_slug(target_name, 40)}.meta.json"
        meta = json.loads(meta_path.read_text("utf-8")) if meta_path.exists() else None
        if not resume:
            part.unlink(missing_ok=True)
            meta = None

        fields: Dict[str, Any] = {"netdisk_provider": detect_provider(url), "link_ref": ref,
                                 "raw": {"url_ref": ref, "target_name": target_name}}
        if title:
            fields["title"] = title
        if exported_at:
            fields["exported_at"] = exported_at
        self.manifest.upsert_source(self.provider, item_id, **fields)
        self.mark_ladder(item_id, "link_exported")

        # 幂等：同条目已校验完成 → 复用，不重下
        row = self._find_source(item_id) or {}
        ladder = list(row.get("ladder") or [])
        mapped = [mp for mp in self.manifest.list_mappings(item_id)
                  if mp["share_id"] == share_id and mp["asset_id"]]
        if "file_downloaded_verified" in ladder and mapped:
            asset_id = str(mapped[-1]["asset_id"])
            return {"status": "reused", "item_id": item_id, "url_ref": ref, "share_id": share_id,
                    "asset_id": asset_id, "reason": "already_verified",
                    "transferred_bytes": 0}

        # 同一分享已下载过 → 复用资产，只补映射（重复分享只遍历一次）
        known = self.registry.asset_for(share_id)
        if known and "file_downloaded_verified" not in ladder:
            self.manifest.link_asset(item_id, known, share_id=share_id, share_file_id=share_file_id)
            self.mark_ladder(item_id, "share_accessible")
            self.mark_ladder(item_id, "file_downloaded_verified", asset_id=known)
            return {"status": "reused", "item_id": item_id, "url_ref": ref, "share_id": share_id,
                    "asset_id": known, "reason": "shared_share_reused",
                    "transferred_bytes": 0}

        with self._sem:
            # D 盘闲置守卫（§6：始终保留 ≥15GiB）
            if self._disk_free() < self.disk_free_guard_bytes:
                return self._block_capacity(item_id, ref, share_id, "disk_free_below_guard", None)
            try:
                probe = self._probe(url)
            except Exception as ex:  # 网络不可达/中断 → 保留待办，可续传
                self._set_status(item_id, "pending")
                return {"status": "pending", "item_id": item_id, "url_ref": ref, "share_id": share_id,
                        "reason": f"probe_failed: {type(ex).__name__}", "resumable": True,
                        "transferred_bytes": 0}
            if probe["status"] >= 400:
                return self._fail(item_id, ref, share_id, "invalid_access_blocked",
                                  f"http_{probe['status']}")

            self.mark_ladder(item_id, "share_accessible")
            size = probe["size"]
            if size is not None and size > self.max_bytes:
                return self._block_capacity(item_id, ref, share_id, "single_file_over_limit", size)
            if size is not None and self._usage_bytes() + size > self.total_budget_bytes:
                return self._block_capacity(item_id, ref, share_id, "total_budget_exceeded", size)
            if self._disk_free() < self.disk_free_guard_bytes:
                return self._block_capacity(item_id, ref, share_id, "disk_free_below_guard", size)

            self._save_meta(meta_path, ref, part, probe, target_name, mime_hint, item_id, 0)
            try:
                got = self._transfer(url, part, meta, probe, resume)
            except _HttpStatus as ex:
                return self._fail(item_id, ref, share_id, "invalid_access_blocked", f"http_{ex.status}")
            except Exception as ex:  # 传输中断：保留 .part 与 meta 供续传
                offset = part.stat().st_size if part.exists() else 0
                self._save_meta(meta_path, ref, part, probe, target_name, mime_hint, item_id, offset)
                self._set_status(item_id, "pending")
                return {"status": "pending", "item_id": item_id, "url_ref": ref, "share_id": share_id,
                        "reason": f"transfer_interrupted: {type(ex).__name__}",
                        "bytes_received": offset, "resumable": True,
                        "transferred_bytes": self.last_transferred_bytes}

            total, sha = got["bytes"], got["sha256"]
            if size is not None and total != size:
                return self._invalid(item_id, ref, share_id, part, meta_path,
                                     f"size_mismatch: {total}!={size}", probe, total)
            sig = self._check_signature(part, target_name, mime_hint)
            if not sig["ok"]:
                return self._invalid(item_id, ref, share_id, part, meta_path,
                                     f"signature_mismatch: {sig['why']}", probe, total, sig)
            if expected_sha256 and sha.lower() != str(expected_sha256).lower():
                return self._invalid(item_id, ref, share_id, part, meta_path,
                                     f"sha256_mismatch: {sha} != {expected_sha256}", probe, total)

            stored = save_original(part, mime=mime_hint or (mimetypes.guess_type(target_name)[0] or ""),
                                   manifest=self.manifest, root=self._root)
            asset_id = stored["asset_id"]
            self.manifest.link_asset(item_id, asset_id, share_id=share_id, share_file_id=share_file_id)
            self.registry.record(share_id, asset_id, share_file_id)
            self.mark_ladder(item_id, "file_downloaded_verified", asset_id=asset_id)
            part.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            return {"status": "verified", "item_id": item_id, "url_ref": ref, "share_id": share_id,
                    "asset_id": asset_id, "sha256": sha, "bytes": total,
                    "path": stored["path"], "format_kind": sig["kind"], "deduped": stored["deduped"],
                    "transferred_bytes": self.last_transferred_bytes}

    def _fail(self, item_id: str, ref: str, share_id: str, status: str, reason: str) -> Dict[str, Any]:
        """失败如实落状态，不记 verified。"""
        self._set_status(item_id, status)
        return {"status": status, "item_id": item_id, "url_ref": ref, "share_id": share_id,
                "reason": reason, "transferred_bytes": self.last_transferred_bytes}

    def _invalid(self, item_id: str, ref: str, share_id: str, part: Path, meta_path: Path,
                 reason: str, probe: Dict[str, Any], total: int, sig: Optional[Dict] = None) -> Dict[str, Any]:
        """校验不一致：删掉可疑 .part（避免下次续传污染），状态记为 parse_failed。"""
        self._set_status(item_id, "parse_failed")
        self.manifest.set_status(item_id, parse_status="unparseable")
        self._save_meta(meta_path, ref, part, probe, "", "", item_id, total, invalid=True)
        part.unlink(missing_ok=True)
        return {"status": "parse_failed", "item_id": item_id, "url_ref": ref, "share_id": share_id,
                "reason": reason, "bytes": total, "signature": sig,
                "transferred_bytes": self.last_transferred_bytes}

    def download_batch(self, items: Sequence[Dict[str, Any]], *,
                       max_parallel: Optional[int] = None) -> List[Dict[str, Any]]:
        """分批下载（§6：初始并行 2）。items 为 download() 的 kwargs 字典序列。"""
        workers = max(1, int(max_parallel or self.max_parallel))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(lambda kw: self.download(**kw), dict(kw)) for kw in items]
            return [f.result() for f in futures]
