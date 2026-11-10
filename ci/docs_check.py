"""Check changed Markdown's local links and Python examples without network access."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from urllib.parse import unquote


def check(source: Path, names: list[str]) -> list[str]:
    errors: list[str] = []
    fence = chr(96) * 3
    for name in names:
        path = source / name
        if path.suffix != ".md" or not path.is_file():
            continue
        content = path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]\n]+\]\(([^)\s]+)(?:\s+[^)]*)?\)", content):
            if ":" in target or target.startswith("#"):
                continue
            relative = unquote(target.split("#")[0]).strip("<>")
            if relative and not (path.parent / relative).exists():
                errors.append(f"{name}: missing local link {relative}")
        for snippet in re.findall(rf"(?ms)^{fence}python\n(.*?)^{fence}", content):
            try:
                ast.parse(snippet)
            except SyntaxError as error:
                errors.append(f"{name}: invalid Python example: {error}")
    return errors


if __name__ == "__main__":
    problems = check(Path(sys.argv[1]), sys.argv[2:])
    print(
        "\n".join(problems)
        if problems
        else "Documentation links/examples passed (offline)"
    )
    raise SystemExit(bool(problems))
