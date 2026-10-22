# Corpus FS 与检索

> 覆盖：图片检索的事实基线、取图通道、opaque 引用密钥口径、检索服务的隐式依赖与测试约束。
> 多库检索性能台账与实验数据见 §7 与 [experiments/corpus-fs/](experiments/corpus-fs/README.md)。
> 多库检索入口 `POST /api/v1/datasets/corpus-search` 与 `/datasets/<id>/fs/*` 都只在 Python 侧存在，归属见 [ARCHITECTURE.md](../ARCHITECTURE.md)。

## 1. 图片检索的事实基线

### 1.1 图片描述没有独立字段，被折进正文

- 聚合源：`image_caption` / `image_footnote` / `chart_caption` / `chart_footnote` / CHART 的 `content` / 运行期生成的 `vlm_description`，见 `mineru_parser._image_semantic_text()` 与 `_enhance_images_with_vlm()`。
- 有像素的图片块走 media 通道，描述文本写入图片块 `text`，最终落库为 **`content_with_weight`**（`rag/flow/parser/parser.py`、`rag/flow/chunker/token_chunker.py` 的媒体块构造）。
- **结论：要拿「图片描述」，读图片块的 `content_with_weight` 即可，不存在 `image_caption` 字段。**（早期口头汇报给的 `token_chunker.py:104-107` 行号不准确，媒体块构造在 `:99-146`。）

### 1.2 图片块的身份是 `doc_type_kwd == "image"`

设置点：`rag/flow/parser/parser.py`（figure → image）、`rag/nlp/__init__.py`。检索结果默认已带 `doc_type_kwd`。

### 1.3 过滤白名单缺 `doc_type_kwd`，且新增过滤键是全路径高危点

- `rag/nlp/search.py` 的 `get_filters()` 允许的标量过滤键原先不含 `doc_type_kwd`，`retrieval()` 签名也没有该参数 → 无法在检索时限定只要图片。
- `get_filters()` / `retrieval()` 是 **chat 问答、corpus_search、grep 相邻路径共用**的。新增过滤键必须 **opt-in**（仅显式传入时生效），否则会静默改变所有问答的召回行为。

### 1.4 `doc_type_kwd` 做标量过滤在两种引擎都成立（反直觉）

- **ES**：它**不在** `conf/mapping.json` 显式声明，靠动态模板 `^(.*_(kwd|id|ids|uid|uids)|uid|id)$` → `type=keyword`。
- **Infinity**：varchar + `whitespace-#`（`conf/infinity_mapping.json`）。
- 故 `condition={"doc_type_kwd": "image"}` 在 ES / Infinity 均可正确过滤。

### 1.5 图片本体不向量化

ES 中只有 `*_vec` 是 `dense_vector`，没有任何 img / image 向量字段。**不存在「以图搜图」，「图片检索」实为对图片描述的文本检索。**

## 2. 取图通道

- `image_ref` **不是**明文 id，而是 `HMAC(secret("reference"), f"{document_id}:{image_id}")` 的十六进制摘要（`CorpusFsService._image_ref()`）。换密钥即全部失效，见 §3。
- `CorpusFsService.image_data(document, image_ref)` 默认按三条路径查找，逐条要求 `image_id` 以 `f"{dataset_id}-"` 开头：
  1. **projection**：`uses_source_projection()` 识别的 PDF、Office、图片取 ACTIVE projection 的 block `image_id` 比对。
  2. **chunk**：遍历该文档 chunk 的 `img_id`；前缀不匹配直接 `break`（不再继续找），随后落到 manifest。
  3. **manifest**：`load_manifest()` 的图片 id 必须前缀匹配**且** `object_name` 以 `inline/` 开头。
