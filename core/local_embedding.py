"""本地字符 n-gram 嵌入向量（零依赖、零下载）。

用途：ChromaDB 嵌入式模式（桌面/演示）下的 embedding function。
官方默认的 all-MiniLM-L6-v2 是英文模型且需从 CDN 下载 ~80MB，
对中文安全法规库语义能力有限、下载链路脆弱，因此嵌入式模式统一
改用本地确定性向量；Docker 服务器模式下由服务端模型负责嵌入，
客户端 embedding_function 不参与计算。
"""
import hashlib
from typing import List


def ngram_vector(text: str, dims: int = 256) -> List[float]:
    """稳定的字符 n-gram 哈希向量（1/2/3-gram，md5 定位 ±符号累加），单位归一化。

    必须归一化：Chroma 默认 L2 距离下，未归一化的词袋向量范数被文本长度主导，
    排序会退化为"最短文档永远最近"；归一化后 L2 与余弦相似度单调等价。
    """
    normalized = (text or "").lower().strip()
    vec = [0.0] * dims
    tokens = set()
    for n in (1, 2, 3):
        if len(normalized) >= n:
            tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
    if not tokens:
        tokens.add(normalized)

    for token in tokens:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        idx = int.from_bytes(digest[:4], "big") % dims
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign

    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


class LocalEmbeddingFunction:
    """ChromaDB 兼容的本地 embedding function（新式 input 签名 + embed_query 协议）。"""

    def __call__(self, input):  # noqa: A002 - chromadb 约定参数名
        return self.embed_documents(input)

    def embed_documents(self, input):
        return [ngram_vector(t if isinstance(t, str) else str(t)) for t in input]

    def embed_query(self, input):
        return self.embed_documents(input)

    def name(self) -> str:
        return "safetymind-local-ngram-256"
