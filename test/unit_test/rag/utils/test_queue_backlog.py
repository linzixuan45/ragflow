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
import json

import pytest

from api.db.services import document_service as ds

STALE_MS = 120_000


def _fields(task_id):
    return {"message": json.dumps({"id": task_id, "doc_id": "doc-" + task_id})}


class FakeRedis:
    """Only the two reads queue_backlog performs are needed."""

    def __init__(self, undelivered=(), entries=None):
        self._undelivered = list(undelivered)
        self._entries = dict(entries or {})

    def xrange(self, _queue, low, high):
        if low == high:
            fields = self._entries.get(low)
            return [(low, fields)] if fields else []
        return list(self._undelivered)


@pytest.fixture
def install(monkeypatch):
    def _install(undelivered=(), pending=(), entries=None, group="te.0.common"):
        monkeypatch.setattr(ds.REDIS_CONN, "REDIS", FakeRedis(undelivered=undelivered, entries=entries))
        monkeypatch.setattr(ds.REDIS_CONN, "get_pending_msg", lambda *_a, **_k: list(pending))
        monkeypatch.setattr(ds.REDIS_CONN, "queue_info", lambda *_a, **_k: None if group is None else {"last-delivered-id": "100-0"})
        return ds.REDIS_CONN

    return _install


@pytest.mark.p2
class TestQueueBacklog:
    def test_undelivered_messages_count_as_inflight(self, install):
        conn = install(undelivered=[("101-0", _fields("t-undelivered"))])
        inflight, stale = conn.queue_backlog(["te.0.common"], "group", STALE_MS, {"exec-live"})
        assert inflight == {"t-undelivered"}
        assert stale == []

    def test_pending_for_live_consumer_stays_inflight(self, install):
        pending = [{"message_id": "50-0", "consumer": "exec-live", "time_since_delivered": 900_000}]
        conn = install(pending=pending, entries={"50-0": _fields("t-running")})
        inflight, stale = conn.queue_backlog(["te.0.common"], "group", STALE_MS, {"exec-live"})
        assert inflight == {"t-running"}
        assert stale == []

    def test_pending_for_dead_consumer_is_stale(self, install):
        pending = [{"message_id": "50-0", "consumer": "exec-gone", "time_since_delivered": STALE_MS}]
        conn = install(pending=pending, entries={"50-0": _fields("t-stale")})
        inflight, stale = conn.queue_backlog(["te.0.common"], "group", STALE_MS, {"exec-live"})
        assert inflight == set()
        assert stale == [("te.0.common", "50-0", "t-stale")]

    def test_recently_delivered_to_dead_consumer_is_not_taken(self, install):
        # A consumer that is missing from TASKEXE may just be starting up; only
        # messages idle for the full stale window may be republished.
        pending = [{"message_id": "50-0", "consumer": "exec-starting", "time_since_delivered": STALE_MS - 1}]
        conn = install(pending=pending, entries={"50-0": _fields("t-fresh")})
        inflight, stale = conn.queue_backlog(["te.0.common"], "group", STALE_MS, {"exec-live"})
        assert inflight == {"t-fresh"}
        assert stale == []

    def test_missing_consumer_group_reports_nothing(self, install):
        conn = install(group=None)
        assert conn.queue_backlog(["te.0.common"], "group", STALE_MS, {"exec-live"}) == (set(), [])

    def test_pending_entry_with_broken_payload_is_skipped(self, install):
        pending = [{"message_id": "50-0", "consumer": "exec-gone", "time_since_delivered": STALE_MS}]
        conn = install(pending=pending, entries={"50-0": {"message": "not json"}})
        assert conn.queue_backlog(["te.0.common"], "group", STALE_MS, {"exec-live"}) == (set(), [])


@pytest.mark.p2
class TestTaskIdOf:
    def test_reads_task_id_from_payload(self, install):
        conn = install()
        assert conn._task_id_of(_fields("abc")) == "abc"

    @pytest.mark.parametrize("fields", [None, {}, {"message": ""}, {"message": "{"}, {"message": json.dumps({})}])
    def test_returns_none_for_unusable_payload(self, install, fields):
        conn = install()
        assert conn._task_id_of(fields) is None


@pytest.mark.p2
class TestRequeueMsg:
    def test_missing_entry_is_not_acknowledged(self, install, monkeypatch):
        # xack'ing an entry that could not be re-read would drop the work.
        acked = []
        conn = install()
        monkeypatch.setattr(
            conn,
            "REDIS",
            type("R", (), {"xrange": lambda self, *_a: [], "xack": lambda self, *a: acked.append(a)})(),
        )
        assert conn.requeue_msg("te.0.common", "group", "50-0") is False
        assert acked == []

    def test_entry_is_copied_then_acknowledged(self, install, monkeypatch):
        calls = []
        fake = type(
            "R",
            (),
            {},
        )()
        fake.xrange = lambda _queue, low, high: [(low, _fields("t1"))]
        fake.xadd = lambda queue, fields: calls.append(("xadd", queue, fields))
        fake.xack = lambda queue, group, msg_id: calls.append(("xack", queue, group, msg_id))
        conn = install()
        monkeypatch.setattr(conn, "REDIS", fake)
        assert conn.requeue_msg("te.0.common", "group", "50-0") is True
        assert calls == [("xadd", "te.0.common", _fields("t1")), ("xack", "te.0.common", "group", "50-0")]
