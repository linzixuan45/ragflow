#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
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

from rag.graphrag.graph_text import (
    GRAPH_FIELD_SEP,
    is_chinese_language,
    is_extraction_noise,
    join_graph_descriptions,
    needs_description_summary,
    sanitize_graph_description,
    split_graph_descriptions,
    strip_graph_think,
    text_is_mostly_cjk,
)
from rag.graphrag.light.graph_prompt import PROMPTS, select_entity_extraction_examples


_THINKING_DESC = (
    "This entry is incorrect in type mapping in thought, but keeping entity name. "
    "Wait, already listed as organization. I will skip duplicate if name is identical, "
    "or merge. The prompt asks for entities. I have already captured the organization. "
    "I will not duplicate."
)
_FACT_EN = (
    "A customer organization in Anhui, signed contract P250328035 with Speedbot "
    "(Shanghai) for weld seam detection units."
)
_FACT_ZH = "安徽普仕德自动化科技有限公司位于芜湖，向速博特采购焊缝尺寸检测单元。"


def test_strip_graph_think_removes_tagged_reasoning():
    assert strip_graph_think("<think>内部推理</think>\n(\"entity\"<|>安徽普仕德<|>organization<|>客户)") == (
        '("entity"<|>安徽普仕德<|>organization<|>客户)'
    )
    assert strip_graph_think("<think>未闭合的思考") == ""


def test_extraction_noise_matches_self_talk_but_keeps_facts():
    assert is_extraction_noise(_THINKING_DESC)
    assert not is_extraction_noise(_FACT_EN)
    assert not is_extraction_noise(_FACT_ZH)
    assert sanitize_graph_description(_THINKING_DESC) == ""
    assert sanitize_graph_description(_FACT_ZH) == _FACT_ZH


def test_join_graph_descriptions_drops_thinking_and_keeps_facts():
    joined = join_graph_descriptions([_FACT_EN, _THINKING_DESC, _FACT_ZH, _FACT_EN])
    assert _THINKING_DESC not in joined
    assert _FACT_EN in joined
    assert _FACT_ZH in joined
    assert GRAPH_FIELD_SEP in joined


def test_split_graph_descriptions_filters_sep_fragments():
    raw = GRAPH_FIELD_SEP.join([_FACT_EN, _THINKING_DESC, _FACT_ZH])
    assert split_graph_descriptions(raw) == [_FACT_EN, _FACT_ZH]


def test_chinese_language_triggers_summary_for_english_or_multiple_fragments():
    assert is_chinese_language("Chinese")
    assert is_chinese_language("zh-CN")
    assert not is_chinese_language("English")
    assert text_is_mostly_cjk(_FACT_ZH)
    assert not text_is_mostly_cjk(_FACT_EN)
    assert needs_description_summary([_FACT_EN], "Chinese")
    assert needs_description_summary([_FACT_ZH, _FACT_EN], "Chinese")
    assert not needs_description_summary([_FACT_ZH], "Chinese")
    assert not needs_description_summary([_FACT_EN, _FACT_EN], "English")


def test_chinese_extraction_examples_are_used_for_chinese_language():
    examples = select_entity_extraction_examples("Chinese", 2)
    prompt = PROMPTS["entity_extraction"].format(
        language="Chinese",
        entity_types="organization,person,geo,event,category",
        tuple_delimiter="<|>",
        record_delimiter="##",
        completion_delimiter="<|COMPLETE|>",
        examples=examples,
        input_text="安徽普仕德自动化科技有限公司采购焊缝检测设备。",
    )
    assert "必须使用 Chinese 撰写" in prompt
    assert "不要输出推理" in prompt
    assert "安徽星河自动化科技有限公司" in examples
    assert "Alex is a character" not in examples
    english_examples = select_entity_extraction_examples("English", 2)
    assert "Alex is a character" in english_examples
