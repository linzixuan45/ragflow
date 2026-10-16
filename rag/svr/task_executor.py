#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
import argparse
import time

from rag.svr.task_executor_refactor.recording_context import NullRecordingContext, get_recording_context, set_recording_context
from rag.svr.task_executor_refactor.task_manager import TaskManager

start_ts = time.time()

# LiteLLM fetches a model cost map from GitHub during import unless this is set.
# Parser pods should not block startup on external network access.
import os

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # no internet, save about 10s


import asyncio
import copy
import faulthandler
import json
import logging

logger = logging.getLogger(__name__)
import os

# from beartype import BeartypeConf
# from beartype.claw import beartype_all  # <-- you didn't sign up for this
# beartype_all(conf=BeartypeConf(violation_type=UserWarning))    # <-- emit warnings from all code
import random
import signal
import socket
import sys
import threading
from datetime import UTC, datetime

import exceptiongroup
from peewee import DoesNotExist

from api.db import PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES
from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from api.db.services.task_service import CANVAS_DEBUG_DOC_ID, GRAPH_RAPTOR_FAKE_DOC_ID, TaskService, has_canceled, requeue_orphaned_tasks
from common import settings
from common.config_utils import show_configs
from common.constants import SVR_CONSUMER_GROUP_NAME, PipelineTaskType
from common.exceptions import TaskCanceledException
from common.log_utils import init_root_logger
from common.signal_utils import start_tracemalloc_and_snapshot, stop_tracemalloc
from common.versions import get_ragflow_version
from rag.graphrag.utils import chat_limiter
from rag.svr.task_executor_limiter import (
    chunk_limiter,
    embed_limiter,
    kg_limiter,
    minio_limiter,
    task_limiter,
)
from rag.utils.redis_conn import REDIS_CONN, RedisDistributedLock

TASK_TYPE_TO_PIPELINE_TASK_TYPE = {
    "dataflow": PipelineTaskType.PARSE,
    "raptor": PipelineTaskType.RAPTOR,
    "graphrag": PipelineTaskType.GRAPH_RAG,
    "mindmap": PipelineTaskType.MINDMAP,
    "memory": PipelineTaskType.MEMORY,
    "artifact": PipelineTaskType.ARTIFACT,
    "skill": PipelineTaskType.SKILL,
}

UNACKED_ITERATOR = None
# Task type and executor index (consistent with SAAS version)
TASK_TYPE = "common"
TE_IDX = "0"

BOOT_AT = datetime.now().astimezone().isoformat(timespec="milliseconds")
PENDING_TASKS = 0
LAG_TASKS = 0
DONE_TASKS = 0
FAILED_TASKS = 0

CURRENT_TASKS = {}

WORKER_HEARTBEAT_TIMEOUT = int(os.environ.get("WORKER_HEARTBEAT_TIMEOUT", "120"))
# Interval between sweeps for unfinished tasks whose queue message was lost,
# and the grace period that keeps a freshly queued task from being seen as lost
# while the API is still publishing its message.
ORPHAN_SWEEP_INTERVAL = int(os.environ.get("ORPHAN_SWEEP_INTERVAL", "300"))
ORPHAN_GRACE_SECONDS = int(os.environ.get("ORPHAN_GRACE_SECONDS", "300"))
stop_event = threading.Event()


def signal_handler(sig, frame):
    logger.info("Received interrupt signal, shutting down...")
    stop_event.set()
    time.sleep(1)
    sys.exit(0)


def set_progress(task_id, from_page=0, to_page=-1, prog=None, msg="Processing..."):
    try:
        if prog is not None and prog < 0:
            msg = "[ERROR]" + msg
        cancel = has_canceled(task_id)

        if cancel:
            msg += " [Canceled]"
            prog = -1

        if to_page > 0 and msg and from_page < to_page:
            msg = f"Page({from_page + 1}~{to_page + 1}): " + msg
        if msg:
            msg = datetime.now().astimezone().strftime("%H:%M:%S") + " " + msg
        d = {"progress_msg": msg}
        if prog is not None:
            d["progress"] = prog

        TaskService.update_progress(task_id, d)

        if cancel:
            raise TaskCanceledException(msg)
        logger.info(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}")
    except TaskCanceledException:
        raise
    except DoesNotExist:
        logger.warning(f"set_progress({task_id}) got exception DoesNotExist")
    except Exception:
        logger.exception(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}")


