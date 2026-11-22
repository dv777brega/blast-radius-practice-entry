"""Map edits to the tests which could observe their first effect on the base run."""
import ast
import io
import operator
import tokenize
from functools import lru_cache, reduce
from pathlib import Path

from observations import equivalent, passive
from unidiff import apply, parse


def union(masks):
    return reduce(operator.or_, masks, 0)


def nearby(node, affected):
    """Visit nodes that intersect an edited line, pruning untouched functions and classes."""
    if hasattr(node, "lineno"):
        start = min((d.lineno for d in getattr(node, "decorator_list", ())), default=node.lineno)
        if not any(start <= line <= node.end_lineno for line in affected):
            return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from nearby(child, affected)


def _non_code_lines(source, changed):
    """Identify edited blank and comment-only lines outside multiline tokens."""
    lines = source.splitlines()
    possible = {n for n in changed if n <= len(lines) and
                (not lines[n - 1].strip() or lines[n - 1].lstrip().startswith("#"))}
    if not possible:
        return set()
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
        code_lines = set()
        for tok in tokens:
            if tok.type not in (
                tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE,
                tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING, tokenize.ENDMARKER
            ):
                code_lines.update(range(tok.start[0], tok.end[0] + 1))
        return possible - code_lines
    except tokenize.TokenError:
        return set()



def _passive_signature(func_node):
    args = func_node.args
    parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]
    if (func_node.decorator_list or func_node.type_params or func_node.returns is not None
            or any(param is not None and param.annotation is not None for param in parameters)):
        return False
    return all(passive(d) for d in args.defaults) and all(
        d is None or passive(d) for d in args.kw_defaults
    )


