# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.
#

"""Guard the translation prompts embedded in ``po_translate.py``.

The SYSTEM_PROMPT / TRANSLATION_PROMPT constants carry the terminology and
product-line naming rules used by the auto doc translation workflow.  This
static test keeps those rules from being accidentally dropped or regressing
(the prompts would otherwise only be exercised by a live DeepSeek run).
"""

import ast
from pathlib import Path
from types import SimpleNamespace

PO_TRANSLATE_PATH = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "scripts" / "po_translate.py"

SOURCE = PO_TRANSLATE_PATH.read_text(encoding="utf-8")


def test_po_translate_has_new_system_prompt_rules():
    assert "TERMINOLOGY RULE (MUST FOLLOW):" in SOURCE
    assert "'Ascend' (Huawei's NPU brand) MUST be translated to '昇腾'" in SOURCE
    assert "NEVER translate it to '升腾'." in SOURCE
    assert "'vLLM Ascend' (the project name) MUST be kept in English verbatim" in SOURCE
    assert "'vllm-ascend' (the package/repo name): keep it verbatim in English." in SOURCE
    assert "PRODUCT-LINE NAMING RULE (MUST FOLLOW EXACTLY, including spaces):" in SOURCE
    assert "'950PR&950DT Products'  -> 'Ascend 950PR&950DT系列产品'" in SOURCE
    assert "'950DT Products'        -> 'Ascend 950DT系列产品'" in SOURCE
    assert "'950PR Products'        -> 'Ascend 950PR系列产品'" in SOURCE
    assert "'Atlas A2 Products'     -> 'Atlas A2系列产品'" in SOURCE
    assert "'Atlas A3 Products'     -> 'Atlas A3系列产品'" in SOURCE
    assert "SOURCE MATCHING IS CASE-INSENSITIVE for these five product-line names." in SOURCE
    assert "'Atlas A3 products', '950DT products', or any other capitalization" in SOURCE
    assert "MUST be NO space between the model" in SOURCE
    assert "'950DT 系列产品' (with a space) or '950DT系列 产品' (split)" in SOURCE


def test_po_translate_has_terminology_translation_section():
    assert "--- TERMINOLOGY (MUST TRANSLATE) ---" in SOURCE
    assert "13. standalone 'Ascend' → '昇腾'" in SOURCE
    assert "14. '950PR&950DT Products' → 'Ascend 950PR&950DT系列产品'" in SOURCE
    assert "15. '950DT Products'       → 'Ascend 950DT系列产品'" in SOURCE
    assert "16. '950PR Products'       → 'Ascend 950PR系列产品'" in SOURCE
    assert "17. 'Atlas A2 Products'    → 'Atlas A2系列产品'" in SOURCE
    assert "18. 'Atlas A3 Products'    → 'Atlas A3系列产品'" in SOURCE
    assert "Source matching for all five product-line names above is CASE-INSENSITIVE." in SOURCE
    assert "- 'Atlas A3 products' → 'Atlas A3系列产品'" in SOURCE
    assert "- '950DT products'    → 'Ascend 950DT系列产品'" in SOURCE


def test_po_translate_prompt_still_format_compatible():
    # TRANSLATION_PROMPT must remain usable with .format(content=...) — the
    # {content} placeholder terminates the triple-quoted string, and doubled
    # braces escape the literal format specifiers shown to the model.
    assert SOURCE.count('{content}"""') == 1
    assert "{{}}, {{{{}}}}, {{name}}" in SOURCE


def test_simplified_conversion_preserves_ascend_brand_name():
    tree = ast.parse(SOURCE)
    selected_nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id in {"_ASCEND_BRAND_NAME", "_ASCEND_BRAND_PLACEHOLDER"}
            for target in node.targets
        ):
            selected_nodes.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_convert_po_to_simplified":
            selected_nodes.append(node)

    namespace = {
        "zhconv": SimpleNamespace(
            convert=lambda text, _locale: text.replace("昇", "升")
            .replace("繁體", "繁体")
            .replace("文檔", "文档")
        )
    }
    exec(compile(ast.Module(body=selected_nodes, type_ignores=[]), str(PO_TRANSLATE_PATH), "exec"), namespace)

    entries = [SimpleNamespace(msgstr="昇腾平台使用繁體文檔")]
    namespace["_convert_po_to_simplified"](entries)

    assert entries[0].msgstr == "昇腾平台使用繁体文档"
