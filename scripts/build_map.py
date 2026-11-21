"""Build or verify the dependency index from the contest's pinned checkout."""

import argparse
import ast
import hashlib
import io
import marshal
import os
import sys
import tempfile
import tokenize
import types
import zlib
from pathlib import Path

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
IGNORED = {".git", ".pytest_cache", "__pycache__", ".mypy_cache", ".ruff_cache"}


def files(root):
    for directory, dirs, names in os.walk(root):
        dirs[:] = sorted(name for name in dirs if name not in IGNORED)
        for name in sorted(names):
            yield Path(directory, name)


def fingerprints(root):
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in files(root)
    }


def source_index(root, hashes):
    index = {name: {} for name in (
        "scopes", "statements", "lambdas", "branches", "docstrings", "comments", "blanks",
        "attr_uses", "name_uses", "imports", "executable",
    )}
    for name in hashes:
        if not name.endswith(".py"):
            continue
        data = (root / name).read_bytes()
        source = data.decode(tokenize.detect_encoding(io.BytesIO(data).readline)[0])
        tree = ast.parse(source, name)
        compiled = [compile(tree, name, "exec", dont_inherit=True)]
        executable = set()
        while compiled:
            code = compiled.pop()
            if code.co_flags & 1:
                executable.update(n for _, _, n in code.co_lines() if n and n > 0)
            compiled.extend(child for child in code.co_consts if isinstance(child, types.CodeType))
        index["executable"][name] = executable
        lines = source.splitlines()
        scopes = [(1, len(lines), "module", "")]
        statements = []
        lambdas = []
        branches = []
        docstrings = set()
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.Attribute, ast.Name)) and isinstance(node.ctx, ast.Load):
                attribute = isinstance(node, ast.Attribute)
                by_path = index["attr_uses" if attribute else "name_uses"].setdefault(
                    node.attr if attribute else node.id, {}
                )
                by_path.setdefault(name, set()).add(node.lineno)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                level = getattr(node, "level", 0)
                module = getattr(node, "module", None)
                names = tuple((alias.name, alias.asname) for alias in node.names)
                imports.append((node.lineno, level, module, names))
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.body and isinstance(node.body[0], ast.Expr):
                    expr = node.body[0].value
                    if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                        docstrings.update(range(expr.lineno, expr.end_lineno + 1))
                if not isinstance(node, ast.Module):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    kind = "class" if isinstance(node, ast.ClassDef) else "function"
                    scopes.append((start, node.end_lineno, kind, node.name))
            if isinstance(node, ast.stmt):
                statements.append((node.lineno, node.end_lineno))
                for field, children in ast.iter_fields(node):
                    if isinstance(children, list) and children and isinstance(children[0], ast.stmt):
                        branches.append((node.lineno, field, tuple(child.lineno for child in children)))
            if isinstance(node, ast.Lambda):
                lambdas.append((node.lineno, node.end_lineno))
        index["scopes"][name] = scopes
        index["statements"][name] = sorted(statements)
        index["lambdas"][name] = sorted(lambdas)
        index["branches"][name] = branches
        index["imports"][name] = sorted(imports, key=lambda entry: entry[0])
        index["docstrings"][name] = docstrings
        index["blanks"][name] = {n for n, line in enumerate(lines, 1) if not line.strip()}
        index["comments"][name] = {
            tok.start[0] for tok in tokenize.generate_tokens(io.StringIO(source).readline)
            if tok.type == tokenize.COMMENT
        }
    return index