async def collect():
    global FAILED_TASKS
    global UNACKED_ITERATOR

    svr_queue_names = settings.get_svr_queue_names(TASK_TYPE)

    redis_msg = None
    try:
        if not UNACKED_ITERATOR:
            UNACKED_ITERATOR = REDIS_CONN.get_unacked_iterator(svr_queue_names, SVR_CONSUMER_GROUP_NAME, CONSUMER_NAME)
        try:
            redis_msg = next(UNACKED_ITERATOR)
        except StopIteration:
            for svr_queue_name in svr_queue_names:
                redis_msg = REDIS_CONN.queue_consumer(svr_queue_name, SVR_CONSUMER_GROUP_NAME, CONSUMER_NAME)
                if redis_msg:
                    break
    except Exception:
        logger.exception("collect got exception")
        return None, None

    if not redis_msg:
        return None, None
    msg = redis_msg.get_message()
    if not msg:
        logger.error(f"collect got empty message of {redis_msg.get_msg_id()}")
        redis_msg.ack()
        return None, None

    canceled = False
    if msg.get("doc_id", "") in [GRAPH_RAPTOR_FAKE_DOC_ID, CANVAS_DEBUG_DOC_ID]:
        task = msg
        if task["task_type"] in PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES:
            task = TaskService.get_task(msg["id"], msg["doc_ids"])
            if task:
                task["doc_id"] = msg["doc_id"]
                task["doc_ids"] = msg.get("doc_ids", []) or []
    elif msg.get("task_type") == PipelineTaskType.MEMORY.lower():
        _, task_obj = TaskService.get_by_id(msg["id"])
        task = task_obj.to_dict()
    else:
        task = TaskService.get_task(msg["id"])

    if task:
        canceled = has_canceled(task["id"])
    if not task or canceled:
        state = "is unknown" if not task else "has been cancelled"
        FAILED_TASKS += 1
        logger.warning(f"collect task {msg['id']} {state}")
        redis_msg.ack()
        return None, None

    task_type = msg.get("task_type", "")
    task["task_type"] = task_type
    # Per-doc fan-out task types (today: doc-scoped raptor) carry their
    # participating doc id list on the Redis message but not on the DB
    # row. The KB-scoped branch above already does this for FAKE doc
    # tasks; mirror here so ``ctx.doc_ids`` is populated for the
    # per-doc path too.
    if "doc_ids" in msg and not task.get("doc_ids"):
        task["doc_ids"] = msg.get("doc_ids", []) or []
    if task_type[:8] == "dataflow":
        task["tenant_id"] = msg["tenant_id"]
        task["dataflow_id"] = msg["dataflow_id"]
        task["kb_id"] = msg.get("kb_id", "")
    if task_type[:6] == "memory":
        task["memory_id"] = msg["memory_id"]
        if msg.get("tenant_id"):
            task["tenant_id"] = msg["tenant_id"]
        task["source_id"] = msg["source_id"]
        task["message_dict"] = msg["message_dict"]
    return redis_msg, task


async def handle_task():
    global DONE_TASKS, FAILED_TASKS
    redis_msg, task = await collect()
    if not task:
        await asyncio.sleep(5)
        return

    task_type = task["task_type"]
    pipeline_task_type = TASK_TYPE_TO_PIPELINE_TASK_TYPE.get(task_type, PipelineTaskType.PARSE) or PipelineTaskType.PARSE
    task_id = task["id"]
    try:
        CURRENT_TASKS[task["id"]] = copy.deepcopy(task)
        logger.info(f"-----run task executor:{task_id}, {task.get('name', '')}, doc id:{task.get('doc_id', '')}")
        set_recording_context(NullRecordingContext())
        await TaskManager.run_refactored_task(task, chat_limiter, minio_limiter, chunk_limiter, embed_limiter, kg_limiter, set_progress, has_canceled)

        DONE_TASKS += 1
        CURRENT_TASKS.pop(task_id, None)
        logger.info(f"handle_task done for task {json.dumps(task)}")
    except TaskCanceledException as e:
        DONE_TASKS += 1
        CURRENT_TASKS.pop(task_id, None)
        logger.info(f"handle_task canceled for task {task_id}: {getattr(e, 'msg', str(e))}")
    except Exception as e:
        FAILED_TASKS += 1
        CURRENT_TASKS.pop(task_id, None)
        try:
            err_msg = str(e)
            while isinstance(e, exceptiongroup.ExceptionGroup):
                e = e.exceptions[0]
                err_msg += " -- " + str(e)
            set_progress(task_id, prog=-1, msg=f"[Exception]: {err_msg}")
        except Exception:
            logger.exception("[Exception]")
        logger.exception(f"handle_task got exception for task {json.dumps(task)}")
    finally:
        if not task.get("dataflow_id", ""):
            referred_document_id = None
            if task_type in ["graphrag", "raptor", "mindmap", "artifact", "skill"]:
                # KB-level fan-out tasks store the participating doc list in
                # task["doc_ids"]; the first entry is used as a referent so
                # the pipeline operation log has something to anchor to.
                referred_document_id = (task.get("doc_ids") or [None])[0]
            ret = PipelineOperationLogService.record_pipeline_operation(
                document_id=task["doc_id"], pipeline_id="", task_type=pipeline_task_type, task_id=task_id, referred_document_id=referred_document_id
            )
            get_recording_context().save_func_return_value("PipelineOperationLogService.record_pipeline_operation", ret)

    redis_msg.ack()


