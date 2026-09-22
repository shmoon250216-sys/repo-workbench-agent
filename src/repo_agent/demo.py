"""Deterministic integration fixture; this is NOT model intelligence."""

import json


class ScriptedDemo:
    def complete(self, messages, tools):
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        count = len(results)
        if count == 0:
            name, args = "load_skill", {"name": "bug-diagnosis"}
        elif count == 1:
            name, args = "read_file", {"path": "calculator.py"}
        elif count == 2:
            name, args = (
                "propose_edit",
                {
                    "path": "calculator.py",
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                },
            )
        elif count == 3:
            name, args = "apply_edit", {"proposal_id": results[-1]["proposal_id"]}
        elif count == 4:
            name, args = "run_tests", {"runner": "unittest"}
        elif count == 5:
            name, args = "show_diff", {}
        else:
            return {
                "content": "离线脚本流程结束：已处理 calculator.py 的修改提案。请查看工具记录中的测试状态；此演示不衡量真实模型能力。"
            }, {}
        return {
            "content": "离线脚本步骤",
            "tool_calls": [
                {
                    "id": f"demo-{count}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }, {}


def create_fixture(root):
    root = __import__("pathlib").Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("Demo requires an empty directory to protect existing files")
    (root / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (root / "tests").mkdir()
    (root / "tests/test_calculator.py").write_text(
        "import importlib.util\nfrom pathlib import Path\nimport unittest\nspec=importlib.util.spec_from_file_location('calculator',Path(__file__).resolve().parents[1]/'calculator.py')\nm=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)\nclass TestCalculator(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(5,m.add(2,3))\n    def test_negative(self):\n        self.assertEqual(-1,m.add(-3,2))\n",
        encoding="utf-8",
    )
