# 解析链路（MinerU / DeepDOC / Dataflow）

> 覆盖：MinerU 路由与产物形态、图片进入 chunk 与 source projection 的通道、单文档解析关键路径性能。
> 归属与路由见 [ARCHITECTURE.md](../ARCHITECTURE.md)；overlay 挂载与重建口径见 [runtime-env.md](runtime-env.md)。
> 引用代码时优先看符号名；行号是记录时点，会漂移。

## 1. 路由事实

- `MINERU_SUFFIXES` = PDF + 8 种图片（png / jpeg / jp2 / webp / gif / bmp / jpg / tiff）+ docx / pptx / xlsx（`common/mineru_formats.py`）。
- `is_mineru_file()` 是格式判定谓词；`uses_source_projection()` 与它同值，是「该文件是否需要 source projection」的**唯一谓词**。历史上写侧门禁是 `.pdf` 硬判断，已收敛到该谓词。
- `rag/app/parser_router.py` 的 `effective_parser_id()` 对 MinerU 文件**强制返回 `naive`**，任务创建与执行都按此判断。
- `rag/app/naive.py` 中 MinerU 分支是 elif 链第一分支；链中**没有** `.docx`（只有 `.doc`），所以 docx 只能进 MinerU。
- canvas 场景走 `rag/flow/parser/parser.py` 的 `is_mineru_file` → `_mineru`，与 naive 路径**并行且行为不同**（图片 bucket 不同，见 §3）。

## 2. MinerU 产物形态：PDF 与 office 分支不同

### 2.1 office（docx / pptx / xlsx）

- ZIP 内为 `<stem>/office/<stem>_content_list.json` 与 `<stem>/office/images/<sha256>.jpg`。目录名是 **`office`**，不是 `hybrid_auto`；读取侧靠 `**/<stem_dir>/<parse_subdir>/*_content_list.json` 一类 fallback 才能命中（`mineru_parser._read_output`）。
- content_list 里图片是**独立 item**：`{"type":"image","img_path":"images/<sha>.jpg","image_caption":[],"page_idx":0}`；表格 item **无** `img_path`（`table_body` 是 HTML）。
- 图片引用**只**出现在 `img_path` 字段（相对路径），不在正文 Markdown 中——md 里只有 `![](images/<sha>.jpg)`。`mineru_parser` 会把 `img_path` / `table_img_path` / `equation_img_path` 按 json 所在目录 resolve 成绝对路径。
- **office 分支不返回任何图片描述**：`image_caption` / `content` / `chart_caption` 恒为空，`image_analysis=true/false` 的输出 diff 为空（2026-09-28 实测，`return_md` / `return_middle_json` / `return_model_output` 全开也一样）。

### 2.2 PDF

- hybrid-engine 会返回图表解析：`{"type":"chart","content":"| ... |","chart_caption":[...],"bbox":[...]}`。`_image_semantic_text()` 消费 `chart_caption` / `chart_footnote` 与 CHART 的 `content`，因此 PDF 图片有非空语义文本，能成块并被检索命中，bbox 也正常返回。
- 同一 PDF 换 `backend=pipeline` 时 caption 识别明显更弱（`chart_caption: []`、`content: ""`，图文落到 `chart_footnote`）。**不要用 pipeline 的输出推断 hybrid 行为。**
- 页面渲染必须复用全局 PDFium 锁：并发解析时否则出现 `PdfiumError: Data format error`，且渲染失败会同时失去 chunk 预览图与坐标归一化所需的页面尺寸。

### 2.3 视觉模型

- 本部署 `tenant_llm` / `llm` 两表 0 行，`conf/service_conf.yaml` 的 `image2text_model` 为注释 → 没有可用的 IMAGE2TEXT 模型，`_enhance_images_with_vlm()` 实际不执行（该链路代码已存在）。
- 结论：docx 要**恢复图片本体**不需要视觉模型（MinerU 已返回 jpg，属代码修复）；要额外获得**可检索的图片描述**，只能靠文档题注或注册 IMAGE2TEXT 模型，属可选增强。

### 2.4 历史坑：MinerU Hybrid 停在 43.75%（已修）

