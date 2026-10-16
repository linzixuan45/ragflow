# 试验 DeerFlow 侧 agent 调用 corpus_fs 耗时实测（2026-10-04）

> 上一版：[`21-版本V3.9-尾延迟两类根因修复-2026-10-02.md`](21-版本V3.9-尾延迟两类根因修复-2026-10-02.md)
> 关联：[`../corpus_fs多库检索耗时分析-2026-10-01.md`](../corpus_fs多库检索耗时分析-2026-10-01.md) §8.2「未做 DeerFlow → Gateway → RAGFlow 的端到端复测」
> 本版**不改产品代码**，是一次「补测 + 口径校准」试验，记为 `22-试验`（不占用优化编号）。
> 脚本：[`../scripts/deerflow/bench_deerflow_corpus_fs.py`](../scripts/deerflow/bench_deerflow_corpus_fs.py)、[`../scripts/deerflow/bench_deerflow_agent_run.py`](../scripts/deerflow/bench_deerflow_agent_run.py)

---

## 0. 一句话结论

在 **V3.9 代码 + 用户 324 的真实 9 个可见库（361 文档）** 上，agent 真正发起的一次
`knowledge_search` 端到端 **p50 0.604s**（RAGFlow 服务端 0.366s + Gateway/DeerFlow 0.238s）；
DeerFlow 工具层自身只占 **6.8ms**（不随库数增长），**Gateway 一跳才是链路上最大的非引擎开销（+0.18s / 9 库）**。
`corpus_fs` 只占该轮 agent 会话（11.4~16.7s）的 **约 4%**——用户感知的慢不在这条链路上。

⚠ **三条硬性限制（读数前必看）**：

1. **范围比原始抱怨场景小约 18 倍**：本次 9 库 / 361 文档；V0 基线是 12 库 / 6531 文档、最大单库 1912 文档。
   绝对耗时**不可**与 `L(12)=17.95s`（V0）或 `L(12)=0.75s`（V3.9）直接比较。
2. **协议里那 12 个固定采样库在现网已经不存在**（见 §3.3），无法按原协议复现梯度。
3. **本机噪声地板 CV=18%（>15% 阈值）**，因此本文件只对 **≥20%** 的差异下结论，其余标"不可分辨"。

---

## 1. 假设与动机

- **假设**：`README.md` §2 的采集协议规定「直连 RAGFlow，不经 DeerFlow」，所以 V0~V3.9 全部结论
  都只证明了 **RAGFlow 引擎侧**变快了；**DeerFlow → Gateway → RAGFlow 整条 agent 链路**是否同样受益，
  以及 gateway / 工具层各占多少，此前**从未实测**（分析文 §8.2 自己列为未验证项）。
- **可证伪的表述**：若链路各层都只是转发，则「agent 工具调用总耗时 − RAGFlow 服务端耗时」应当
  是一个与库数无关的小常数（≲50ms）；若该差值随库数增长，说明 DeerFlow/Gateway 侧存在**按 scope 放大**的成本。
- **改动**：无（只新增两个测量脚本）。本试验的产物是**一条口径**和**一组数**，不是一次优化。

---

## 2. 影响面（四类必填）

### 2.1 接口 / 契约

| 项 | 结论 |
|---|---|
| HTTP 路径 / 方法 | **无变化**，本试验不产出产品代码改动 |
| 请求字段 / 校验 | **无变化** |
| 响应字段 | **无变化** |
| 错误码 / 状态码 | **无变化**（3/3 真实会话 + 全部压测请求均 200） |
| 上游调用方 | **无感知** |

### 2.2 数据与检索质量

| 项 | 结论 |
|---|---|
| 可见文档集合 | **无变化**。脚本只读，未写任何库 |
| 候选池构造 | **无变化** |
| 排序变化 | **无**（同一 query 同一参数；未改排序代码） |
| 已存在缺陷 | **未修未引入**。附带观察：`agent 工具无法指定 N 个任意库`（只能「单库」或「全部可见库」），这是**既有产品语义**，非本次引入 |

### 2.3 性能

| 项 | 结论 |
|---|---|
| 本接口 | 见 §4：9 库 agent 工具调用 p50 **0.604s**（真实会话）/ 0.535~0.612s（确定性） |
| 事件循环 `max_stall` | **未测量**（本试验在 DeerFlow 侧取值，未在 RAGFlow 进程内埋点。21 号 V3.9 的历史值为 0.060s，但那是**邻扰 max**，不是 `max_stall`，不能替代） |
| 其他接口邻扰 | **未测量**（未做邻扰探针） |
| 资源占用 | **未测量** |

