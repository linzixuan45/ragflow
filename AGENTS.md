# RAGFlow 仓库操作指南

本文件是当前代码库的本地操作指南。**以代码与 [ARCHITECTURE.md](ARCHITECTURE.md) 为准**，不要依据旧约定或记忆中的项目形态。

## 核心立场
- 遗留代码是负债，不是兼容目标。
- 优先删除，而不是保留 shim、deprecated 分支、包装 API 或双轨迁移说明。
- 新旧实现并存时收敛到一条路径，除非外部契约强制要求兼容。
- 删除死测试、注释掉的代码、过期文档与「以后再清理」的说明，而不是留着它们。
- 能私有化的 helper 就私有化，缩小公开面。
- 重构围绕拥有该行为的抽象，不要围绕相邻的兼容层。

## 当前技术栈
- 后端：Python 3.13+、Quart API 服务、Peewee ORM、异步 worker。
- 前端：`web/` 下的 React + TypeScript + Vite。
- Go：仓库另有一个体量可观的 Go module，覆盖 server、ingestion、parser/runtime、CLI 与配套服务。
- 运行时依赖按配置可能包括 MySQL/PostgreSQL、Redis、MinIO，以及 Elasticsearch/Infinity/OpenSearch。

## 代码布局
- `api/`：Python API 服务入口、blueprint、service 与数据库代码。
- `rag/`：摄取、检索、LLM 集成与 GraphRAG 逻辑。
- `deepdoc/`：解析与 OCR。
- `agent/`：工作流画布、组件、工具与模板。
- `cmd/`：Go 入口。`ragflow_main` 是主服务/管理/摄取二进制入口；`ragflow-cli` 是 CLI 入口。
- `internal/`：Go 主应用代码。重要子树：
- `internal/agent/`：Go agent 运行时、画布执行、组件、工具绑定与工作流辅助。
- `internal/cli/`：CLI 解析、HTTP 传输、命令执行与响应格式化。
- `internal/dao/`：Go 数据访问层与持久化辅助。
- `internal/deepdoc/`：Go 侧 DeepDOC 集成，尤其是原生 PDF/DOCX 解析。
- `internal/engine/`：检索/索引后端，如 Elasticsearch 与 Infinity。
- `internal/entity/`：共享 Go 实体与模型定义。
- `internal/handler/`：HTTP handler 与面向路由的请求逻辑。
- `internal/ingestion/`：Go 摄取流水线、画布适配器、组件、装配与服务编排。
- `internal/ingestion/component/`：file/parser/chunker/tokenizer/extractor 等阶段实现。
- `internal/ingestion/pipeline/`：DSL 转换、画布驱动执行、检查点与恢复/运行逻辑。
- `internal/parser/`：摄取等 Go 路径使用的解析器与分块库。
- `internal/parser/parser/`：markdown/html/pdf/docx/xlsx/text 等类型的解析结果解析器。
- `internal/parser/chunk/`：分块算子库与 DSL/类型化执行辅助。
- `internal/service/`：handler 与 server 流程使用的高层业务服务。
- `internal/storage/`：存储后端与内存测试替身。
- `internal/router/`：HTTP 路由注册。
- `internal/server/`：服务启动与配置装配。
- `internal/binding/cpp/`：原生 Go 能力使用的 C++ 源码。
- `web/`：前端应用。
- `docker/`：本地与生产 compose 文件。
- `sdk/` 与 `test/`：SDK 与自动化测试。

## Go 侧规则
- `internal/ingestion`、`internal/parser`、`internal/deepdoc` 是仍在重构中的代码：优先合并重复路径，不要保留过渡性包装。
- 不要为了仓库内部迁移而新增或保留 deprecated 的 Go API。
- 删除注释掉的 Go 代码，不要留下「恢复用」的说明。
- package / doc 注释要与**当前**运行路径一致，不要写迁移历史。

## 工作准则
- 改之前先看真正拥有该行为的那条代码路径。
- 除非任务明确要求更大范围重构，否则保持改动小且局部。
- 只保留一条实现路径，不要新旧两版并存。
- 行为仍然有效时用聚焦测试锁住；不要保留维护过时行为的测试。
- 只为兼容而存在的表面，除非用户要求保留，否则删掉。
- 注释与文档里不要新增兼容性措辞。