- 现象：MinerU 已返回 ZIP 并打印 `Parsed N blocks from PDF`，但 ingestion pipeline 长时间停在 43.75%。
- 根因：Parser 在 MinerU 完成后**又隐式用租户默认 IMAGE2TEXT 模型**逐个处理图片和表格，单次视觉调用可能耗时 30~60 秒。43.75% 是 File 完成与 Parser 局部进度折算出的总进度，**不是 MinerU 服务卡住**。
- 修复：`rag/flow/parser/utils.py` 仅在显式配置非空 `vlm.llm_id` 时执行媒体视觉增强，不再自动回退租户默认视觉模型。MinerU hybrid 默认不会重复执行第二轮 VLM。
- 判据：修复后新任务在 `Parsed 28 blocks from PDF` 后约 2 秒完成，不再出现 `VisionFigureParser` 二次处理。

## 3. 图片进入 chunk 与 source projection 的通道

**naive 路径**

- `_mineru_section_images()`（`rag/app/naive.py`）把**无 caption 的无文本媒体**挂到最近的文本 section，走 `tokenize_chunks_with_images` 生成带图 chunk；有 caption 的媒体仍走 `tokenize_table` 的独立 image chunk 路径（PDF 行为不变）。
- 为什么按 `suffix != "pdf"` 分叉：MinerU 只为 PDF 渲染 `page_images`，非 PDF 的 `crop()` 恒返回 `(None, None)`。PDF 继续走共享尾部，保留 crop 预览图与真实 `page_num_int` / `position_int`。
- 顺序信号：docx 全部 `page_idx=0` 且无 bbox，位置排序无法还原阅读序，因此 `source_projection_media_indexes` 暴露 MinerU 输出数组顺序作为**唯一**顺序信号（docx 的 media 均为「无 rows」的图片，故全部走上述挂载规则）。
- 历史上图片会在 4 处被静默丢弃，改动该链路时逐点确认：`mineru_parser` 对 IMAGE/CHART 且 `img_path` 存在时 `continue`（不进 sections、不进 projection_sections）；`_transfer_to_tables` 给图片的 text 为空串；`tokenize_table` 的 `if not rows: continue` 直接跳过空文本媒体；`rag/utils/base64_image.py` 只在 `d["image"]` 存在时才写 `img_id`。

**source projection**

- 写入侧门禁**已放开**：`rag/svr/task_executor.py` 与 `rag/svr/task_executor_refactor/chunk_service.py` 都用 `uses_source_projection()`，不再只认 `.pdf`。
- 读取侧**仍**要求 `document.type == "pdf"`（`api/apps/services/corpus_fs_service.py` 三处：projection 选择、block 读取、`image_data`）。**所以非 PDF 图片走的是 chunk 通道，不依赖 projection 读路径。**
- 图片对象键为 `<dataset_id>-<object_name>`：`CorpusFsService.image_data()` 只接受以 `f"{self.dataset_id}-"` 开头的 `image_id`，chunk 通道前缀不匹配时直接 `break`。
  - naive 路径用 `image2id(..., bucket=ctx.kb_id)` 满足前缀。
  - **canvas 路径**（`rag/flow/parser/parser.py`）用默认 bucket `imagetemps`，前缀不匹配 → `fs/image` 取不到图。
- projection 落盘形如 `<dataset_id 桶>/source-projections/<doc_id>/<rev>/images/<block_ref>.png`，block 自带 `image_id`。

## 4. 单文档解析关键路径性能

基线：7 页简历、272 chunks、`DOC_ENGINE=elasticsearch`，总耗时约 **159s**。

| 阶段 | 实测 | 占比 | 性质 |
|---|---|---|---|
| TOC | 102.7s | 65% | **关键路径主导** |
| ES indexing | 71.31s | 45% | 配置型（纯等待） |
| 3 张图片 VLM 描述 | 25.9s | 16% | 质量换速度 |
| MinerU 远程解析 | 20.7s | 13% | 外部服务 |
| chunk 构建 | 4.2s | 3% | 正常 |
| embedding | 3.49s | 2% | 接近最优 |

总耗时由 `max(indexing, TOC) + 同步收尾` 决定（两者几乎并行）。**只优化 indexing 不会显著缩短总耗时，必须同步压 TOC。**

原因与已定处理：