### 2.4 运维与回滚

| 项 | 内容 |
|---|---|
| 部署方式 | **无部署**（脚本跑在既有 `deer-flow-langgraph` 容器内 / 宿主机，只读） |
| **回滚命令** | 无需回滚。若要移除产物：删除 `../scripts/deerflow/` 与本文件 |
| 回滚后影响 | 回到「DeerFlow 链路未验证」状态 |
| 依赖的前置版本 | 测量的是 V3.9（`ragflow 577d85cd6`）+ DeerFlow `98a386153` |

---

## 3. 环境与可复现性

### 3.1 环境指纹

- **采集时点（CST）**：2026-10-04 18:44 ~ 18:55
- **RAGFlow git rev（宿主机仓库，容器无 .git）**：`577d85cd6d94d0f96c2cf576ea9393b7e0f3de81`
  - V3.9 两项修复**已确认部署**：`db_models._retry_backoff` 存在、`source_projection_cache` 存在、
    `api/apps/__init__.py` 中每请求 `close_stale(age=30)` 已删除
- **DeerFlow git rev**：`98a3861535ce446120529b44f324814ff002e44b`
- **容器 / Compose 项目**：`ragflow-knowledge-dev-ragflow-gpu-1`（MySQL/ES/MinIO 同项目）、
  `deer-flow-langgraph`、`deer-flow-gateway`、`deer-flow-nginx`
- **模型服务**：embedding `10.100.15.70:8000`、rerank `10.100.15.70:8001`（均可达）
- **测试用户**：`324`（真实用户身份，`issue_signed_token` 现场签发，TTL 2h，未落盘任何 token）

文件 md5（**host 与容器逐字节一致，8/8 已验证**）：

| 文件 | md5 |
|---|---|
| `api/apps/restful_apis/corpus_fs_api.py` | `8c996c4d3afc8f96b52909f553ec0739` |
| `api/apps/services/corpus_fs_service.py` | `823e9da99a0253ba0b265dd62fc9d422` |
| `api/apps/services/corpus_tool_service.py` | `c9063b51c3cf81d91d2d28ae802bc2eb` |
| `api/apps/services/dataset_api_service.py` | `41a3815e25b177e3005fe64b14cbea3c` |
| `rag/nlp/search.py` | `3ef58ac514db4a010e9a90ead451eb6c` |
| `api/apps/services/keyword_search_service.py` | `14ceb8d132c9cac0e113ff9c2594c8ce` |
| `api/apps/services/corpus_recall_service.py` | `937de79093f33b1e29feb95b9a3391f8` |
| `api/apps/services/corpus_context_cache.py` | `3240fafb4f6a19ce1f7bd6df832a637d` |

### 3.2 数据快照（用户 324 的 9 个可见库）

| mount_key | dataset_id | 文档 | chunk | token |
|---|---|---|---|---|
| 开发日志 | `a3a47d2e…` | 221 | 1806 | 604,625 |
| 招投标资料 | `3895f3fa…` | 54 | 5047 | 1,568,806 |
| 2026简历库-应届生 | `d2b87b76…` | 33 | 164 | 55,464 |
| df-agent-kb-…-1 | `0f711712…` | 15 | 241 | 75,576 |
| 视比特-公司资料 | `a8666790…` | 12 | 48 | 21,450 |
| 测试知识库 | `81ada0f4…` | 9 | 605 | 68,458 |
| df-my-docs-… | `cb883bbe…` | 7 | 220 | 73,538 |
| 投标资料 | `748b1122…` | 6 | 212 | 83,774 |
| 可见性测试123 | `a72da4a6…` | 4 | 895 | 352,285 |
| **合计** | | **361** | **9,238** | **2.90M** |

> 对照 V0 样本：12 库 / 6531 文档，最大单库 1912 文档。**本次最大单库仅 221 文档。**

### 3.3 协议偏差（必须声明）

原协议 §2 固定了一张 16 项（后改 12 项）采样库表（`scripts/baseline/collect_baseline_http.py::DATASETS`）。
**这 12 个 dataset_id 在现网已基本不再作为 DeerFlow 知识库存在**：

