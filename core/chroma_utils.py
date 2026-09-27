"""本地嵌入式 chromadb 的自愈打开工具。

Windows 上批量入库后偶发 HNSW 段损坏（"Error loading hnsw index"，
本机已两次独立复现）：损坏藏在段文件里，创建客户端不报错，
首次 count()/query() 才爆炸——曾直接炸掉 API 启动（lifespan 崩溃）。

此工具在打开时立即对全部集合做 count() 探测：
  - 探测通过 → 正常返回客户端；
  - 探测到 hnsw/segment 损坏 → 归档整个目录后重建空库（种子数据由调用方重导）；
    归档失败（Windows 文件句柄未释放时 rename 会被拒）则退到时间戳新目录，
    保证服务一定起得来。损坏归档目录可事后人工删除。
"""
import logging
import os
import time

import chromadb

logger = logging.getLogger(__name__)


def _is_corruption(ex: Exception) -> bool:
    msg = str(ex).lower()
    return "hnsw" in msg or "segment" in msg


def _new_client(path: str) -> chromadb.api.ClientAPI:
    return chromadb.PersistentClient(
        path=path,
        settings=chromadb.Settings(anonymized_telemetry=False),
    )


def open_local_chroma(path: str) -> chromadb.api.ClientAPI:
    """打开本地嵌入式 chroma，带损坏自愈。只在损坏时才走归档/换目录路径。"""
    client = _new_client(path)
    try:
        for col in client.list_collections():
            col.count()  # 强制加载 HNSW 段，最快暴露损坏
        return client
    except Exception as ex:
        if not _is_corruption(ex):
            raise  # 非损坏类故障按原样抛出，不吞
        logger.warning(f"chroma 本地库损坏（{ex}），尝试归档重建: {path}")
        archive = f"{path}_corrupt_{int(time.time())}"
        try:
            os.rename(path, archive)
            logger.warning(f"已归档损坏库 → {archive}（可人工删除）")
            return _new_client(path)
        except OSError:
            # 句柄未释放，rename 被拒：换时间戳新目录，坏目录留给人工清理
            fresh = f"{path}_fresh_{int(time.time())}"
            logger.warning(f"归档失败（文件被占用），改用新目录: {fresh}")
            return _new_client(fresh)
