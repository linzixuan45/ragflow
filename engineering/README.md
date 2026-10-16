# engineering/ — fork 内部工程文档

本目录是 RAGFlow fork 的**内部工程文档**：子系统不变式、接口契约、运行口径、事故与性能台账。

- 用户文档在 [`docs/`](../docs)（上游 Docusaurus 树），**两者不混放**。
- 子系统索引与硬约束在仓库根 [AGENTS.md](../AGENTS.md)。
- 双栈边界与路由/DB 契约在 [ARCHITECTURE.md](../ARCHITECTURE.md)。

## 索引

| 文件 | 覆盖范围 |
|---|---|
| [parse-pipeline.md](parse-pipeline.md) | 解析链路：MinerU 路由与产物形态（PDF / office 分支）、图片进入 chunk 与 source projection 的通道、Dataflow 适配、单文档解析关键路径性能 |
| [corpus-fs.md](corpus-fs.md) | Corpus FS 检索与写路径：图片检索事实、取图通道、opaque 引用密钥、排序与重排、修订号与完整性契约、多库检索性能台账 |
| [task-queue.md](task-queue.md) | Redis 任务流结构、任务丢失对账恢复、兜底缺口与排障口径 |
| [deerflow-integration.md](deerflow-integration.md) | DeerFlow 集成面：内容版本正本契约、默认空间与账户隔离、两次 E2E 回归 |
| [contracts.md](contracts.md) | 外部服务契约：远程 MinerU（版本/端点/排障）、本地 vLLM embedding 与 rerank（长度预算） |
| [runtime-env.md](runtime-env.md) | knowledge-dev overlay 挂载与重建、路由表刷新、GPU/CUDA 库路径、测试与 lint 命令口径 |
| [experiments/corpus-fs/](experiments/corpus-fs/README.md) | Corpus 多库检索的实验记录、标注集与原始数据（`corpus-fs.md` §7 的证据集） |

## 记录规范

正文里的每条记录固定五段，缺段视为未完成：

```markdown
## <主题>（<已修 / 未修 / 已接受>，<日期>）

现象：<可观察到的行为，含具体命令/接口/日志>

根因：<确认后的机制，写清是确认还是推测>

处理：<改动落在哪个符号/配置；未修则写"本次不改"与理由>

验证证据：<测试名、实测数值、复现步骤>

残余风险：<未覆盖的范围与重启条件>
```

- **一主题一段**：同一子系统的后续变化追加进该段，不新建平行段落。
- **必标状态与日期**。标「已修」必须有可复现的验证证据或测试名；标「已接受」必须写重启条件。
- **口径变化必须显式声明新旧不可比**（例如采样协议修订后，旧的库数梯度指标与新口径不能直接比较）。
- **同一事实只写一处**，别处只放链接；禁止把同一段结论抄进多个文件，也禁止把实现细节堆回 [AGENTS.md](../AGENTS.md)。
- **行号会漂移**：引用代码优先给符号名（函数 / 常量 / 类 / 文件），行号只作辅助并标注记录时点。

## 语言

fork 新增内容用中文；与上游同步的文件（[AGENTS.md](../AGENTS.md) 既有小节、[README_zh.md](../README_zh.md)）保留上游原文。