- 12 个里只有 7 个还能在 `knowledge_datasets` 里查到；
- 5 个公开评测库（DuReader-yesno / CMRC2018 / DuReader-robust / LongBench×2）已不在表内；
- 现存的 CMRC2018 与「模型能力评测」两库被标为 `benchmark` / `model_eval`，
  其中 CMRC2018 的 `agent_retrieval_enabled = false`，**默认已被排除在 agent 隐式检索范围之外**。

因此本试验**无法**按原协议复现 `L(1/2/4/8/12)`，改为「**以真实用户当前可见库为 scopes**」，
并把库数梯度定义为「该用户可见库的前 N 个」。**本文件的 L(N) 与历史 L(N) 不是同一个东西**，
已在 §1 与 §5 显式标注。

### 3.4 采集条件（会影响读数）

- 采集期间**存在其他真实流量**：RAGFlow 访问日志持续出现来自 `172.20.0.1` 的
  `/fs/tree`、来自 `172.16.20.100` 的 `/fs/content`，`deer-flow-gateway` CPU 约 33%。
  违反 README §2「采集纪律 1（不对同机跑其他负载）」，**无法在本机复现完全静默条件**。
- 宿主机 load average ≈ 0.6，未跑构建/测试。
- 已知环境缺陷（20 号记录提到的 task executor 崩溃循环）**本次未观察到**：
  近 10 分钟容器日志中 `task_executor|timeout|MinerU` 匹配数为 0。

### 3.5 采集命令

```bash
# ① 确定性三层对照（容器内，真调 agent 工具）
docker cp scripts/deerflow/bench_deerflow_corpus_fs.py deer-flow-langgraph:/tmp/
docker exec -w /app/backend deer-flow-langgraph .venv/bin/python /tmp/bench_deerflow_corpus_fs.py \
  --mode all --user 324 --scopes 1,2,4,8,9 --runs 5 --warmup 1 \
  --out /tmp/2026-10-04T1844-deerflow-tool.json

# ② 真实 agent 会话（宿主机，与前端同入口 nginx :5678/api/langgraph）
python3 scripts/deerflow/bench_deerflow_agent_run.py --user 324 --runs 3 \
  --out baseline/2026-10-04T1850-deerflow-agent-run.json

# ③ 同版本噪声地板（9 库，20 次）
docker exec -w /app/backend deer-flow-langgraph .venv/bin/python /tmp/bench_deerflow_corpus_fs.py \
  --mode tool --user 324 --scopes 9 --runs 20 --warmup 2 --out /tmp/m11.json
```

固定参数（与协议一致）：`query="什么是人工智能"`、`limit=10`、`per_document_limit=3`、
`mode="hybrid"`、`path="/knowledge"`。

---

## 4. 实验数据

### 4.1 被测量的三层链路

```
knowledge_search_tool                                  ← ① tool   （agent 工具层）
  └─ KnowledgeProvider._batch_semantic_search
       └─ POST {gateway}/api/knowledge/fs/search        ← ② gateway（DeerFlow 网关层）
            └─ Gateway.fs_search_batch
                 └─ POST {ragflow}/api/v1/datasets/corpus-search  ← ③ ragflow（引擎层）
```

三层用**完全相同的 scopes 与参数**测量，可直接相减。
（工具层只接受 `path`，无法指定任意 N 个库；脚本按 case **固定 provider 的可见挂载集合**，
使三层看到同一组 scopes——这是测量装置，不是产品行为。）

### 4.2 三层耗时（n=5，中位数；warmup 1 次已丢弃）

| scopes | ① tool p50 | ② gateway p50 | ③ ragflow p50 | ② − ③（Gateway 开销） | ① − ②（DeerFlow 工具开销） |
|---|---|---|---|---|---|
| 1 | 0.1478s | 0.1437s | 0.1030s | +0.0407s | +0.0041s |
| 2 | 0.1810s | 0.1788s | 0.1416s | +0.0372s | +0.0022s |
| 4 | 0.3979s | 0.3390s | 0.2577s | +0.0813s | +0.0589s |
| 8 | 0.5444s | 0.4882s | 0.3774s | +0.1108s | +0.0562s |
| 9 | 0.5352s | 0.5483s | 0.3687s | +0.1796s | −0.0131s |

逐次值见 `../baseline/2026-10-04T1844-deerflow-tool.json`。

**关键读数：**

- **① − ② 不是稳定的正数**（4 库 +0.059s、9 库 −0.013s），说明它落在噪声内；
  但**进程内 `HttpRecorder` 直接量到的「工具层本地开销」非常稳定**：

  | scopes | 1 | 2 | 4 | 8 | 9 |
  |---|---|---|---|---|---|
  | 工具层本地开销 p50 | 8.5ms | 7.7ms | 7.5ms | 6.8ms | **6.8ms** |

  → **DeerFlow 工具层是常数级 ~7ms 开销，不随库数增长，可忽略。**

