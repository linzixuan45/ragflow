# DeerFlow 集成面

> 覆盖：DeerFlow（消费方）与 RAGFlow（内容正本）之间的内容版本契约、默认空间与账户隔离、以及两次真实 E2E 暴露的回归。
> RAGFlow 侧的 Corpus FS 与检索契约见 [corpus-fs.md](corpus-fs.md)；分工边界见仓库根 [ARCHITECTURE.md](../ARCHITECTURE.md)。

## 1. 内容版本单一数据源（RAGFlow 是正本）

**现象**：Corpus FS 发布状态可能被两个并发轮询请求同时按 `PENDING` 处理，重复切换 active 指针并递增 `content_revision`。逻辑文件删除原先先移除版本数据库行或 Document、再清理 MinIO 对象，对象清理失败后会丢失版本对象元数据或 Corpus FS 路径，无法按原路径安全重试。此外 DeerFlow 会丢弃 RAGFlow 返回的删除失败信息。

**处理（并发激活）**：发布激活使用 `PENDING -> ACTIVE` **条件更新**抢占唯一执行权，只有抢占成功者切换 File 指针并递增 dataset revision。

**处理（删除一致性）**：

- 先依据仍存在的版本元数据清理 derived、publication 和源文件对象；**全部成功后再**删除 Document、版本行、File2Document 和 File。
- 对象清理失败时**保留数据库元数据与逻辑路径**，以便按原路径重试，不产生无法追踪的孤儿。
- 失败响应只返回调用方提交的**虚拟路径**，不暴露内部 file/document ID；DeerFlow 侧透传 `failed_paths`。

**验证证据**：RAGFlow Corpus FS / Markdown / delegation 聚焦测试 54 项通过；DeerFlow 知识网关、Corpus FS 客户端与迁移测试 54 项通过；新增测试覆盖「重复激活只递增一次 revision」「MinIO 失败保留版本行与逻辑文件元数据」「删除失败路径透传」。真实迁移于 2026-08-06 在两仓停机快照后完成：旧目录 2 个逻辑文件、5 个版本全部导入，旧正文 / MySQL `content_sha256` / MinIO 实体内容三方逐条核对，5 个 SHA-256 全部一致，active revision 分别为 3 和 2。

## 2. 默认空间重复创建与账户隔离

**现象**：DeerFlow 打开知识库列表或点击个人空间文件后，RAGFlow 中持续出现带数字后缀的「我的云文档」与「Agent 知识库」；共享服务账户下数据集增多后，先按 RAGFlow 页码取数再执行 DeerFlow ACL 过滤，还可能让用户自己的空间从列表消失。

**根因**：列表 GET 曾带默认空间创建副作用；PostgreSQL 缺少 `kind` 字段时静默丢弃空间类型；并发「检查、创建、打标」没有跨进程锁。共享 Token 只代表**服务身份**，不能当 DeerFlow 租户身份；分页与 ACL 顺序错误会扩大跨租户可见性风险。历史清理若未加载与 Gateway 相同的运行环境，还会读取错误的本地文件访问索引而非实际 PostgreSQL 索引。

**处理**：

- 列表改为**纯读**，默认空间只通过显式接口按需创建。
- 新增 `kind` 迁移、按用户和类型的 PostgreSQL advisory lock、唯一技术名称、创建失败补偿、旧标准空间原地认领。
- 列表先遍历共享服务账户的**完整上游页**，再按 DeerFlow 登记、当前用户 ACL 与 scope 过滤，最后分页。
- 清理脚本要求显式输出访问索引后端，只删除未绑定、空库且描述精确匹配系统预置的后缀空间。

**验证证据**：DeerFlow 后端聚焦测试 132 项、Ruff、前端 TypeScript、ESLint 与 21 项资料包/用户隔离聚焦测试通过。真实 PostgreSQL 迁移完成；清理 29 个无文件历史空壳，删除失败 0，移除 4 条仅指向已删除空壳的陈旧绑定；用户原有含 2 个文件的「我的云文档」被原地认领；连续 2 次显式创建、10 次列表请求前后 RAGFlow 数据集总数不变。

**残余风险**

- 共享 RAGFlow Token 的泄漏、吊销和限流会影响**全部** DeerFlow 用户，且 RAGFlow 原生日志无法区分最终用户。
- 当前必须保持 Token 只在 Gateway 服务端，并以 DeerFlow PostgreSQL ACL 与审计作为租户边界。
- 生产规模扩大后应升级为至少按组织/租户拆分的短期凭证。
- 临时签名验证 Token 未被 LangGraph 接受，真实 Agent 最终回答链未完成（不影响默认空间与 Gateway 列表验收）。

## 3. 目录创建回归：`active_document_id` 误加（已修）

**现象**：真实认证 E2E 首次通过 DeerFlow 创建临时知识库时返回 502；RAGFlow HTTP 200 但业务响应为内部错误，日志显示 `FileService.new_a_file_from_kb()` 访问未定义变量 `doc`。

**根因**：新增 File active 指针时，`active_document_id=doc["id"]` 被误加到**知识库目录创建**函数。目录创建路径没有 Document，导致所有新知识库创建失败。该字段只应由真实文件与 Document 建立关系时维护，**纯目录必须保持为空**。

**处理**：删除目录创建中的错误 active 指针赋值，并增加「目录创建不依赖 Document」的 `FileService` 回归测试。

**验证证据**：认证写入链完成连续保存、stale revision `409 REVISION_CONFLICT`、历史 actor_ref、恢复来源、active 发布、重命名后历史保留、DeerFlow 重启后历史完整；临时文件删除后 MySQL 中对应版本 / File / File2Document / Document 均为 0，MinIO `markdown-derivatives` 与 `markdown-publications` 对应前缀对象均为 0。真实 `Qwen3-32B-INT8` Agent E2E 完成 `kb_ls -> knowledge_search -> kb_cat`，三个工具响应均 200，并通过「认证身份、公共路径、上游 ID 不泄露」断言。

**残余风险**

- 真实 E2E 覆盖了成功发布与旧版本持续可用，但**没有人为制造解析失败**来证明失败 shadow 不会切换 active 指针；该分支目前只由聚焦单测证明。
- 开发配置 `runtime/dev/config.yaml` 按既有开发策略向所有角色授予知识库维护权限，因此开发实例里的普通 user 会被视为 platform manager。**不能把开发实例上的 user 身份当作生产「普通读者」权限的证据**；403 场景是用短时 prod 配置的 Gateway 在真实数据上验证的。