## 命令
### 后端
```bash
uv sync --python 3.13 --all-extras
uv run python3 ragflow_deps/download_deps.py
docker compose -f docker/docker-compose-base.yml up -d
source .venv/bin/activate
export PYTHONPATH=$(pwd)
bash docker/launch_backend_service.sh
uv run pytest
# ruff 由 `lint` 依赖组（uv.lock）定版；裸 `ruff` 取的是 PATH 上的任意版本，
# 可能与 CI 不一致。
uv run --group lint ruff check
uv run --group lint ruff format
```

### 文档与钩子
```bash
npm install                                  # 仓库根：为 git 钩子提供 lefthook
python3 tools/hooks/check_files.py docs      # Docs-as-Code 闸门（pre-commit 钩子跑的就是这条）
python3 tools/hooks/check_files.py symbols   # 只查代码引用漂移
```

### 前端
```bash
cd web
npm install
npm run dev
npm run build
npm run lint
npm run test
npm run type-check
```

### Go
```bash
uv run ragflow_deps/download_deps.py
bash build.sh --test ./path/to/package/...
bash build.sh --go
# 或构建指定二进制：
bash build.sh --all
```

## 验证偏好
- 改完跑**最窄**的相关测试、lint 或构建命令。
- 文档改动：跑 `python3 tools/hooks/check_files.py docs`（链接、`§` 引用、索引覆盖、小节编号、代码引用漂移）。[lefthook.yml](lefthook.yml) 的 `check-docs` 在提交前跑同一条命令，[.github/workflows/docs.yml](.github/workflows/docs.yml) 在任何 markdown 改动上跑它。
- 后端改动：优先跑聚焦的 pytest 或 ruff，而不是全量。
- 前端改动：优先跑受影响包的 lint、type-check 或测试命令。
- Go 改动：优先跑包级 `bash build.sh --test ...`。
- **不要**默认用裸 `go test`、`go build` 或 IDE Run/Debug 跑 Go。它们通常缺少 `build.sh` 配好的 CGO 标志与原生静态库（`office_oxide`、`pdfium-static`、`pdf_oxide`）。
- Go 原生构建失败时，先看 [build.sh](build.sh) 与 [internal/development.md](internal/development.md) 再改代码。常见环境问题是原生依赖未下载、Linux 缺 `lld`。

## 架构索引

每个条目给出该子系统的定位、必知不变式、关键文件与回归测试。**先读索引，再按链接读正文**；正文在 `engineering/`，双栈边界与理由在 `ARCHITECTURE.md`。

### 双栈边界、路由归属与共享 DB 契约

不跟随上游删除 Python：fork 的差异化能力全部在 Python 侧，Go 侧几乎未改、可近乎免费跟随上游。

- `API_PROXY_SCHEME` 保持 `python` 或 `hybrid`，**不得设为 `go`**。
- Python 专属路由只靠 nginx 规则落到 Python（`docker/nginx/ragflow.conf.hybrid`），**不是设计约束**。
- 两侧共享同一 MySQL/PostgreSQL 实例，表名不重叠，但 `document` / `task` / `file` / `knowledgebase` 是双方各自定义同一张表。
- 不要 `git merge upstream/main`，只选择性移植。
- 正文：[ARCHITECTURE.md](ARCHITECTURE.md)（§2 路由归属、§3 表契约、§4 同步方式、§6.2 同步后固定检查项）

### 解析链路（MinerU / DeepDOC / Dataflow）

`MINERU_SUFFIXES`（PDF + 8 种图片 + docx/pptx/xlsx）一律经 MinerU 解析；PDF 与 office 分支的产物形态不同。