- **② − ③ 随库数单调增长**（+0.041 → +0.180s），是本链路最大的非引擎成本。

### 4.3 真实 agent 会话（n=3）

同一用户、同一 query，通过 `nginx :5678/api/langgraph` 发起真实会话，
assistant `lead_agent`，`context.authentication` 携带真实 token。3/3 都在第一轮调用了 `knowledge_search`。

| # | 工具节点 span（`updates`） | 含参数流式的上界（`messages`） | RAGFlow 服务端 | 整轮 agent 会话 |
|---|---|---|---|---|
| 1 | 0.6036s | 0.9039s | 0.3661s | 11.42s |
| 2 | 0.7316s | 1.1994s | 0.3715s | 12.64s |
| 3 | 0.6013s | 1.0009s | 0.3597s | 16.67s |
| **p50** | **0.6036s** | 1.0009s | **0.3661s** | 12.64s |

**交叉验证成立**：真实会话里 RAGFlow 服务端 p50 **0.3661s**，与 §4.2 直连引擎的 9 库 p50
**0.3687s** 几乎相同（差 0.7%）。→ 确定性装置测到的确实是 agent 实际走的那条链路，没有偏差。

**链路分解（9 库，agent 视角）**：

```
RAGFlow 引擎          0.366s   (61%)
Gateway + 序列化      +0.238s  (39%)   ← 其中 DeerFlow 工具层仅 0.007s
─────────────────────────────
agent 看到的工具耗时   0.604s
```

**口径说明**：`updates` span = 「model 节点产出 tool_call」到「tools 节点回填 ToolMessage」的墙钟差，
是最接近工具执行的口径；`messages` span 会多算模型逐字生成工具参数的时间，只作上界。
RAGFlow 服务端耗时来自容器访问日志最后一段数字（**微秒**），已用已知耗时的直连请求标定：
客户端实测 0.4287s 的 9 库请求 → 日志 `361135`（=0.361s，差值 0.068s 为 68KB 响应体传输 + 解析）。

> ⚠ **口径陷阱（本试验纠正的一个错误）**：该日志字段是
> `... <方法> <路径> <HTTP版本> <状态> <字节数> <耗时us>`，
> 其中 `1.1` 是 **HTTP 版本**而不是秒数。按秒读会把服务端耗时**高估约 3 倍**
> （0.36s 读成 1.1s）。本仓此前的采集脚本未使用该字段，故历史数据未受影响。

### 4.4 同版本噪声地板（9 库工具层，n=20）

| 指标 | 值 |
|---|---|
| min / p25 / p50 / p75 / max | 0.4798 / 0.5250 / **0.6120** / 0.6867 / 0.9069s |
| CV（含尾） | **0.180** |
| robust_cv = (p75−p25)/p50 | 0.264 |
| tail_events（>1.5× p50） | **0 / 20** |

**判定**：`CV=18% > 15%`，按 README §2 本档的**跨版本结论应降级为「待定」**。
本机在采集期有其他真实流量，**任何 <20% 的差异在本环境下不可分辨**。

单次尾部：4 库那组出现过一次 **0.7786s**（p50 0.3979s 的 1.96×），n=5 内 1 次；
20 次的 9 库组反而 0 次。与 19/20/21 号观察到的「间歇性真实尾延迟」一致，但样本量不足以定档。

### 4.5 库数缩放

引擎层（③ ragflow）从 1 库 0.103s 到 9 库 0.369s，**近似线性**（约 +0.033s/库），
与 V3.x 融合召回后的预期一致（**未**出现 V0 那种 1.3~1.6s/库的线性放大）。

### 4.6 原始数据

- [`../baseline/2026-10-04T1844-deerflow-tool.json`](../baseline/2026-10-04T1844-deerflow-tool.json) — §4.2 三层对照（含逐次值与每次出站 HTTP 明细）
- [`../baseline/2026-10-04T1850-deerflow-agent-run.json`](../baseline/2026-10-04T1850-deerflow-agent-run.json) — §4.3 真实 agent 会话 3 次
- [`../baseline/2026-10-04T1855-deerflow-m11.json`](../baseline/2026-10-04T1855-deerflow-m11.json) — §4.4 噪声地板 20 次

