# 外部服务契约（MinerU / 本地 vLLM）

> 覆盖：远程 MinerU 解析服务的版本、端点与排障口径；本地 vLLM embedding / rerank 的模型契约与长度预算。
> 解析链路怎么消费这些契约见 [parse-pipeline.md](parse-pipeline.md)；容器内配置见 [runtime-env.md](runtime-env.md)。

## 1. 远程 MinerU 解析服务

客户端生效配置（容器内实测）：`MINERU_APISERVER=http://10.100.15.70:8886`、`MINERU_BACKEND=hybrid-engine`、`API_PROXY_SCHEME=python`、`MINERU_DELETE_OUTPUT=0`。

### 1.1 当前版本：API v4.0.8（2026-10-02）

- 健康与配额是**两个端点**：`GET /v1/health` 只返回 `status` / `version` / `features`；并发与配额上限在 `GET /v1/usage`（`limits.max_concurrent_jobs=3`、`max_files_per_job=500`、`max_pages_per_file=5000`）。
- 作业式接口：`/v1/uploads` → `/v1/files` → `/v1/parse/jobs`；产物经 `/v1/files/<id>/content`（zip）取回。
- `GET /v1/models` → MinerU-Flash / Hybrid-Basic / MinerU-HTML / MinerU2.5-Pro-2605-1.2B；`GET /v1/tiers` → flash / basic / standard / advanced。

**排障口径（照旧记录排会直接落空）**

- 3.4.4 时代的 `/health`、同步 `/file_parse`、`/tasks` **已不在 OpenAPI 路径表**里。按旧 URL 与旧字段（`completed_tasks` / `failed_tasks` / `max_concurrent_requests` / `processing_window_size`）排障会全部落空，改用 `/v1/usage` + `/v1/parse/jobs` 的状态计数。
- 服务端任务状态是**纯内存态**：2026-10-02 服务在 20:12~20:22 之间重启，客户端在窗口内对 8886 报 `Connection refused`，一份 92.9MB docx 直接落 `run=4`（FAIL）、`progress=-1`，**没有自动重试**。
- `billing_period.start`（`/v1/usage`）是判断该服务**重启过**的唯一现成线索；重启审计先看它。
- 槽位被僵死 job 占满时，症状是「解析变慢 / `Task done` 耗时几百秒 / 复用上传失败重试」，**不是报错**：`running` 数与 `max_concurrent_jobs` 相同且 `started_at` 很旧即为该状态。

### 1.2 历史边界：同步接口会把失败压成空 409

3.4.4 时代一次 316 页 PDF（RAGFlow 按 12 页窗口拆分）的首窗成功、后续窗口全部返回**空响应 HTTP 409**，而远端 `/health` 仍 healthy、队列与处理中任务均为 0。真实原因只能经异步 `/tasks/{task_id}` 拿到：远端 **CUDA OOM**（44.40 GiB 显存仅剩 8.19 MiB，另三个进程分别占约 12.51 / 11.21 / 19.21 GiB）。可迁移的三条结论：

- 同步 `/file_parse` 会把内部失败压缩成**没有正文的 409**，排障必须走异步任务接口取真实错误。
- 该现象与页窗边界、同名文件、RAGFlow 的 embedding 或索引都无关；**降低 RAGFlow 分页并发或 MinerU `max_concurrent_requests` 解决不了单任务 OOM**。恢复要在远端 GPU 主机上释放/迁移显存，或把 VLM 卸载到 `hybrid-http-client`。
- 不要为绕开 409 去改「随机上传文件名」这类客户端行为（当年实测证明无效）。

### 1.3 仓库侧不可见的部分

- 服务端 `BACKEND=legacy` 与客户端 `MINERU_BACKEND=hybrid-engine` 是**两个层次**的值（前者是服务端自选后端，后者是客户端请求参数），`/v1/tiers` 里也没有 legacy 档，不要互相推断。
- 服务端主机侧加固（systemd 单元的 `Environment=`、`/proc/<pid>/environ`、`health.json.config`、离线自检脚本）在本仓库没有通道，无法从 RAGFlow 侧提前发现「服务被手起或换单元」导致的配置退化，只能等报错。

## 2. 本地 vLLM embedding / rerank

### 2.1 端点与模型

| 端口 | 模型 | 关键能力 |
|---|---|---|
| 8000 | `Qwen3-Embedding-4B` | 固定输出 **2560 维**；`max_model_len=2048`、`max_num_batched_tokens=8192` |
| 8001 | `Qwen3-Reranker-4B` | `max_model_len=2048`；支持 `/v1/rerank`、`/score`、`/classify`；**不支持 embeddings** |

### 2.2 长度语义（最容易搞错的一条）

- `max_model_len` 约束**每一条输入序列**；`max_num_batched_tokens` 才是调度批次预算。
- 「整个 embedding 数组共享 1024 token 上限」的判断**不成立**。当时 1025 token 报 HTTP 400 的直接原因是**单条**超限，不是数组求和。

### 2.3 客户端预算

- **Embedding**：每条输入按 **1024** 个本地估算 token 截断，保留 16 条 item batching，**不再按数组总 token 拆成多个请求**。上游仍返回明确的 `input_tokens` / `context length` 400 时，客户端拆成单条重试，只把实际超限的文本逐次减半，正常短文本保持原文。
- **Rerank**：query 截到最多 **256** 个本地估算 token，每个 `query + document` 控制在 **768** token 内（按上游 `max_model_len=2048` 的 **3/8** 保守比例收紧）。之所以不是 1024：真实调用证明本地 1024 的组合经 Qwen yes/no 分类模板扩展后达到 **2049 token** → 400；768 用于在覆盖模板与 tokenizer 开销的前提下保留完整 512-token document。
- 其他 OpenAI-compatible embedding 与 Cohere rerank 模型**保持原行为**，不引入全局兼容分支。
- 默认配置只影响新解析的系统默认模型；已有租户、知识库和模型实例可能继续引用旧的 rerank ID 或 URL，迁移时要按记录 ID 原地更新。

### 2.4 残余风险

- RAGFlow 与 Qwen 的 tokenizer 对中文、OCR 和表格文本的估算不同，**不能按 2048 贴线发送**。
- 自定义换行 delimiter 仍可能生成远超 `chunk_token_num=512` 的块；当前只在 provider 边界截断防请求失败，**分块质量本身另行治理**。
