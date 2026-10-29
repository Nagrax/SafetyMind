"""K05 统一解析入口：Office / PDF / 压缩包 / 文本 → 完整段落、页、表格、定位与质量分级。

设计依据：docs/资料库建设决策与Agent实施方案_2026-10-19.md §5.1（解析而非直接扁平化）。

约定：
  - 统一入口 `parse_file(path, *, mime="", staging_dir=None) -> ParseResult`；
  - **完整正文**入 blocks，不做"截前 6000 字"式截断；定位随块（page/sheet/slide/para/row）；
  - 质量分级用 `status` + `quality` 双字段表达，低质量不得进入可靠证据集合；
  - **扫描件 OCR**：只对"该页确实无文字层"的 PDF 页与 image_scan 图片触发，产出 kind=text
    的文字块并保留页码定位；OCR 失败只影响该页并记 errors，不把整份文档判成 unparseable；
    引擎缺失（可选增强）如实降级为待 OCR，不抛错、不硬装系统二进制；
  - **旧 PPT 受控转换**：OLE 签名识别后走 `PptConverter` 抽象（LibreOffice headless），
    成功则继续解析为块并记录原件 sha256 / 工具名与版本 / 转换前后页数对照 / 输出路径；
    无转换工具或转换失败时保持 `pending_conversion` 并记明原因，绝不静默当成功；
  - **重库惰性导入**：解析库（python-docx / openpyxl / python-pptx / pypdf）只在工具链
    venv（D:/SafetyMindData/venv）里，缺失时抛清晰的 RuntimeError，不静默降级。
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ── 状态与质量枚举 ────────────────────────────────────────────────────────────
STATUS_PARSED = "parsed"
STATUS_LOW_QUALITY = "low_quality"
STATUS_UNPARSEABLE = "unparseable"
STATUS_UNSUPPORTED = "unsupported"
STATUS_PENDING_CONVERSION = "pending_conversion"

QUALITY_OK = "ok"
QUALITY_LOW = "low_quality"
QUALITY_UNPARSEABLE = "unparseable"
QUALITY_UNSUPPORTED = "unsupported"
QUALITY_PENDING = "pending"  # pending_conversion 的显式质量标记（尚未解析，不入证据集合）

# 质量阈值（可调；低质量一律不进"可靠证据集合"）
MIN_TEXT_CHARS = 30        # 正文过短阈值
MAX_GARBLE_RATIO = 0.10    # 乱码（替换符/控制符）占比阈值
MAX_SCAN_PAGE_RATIO = 0.5  # 需要 OCR 的页占比阈值

# OCR 阈值与渲染参数（OCR 结果仍按 ok / low_quality 分级）
OCR_RENDER_DPI = int(os.getenv("SAFETYMIND_OCR_DPI", "200"))  # PDF 页栅格化 DPI
MIN_OCR_PAGE_CHARS = 4     # 单页 OCR 字符数下限；低于此视为识别内容可疑

# 压缩包受控体积上限（解压后总量），可由环境变量覆盖
MAX_ZIP_UNCOMPRESSED = 512 * 1024 * 1024
ZIP_MEMBER_LIST_MAX = 2000  # 成员清单最多列出多少项（避免超大包把清单本身撑爆）

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_PDF_MAGIC = b"%PDF"
_ZIP_MAGIC = b"PK\x03\x04"
_IMAGE_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a",
                b"BM", b"RIFF", b"II*\x00", b"MM\x00*")

_EXT_KIND = {
    ".docx": "docx", ".xlsx": "xlsx", ".xlsm": "xlsx", ".pptx": "pptx", ".ppt": "ppt_legacy",
    ".pdf": "pdf", ".zip": "zip",
    ".txt": "txt", ".md": "md", ".markdown": "md",
    ".json": "json", ".csv": "csv",
    ".png": "image_scan", ".jpg": "image_scan", ".jpeg": "image_scan", ".gif": "image_scan",
    ".bmp": "image_scan", ".webp": "image_scan", ".tif": "image_scan", ".tiff": "image_scan",
}

_MIME_KIND = {
    "application/pdf": "pdf",
    "application/zip": "zip",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt_legacy",
    "text/plain": "txt", "text/markdown": "md", "application/json": "json", "text/csv": "csv",
}

_SIGNATURE_BLOCKERS = ("签字", "签名", "审核人", "审核", "批准", "监护人", "作业人", "验收人", "负责人")

# 解析工具链依赖：模块名 → (发布包名, 能力说明)
_DEPS = {
    "docx": ("docx", "python-docx", "DOCX 段落/表格解析"),
    "xlsx": ("openpyxl", "openpyxl", "XLSX sheet/行列/公式解析"),
    "pptx": ("pptx", "python-pptx", "PPTX 幻灯片/注释解析"),
    "pdf": ("pypdf", "pypdf", "PDF 逐页文字提取"),
}


@dataclass
class Block:
    """可供定位引用的正文单元（段落/表格/注释/标题）。"""
    content: str
    locator: Dict[str, Any]
    kind: str = "text"           # text / table / notes / heading / manifest / image
    detail: Optional[Dict[str, Any]] = None  # 表头、合并单元格、签字栏、needs_ocr 等


@dataclass
class ParseResult:
    """单份文件的解析结果（完整正文 + 定位 + 质量分级 + 工具版本）。"""
    status: str
    kind: str
    blocks: List[Block] = field(default_factory=list)
    pages_or_sheets: int = 0
    parser_name: str = ""
    parser_version: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    bytes: int = 0
    sha256: str = ""
    quality: str = ""
    notes: str = ""
    # 旧 PPT 受控转换记录（原件 sha256 / 工具名与版本 / 转换前后页数对照 / 输出路径 / 失败原因）
    conversion: Optional[Dict[str, Any]] = None

    def is_reliable_evidence(self) -> bool:
        """是否可进入"可靠证据集合"：只有 status=parsed 且 quality=ok 才可。"""
        return self.status == STATUS_PARSED and self.quality == QUALITY_OK


# ── 依赖与工具版本 ────────────────────────────────────────────────────────────

def _require(kind: str) -> None:
    """惰性导入重库：缺失时抛清晰 RuntimeError（提示改用解析工具链 venv）。"""
    spec = _DEPS.get(kind)
    if not spec:
        return
    module, package, feature = spec
    try:
        importlib.import_module(module)
    except ImportError as ex:  # pragma: no cover - 取决于运行环境
        raise RuntimeError(
            f"需要解析工具链 venv: {feature} 依赖 {package} 未安装（{ex}）；"
            f"请在 D:/SafetyMindData/venv/Scripts/python.exe 下运行解析。") from ex


def _tool_version(module_name: str, dist_name: str) -> str:
    """解析工具版本（优先发行版元数据，回退模块 __version__）。"""
    try:
        return importlib.metadata.version(dist_name)
    except Exception:
        try:
            mod = importlib.import_module(module_name)
            return str(getattr(mod, "__version__", "unknown"))
        except Exception:
            return "unknown"


# ── 基础工具 ──────────────────────────────────────────────────────────────────

def _read_head_and_hash(path: Path, head_len: int = 16) -> tuple[bytes, str, int]:
    """流式计算 sha256 与字节数，只读文件头 —— 大文件不整份进内存（方案 §6 容量约束）。"""
    h = hashlib.sha256()
    total = 0
    head = b""
    with path.open("rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            if len(head) < head_len:
                head = (head + buf)[:head_len]
            total += len(buf)
            h.update(buf)
    return head, h.hexdigest(), total


def _decode_text(data: bytes) -> tuple[str, str]:
    """文本解码：utf-8 → utf-8-sig → gbk → latin-1 回退（记录实际编码）。"""
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1/replace"


def _garble_ratio(blocks: List[Block]) -> float:
    """乱码率：U+FFFD 与控制字符（保留 \\n \\r \\t）占正文比例。"""
    text = "".join(b.content for b in blocks)
    if not text:
        return 0.0
    bad = sum(1 for ch in text if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\n\r\t"))
    return bad / len(text)


def _text_of(blocks: List[Block]) -> str:
    return "".join(b.content for b in blocks if b.content)


def _zip_members(path: Path) -> List[tuple[str, int]]:
    with zipfile.ZipFile(path) as z:
        return [(i.filename, i.file_size) for i in z.infolist()]


def _detect_kind(path: Path, head: bytes, mime: str = "") -> str:
    """按扩展名 + 真实文件签名识别格式（旧 PPT 以 OLE 签名判定）。"""
    ext = path.suffix.lower()
    if head.startswith(_OLE_MAGIC):
        # OLE 复合文档：旧 Office 二进制。旧 PPT 明确识别为待转换。
        return "ppt_legacy" if ext == ".ppt" else "other"
    if head.startswith(_PDF_MAGIC):
        return "pdf"
    if head.startswith(_ZIP_MAGIC):
        try:
            names = {n for n, _ in _zip_members(path)}
        except Exception:
            names = set()
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        if ext in _EXT_KIND and _EXT_KIND[ext] in ("docx", "xlsx", "pptx"):
            # 扩展名声称 Office 但容器缺标记 → 交给对应解析器，失败即 unparseable
            return _EXT_KIND[ext]
        return "zip"
    if any(head.startswith(m) for m in _IMAGE_MAGIC):
        return "image_scan"
    if ext in _EXT_KIND:
        return _EXT_KIND[ext]
    mt = (mime or "").split(";")[0].strip().lower()
    return _MIME_KIND.get(mt, "other")


# ── 质量判定 ──────────────────────────────────────────────────────────────────

def _assess(blocks: List[Block], *, scan_pages: int, total_pages: int,
            force_status: Optional[str] = None) -> tuple[str, str, List[str]]:
    """返回 (status, quality, warnings)。低质量的三种来源：扫描页占比高、正文过短、乱码率高。"""
    warnings: List[str] = []
    if force_status is not None:
        return force_status, QUALITY_LOW, warnings
    text = _text_of(blocks)
    scan_ratio = (scan_pages / total_pages) if total_pages else 0.0
    if total_pages and scan_ratio >= MAX_SCAN_PAGE_RATIO and not text.strip():
        warnings.append(f"{scan_pages}/{total_pages} 页无文字层（needs_ocr），不得当解析成功")
        return STATUS_LOW_QUALITY, QUALITY_LOW, warnings
    if len(text.strip()) < MIN_TEXT_CHARS:
        warnings.append(f"正文过短（{len(text.strip())} < {MIN_TEXT_CHARS} 字符）")
        return STATUS_LOW_QUALITY, QUALITY_LOW, warnings
    gr = _garble_ratio(blocks)
    if gr > MAX_GARBLE_RATIO:
        warnings.append(f"乱码率 {gr:.2%} > {MAX_GARBLE_RATIO:.0%}")
        return STATUS_LOW_QUALITY, QUALITY_LOW, warnings
    if total_pages and scan_ratio >= MAX_SCAN_PAGE_RATIO:
        warnings.append(f"{scan_pages}/{total_pages} 页无文字层（needs_ocr）")
        return STATUS_LOW_QUALITY, QUALITY_LOW, warnings
    return STATUS_PARSED, QUALITY_OK, warnings


# ── DOCX ──────────────────────────────────────────────────────────────────────

def _docx_para_kind(paragraph) -> str:
    try:
        name = (paragraph.style.name or "") if paragraph.style is not None else ""
    except Exception:
        name = ""
    if name.startswith(("Heading", "标题")):
        return "heading"
    return "text"


def _parse_docx(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    blocks: List[Block] = []
    errors: List[str] = []
    warnings: List[str] = []
    para_idx = 0
    table_idx = 0

    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            text = (p.text or "").strip()
            locator = {"para": para_idx}
            para_idx += 1
            if text:
                blocks.append(Block(content=text, locator=locator, kind=_docx_para_kind(p)))
        elif tag == "tbl":
            t = Table(child, doc)
            rows_out: List[str] = []
            merged: List[str] = []
            for ri, row in enumerate(t.rows):
                cells_text: List[str] = []
                cells = list(row.cells)
                for ci, cell in enumerate(cells):
                    # 横向合并：同一 _tc 的重复单元格只保留一次（保留合并关系）
                    if ci > 0 and cells[ci]._tc is cells[ci - 1]._tc:
                        merged.append(f"r{ri + 1}c{ci + 1}")
                        continue
                    cells_text.append((cell.text or "").strip())
                rows_out.append(" | ".join(cells_text))
            headers = rows_out[0].split(" | ") if rows_out else []
            joined = "\n".join(rows_out)
            blocks.append(Block(
                content=joined,
                locator={"table": table_idx},
                kind="table",
                detail={
                    "headers": headers,
                    "rows": len(rows_out),
                    "merged_cells": merged,
                    "signature_block": any(kw in joined for kw in _SIGNATURE_BLOCKERS),
                },
            ))
            table_idx += 1
        else:
            continue

    status, quality, warns = _assess(blocks, scan_pages=0, total_pages=0)
    warnings.extend(warns)
    return ParseResult(
        status=status, kind="docx", blocks=blocks, pages_or_sheets=0,
        parser_name="python-docx", parser_version=_tool_version("docx", "python-docx"),
        errors=errors, warnings=warnings, quality=quality,
        notes=f"段落 {para_idx} 个、表格 {table_idx} 个",
    )


# ── XLSX ──────────────────────────────────────────────────────────────────────

_XY_UNIT_HINTS = ("℃", "MPa", "kPa", "Pa", "kg", "m³", "m3", "mm", "cm", "m²", "m2", "%", "（", "(")


def _xlsx_merged_ranges(path: Path, ws) -> List[str]:
    """read_only 模式没有 ws.merged_cells，从 sheet XML 里流式抽取 mergeCell ref。"""
    member = getattr(ws, "_worksheet_path", None)
    if not member:
        return []
    try:
        import xml.etree.ElementTree as ET
        from io import BytesIO
        with zipfile.ZipFile(path) as z:
            if member not in z.namelist():
                return []
            data = z.read(member)
        refs: List[str] = []
        for _event, el in ET.iterparse(BytesIO(data), events=("end",)):
            if el.tag.rsplit("}", 1)[-1] == "mergeCell":
                ref = el.get("ref")
                if ref:
                    refs.append(ref)
                el.clear()
        return refs
    except Exception:
        return []


def _detect_units(header: List[str]) -> List[str]:
    return [h for h in header if any(hint in (h or "") for hint in _XY_UNIT_HINTS)]


def _parse_xlsx(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=False)  # 只读控内存（方案 §5.1）
    blocks: List[Block] = []
    warnings: List[str] = []
    errors: List[str] = []
    sheet_count = len(wb.worksheets)
    try:
        for si, ws in enumerate(wb.worksheets):
            rows: List[List[str]] = []
            formulas: List[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = ["" if v is None else str(v) for v in row]
                formulas.extend(c for c in cells if c.startswith("="))
                while cells and not cells[-1].strip():
                    cells.pop()
                if any(c.strip() for c in cells):
                    rows.append(cells)
            if not rows:
                warnings.append(f"sheet {ws.title!r} 无有效单元格")
                continue
            header = rows[0]
            merged = _xlsx_merged_ranges(path, ws)
            content = "\n".join(" | ".join(r) for r in rows)
            blocks.append(Block(
                content=content,
                locator={"sheet": ws.title, "sheet_index": si, "header_row": 1},
                kind="table",
                detail={
                    "headers": header,
                    "units": _detect_units(header),
                    "merged_cells": merged,
                    "rows": len(rows),
                    "formulas": formulas,
                },
            ))
    finally:
        try:
            wb.close()
        except Exception:
            pass

    status, quality, warns = _assess(blocks, scan_pages=0, total_pages=0)
    warnings.extend(warns)
    return ParseResult(
        status=status, kind="xlsx", blocks=blocks, pages_or_sheets=sheet_count,
        parser_name="openpyxl", parser_version=_tool_version("openpyxl", "openpyxl"),
        errors=errors, warnings=warnings, quality=quality,
        notes="read_only 模式读取；公式以文本保留（data_only=False）",
    )


# ── PPTX ──────────────────────────────────────────────────────────────────────

def _parse_pptx(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    from pptx import Presentation

    prs = Presentation(str(path))
    blocks: List[Block] = []
    warnings: List[str] = []
    for idx, slide in enumerate(prs.slides, start=1):
        texts: List[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [" | ".join((c.text or "").strip() for c in row.cells) for row in shape.table.rows]
                joined = "\n".join(rows)
                if joined.strip():
                    texts.append(joined)
            elif getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                txt = (shape.text_frame.text or "").strip()
                if txt:
                    texts.append(txt)
        content = "\n".join(texts)
        if content.strip():
            blocks.append(Block(content=content, locator={"slide": idx}, kind="text"))
        notes = ""
        try:
            if slide.has_notes_slide:
                notes = (slide.notes_slide.notes_text_frame.text or "").strip()
        except Exception as ex:  # pragma: no cover - 备注读取失败不影响正文
            warnings.append(f"第 {idx} 页备注读取失败: {ex}")
        if notes:
            blocks.append(Block(content=notes, locator={"slide": idx}, kind="notes",
                                detail={"source": "notes"}))
    status, quality, warns = _assess(blocks, scan_pages=0, total_pages=0)
    warnings.extend(warns)
    return ParseResult(
        status=status, kind="pptx", blocks=blocks, pages_or_sheets=len(prs.slides),
        parser_name="python-pptx", parser_version=_tool_version("pptx", "python-pptx"),
        errors=[], warnings=warnings, quality=quality,
        notes="保留幻灯片页码、页内文字与注释",
    )


# ── OCR（扫描页/图片；仅对"确实无文字层"的目标触发） ──────────────────────────

class OcrEngine:
    """OCR 引擎抽象：把一张 PIL 图像识别成文本行。失败由调用方按页兜底。"""
    engine_name = "unknown"
    engine_version = "unknown"
    lang = "unknown"

    def recognize(self, image: Any) -> List[str]:  # pragma: no cover - 抽象基类
        raise NotImplementedError


class RapidOcrEngine(OcrEngine):
    """rapidocr-onnxruntime：纯 pip 安装（自带 PP-OCR 模型，无需系统二进制）。

    模型体积随包落在**工具链 venv（D 盘）**内（ch_PP-OCRv4_det/rec + cls），运行期不下载，
    因此不产生 C 盘缓存；安装时 pip 缓存也已用 `--cache-dir D:/pipcache` 固定到 D 盘。
    """
    engine_name = "rapidocr-onnxruntime"
    lang = "ch"

    def __init__(self) -> None:
        import numpy as np
        from rapidocr_onnxruntime import RapidOCR
        self._np = np
        self._engine = RapidOCR()
        self.engine_version = _tool_version("rapidocr_onnxruntime", "rapidocr-onnxruntime")

    def recognize(self, image: Any) -> List[str]:
        result, _elapse = self._engine(self._np.asarray(image.convert("RGB")))
        lines: List[str] = []
        for item in result or []:
            if len(item) > 1 and str(item[1]).strip():
                lines.append(str(item[1]).strip())
        return lines


class PytesseractEngine(OcrEngine):
    """pytesseract：仅在 rapidocr 不可用且**系统已存在** tesseract 时启用（不装系统二进制）。"""
    engine_name = "pytesseract"
    lang = os.getenv("SAFETYMIND_OCR_LANG", "chi_sim")

    def __init__(self) -> None:
        import pytesseract
        exe = shutil.which("tesseract")
        if not exe:
            raise RuntimeError("系统缺 tesseract 可执行文件（不自动安装系统二进制）")
        self._pt = pytesseract
        self._exe = exe
        self.engine_version = _tool_version("pytesseract", "pytesseract")

    def recognize(self, image: Any) -> List[str]:
        text = self._pt.image_to_string(image, lang=self.lang)
        return [ln.strip() for ln in text.splitlines() if ln.strip()]


# 引擎探测结果缓存：首次调用才导入重库（保持惰性导入纪律），进程内复用同一引擎
_OCR_ENGINE_CACHE: Dict[str, Any] = {"done": False, "engine": None, "reason": ""}


def _create_ocr_engine() -> Tuple[Optional[OcrEngine], str]:
    """OCR 引擎工厂（测试可替换本函数以模拟引擎缺失或注入假引擎）。

    优先级：rapidocr-onnxruntime（纯 pip）→ pytesseract（需系统 tesseract）。都不可用返回 None。
    """
    rapid_reason = ""
    try:
        return RapidOcrEngine(), ""
    except Exception as ex:  # noqa: BLE001 - 缺依赖/模型加载失败都走降级
        rapid_reason = f"rapidocr-onnxruntime 不可用（{type(ex).__name__}: {ex}）"
    if shutil.which("tesseract"):
        try:
            return PytesseractEngine(), rapid_reason
        except Exception as ex:  # noqa: BLE001
            return None, f"{rapid_reason}；pytesseract 不可用（{type(ex).__name__}: {ex}）"
    return None, f"{rapid_reason}；系统无 tesseract 可执行文件"


def _resolve_ocr_engine() -> Tuple[Optional[OcrEngine], str]:
    """解析并缓存 OCR 引擎；返回 (engine, 不可用原因)。惰性导入，缺失不抛错。"""
    if not _OCR_ENGINE_CACHE["done"]:
        engine, reason = _create_ocr_engine()
        _OCR_ENGINE_CACHE.update(done=True, engine=engine, reason=reason)
    return _OCR_ENGINE_CACHE["engine"], _OCR_ENGINE_CACHE["reason"]  # type: ignore[return-value]


def reset_ocr_engine_cache() -> None:
    """清空 OCR 引擎缓存（测试模拟引擎缺失/切换引擎时使用；长驻进程换引擎也可调用）。"""
    _OCR_ENGINE_CACHE.update(done=False, engine=None, reason="")


def _ocr_detail(engine: OcrEngine, *, dpi: Optional[int] = None) -> Dict[str, Any]:
    """OCR 结果块的 detail.ocr（引擎名/版本/语言；PDF 页另记渲染 DPI）。"""
    detail: Dict[str, Any] = {"engine": engine.engine_name, "version": engine.engine_version,
                              "lang": engine.lang}
    if dpi:
        detail["render_dpi"] = dpi
    return detail


def _downgrade_if_weak_ocr(status: str, quality: str, warnings: List[str],
                           weak_pages: int, total_pages: int) -> Tuple[str, str]:
    """OCR 识别字符数过少的页占比过高时，把 ok 降级为 low_quality（不得混入可靠证据集合）。"""
    if not weak_pages or not total_pages:
        return status, quality
    if status == STATUS_PARSED and weak_pages / total_pages >= MAX_SCAN_PAGE_RATIO:
        warnings.append(f"{weak_pages}/{total_pages} 页 OCR 识别字符数 < {MIN_OCR_PAGE_CHARS}，降级为低质量")
        return STATUS_LOW_QUALITY, QUALITY_LOW
    return status, quality


def _pdf_page_images(path: Path, page: Any, page_index: int) -> List[Any]:
    """把 PDF 页转成可供 OCR 的 PIL 图像列表（空列表 = 该页无可用位图）。

    优先 pypdfium2 栅格化（覆盖 CCITT G4 / 矢量扫描 / 多图页）；缺失或失败时退回抽取页内嵌位图。
    """
    try:
        import pypdfium2 as pdfium
    except ImportError:
        pdfium = None
    if pdfium is not None:
        try:
            doc = pdfium.PdfDocument(str(path))
            try:
                return [doc[page_index - 1].render(scale=OCR_RENDER_DPI / 72).to_pil()]
            finally:
                doc.close()
        except Exception:  # noqa: BLE001 - 渲染失败退回内嵌位图
            pass
    try:
        from io import BytesIO
        from PIL import Image
    except ImportError:
        return []
    images: List[Any] = []
    try:
        for img in getattr(page, "images", []) or []:
            try:
                images.append(Image.open(BytesIO(img.data)).convert("RGB"))
            except Exception:  # noqa: BLE001 - 单个位图解码失败不影响该页其他位图
                continue
    except Exception:  # noqa: BLE001
        return []
    return images


def _ocr_page(engine: OcrEngine, images: List[Any]) -> Tuple[str, str]:
    """对一页的位图做 OCR，返回 (拼接文本, 错误说明)；单页失败只记错误，不牵连整份文档。"""
    lines: List[str] = []
    err = ""
    for image in images:
        try:
            lines.extend(engine.recognize(image))
        except Exception as ex:  # noqa: BLE001 - 单页/单图失败只影响本页
            err = f"{type(ex).__name__}: {ex}"
    return "\n".join(lines).strip(), err


# ── PDF（逐页） ───────────────────────────────────────────────────────────────

def _parse_pdf(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    errors: List[str] = []
    warnings: List[str] = []
    page_texts: List[str] = []
    for i, page in enumerate(reader.pages, start=1):
        try:
            page_texts.append((page.extract_text() or "").strip())
        except Exception as ex:  # pragma: no cover - 单页失败只记错误，不整体崩
            errors.append(f"第 {i} 页文字提取失败: {ex}")
            page_texts.append("")
    total = len(reader.pages)

    # OCR 只对"该页确实无文字层"触发；有文字层的页绝不重复 OCR
    blank_indexes = [i for i, t in enumerate(page_texts, start=1) if not t]
    engine: Optional[OcrEngine] = None
    engine_reason = ""
    if blank_indexes:
        engine, engine_reason = _resolve_ocr_engine()
        if engine is None:
            warnings.append(f"{len(blank_indexes)}/{total} 页无文字层；OCR 引擎不可用（{engine_reason}）")

    blocks: List[Block] = []
    scan_pages = ocr_pages = weak_pages = 0
    for i, text in enumerate(page_texts, start=1):
        if text:
            blocks.append(Block(content=text, locator={"page": i}, kind="text"))
            continue
        ocr_text, ocr_err = "", ""
        if engine is not None:
            ocr_text, ocr_err = _ocr_page(engine, _pdf_page_images(path, reader.pages[i - 1], i))
        if ocr_text:
            blocks.append(Block(content=ocr_text, locator={"page": i}, kind="text",
                                detail={"ocr": _ocr_detail(engine, dpi=OCR_RENDER_DPI)}))
            ocr_pages += 1
            if len(ocr_text) < MIN_OCR_PAGE_CHARS:
                weak_pages += 1
            continue
        detail: Dict[str, Any] = {"needs_ocr": True}
        if engine is None:
            detail["ocr_unavailable"] = engine_reason
        elif ocr_err:
            detail["ocr_error"] = ocr_err
            errors.append(f"第 {i} 页 OCR 失败: {ocr_err}")
        blocks.append(Block(content="", locator={"page": i}, kind="text", detail=detail))
        scan_pages += 1

    status, quality, warns = _assess(blocks, scan_pages=scan_pages, total_pages=total)
    warnings.extend(warns)
    status, quality = _downgrade_if_weak_ocr(status, quality, warnings, weak_pages, total)

    if ocr_pages:
        notes = (f"逐页提取；{total} 页；{ocr_pages} 页无文字层已经 {engine.engine_name} OCR 补齐"
                 + (f"；仍 {scan_pages}/{total} 页无文字层需 OCR" if scan_pages else ""))
    elif scan_pages:
        notes = f"逐页提取；{scan_pages}/{total} 页无文字层需 OCR"
    else:
        notes = f"逐页提取；{total} 页均有文字层"
    return ParseResult(
        status=status, kind="pdf", blocks=blocks, pages_or_sheets=total,
        parser_name="pypdf", parser_version=_tool_version("pypdf", "pypdf"),
        errors=errors, warnings=warnings, quality=quality, notes=notes,
    )


# ── 旧 PPT（OLE）受控转换 ─────────────────────────────────────────────────────

PPT_CONVERT_TIMEOUT_S = float(os.getenv("SAFETYMIND_PPT_TIMEOUT_S", "180"))

_SOFFICE_CANDIDATES = (
    "C:/Program Files/LibreOffice/program/soffice.exe",
    "C:/Program Files (x86)/LibreOffice/program/soffice.exe",
    "D:/Program Files/LibreOffice/program/soffice.exe",
    "D:/LibreOffice/program/soffice.exe",
)


def _probe_soffice() -> Optional[str]:
    """探测 LibreOffice headless 可执行文件（PATH 优先，再查常见安装路径）；无则 None。

    **只探测不下载**：体积大且属系统级变更，缺工具就如实登记待转换（不硬装）。
    """
    found = shutil.which("soffice") or shutil.which("soffice.exe") or shutil.which("soffice.com")
    if found:
        return found
    for candidate in _SOFFICE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    return None


def _pe_file_version(exe: str) -> str:
    """读 Windows PE 可执行文件的 FileVersion。

    soffice.exe 是 GUI 子系统程序，`--version` 无 stdout（实测输出为空），故直接读版本资源；
    非 Windows 或读取失败返回 ""（不抛出）。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes

        size = ctypes.windll.version.GetFileVersionInfoSizeW(exe, None)
        if not size:
            return ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(exe, 0, size, buf):
            return ""
        res_ptr = ctypes.c_void_p()
        res_len = ctypes.c_uint()
        if not ctypes.windll.version.VerQueryValueW(buf, "\\", ctypes.byref(res_ptr),
                                                    ctypes.byref(res_len)):
            return ""

        class _FixedFileInfo(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint32) for name in
                        ("sig", "struc_ver", "file_ms", "file_ls", "prod_ms", "prod_ls",
                         "flags_mask", "flags", "os", "type", "subtype", "date_ms", "date_ls")]

        info = ctypes.cast(res_ptr, ctypes.POINTER(_FixedFileInfo)).contents
        return "%d.%d.%d.%d" % (info.file_ms >> 16, info.file_ms & 0xFFFF,
                                info.file_ls >> 16, info.file_ls & 0xFFFF)
    except Exception:  # noqa: BLE001 - 版本读取失败不阻塞转换
        return ""


