"""
RAG 知识库 —— 基于 ChromaDB 的真实检索实现（安全生产法规与制度库）。

功能：
  1. 文档导入：将文本按 500 字切片（句级边界 + 相邻重叠）后存入 ChromaDB
  2. 父子召回（small-to-big）：切片（子块）用于向量检索命中，
     命中后返回其所属整篇文档（父块）作为生成上下文——
     法规条文一句话命中、整篇制度可用的典型场景。
  3. 语义检索：根据 query 从知识库中检索最相关的文档片段
  4. 与 MCP 工具框架集成：作为 knowledge_search 工具的真实 handler

ChromaDB 在这里的角色：
  - memory/ 中用于存储对话记忆（情景记忆 + 用户画像）
  - 这里用于存储知识库文档（子块集合 + 父块集合，两个 collection）
  两者互不干扰。
"""
import asyncio
import hashlib
import logging
from typing import Any, Dict, List, Optional

import os
import chromadb

logger = logging.getLogger(__name__)


class KnowledgeBase:
    """
    基于 ChromaDB 的 RAG 知识库（父子召回）。

    ChromaDB 内置了 Embedding 模型（all-MiniLM-L6-v2），
    调用 add() 时自动生成向量，query() 时自动做语义匹配。
    不需要额外调用 Anthropic Embeddings API。

    存储：
      - 子块集合（knowledge_base）：按句切分的 ~500 字片段，带重叠，用于检索命中
      - 父块集合（knowledge_base_parents）：整篇原文，命中子块后返回父块内容
    """

    COLLECTION_NAME         = "knowledge_base"
    PARENT_COLLECTION_NAME  = "knowledge_base_parents"
    PARENT_MAX_CHARS        = 6000   # 父块（整篇文档）入库截断上限
    RETURN_PARENT_MAX_CHARS = 1200   # 返回给 prompt 的父块内容截断
    OVERLAP_SENTENCES       = 1      # 相邻子块重叠的句子数

    def __init__(
        self,
        chroma_host: str = "localhost",
        chroma_port: int = 8000,
        chroma_path: str = "./data/chroma",
    ):
        # 优先连接独立 ChromaDB 服务（服务端内置 embedding 模型，客户端无需下载）
        self._use_server = False
        try:
            # HttpClient 默认也会初始化 ChromaDB telemetry；显式关闭避免 posthog 兼容性错误日志。
            self._client = chromadb.HttpClient(
                host=chroma_host,
                port=chroma_port,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )
            self._client.heartbeat()
            self._use_server = True
            logger.info(f"知识库 ChromaDB 已连接: {chroma_host}:{chroma_port}")
        except Exception:
            logger.info(f"知识库 ChromaDB 服务不可用，使用本地模式: {chroma_path}")
            self._client = chromadb.PersistentClient(
                path=chroma_path,
                settings=chromadb.Settings(anonymized_telemetry=False),
            )

        # 服务器模式由服务端模型负责嵌入；嵌入式（桌面/单机）模式优先 BGE 语义嵌入
        # （真实中文语义，本地推理零 API 成本），依赖缺失或加载失败时回退 n-gram。
        # 两种嵌入的向量空间不兼容，集合名以 @后缀 区分，各自独立播种默认文档。
        embedding_function = None
        suffix = ""
        if not self._use_server:
            mode = os.getenv("SAFETYMIND_EMBEDDING", "auto")  # auto | bge | ngram
            if mode in ("auto", "bge"):
                from core.bge_embedding import try_bge_embedding
                embedding_function = try_bge_embedding()
                if embedding_function is not None and mode == "bge":
                    pass
            if embedding_function is None and mode == "bge":
                raise RuntimeError("SAFETYMIND_EMBEDDING=bge 但 BGE 嵌入加载失败")
            if embedding_function is None:
                from core.local_embedding import LocalEmbeddingFunction
                embedding_function = LocalEmbeddingFunction()
            if embedding_function.name().startswith("safetymind-bge"):
                suffix = "_bge"
            logger.info(f"知识库嵌入: {embedding_function.name()}")

        # 子块集合：检索入口
        self._collection = self._client.get_or_create_collection(
            name=self.COLLECTION_NAME + suffix,
            metadata={"description": "SafetyMind RAG 知识库（子块）"},
            embedding_function=embedding_function,
        )
        # 父块集合：整篇文档，命中子块后取回完整上下文（仅为批量 get 的载体，不参与检索）
        self._parents = self._client.get_or_create_collection(
            name=self.PARENT_COLLECTION_NAME + suffix,
            metadata={"description": "SafetyMind RAG 知识库（父块·整篇）"},
            embedding_function=embedding_function,
        )

        # 如果知识库为空，导入默认文档（安全生产法规与制度）
        if self._collection.count() == 0:
            self._load_default_docs()

    # ── 文档管理 ──────────────────────────────────────────────────────────────

    def add_documents(self, documents: List[Dict[str, str]]) -> int:
        """
        批量导入文档到知识库。

        documents 格式: [{"title": "...", "content": "..."}, ...]
        长文档会自动切片（每片约 500 字，句级边界，相邻块重叠 1 句）。
        同时将整篇原文存入父块集合，供父子召回使用。

        返回值：导入的子块数量。
        """
        ids, docs, metas = [], [], []

        for doc in documents:
            title   = doc.get("title", "")
            content = doc.get("content", "")
            parent_id = hashlib.md5(f"{title}_parent".encode()).hexdigest()
            chunks  = self._chunk_text(content, chunk_size=500)

            for i, chunk in enumerate(chunks):
                doc_id = hashlib.md5(f"{title}_{i}_{chunk[:50]}".encode()).hexdigest()
                ids.append(doc_id)
                docs.append(chunk)
                metas.append({
                    "title": title,
                    "type": "child",
                    "parent_id": parent_id,
                    "chunk_index": i,
                    "total_chunks": len(chunks),
                })

            # 父块：整篇文档（截断），供命中后取回完整上下文
            self._parents.upsert(
                ids=[parent_id],
                documents=[content[:self.PARENT_MAX_CHARS]],
                metadatas=[{"title": title, "type": "parent"}],
            )

        if ids:
            # ChromaDB 会自动生成 Embedding
            self._collection.add(ids=ids, documents=docs, metadatas=metas)
            logger.info(f"知识库导入 {len(ids)} 个子块 / {len(documents)} 篇父文档")

        return len(ids)

    async def add_documents_async(self, documents: List[Dict[str, str]]) -> int:
        """异步导入文档；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.add_documents, documents)

    # ── 检索 ──────────────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        父子召回（small-to-big）：
          1. 用 query 在子块集合上做向量检索（超采 top_k * 4）
          2. 按父块聚合去重，保留每篇父块的最高子块得分
          3. 批量取回父块（整篇）内容，按子块得分排序返回前 top_k 篇

        旧数据（子块无 type/parent_id 元数据）自动退化为子块直出。
        """
        results = self._collection.query(
            query_texts=[query],
            n_results=top_k * 4,
            where={"type": "child"},
        )
        if not results["documents"] or not results["documents"][0]:
            # 兼容旧数据：子块没有 type 元数据时不过滤重查
            results = self._collection.query(query_texts=[query], n_results=top_k * 4)
            if not results["documents"] or not results["documents"][0]:
                return []
            hits = zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            )
            return [
                {
                    "title":    meta.get("title", ""),
                    "content":  doc[:self.RETURN_PARENT_MAX_CHARS],
                    "snippet":  doc[:300],
                    "score":    round(1.0 - dist, 4),
                    "chunk":    meta.get("chunk_index", 0),
                }
                for doc, meta, dist in hits
            ][:top_k]

        hits = zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        )

        # 按父块聚合：同一篇文档的多个子块命中只保留最高分
        best_by_parent: Dict[str, Dict[str, Any]] = {}
        for doc, meta, dist in hits:
            parent_id = meta.get("parent_id", "")
            score = round(1.0 - dist, 4)  # ChromaDB 返回距离，转为相似度
            if not parent_id or parent_id not in best_by_parent or score > best_by_parent[parent_id]["score"]:
                best_by_parent[parent_id] = {
                    "parent_id": parent_id,
                    "title":     meta.get("title", ""),
                    "score":     score,
                    "chunk":     meta.get("chunk_index", 0),
                    "snippet":   doc,
                }

        # 按子块得分排序，取前 top_k 篇父文档
        ordered = sorted(best_by_parent.values(), key=lambda x: x["score"], reverse=True)[:top_k]

        # 批量取回父块整篇内容
        parents_map: Dict[str, str] = {}
        ids_to_fetch = [e["parent_id"] for e in ordered if e["parent_id"]]
        if ids_to_fetch:
            try:
                got = self._parents.get(ids=ids_to_fetch)
                if got and got.get("documents"):
                    parents_map = dict(zip(got["ids"], got["documents"]))
            except Exception as ex:
                logger.warning(f"父块取回失败，退化为子块直出: {ex}")

        items: List[Dict[str, Any]] = []
        for entry in ordered:
            parent_text = parents_map.get(entry["parent_id"])
            if not parent_text:
                parent_text = entry["snippet"]  # 无父块时退化为命中的子块
            items.append({
                "title":    entry["title"],
                "content":  parent_text[:self.RETURN_PARENT_MAX_CHARS],
                "snippet":  entry["snippet"][:300],
                "score":    entry["score"],
                "chunk":    entry["chunk"],
            })
        return items

    async def search_async(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """异步检索；ChromaDB 客户端为同步实现，因此放入线程池执行。"""
        return await asyncio.to_thread(self.search, query, top_k)

    @property
    def doc_count(self) -> int:
        return self._collection.count()

    async def doc_count_async(self) -> int:
        """异步获取文档片段数量。"""
        return await asyncio.to_thread(self._collection.count)

    # ── MCP 工具 handler ─────────────────────────────────────────────────────

    async def search_handler(self, params: Dict[str, Any], context: Any) -> List[Dict]:
        """
        作为 MCP 工具的 handler 注册。

        MCPToolManager.register(Tool(
            name="knowledge_search",
            handler=kb.search_handler,
            ...
        ))
        """
        query = params.get("query", "")
        top_k = params.get("top_k", 5)
        return await self.search_async(query, top_k=top_k)

    # ── 内部方法 ──────────────────────────────────────────────────────────────

    def _chunk_text(self, text: str, chunk_size: int = 500) -> List[str]:
        """
        将长文本按 chunk_size 切片：句级边界 + 相邻块重叠 1 句。

        重叠的目的：法规/制度条款被切断时，相邻块共享边界句，
        降低"关键限定词落在上一块末尾"导致的召回缺上下文问题。
        """
        if len(text) <= chunk_size:
            return [text] if text.strip() else []

        # 按句子切分
        sentences = [s.strip() for s in text.replace("\n", "。").split("。") if s.strip()]

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0
        for sent in sentences:
            if current_len + len(sent) + 1 > chunk_size and current:
                chunks.append("。".join(current))
                # 重叠：下一块以上一块的末句开头
                current = [current[-1]] if self.OVERLAP_SENTENCES else []
                current_len = sum(len(s) for s in current)
            current.append(sent)
            current_len += len(sent) + 1

        if current:
            chunks.append("。".join(current))

        return chunks

    def _load_default_docs(self) -> None:
        """导入默认知识库文档（安全生产法规与制度摘编）。"""
        default_docs = [
            {
                "title": "安全生产法要点摘编",
                "content": (
                    "安全生产法要点摘编。"
                    "安全生产工作坚持中国共产党的领导，坚持人民至上、生命至上，把保护人民生命安全摆在首位。"
                    "生产经营单位的主要负责人是本单位安全生产第一责任人，对本单位的安全生产工作全面负责。"
                    "主要负责人职责包括：建立健全并落实全员安全生产责任制；组织制定并实施安全生产规章制度和操作规程；"
                    "组织制定并实施安全生产教育和培训计划；保证安全生产投入的有效实施；"
                    "组织建立并落实安全风险分级管控和隐患排查治理双重预防工作机制；"
                    "组织制定并实施生产安全事故应急救援预案；及时、如实报告生产安全事故。"
                    "从业人员义务：从业人员在作业过程中必须严格落实岗位安全责任，遵守本单位的安全生产规章制度和操作规程，"
                    "服从管理，正确佩戴和使用劳动防护用品；接受安全生产教育和培训；发现事故隐患或不安全因素立即报告。"
                    "从业人员权利：有权对本单位安全生产工作中存在的问题提出批评、检举、控告；有权拒绝违章指挥和强令冒险作业；"
                    "发现直接危及人身安全的紧急情况时，有权停止作业或在采取可能的应急措施后撤离作业场所。"
                    "事故报告要求：事故发生后，事故现场有关人员应当立即向本单位负责人报告；"
                    "单位负责人接到报告后应当于1小时内向事故发生地县级以上人民政府应急管理部门和有关部门报告。"
                    "处罚要点：未按规定设置安全生产管理机构或配备管理人员的，责令限期改正，可处罚款；"
                    "发生生产安全事故负有责任的，依法承担赔偿责任，并追究相应法律责任。"
                ),
            },
            {
                "title": "隐患排查治理制度",
                "content": (
                    "隐患排查治理制度要点。"
                    "隐患分级：一般隐患和重大隐患。重大隐患指危害和整改难度较大，应当全部或者局部停产停业，"
                    "并经过一定时间整改治理方能排除的隐患，或者因外部因素影响致使生产经营单位自身难以排除的隐患。"
                    "排查方式：日常排查（岗位巡检、交接班检查）、专业性排查（电气、仪表、特种设备专项）、"
                    "季节性排查（防汛、防雷、防冻、防暑）、节假日排查和综合性排查。"
                    "排查频次：生产装置日常巡检每班不少于一次；车间级综合检查每周不少于一次；"
                    "厂级综合检查每月不少于一次；专业性检查每季度不少于一次。"
                    "闭环流程：排查发现 → 登记建档 → 评估分级 → 制定治理方案 → 落实治理 → 验收销号。"
                    "一般隐患应当立即整改，不能立即整改的要明确责任人、措施、资金、时限和预案（五落实）。"
                    "重大隐患治理方案应当包括：治理的目标和任务、采取的方法和措施、经费和物资的落实、"
                    "负责治理的机构和人员、治理的时限和要求、安全措施和应急预案。"
                    "重大隐患要向属地应急管理部门报告，治理完成后组织验收评估方可销号。"
                    "上报渠道：现场报告班组长或安全员、企业隐患上报系统、安全生产举报电话12350。"
                    "企业应当对发现和报告隐患的员工给予奖励。"
                ),
            },
            {
                "title": "特殊作业票证办理指引",
                "content": (
                    "特殊作业票证办理指引（依据GB30871危险化学品企业特殊作业安全规范）。"
                    "特殊作业包括：动火作业、受限空间作业、高处作业、吊装作业、临时用电作业、断路作业、动土作业、盲板抽堵作业八大类。"
                    "动火作业分级：特殊动火（正在运行的生产装置区、罐区等重点部位）、一级动火、二级动火。"
                    "动火分析要求：作业前30分钟内进行动火分析，使用测爆仪时被测气体浓度应小于爆炸下限的20%；"
                    "特殊动火作业过程中应随时监测。动火票有效期：特殊动火不超过8小时，一级不超过24小时，二级不超过72小时。"
                    "受限空间作业：作业前30分钟内取样分析，氧含量19.5%~21%，"
                    "有毒有害物质不超过GBZ2.1规定的限值，可燃气体浓度不超过爆炸下限的10%（或按设计要求）。"
                    "必须先通风、再检测、后作业，作业中持续通风并定时或连续检测。"
                    "监护人要求：动火、受限空间等特殊作业必须设专人监护，监护人不得离开作业现场，"
                    "发现异常立即停止作业并组织撤离。"
                    "高处作业分级：2米至5米为一级，5米至15米为二级，15米至30米为三级，30米以上为特级。"
                    "审批权限：特殊作业票由所属车间审核、安全管理部门审批，"
                    "特殊动火和一级动火还需企业分管负责人批准。"
                    "同一作业涉及两种以上特殊作业时，应同时办理相应作业票。"
                    "作业票一式三联，分别由作业人、监护人、审批人持有，作业结束后及时关闭。"
                ),
            },
            {
                "title": "设备报警与异常处置通则",
                "content": (
                    "设备报警与异常处置通则。"
                    "处置总原则：先保人、再保装置、后保环境；先控制、再查明、后恢复。"
                    "温度超限报警：确认DCS趋势和现场表计一致性，检查冷却系统（循环水、冷冻水）运行状态，"
                    "检查搅拌、换热器结垢情况；超温涉及放热反应时，确认紧急冷却和紧急泄放系统可用，必要时按预案降温降压甚至紧急停车。"
                    "压力超限报警：核对现场压力表与远传仪表，检查安全阀、爆破片状态，"
                    "检查排放/泄放系统是否通畅；超压逼近联锁值时不得随意旁路联锁，确需旁路的按审批流程执行并加强监控。"
                    "液位异常报警：检查变送器导压管是否堵、阀位反馈是否正常，核对进出料平衡，防止抽空或满溢。"
                    "可燃/有毒气体报警器报警：立即携带便携检测仪到现场确认，"
                    "着正压式空气呼吸器和防化服接近，首先控制泄漏源，设置警戒区，禁止无关车辆和火种进入。"
                    "联锁动作后的恢复：查明联锁原因并消除后，按复位程序由授权人员复位，严禁强行短接。"
                    "所有报警处置必须在操作记录中留痕：报警时间、确认时间、处置措施、恢复正常时间。"
                    "发生无法控制的泄漏、火灾苗头时立即启动应急预案，先撤离、先报告。"
                ),
            },
            {
                "title": "危险化学品管理要点",
                "content": (
                    "危险化学品管理要点。"
                    "储存通则：危险化学品应当储存在专用仓库、专用场地或专用储存室内，由专人管理。"
                    "禁忌物料分离储存：性质相抵触、灭火方法不同的物料严禁混存，"
                    "如氧化剂与还原剂、酸与碱、氰化物与酸类必须分库存放并设置隔离措施。"
                    "甲醇、乙醇等易燃液体：库房通风良好，远离火种热源，桶装存放防静电接地，配备抗溶性泡沫或干粉灭火器材。"
                    "液氨、氯气等有毒气体：储存区设气体泄漏检测报警仪，配备正压式空气呼吸器、过滤式防毒面具和堵漏工具，"
                    "氯气库房宜设碱液喷淋吸收装置。"
                    "MSDS管理：危险化学品应当有与其危险性一致的化学品安全技术说明书（SDS/MSDS），"
                    "作业现场应有方便获取的MSDS查询途径，员工应知悉所接触化学品的危险特性、急救措施和泄漏应急处置。"
                    "重大危险源：构成重大危险源的储存设施应当登记建档，定期检测评估监控，"
                    "配备完善的温度、压力、液位等监控和连锁报警措施，并制定专项应急预案备案。"
                    "装卸作业：装卸前核对物料相容性，连接静电跨接，控制流速防止静电积聚，"
                    "装卸作业时操作人员和槽车驾驶员不得离开现场。"
                ),
            },
            {
                "title": "生产安全事故上报流程",
                "content": (
                    "生产安全事故上报流程要点。"
                    "事故分级：特别重大事故（30人以上死亡或100人以上重伤或1亿元以上直接经济损失）、"
                    "重大事故（10~30人死亡）、较大事故（3~10人死亡）、一般事故（3人以下死亡）。"
                    "内部上报：事故发生后，现场有关人员立即向本单位负责人报告；"
                    "单位负责人接报后1小时内向县级以上应急管理部门报告，情况紧急时现场人员可直接报告。"
                    "报告内容：事故发生的时间、地点、单位；简要经过；伤亡人数和初步经济损失估计；"
                    "已经采取的措施；报告人及联系方式。情况发生变化时及时续报。"
                    "事故现场保护：事故发生后，有关单位和人员应当妥善保护事故现场以及相关证据，"
                    "因抢救人员、防止事故扩大需要移动现场物件的，应当做出标志、绘制现场简图并做出书面记录。"
                    "报告时限红线：迟报、漏报、谎报、瞒报事故将依法追究单位负责人及有关人员的法律责任。"
                    "工伤认定：职工发生事故伤害，所在单位应当自事故伤害发生之日起30日内向统筹地区社会保险行政部门提出工伤认定申请。"
                    "应急响应同时启动：人员伤害先抢救（触电先断电、中毒先佩戴防护再施救、灼伤先大量清水冲洗），"
                    "同时在保障施救人员安全的前提下控制事态，防止次生事故。"
                ),
            },
            {
                "title": "个体防护用品选用指南",
                "content": (
                    "个体防护用品（PPE）选用指南。"
                    "选用原则：按岗位危险有害因素识别配置，遵循国家标准GB39800系列（个体防护装备配备规范）。"
                    "头部防护：存在物体打击、碰撞、飞溅风险的作业区域必须佩戴安全帽，安全帽应在有效期内使用，"
                    "帽衬调整合适，破损或受过重击的安全帽立即报废。"
                    "呼吸防护：一般粉尘选自吸过滤式防尘口罩（KN95及以上）；"
                    "有毒气体环境选防毒面具并配对应滤毒盒，滤毒盒按防护介质和保质期更换；"
                    "缺氧环境（氧含量低于19.5%）或未知浓度环境必须使用正压式空气呼吸器，严禁使用过滤式面具。"
                    "眼面防护：打磨、切割、化学飞溅作业佩戴护目镜或防护面屏，接触强酸碱配备耐腐蚀面罩。"
                    "听觉防护：8小时等效声级超过85分贝的岗位佩戴耳塞或耳罩。"
                    "坠落防护：2米以上高处作业系挂安全带，高挂低用，挂点牢固，"
                    "安全带使用前检查缝线、卡扣，坠落悬挂后安全带报废。"
                    "手部防护：化学品作业按MSDS选用耐相应介质的防护手套，高温作业用隔热手套。"
                    "躯干防护：易燃液体区域穿防静电工作服，酸碱区域穿防酸碱服，进入装置区不得穿化纤衣物和带铁钉鞋。"
                    "管理要求：PPE按规定周期发放并记录，班组日常检查佩戴情况，破损及时更换。"
                ),
            },
        ]
        self.add_documents(default_docs)
        logger.info(f"已导入默认知识库: {len(default_docs)} 篇安全文档")
