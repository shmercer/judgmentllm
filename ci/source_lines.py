"""Conservative source correspondence without executing candidate Python."""

from __future__ import annotations

import ast
import difflib
import re
from collections import defaultdict


def scopes(text: str) -> dict[tuple[str, ...], list[tuple[int, str]]]:
    lines = text.splitlines()
    owners: dict[int, tuple[str, ...]] = {}
    seen: set[tuple[str, ...]] = set()

    def visit(node: ast.AST, parent: tuple[str, ...]) -> None:
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"eval", "exec", "compile"}
        ):
            raise ValueError("Dynamic source execution requires Level 3 assessment")
        owner = parent
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = (*parent, type(node).__name__ + ":" + node.name)
            if owner in seen:
                raise ValueError(
                    "Repeated named source scopes require Level 3 assessment"
                )
            seen.add(owner)
            start = min([node.lineno, *(item.lineno for item in node.decorator_list)])
            for line in range(start, (node.end_lineno or node.lineno) + 1):
                owners[line] = owner
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(ast.parse(text), ())
    grouped: dict[tuple[str, ...], list[tuple[int, str]]] = defaultdict(list)
    for number, line in enumerate(lines, 1):
        grouped[owners.get(number, ())].append((number, line))
    return dict(grouped)


def correspondence(before: str, after: str) -> dict[int, int]:
    """Identical text in a different function cannot cover a new function body."""
    old, new = scopes(before), scopes(after)

    def exclusions(
        groups: dict[tuple[str, ...], list[tuple[int, str]]],
    ) -> list[tuple[tuple[str, ...], str]]:
        return sorted(
            (owner, text.strip())
            for owner, lines in groups.items()
            for _, text in lines
            if re.search(r"#\s*pragma\s*:\s*no\s*(?:cover|branch)", text, re.I)
        )

    if exclusions(old) != exclusions(new):
        raise ValueError("Changed coverage exclusions require Level 3 assessment")
    mapping: dict[int, int] = {}
    for owner in sorted(set(old) & set(new)):
        previous, current = old[owner], new[owner]
        for a, b, size in difflib.SequenceMatcher(
            None,
            [text for _, text in previous],
            [text for _, text in current],
            autojunk=False,
        ).get_matching_blocks():
            mapping.update(
                {
                    previous[a + offset][0]: current[b + offset][0]
                    for offset in range(size)
                }
            )
    return mapping
