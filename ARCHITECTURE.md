# 双栈架构与能力归属

本仓库是 RAGFlow 的 fork。上游在 2026-09-28 的三个提交里删除了整个 Python 后端
（`562e02184` Remove python source code、`670e68872` part2、`e111e1f9e`），
改写为 Go。本 fork 的差异化能力全部建在 Python 侧，因此**不跟随该删除**，改为
双栈共存。

本文件记录三件事：谁服务哪个路由、哪些能力禁止重写、两侧共享数据库的契约。
`AGENTS.md` 是日常操作指南，本文件是边界与理由，两者不重复。

---

## 1. 为什么是双栈而不是统一 Go

上游删除 Python 后的规模对比：

| | 本 fork | 上游 main |
|---|---|---|
| `.py` 文件 | 1055 | 185 |
| `.go` 文件 | 1317 | 2436 |
| `rag/` | 完整（llm / graphrag / svr / flow / nlp） | 仅 `prompts/` + `res/` |
| `api/` | 151 个 py | 仅 `channels/` + `db/` |

关键数字是本 fork 相对上游 merge-base 的改动量：

- **Go 侧：16 个文件，约 580 行**（其中 5 个是测试）
- **Python 侧：约 6300 行产品代码 + 约 6900 行配套测试**（合计约 13000 行）

比例约 **1:30**（以产品代码计）。Go 侧几乎没动，说明 **Go 侧可以近乎免费地
跟随上游**；分叉面完全集中在 Python 侧。统一 Go 意味着把 6300 行产品代码和
6900 行测试资产归零，去重写上游并不具备的能力（`DocumentProjection`、
`DocumentMarkdownVersion`、Corpus FS 的路径/密钥/游标语义在 `internal/`
中零命中）。**结论：不统一 Go。**

---

## 2. 路由归属

分流由 `docker/nginx/ragflow.conf.hybrid` 决定，端口映射如下：

| 端口 | 服务 | 语言 |
|---|---|---|
| 9380 | `api/ragflow_server.py` | Python（API + 定制能力） |
| 9381 | Python admin server | Python（部分 admin 端点） |
| 9383 | `bin/ragflow_server --admin` | Go admin |
| 9384 | `bin/ragflow_server --api` | Go API |

nginx matches `location ~` regexes **in file order, first match wins** — not by
longest prefix. The trailing `^/(v1|api)` → Python is the API fallback and
`location /` serves the frontend:

```
^/api/v1/admin/roles_with_permission   -> 9381  Python
^/api/v1/admin/sandbox                 -> 9381  Python
^/api/v1/admin/roles                   -> 9381  Python
^/api/v1/admin/whitelist               -> 9381  Python
^/api/v1/admin/variables               -> 9381  Python
^/api/v1/admin                         -> 9383  Go
^/api/v1/skills                        -> 9384  Go
^/api/v1/datasets/search$              -> 9384  Go     <- 末尾 `$` 必须保留
^/api/v1/chat/completions              -> 9384  Go
^/v1/system/config                     -> 9384  Go
^/v1/user/(login|logout)               -> 9384  Go
^/(v1|api)                             -> 9380  Python   <- 兜底
```

剩余的 Python 专属 `/datasets/*` 子路由（全部落兜底规则 → Python）：

```
/api/v1/datasets/keyword-search        -> 9380  Python  （keyword_search_api.py）
/api/v1/datasets/<id>/fs/*             -> 9380  Python  （corpus_fs_api.py）
```

### 归属划分

**Go 领地（跟随上游，不要在此加定制）**

Go 侧在 `internal/router/` 注册了完整的标准能力路由（dataset / document /
chunk / chat / agent / canvas / connector / memory / search / models /
providers / mcp / users / tenants 等），但 hybrid 配置当前**只把其中少数
路径显式指向 Go**（见上表），其余 `/datasets/*`、`/documents/*` 等仍由
兜底规则送到 Python。

这意味着两件事：

1. 切换到 `API_PROXY_SCHEME=go` 时，这些路由会整体改由 Go 服务
2. 在 Python 侧修改**任何** `/datasets/*`、`/documents/*` 行为前，先确认
   目标环境下该路由实际由谁服务

**Python 领地（产品核心，禁止重写）**

不论 `API_PROXY_SCHEME` 取值，以下能力**只在 Python 侧存在**，Go 侧无对应
实现；切换到 `go` 模式会直接丢失它们：

