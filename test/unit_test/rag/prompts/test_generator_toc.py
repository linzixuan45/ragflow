import asyncio

import pytest

from rag.prompts import generator


class _ChatModel:
    max_length = 4096


class _TrackingLimiter:
    def __init__(self, value=1):
        self.semaphore = asyncio.Semaphore(value)
        self.active = 0
        self.maximum = 0

    async def __aenter__(self):
        await self.semaphore.acquire()
        self.active += 1
        self.maximum = max(self.maximum, self.active)

    async def __aexit__(self, exc_type, exc, tb):
        self.active -= 1
        self.semaphore.release()


def test_build_structured_toc_prefers_outline_and_maps_chunks_in_order():
    chunks = ["Introduction\nFirst section", "Details\nBody", "Details\nAppendix"]
    outline = [
        {"title": "Introduction", "depth": 0},
        {"title": "Details", "depth": 1},
        {"title": "Details", "depth": 0},
    ]

    toc = generator.build_structured_toc(chunks, outline=outline)

    assert toc == [
        {"level": "1", "title": "Introduction", "chunk_id": "0"},
        {"level": "2", "title": "Details", "chunk_id": "1"},
        {"level": "1", "title": "Details", "chunk_id": "2"},
    ]


def test_build_structured_toc_uses_mineru_heading_levels():
    chunks = ["Overview\nBody", "Implementation\nMore"]
    source_blocks = [
        {"layout_type": "title", "level": 1, "text": "Overview"},
        {"layout_type": "text", "text": "Body"},
        {"layout_type": "title", "level": 2, "text": "Implementation"},
    ]

    toc = generator.build_structured_toc(chunks, source_blocks=source_blocks)

    assert toc == [
        {"level": "1", "title": "Overview", "chunk_id": "0"},
        {"level": "2", "title": "Implementation", "chunk_id": "1"},
    ]


def test_build_structured_toc_matches_heading_lines_not_body_mentions():
    chunks = [
        "This paragraph previews the Implementation section.",
        "Implementation\nActual section body",
    ]

    toc = generator.build_structured_toc(
        chunks,
        source_blocks=[{"layout_type": "title", "level": 1, "text": "Implementation"}],
    )

    assert toc == [{"level": "1", "title": "Implementation", "chunk_id": "1"}]


def test_build_structured_toc_uses_more_complete_source_titles_when_outline_is_partial():
    chunks = ["Overview\nBody", "Implementation\nMore"]
    outline = [
        {"title": "Overview", "depth": 0},
        {"title": "Missing outline entry", "depth": 1},
    ]
    source_blocks = [
        {"layout_type": "title", "level": 1, "text": "Overview"},
        {"layout_type": "title", "level": 2, "text": "Implementation"},
    ]

    toc = generator.build_structured_toc(chunks, outline=outline, source_blocks=source_blocks)

    assert toc == [
        {"level": "1", "title": "Overview", "chunk_id": "0"},
        {"level": "2", "title": "Implementation", "chunk_id": "1"},
    ]


@pytest.mark.asyncio
async def test_run_toc_returns_structured_toc_without_llm(monkeypatch):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("LLM should not be called for structured TOC")

    monkeypatch.setattr(generator, "gen_toc_from_text", unexpected)
    structured = [{"level": "1", "title": "Overview", "chunk_id": "0"}]

    result = await generator.run_toc_from_text(
        ["Overview"],
        _ChatModel(),
        structured_toc=structured,
        limiter=_TrackingLimiter(),
    )

    assert result == structured


@pytest.mark.asyncio
async def test_run_toc_limits_pack_concurrency_and_degrades_pack_failure(monkeypatch):
    limiter = _TrackingLimiter()
    calls = 0

    def split_into_three(_chunks, _max_length):
        return [[{0: "a"}], [{1: "b"}], [{2: "c"}]]

    async def fake_generate(info, _chat_model, _callback=None):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        if calls == 2:
            raise RuntimeError("provider failure")
        info["toc"] = [{"title": f"title-{calls}", "chunk_id": str(calls - 1)}]

    async def fake_levels(titles, _chat_model, _config):
        return [{"level": "1", "title": title} for title in titles]

    monkeypatch.setattr(generator, "split_chunks", split_into_three)
    monkeypatch.setattr(generator, "gen_toc_from_text", fake_generate)
    monkeypatch.setattr(generator, "assign_toc_levels", fake_levels)

    result = await generator.run_toc_from_text(
        ["a", "b", "c"],
        _ChatModel(),
        limiter=limiter,
        pack_timeout=1,
        total_timeout=2,
    )

    assert limiter.maximum <= 1
    assert [item["title"] for item in result] == ["title-1", "title-3"]


@pytest.mark.asyncio
async def test_run_toc_total_timeout_degrades_to_empty(monkeypatch):
    async def slow_generate(_info, _chat_model, _callback=None):
        await asyncio.sleep(1)

    monkeypatch.setattr(generator, "gen_toc_from_text", slow_generate)

    result = await generator.run_toc_from_text(
        ["content"],
        _ChatModel(),
        limiter=_TrackingLimiter(),
        pack_timeout=1,
        total_timeout=0.01,
    )

    assert result == []


@pytest.mark.asyncio
async def test_run_toc_clamps_pack_timeout_to_total_deadline(monkeypatch):
    captured = {}

    async def capture(_chunks, _chat_model, _callback, _limiter, pack_timeout):
        captured["pack_timeout"] = pack_timeout
        return []

    monkeypatch.setattr(generator, "_run_toc_from_text", capture)

    await generator.run_toc_from_text(
        ["content"],
        _ChatModel(),
        limiter=_TrackingLimiter(),
        pack_timeout=60,
        total_timeout=30,
    )

    assert captured["pack_timeout"] == 30


@pytest.mark.asyncio
async def test_run_toc_level_assignment_timeout_degrades_to_empty(monkeypatch):
    async def generate(info, _chat_model, _callback=None):
        info["toc"] = [{"title": "Overview", "chunk_id": "0"}]

    async def slow_levels(*_args, **_kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(generator, "gen_toc_from_text", generate)
    monkeypatch.setattr(generator, "assign_toc_levels", slow_levels)

    result = await generator.run_toc_from_text(
        ["content"],
        _ChatModel(),
        limiter=_TrackingLimiter(),
        pack_timeout=0.01,
        total_timeout=1,
    )

    assert result == []


@pytest.mark.asyncio
async def test_run_toc_preserves_task_cancellation(monkeypatch):
    started = asyncio.Event()

    async def blocked_generate(_info, _chat_model, _callback=None):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(generator, "gen_toc_from_text", blocked_generate)
    task = asyncio.create_task(
        generator.run_toc_from_text(
            ["content"],
            _ChatModel(),
            limiter=_TrackingLimiter(),
            pack_timeout=10,
            total_timeout=10,
        )
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
