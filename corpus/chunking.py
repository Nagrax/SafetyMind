"""K06 token 计量切块：正文约 180~220 token、超长句硬切、表格按行组重复表头。

设计依据：方案 §5.2 —— 当前嵌入 max_length=256；正文目标约 180~220 token 并预留
标题/特殊 token，实际以 tokenizer 计数；超长句需硬切；表格按行组附表头；
按 block 定位取邻块（chunk 的 locator 继承源 block 的定位）。

tokenizer 优先级：BGE tokenizer（与嵌入同一契约）→ 不可用时用**保守估算**函数，
并在返回值里标注 `tokenizer="estimate"`（两个 venv 均可运行）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from corpus.parsers import Block

CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF))

_TOKENIZER_STATE: Dict[str, Optional[Callable[[str], int]]] = {}


def estimate_tokens(text: str) -> int:
    """保守 token 估算（无 BGE tokenizer 时使用）。

    中文按 1 字≈1 token 计（与 bge-base-zh-v1.5 的中文切分近似），非中文按 3 字符
    ≈1 token 保守折算（宁可高估，避免切块超过 256 上限被静默截断）。
    """
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in CJK_RANGES):
            cjk += 1
        elif ch.isspace():
            continue
        else:
            other += 1
    return cjk + (other + 2) // 3


def _bge_tokenizer() -> Optional[Callable[[str], int]]:
    """尝试加载 BGE tokenizer（只加载分词器，不加载模型）；不可用返回 None。"""
    if "fn" in _TOKENIZER_STATE:
        return _TOKENIZER_STATE["fn"]
    fn: Optional[Callable[[str], int]] = None
    try:
        # 本机缓存只在 D 盘（铁律 7）；xet 下载在本机会挂死，必须禁用
        if os.path.isdir(r"D:\hf_cache"):
            os.environ.setdefault("HF_HOME", r"D:\hf_cache")
        os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
        from transformers import AutoTokenizer  # 惰性导入（工具链 venv 无此依赖）
        tok = AutoTokenizer.from_pretrained("BAAI/bge-base-zh-v1.5", local_files_only=True)

        def _count(text: str) -> int:
            return len(tok(text, add_special_tokens=False)["input_ids"])

        fn = _count
    except Exception:
        fn = None
    _TOKENIZER_STATE["fn"] = fn
    return fn


def resolve_tokenizer(tokenizer: Any = None) -> tuple[Callable[[str], int], str]:
    """返回 (计数函数, 名称)。tokenizer 可为 None / callable / "estimate"。"""
    if tokenizer is None:
        bge = _bge_tokenizer()
        if bge is not None:
            return bge, "bge"
        return estimate_tokens, "estimate"
    if isinstance(tokenizer, str):
        if tokenizer != "estimate":
            raise ValueError(f"未知 tokenizer 标识: {tokenizer!r}（仅支持 'estimate' 或 callable）")
        return estimate_tokens, "estimate"
    if callable(tokenizer):
        return tokenizer, getattr(tokenizer, "__name__", "custom")
    raise ValueError(f"tokenizer 必须是 callable / None / 'estimate'，收到 {type(tokenizer)!r}")


@dataclass
class Chunk:
    """切块结果：正文 + 继承自源 block 的定位 + token 计量。"""
    content: str
    locator: Dict[str, Any]
    token_count: int
    tokenizer: str
    kind: str = "text"
    evidence_kind: str = "body"
    chunk_index: int = 0
    header: str = ""


def _as_block(blk: Any) -> Block:
    if isinstance(blk, Block):
        return blk
    if isinstance(blk, Mapping):
        return Block(content=str(blk.get("content", "") or ""),
                     locator=dict(blk.get("locator") or {}),
                     kind=str(blk.get("kind") or "text"),
                     detail=blk.get("detail"))
    return Block(content=str(getattr(blk, "content", "") or ""),
                 locator=dict(getattr(blk, "locator", {}) or {}),
                 kind=str(getattr(blk, "kind", "text") or "text"),
                 detail=getattr(blk, "detail", None))


def _sentences(text: str) -> List[str]:
    """按中文句末标点/换行切句（保留分隔符，避免丢信息）。"""
    import re
    return [p for p in re.split(r"(?<=[。！？!?；;\n])", text) if p.strip()]


def _cut_to_budget(text: str, tok: Callable[[str], int], budget: int) -> tuple[str, str]:
    """二分法把 text 切成 token 数 ≤ budget 的前半与剩余部分。"""
    if budget <= 0:
        return "", text
    if tok(text) <= budget:
        return text, ""
    lo, hi = 1, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if tok(text[:mid]) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo], text[lo:]


def _hard_pieces(text: str, tok: Callable[[str], int], budget: int) -> List[str]:
    """超长句硬切：每片 ≤ budget token。"""
    pieces: List[str] = []
    rest = text
    while tok(rest) > budget:
        piece, rest = _cut_to_budget(rest, tok, budget)
        if not piece:
            break
        pieces.append(piece)
    if rest.strip():
        pieces.append(rest)
    return pieces


def _chunk_text_block(block: Block, tok: Callable[[str], int], tname: str,
                      max_tokens: int, target_tokens: int) -> List[Chunk]:
    chunks: List[Chunk] = []
    cur = ""

    def emit() -> None:
        nonlocal cur
        if cur.strip():
            chunks.append(Chunk(
                content=cur,
                locator={**block.locator, "chunk": len(chunks)},
                token_count=tok(cur),
                tokenizer=tname,
                kind=block.kind,
            ))
        cur = ""

    def append(text: str) -> None:
        nonlocal cur
        if cur and tok(cur) + tok(text) > max_tokens:
            emit()
        cur += text
        if tok(cur) >= target_tokens:
            emit()

    for sent in _sentences(block.content):
        if tok(sent) > max_tokens:
            emit()
            for piece in _hard_pieces(sent, tok, max_tokens):
                append(piece)
        else:
            append(sent)
    emit()
    return chunks


def _chunk_table(block: Block, tok: Callable[[str], int], tname: str,
                 max_tokens: int, target_tokens: int) -> List[Chunk]:
    lines = [ln for ln in block.content.split("\n") if ln.strip()]
    if not lines:
        return []
    detail = block.detail or {}
    headers = detail.get("headers") or []
    header_line = " | ".join(str(h) for h in headers) if headers else lines[0]
    body = lines[1:]
    base_header_row = int(detail.get("header_row") or 1)

    chunks: List[Chunk] = []
    group: List[str] = []
    group_start = base_header_row + 1

    def flush(end_row: int) -> None:
        nonlocal group, group_start
        if not group:
            return
        content = f"{header_line}\n" + "\n".join(group)
        chunks.append(Chunk(
            content=content,
            locator={**block.locator, "chunk": len(chunks),
                     "row_start": group_start, "row_end": end_row},
            token_count=tok(content),
            tokenizer=tname,
            kind="table",
            header=header_line,
        ))
        group = []

    hdr_tokens = tok(header_line)
    row_no = base_header_row
    group_tokens = 0
    for row in body:
        row_no += 1
        rt = tok(row)
        if rt + hdr_tokens > max_tokens:  # 单行超长 → 行内硬切，仍重复表头
            flush(row_no - 1)
            for piece in _hard_pieces(row, tok, max(1, max_tokens - hdr_tokens)):
                group = [piece]
                group_start = row_no
                flush(row_no)
            group_tokens = 0
            continue
        if group and group_tokens + rt + hdr_tokens > max_tokens:
            flush(row_no - 1)
            group_tokens = 0
        if not group:
            group_start = row_no
        group.append(row)
        group_tokens += rt
    flush(row_no)
    return chunks


def chunk_blocks(blocks: Sequence[Any], *, max_tokens: int = 220, target_tokens: int = 180,
                 tokenizer: Any = None) -> List[Chunk]:
    """把 blocks 切成 token 计量的 Chunk 列表。

    - 正文块：句级累积，chunk token 数落在 [target_tokens, max_tokens]（末块可短于 target）；
    - 超长句：硬切成 ≤ max_tokens 的片；
    - 表格块：按行组切，每组重复表头；
    - locator 继承自源 block（另附 chunk/行号等块内定位），便于按定位读邻块。
    """
    if max_tokens < 1 or target_tokens < 1 or target_tokens > max_tokens:
        raise ValueError(f"非法切块预算: max_tokens={max_tokens}, target_tokens={target_tokens}")
    tok, tname = resolve_tokenizer(tokenizer)
    out: List[Chunk] = []
    for blk in blocks:
        b = _as_block(blk)
        if not b.content.strip():
            continue
        if b.kind == "table":
            out.extend(_chunk_table(b, tok, tname, max_tokens, target_tokens))
        else:
            out.extend(_chunk_text_block(b, tok, tname, max_tokens, target_tokens))
    for i, c in enumerate(out):
        c.chunk_index = i
    return out


def chunk_text(text: str, *, locator: Optional[Dict[str, Any]] = None, kind: str = "text",
               max_tokens: int = 220, target_tokens: int = 180,
               tokenizer: Any = None) -> List[Chunk]:
    """单段文本的便捷切块封装。"""
    block = Block(content=text or "", locator=dict(locator or {}), kind=kind)
    return chunk_blocks([block], max_tokens=max_tokens, target_tokens=target_tokens,
                        tokenizer=tokenizer)
