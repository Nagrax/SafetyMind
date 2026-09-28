"""BGE 中文语义嵌入函数（ChromaDB 兼容）。

用途：替换嵌入式（桌面/单机）模式下的 n-gram 词面近似向量，
为 RAG 知识库与情景记忆提供真实中文语义检索。
零 API 成本：模型为本地 BAAI/bge-base-zh-v1.5（约 400MB，首次自动下载）。

回退策略：torch/transformers 缺失或模型加载失败时返回 None，
调用方应回退 LocalEmbeddingFunction（n-gram），保证零破坏。

小核服务器保护（线上压测实测：2 核 + 多路并发推理会打满 CPU 并饿死事件循环，
整个服务连 /health 都无响应，撤载后 30-40s 才恢复）：
- torch 推理线程数默认 1（SAFETYMIND_TORCH_THREADS 可覆盖）；进程级全局设置，
  同时约束知识库嵌入与意图 BGE 两条推理路径；
- 推理并发闸门默认 1（SAFETYMIND_EMBED_MAX_CONCURRENCY）：上层 asyncio.to_thread
  的线程扇出在闸门处串行化，宁可排队不雪崩；
- 确定性结果 LRU 缓存：同一文本不重复推理（压测/检索测试大量重复查询）。
"""
import logging
import os
import threading
from collections import OrderedDict
from typing import List, Optional

logger = logging.getLogger(__name__)

_state_lock = threading.Lock()
_shared: Optional["BgeEmbeddingFunction"] = None
_shared_tried = False

# 进程级推理闸门与缓存：进程内所有 BgeEmbeddingFunction 共享（单例语义）。
_infer_gate: Optional[threading.Semaphore] = None
_embed_cache: "OrderedDict[tuple, list]" = OrderedDict()
_embed_cache_lock = threading.Lock()
_EMBED_CACHE_MAX = 1024


def _apply_torch_thread_limit() -> None:
    """限制 torch 进程内推理线程数（幂等；torch.set_num_threads 是进程全局的）。"""
    try:
        import torch
    except Exception:
        return
    n = int(os.getenv("SAFETYMIND_TORCH_THREADS", "1"))
    if n >= 1:
        torch.set_num_threads(n)


def _infer_gate_get() -> threading.Semaphore:
    global _infer_gate
    if _infer_gate is None:
        n = max(1, int(os.getenv("SAFETYMIND_EMBED_MAX_CONCURRENCY", "1")))
        _infer_gate = threading.Semaphore(n)
    return _infer_gate


# 进程级模型单例：同一 model_id 只加载一份。意图 BGE（core/intent_bge.py）与本嵌入器
# 用的是同一个 bge-base——曾经各自加载一份把 2c2G 的 RSS 推到 1362MB（MemoryMax=1500M
# 仅剩 138MB 余量，并发有 OOM-kill 风险），共享后省约 450MB 且二次加载零成本。
_MODEL_CACHE: "OrderedDict[str, tuple]" = OrderedDict()
_MODEL_CACHE_LOCK = threading.Lock()


def get_shared_bge(model_id: str, device: str = "cpu"):
    """获取（或首次加载并缓存）共享的 bge tokenizer+model。进程内幂等。"""
    import torch
    from transformers import AutoModel, AutoTokenizer
    # 本机缓存优先 D:\hf_cache（存在才覆盖默认值）；xet 下载在本机会挂死，必须禁用
    if os.path.isdir(r"D:\hf_cache"):
        os.environ.setdefault("HF_HOME", r"D:\hf_cache")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    _apply_torch_thread_limit()
    with _MODEL_CACHE_LOCK:
        if model_id not in _MODEL_CACHE:
            tok = AutoTokenizer.from_pretrained(model_id)
            mdl = AutoModel.from_pretrained(model_id)
            mdl.eval()
            mdl.to(device)
            _MODEL_CACHE[model_id] = (tok, mdl)
        return _MODEL_CACHE[model_id]


class BgeEmbeddingFunction:
    """ChromaDB 兼容的 bge 嵌入函数（新式 input 签名 + embed_query 协议）。

    与 core.local_embedding.LocalEmbeddingFunction 同接口，可直接互换。
    torch/transformers 在构造时惰性导入——模块导入本身必须零重依赖，
    否则 auto 模式"依赖缺失回退 n-gram"的承诺在 import 阶段就会失效。
    """

    def __init__(self, model_id: str = "BAAI/bge-base-zh-v1.5", device: str = "cpu",
                 max_length: int = 256, batch_size: int = 32):
        import torch
        # 进程级共享单例（见 get_shared_bge 注释）：与意图 BGE 共用同一份权重
        tok, mdl = get_shared_bge(model_id, device)
        self._torch = torch
        self._model_id = model_id
        self._max_length = max_length
        self._batch_size = batch_size
        self._tokenizer = tok
        self._model = mdl
        self._device = device

    def _mean_pool(self, last_hidden, mask):
        mask = mask.unsqueeze(-1).float()
        return (last_hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)

    def _encode(self, texts: List[str]) -> List[List[float]]:
        torch = self._torch
        cache_key = (self._model_id, tuple(texts))
        with _embed_cache_lock:
            hit = _embed_cache.get(cache_key)
            if hit is not None:
                _embed_cache.move_to_end(cache_key)
                return [list(v) for v in hit]  # 拷贝，防调用方改动污染缓存
        gate = _infer_gate_get()
        with gate:  # 串行/限并发推理：小核机器上并行 torch 只会互相拖死（见模块注释）
            outs = []
            with torch.no_grad():
                for i in range(0, len(texts), self._batch_size):
                    enc = self._tokenizer(texts[i:i + self._batch_size], padding=True,
                                          truncation=True, max_length=self._max_length,
                                          return_tensors="pt").to(self._device)
                    emb = self._mean_pool(self._model(**enc).last_hidden_state, enc["attention_mask"])
                    outs.append(torch.nn.functional.normalize(emb, dim=-1))
            vectors = torch.cat(outs).tolist() if outs else []
        with _embed_cache_lock:
            _embed_cache[cache_key] = vectors
            _embed_cache.move_to_end(cache_key)
            while len(_embed_cache) > _EMBED_CACHE_MAX:
                _embed_cache.popitem(last=False)
        return [list(v) for v in vectors]

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