- 无可用投影时，naive chunk 的 `image2id(..., bucket=ctx.kb_id)` 满足 `<dataset_id>-` 前缀；**canvas chunk 路径 `rag/flow/parser/parser.py` 用默认 bucket `imagetemps`，前缀不匹配 → 该通道取不到图**。
- 对象键形如 `<dataset_id 桶>/source-projections/<doc_id>/<rev>/images/<block_ref>.png`，取图时用 `STORAGE_IMPL.get(dataset_id, object_name)`。
- `/fs/image` 可带 `citation_ref`：解签后按稳定文档定位重新授权，再按引用的 projection revision 与 source hash 查图；指定投影后不得回落到当前 chunk。非投影引用验证当前原件 hash。重命名后仍按当前路径授权；已删除的投影或对象明确失败，不扫描全部历史版本。
- `/fs/content` 的块与顶层图片均保留对应的引用和页码，多图块通过 `images` 表达。UTF-8 原文、chunk 与 ACTIVE/历史投影块中的内嵌 data URL 资产化后进入结构化图片列表；引用预览先完成相同替换再按字节范围裁剪。资产化失败仍可能留下原始 data URL，调用方不能把它当作成功的图像交付。

### 2.1 图文证据复审修复（2026-10-17，开发栈验证通过）

- 现象：检索 chunk 的预览图配合 projection citation 取图 404；Office 投影表格仍含 Base64；`paragraph` 过滤排除存储类型为 `text` 的正文。
- 根因：图片身份来自 chunk，但引用定位的是独立复制的 projection 资产；读路径只在原文与 chunk 资产化内联图；公开块类型未统一。
- 处理：检索与 grep 完成引用定位后，从同一快照的目标块生成图片引用与资产化正文；排序与去重仍使用原检索片段。正文、引用范围、预览和取图共用投影块媒体转换；历史内联图的成员资格从指定投影正文推导，不接受当前或聚合 manifest 中的其它版本图片，也不改变 source hash。块过滤与输出共用 `text -> paragraph` 规范化。
- 验证证据：`test/unit_test/api/apps/services/test_corpus_evidence_regressions.py` 覆盖 PDF/DOCX 实际检索服务至版本取图、投影内联图 UTF-8 分页/预览、旧版本成员隔离以及类型过滤。修复前四个场景均失败。
- 真实验证：RAGFlow 相关集合 143 项通过；开发栈经认证 2026 入口验证现有 PDF、DOCX 检索图片与引用配套下载；新 DOCX 经真实 MinerU 解析，投影表格内联 PNG 进入模型输入（19,780 字节，SHA-256 已归档）。本地视觉模型正确识别 K7M4、3 个蓝圆与 1 个红方块。历史样本替换重解析后，投影 1→2，新旧引用取回各自像素；新图配旧引用 404，删除后旧引用 404。`parsed_chunks` 实际文档按 `paragraph` 筛选返回 21 个正文块。证据在 LocalAgentOps 开发日志「2026-10-17_CorpusFS_修复与E2E」。
- 残余风险：资产化仍是 best-effort，存储失败可能保留 data URL；历史投影正文含完整内联字节时，可重新生成丢失的衍生对象。独立图片对象或历史投影缺失仍失败。认证浏览器已验证当前图文展示与引用入口，但图片请求尚不携带版本引用，未验收历史图片版本固定展示；未作生产部署或业务验收。

## 3. opaque 引用的密钥口径（改引用/游标前必读）

`api/apps/services/corpus_secret_service.py`：

- `corpus_secret(purpose)` = `HMAC-SHA256(root, "corpus-fs:" + purpose)`，purpose 目前有 `reference` / `citation` / `page-cursor` / `delete-confirmation`。**按用途派生，不共用同一把密钥**（历史问题「多个用途共用同一密钥」已修）。
- `root` = 环境变量 `RAGFLOW_CORPUS_SECRET`，否则 `settings.get_secret_key()`；不足 32 字节直接抛 `CorpusSecretError`。
- `settings.get_secret_key()` 的 `RAGFLOW_SECRET_KEY` 分支是**注释掉的**，实际走 `REDIS_CONN.get_or_create_secret_key("ragflow:system:secret_key", secrets.token_hex(32))` → **随机生成 64 位十六进制并存 Redis**。
- 由此得两条不变的运行口径：
  1. 可用至少 32 字节的 `RAGFLOW_CORPUS_SECRET` 固定 Corpus 根密钥；未设置时才依赖 Redis。`RAGFLOW_SECRET_KEY` 不是该覆盖项。
  2. 使用 Redis 回落时，该根密钥丢失会让**所有已签发的 image_ref / citation / 分页游标 / 删除确认串同时失效**；显式根密钥变更同样导致旧引用失效。运维不得随意 flush，也不能在本轮优化中静默切换密钥来源。

