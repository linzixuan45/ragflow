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

from types import SimpleNamespace

import pytest

from rag.graphrag.light.graph_extractor import GraphExtractor, count_chat_tokens
from rag.graphrag.light.graph_prompt import PROMPTS


def _build_llm_stub():
    return SimpleNamespace(llm_name="test-llm", max_length=8192)


def _complete_record() -> str:
    return (
        '("entity"<|>"星河公司"<|>"organization"<|>"星河公司位于芜湖。")##'
        f'{PROMPTS["DEFAULT_COMPLETION_DELIMITER"]}'
    )


@pytest.mark.asyncio
async def test_complete_first_pass_skips_gleaning(monkeypatch):
    extractor = GraphExtractor(_build_llm_stub(), language="Chinese")
    extractor.callback = None
    calls = []

    async def fake_async_chat(_system, history, _gen_conf=None, task_id=""):
        calls.append(history)
        return _complete_record()

    monkeypatch.setattr(extractor, "_async_chat", fake_async_chat)
    monkeypatch.setattr(extractor, "_entities_and_relations", lambda *_a, **_k: ({"n": [1]}, {"e": [1]}))

    out_results = []
    await extractor._process_single_content(("chunk-1", "安徽星河自动化"), 0, 1, out_results)

    assert len(calls) == 1
    assert out_results[0][2] > 0


@pytest.mark.asyncio
async def test_incomplete_first_pass_still_gleans(monkeypatch):
    extractor = GraphExtractor(_build_llm_stub(), language="Chinese")
    extractor.callback = None
    calls = []
    responses = iter(
        [
            '("entity"<|>"星河公司"<|>"organization"<|>"星河公司位于芜湖。")##',
            _complete_record(),
        ]
    )

    async def fake_async_chat(_system, history, _gen_conf=None, task_id=""):
        calls.append(history)
        return next(responses)

    monkeypatch.setattr(extractor, "_async_chat", fake_async_chat)
    monkeypatch.setattr(extractor, "_entities_and_relations", lambda *_a, **_k: ({}, {}))

    out_results = []
    await extractor._process_single_content(("chunk-1", "安徽星河自动化"), 0, 1, out_results)

    assert len(calls) == 2


def test_count_chat_tokens_does_not_repeat_the_prompt():
    hint = "prompt " * 50
    result = "output " * 10
    once = count_chat_tokens([{"role": "user", "content": hint}], result)
    doubled = count_chat_tokens(
        [{"role": "user", "content": hint}, {"role": "assistant", "content": result}],
        hint,
    )
    assert once < doubled
    assert doubled > once * 1.4