class Impact:
    def __init__(self, source, index, observations=None):
        self.source = Path(source)
        self.index = index
        self.every = (1 << len(index["tests"])) - 1
        self.observations = observations
        self.by_file = {}
        for i, node in enumerate(index["tests"]):
            path = node.split("::", 1)[0]
            self.by_file[path] = self.by_file.get(path, 0) | (1 << i)
        self.risk = 0
        self.tree = lru_cache(maxsize=32)(lambda path: ast.parse(self.original(path), path))
        self.original = lru_cache(maxsize=96)(self._original)

    def _original(self, name):
        return (self.source / name).read_text("utf-8")

    def changed(self, patch):
        self.risk = 0
        result = self.index.get("baseline_failures", 0)
        for change in parse(patch):
            result |= self.file(change)
            if result == self.every:
                break
        return result

    def file(self, change):
        old, new = change.old, change.new
        idx = self.index
        reads = idx["reads"].get(old, 0) | idx["reads"].get(new, 0)
        path = new or old
        if path.endswith("/conftest.py") or path == "conftest.py":
            prefix = path.removesuffix("conftest.py")
            return union(mask for name, mask in self.by_file.items() if name.startswith(prefix))
        if old != new:
            if (old and old.endswith(".py")) or (new and new.endswith(".py")):
                return self.every
            return self.every if not old else reads
        if not old.endswith(".py"):
            if old not in idx["hash"]:
                return self.every
            if old in {"pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini"}:
                return self.every
            apply(change, self.original(old))
            return reads

        base = self.original(old)
        updated, groups = apply(change, base)
        local = self.by_file.get(old, 0)
        module_users = idx["modules"].get(old, 0)
        try:
            new_tree = ast.parse(updated, old)
            compile(new_tree, old, "exec", dont_inherit=True)
        except (SyntaxError, ValueError, OverflowError):
            if old.startswith("sqlglot/"):
                return self.every
            return self.every if not local else local | module_users | reads

        if not groups:
            return reads | local
        if base == updated:
            return local
        source_reads = idx["sensitive"].get(old, 0)
        lines = idx["lines"].get(old, {})
        old_tree = self.tree(old)

        def code_text(op):
            return "".join(line.partition("#")[0].strip() for h in change.hunks for kind, line in h.body if kind == op)

        if code_text("-") == code_text("+") and equivalent(old_tree, new_tree):
            return source_reads | local

        if old not in idx["scopes"]:
            return self.every

        if any(start < line <= end for start, end, _, _ in groups
               for line in idx["docstrings"].get(old, ())):
            if self.only_docstrings(old_tree, new_tree):
                return self.doc_readers() | local | source_reads

        deleted = {line for start, end, _, _ in groups for line in range(start + 1, end + 1)}
        added = {n for _, _, a, b in groups for n in range(a + 1, b + 1)}
        refined = self.observations.refine(old, old_tree, new_tree, deleted) if self.observations else {}

        old_nodes = list(nearby(old_tree, deleted))
        new_nodes = list(nearby(new_tree, added))

        func_sig_lines = {}
        class_sig_lines = set()
        for node in old_nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                end = max(node.body[0].lineno - 1, node.lineno)
                for ln in range(start, end + 1):
                    func_sig_lines[ln] = node
            elif isinstance(node, ast.ClassDef):
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                end = max(node.body[0].lineno - 1, node.lineno)
                class_sig_lines.update(range(start, end + 1))

        scopes = idx["scopes"][old]
        result = local | reads | source_reads

        new_functions = [(n.body[0].lineno, n.end_lineno) for n in new_nodes
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        top_level_new_funcs = [
            n for n in new_tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and _passive_signature(n)
        ]
        new_non_code = _non_code_lines(updated, added) if added else set()
        old_non_code = idx["blanks"].get(old, set()) | idx["comments"].get(old, set())

        boolean_entries = self.boolean_entries(old_nodes, new_nodes)
        result |= union(lines.get(n, 0) for n in boolean_entries)

        for start, end, first, last in groups:
            added_lines = list(range(first + 1, last + 1))
            added_in_function = all(
                line in new_non_code or any(a <= line <= b for a, b in new_functions)
                for line in added_lines
            )
            if not added_in_function:
                old_lines = range(start + 1, end + 1)
                in_lambda = all(any(a <= n <= b for a, b in idx["lambdas"].get(old, ()))
                                for n in old_lines)
                added_new_func = bool(added_lines) and all(
                    line in new_non_code or any(
                        min([fn.lineno, *(d.lineno for d in fn.decorator_list)]) <= line <= fn.end_lineno
                        for fn in top_level_new_funcs
                    )
                    for line in added_lines
                )
                if added_new_func and start == end:
                    result |= module_users
                    continue
                if start == end or not (all(n in refined for n in old_lines) or in_lambda):
                    return self.every

            if start == end:
                if all(line in new_non_code for line in added_lines):
                    continue
                result |= self.insertion(old, start, first, last, lines, scopes, new_nodes)

            for line in range(start + 1, end + 1):
                if line in refined:
                    result |= refined[line]
                    continue
                if line in class_sig_lines or line in idx["import_lines"].get(old, ()):
                    return self.every
                if line in func_sig_lines:
                    old_fn = func_sig_lines[line]
                    new_fn = next(
                        (n for n in new_nodes
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == old_fn.name),
                        None,
                    )
                    if new_fn is not None and _passive_signature(old_fn) and _passive_signature(new_fn):
                        result |= module_users
                        continue
                    return self.every
                if self.function_at(scopes, line):
                    result |= lines.get(line, 0)
                    if line not in lines and line not in idx.get("executable", {}).get(old, ()):
                        result |= self.statement_mask(old, line, lines)
                elif any(a <= line <= b for a, b in idx.get("lambdas", {}).get(old, ())):
                    interval = min((b - a, a, b) for a, b in idx["lambdas"][old] if a <= line <= b)
                    result |= lines.get(line, 0) or union(lines.get(n, 0) for n in range(interval[1], interval[2] + 1))
                elif line in old_non_code:
                    continue
                else:
                    return self.every
        return result

    @staticmethod
    def boolean_entries(old, new):
        def sites(tree):
            return {(n.lineno, n.col_offset): n for n in tree if isinstance(n, ast.BoolOp)}

        before, after = sites(old), sites(new)
        for key, node in before.items():
            other = after.get(key)
            if other is None or type(other.op) != type(node.op) or len(other.values) != len(node.values):
                yield node.values[0].lineno

    @classmethod
    def only_docstrings(cls, old, new):
        changed = []
        try:
            for tree in (old, new):
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                        if ast.get_docstring(node, clean=False) is not None:
                            changed.append((node, node.body))
                            node.body = node.body[1:]
            return bool(changed) and equivalent(old, new)
        finally:
            for node, body in changed:
                node.body = body

    def doc_readers(self):
        result = 0
        for symbol in ("__doc__", "getdoc", "DocTestSuite", "doctest"):
            for table in ("name_uses", "attr_uses"):
                for path, uses in self.index.get(table, {}).get(symbol, {}).items():
                    lines = self.index["lines"].get(path, {})
                    result |= union(lines.get(line, 0) for line in uses)
        return result

    @staticmethod
    def function_at(scopes, line):
        enclosing = min(((b - a, kind) for a, b, kind, name in scopes
                         if a <= line <= b and kind in ("class", "function")), default=None)
        return enclosing and enclosing[1] == "function"

    def statement_mask(self, path, line, lines):
        candidates = [(b - a, a, b) for a, b in self.index["statements"][path] if a <= line <= b]
        if not candidates:
            return 0
        _, a, b = min(candidates)
        return union(lines.get(n, 0) for n in range(a, b + 1))

    def insertion(self, path, position, first, last, lines, scopes, nodes):
        terminal = (ast.Return, ast.Raise, ast.Continue, ast.Break, ast.Try, ast.With)
        for container in nodes:
            for _, block in ast.iter_fields(container):
                if not isinstance(block, list) or not block or not isinstance(block[0], ast.stmt):
                    continue
                inside = [i for i, stmt in enumerate(block)
                          if first < stmt.lineno <= stmt.end_lineno <= last]
                if not inside or not isinstance(container, (ast.FunctionDef, ast.AsyncFunctionDef,
                                                           ast.If, ast.For, ast.While)):
                    continue
                left, right = inside[0] - 1, inside[-1] + 1
                if left < 0 or right >= len(block):
                    continue
                previous, following = block[left], block[right]
                if isinstance(previous, terminal) or isinstance(following, terminal):
                    continue
                a = previous.lineno
                b = following.lineno - (last - first)
                if a in lines and b in lines:
                    self.risk = max(self.risk, 0.005)
                    return lines[a] | lines[b]
        enclosing = [(b - a, a, b) for a, b, kind, name in scopes
                     if kind == "function" and a <= position <= b]
        if not enclosing:
            return self.every
        _, a, b = min(enclosing)
        enclosing_mask = union(mask for line, mask in lines.items() if a <= line <= b)
        self.risk = max(self.risk, 0.005 if enclosing_mask else 0.03)
        return enclosing_mask