## 4. 检索服务的隐式依赖（改动前必读）

### 4.1 `LLMBundle` 的 `model_config` 契约

- `LLMBundle`（`api/db/services/llm_service.py`）继承 `LLM4Tenant`（`api/db/services/tenant_llm_service.py`），后者在 `__init__` 强制要求 `model_config` 含 **`llm_name`** 与 **`model_type`**，并按 provider 实例解析模型，缺失直接断言失败。
- **坑**：`settings.RERANK_CFG` 的结构是 `{model, factory, api_key, base_url, max_model_len}`，**缺 `llm_name` / `model_type`**，不能直接喂给 `LLMBundle`。
- **正确路径**：经 `get_model_config_from_provider_instance(tenant_id, LLMType.RERANK, model_name)`（`api/db/joint_services/tenant_model_service.py`）解析。它会先匹配 `_configured_system_default`（`settings.RERANK_CFG.model` 命中时）→ 返回含 `llm_factory` / `api_key` / `llm_name` / `api_base` / `model_type` / `max_tokens` 的完整配置，**无需 tenant 注册模型**；否则按 tenant 模型池（Provider + Instance + Model）查找，缺失抛 `LookupError`。

### 4.2 `dataset_api_service.search` 的测试 mock 链

调用 `await dataset_api_service.search(...)` 必须 mock 掉下列依赖，否则真实连库：

- `KnowledgebaseService.accessible` / `get_by_id` / `query`
- `UserTenantService.query`（`search()` 内真实查库构建 `tenant_ids`）
- **`LLMBundle` 是函数内 import**（`from api.db.services.llm_service import LLMBundle`）→ monkeypatch 必须打 `api.db.services.llm_service.LLMBundle`，直接 patch `dataset_api_service.LLMBundle` 无效。
- `get_tenant_default_model_by_type`（同为函数内 import；`kb.embd_id` 为空时用于 embedding）
- `settings.retriever.retrieval` 是 **async**，mock 必须 `async def`；`retrieval` 前 8 个参数是位置参数。
- `label_question`（函数内 import `rag.app.tag`）

## 5. 语义检索的排序与重排口径（含跨语言）

链路：`/fs/search` → `dataset_api_service.search()` → `rag/nlp/search.py` 的 `retrieval()`。

**rerank 回落**

- 请求没带 `rerank_id` 时，`dataset_api_service.search()` 会回落到**系统默认 rerank 模型**（`settings.RERANK_MDL`，即 `user_default_llm.default_models.rerank_model`）：经 `get_model_config_from_provider_instance(tenant_id, LLMType.RERANK, ...)` 解析出含 `llm_name` / `model_type` 的完整配置再构造 `LLMBundle`；配置缺失或解析异常被捕获 → 保持无 rerank。
- 因此「corpus fs 请求不带 rerank_id ⇒ 没有重排」这个旧结论**已不成立**，排障时先看日志里的 `rerank_enabled`。

**候选窗口**

- `_rerank_window(page_size, top)` 决定检索的 block 大小，同时也是从重排结果里切页的模数：`page = global_offset // window`、`begin = global_offset % window`。**窗口必须是 `page_size` 的整数倍**，否则 block 与 page 漂移，深分页会静默丢结果、返回短页。
- 目标池约 64 个候选，有外部 reranker 时被 `top` 收紧，并且总是向上取整到整页。`/fs/search` 的 `top_k` 入参上界是 50。

**排序信号**

- `sim = tk * tksim + vt * vtsim + rank_fea`。`tksim` 是 token 覆盖率相似度（同语言精确，**跨语言 ≈ 0**）；`vtsim` 无 rerank 时是 embedding cosine，有 rerank 时是交叉编码分；`rank_fea` 是 tag 余弦 + pagerank（默认 0，加性）。
- 首查融合是 `FusionExpr("weighted_sum", {"weights": "0.05,0.95"})` → **BM25 只占 5%**。
- `vector_similarity_weight` 默认 0.3 → `tk=0.7` / `vt=0.3`，即**无 rerank 时模型分只占 30%**。