- **Corpus FS**：`/api/v1/datasets/<dataset_id>/fs/*`
  （`api/apps/restful_apis/corpus_fs_api.py` + 8 个 `corpus_*_service.py`）
- **源投影**：`api/apps/services/source_projection_service.py`
- **Markdown 版本**：`api/apps/services/document_markdown_service.py`
- **关键字检索**：`api/apps/services/keyword_search_service.py`
- **GraphRAG 定制**：`rag/graphrag/general/*`、`rag/graphrag/light/*`
- **LLM 层定制**：`rag/llm/{chat,embedding,rerank,ocr}_model.py`
- **MinerU 富结构化解析**：`deepdoc/parser/mineru_parser.py`
- **任务执行器**：`rag/svr/task_executor_refactor/*`

> 因此 `API_PROXY_SCHEME` **不应设为 `go`**：`docker/.env` 保持 `python`
> 或 `hybrid`。上游把默认值翻成 `go` 是因为它已删除 Python 后端，本 fork
> 的前提不同。

### ⚠️ 路由归属的脆弱点

Python 专属的 `/datasets/*` 子路由只靠 nginx 规则落到 Python：

- **Corpus FS**：`/api/v1/datasets/<id>/fs/*`
- **关键词检索**：`/api/v1/datasets/keyword-search`（`keyword_search_api.py`）

它们靠兜底的 `^/(v1|api)` → Python 正确到达，而 Go 侧
`internal/router/router.go` 也注册了 `/datasets` 分组。
**这只靠 nginx 规则维持，不靠设计约束。**

已实际踩到过一次：`^/api/v1/datasets/search` 未加 `$` 时**同时匹配**
`datasets/search` 与 `datasets/keyword-search`（`search` 字面量在 `keyword-`
之后仍连续出现），把 Python 的关键词检索静默转发到未实现该路由的 Go。
该规则现已收窄为 `^/api/v1/datasets/search$`，并加注释锁定。

若上游把 Go 的 datasets 路由扩展到覆盖 `/datasets/<id>/fs`，或有人去掉该
`$`、调整 nginx 顺序，这些能力会被静默劫持到 Go 并返回 404。

**修改 nginx 或 Go datasets 路由时，必须回归验证 Corpus FS 与
keyword-search 端点可达。**

---

## 3. 数据库共存契约

两侧共享同一 MySQL/PostgreSQL 实例，各自建表。

| | Python | Go |
|---|---|---|
| ORM | Peewee | GORM |
| 建表 | `api/db/db_models.py:init_database_tables` | `internal/dao/database.go` AutoMigrate |
| 模型数 | 151 个 py 文件 | 46 个 entity（上游 54 个） |

### 为什么能共存

`internal/dao/database.go` 的 `autoMigrateSafely` **主动为 Python 共存做了防御**，
吞掉 4 类错误：

```go
// This handles cases where indexes already exist (e.g., created by Python backend)
Error 1061  Duplicate key name    -> skip
Error 1060  Duplicate column name -> skip
Error 1050  Table already exists  -> skip
Error 1091  Can't DROP            -> skip
```

GORM AutoMigrate **不会删除它不认识的表**，所以 Python 建的表对 Go 安全。

### 表命名空间

两侧表名**不重叠**：

- Python 定制表：`document_projection`、`document_markdown_version`
  （`api/db/db_models.py:945` / `:963`），Go 侧零对应物
- 上游新增 10 个 Go 模型（`API4ConversationMessage`、`ChatChannel`、
  `CompilationTemplate`、`KnowledgeCompileDataset`、`MemoryTask`、
  `WikiDocumentDirty` 等）全是**加表**，不是改表

### ⚠️ 唯一需要长期监控的风险

两侧对**同一张共享表**（`document`、`task`、`file`、`knowledgebase`）各有定义：

- Python：`api/db/db_models.py`
- Go：`internal/entity/document.go` 等

字段定义若漂移，双方 AutoMigrate 会互相改写同一张表。当前无冲突，但**每次
同步上游后应检查共享表的字段差异**。

```bash
# 抽查共享表定义
grep -A30 "type Document struct" internal/entity/document.go
grep -A30 "^class Document(DataBaseModel)" api/db/db_models.py
```

---

## 4. 与上游同步的方式