- `uses_source_projection()` 是「是否需要 source projection」的唯一谓词，与 `is_mineru_file()` 同值。
- projection 写侧与读侧都按 `uses_source_projection()` 识别 MinerU 格式；已有 ACTIVE 投影优先，无投影时才走原文或 chunk 通道。
- office 分支**不返回任何图片描述**；恢复 docx 图片不需要视觉模型。
- 关键文件：`common/mineru_formats.py`、`deepdoc/parser/mineru_parser.py`、`rag/app/naive.py`、`api/apps/services/corpus_fs_service.py`
- 回归测试：`test/unit_test/deepdoc/parser/test_mineru_parser.py`、`test/unit_test/common/test_mineru_formats.py`、`test/unit_test/rag/app/test_naive_mineru_images.py`
- 正文：[engineering/parse-pipeline.md](engineering/parse-pipeline.md)

### Corpus FS 与检索

多库检索入口 `POST /api/v1/datasets/corpus-search` 与 `/datasets/<id>/fs/*` 只在 Python 侧存在，Go 侧零对应物。

- 图片描述没有独立字段，读图片块的 `content_with_weight`；图片块身份是 `doc_type_kwd == "image"`。
- `get_filters()` / `retrieval()` 是全检索路径共用，新增过滤键必须 **opt-in**。
- 请求不带 `rerank_id` 时回落到系统默认 rerank 模型；候选窗口必须是 `page_size` 的整数倍，否则深分页静默丢结果。
- 中文判定两侧不一致：Python 按汉字字符占比 > 0.2，Go 按字段占比 ≥ 0.7。
- `image_ref` 是 HMAC 摘要；只看 `image_id` 以 `<dataset_id>-` 开头的图片，canvas 路径用默认 bucket 取不到图。
- 图片可携带 `citation_ref` 定位原投影版本，仍须重新授权；MCP 负责原生图像交付，正文预算与媒体预算分别约束。
- Corpus 根密钥可由 `RAGFLOW_CORPUS_SECRET` 固定，否则回落 Redis；变更根密钥或丢失 Redis 回落值会使已签发引用与游标失效，优化任务不得静默更换密钥来源。
- PDF 排序前定位修正页码；入选签发复用本次已算 target，非 PDF 保持入选后读取。细节与验证见主题文档。
- 关键文件：`api/apps/services/corpus_fs_service.py`、`corpus_secret_service.py`、`corpus_recall_service.py`、`rag/nlp/search.py`
- 回归测试：`test/unit_test/api/apps/services/test_dataset_api_service.py`、`test/unit_test/api/apps/restful_apis/test_corpus_search.py`
- 正文：[engineering/corpus-fs.md](engineering/corpus-fs.md)

### 运行环境与命令口径

knowledge-dev 用 [re_start.sh](re_start.sh) 起 Compose base + overlay。

- overlay 以**逐文件 bind mount** 为主（当前 56 个 `.py` 单文件；`rag/svr/task_executor_refactor/` 是唯一整目录挂载），新增源文件必须同步进 `docker/docker-compose.knowledge-dev.yml`。
- 改已挂载文件后必须 `--force-recreate`（原子写换了 inode）。
- 容器的 `env_file` 是 `docker/.env.local` 与 `docker/.env`；`--env-file` 只做 Compose 变量替换。
- 正文：[engineering/runtime-env.md](engineering/runtime-env.md)

### 任务队列与 task executor

队列是 Redis 流 `te.<priority>.common`，消费组 `rag_flow_svr_task_broker`；`ack()` 有三条路径，其中「空消息」与「取不到/已取消」会静默丢消息。

- 任务丢失已有对账恢复（`requeue_orphaned_tasks` + `recover_orphaned_tasks()` 周期扫描），只覆盖 `task_type=""` 的 `common` / `resume` 队列。
- `lag=0` 与 `progress_msg` 文案都不能证明任务在跑：前者只表示消费组读完整个流，后者由进度同步周期性重写。
- 关键文件：`rag/utils/redis_conn.py`、`api/db/services/task_service.py`、`rag/svr/task_executor.py`
- 回归测试：`test/unit_test/api/db/services/test_task_orphan_recovery.py`、`test/unit_test/rag/utils/test_queue_backlog.py`
- 正文：[engineering/task-queue.md](engineering/task-queue.md)

### DeerFlow 集成面

RAGFlow 是内容版本的正本；DeerFlow 只做 Tool Adapter、会话编号与多轮调度。

