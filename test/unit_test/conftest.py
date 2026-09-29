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

"""Shared setup for RAGFlow unit tests.

Several parsers and the chunking pipeline tokenize text with NLTK, which needs
the ``punkt_tab`` and ``wordnet`` data sets. Production provisions these via
``download_deps.py`` (into ``nltk_data``, exported as ``NLTK_DATA`` by
``docker/launch_backend_service.sh``) and ``api.validation`` at startup, but the
unit-test runner has neither. Without the data, tokenizer-backed tests such as
``test_epub_parser`` and ``test_dataflow_service`` fail with
``LookupError: Resource 'punkt_tab' not found``.

Provisioned corpora commonly ship as ``<name>.zip`` next to the unpacked
directory. ``nltk.data.find()`` does not see inside those archives, so a naive
``find``/``download`` loop treats an already-downloaded corpus as missing and
re-fetches it over the network on every run; when the network is slow or
blocked, collection then hangs with no output at all. Resolve a local archive
first (offline, instant) and only fall back to a bounded network fetch.
"""

import os
import zipfile

import nltk

# Reuse data already fetched by download_deps.py (the directory the app exports
# as NLTK_DATA) so provisioned environments do not download it again.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
_LOCAL_NLTK_DATA = os.path.join(_REPO_ROOT, "ragflow_deps", "nltk_data")
if os.path.isdir(_LOCAL_NLTK_DATA) and _LOCAL_NLTK_DATA not in nltk.data.path:
    nltk.data.path.insert(0, _LOCAL_NLTK_DATA)

# (download name, resource path used by nltk.data.find)
_REQUIRED_NLTK_DATA = (
    ("punkt_tab", "tokenizers/punkt_tab"),
    ("wordnet", "corpora/wordnet"),
)


def _unpack_local_archive(name: str, find_path: str) -> bool:
    """Expand an already-downloaded ``<name>.zip`` so ``find_path`` resolves."""
    for root in nltk.data.path:
        archive = os.path.join(root, *find_path.split("/")[:-1], f"{name}.zip")
        target = os.path.join(root, *find_path.split("/")[:-1])
        if not os.path.isfile(archive):
            continue
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(target)
        except (OSError, zipfile.BadZipFile):
            continue
        return True
    return False


for _name, _find_path in _REQUIRED_NLTK_DATA:
    try:
        nltk.data.find(_find_path)
        continue
    except LookupError:
        pass
    # Offline path: a provisioned archive just needs expanding.
    if _unpack_local_archive(_name, _find_path):
        try:
            nltk.data.find(_find_path)
            continue
        except LookupError:
            pass
    # Last resort. Offline-only environments must not hang here, so skip the
    # fetch entirely unless the caller explicitly opts in.
    if os.environ.get("RAGFLOW_ALLOW_NLTK_DOWNLOAD") == "1":
        nltk.download(_name, quiet=True)