**跨语言检索（2026-08-12 实测，未修）**

- 现象：中文为主的 query 对英文论文相似度只有 0.22~0.23、贴着 0.2 阈值，被压出候选池，全库 top-50 召回 0 条；英文 query 可达 0.42 / 15 命中；中英混合 0.355 / 12 命中。
- 推定根因链：跨语言向量相似度低 + BM25 跨语言失效（`t≈0`）+ 首查融合权重失衡（0.05 / 0.95）+ 重排时 `tk=0.7` 对跨语言是**负贡献**。
- **双栈中文判定不一致（仍然存在）**：Python `rag/nlp/__init__.py` 的 `is_chinese()` 按**汉字字符占比 > 0.2**；Go `internal/service/nlp/query_builder.go` 的 `IsChinese()` 按**空白分隔字段占比 ≥ 0.7**。同一个 query 在两侧可能被判成不同语言，进而走不同分词与权重路径。
- 已知薄弱点：阈值 0.2 与混合尺度 `sim` 的可比性存疑；`retrieval_by_children` 之后同文档多 chunk 平铺导致主题重复；`cross_languages` 是**替换** query 而非双语共存；端点也没有暴露 `cross_languages` / `keyword_extraction`（LLM 翻译与关键词扩展机制存在但未被 `/fs/search` 使用）。

## 6. 写路径、修订号与完整性契约

### 6.1 写路径：`If-Match` + 行锁事务

- 所有 Corpus 写接口都经 `_commit_content_mutation()`：`If-Match` 头**必填**且必须是正整数 revision（缺失 412，非法 400）。
- `content_mutation()` 在 `DB.atomic()` 内先 `SELECT ... FOR UPDATE` 锁住 dataset 行、比对 revision，再执行 mutation 体，最后用条件 `UPDATE` 递增 `content_revision`（影响行数 ≠ 1 → `RevisionConflictError`）。
- mutation 体经 `_run_sync_io` 在 worker 线程执行，不阻塞事件循环；但**事务与行锁覆盖整个 mutation 体**。不要在体内做长耗时的对象存储、索引或队列等待。
- revision **只在 mutation 成功返回后递增**：抛异常即回滚，revision 不变，因此同一 revision 可以重试。

### 6.2 读路径严格只读

`_dataset_file_root()` 是纯查询（沿祖先链定位 dataset 目录，取不到就按 dataset 名回落），目录结构缺失返回 `NOT_FOUND`。**读请求不得初始化或重命名目录**；`_in_mutation` 期间不复用投影缓存。

### 6.3 完整性契约现状

- `/fs/content` 的 `max_bytes` 硬限制返回正文的 UTF-8 字节；优先保留完整块，超大首块按完整 UTF-8 字符切片。签名 cursor 保存块索引与块内字节位置，续读保持原 `block_ref`，引用 range 计入 `block_byte_start`。不足容纳下一个字符时明确报错，不静默丢内容。
- 单次块数上限为 `min(64, max(1, max_bytes // 512))`，纯图空块也受限。`budget` 报告正文实际字节、正文上限、块上限与图片数；它不是整个 JSON、token 或像素预算。
- `/fs/content` 的分页语义：响应始终带 `page_count`（投影的最大页号）。请求页**不在投影页集合里**时返回 200 + `page_out_of_range: true` + 空 `blocks`，不再报 415——越界翻页是「读完了」，不是「内容读不了」；页存在但被 `type` 过滤空仍是 415 `UNSUPPORTED_CONTENT`（空过滤）。调用方据此在末页收口，不必再探一次越界页。投影的 `page` 来自解析产物：一份 37 页的 docx 可能只有 12 个页号，且单页可远超一次响应的字符预算（实测 247 块 / 2.4 万字符），按页读取必须允许页内用块序号续读。
- MCP tree/find/read/grep 暴露 cursor；read 可按页与块类型过滤。read / citation preview 默认从授权接口下载并返回原生图片块，可用 `include_images=false` 禁用；`image_delivery` 逐图报告状态与失败原因，最多 6 图、单图 5 MiB、合计 20 MiB，只接受签名与 MIME 一致的 PNG/JPEG/GIF/WebP。
- DeerFlow 批量 `kb_cat` 的续读位置属于各文件；本地裁剪时从该文件本次输入 cursor 重试并缩小范围，不能用上游下一页位置跳过尚未交付正文。
- `/fs/grep`：cursor 按**本页实际消费的 ES hit 数**（`consumed_count`）推进，而不是映射成功的条数；响应带 `returned_count` / `consumed_count` / `total` / `truncated`。
- `/fs/search`：响应带 `schema` / `items` / `total` / `truncated` / `continuation`（`refine` | `terminal`）/ `search_meta`。**`total` 是「去重 + 每文档限流之后、按 `limit` 截断之前」的条数，不是候选总数**；候选总数看 `search_meta.raw_candidate_count`。
- 语义检索是 **Top-K 非穷举**，不能用来回答「全部 / 所有」类问题；需要查全时要另建编排，不要改变 `/search` 的语义。

