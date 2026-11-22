"""Validation only. Generate independent mutations from a pinned, unmodified Git tree."""

import argparse
import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import difflib
import io
import json
from pathlib import Path
import random
import re
import shutil
import subprocess
import tarfile

from label import PIN


@dataclass(frozen=True)
class Edit:
    path: str
    start: int
    end: int
    replacement: str


def candidates(files):
    groups = defaultdict(list)
    for path, source in sorted(files.items()):
        if path.startswith("tests/fixtures/") and path.endswith(".sql"):
            for match in re.finditer(r"(?im)^SELECT\s+(?=[^\n]+;)", source):
                groups["fixture"].append(Edit(path, match.end(), match.end(), "NULL, "))
            continue
        if path.endswith(".md"):
            for heading in re.finditer(r"(?m)^#+ .+?$", source):
                groups["noncode"].append(Edit(path, heading.end(), heading.end(),
                                              "\n\n<!-- Validation note. -->"))
            continue
        if not path.endswith(".py"):
            continue
        production = path.startswith("sqlglot/")
        test_file = path.startswith("tests/") and Path(path).name.startswith("test_")
        if not (production or test_file):
            continue
        tree = ast.parse(source)
        lines = source.splitlines(keepends=True)
        starts = [0]
        for line in lines:
            starts.append(starts[-1] + len(line))

        def offset(line, column):
            prefix = lines[line - 1].encode()[:column].decode()
            return starts[line - 1] + len(prefix)

        def change(group, node, value):
            groups[group].append(Edit(path, offset(node.lineno, node.col_offset),
                                      offset(node.end_lineno, node.end_col_offset), value))

        if production:
            groups["comment"].append(Edit(path, len(source), len(source), "\n# Explain the preceding implementation.\n"))
            groups["whitespace"].append(Edit(path, len(source), len(source), "\n\n"))
            groups["new-function"].append(Edit(path, len(source), len(source),
                                                "\n\ndef _private_example(value):\n    return value\n"))
            groups["syntax"].append(Edit(path, len(source), len(source), "\nif (\n"))
            groups["import"].append(Edit(path, len(source), len(source),
                                         "\nfrom sqlglot.missing_validation_module import helper\n"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Module)):
                if production and ast.get_docstring(node, clean=False) is not None:
                    literal = node.body[0].value
                    change("docstring", literal, repr(literal.value + "\nAdditional details."))
            if test_file:
                if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                    body = node.body[-1]
                    if body.lineno != node.lineno:
                        start = starts[body.lineno - 1]
                        indent = re.match(r"\s*", lines[body.lineno - 1])[0]
                        groups["test-file"].append(Edit(path, start, start, indent + "assert False\n"))
                continue
            if isinstance(node, ast.If) and node.test.lineno == node.test.end_lineno:
                start = offset(node.test.lineno, node.test.col_offset)
                end = offset(node.test.end_lineno, node.test.end_col_offset)
                text = source[start:end]
                change("condition", node.test, "not (" + text + ")")
                if path.startswith(("sqlglot/generators/", "sqlglot/parsers/", "sqlglot/optimizer/", "sqlglot/executor/")):
                    group = path.split("/")[1]
                    change(group, node.test, "not (" + text + ")")
            elif isinstance(node, ast.Compare) and len(node.ops) == 1 and node.lineno == node.end_lineno:
                start = offset(node.left.end_lineno, node.left.end_col_offset)
                arg = node.comparators[0]
                end = offset(arg.lineno, arg.col_offset)
                flips = {"==": "!=", "!=": "==", "<": "<=", ">": ">=", "is": "==", "is not": "!=",
                         "<=": "<", ">=": ">", "in": "not in", "not in": "in"}
                op = source[start:end].strip()
                if op in flips:
                    groups["operator"].append(Edit(path, start, end, " " + flips[op] + " "))
                if op in ("==", "!=", "<", "<=", ">", ">="):
                    for operand in (node.left, arg):
                        if isinstance(operand, ast.Constant) and type(operand.value) is int:
                            change("comparison-literal", operand, repr(operand.value + 1))
                        if isinstance(operand, ast.Constant) and type(operand.value) is str:
                            change("comparison-literal", operand, repr(operand.value + "_alt"))
            elif isinstance(node, ast.Constant) and type(node.value) is bool:
                change("constant", node, repr(not node.value))
            elif isinstance(node, ast.Call) and len(node.args) >= 2 and node.lineno == node.end_lineno:
                first, second = node.args[:2]
                if all(isinstance(a, (ast.Name, ast.Constant, ast.Attribute)) for a in (first, second)):
                    a, b = offset(first.lineno, first.col_offset), offset(first.end_lineno, first.end_col_offset)
                    c, d = offset(second.lineno, second.col_offset), offset(second.end_lineno, second.end_col_offset)
                    if source[a:b] != source[c:d]:
                        groups["arguments"].append(Edit(path, a, d, source[c:d] + source[b:c] + source[a:b]))
            elif isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and type(key.value) is str and key.value:
                        change("lookup-key", key, repr(key.value + "_VALIDATE"))
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and node.lineno == node.end_lineno:
                start, end = starts[node.lineno - 1], starts[node.end_lineno]
                if lines[node.lineno - 1].lstrip().startswith(source[offset(node.lineno, node.col_offset):offset(node.end_lineno, node.end_col_offset)]):
                    groups["deletion"].append(Edit(path, start, end, ""))
    return groups