未落盘：RAGFlow 原始访问日志（只提取了耗时，未存档 `.raw.log`）。**标为欠缺。**

---

## 5. 结论与对比

### 5.1 逐项对比

| 项 | 结论 |
|---|---|
| 「DeerFlow 工具层有额外开销」 | **否证**：常数 ~6.8ms，不随库数增长 |
| 「Gateway 只是转发」 | **否证**：+0.041s（1 库）→ +0.180s（9 库），**随 scope 放大**，是链路最大非引擎成本 |
| 「V3.x 的优化在 agent 链路同样生效」 | **支持**：引擎层 9 库 0.369s，未出现 V0 的每库 1.3~1.6s 放大 |
| 「corpus_fs 是用户感知慢的主因」 | **不支持（本范围）**：0.60s 的 `knowledge_search` 只占 11.4~16.7s 轮次的约 4% |
| 与历史 `L(12)` 比较 | **不可比**（库集合与规模都不同，见 §3.3） |

### 5.2 本版结论

**采纳为「DeerFlow 侧链路口径 + 基线」**，原因：

1. 补上了分析文 §8.2 自己标注的未验证项，且用**独立第三方证据**（RAGFlow 访问日志）交叉验证通过；
2. 三层分解给出了可执行的下一步指向：**优化目标应落在 Gateway 的按-scope 开销上，而不是工具层**；
3. 发现并纠正了一个**日志字段口径错误**（HTTP 版本被当成秒），避免后续误读。

**不声称**：没有证明「corpus_fs 慢」或「corpus_fs 不慢」——本范围只有 361 文档，
原始抱怨场景是 6531 文档。要回答那个问题必须补重库范围（见 §7）。

### 5.3 与预期的差异

预期是「各层都只是转发，差值应是与库数无关的小常数」。实际**分层**：
工具层确实是小常数（7ms），但 **Gateway 不是**。这是本次唯一超出预期的发现，成因未归因（见 §7）。

---

## 6. 功能守卫与回归

| 守卫 | 结果 | 下限/要求 |
|---|---|---|
| 聚焦单测 | **未跑**——本试验**未改动任何产品代码**，无回归面 | ≥ 80（仅对改代码的版本适用） |
| 契约回归 | **未跑**——未改契约 | 全通过（同上） |
| 质量证据 | **未跑**——未改排序 | 不劣化（同上） |
| 脚本自检 | 两个脚本均实跑通过；3/3 真实会话 200，全部压测请求 200 | 通过 |

---

## 7. 风险与后续

### 7.1 残余风险

- **样本量小**：真实会话 n=3；确定性每组 n=5；噪声地板 n=20。
- **环境不静默**：采集期有其他真实流量（§3.4），CV 18%，<20% 差异不可分辨。
- **可外推性有限**：9 库 / 361 文档，最大单库 221 文档；**不能外推到 1912 文档或 50 库场景**。

### 7.2 未验证项（明确标"未测量"）

1. **Gateway +0.18s 的成因**——未归因。候选：每 scope 的 `_ensure_dataset_allowed` / 权限快照读取、
   `_decorate_search_hits`、Redis token 校验。建议按「每 scope 增量」与「固定增量」拆开测量。
2. **重库范围**——未测。建议在 221 文档以上的库（如 admin 的 23 个库 / 342 的 22 个库）复跑同一梯度。
3. **`max_stall` / 邻扰 M3 / MySQL 连接放大 M6**——未测量（本次未在 RAGFlow 进程内埋点）。
4. **公开评测/标注集质量指标**——未测量（本次未改排序，无回归面）。
5. **RAGFlow 原始 `.raw.log`**——未存档。

### 7.3 下一步建议

1. **补重库范围**：用 `admin`（23 库）或 `342`（22 库）复跑 §4.2 三层对照，
   把「引擎 / Gateway / 工具层」的分离度放到用户实际抱怨的规模上。
2. **归因 Gateway 的按-scope 开销**：在 `app/gateway/routers/knowledge.py::fs_search_batch`
   内对 `_require_dataset_reader` / `_decorate_search_hits` / `mask_search_response` 分段计时。
3. **修协议**：把 §2 的固定采样库表更新为「按用户真实可见库发现」的方式，
   或在实例上重建固定的 12 库样本，否则 `L(N)` 口径无法跨版本比较。
4. **把本试验的两个脚本纳入常规复测**（它们补上了协议里缺失的「DeerFlow 侧」一环）。
