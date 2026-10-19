"""Release configuration references must cover the actual supported interface.

Read parser declarations with AST; never import native Python or send requests.
The negative cases ensure a new option cannot silently miss its documentation.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

AUTOMATION_ROOT = Path(__file__).resolve().parent
if AUTOMATION_ROOT.name == "tests":
    AUTOMATION_ROOT = AUTOMATION_ROOT.parent
RELEASE_ROOT = AUTOMATION_ROOT.parent
CORE_ROOT = AUTOMATION_ROOT / "core"
sys.path.insert(0, str(CORE_ROOT))
import aisbench_configuration as configuration

BEGIN = "# ===== BEGIN CONFIGURATION REFERENCE ====="
END = "# ===== END CONFIGURATION REFERENCE ====="


def reference_files() -> list[Path]:
    files = [RELEASE_ROOT / "env.env"]
    for name in ("test_a.env", "test_b.env"):
        candidates = [AUTOMATION_ROOT / "examples" / name, AUTOMATION_ROOT / name]
        files.append(next((path for path in candidates if path.is_file()), candidates[-1]))
    return files


def exported_literals(text: str) -> dict[str, str]:
    """Only the public examples' literal assignments, without running Bash."""
    result = {}
    prefix = text.split(BEGIN, 1)[0]
    for name, expression in re.findall(r"^export ([A-Z][A-Z0-9_]*)=(.*)$", prefix, re.M):
        values = shlex.split(expression, comments=True, posix=True)
        if len(values) > 1 or any(token in expression for token in ("$(", "`")):
            raise AssertionError("公开示例需使用可审查的字面全局赋值: " + name)
        result[name] = values[0] if values else ""
    return result