- 发布激活用 `PENDING -> ACTIVE` 条件更新抢占唯一执行权；删除先清对象再删元数据，对象清理失败时保留元数据以便重试。
- 纯目录的 File 不得带 `active_document_id`（只由真实文件与 Document 建立关系时维护）。
- 共享 RAGFlow Token 只代表服务身份，不能当租户身份；租户边界靠 DeerFlow 的 PostgreSQL ACL 与审计。
- 关键文件：`api/apps/services/document_markdown_service.py`、`api/db/services/file_service.py`、`api/apps/services/corpus_*_service.py`
- 正文：[engineering/deerflow-integration.md](engineering/deerflow-integration.md)

### 外部服务契约（MinerU / 本地 vLLM）

远程 MinerU 与本地 embedding / rerank 的版本、端点与长度预算都在这里，改模型配置前先读。

- MinerU 4.x 的健康与配额分属 `/v1/health` 与 `/v1/usage`；旧 `/health`、`/file_parse`、`/tasks` 已失效，同步接口会把内部失败压成空响应 409。
- 本地 8000 embedding 固定 2560 维、`max_model_len=2048`；8001 rerank 不支持 embeddings。`max_model_len` 约束**单条输入序列**，不是数组总长度。
- 客户端预算：embedding 每条 1024，rerank `query + document` 768。
- 正文：[engineering/contracts.md](engineering/contracts.md)

### 事故、验证与性能台账

按子系统分散在各主题文档的「验证证据 / 残余风险 / 已知缺口」小节：[parse-pipeline.md](engineering/parse-pipeline.md)、[corpus-fs.md](engineering/corpus-fs.md)、[task-queue.md](engineering/task-queue.md)、[deerflow-integration.md](engineering/deerflow-integration.md)、[runtime-env.md](engineering/runtime-env.md)。

Corpus 多库检索的性能实验数据与逐版本记录在 `engineering/experiments/corpus-fs/`。

## 硬约束

下表每条都是「改错了会静默坏掉」的口径。动相关代码前先确认本表，再读对应正文。