def build(root, output):
    import pytest
    from trace_maps import Tracker, track_pool_submissions

    hashes = fingerprints(root)
    result = source_index(root, hashes)
    scripts = Path(__file__).resolve().parent
    os.environ.update(
        SKIP_INTEGRATION="0",
        PYTHON_CPU_COUNT="2",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        PYTHONDONTWRITEBYTECODE="1",
        BLAST_MAP_ROOT=str(root),
        PYTHONPATH=os.pathsep.join((str(scripts / "child_hooks"), str(scripts), str(root))),
    )
    sys.path.insert(0, str(root))
    os.chdir(root)
    tracker = Tracker(root)
    track_pool_submissions(tracker)

    class Plugin:
        tests = []
        numbers = {}
        groups = {}

        def pytest_collection_modifyitems(self, items):
            self.tests = sorted(item.nodeid for item in items)
            self.numbers = {key: 1 << i for i, key in enumerate(self.tests)}
            for item in items:
                for parent in item.listchain():
                    key = parent.nodeid
                    self.groups[key] = self.groups.get(key, 0) | self.numbers[item.nodeid]

        def change(self, bits):
            tracker.set_mask(bits)
            if bits:
                os.environ["BLAST_MAP_MASK"] = hex(bits)
            else:
                os.environ.pop("BLAST_MAP_MASK", None)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            self.change(self.numbers[item.nodeid])
            try:
                return (yield)
            finally:
                self.change(0)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            previous = tracker.mask
            self.change(self.groups.get(request.node.nodeid, previous))
            try:
                return (yield)
            finally:
                self.change(previous)

    plugin = Plugin()
    with tempfile.TemporaryDirectory(prefix="map-children-") as scratch:
        os.environ["BLAST_MAP_CHILDREN"] = scratch
        ret = pytest.main(["-q", "-p", "no:cacheprovider", "--disable-warnings", "tests"], plugins=[plugin])
        tracker.stop()
        if ret:
            raise RuntimeError(f"pristine pytest run failed with exit status {ret}")
        for path in Path(scratch).iterdir():
            with path.open("rb") as stream:
                tracker.merge(marshal.load(stream))

    for collection, reads in (
        (tracker.collect_reads, tracker.reads),
        (tracker.collect_sensitive, tracker.sensitive),
    ):
        for path, owner in collection:
            mask = plugin.groups.get(owner, plugin.groups[""])
            reads[path] = reads.get(path, 0) | mask
    # Directory descriptors and temporary files created by a test have no
    # corresponding source file on which an incoming patch can operate.
    reads = {p: bits for p, bits in tracker.reads.items() if p in hashes}
    sensitive = {p: bits for p, bits in tracker.sensitive.items() if p in hashes}
    only_at_import = {name: seen.difference(tracker.lines.get(name, {}))
                      for name, seen in tracker.init.items()}
    result.update(
        version=1, commit=COMMIT, python=sys.version.split()[0],
        tests=plugin.tests, hash=hashes, lines=tracker.lines, modules=tracker.modules,
        import_lines=only_at_import, reads=reads, sensitive=sensitive,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    data = zlib.compress(marshal.dumps(result, 4), 9)
    with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as stream:
        stream.write(data)
    os.replace(stream.name, output)
    print(f"{len(plugin.tests)} collected node IDs, {len(hashes)} hashes, {len(data)} compressed bytes")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "verify"))
    parser.add_argument("source", type=Path)
    parser.add_argument("map", type=Path)
    parser.add_argument("--observations", type=Path, help="also verify baseline observations")
    args = parser.parse_args()
    root = args.source.resolve()
    target = args.map.resolve()
    if args.command == "build":
        build(root, target)
        return
    index = marshal.loads(zlib.decompress(target.read_bytes()))
    if (index["version"], index["commit"], index["python"]) != (1, COMMIT, sys.version.split()[0]):
        raise SystemExit("map version, pinned commit or Python version mismatch")
    actual = fingerprints(root)
    if actual != index["hash"]:
        changed = sorted(path for path in actual.keys() | index["hash"].keys()
                         if actual.get(path) != index["hash"].get(path))
        raise SystemExit("source differs from map: " + ", ".join(changed))
    if args.observations:
        observations = marshal.loads(zlib.decompress(args.observations.read_bytes()))
        expected = {p: bytes.fromhex(h) for p, h in actual.items()
                    if p.startswith("sqlglot/") and p.endswith(".py")}
        if (observations["version"] != 1 or observations["revision"] != COMMIT or
                observations["tests"] != index["tests"] or observations["sha256"] != expected):
            raise SystemExit("observation index does not match coverage inventory or pinned source")
    print(f"verified {len(actual)} pinned files")


if __name__ == "__main__":
    main()