def declared_matrix_options(path: Path) -> set[str]:
    """Expand literal parser loops, failing explicitly on new dynamic syntax."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parser_function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "parse_args")
    options: set[str] = set()

    def value(node, bindings):
        if isinstance(node, ast.Constant):
            return node.value
        if type(node).__name__ == 'Str':
            return node.s
        if type(node).__name__ == 'Num':
            return node.n
        if type(node).__name__ == 'NameConstant':
            return node.value
        if isinstance(node, ast.Name) and node.id in bindings:
            return bindings[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return value(node.left, bindings) + value(node.right, bindings)
        raise AssertionError("新增CLI声明形式需要同步参考检查，不能忽略未知选项")

    def walk(node, bindings):
        if isinstance(node, ast.For) and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                                             and child.func.attr == "add_argument" for child in ast.walk(node)):
            if not isinstance(node.target, ast.Name):
                raise AssertionError("CLI参数循环需更新参考提取规则")
            try:
                choices = ast.literal_eval(node.iter)
            except (TypeError, ValueError):
                raise AssertionError("CLI参数循环不再是字面清单，请更新参考提取规则") from None
            for choice in choices:
                for child in node.body:
                    walk(child, {**bindings, node.target.id: choice})
            return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "add_argument":
            for argument in node.args:
                option = value(argument, bindings)
                if isinstance(option, str) and option.startswith("--"):
                    options.add(option)
        for child in ast.iter_child_nodes(node):
            walk(child, bindings)

    walk(parser_function, {})
    return options


def check_reference(text: str, *, public_names=None, scene_names=None, matrix_options=None) -> None:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise AssertionError("每个公开配置必须只有一个完整参数参考块")
    reference = text.split(BEGIN, 1)[1].split(END, 1)[0]
    if any(line.strip() and not line.startswith("#") for line in reference.splitlines()):
        raise AssertionError("参考必须全部为注释，不能在加载时执行")
    blocks = re.findall(r"^# @env ([A-Z][A-Z0-9_]*)\n(.*?)^# @end$", reference, re.M | re.S)
    names = [name for name, _ in blocks]
    expected = set(configuration.PUBLIC_ENV_NAMES if public_names is None else public_names)
    if len(names) != len(set(names)) or set(names) != expected:
        raise AssertionError("全局参数参考缺失、多余或重复: " + repr(set(names) ^ expected))
    assignments = exported_literals(text)
    labels = ("代码默认", "本文件全局有效值", "类型/单位", "作用域", "覆盖", "空值", "本文件场景覆盖", "示例")
    decoder = json.JSONDecoder()
    for name, block in blocks:
        for label in labels:
            if not re.search(r"^# " + re.escape(label) + r": \S.*$", block, re.M):
                raise AssertionError(name + "缺少参考字段: " + label)
        default_line = re.search(r"^# 代码默认: (.*)$", block, re.M).group(1)
        if json.loads(default_line) != configuration.ENV_DEFAULTS[name]:
            raise AssertionError(name + "参考默认值与实际代码不一致")
        effective_line = re.search(r"^# 本文件全局有效值: (.*)$", block, re.M).group(1)
        effective, _ = decoder.raw_decode(effective_line)
        if effective != (assignments.get(name) or configuration.ENV_DEFAULTS[name]):
            raise AssertionError(name + "发布示例有效值与上方赋值不一致")
        if not re.search(r"^# 示例: export " + re.escape(name) + r"=.+$", block, re.M):
            raise AssertionError(name + "缺少可复制的注释用例")
    scenes = re.findall(r"^# @scene ([A-Za-z][A-Za-z0-9_]*)$", reference, re.M)
    expected_scenes = (set(configuration.SCENE_KEYS) | configuration.native_options(RELEASE_ROOT / "aisbench_auto_tools_prefix")) if scene_names is None else set(scene_names)
    if len(scenes) != len(set(scenes)) or set(scenes) != expected_scenes:
        raise AssertionError("JSON场景参考与实际支持字段不一致: " + repr(set(scenes) ^ expected_scenes))
    matrix = re.findall(r"^# @matrix (--[a-z][a-z0-9-]*)$", reference, re.M)
    expected_matrix = declared_matrix_options(CORE_ROOT / "aisbench_perf_matrix.py") if matrix_options is None else set(matrix_options)
    if len(matrix) != len(set(matrix)) or set(matrix) != expected_matrix:
        raise AssertionError("独立matrix参考与CLI声明不一致: " + repr(set(matrix) ^ expected_matrix))
    for marker in ("scene", "matrix"):
        if not all(re.search(r"^# @" + marker + " " + re.escape(name) + r"\n# 说明: \S", reference, re.M)
                   for name in (scenes if marker == "scene" else matrix)):
            raise AssertionError(marker + "参数缺少说明")


class ConfigurationReferenceContracts(unittest.TestCase):
    def test_all_public_config_references_match_global_scene_and_cli_definitions(self):
        for path in reference_files():
            with self.subTest(config=path.name):
                self.assertTrue(path.is_file(), path)
                check_reference(path.read_text(encoding="utf-8"))

    def test_new_public_variable_without_reference_fails(self):
        text = reference_files()[0].read_text(encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "全局参数参考"):
            check_reference(text, public_names=[*configuration.PUBLIC_ENV_NAMES, "NEW_PARAMETER_MUST_BE_DOCUMENTED"])

    def test_new_scene_or_matrix_option_without_reference_fails(self):
        text = reference_files()[0].read_text(encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "JSON场景参考"):
            check_reference(text, scene_names={*configuration.SCENE_KEYS, *configuration.native_options(RELEASE_ROOT / "aisbench_auto_tools_prefix"), "new_scene_option"})
        with self.assertRaisesRegex(AssertionError, "独立matrix参考"):
            check_reference(text, matrix_options={*declared_matrix_options(CORE_ROOT / "aisbench_perf_matrix.py"), "--new-matrix-option"})

    def test_active_reference_or_wrong_default_fails(self):
        text = reference_files()[0].read_text(encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "全部为注释"):
            check_reference(text.replace(BEGIN, BEGIN + "\nexport DATA_NUM_MULTIPLIER=99", 1))
        block = re.search(r"^# @env DATA_NUM_MULTIPLIER\n(.*?)^# @end$", text, re.M | re.S).group(0)
        with self.assertRaisesRegex(AssertionError, "默认值"):
            check_reference(text.replace(block, block.replace('# 代码默认: "4"', '# 代码默认: "99"'), 1))

    def test_public_files_keep_literal_scenarios_and_use_lf(self):
        for path in reference_files():
            with self.subTest(config=path.name):
                content = path.read_bytes()
                self.assertNotIn(b"\r\n", content)
                scenes = configuration.read_scenarios(path)
                self.assertTrue(scenes)
                self.assertEqual(len(re.findall(r"^SCENARIOS_JSON=", content.decode(), re.M)), 1)

    def test_comment_reference_does_not_change_sourced_configuration(self):
        bundled = Path("C:/Program Files/Git/bin/bash.exe")
        bash = str(bundled) if os.name == "nt" and bundled.is_file() else shutil.which("bash")
        if not bash:
            self.skipTest("Bash is needed to compare the real source behavior")
        capture = "import json,os;print(json.dumps({k:os.environ.get(k) for k in " + repr(tuple(configuration.PUBLIC_ENV_NAMES)) + "},sort_keys=True))"
        environment = {key: value for key, value in os.environ.items() if key not in configuration.PUBLIC_ENV_NAMES and key not in {"BASH_ENV", "ENV"}}
        with tempfile.TemporaryDirectory(prefix="aisbench_reference_contract_") as directory:
            for path in reference_files():
                with self.subTest(config=path.name):
                    prefix = Path(directory) / (path.stem + "-prefix.env")
                    prefix.write_bytes(path.read_text(encoding="utf-8").split(BEGIN, 1)[0].encode("utf-8"))
                    command = [bash, "-c", 'source "$1" || exit $?; exec "$2" -B -c "$3"', "reference-contract"]
                    before = subprocess.run([*command, prefix.as_posix(), Path(sys.executable).as_posix(), capture],
                                            env=environment, capture_output=True, text=True, timeout=30)
                    after = subprocess.run([*command, path.as_posix(), Path(sys.executable).as_posix(), capture],
                                           env=environment, capture_output=True, text=True, timeout=30)
                    self.assertEqual((before.returncode, after.returncode), (0, 0), before.stderr + after.stderr)
                    self.assertEqual(json.loads(before.stdout), json.loads(after.stdout))


if __name__ == "__main__":
    unittest.main()
