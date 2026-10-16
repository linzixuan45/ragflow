# RAGFlow Instructions

Use this file as the local operating guide for the current codebase. Prefer the code and the current ARCHITECTURE.md over any older convention or remembered project shape.

## Core stance
- Treat legacy code as liability, not as a compatibility target.
- Prefer deletion over shims, deprecated branches, wrapper APIs, and dual-track migration notes.
- If old and new implementations coexist, converge to one path unless an external contract forces compatibility.
- Remove dead tests, commented-out code, stale docs, and "move later" notes instead of preserving them.
- Reduce public surface area when a helper can be made private or internal.
- Keep refactors centered on the owning abstraction, not on adjacent compatibility layers.

## Current stack
- Backend: Python 3.13+, Quart-based API server, Peewee ORM, async workers.
- Frontend: React + TypeScript + Vite in `web/`.
- Go: the repository also has a substantial Go module for servers, ingestion, parser/runtime, CLI, and supporting services.
- Runtime services commonly include MySQL/PostgreSQL, Redis, MinIO, and Elasticsearch/Infinity/OpenSearch depending on configuration.

## Code layout to expect
- `api/`: Python API server entrypoints, blueprints, services, and database code.
- `rag/`: ingestion, retrieval, LLM integration, and graph RAG logic.
- `deepdoc/`: parsing and OCR.
- `agent/`: workflow canvas, components, tools, and templates.
- `cmd/`: Go entrypoints. `ragflow_main` is the main server/admin/ingestor binary surface; `ragflow-cli` is the CLI entrypoint.
- `internal/`: main Go application code. Important subtrees:
- `internal/agent/`: Go agent runtime, canvas execution, components, tool bindings, workflow helpers.
- `internal/cli/`: CLI parsing, HTTP transport, command execution, response formatting.
- `internal/dao/`: Go data-access layer and persistence-facing helpers.
- `internal/deepdoc/`: Go DeepDOC integrations, especially native-backed PDF/DOCX parsing.
- `internal/engine/`: search/index backends such as Elasticsearch and Infinity.
- `internal/entity/`: shared Go entities and model definitions.
- `internal/handler/`: HTTP handlers and route-facing request logic.
- `internal/ingestion/`: Go ingestion pipeline, canvas adapter, components, wiring, service orchestration.
- `internal/ingestion/component/`: stage implementations such as file/parser/chunker/tokenizer/extractor.
- `internal/ingestion/pipeline/`: DSL translation, canvas-driven execution, checkpoints, resume/run logic.
- `internal/parser/`: parser and chunk libraries used by ingestion and other Go paths.
- `internal/parser/parser/`: typed parse-result parsers for markdown/html/pdf/docx/xlsx/text and related families.
- `internal/parser/chunk/`: chunk operator library and DSL/typed execution helpers.
- `internal/service/`: higher-level business services used by handlers and server flows.
- `internal/storage/`: storage backends and in-memory test doubles.
- `internal/router/`: HTTP route registration.
- `internal/server/`: server bootstrap/config wiring.
- `internal/binding/cpp/`: C++ sources used by native-backed Go features.
- `web/`: frontend application.
- `docker/`: local and production compose files.
- `sdk/` and `test/`: SDK and automated tests.

## Go-specific rules
- Treat `internal/ingestion`, `internal/parser`, and `internal/deepdoc` as actively refactored code. Prefer collapsing duplicate paths over preserving transitional wrappers.
- Do not add or preserve deprecated Go APIs just to ease migration inside the repo.
- Remove commented-out Go code instead of leaving recovery notes in place.
- Keep package comments and doc comments aligned with the current runtime path, not with migration history.

## Working rules
- Before editing, inspect the nearest code path that actually owns the behavior.
- Keep changes small and local unless the task is explicitly a broader refactor.
- Prefer one implementation path instead of preserving old and new versions side by side.
- Preserve behavior with focused tests when the behavior is still valid; do not keep tests that protect obsolete behavior.
- If a surface is only there for compatibility, remove it unless the user asks to keep it.
- Do not add new compatibility wording in comments or docs.

## Commands
### Backend
```bash
uv sync --python 3.13 --all-extras
uv run python3 ragflow_deps/download_deps.py
docker compose -f docker/docker-compose-base.yml up -d
source .venv/bin/activate
export PYTHONPATH=$(pwd)
bash docker/launch_backend_service.sh
uv run pytest
# ruff is pinned in the `lint` dependency group (uv.lock); a bare `ruff` picks up
# whatever is on PATH, which can differ from CI.
uv run --group lint ruff check
uv run --group lint ruff format
```