- **ES indexing**：`DOC_BULK_SIZE=4` 串行分批（272 chunks → 68 批），每批几乎稳定约 1s——等 refresh 周期。ES 集群 green、write queue=0、rejected=0、heap 49%，不是算力不足。→ 默认值 **4 → 32**，**保留 `refresh=wait_for` 语义**，检索即时性不变。
- **TOC**：全量 chunk 按 1024 token 分包，每包一个 LLM 任务，`asyncio.gather` **无全局并发上限**，未复用 `chat_limiter`，也未优先走结构化标题路径；取消路径会向上抛、可能阻塞正文 chunk 发布。→ 改为 executor 主 event loop 上的原生 async task 并复用现有 `chat_limiter`（不新建第二个全局信号量），单包与整次 deadline 均为 30s 且单包受整次上限约束；普通超时/供应商异常降级为空 TOC，任务取消继续上传。标题来源优先级：PDF 原生 outline → MinerU 标题 → LLM；结构化 TOC **只匹配完整标题行**（避免把正文里的章节引用当成标题），outline 未完整映射时选用**命中更多**的 parser 标题。
- **VLM 描述**：单文档 `ThreadPoolExecutor(max_workers=10)`，无跨任务限流、无内容哈希缓存。→ 并发 **10 → 4**；描述缓存键 = 图片 SHA-256 + 模型身份 + prompt 摘要，其中身份含**语言**与不含密钥的 factory / model / API / instance 配置（同租户同模型名切换语言或端点后不得复用旧描述），256 项进程内 LRU 并合并并发中的相同请求。
- **不动项**：`EMBEDDING_BATCH_SIZE=16`（3.49s / 272 chunk 已接近最优）、`refresh=wait_for` 语义、`MAX_CONCURRENT_CHATS` 的含义与 GraphRAG 限流路径。
- 附带采集：chunk token 数的 min/p50/p90/max、空块数、短块比例、结构标题数（**只采集，不改 chunk 合并逻辑**）。

**验证门槛尚未达成**：同一 7 页简历的 `indexing ≤ 12s`、`TOC ≤ 30s`、总耗时 `≤ 60s` 仍缺同文档复测（该样本文档/任务标识未固定）。测耗时前必须取静默实例，方法见 [runtime-env.md](runtime-env.md)。

验证证据（已落地部分）：TOC / MinerU / chunk 指标与邻近 prompt/settings 测试 78 项通过，refactor task context / post processor / recording 测试 97 项通过；重建 `ragflow-gpu` 后验证过 bulk 32、TOC deadline、VLM 4 / 256 项缓存、模块导入、API/Web 与 task executor。**残留缺口**：`indexing <= 12s`、`TOC <= 30s`、总耗时 `<= 60s` 三个门槛仍未在同文档上复测；VLM 缓存只在单进程内有效，多 executor 各自维护缓存与 chat 限流；refactor 全套测试在收集 `test_dataflow_service.py` 时因本机缺 OCR 模型缓存且网络不可达而中止（非断言失败）。

## 5. Dataflow 适配：多格式路由与图片定位

### 5.1 多格式路由与历史 XLSX 重复解析（已修）

- 现象：普通 ingestion 已把 PDF、目标图片和 DOCX/PPTX/XLSX 路由到 MinerU，但 Python Dataflow Parser 仍分别使用 DeepDOC、OCR 和 Office 解析器；历史 `parser_id=table` 的 XLSX 重解析还会按每 3000 行拆成多个任务，执行阶段才动态切到 MinerU。
- 根因：MinerU 的 **Office 分支忽略 `start_page_id` / `end_page_id`**，所以每个历史 XLSX 分片任务都会**完整**解析、分块并嵌入同一工作簿。确定性 chunk ID 可能覆盖重复索引项，但远端解析、embedding、chunk/token 统计与 table 后处理仍会重复或失真。
- 处理：Dataflow Parser 在 family dispatch **之前**按统一 MinerU 后缀集合拦截——PDF、`png/jpeg/jpg/jp2/webp/gif/bmp/tiff`、DOCX、PPTX、XLSX 共用同一条 MinerU JSON 输出路径，并保留原生媒体、坐标、媒体 VLM 增强和 PDF source projection；`ppt/xls/csv/doc` 等非目标格式继续走原 handler。任务创建改为依据**文件名解析后的有效 parser** 判断是否使用 table 行任务，历史 `table + xlsx` 只生成一个整文件任务。
- 残余风险：未向远端 MinerU 提交 12 个真实格式样本，也未对历史大 XLSX 做真实重解析；逐格式产物与成本下降仍需运行时 E2E 确认。