async def get_server_ip() -> str:
    # get ip by udp
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception as e:  # noqa: BLE001
        logger.error(str(e))
        return "Unknown"


async def report_status():
    """
    Periodically reports the executor's heartbeat
    """
    global PENDING_TASKS, LAG_TASKS

    ip_address = await get_server_ip()
    pid = os.getpid()

    # Register the executor in Redis
    REDIS_CONN.sadd("TASKEXE", CONSUMER_NAME)
    redis_lock = RedisDistributedLock("clean_task_executor", lock_value=CONSUMER_NAME, timeout=60)

    while True:
        now = datetime.now(UTC)
        now_ts = now.timestamp()

        group_info = REDIS_CONN.queue_info(settings.get_svr_queue_name(0), SVR_CONSUMER_GROUP_NAME) or {}
        PENDING_TASKS = int(group_info.get("pending", 0))
        LAG_TASKS = int(group_info.get("lag", 0))

        current = copy.deepcopy(CURRENT_TASKS)
        heartbeat = json.dumps(
            {
                "ip_address": ip_address,
                "pid": pid,
                "name": CONSUMER_NAME,
                "now": now.astimezone().isoformat(timespec="milliseconds"),
                "boot_at": BOOT_AT,
                "pending": PENDING_TASKS,
                "lag": LAG_TASKS,
                "done": DONE_TASKS,
                "failed": FAILED_TASKS,
                "current": current,
            }
        )

        # Report heartbeat to Redis
        try:
            REDIS_CONN.zadd(CONSUMER_NAME, heartbeat, now_ts)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to report heartbeat: {e}")
        else:
            logger.debug(f"{CONSUMER_NAME} reported heartbeat: {heartbeat}")

        # Clean up own expired heartbeat
        try:
            REDIS_CONN.zremrangebyscore(CONSUMER_NAME, 0, now_ts - 60 * 30)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to clean heartbeat: {e}")

        # Clean other executors
        lock_acquired = False
        try:
            lock_acquired = redis_lock.acquire()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Failed to acquire Redis lock: {e}")
        if lock_acquired:
            try:
                task_executors = REDIS_CONN.smembers("TASKEXE") or set()
                for worker_name in task_executors:
                    if worker_name == CONSUMER_NAME:
                        continue
                    try:
                        last_heartbeat = REDIS_CONN.REDIS.zrevrange(worker_name, 0, 0, withscores=True)
                    except Exception as e:  # noqa: BLE001
                        logger.warning(f"Failed to read zset for {worker_name}: {e}")
                        continue

                    if not last_heartbeat or now_ts - last_heartbeat[0][1] > WORKER_HEARTBEAT_TIMEOUT:
                        logger.info(f"{worker_name} expired, removed")
                        REDIS_CONN.srem("TASKEXE", worker_name)
                        REDIS_CONN.delete(worker_name)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Failed to clean other executors: {e}")
            finally:
                redis_lock.release()
        await asyncio.sleep(30)


