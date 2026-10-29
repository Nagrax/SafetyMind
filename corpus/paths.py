"""资料库根目录与路径解析（K02 数据契约的第一块）。

约定（方案 §5）：
  - 资料根目录默认 `D:/SafetyMindData/corpus`，可用 `SAFETYMIND_CORPUS_ROOT` 覆盖；
  - 大文件与代码分离：**所有资料产物只落 D 盘**，函数层强制校验（铁律 7）；
  - manifest/FTS 用 SQLite 单文件，便于备份、断点与跨进程读取。

本模块只做路径解析与目录创建，不做任何下载/解析/写业务数据。
"""
import os
from pathlib import Path
from typing import Dict

DEFAULT_ROOT = "D:/SafetyMindData/corpus"

# 子目录（方案 §5 的拟建目录，创建即用）
SUBDIRS = (
    "originals/sha256",   # 完整原件，内容去重（按 sha256 分桶）
    "normalized",         # 完整段落、页、表格、OCR 结果
    "templates",          # 原模板与字段 schema
    "indexes",            # indexes/<build_id>/ 短目录卡、精选正文向量
    "staging",            # 未完成下载、解压、解析
    "reports",            # 覆盖、容量、质量、效果与阻塞
    "exports",            # 用户私有学习产物
)


def corpus_root() -> Path:
    """资料根目录（可被 SAFETYMIND_CORPUS_ROOT 覆盖）。"""
    return Path(os.getenv("SAFETYMIND_CORPUS_ROOT", DEFAULT_ROOT)).resolve()


def _assert_on_d(path: Path) -> Path:
    """铁律 7：资料产物不得写 C 盘（含一切下载/缓存）。"""
    drive = path.drive.upper()
    if drive and drive != "D:":
        raise ValueError(f"资料产物只允许落 D 盘，收到：{path}")
    return path


def ensure_layout(root: Path | None = None) -> Path:
    """创建（幂等）资料根目录与全部子目录，返回根目录。"""
    base = _assert_on_d(Path(root).resolve() if root else corpus_root())
    base.mkdir(parents=True, exist_ok=True)
    for sub in SUBDIRS:
        _assert_on_d(base).joinpath(sub).mkdir(parents=True, exist_ok=True)
    return base


def subdir(name: str, root: Path | None = None) -> Path:
    """取子目录（自动创建）。name 必须在 SUBDIRS 内，避免拼出任意路径。"""
    if name not in SUBDIRS:
        raise ValueError(f"未知子目录 {name!r}，只允许 {SUBDIRS}")
    base = ensure_layout(root)
    return base / name


def manifest_db(root: Path | None = None) -> Path:
    """manifest SQLite 路径（来源/资产/版本/定位/FTS/状态；**单库**）。"""
    return ensure_layout(root) / "manifest.sqlite"


def chroma_dir(root: Path | None = None) -> Path:
    """向量持久化目录（**单一共享目录** indexes/chroma）。

    存储收敛（2026-10）：所有 build 共用同一个 chroma 目录，不再每 build 一份
    indexes/<build_id>/chroma/；build 之间靠 collection 名（corpus_cards__<build> /
    corpus_body__<build>）区分，退役时按 collection 回收空间。

    **刻意不复用 data/kb_chroma**：corpus 建库失败或写入压力不得威胁用户现网 RAG 目录
    （铁律 9 明确并发打开会损坏 HNSW 段），故 corpus 向量始终落资料根目录下的独立目录。
    """
    return subdir("indexes", root) / "chroma"


def layout(root: Path | None = None) -> Dict[str, str]:
    """根目录与子目录的绝对路径映射（供报告/日志使用）。"""
    base = ensure_layout(root)
    out = {"root": str(base), "manifest_db": str(base / "manifest.sqlite"),
           "chroma_dir": str(base / "indexes" / "chroma")}
    for sub in SUBDIRS:
        out[sub] = str(base / sub)
    return out


def build_dir(build_id: str, root: Path | None = None) -> Path:
    """单次索引构建目录 indexes/<build_id>/（小批构建与切换、保留回滚能力）。"""
    if not build_id or "/" in build_id or "\\" in build_id:
        raise ValueError(f"非法 build_id: {build_id!r}")
    p = subdir("indexes", root) / build_id
    p.mkdir(parents=True, exist_ok=True)
    return p


def staging_dir(tag: str, root: Path | None = None) -> Path:
    """受控临时目录 staging/<tag>/（下载、解压、解析中间件；单批受控）。"""
    if not tag or "/" in tag or "\\" in tag:
        raise ValueError(f"非法 staging tag: {tag!r}")
    p = subdir("staging", root) / tag
    p.mkdir(parents=True, exist_ok=True)
    return p
