"""BGE 中文语义嵌入函数（ChromaDB 兼容）。

用途：替换嵌入式（桌面/单机）模式下的 n-gram 词面近似向量，
为 RAG 知识库与情景记忆提供真实中文语义检索。
零 API 成本：模型为本地 BAAI/bge-base-zh-v1.5（约 400MB，首次自动下载）。

回退策略：torch/transformers 缺失或模型加载失败时返回 None，
调用方应回退 LocalEmbeddingFunction（n-gram），保证零破坏。
"""
import logging
import threading
from typing import List, Optional

import torch
from transformers import AutoModel, AutoTokenizer

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()
_shared: Optional["BgeEmbeddingFunction"] = None
_shared_tried = False


class BgeEmbeddingFunction:
    """ChromaDB 兼容的 bge 嵌入函数（新式 input 签名 + embed_query 协议）。

    与 core.local_embedding.LocalEmbeddingFunction 同接口，可直接互换。
    """

    def __init__(self, model_id: str = "BAAI/bge-base-zh-v1.5", device: str = "cpu",
                 max_length: int = 256, batch_size: int = 32):
        self._model_id = model_id
        self._max_length = max_length
        self._batch_size = batch_size
        self._tokenizer = AutoTokenizer.from_pretrained(model_id)
        self._model = AutoModel.from_pretrained(model_id)
        self._model.eval()
        self._model.to(device)
        self._device = device

    def _mean_pool(self, last_hidden, mask):
        mask = mask.unsqueeze(-1).float()
        return (last_hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)

    @torch.no_grad()
    def _encode(self, texts: List[str]) -> List[List[float]]:
        outs = []
        for i in range(0, len(texts), self._batch_size):
            enc = self._tokenizer(texts[i:i + self._batch_size], padding=True,
                                  truncation=True, max_length=self._max_length,
                                  return_tensors="pt").to(self._device)
            emb = self._mean_pool(self._model(**enc).last_hidden_state, enc["attention_mask"])
            outs.append(torch.nn.functional.normalize(emb, dim=-1))
        return torch.cat(outs).tolist() if outs else []

    def __call__(self, input):  # noqa: A002 - chromadb 约定参数名
        return self.embed_documents(input)

    def embed_documents(self, input):
        return self._encode([t if isinstance(t, str) else str(t) for t in input])

    def embed_query(self, input):
        return self.embed_documents(input)

    def name(self) -> str:
        return f"safetymind-bge-{self._model_id.split('/')[-1]}"


def try_bge_embedding(model_id: str = "BAAI/bge-base-zh-v1.5",
                      device: str = "cpu") -> Optional[BgeEmbeddingFunction]:
    """尝试构建 bge 嵌入函数；任何失败（依赖缺失/加载失败）返回 None，调用方回退 n-gram。

    进程内共享单例：同一模型只加载一次。
    """
    global _shared, _shared_tried
    with _state_lock:
        if _shared_tried:
            return _shared
        _shared_tried = True
        try:
            _shared = BgeEmbeddingFunction(model_id=model_id, device=device)
            logger.info(f"BGE 语义嵌入已启用: {model_id} ({device})")
        except Exception as ex:
            logger.warning(f"BGE 嵌入不可用，回退 n-gram: {ex}")
            _shared = None
        return _shared