async def recover_orphaned_tasks():
    """Put unfinished tasks whose queue message was lost back on the queue.

    Nothing links a task row to its message after it is published, and the
    broker only redelivers to the consumer that read a message, so a message
    that is acked or skipped without the task running leaves its document
    RUNNING forever. See :func:`requeue_orphaned_tasks`.
    """
    redis_lock = RedisDistributedLock("recover_orphaned_tasks", lock_value=CONSUMER_NAME, timeout=max(ORPHAN_SWEEP_INTERVAL, 60))
    # Let report_status register this executor before it starts judging which
    # consumers are gone, otherwise it could steal a live executor's backlog.
    await asyncio.sleep(30)
    while True:
        try:
            if redis_lock.acquire():
                try:
                    requeued = requeue_orphaned_tasks(exclude_ids=set(CURRENT_TASKS), grace_seconds=ORPHAN_GRACE_SECONDS)
                    if requeued:
                        logger.warning(f"recover_orphaned_tasks requeued {len(requeued)} lost task(s): {requeued}")
                finally:
                    redis_lock.release()
        except Exception:
            logger.exception("recover_orphaned_tasks got exception")
        await asyncio.sleep(ORPHAN_SWEEP_INTERVAL)


async def task_manager():
    try:
        await handle_task()
    finally:
        task_limiter.release()


async def main():
    # Stagger executor startup to prevent connection storm to Infinity
    # Extract worker number from CONSUMER_NAME (e.g., "task_executor_abc123_5" -> 5)
    try:
        worker_num = int(CONSUMER_NAME.rsplit("_", 1)[-1])
        # Add random delay: base delay + worker_num * 2.0s + random jitter
        # This spreads out connection attempts over several seconds
        startup_delay = worker_num * 2.0 + random.uniform(0, 0.5)
        if startup_delay > 0:
            logger.info(f"Staggering startup by {startup_delay:.2f}s to prevent connection storm")
            await asyncio.sleep(startup_delay)
    except (ValueError, IndexError):
        pass  # Non-standard consumer name, skip delay

    logger.info(r"""
    ____                      __  _
   /  _/___  ____ ____  _____/ /_(_)___  ____     ________  ______   _____  _____
   / // __ \/ __ `/ _ \/ ___/ __/ / __ \/ __ \   / ___/ _ \/ ___/ | / / _ \/ ___/
 _/ // / / / /_/ /  __(__  ) /_/ / /_/ / / / /  (__  )  __/ /   | |/ /  __/ /
/___/_/ /_/\__, /\___/____/\__/_/\____/_/ /_/  /____/\___/_/    |___/\___/_/
          /____/
    """)
    logger.info(f"RAGFlow ingestion version: {get_ragflow_version()}")
    show_configs()
    settings.init_settings()
    settings.configure_parallel_devices()
    logger.info(f"default embedding config: {settings.EMBEDDING_CFG}")
    settings.print_rag_settings()
    if sys.platform != "win32":
        signal.signal(signal.SIGUSR1, start_tracemalloc_and_snapshot)
        signal.signal(signal.SIGUSR2, stop_tracemalloc)
    TRACE_MALLOC_ENABLED = int(os.environ.get("TRACE_MALLOC_ENABLED", "0"))
    if TRACE_MALLOC_ENABLED:
        start_tracemalloc_and_snapshot(None, None)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    report_task = asyncio.create_task(report_status())
    recover_task = asyncio.create_task(recover_orphaned_tasks())
    tasks = []

    logger.info(f"RAGFlow ingestion is ready after {time.time() - start_ts}s initialization.")
    try:
        while not stop_event.is_set():
            await task_limiter.acquire()
            t = asyncio.create_task(task_manager())
            tasks.append(t)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)
        recover_task.cancel()
        await asyncio.gather(recover_task, return_exceptions=True)
    logger.error("BUG!!! You should not reach here!!!")


if __name__ == "__main__":
    # Parse command line arguments (consistent with SAAS version)
    parser = argparse.ArgumentParser(description="Task Executor")
    parser.add_argument("-i", "--index", type=str, default="0")
    parser.add_argument("-t", "--type", type=str, default="common", help="[common, graphrag, raptor, resume]")
    args = parser.parse_args()

    # Update global variables
    TASK_TYPE = args.type
    TE_IDX = args.index
    CONSUMER_NAME = f"task_executor_{TASK_TYPE}_{TE_IDX}"

    faulthandler.enable()
    init_root_logger(CONSUMER_NAME)
    try:
        asyncio.run(main())
    except Exception:
        logger.exception("Unhandled exception")
        sys.exit(1)