### 5.2 PDF 图片与定位回归（已修）

- 现象：DeerFlow 的 PDF 预览、编辑和 Chunk 视图同时丢图，PDF 高亮坐标也不准；而 MinerU 服务实际已返回图片、图表、表格及媒体文件。
- 根因（四处）：
  1. Dataflow Parser 用 `lines, _ = pdf_parser.parse_pdf(...)` **丢弃** MinerU 第二返回值里的原生媒体 → source projection 没有 figure/table 图片。
  2. MinerU 本地 PDF 页面渲染**未使用仓库已有的全局 PDFium 锁**，并发解析出现 `PdfiumError: Data format error`；渲染失败后既无 Chunk 预览图，也没有页面尺寸用于把 MinerU 的 0-1000 坐标缩放到 PDF 点坐标。
  3. 表格媒体实际使用 `img_path`，旧转换只读 `table_img_path`。
  4. 修复初版对每条文本执行 `crop()`，会让几乎所有正文 block 都携带截图——该路径已删除。
- 处理：Dataflow 适配器同时接收 `lines` 和原生 `media` 并统一转成 parser JSON；页面加载复用全局 PDFium 锁，渲染失败时保留页面尺寸用于坐标归一化；文本与媒体共用同一个坐标转换；表格读取 `table_img_path` 或 `img_path`。**正文只保留坐标，原生 image/table 才进 source projection 图片通道**，Chunk 正文预览仍由下游专用逻辑生成。
- 验证证据：聚焦测试 47 项通过；仅重建 `ragflow-gpu` 后对两份真实 PDF 重解析均到 DONE，新 ACTIVE projection 分别为 revision 45（634 blocks / 6 image / 7 table）与 11（110 blocks / 8 image / 9 table）；30 个原生媒体 block 全部有唯一可比对图片引用且可读取，**正文 block 携图数为 0**；坐标页范围 1-51 / 1-11，最大坐标约 `506.736 x 722.3` 与 `567.3 x 814.137`（已不再是 0-1000 原始坐标）；ES 中 75 / 23 个 Chunk 均已恢复 `img_id`。
- 残余风险：未执行 DeerFlow 已登录浏览器里的图片渲染与点击高亮回归，前端视觉交互仍需单独确认。

## 6. 已知限制

- 纯图片输入（png / jpg 等）没有文本宿主，chunk 仍无 `img_id`，图片只进 projection。
- docx 的 projection block 顺序仍由位置排序决定（`page_idx` 全 0 → 稳定序为 sections 后接 tables），图片排在正文之后；只影响 projection 视图顺序，chunk 派生取图不受影响。
- canvas / dataflow 路径的 `imagetemps` bucket 前缀与 dataset 不匹配（§3），`fs/image` 取不到图。
- VLM 缓存只在单进程内有效：多个 executor 各自持有缓存与 chat 限流，冷缓存不会缩短首次图片描述。
- 自定义换行 delimiter 仍可能生成远超 `chunk_token_num=512` 的块；当前在 provider 边界截断以防请求失败，分块质量另行治理。

## 7. 回归测试

| 关注点 | 测试 |
|---|---|
| MinerU 解析与媒体 | `test/unit_test/deepdoc/parser/test_mineru_parser.py` |
| 格式谓词与 projection 归属 | `test/unit_test/common/test_mineru_formats.py` |
| 无 caption 图片挂载到文本 section | `test/unit_test/rag/app/test_naive_mineru_images.py` |
| source projection | `test/unit_test/api/apps/services/test_source_projection_service.py` |
| 解析器与 Dataflow 适配（邻近） | `test/unit_test/rag/` 下 parser 相关用例 |

运行命令（`--confcutdir` 规避根 conftest 下载 NLTK）见 [runtime-env.md](runtime-env.md)。
