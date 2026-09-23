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

"""GraphRAG parser_config must accept DeerFlow's chat_llm override."""

import pytest
from pydantic import ValidationError

from api.utils.validation_utils import GraphragConfig, ParserConfig


def test_graphrag_config_accepts_openai_compatible_chat_llm():
    config = GraphragConfig.model_validate(
        {
            "use_graphrag": True,
            "method": "light",
            "resolution": True,
            "community": True,
            "chat_llm": {
                "llm_factory": "OpenAI-API-Compatible",
                "llm_name": "Qwen3_32B_INT8",
                "api_base": "http://10.100.15.70:11421/v1",
                "api_key": "secret-token",
                "deerflow_model": "Qwen3-35B-A3B-FP8",
            },
        }
    )
    dumped = config.model_dump(exclude_unset=True)
    assert dumped["chat_llm"]["llm_name"] == "Qwen3_32B_INT8"
    assert dumped["chat_llm"]["api_base"] == "http://10.100.15.70:11421/v1"
    assert dumped["chat_llm"]["deerflow_model"] == "Qwen3-35B-A3B-FP8"


def test_graphrag_config_accepts_thinking_extra_body():
    config = GraphragConfig.model_validate(
        {
            "use_graphrag": True,
            "chat_llm": {
                "llm_name": "Qwen3_32B_INT8",
                "api_base": "http://10.100.15.70:11421/v1",
                "extra_body": {
                    "enable_thinking": False,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
            },
        }
    )
    dumped = config.model_dump(exclude_unset=True)
    assert dumped["chat_llm"]["extra_body"]["enable_thinking"] is False
    assert dumped["chat_llm"]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_parser_config_update_payload_keeps_chat_llm():
    parser = ParserConfig.model_validate(
        {
            "graphrag": {
                "use_graphrag": True,
                "chat_llm": {
                    "llm_name": "Qwen3_32B_INT8",
                    "api_base": "http://example.local/v1",
                },
            }
        }
    )
    dumped = parser.model_dump(exclude_unset=True)
    assert dumped["graphrag"]["chat_llm"]["llm_name"] == "Qwen3_32B_INT8"


def test_graphrag_config_defaults_max_parallel_docs_to_eight():
    config = GraphragConfig.model_validate({"use_graphrag": True})
    assert config.max_parallel_docs == 8
    config = GraphragConfig.model_validate({"use_graphrag": True, "max_parallel_docs": 8})
    assert config.max_parallel_docs == 8


def test_graphrag_config_still_rejects_unknown_fields():
    with pytest.raises(ValidationError, match="Extra inputs"):
        GraphragConfig.model_validate({"use_graphrag": True, "unknown_field": True})