### 6.4 已知缺口（改动前自行兜底）

- **外部副作用不在事务内**：mutation 体里的对象存储、索引与任务队列动作不受 `DB.atomic()` 保护。DB 回滚撤不回已上传的对象、已删除的索引或已投递的任务，且 revision 不变 → 会留下孤儿对象，且重试同一 revision 是被允许的。仓库内**没有** outbox / 补偿 / 操作状态机，删除部分成功只通过 `error_count` 反映。
- **revision 事件覆盖不全**：`CorpusRevisionService.bump_content` 只被 `document_api_service.py` 的 3 处调用；移动、改名、权限变更、projection `ACTIVE`/`RETIRED` 切换都不会递增 revision → 旧 cursor、旧缓存与旧路径投影可能继续命中。
- **dataset 改名不更新目录、也不递增 revision**；历史缺失的目录结构只有明确报错（`NOT_FOUND`），没有可审计的修复入口。
- **重解析期间旧检索结果不保证可用**：阅读投影与 Chunk 检索索引走不同发布路径。已有 ACTIVE source projection 可独立读取，Corpus FS 在文档 `RUNNING` 时优先返回它，所以**旧阅读投影在重解析期间仍可预览**（由 `test_pdf_content_keeps_active_projection_readable_during_reparse` 固化）；但 ingestion 会在重解析开始前删除旧 Chunk 索引，**没有 active/pending 代际切换机制**，旧检索结果无法保证服务到新结果成功。正确修复需要解析代际或影子索引：新一代完整成功后原子发布并清理旧代，**不能同时混用新旧 Chunk**（会产生重复与版本不一致的检索结果）。批量 retry / cancel 统一走 `POST /datasets/{dataset_id}/fs/parse` 并返回逐路径结果。
- 尚未验收：跨租户 / 跨 dataset 授权、并发 `If-Match`、丢映射时的多页 cursor 不重复、大目录性能（1K / 10K / 100K）。

### 6.5 保留的设计边界

- 调用方只用**展示路径**；`document_id` / `file_id` / `chunk_id` / 对象存储位置留在服务端。
- `/blob`（不可变原件）、`/content`（source projection）、Markdown 派生版本、检索 Chunk 是**四种不同生命周期**，不要合并成一个模糊的「文档内容」。
- literal grep 不初始化模型、不自动退化为语义检索；精确性（grep）与完整性（search / comprehensive）不混在同一个接口里表达。
- citation 是**证据版本定位**（可读 RETIRED projection，重命名后仍可重新授权），不是分页 cursor。
- RAGFlow 不生成会话级 `[[idx]]`、不负责 LLM 展示文本与多轮调度；那些属于 DeerFlow 侧。

## 7. 多库检索：性能台账与并发修复

> 逐版本的实验记录、原始数据与采集协议在 `engineering/experiments/corpus-fs/`；本节只留结论与口径。

### 7.1 起点与根因

多库检索（`POST /api/v1/datasets/corpus-search`）随库数线性变慢：12 库 17.95s、16 库 23.20s，且 8 库请求会把整个 API 服务冻结 7.77s（同机无关请求 0.07s → 13.4s）。三条根因：① 每库完整跑一遍单库检索（含重排）；② 重排在事件循环线程内**同步**执行；③ 每库全量重建目录投影（N+1 查询 + 3 次全量文档取数）。