| # | 约束 | 落点 / 验证 |
|---|------|-------------|
| 1 | `API_PROXY_SCHEME` 不得设为 `go`：Corpus FS、源投影、Markdown 版本、keyword-search、GraphRAG/LLM 定制、MinerU 富结构化解析、task executor 只在 Python 侧 | `docker/.env`；未鉴权探测应为 401 |
| 2 | nginx 的 `^/api/v1/datasets/search$` 的 `$` 不得去掉；动 nginx 或 Go `/datasets` 分组后必须回归 Corpus FS 与 keyword-search 可达（401 = 可达，404 = 被劫持到 Go） | `docker/nginx/ragflow.conf.hybrid` |
| 3 | 不要 `git merge upstream/main`（上游删了 838 个 `.py`），只 cherry-pick 或按文件取 | [ARCHITECTURE.md](ARCHITECTURE.md) §4 |
| 4 | Go 禁止裸 `go test` / `go build`，必须走 [build.sh](build.sh)（CGO + 原生静态库 + clang/lld） | [build.sh](build.sh)、[internal/development.md](internal/development.md) |
| 5 | overlay 挂载的单个源文件改动后必须 `--force-recreate`；新增源文件必须同步进 overlay | `docker/docker-compose.knowledge-dev.yml` |
| 6 | 进程导入时读取的配置（如 `CORPUS_POOL_BUDGET_CANDIDATES`）改动后必须重建容器 | overlay 的 `environment:` |
| 7 | `--env-file` 只做 Compose 变量替换；变量要进容器必须写 `docker/.env.local` 或 `docker/.env` | [re_start.sh](re_start.sh) |
| 8 | 耗时测量前必须取静默实例（`--disable-taskexecutor`），测完改回 `command: []` | [engineering/runtime-env.md](engineering/runtime-env.md) §1 |
| 9 | 同步上游后必须抽查共享表字段漂移；两条 Python 专属路由必须回归 | [ARCHITECTURE.md](ARCHITECTURE.md) §6.2 |
| 10 | 远程 MinerU 健康判定用 `/v1/health` + `/v1/usage`；4.0.8 起旧 `/health`、`/file_parse`、`/tasks` 已失效 | [ARCHITECTURE.md](ARCHITECTURE.md) §6.2.3 |
| 11 | 检索过滤键必须 opt-in；`get_filters()` / `retrieval()` 是全检索路径共用 | [engineering/corpus-fs.md](engineering/corpus-fs.md) §1.3 |
| 12 | `image_id` 必须以 `<dataset_id>-` 为前缀才可取图；canvas 路径的默认 bucket 不满足 | [engineering/corpus-fs.md](engineering/corpus-fs.md) §2 |
| 13 | Corpus 根密钥优先显式覆盖、否则依赖 Redis；不得静默换源或随意 flush Redis | [engineering/corpus-fs.md](engineering/corpus-fs.md) §3 |
| 14 | 引用签发语义（`source_hash`）不得改；「同库文件名不跨库去重」是有意契约 | `test_same_file_name_in_two_datasets_is_not_cross_deduplicated` |
| 15 | 单文档解析的关键路径门槛（indexing ≤ 12s / TOC ≤ 30s / 总 ≤ 60s）尚未达标，改动后需同文档复测 | [engineering/parse-pipeline.md](engineering/parse-pipeline.md) §4 |
| 16 | `_rerank_window` 必须是 `page_size` 的整数倍（block 与 page 共用该模数，破坏即深分页静默丢结果） | [engineering/corpus-fs.md](engineering/corpus-fs.md) §5 |
| 17 | Corpus 写路径的 mutation 体在 `DB.atomic()` + 行锁内执行：`If-Match` 必填，体内不得做长耗时的存储/索引/队列等待 | [engineering/corpus-fs.md](engineering/corpus-fs.md) §6.1 |
| 18 | 外部副作用不受事务保护且仓库无 outbox：改 Corpus 写路径时必须自己兜底孤儿对象与重试幂等 | [engineering/corpus-fs.md](engineering/corpus-fs.md) §6.4 |
| 19 | 新增或改动 HTTP 路由后必须 `--force-recreate` 容器（Quart 路由表不随 bind mount 刷新，重启不够，否则业务级 405） | [engineering/runtime-env.md](engineering/runtime-env.md) §1 |
| 20 | 任务丢失兜底只覆盖 `task_type=""` 的 `common`/`resume` 队列；raptor/graphrag/memory/dataflow 这类带额外字段的任务消息若丢失仍无兜底 | [engineering/task-queue.md](engineering/task-queue.md) §2 |
| 21 | 文档改动必须过 `python3 tools/hooks/check_files.py docs`：悬空链接/锚点、`§` 引用、索引覆盖、小节编号连续性，以及**内联引用的路径/函数/常量/测试名是否仍存在** | [tools/hooks/check_files.py](tools/hooks/check_files.py)、[lefthook.yml](lefthook.yml)、[.github/workflows/docs.yml](.github/workflows/docs.yml) |
| 22 | 正文只写在 `engineering/`，入口文件只做索引与约束；新主题文档必须同时进「架构索引」与 [engineering/README.md](engineering/README.md)，否则视为不存在 | `check_files.py docs` 的索引覆盖检查 |
| 23 | 本地提交门禁由 lefthook 执行，它的二进制来自仓库根 `npm install`；**不要**给钩子加 `uv run lefthook` 之类的回退（未声明依赖时 uv 会联网解析并挂住提交） | 根 [package.json](package.json)、`.git/hooks/pre-commit` |
| 24 | ruff 版本以 [pyproject.toml](pyproject.toml) 的 `lint` 依赖组 + `uv.lock` 为准，用 `uv run --group lint ruff`；CI 里另有版本一致性守卫 | [.github/workflows/lint.yml](.github/workflows/lint.yml) |

## 文档维护