def changed(source, edit):
    return source[:edit.start] + edit.replacement + source[edit.end:]


def diff(files, edit):
    original = files[edit.path].splitlines(keepends=True)
    modified = changed(files[edit.path], edit).splitlines(keepends=True)
    chunks = difflib.unified_diff(original, modified, fromfile="a/" + edit.path, tofile="b/" + edit.path)
    body = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in chunks)
    return f"diff --git a/{edit.path} b/{edit.path}\n" + body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("destination", type=Path, help="new or empty validation-only directory")
    parser.add_argument("--seed", type=int, default=904271)
    parser.add_argument("--count", type=int, default=160)
    parser.add_argument("--comparisons-only", action="store_true",
                        help="generate edits to string literals and integer thresholds in comparisons")
    args = parser.parse_args()
    args.destination.mkdir(parents=True, exist_ok=True)
    if any(args.destination.iterdir()):
        parser.error("destination must be empty")
    archive = subprocess.check_output(["git", "-C", args.source, "archive", PIN])
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        files = {member.name: tar.extractfile(member).read().decode()
                 for member in tar if member.isfile() and member.name.endswith((".py", ".sql", ".md"))
                 and (member.name.startswith(("sqlglot/", "tests/")) or "/" not in member.name)}
    groups = candidates(files)
    rng = random.Random(args.seed)
    cycles = (["condition", "operator", "constant", "deletion", "arguments", "generators",
               "parsers", "optimizer", "executor", "lookup-key", "fixture"] * 2
              + ["new-function", "comment", "docstring", "whitespace", "test-file", "noncode",
                 "syntax", "import", "multi"])
    if args.comparisons_only:
        cycles = ["comparison-literal"]
    available = {group: defaultdict(list) for group in groups}
    for group, edits in groups.items():
        for edit in edits:
            available[group][edit.path].append(edit)
    used = set()

    def pick(group, exclude=""):
        for _ in range(500):
            path = rng.choice(list(available[group]))
            if path == exclude:
                continue
            edit = rng.choice(available[group][path])
            if edit in used:
                continue
            if path.endswith(".py") and group != "syntax":
                try:
                    compile(changed(files[path], edit), path, "exec")
                except SyntaxError:
                    continue
            used.add(edit)
            return edit
        raise ValueError(f"No distinct valid {group} mutation found")

    commits = args.destination / "commits"
    commits.mkdir()
    shutil.copyfile(args.inventory, args.destination / "tests.json")
    manifest = {}
    for i in range(args.count):
        group = cycles[i % len(cycles)]
        if group == "multi":
            first = pick(rng.choice(["condition", "constant", "lookup-key"]))
            edits = [first, pick(rng.choice(["fixture", "generators", "optimizer"]), first.path)]
        else:
            edits = [pick(group)]
        name = f"m{i:04}"
        (commits / (name + ".patch")).write_text("".join(diff(files, edit) for edit in edits), newline="\n")
        manifest[name] = {"kind": group, "files": [edit.path for edit in edits]}
    (args.destination / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Generated {len(manifest)} independent patches, seed {args.seed}")
    print(dict(Counter(row["kind"] for row in manifest.values())))


if __name__ == "__main__":
    main()