def _default_runner(cmd: List[str], timeout: float):
    """默认 runner：subprocess.run，捕获输出并设超时（受控转换不得无限等待）。"""
    return subprocess.run(cmd, timeout=timeout, capture_output=True)  # noqa: S603


def _default_lo_profile() -> Path:
    """LibreOffice 用户配置目录固定在 D 盘（铁律 7：不得写 C 盘）。"""
    return Path(os.getenv("SAFETYMIND_LO_PROFILE", "D:/SafetyMindData/lo_profile"))


def _tail(raw: Any, limit: int = 200) -> str:
    """把 runner 捕获的 stdout/stderr 截成短尾巴，供失败原因记录。"""
    try:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    except Exception:  # noqa: BLE001
        return ""
    text = " ".join(text.split())
    return text[-limit:]


@dataclass
class PptConversion:
    """旧 .ppt 一次受控转换的完整记录（成功/失败/超时都必须如实返回，绝不静默当成功）。"""
    ok: bool = False
    tool_name: str = ""
    tool_version: str = ""
    output_path: str = ""
    output_sha256: str = ""
    source_pages: Optional[int] = None   # 源 .ppt 页数（OLE 二进制不可直读，需转 PDF 复核）
    output_pages: Optional[int] = None   # 转换后 PPTX 幻灯页数
    page_count_note: str = ""
    command: List[str] = field(default_factory=list)
    exit_code: Optional[int] = None
    duration_s: float = 0.0
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "tool_name": self.tool_name, "tool_version": self.tool_version,
                "output_path": self.output_path, "output_sha256": self.output_sha256,
                "source_pages": self.source_pages, "output_pages": self.output_pages,
                "page_count_note": self.page_count_note, "command": list(self.command),
                "exit_code": self.exit_code, "duration_s": self.duration_s, "reason": self.reason}


