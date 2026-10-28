"""Normalize generated constraint annotations without changing model behavior."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path


def normalize(source: str) -> str:
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line.encode()))
    replacements: list[tuple[int, int, bytes]] = []
    allowed = {
        "constr": {"strict", "strip_whitespace", "to_upper", "to_lower", "min_length", "max_length", "pattern"},
        "conint": {"strict", "gt", "ge", "lt", "le", "multiple_of"},
        "confloat": {"strict", "gt", "ge", "lt", "le", "multiple_of", "allow_inf_nan"},
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in allowed:
            continue
        kind = node.func.id
        if node.args or any(k.arg not in allowed[kind] for k in node.keywords):
            raise ValueError(f"Unsupported constraint syntax: {ast.unparse(node)}")
        base = {"constr": "str", "conint": "int", "confloat": "float"}[kind]
        metadata = "StringConstraints" if kind == "constr" else "Field"
        call = ast.Call(func=ast.Name(id=metadata, ctx=ast.Load()), args=[], keywords=node.keywords)
        replacement = f"Annotated[{base}, {ast.unparse(call)}]".encode()
        assert node.end_lineno is not None and node.end_col_offset is not None
        replacements.append((offsets[node.lineno - 1] + node.col_offset, offsets[node.end_lineno - 1] + node.end_col_offset, replacement))
    if not replacements:
        return source
    # Keep existing imported symbols: generated modules may be imported directly.
    anchor = next(n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "__future__")
    assert anchor.end_lineno is not None
    replacements.append(
        (
            offsets[anchor.end_lineno],
            offsets[anchor.end_lineno],
            b"\nfrom typing import Annotated\nfrom pydantic import StringConstraints\n",
        )
    )
    data = source.encode()
    for start, end, replacement in sorted(replacements, reverse=True):
        data = data[:start] + replacement + data[end:]
    result = data.decode()
    compile(result, "<normalized-sdk>", "exec")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+")
    args = parser.parse_args()
    for path in args.paths:
        path.write_text(normalize(path.read_text()))


if __name__ == "__main__":
    main()