### 7.2 版本与效果（每个版本可独立验收）

| 版本 | 改动 | 效果 |
|---|---|---|
| V1 | 同步段移入 `thread_pool_exec` | 事件循环最大停顿 7.77s → **0.16s** |
| V2 | 目录投影改「按层级批量取数 + 内存建树」，删 N+1 与重复取数 | 逐字段等价；单库列举 −35%~−70%；SQL 15 → 12（嵌套库 53 → 16） |
| V3 | 新增 `corpus_recall_service.py`：一次严格检索 + 两段式逐库恢复 + **一次**全局重排 | rerank 调用 N → **1**；L(12) 17.95s → 6.37s |
| V3.1 | 恢复/兜底判定改用按 scope 的**可见**命中数 | 子目录 scope 零候选修复，候选覆盖 4/12 → 12/12 |
| V3.2 | `FulltextQueryer.token_similarity` 文档侧只构造键集 | 打分逐条等价；CPU 3.26s → **0.029s** |
| V3.3 | `/fs/grep` 文档过滤上限 1000 → 20000 | 1912 文档库由 HTTP 400 变 200 |
| V3.4 | 新增 `corpus_context_cache.py`（按数据层缓存数据集上下文） | `context` −90%；L(12) 0.93s |
| V3.5 | 引用后置：排序只用 chunk 页码，源对象只读返回项 | 16 库 citation 3.83s → 1.20s |
| V3.6 | 候选池按 scope 配额（12 库→5 / 16 库→4 / 50 库→2，上界 540 → 140） | 排序变化**只经人工评审**，当时无标注集 |
| V3.7 | 恢复窗口并发上限 ≤ 8 | 50 库典型停顿 0.11~0.41s → 0.06s |
| V3.8 | 引用 manifest 按 `document.content_hash` 缓存 | 16 库 −44%、50 库 −42%；`source_hash` / `citation_ref` 逐位不变 |
| V3.9 | 修尾延迟两处根因：PDF 源投影每次重读重解析 → 新增 `source_projection_cache`（键含 `object_name` 与投影版本，内容不可变），典型 −92%；陈旧连接重试前固定睡满 1s → 抽 `_retry_backoff`，首次重试不 sleep | L(12) 暖 p50 **0.860 → 0.750s（−12.8%）**；`tail_events` **16/100 → 0/100** |

### 7.3 终态与判定口径

- 新协议 12 互异库：`L(1/2/4/8/12)` 暖中位数 **0.11 / 0.17 / 0.25 / 0.65 / 0.95s**；邻扰 max **0.070s**；rerank 调用恒为 1；12 库 SQL 20；MySQL 连接增量 +1/+2。
- 质量：cmrc2018 金标 12 库 **Hit@10 0.86 / MRR 0.5223**，只查金标库的上界是 0.96 / 0.6987（**多库融合差距 10pp**）。
- **口径纪律（判读指标前必读）**
  - **V0 在新协议下不可得**（代码已被 V1~V3.8 覆盖）；旧口径的 `L(12)` / `L(16)` 与现口径**不可直接比较**（旧「12 库」实为 8 个互异语料，含逐字节相同的重复库）。
  - `L(4)` **不可判定**（噪声地板 CV 0.686），该档不得用于判定改善/回归。
  - 判定用 `warm_p50` + `mdd_robust`（抗尾）；`tail_events` 增加即视为回归。尾部是混合体，必须同时看阶段分布与进程 CPU 以区分「在算」与「在等」。

### 7.4 未决问题