class PptConverter:
    """旧 .ppt（OLE 复合文档）受控转换器抽象。

    唯一采用的转换路径是 LibreOffice headless：`soffice --headless --convert-to pptx`，
    在受控临时目录内执行并设超时；命令、探测函数、runner、超时与页数探测均可注入，
    使**没有 LibreOffice** 的环境也能用假转换器覆盖成功/失败/超时三条路径。
    """

    tool_name = "libreoffice-soffice"

    def __init__(self, *, executable: Optional[str] = None, probe: Optional[Callable[[], Optional[str]]] = None,
                 runner: Optional[Callable[[List[str], float], Any]] = None,
                 timeout_s: Optional[float] = None,
                 user_profile_dir: Optional[Path] = None,
                 page_probe: Optional[Callable[[Path, Path], Tuple[Optional[int], str]]] = None) -> None:
        self._executable = executable
        self._probe = probe or _probe_soffice
        self._runner = runner or _default_runner
        self.timeout_s = float(timeout_s if timeout_s is not None else PPT_CONVERT_TIMEOUT_S)
        self._profile = Path(user_profile_dir) if user_profile_dir else _default_lo_profile()
        self._page_probe = page_probe

    def executable(self) -> Optional[str]:
        return self._executable or self._probe()

    def available(self) -> bool:
        try:
            return bool(self.executable())
        except Exception:  # noqa: BLE001 - 探测异常等同"未就绪"
            return False

    def tool_version(self) -> str:
        exe = self.executable()
        return _pe_file_version(exe) if exe else ""

    def _base_cmd(self, exe: str, target: str, out_dir: Path, src: Path) -> List[str]:
        return [exe, f"-env:UserInstallation=file:///{self._profile.as_posix()}",
                "--headless", "--norestore", "--convert-to", target,
                "--outdir", str(out_dir), str(src)]

    def _run(self, cmd: List[str]) -> Tuple[Optional[Any], Optional[str], float]:
        """执行转换命令；返回 (进程结果, 失败原因, 耗时)。runner 异常被收敛为失败原因。"""
        t0 = time.time()
        try:
            proc = self._runner(cmd, self.timeout_s)
        except subprocess.TimeoutExpired:
            return None, f"转换超时（>{self.timeout_s:g}s）", time.time() - t0
        except Exception as ex:  # noqa: BLE001 - 转换器不可用/启动失败都算失败
            return None, f"转换器执行失败：{type(ex).__name__}: {ex}", time.time() - t0
        return proc, None, time.time() - t0

    def convert(self, src: Path, out_dir: Path) -> PptConversion:
        """把旧 .ppt 转为 .pptx（受控目录、超时、失败如实记录）。"""
        exe = self.executable()
        conv = PptConversion(tool_name=self.tool_name, tool_version=self.tool_version())
        if not exe:
            conv.reason = "受控转换工具未就绪：系统缺 LibreOffice headless（soffice）"
            return conv
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
        except Exception as ex:  # noqa: BLE001
            conv.reason = f"受控输出目录不可用：{type(ex).__name__}: {ex}"
            return conv

        conv.command = self._base_cmd(exe, "pptx", out_dir, src)
        proc, failure, elapsed = self._run(conv.command)
        conv.duration_s = round(elapsed, 3)
        if failure is not None:
            conv.reason = failure
            return conv
        conv.exit_code = getattr(proc, "returncode", None)
        out_path = out_dir / f"{src.stem}.pptx"
        if conv.exit_code != 0:
            tail = _tail(getattr(proc, "stderr", b"")) or _tail(getattr(proc, "stdout", b""))
            conv.reason = f"转换器退出码 {conv.exit_code}" + (f"；输出={tail}" if tail else "")
            return conv
        if not out_path.exists() or out_path.stat().st_size == 0:
            conv.reason = "转换器退出码 0 但未产出非空 .pptx（不当作成功）"
            return conv

        conv.ok = True
        conv.output_path = str(out_path)
        conv.output_sha256 = _read_head_and_hash(out_path)[1]
        probe = self._page_probe or self._source_pages_via_pdf
        pages, why = probe(src, out_dir)
        conv.source_pages = pages
        conv.page_count_note = why
        return conv

    def _source_pages_via_pdf(self, src: Path, out_dir: Path) -> Tuple[Optional[int], str]:
        """源 .ppt 页数复核：OLE 二进制无法直读，只能用同一工具转 PDF 后数页（页数对照的来源）。"""
        exe = self.executable()
        if not exe:
            return None, "源页数未复核：无转换工具"
        ref_dir = out_dir / "_source_pdf"
        cmd = self._base_cmd(exe, "pdf", ref_dir, src)
        proc, failure, _elapsed = self._run(cmd)
        if failure is not None:
            return None, f"源页数未复核（{failure}）"
        if getattr(proc, "returncode", None) != 0:
            return None, f"源页数未复核（PDF 转换退出码 {getattr(proc, 'returncode', None)}）"
        pdf_path = ref_dir / f"{src.stem}.pdf"
        try:
            from pypdf import PdfReader
            return len(PdfReader(str(pdf_path)).pages), "源页数经同工具转 PDF 复核"
        except Exception as ex:  # noqa: BLE001
            return None, f"源页数未复核（PDF 解析失败 {type(ex).__name__}）"