**不要 `git merge upstream/main`。** 上游删除了 838 个 `.py` 文件，其中 52 个是
本 fork 改过的；直接 merge 会产出「Python 被删 + 定制文件残留但失去依赖」的
残缺树，既跑不起来也拿不到上游新特性。

正确做法是**选择性移植**：

```bash
git remote add upstream https://github.com/infiniflow/ragflow.git
git fetch upstream --unshallow     # 首次必须，浅克隆会让 merge-base 算错
```

- **Go 侧**：可直接跟进。改动面约 580 行，冲突面小
- **Python 侧**：上游已删除，**不存在合并冲突**，保持独立演进
- **移植上游 Go 改进时**：`git cherry-pick <sha>` 或按文件取，不要整支 merge

### Go 侧当前的分叉面

仅 6 类真实能力（上游均无）：

1. `MinerUEffort` / `MinerUImageAnalysis` 字段（`pdf_parser_common.go`）
2. `minerUInputSuffixes` 多格式路由：docx/pptx/xlsx + 8 种图片 → MinerU
   （`parser_dispatch.go`）
3. `TaskSegment` 富字段：`Type` / `Layout` / `LayoutType` / `Image` / `Positions`
   （`types.go`）
4. **MinerU 4.x V1 客户端**（`internal/entity/models/mineru_local_v1.go`）：
   `/v1/health` 探测 → `/v1/uploads`（create/PUT/complete）→
   `/v1/parse/jobs` → `/v1/files/<id>/content`（zip）→ `middle_json.json`
   投影为 Content List V1；含按内容哈希的上传复用与 `tier` 映射
5. legacy（3.x）提交路由回落：`tasks` → `file_parse`（`pdf_parser_mineru.go`）
6. `FileTypeVISUAL` 映射（`utility/file.go`）

其余为采纳上游契约的结果，**不构成分叉**：`parser.ParseResult` 已统一
（上游独立收敛到相同字段），`ctx` 参数与 `buildParserOutputs` 签名同理。

> **不要用上游 Go 版 MinerU 覆盖这里。** 上游实现只取 `segment.Content`，
> 丢弃版面/图片/层级，且只支持 legacy 协议（见
> `开发日志/RAGFlow更新-加入原仓库改动/官方更新合并评估报告-*.md` §5.3）。

---

## 5. 验证

```bash
# Go：本仓库禁止裸 go test / go build，必须走 build.sh
uv run ragflow_deps/download_deps.py
bash build.sh --test ./internal/parser/parser/... ./internal/ingestion/component/...
bash build.sh --go

# 无 CGO 依赖的包可直接跑（绕开原生库与链接器）
CGO_ENABLED=0 go test ./internal/entity/models/...

# Python
uv run pytest test/unit_test/deepdoc/parser/test_mineru_parser.py
ruff check
```

Go 构建依赖原生静态库（`office_oxide`、`pdfium-static`、`pdf_oxide`）与
**clang + lld**。用 g++ 编译 `internal/binding/cpp` 会在 CGO 链接期因
RE2 全局构造顺序崩溃（`_GLOBAL__sub_I_RAGAnalyzer_Create`），这是
`build.sh` 强制要求 clang 的原因。

> **本机（无 root）还有一个独立的环境缺口**：系统只有 `libpcre2-8-0`
> 运行时，缺 dev 包，`ld.lld` 会报 `unable to find library -lpcre2-8`。
> 指向 `~/lib-shim/libpcre2-8.a`（`vim` 用的 conda 静态库）可编过并链接，
> 但产出的 `parser.test` 二进制在**任何测试执行前**即段错误。该现象在
> 未改动的代码上同样存在，属工具链问题，不是代码问题——**请在正常环境跑
> `build.sh --test` 取绿灯**。`internal/entity/models` 无 CGO 依赖，故
> MinerU V1 客户端的运行时逻辑可用 `CGO_ENABLED=0` 直接验证。

---

## 6. 修改前的检查清单

- 新增能力时先确认归属：**Go 领地还是 Python 领地**
- 不要为了让两个后端「一致」而重写 Python 定制
- 动 nginx 路由或 Go 的 `/datasets` 分组后，回归验证 Corpus FS 可达
- 同步上游后抽查共享表（`document` / `task` / `file` / `knowledgebase`）字段差异
- 若发现某能力两侧都有实现，收敛到**当前实际服务该路由的那一侧**