- **50 库偶发停顿未消除**：跨运行 max 0.38s（citation 归因），与 V3.7 的 0.41~0.42s 同量级；「<0.10s 门槛」按跨运行 max 口径**未达成**。
- **标注集只有 v1**（12/20 条客观可判定且 query 偏易，把主题词当查询），对排序退化的敏感度未校准 → V3.6 的配额取值（64 vs 120）**仍不能定档**。
- **加深候选池反而更差**：`POOL_BUDGET_CANDIDATES` 64 → 120 实测 Hit@10 −4pp、MRR −0.023、p50 +24%（该 A/B 在重复库清理**之前**做的，需在 12 互异库上重做才能定论）。当前维持 64。
- **跨 scope 重复占位不是质量损失的原因（已证否）**：top-10 里同 basename 重复占 42.6% 的槽位，但同库数下「把重复副本换成别的库」与「直接少两个库」的 Hit@10 完全相同 → 病因是**库数**；**不做**内容/跨 scope 去重（那会推翻 `test_same_file_name_in_two_datasets_is_not_cross_deduplicated`）。
- `/fs/tree` 的 limit 上限落在 **(200, 224]**：`limit=200` 正常、`limit=224` 起返回 400 `INVALID_ARGUMENT`，无法一次列举 > 200 个条目（设计意图未确认）。
- **按文件名检索可能失效**：一个 query 未召回库内确实存在的同名 PPT，**根因未验证**（疑为路径/文件名不参与检索）——**不要据此改代码**。
- 成对交替 A/B 仍缺（无 tag/单 commit，无法在同会话内切换两个版本）；冷启动已定档接受（12 库 1.89s / 16 库 2.58s），**不改 `source_hash` 契约**（会动引用签发语义）。

### 7.5 并发共享 MySQL 连接（已修）

- 现象：DeerFlow 同一轮并发两个 `knowledge_search`，一个成功、另一个返回 HTTP 400 `INVALID_ARGUMENT`；四路相同只读请求稳定复现 2 成功 / 2 失败，真实异常是 `OSError(9, 'Bad file descriptor')`。
- 根因：`CorpusToolService.search` 在 Quart 事件循环线程里**同步**读 Corpus 路径与 citation projection。Peewee/PyMySQL 的连接状态**按线程**保存，并发协程因此共享同一 socket：一个请求的连接恢复或关闭，会让另一个请求随后在 `SourceProjectionService.active()` 时用到已关闭的文件描述符。通用错误处理又把底层 I/O 异常压成 400 `INVALID_ARGUMENT` 或 HTTP 200 业务错误，下游只能显示 `UPSTREAM_UNAVAILABLE`。
- 处理：批量 Corpus 检索把 `search_context` 与 `citation_context` 这两个同步 ORM/对象存储边界移到工作线程，使并发请求使用线程隔离的连接；`InterfaceError` / `OperationalError` / `OSError` 统一转 **503 `SEARCH_UNAVAILABLE`**，不再伪装成参数错误。
- 残余风险：Peewee 的全局连接状态仍按线程而非协程隔离；本次只收敛了批量 Corpus 检索里已确认的同步 I/O 边界。**其他长耗时 Quart 路由若在一次请求内跨 `await` 保留数据库连接，仍需独立审计。**

### 7.6 批量检索路由 405（已修）

- 现象：DeerFlow `knowledge_search` 返回 502 `UPSTREAM_UNAVAILABLE`，消息为 `MethodNotAllowed '405'`；但 RAGFlow 传输层对 `POST /api/v1/datasets/corpus-search` 返回 HTTP 200、响应体里是业务错误 405。
- 根因：运行中的容器在新增该路由**之前**就已启动，而 **Quart 的路由表不会因 bind mount 的文件变化而更新**；运行时 `OPTIONS` 只允许 `GET/PUT/HEAD`，请求落到 `/datasets/<dataset_id>` 的方法集。同时新路由依赖的 `corpus_tool_service.py` 未加入 overlay，直接重启会让 API 进程在导入阶段失败。
- 处理：overlay 显式挂载 `corpus_tool_service.py`，只强制重建 `ragflow-gpu`。重建后 `OPTIONS /api/v1/datasets/corpus-search` 已包含 `POST`。
- 结论：**新增或改动路由后必须重建容器，重启不够**（详见 [runtime-env.md](runtime-env.md) §1）。

## 8. 回归测试

