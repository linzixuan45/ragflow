# 任务队列与 task executor

> 覆盖：Redis 任务流的队列/消费组结构、任务丢失后的对账恢复、仍存在的兜底缺口与排障口径。
> 运行与重建口径见 [runtime-env.md](runtime-env.md)；被队列消费的解析行为见 [parse-pipeline.md](parse-pipeline.md)。
> 记录时点 2026-10-02，引用代码时优先看符号名。

## 1. 队列结构

- 队列是 Redis 流 `te.<priority>.common`（`common/settings.py` 的 `get_svr_queue_name`），消费组 `rag_flow_svr_task_broker`。
- `ack()` 只有三条调用路径（`rag/svr/task_executor.py`）：**处理完成**、**空消息**、以及 `TaskService.get_task` **取不到或已取消**。后两条是静默丢弃消息的来源。
- 入队只发生在 `TaskService.queue_tasks`（显式解析或重跑）内。

## 2. 丢失队列消息的任务永久停在 RUNNING（已修，2026-10-02）

**现象**

两份文档（同属一个知识库）自 2026-09-22 起一直是 `run=1`（RUNNING），`progress` 冻结在 0.00810874 / 0.00651349，`progress_msg` 恒为「0 tasks are ahead in the queue...」，但 `update_time` 仍每约 25s 刷新。

**根因**

- 两条任务的消息**仍在流里**（`xrange` 可查到），但 `xinfo groups` 显示 `entries-read == xlen`、`lag=0`、`pending=14`（最老 2026-07-24、最新 2026-09-21，都早于这两条）→ 消息**早已被投递并 ack**。
- DB 里 `task.progress=0`、`document.run=RUNNING`，可排除「处理完成」，说明是在「空消息」或「取不到/已取消」分支被丢弃的（当日日志已轮转，无法确证是哪一条）。
- 代码里**没有任何**把 DB 中遗留未完成任务重新入队的路径 → 文档不会自行恢复。
- 两处结构性缺口：`get_unacked_iterator` 只按**本消费者名**读自己的 PEL（`XREADGROUP ... <own consumer>`），死在已消失消费者名下的 pending 消息没有任何存活消费者会认领；`get_pending_msg` / `requeue_msg` 虽已存在但**从不被调用**。
- 之所以看起来「还活着」：进度同步（`document_service.py`）每个周期重写 `progress_msg` 与 `update_time`；文案恒为 0 是因为 `get_queue_length` 取的是消费组 lag，此处 `lag=0`。

**处理**：补上「任务丢失对账」这条缺失路径。

- `rag/utils/redis_conn.py`：新增 `queue_backlog(queue_names, group_name, stale_pending_ms, alive_consumers)`，把积压分成「还会被投递」（未投递段 + 存活消费者名下 pending）与「永远不会再被投递」（已消失消费者名下、闲置 ≥ `stale_pending_ms` 的 pending）；新增 `queue_ack`；`requeue_msg` 改为有返回值，并且**条目不存在时不再 ack**（原实现 `xrange` 取空仍会 `xack`，会静默丢消息）。
- `api/db/services/task_service.py`：新增 `unfinished_parse_tasks_of_running_docs(queue_suffix)`（`task_type=""`、`progress<1`、`Document.run=RUNNING`、按 `parser_id` 区分 `common`/`resume` 队列）与 `progress_by_ids`；新增模块级 `requeue_orphaned_tasks(exclude_ids, grace_seconds, queue_suffix, stale_pending_ms)`：DB 未完成且消息既不在未投递段也不在存活消费者 PEL 的任务重新 `queue_product`；PEL 里死在已消失消费者名下且任务仍未完成的 `requeue_msg`（复制到流尾 + ack 旧条目），行已删或已完成的 `queue_ack` 丢弃。`CURRENT_TASKS` 与 `grace_seconds`（默认 300s，覆盖「插 task 行」到「发消息」的窗口）防误判；**先读 DB 再动 PEL**——DB 不可达时不允许把 pending 判成废弃。
- `rag/svr/task_executor.py`：新增 `recover_orphaned_tasks()` 周期协程（`ORPHAN_SWEEP_INTERVAL` 默认 300s、`ORPHAN_GRACE_SECONDS` 默认 300s、启动后 30s 首扫，`RedisDistributedLock("recover_orphaned_tasks")` 保证单实例执行），在 `main()` 里与 `report_status` 一起创建、退出时一并取消。重投上限仍由 `get_task` 的 `retry_count>=3` 兜底（第三次后标 FAIL），不会无限重投。

**验证证据**：首扫把 PEL 里 14 条挂在已消失消费者名下、task 行已删除的旧消息逐条 `queue_ack` 丢弃，并把两份卡住的文档重新入队；两条任务随后跑完（`Task done` 1.21s / 4.33s），`document.run` 由 `RUNNING 2 / DONE 4393` 变为 `RUNNING 0 / DONE 4395`，两份文档各 1 chunk（80 / 51 token），`te.0.common` 的 PEL 由 14 → **0**、`entries-read` 9015 → 9017、`lag=0`；第二轮扫描零动作零日志（幂等）。新增聚焦测试 26 项全过（`test/unit_test/api/db/services/test_task_orphan_recovery.py`、`test/unit_test/rag/utils/test_queue_backlog.py`）。

**残余风险**

1. 恢复执行的任务若 `doc:chunking_*` 计数器已过期，会打印 `skip post-processing to avoid duplicate finalizers` 并跳过收尾（本次两份文档正是如此，RAPTOR 汇总未生成），需要时得显式重跑。
2. 挂载 `task_service.py` 会把 fork 的 xlsx 路由（`uses_table_row_tasks`：`.xlsx` 走 MinerU 整文件任务而非按行任务）一并带进容器——这是仓库 HEAD 的行为，容器此前用的是镜像内旧版，本次未单独回归标注质量。
3. 丢失任务最坏要等一个扫描周期（默认 300s）才被找回；缩短周期会加大 Redis/DB 轮询。
4. 同一文档仍用「`update_time` 每 25s 刷新 + `progress` 不动」伪装存活，排障口径不变。
5. 本次只覆盖 `task_type=""` 的普通解析任务（`common` / `resume` 队列）；raptor / graphrag / memory / dataflow 这类**带额外字段**的任务不在扫描范围内，它们的消息若丢失仍无兜底。

## 3. 排障口径

- 判断任务是否真的在跑，**不能只看 `progress_msg` 文案**：它由进度同步周期性重写。要看 `progress` 是否推进、`update_time` 是否只是被刷新。
- `xinfo groups` 的 `lag=0` 只表示**消费组已读完整个流**，不代表没有任务在跑；`get_queue_length` 取的就是这个值，所以文案恒为 0。
- 对照三处证据：`xrange`（消息还在不在流里）、`xinfo groups` 的 `entries-read` / `lag` / `pending`（是否已投递并 ack、pending 挂在哪个消费者名下）、DB 的 `task.progress` 与 `document.run`（业务状态）。
- 有积压时看 `queue_backlog()` 的两分类结果，而不是原始 `xlen`。