def _default_ppt_staging() -> Path:
    """旧 PPT 受控转换目录：资料根目录 staging/ppt_convert（D 盘，铁律 7）。"""
    from . import paths as _paths

    return _paths.staging_dir("ppt_convert")


def _ppt_converter_factory() -> PptConverter:
    """旧 PPT 转换器工厂（测试可替换以注入假转换器，覆盖成功/失败/超时三条路径）。"""
    return PptConverter()


def _ppt_pending_note(reason: str) -> str:
    return ("旧 PPT（OLE 复合文档签名 D0CF11E0，非 OOXML）：仍在待受控转换状态，尚未取得正文；"
            "原件哈希与字节由 parse_file 记录；受控转换须保留原件 hash、转换工具名与版本、"
            "转换前后页数对照与输出路径。本轮原因：" + reason)


def _fmt_pages(n: Optional[int]) -> str:
    return str(n) if n is not None else "未知"


def _parse_ppt_legacy(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    """旧 .ppt：先真实签名识别，再走受控转换；转换成功才继续解析为块。"""
    converter = _ppt_converter_factory()
    if not converter.available():
        reason = "受控转换工具未就绪：系统缺 LibreOffice headless（soffice）；如安装后可一键转换"
        conv = PptConversion(tool_name=converter.tool_name, reason=reason)
        return ParseResult(
            status=STATUS_PENDING_CONVERSION, kind="ppt_legacy", blocks=[], pages_or_sheets=0,
            parser_name="ole_signature", parser_version="1.0", errors=[reason],
            warnings=["旧 PPT 未完成受控转换，正文缺失"],
            quality=QUALITY_PENDING, notes=_ppt_pending_note(reason), conversion=conv.as_dict(),
        )

    try:
        importlib.import_module("pptx")
    except ImportError:
        reason = ("转换工具已探测到，但当前 venv 缺 python-pptx，无法解析转换后的 PPTX；"
                  "请在解析工具链 venv（D:/SafetyMindData/venv）下解析。")
        conv = PptConversion(tool_name=converter.tool_name, tool_version=converter.tool_version(),
                             reason=reason)
        return ParseResult(
            status=STATUS_PENDING_CONVERSION, kind="ppt_legacy", blocks=[], pages_or_sheets=0,
            parser_name="ole_signature", parser_version="1.0", errors=[reason],
            warnings=["旧 PPT 未完成受控转换，正文缺失"],
            quality=QUALITY_PENDING, notes=_ppt_pending_note(reason), conversion=conv.as_dict(),
        )

    base = Path(staging_dir) if staging_dir else _default_ppt_staging()
    out_dir = base / f"{path.stem}_{uuid.uuid4().hex[:8]}"
    conv = converter.convert(path, out_dir)
    if not conv.ok:
        reason = conv.reason or "受控转换失败（原因未记录）"
        return ParseResult(
            status=STATUS_PENDING_CONVERSION, kind="ppt_legacy", blocks=[], pages_or_sheets=0,
            parser_name="ole_signature", parser_version="1.0", errors=[reason],
            warnings=["旧 PPT 未完成受控转换，正文缺失"],
            quality=QUALITY_PENDING, notes=_ppt_pending_note(reason), conversion=conv.as_dict(),
        )

    res = _parse_pptx(Path(conv.output_path), staging_dir)
    conv.output_pages = res.pages_or_sheets
    res.kind = "ppt_legacy"
    res.parser_name = f"{conv.tool_name}+python-pptx"
    res.parser_version = " + ".join(x for x in (conv.tool_version,
                                                _tool_version("pptx", "python-pptx")) if x)
    res.conversion = conv.as_dict()
    res.notes = ("旧 PPT 受控转换后解析："
                 f"工具={conv.tool_name} {conv.tool_version or '版本未知'}；"
                 f"页数对照=源 {_fmt_pages(conv.source_pages)} → 转换后 {conv.output_pages}；"
                 f"转换输出={conv.output_path}（sha256={conv.output_sha256[:16]}）；"
                 f"原件 sha256 与字节由 parse_file 记录；"
                 f"{conv.page_count_note}；{res.notes}")
    return res


# ── 压缩包 ────────────────────────────────────────────────────────────────────

def _parse_zip(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    limit = int(os.getenv("SAFETYMIND_ZIP_MAX_BYTES", str(MAX_ZIP_UNCOMPRESSED)))
    members = _zip_members(path)
    total_uncompressed = sum(size for _n, size in members)
    errors: List[str] = []
    warnings: List[str] = []
    macros = [n for n, _ in members if n.lower().endswith((".docm", ".xlsm", ".pptm", ".vba", ".exe", ".dll", ".bat", ".ps1", ".js", ".vbs"))]
    if macros:
        warnings.append(f"含宏/可执行类成员 {len(macros)} 个（仅登记，不执行）")
    if total_uncompressed > limit:
        errors.append(f"解压后总量 {total_uncompressed} 字节超过受控上限 {limit} 字节")
        return ParseResult(
            status=STATUS_UNPARSEABLE, kind="zip", blocks=[],
            pages_or_sheets=0, parser_name="stdlib-zipfile", parser_version="1",
            errors=errors, warnings=warnings, quality=QUALITY_UNPARSEABLE,
            notes="压缩包超受控体积上限，未展开成员清单",
        )
    shown = members[:ZIP_MEMBER_LIST_MAX]
    lines = [f"成员清单（{len(members)} 项，解压后合计 {total_uncompressed} 字节）"]
    lines += [f"{name}\t{size}" for name, size in shown]
    if len(members) > len(shown):
        warnings.append(f"成员清单截断至前 {len(shown)} 项（共 {len(members)} 项）")
    return ParseResult(
        status=STATUS_PARSED, kind="zip",
        blocks=[Block(
            content="\n".join(lines),
            locator={"members": len(members)},
            kind="manifest",
            detail={"members": shown, "total_uncompressed": total_uncompressed,
                    "macro_or_exec": macros},
        )],
        pages_or_sheets=0, parser_name="stdlib-zipfile", parser_version="1",
        errors=errors, warnings=warnings, quality=QUALITY_OK,
        notes="仅登记成员与解压后大小；未解压、未执行宏/附件/可执行文件，成员需单独解析",
    )


# ── 文本类 ────────────────────────────────────────────────────────────────────

def _parse_text_like(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    data = path.read_bytes()
    text, enc = _decode_text(data)
    ext = path.suffix.lower()
    kind = _EXT_KIND.get(ext, "txt")
    blocks: List[Block] = []
    warnings: List[str] = []

    if kind == "json":
        try:
            payload = json.loads(text)
        except Exception as ex:
            return _failed(kind, "stdlib-json", [f"JSON 解析失败: {ex}"], enc)
        items = payload if isinstance(payload, list) else [payload]
        for i, item in enumerate(items):
            if isinstance(item, dict):
                title = str(item.get("title", "") or "")
                content = item.get("content") or item.get("text") or json.dumps(item, ensure_ascii=False)
                body = f"{title}\n{content}" if title else str(content)
                blocks.append(Block(content=str(body), locator={"index": i}, kind="text",
                                    detail={"title": title, "index": i}))
            else:
                blocks.append(Block(content=str(item), locator={"index": i}, kind="text"))
    elif kind == "csv":
        import csv as _csv
        import io as _io
        rows = [r for r in _csv.reader(_io.StringIO(text)) if any((c or "").strip() for c in r)]
        header = rows[0] if rows else []
        content = "\n".join(" | ".join(r) for r in rows)
        blocks.append(Block(content=content, locator={"row_start": 1}, kind="table",
                            detail={"headers": header, "rows": len(rows)}))
    elif kind == "md":
        heading = None
        para_idx = 0
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            if line.lstrip().startswith("#"):
                heading = line.strip()
                blocks.append(Block(content=heading, locator={"line": line_no}, kind="heading"))
            else:
                para_idx += 1
                blocks.append(Block(content=line.strip(),
                                    locator={"para": para_idx, "line": line_no}, kind="text"))
    else:  # txt
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        for i, para in enumerate(paras, start=1):
            blocks.append(Block(content=para, locator={"para": i}, kind="text"))

    status, quality, warns = _assess(blocks, scan_pages=0, total_pages=0)
    warnings.extend(warns)
    return ParseResult(
        status=status, kind=kind, blocks=blocks, pages_or_sheets=0,
        parser_name="stdlib", parser_version="1", errors=[], warnings=warnings,
        quality=quality, notes=f"编码={enc}",
    )


def _failed(kind: str, parser_name: str, errors: List[str], note: str) -> ParseResult:
    return ParseResult(
        status=STATUS_UNPARSEABLE, kind=kind, blocks=[], pages_or_sheets=0,
        parser_name=parser_name, parser_version="1", errors=errors, warnings=[],
        quality=QUALITY_UNPARSEABLE, notes=note,
    )


# ── 图片（image_scan：OCR 补齐文字层） ────────────────────────────────────────

def _parse_image(path: Path, staging_dir: Optional[Path]) -> ParseResult:
    engine, reason = _resolve_ocr_engine()
    if engine is None:
        return ParseResult(
            status=STATUS_LOW_QUALITY, kind="image_scan",
            blocks=[Block(content="", locator={"page": 1}, kind="image",
                          detail={"needs_ocr": True, "ocr_unavailable": reason})],
            pages_or_sheets=1, parser_name="signature-only", parser_version="1", errors=[],
            warnings=[f"图片无文字层，需 OCR；OCR 引擎不可用（{reason}）"],
            quality=QUALITY_LOW, notes="回归为待 OCR；低质量不进入可靠证据集合",
        )

    errors: List[str] = []
    text = ""
    try:
        from PIL import Image
        with Image.open(str(path)) as image:
            text, ocr_err = _ocr_page(engine, [image.convert("RGB")])
        if ocr_err:
            errors.append(f"OCR 失败: {ocr_err}")
    except Exception as ex:  # noqa: BLE001 - 解码/识别失败只影响本图，不抛给上层
        errors.append(f"图片解码/OCR 失败: {type(ex).__name__}: {ex}")

    if text:
        blocks = [Block(content=text, locator={"page": 1}, kind="text",
                        detail={"ocr": _ocr_detail(engine)})]
        warnings: List[str] = []
        notes = f"图片 OCR：{engine.engine_name} {engine.engine_version}"
        parser_name, parser_version = "rapidocr", engine.engine_version
    else:
        blocks = [Block(content="", locator={"page": 1}, kind="image", detail={"needs_ocr": True})]
        warnings = ["图片无文字层，需 OCR；OCR 未识别出可用文字"]
        notes = "回归为待 OCR；低质量不进入可靠证据集合"
        parser_name, parser_version = "signature-only", "1"

    status, quality, warns = _assess(blocks, scan_pages=0 if text else 1, total_pages=1)
    warnings.extend(warns)
    status, quality = _downgrade_if_weak_ocr(
        status, quality, warnings, 1 if text and len(text) < MIN_OCR_PAGE_CHARS else 0, 1)
    return ParseResult(
        status=status, kind="image_scan", blocks=blocks, pages_or_sheets=1,
        parser_name=parser_name, parser_version=parser_version,
        errors=errors, warnings=warnings, quality=quality, notes=notes,
    )


_HANDLERS = {
    "docx": _parse_docx,
    "xlsx": _parse_xlsx,
    "pptx": _parse_pptx,
    "pdf": _parse_pdf,
    "ppt_legacy": _parse_ppt_legacy,
    "zip": _parse_zip,
    "txt": _parse_text_like, "md": _parse_text_like, "json": _parse_text_like, "csv": _parse_text_like,
    "image_scan": _parse_image,
}


def parse_file(path: str, *, mime: str = "", staging_dir: str | None = None) -> ParseResult:
    """统一解析入口：识别真实格式 → 解析完整正文与定位 → 质量分级。

    path          : 原件路径（只读，不改写原件）
    mime          : 可选 MIME 提示（扩展名/签名无法判定时使用）
    staging_dir   : 可选受控临时目录（仅允许 D 盘；本轮仅作约束，不做展开）

    依赖缺失（重库只在解析工具链 venv）时抛 RuntimeError，不静默降级。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在: {p}")
    if staging_dir is not None:
        sd = Path(staging_dir)
        if sd.drive and sd.drive.upper() != "D:":
            raise ValueError(f"staging_dir 只允许 D 盘（铁律 7）：{sd}")

    head, sha, nbytes = _read_head_and_hash(p)
    kind = _detect_kind(p, head, mime)
    handler = _HANDLERS.get(kind)

    if handler is None:
        return ParseResult(
            status=STATUS_UNSUPPORTED, kind=kind, blocks=[], pages_or_sheets=0,
            parser_name="", parser_version="", errors=[],
            warnings=[f"未支持的格式（扩展名 {p.suffix!r} / mime {mime!r}）"],
            bytes=nbytes, sha256=sha, quality=QUALITY_UNSUPPORTED,
            notes="unsupported：本轮不解析该格式，保留原件与哈希",
        )

    _require(kind)  # 惰性导入重库；缺失 → RuntimeError（清晰提示）
    try:
        res = handler(p, Path(staging_dir) if staging_dir else None)
    except Exception as ex:
        return ParseResult(
            status=STATUS_UNPARSEABLE, kind=kind, blocks=[], pages_or_sheets=0,
            parser_name=_DEPS.get(kind, ("stdlib",))[0] if kind in _DEPS else "stdlib",
            parser_version="", errors=[f"{type(ex).__name__}: {ex}"], warnings=[],
            bytes=nbytes, sha256=sha, quality=QUALITY_UNPARSEABLE,
            notes="解析失败：文件可能损坏或格式不符",
        )
    res.bytes = nbytes
    res.sha256 = sha
    return res