| 关注点 | 测试 |
|---|---|
| 检索服务与 mock 链 | `test/unit_test/api/apps/services/test_dataset_api_service.py` |
| Corpus 检索接口 | `test/unit_test/api/apps/restful_apis/test_corpus_search.py` |
| Corpus 工具服务 | `test/unit_test/api/apps/services/test_corpus_tool_service.py` |
| MCP 工具清单 | `test/unit_test/mcp/test_server_datasets.py` |
| source projection | `test/unit_test/api/apps/services/test_source_projection_service.py` |
| 多格式阅读、旧图定位、UTF-8 分片引用、内嵌图片 | `test/unit_test/api/apps/services/test_corpus_fs_service.py` |
| 越界页 vs 空过滤的分页语义 | `test/unit_test/api/apps/services/test_corpus_fs_service.py::test_page_outside_the_projection_reports_the_real_page_count`、`::test_page_inside_the_projection_but_empty_for_the_filter_stays_unsupported` |
| 取图重新授权、读路径线程隔离 | `test/unit_test/api/apps/restful_apis/test_corpus_fs_read_isolation.py` |
| 签名游标与引用 | `test/unit_test/api/apps/services/test_corpus_cursor_service.py`、`test/unit_test/api/apps/services/test_corpus_citation_service.py` |
| 内嵌图片资产化 | `test/unit_test/api/apps/services/test_inline_image_assets.py` |

同库文件名不跨库去重是**有意契约**，由 `test_same_file_name_in_two_datasets_is_not_cross_deduplicated` 固化，不要为「跨 scope 重复占位」改检索契约。

## 9. Agent 图文证据优化记录

状态：代码与隔离验证通过，运行验收待执行；日期：2026-10-16。

- **现象**：`kb_cat` 有图片引用，通用 Agent 与 MCP 调用方仍拿不到图像；Office 阅读绕过投影，超大块突破正文预算，旧引用的图片可能误读当前版本。
- **根因**：投影读侧只认 PDF，内嵌图缺结构化发现；媒体下载只接入专用知识问答；正文分页只有块索引，批量工具丢失逐文件续读位置；取图未接入已有版本引用。
- **处理**：统一投影选择，保留内嵌图片与引用版本，签名 cursor 增块内字节位置；MCP 交付原生图片，DeerFlow 按模型视觉能力注入授权图片并明确失败状态。边界见 §2 与 §6.3。
- **验证证据**：阶段测试 RAGFlow 图文交付集合 78 项、DeerFlow 图文/装配集合 131 项通过；覆盖 MockTransport 的授权与原生像素、ModelRequest 图片、无视觉不下载、旧投影、失败和媒体预算。阶段结果有重叠，不能累加为独立用例数；最终回归以开发日志中的汇总为准。
- **残余风险**：未重建容器、未批量重解析旧文档、未执行认证入口与真实视觉模型验收；历史语料缺失的 projection 不会自动补齐。无投影的 chunk 媒体仍受 bucket 前缀约束，已删除对象不可回读；source projection 与检索 Chunk 的发布代际问题仍见 §6.4。

## 10. PDF 引用定位复用

状态：代码与隔离验证通过，部署验收待执行；日期：2026-10-16。

- **现象**：PDF 候选在排序去重前定位一次，入选后签发又重复定位，4 候选 / 2 返回需 6 次匹配。
- **根因**：候选内部 source 记录未保留已计算 target；前一次定位不可删除，因为 projection 会修正页码并影响去重。
- **处理**：仅在一次 search 内携带并复用 PDF target；非 PDF 仍只对入选项读取来源。公开返回字段、排序、跨库同名来源、source_hash 与引用签发语义保持。
- **验证证据**：`test_pdf_targets_are_computed_once_and_preserve_corrected_dedup_and_signed_fields` 使用真实位置匹配与 cit1 签发/解签，4 候选 / 2 返回定位 6→4；`test_pdf_target_reuse_is_request_local_and_observes_new_projection_version` 验证下一请求观察新版本。tool/FS/citation 聚焦集合 80 项通过。
- **残余风险**：未减少所有 PDF 候选的首次匹配，未实测生产 CPU/p50 或模型端视觉质量；跨请求投影失效和解析代际不在本轮修改范围。DeerFlow 条件图片请求仍逐次调用此侧授权与来源读取，不在 RAGFlow 缓存用户权限。
