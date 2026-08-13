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

from common.constants import ParserType
from common.mineru_formats import is_mineru_file


def effective_parser_id(parser_id: str, filename: str) -> str:
    if is_mineru_file(filename):
        return ParserType.NAIVE.value
    return parser_id


def uses_table_row_tasks(parser_id: str, filename: str) -> bool:
    return effective_parser_id(parser_id, filename) == ParserType.TABLE.value