### Docs and hooks
```bash
npm install                                  # root: provides lefthook for the git hooks
python3 tools/hooks/check_files.py docs      # Docs-as-Code gate (the pre-commit hook runs this)
python3 tools/hooks/check_files.py symbols   # code-citation drift only
```

### Frontend
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
# or build specific binaries:
bash build.sh --all
```

## Validation preference
- Run the narrowest relevant test, lint, or build command after a change.
- For docs changes, run `python3 tools/hooks/check_files.py docs` (links, `§` references, index coverage, section numbering, code-citation drift). The `check-docs` job in [lefthook.yml](lefthook.yml) runs the same command before commit, and [.github/workflows/docs.yml](.github/workflows/docs.yml) runs it on every markdown change.
- For backend changes, prefer targeted pytest or ruff checks over full-suite runs.
- For frontend changes, prefer the touched-package lint, type-check, or test command.
- For Go changes, prefer package-scoped `bash build.sh --test ...` first.
- Do not default to raw `go test`, `go build`, or IDE Run/Debug for Go in this repo. They often miss the required CGO flags and native static libraries (`office_oxide`, `pdfium-static`, `pdf_oxide`) that `build.sh` wires correctly.
- If Go native builds fail, inspect `build.sh` and `internal/development.md` before changing code. Common environment issues are missing downloaded native deps and missing `lld` on Linux.

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
- 写侧 projection 门禁已放开到所有 MinerU 格式；**读侧仍要求 `document.type == "pdf"`** → 非 PDF 图片走 chunk 通道。
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
- Corpus 根密钥是 Redis 里保存的随机值，**无法用环境变量固定**；flush Redis 会让已签发的引用、引用签名与分页游标全部失效。
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
| 13 | Corpus 根密钥由 Redis 保存，不得用环境变量假定可固定；不得随意 flush Redis | [engineering/corpus-fs.md](engineering/corpus-fs.md) §3 |
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
| 运行、挂载、环境变量、测试命令 | [engineering/runtime-env.md](engineering/runtime-env.md) 与本文 `Commands` 节 |
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
- 语言约定：fork 新增内容用中文；与上游同步的文件（本文既有小节、`README_zh.md`）保留上游原文。

## 文档即代码（Docs-as-Code）

外层文档按 Docs-as-Code 管理，五条契约如下：

1. **与代码同仓同分支**：正文在 `engineering/`，与代码一起评审、一起回滚。**文档正本必须在仓库内**；仓库外只允许放可再生成的原始数据与采集脚本（例：Corpus 检索实验的 raw log 与 scripts）。
2. **单一定位**：一条事实只写一处；入口文件只保留索引、硬约束与维护规则，正文一律进 `engineering/`。
3. **可机械校验**：`python3 tools/hooks/check_files.py docs` 强制悬空链接与锚点、`§` 小节引用、索引覆盖、小节编号连续性，以及内联引用的**路径 / 函数 / 常量 / 测试名**是否仍存在。三道口子跑同一条命令：[lefthook.yml](lefthook.yml) 的 `check-docs`（提交前）、[.github/workflows/docs.yml](.github/workflows/docs.yml)（`*.md` 改动）、以及手动执行。
4. **带状态与日期**：事故与验证记录必须标状态（已修 / 未修 / 已接受）与日期；标「已修」必须给可复现的验证证据或测试名，标「已接受」必须写重启条件。口径变化要显式声明新旧不可比。
5. **过期即删**：描述已被取代设计的文档直接删除，不留「以后清理」的占位、不留新旧两份并存。

**仍然缺的（改动前自行确认）**：

- 正文内联引用的**符号**已由 `docs` 检查覆盖，但它只判「名字是否还在仓库里」；**语义是否仍然正确**（函数行为、参数含义、阈值）机器判不了，仍需人读。
- `engineering/` 不构建站点，所以没有渲染期断链、锚点或样式检查；`docs/`（上游 Docusaurus 树）在本 fork 也不构建。
- 「改了源码却没动文档」这种语义缺口没有门禁：CI 只对**变更文件本身**做 lint，`file-checks` / `python-lint` / `go-format` 都不会因为 `engineering/` 未变而失败。发现漏写文档只能靠 review 对照本文的「文档维护」表。

## Default review checklist
- Remove instead of retaining `deprecated`, `legacy`, or compatibility-only code.
- Collapse duplicate implementations to one path.
- Drop stale comments and documentation that describe a superseded design.
- Keep exported APIs only when the current code actually needs them.
