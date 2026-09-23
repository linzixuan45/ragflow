# Copyright (c) 2024 Microsoft Corporation.
# Licensed under the MIT License

"""
Reference:
 - [graphrag](https://github.com/microsoft/graphrag)
"""

import logging
import re
from dataclasses import dataclass
from typing import Any

import networkx as nx

from rag.graphrag.general.extractor import ENTITY_EXTRACTION_MAX_GLEANINGS, Extractor
from rag.graphrag.light.graph_prompt import PROMPTS, select_entity_extraction_examples
from rag.graphrag.utils import chat_limiter, pack_user_ass_to_openai_messages, split_string_by_multi_markers
from rag.llm.chat_model import Base as CompletionLLM
from common.token_utils import num_tokens_from_string


def count_chat_tokens(messages, response: str = "") -> int:
    """Count one chat call once: message contents plus the model output."""
    parts = []
    for message in messages or []:
        if isinstance(message, dict):
            parts.append(str(message.get("content") or ""))
    if response:
        parts.append(str(response))
    return num_tokens_from_string("\n".join(parts))


@dataclass
class GraphExtractionResult:
    """Unipartite graph extraction result class definition."""

    output: nx.Graph
    source_docs: dict[Any, Any]


class GraphExtractor(Extractor):
    _max_gleanings: int

    def __init__(
        self,
        llm_invoker: CompletionLLM,
        language: str | None = "English",
        entity_types: list[str] | None = None,
        example_number: int = 2,
        max_gleanings: int | None = None,
    ):
        super().__init__(llm_invoker, language, entity_types)
        """Init method definition."""
        self._max_gleanings = max_gleanings if max_gleanings is not None else ENTITY_EXTRACTION_MAX_GLEANINGS
        self._example_number = example_number
        examples = select_entity_extraction_examples(self._language, self._example_number)

        example_context_base = dict(
            tuple_delimiter=PROMPTS["DEFAULT_TUPLE_DELIMITER"],
            record_delimiter=PROMPTS["DEFAULT_RECORD_DELIMITER"],
            completion_delimiter=PROMPTS["DEFAULT_COMPLETION_DELIMITER"],
            entity_types=",".join(self._entity_types),
            language=self._language,
        )
        # add example's format
        examples = examples.format(**example_context_base)

        self._entity_extract_prompt = PROMPTS["entity_extraction"]
        self._context_base = dict(
            tuple_delimiter=PROMPTS["DEFAULT_TUPLE_DELIMITER"],
            record_delimiter=PROMPTS["DEFAULT_RECORD_DELIMITER"],
            completion_delimiter=PROMPTS["DEFAULT_COMPLETION_DELIMITER"],
            entity_types=",".join(self._entity_types),
            examples=examples,
            language=self._language,
        )

        self._continue_prompt = PROMPTS["entity_continue_extraction"].format(**self._context_base)
        self._if_loop_prompt = PROMPTS["entity_if_loop_extraction"]

        self._left_token_count = llm_invoker.max_length - num_tokens_from_string(self._entity_extract_prompt.format(**self._context_base, input_text=""))
        self._left_token_count = max(llm_invoker.max_length * 0.6, self._left_token_count)

    def _extraction_finished(self, text: str) -> bool:
        marker = str(self._context_base.get("completion_delimiter") or "").strip()
        return bool(marker) and marker in (text or "")

    async def _process_single_content(self, chunk_key_dp: tuple[str, str], chunk_seq: int, num_chunks: int, out_results, task_id=""):
        token_count = 0
        chunk_key = chunk_key_dp[0]
        content = chunk_key_dp[1]
        hint_prompt = self._entity_extract_prompt.format(**self._context_base, input_text=content)

        gen_conf = {"enable_thinking": False}
        logging.info(f"Start processing for {chunk_key}: {content[:25]}...")
        if self.callback:
            self.callback(msg=f"Start processing for {chunk_key}: {content[:25]}...")
        extract_messages = [{"role": "user", "content": hint_prompt}]
        async with chat_limiter:
            final_result = await self._async_chat("", extract_messages, gen_conf, task_id)
        token_count += count_chat_tokens(extract_messages, final_result)
        gleanings_done = 0
        # First-pass already ended with the completion marker: do not spend 2–3
        # extra full-prompt calls that only restated “MANY entities were missed”.
        if not self._extraction_finished(final_result):
            history = pack_user_ass_to_openai_messages(hint_prompt, final_result, self._continue_prompt)
            for now_glean_index in range(self._max_gleanings):
                async with chat_limiter:
                    glean_result = await self._async_chat("", history, gen_conf, task_id)
                token_count += count_chat_tokens(history, glean_result)
                history.extend([{"role": "assistant", "content": glean_result}])
                final_result += glean_result
                gleanings_done = now_glean_index + 1
                if now_glean_index == self._max_gleanings - 1 or self._extraction_finished(glean_result):
                    break

                history.extend([{"role": "user", "content": self._if_loop_prompt}])
                async with chat_limiter:
                    if_loop_result = await self._async_chat("", history, gen_conf, task_id)
                token_count += count_chat_tokens(history, if_loop_result)
                if_loop_result = if_loop_result.strip().strip('"').strip("'").lower()
                if if_loop_result != "yes":
                    break
                history.extend([{"role": "assistant", "content": if_loop_result}, {"role": "user", "content": self._continue_prompt}])

        logging.info(f"Completed processing for {chunk_key}: {content[:25]}... after {gleanings_done} gleanings, {token_count} tokens.")
        if self.callback:
            self.callback(msg=f"Completed processing for {chunk_key}: {content[:25]}... after {gleanings_done} gleanings, {token_count} tokens.")
        records = split_string_by_multi_markers(
            final_result,
            [self._context_base["record_delimiter"], self._context_base["completion_delimiter"]],
        )
        rcds = []
        for record in records:
            record = re.search(r"\((.*)\)", record)
            if record is None:
                continue
            rcds.append(record.group(1))
        records = rcds
        maybe_nodes, maybe_edges = self._entities_and_relations(chunk_key, records, self._context_base["tuple_delimiter"])
        out_results.append((maybe_nodes, maybe_edges, token_count))
        if self.callback:
            self.callback(
                0.5 + 0.1 * len(out_results) / num_chunks,
                msg=f"Entities extraction of chunk {chunk_seq + 1} {len(out_results)}/{num_chunks} done, {len(maybe_nodes)} nodes, {len(maybe_edges)} edges, {token_count} tokens.",
            )
