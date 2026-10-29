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
- 确定性结果缓存：同一文本不重复推理（压测/检索测试大量重复查询）；
  **按文本逐条缓存 + 向量数/字节数双限额**（方案 §6"必须修复批量缓存放大"），
  建库等大批量路径经 embed_documents() 绕过在线查询缓存。
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
# 查询结果缓存：**按文本逐条**缓存（键 = (model_id, text)，值 = 单条向量），并以
# "向量数 + 近似字节"双限额约束内存。修复方案 §6 的"批量缓存放大"：旧实现按**调用批次**
# 计数（1024 项）、键包含整批文本、值保留整批 Python 浮点列表，实际可驻留数万条向量，
# 远不等于"1024 个 float32 向量"。
_embed_cache: "OrderedDict[tuple, tuple]" = OrderedDict()  # key -> (向量, 近似字节)
_embed_cache_lock = threading.Lock()
_embed_cache_vectors = 0
_embed_cache_bytes = 0
DEFAULT_CACHE_MAX_VECTORS = 4096                                  # 默认向量数上限
DEFAULT_CACHE_MAX_BYTES = 24 * 1024 * 1024                        # 默认 24MiB


def _cache_limits() -> tuple:
    """缓存双限额（向量数、近似字节）；每次读取，便于监控与测试注入。"""
    try:
        max_vectors = int(os.getenv("SAFETYMIND_BGE_CACHE_MAX_VECTORS",
                                    str(DEFAULT_CACHE_MAX_VECTORS)))
    except ValueError:
        max_vectors = DEFAULT_CACHE_MAX_VECTORS
    try:
        max_bytes = int(os.getenv("SAFETYMIND_BGE_CACHE_MAX_BYTES",
                                  str(DEFAULT_CACHE_MAX_BYTES)))
    except ValueError:
        max_bytes = DEFAULT_CACHE_MAX_BYTES
    return max(0, max_vectors), max(0, max_bytes)


def _vec_approx_bytes(vec: list) -> int:
    """向量近似字节数：按 float32（4 字节/维）计，与方案 §6 的口径一致。"""
    return len(vec) * 4


def _cache_put(key: tuple, vec: list) -> None:
    """写入一条向量并按双限额淘汰最旧项（LRU）。"""
    global _embed_cache_vectors, _embed_cache_bytes
    nbytes = _vec_approx_bytes(vec)
    with _embed_cache_lock:
        old = _embed_cache.pop(key, None)
        if old is not None:
            _embed_cache_vectors -= 1
            _embed_cache_bytes -= old[1]
        _embed_cache[key] = (vec, nbytes)
        _embed_cache_vectors += 1
        _embed_cache_bytes += nbytes
        max_vectors, max_bytes = _cache_limits()
        while _embed_cache and (_embed_cache_vectors > max_vectors
                                or _embed_cache_bytes > max_bytes):
            _, (_ev, eb) = _embed_cache.popitem(last=False)
            _embed_cache_vectors -= 1
            _embed_cache_bytes -= eb


def clear_embed_cache() -> None:
    """清空查询缓存（测试/诊断用）。"""
    global _embed_cache_vectors, _embed_cache_bytes
    with _embed_cache_lock:
        _embed_cache.clear()
        _embed_cache_vectors = 0
        _embed_cache_bytes = 0


def cache_stats() -> dict:
    """查询缓存监控指标：条目数 / 向量数 / 近似字节 / 双限额。"""
    max_vectors, max_bytes = _cache_limits()
    with _embed_cache_lock:
        return {"entries": len(_embed_cache), "vectors": _embed_cache_vectors,
                "approx_bytes": _embed_cache_bytes,
                "max_vectors": max_vectors, "max_bytes": max_bytes}


