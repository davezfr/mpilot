from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ALLOWED_LOCALIZED_FUNCTIONS = {
    "src/mpilot/mcp/acquisition_notifications.py": {
        "_abandoned_message",
        "_completion_hook_failed_message",
        "_completion_message",
        "_progress_timeout_message",
        "_removed_message",
    },
    "src/mpilot/subtitles/notifications.py": {
        "_abandoned_message",
        "_failed_message",
        "_generic_message",
        "_needs_confirmation_message",
        "_running_status_message",
        "_succeeded_message",
        "_terminal_status_message",
        "_title_for_watch",
        "_zh_stage_label",
        "_zh_translation_progress_line",
    },
    "src/mpilot/subtitles/translate.py": {"fake_response"},
}


def _contains_han(value: str) -> bool:
    return any("\u3400" <= char <= "\u9fff" for char in value)


def _nearest_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return current.name
        current = parents.get(current)
    return None


def test_chinese_runtime_literals_are_only_language_selected_or_legacy_input():
    offenders: list[str] = []
    for path in sorted((ROOT / "src").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if not _contains_han(node.value):
                continue
            function_name = _nearest_function(node, parents)
            if function_name in ALLOWED_LOCALIZED_FUNCTIONS.get(relative, set()):
                continue
            if relative == "src/mpilot/mcp/server.py" and "补充搜索" in node.value:
                continue
            offenders.append(f"{relative}:{node.lineno}:{function_name or '<module>'}")

    assert offenders == []