| 改动类型 | 需要同步的文档 |
|----------|----------------|
| 解析链路行为、MinerU/DeepDOC 契约 | [engineering/parse-pipeline.md](engineering/parse-pipeline.md)（并列出相关回归测试名） |
| Corpus / 检索 / 引用行为 | [engineering/corpus-fs.md](engineering/corpus-fs.md) |
| 运行、挂载、环境变量、测试命令 | [engineering/runtime-env.md](engineering/runtime-env.md) 与本文「命令」节 |
| 路由归属、共享表字段、上游移植 | [ARCHITECTURE.md](ARCHITECTURE.md) |
| 新出现的失败模式与事故 | 归属主题文档（`engineering/` 下对应那篇）；需要新开主题时同时补本文「架构索引」 |
| 外部服务版本、端点或长度预算变化 | [engineering/contracts.md](engineering/contracts.md) |
| 任务队列与 executor 行为 | [engineering/task-queue.md](engineering/task-queue.md) |
| DeerFlow 侧契约或隔离方式 | [engineering/deerflow-integration.md](engineering/deerflow-integration.md) |
| 用户可见的变化 | [README_zh.md](README_zh.md) |
| 跨层或部署约定变化 | 本文与 [ARCHITECTURE.md](ARCHITECTURE.md) |

- 记录固定五段（现象 / 根因 / 处理 / 验证证据 / 残余风险），必须带状态与日期；格式见 [engineering/README.md](engineering/README.md)。
- **同一事实只写一处**，别处只放链接。
- **禁止把大段实现细节重新堆回本文件**——本文件是索引与约束清单，正文属于 `engineering/`。
- 语言约定：本文件、[ARCHITECTURE.md](ARCHITECTURE.md) 与 `engineering/` 一律中文；上游同步来的文件（[README_zh.md](README_zh.md)、`docs/` 下的用户文档）保留上游原文。

## 文档即代码（Docs-as-Code）

外层文档按 Docs-as-Code 管理，五条契约如下：

1. **与代码同仓同分支**：正文在 `engineering/`，与代码一起评审、一起回滚。**文档正本必须在仓库内**；仓库外只允许放可再生成的原始数据与采集脚本（例：Corpus 检索实验的 raw log 与 scripts）。
2. **单一定位**：一条事实只写一处；入口文件只保留索引、硬约束与维护规则，正文一律进 `engineering/`。
3. **可机械校验**：`python3 tools/hooks/check_files.py docs` 强制悬空链接与锚点、`§` 小节引用、索引覆盖、小节编号连续性，以及内联引用的**路径 / 函数 / 常量 / 测试名**是否仍存在。覆盖范围是 `AGENTS.md`、`ARCHITECTURE.md`、`README_zh.md` 与 `engineering/`（`experiments/*/{records,baseline,annotations}` 按数据对待）；嵌套徽章 `[![alt](img)](target)` 的两侧目标都会校验。三道口子跑同一条命令：[lefthook.yml](lefthook.yml) 的 `check-docs`（提交前）、[.github/workflows/docs.yml](.github/workflows/docs.yml)（`*.md` 改动）、以及手动执行。
4. **带状态与日期**：事故与验证记录必须标状态（已修 / 未修 / 已接受）与日期；标「已修」必须给可复现的验证证据或测试名，标「已接受」必须写重启条件。口径变化要显式声明新旧不可比。
5. **过期即删**：描述已被取代设计的文档直接删除，不留「以后清理」的占位、不留新旧两份并存。

**仍然缺的（改动前自行确认）**：

- 正文内联引用的**符号**已由 `docs` 检查覆盖，但它只判「名字是否还在仓库里」；**语义是否仍然正确**（函数行为、参数含义、阈值）机器判不了，仍需人读。
- `engineering/` 不构建站点，所以没有渲染期断链、锚点或样式检查；`docs/`（上游 Docusaurus 树）在本 fork 也不构建。
- 「改了源码却没动文档」这种语义缺口没有门禁：CI 只对**变更文件本身**做 lint，`file-checks` / `python-lint` / `go-format` 都不会因为 `engineering/` 未变而失败。发现漏写文档只能靠 review 对照本文的「文档维护」表。

## 默认审查清单
- 删掉，而不是保留 `deprecated`、`legacy` 或仅为兼容而存在的代码。
- 把重复实现收敛成一条路径。
- 丢掉描述已被取代设计的过期注释与文档。
- 只有当前代码真的需要时才保留导出 API。