def _apply_torch_thread_limit() -> None:
    """限制 torch 进程内推理线程数（幂等；torch.set_num_threads 是进程全局的）。

    2026-10-29 实测（`benchmarks/bench_embedding.py`，同一批 60 条真实文本）：
      threads=1 → 2.28 条/秒（439ms/条）
      threads=2 → 4.14 条/秒（+82%）
      threads=4 → 6.29 条/秒（+176%）
      threads=8 → 6.99 条/秒（只再 +11%，边际收益递减）
    batch size 8/32/64 在 4 线程下为 6.69/6.29/5.89（短文本下小批量略优，差异在噪声内）。

    所以默认改为**按核数自适应**：`max(1, min(4, cpu//2))`——20 核机器用 4 线程，
    2 核小机仍是 1 线程（避免 embedding 抢满 CPU 把事件循环饿死，那是历史上 2c2G
    部署的真实故障）。`SAFETYMIND_TORCH_THREADS` 显式设置时仍然优先。
    """
    try:
        import torch
    except Exception:
        return
    raw = (os.getenv("SAFETYMIND_TORCH_THREADS") or "").strip()
    if raw:
        try:
            n = int(raw)
        except ValueError:
            n = 1
    else:
        n = max(1, min(4, (os.cpu_count() or 2) // 2))
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


def apply_hf_offline_policy() -> None:
    """统一的 HF 离线策略（2026-10-26 生产就绪门禁实测后加）。

    ## 问题
    默认联网模式下，模型加载前会向 hf-mirror.com 发 15 次 HEAD 请求做版本检查：
    冷启动 40.2s（离线仅 29.2s，差 11s）；更严重的是**无外网环境**
    （内网部署/防火墙/镜像站限流）会卡住，服务 180s 都起不来
    （benchmarks/prod_readiness.py G1 实测失败两次）。

    ## 为什么必须放在这里
    `get_shared_bge` 是**知识库嵌入器与意图识别器共用的唯一加载入口**。
    第一版只改在 core/intent_bge._load_models，漏了知识库这条更早的加载路径，
    于是外部请求照旧发生、启动照旧卡住——这是"修了一处漏了另一处"的典型。
    现在策略集中在本函数，所有 bge 加载都经过它。

    ## 权衡
    默认离线 = 启动快、可离线部署；代价是"模型有新版本不自动发现"。
    对生产这个取舍是对的：**部署可复现 > 自动升级**。
    需要联网检查时设 SAFETYMIND_HF_ONLINE=1。
    调用方若已显式设置 HF_HUB_OFFLINE，则尊重调用方（用 setdefault）。

    ## 关键陷阱（实测踩过三次，务必保留这段注释）
    `huggingface_hub` 在 **import 时**把 `HF_HUB_OFFLINE` 读成模块常量
    `huggingface_hub.constants.HF_HUB_OFFLINE`，之后 `os.environ` 再改**完全无效**：

        >>> import huggingface_hub; os.environ["HF_HUB_OFFLINE"]="1"
        >>> huggingface_hub.constants.HF_HUB_OFFLINE   # 仍是 False
        False

    所以只设环境变量会出现尴尬局面：日志显示 env 设了，网络请求照旧发出，
    服务照旧卡住（这正是 prod_readiness G1 连失败三次的原因）。
    正确做法是**同时**改模块常量；对 transformers 也做同样处理。
    """
    offline = os.getenv("SAFETYMIND_HF_ONLINE", "").strip() != "1"
    if offline:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    else:
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    # 覆盖已 import 的模块常量（否则上面设的 env 不生效）
    try:
        import huggingface_hub.constants as hf_const
        hf_const.HF_HUB_OFFLINE = bool(offline)
    except Exception:      # 未安装/版本差异都不应影响主流程
        pass
    try:
        import transformers.utils.hub as tf_hub   # type: ignore
        if hasattr(tf_hub, "TRANSFORMERS_OFFLINE"):
            tf_hub.TRANSFORMERS_OFFLINE = bool(offline)
    except Exception:
        pass
    try:
        from transformers.utils import hub as tf_hub2  # noqa: F401
        if hasattr(tf_hub2, "is_offline_mode"):
            # transformers 的 is_offline_mode 会缓存结果，清掉缓存让它重读
            cache_clear = getattr(tf_hub2.is_offline_mode, "cache_clear", None)
            if callable(cache_clear):
                cache_clear()
    except Exception:
        pass


def get_shared_bge(model_id: str, device: str = "cpu"):
    """获取（或首次加载并缓存）共享的 bge tokenizer+model。进程内幂等。"""
    import torch
    from transformers import AutoModel, AutoTokenizer
    # 本机缓存优先 D:\hf_cache（存在才覆盖默认值）；xet 下载在本机会挂死，必须禁用
    if os.path.isdir(r"D:\hf_cache"):
        os.environ.setdefault("HF_HOME", r"D:\hf_cache")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    apply_hf_offline_policy()   # 必须在 from_pretrained 之前（否则版本检查照旧联网）
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

    def _run_model(self, texts: List[str]) -> List[List[float]]:
        """批量推理（与改造前同一计算路径：256 截断、均值池化、归一化、batch 切分）。"""
        if not texts:
            return []
        torch = self._torch
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
            return torch.cat(outs).tolist() if outs else []

    def _encode(self, texts: List[str], *, write_cache: bool) -> List[List[float]]:
        """按文本逐条缓存：命中直接返回；未命中合并为一批推理后逐条写缓存。

        write_cache=False 时**完全绕过缓存**（建库/大批量路径，避免把整库向量灌进
        在线查询缓存）。同一文本的向量与改造前逐位一致（计算路径未变）。
        """
        texts = [t if isinstance(t, str) else str(t) for t in texts]
        if not texts:
            return []
        out: List[Optional[List[float]]] = [None] * len(texts)
        miss: List[int] = []
        if write_cache:
            with _embed_cache_lock:
                for i, text in enumerate(texts):
                    key = (self._model_id, text)
                    entry = _embed_cache.get(key)
                    if entry is not None:
                        _embed_cache.move_to_end(key)
                        out[i] = list(entry[0])  # 拷贝，防调用方改动污染缓存
                    else:
                        miss.append(i)
        else:
            miss = list(range(len(texts)))
        if miss:
            # 同批内重复文本只推理一次（保持与旧实现相同的批次切分口径）
            uniq: "OrderedDict[str, List[int]]" = OrderedDict()
            for i in miss:
                uniq.setdefault(texts[i], []).append(i)
            uniq_texts = list(uniq.keys())
            vecs = self._run_model(uniq_texts)
            for text, vec in zip(uniq_texts, vecs):
                if write_cache:
                    _cache_put((self._model_id, text), vec)
                for i in uniq[text]:
                    out[i] = list(vec)
        return [v if v is not None else [] for v in out]

    def __call__(self, input):  # noqa: A002 - chromadb 约定参数名
        # ChromaDB 写入文档走 __call__ → 建库路径默认不写在线查询缓存（方案 §6）
        return self.embed_documents(input)

    def embed_documents(self, input):
        """建库/批量入口：**默认不写查询缓存**（大批量建库绕过在线缓存）。"""
        return self._encode(list(input), write_cache=False)

    def embed_query(self, input):
        """查询入口：按文本逐条读写查询缓存（受向量数/字节数双限额约束）。"""
        return self._encode(list(input), write_cache=True)

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
