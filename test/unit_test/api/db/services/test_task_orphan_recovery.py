#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
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
#
import pytest

from api.db.services import task_service as ts

GRACE = 300


@pytest.fixture
def broker(monkeypatch):
    state = {
        "alive": {"exec-live"},
        "inflight": set(),
        "stale": [],
        "published": [],
        "republished": [],
        "acked": [],
        "queue_product_ok": True,
        "requeue_ok": True,
        "candidates": [],
        "progress": {},
        "query_suffixes": [],
    }
    monkeypatch.setattr(ts.REDIS_CONN, "smembers", lambda *_a, **_k: set(state["alive"]))
    monkeypatch.setattr(
        ts.REDIS_CONN,
        "queue_backlog",
        lambda *_a, **_k: (set(state["inflight"]), list(state["stale"])),
    )

    def queue_product(queue, message):
        state["published"].append((queue, message))
        return state["queue_product_ok"]

    monkeypatch.setattr(ts.REDIS_CONN, "queue_product", queue_product)

    def requeue_msg(queue, group_name, msg_id):
        state["republished"].append((queue, group_name, msg_id))
        return state["requeue_ok"]

    monkeypatch.setattr(ts.REDIS_CONN, "requeue_msg", requeue_msg)
    monkeypatch.setattr(ts.REDIS_CONN, "queue_ack", lambda queue, group_name, msg_id: state["acked"].append((queue, msg_id)) or True)

    def unfinished(queue_suffix="common"):
        state["query_suffixes"].append(queue_suffix)
        return list(state["candidates"])

    monkeypatch.setattr(ts.TaskService, "unfinished_parse_tasks_of_running_docs", unfinished)
    monkeypatch.setattr(ts.TaskService, "progress_by_ids", lambda task_ids: {task_id: state["progress"][task_id] for task_id in task_ids if task_id in state["progress"]})
    monkeypatch.setattr(ts.settings, "get_svr_queue_names", lambda suffix: [f"te.1.{suffix}", f"te.0.{suffix}"])
    monkeypatch.setattr(ts.settings, "get_svr_queue_name", lambda priority, suffix: f"te.{priority}.{suffix}")
    return state


def _task(task_id="t1", doc_id="d1", priority=0, age_ms=900_000):
    return {
        "id": task_id,
        "doc_id": doc_id,
        "from_page": 0,
        "to_page": 100000000,
        "priority": priority,
        "create_time": ts.current_timestamp() - age_ms,
    }


@pytest.mark.p2
class TestRequeueOrphanedTasks:
    def test_lost_task_is_published_again(self, broker):
        broker["candidates"] = [_task()]
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == ["t1"]
        queue, message = broker["published"][0]
        assert queue == "te.0.common"
        assert message == {
            "id": "t1",
            "doc_id": "d1",
            "from_page": 0,
            "to_page": 100000000,
            "task_type": "",
            "priority": 0,
        }

    def test_inflight_task_is_left_alone(self, broker):
        broker["candidates"] = [_task()]
        broker["inflight"] = {"t1"}
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == []
        assert broker["published"] == []

    def test_running_task_of_this_executor_is_left_alone(self, broker):
        broker["candidates"] = [_task()]
        assert ts.requeue_orphaned_tasks(exclude_ids={"t1"}, grace_seconds=GRACE) == []
        assert broker["published"] == []

    def test_freshly_queued_task_is_inside_the_grace_window(self, broker):
        # The API inserts the task row and publishes its message right after;
        # a task younger than the grace window must not be seen as lost.
        broker["candidates"] = [_task(age_ms=1000)]
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == []
        assert broker["published"] == []

    def test_query_uses_the_requested_queue_suffix(self, broker):
        ts.requeue_orphaned_tasks(grace_seconds=GRACE, queue_suffix="resume")
        assert broker["query_suffixes"] == ["resume"]

    def test_task_priority_selects_the_queue(self, broker):
        broker["candidates"] = [_task(priority=1)]
        ts.requeue_orphaned_tasks(grace_seconds=GRACE)
        assert broker["published"][0][0] == "te.1.common"

    def test_publish_failure_is_not_reported_as_recovered(self, broker):
        broker["candidates"] = [_task()]
        broker["queue_product_ok"] = False
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == []


@pytest.mark.p2
class TestStalePendingMessages:
    def test_unfinished_task_is_republished_and_not_duplicated(self, broker):
        broker["stale"] = [("te.0.common", "50-0", "t1")]
        broker["progress"] = {"t1": 0.0}
        broker["candidates"] = [_task()]
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == ["t1"]
        assert broker["republished"] == [("te.0.common", ts.SVR_CONSUMER_GROUP_NAME, "50-0")]
        # The very same task is still an unfinished row; the sweep must not also
        # publish a second copy of it.
        assert broker["published"] == []

    def test_finished_task_message_is_dropped(self, broker):
        broker["stale"] = [("te.0.common", "50-0", "t1")]
        broker["progress"] = {"t1": 1.0}
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == []
        assert broker["republished"] == []
        assert broker["acked"] == [("te.0.common", "50-0")]

    def test_message_of_deleted_task_is_dropped(self, broker):
        broker["stale"] = [("te.0.common", "50-0", "gone")]
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == []
        assert broker["republished"] == []
        assert broker["acked"] == [("te.0.common", "50-0")]

    def test_message_of_running_task_of_this_executor_is_dropped(self, broker):
        broker["stale"] = [("te.0.common", "50-0", "t1")]
        ts.requeue_orphaned_tasks(exclude_ids={"t1"}, grace_seconds=GRACE)
        assert broker["republished"] == []
        assert broker["acked"] == []

    def test_republish_failure_is_not_reported_as_recovered(self, broker):
        broker["stale"] = [("te.0.common", "50-0", "t1")]
        broker["progress"] = {"t1": 0.0}
        broker["requeue_ok"] = False
        assert ts.requeue_orphaned_tasks(grace_seconds=GRACE) == []
