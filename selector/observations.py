"""Interpret source-derived observations of the unchanged, pinned test suite."""

import ast
import collections
import marshal
import zlib


def equivalent(left, right):
    if type(left) is not type(right):
        return False
    if isinstance(left, ast.AST):
        return all(equivalent(getattr(left, field, None), getattr(right, field, None))
                   for field in left._fields)
    if isinstance(left, list):
        return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
    return left == right


def node_key(node):
    if isinstance(node, ast.Constant) and type(node.value) in (str, int):
        return ("key", node.value)
    if isinstance(node, ast.Attribute):
        return ("enum" if node.attr.isupper() else "type", node.attr)
    return None


def passive(node):
    return isinstance(node, ast.Constant) or (
        isinstance(node, (ast.Tuple, ast.List)) and all(passive(i) for i in node.elts)
    ) or (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
          and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (float, int))


def closed_factory(old, new):
    if not isinstance(old, ast.Call) or not isinstance(new, ast.Call):
        return False
    if not equivalent(old.func, new.func) or not equivalent(old.args, new.args):
        return False
    return (len(old.keywords) == len(new.keywords)
            and all(a.arg == b.arg and (equivalent(a.value, b.value) or
                    (isinstance(a.value, ast.Constant) and isinstance(b.value, ast.Constant)
                     and type(a.value.value) is bool and type(b.value.value) is bool))
                    for a, b in zip(old.keywords, new.keywords)))


def result_of(op, relation):
    if relation == "unknown":
        return None
    if op == "Is":
        return relation == "same"
    if op == "IsNot":
        return relation != "same"
    if op == "Eq":
        return relation in ("same", "equal")
    if op == "NotEq":
        return relation not in ("same", "equal")
    if relation not in ("lt", "gt", "equal"):
        return None
    if op == "Lt":
        return relation == "lt"
    if op == "LtE":
        return relation != "gt"
    if op == "Gt":
        return relation == "gt"
    if op == "GtE":
        return relation != "lt"
    return None


class Observations:
    @classmethod
    def load(cls, path, tests):
        data = marshal.loads(zlib.decompress(path.read_bytes()))
        if data["version"] != 1 or data["revision"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
            raise ValueError("incompatible observation index")
        if tests != data["tests"]:
            raise ValueError("observation index collected a different test inventory")
        return cls(data)

    def __init__(self, data):
        self.declarations = set(data["declarations"])
        self.comparisons = set(data["comparisons"]).difference(data.get("import_comparisons", ()))
        self.unsafe = set(data["unsafe"])
        self.children = data["children"]
        self.reads = data["reads"]
        self.relations = data["relations"]
        self.factories = set(data.get("factories", []))
        self.spreads = collections.defaultdict(list)
        self.line_coverage = {}
        for parent, child in data["spreads"]:
            self.spreads[parent].append(child)

    def data_mask(self, declaration, key):
        stack = [declaration]
        seen = set()
        mask = self.children
        while stack:
            location = stack.pop()
            if location in seen:
                continue
            seen.add(location)
            values = self.reads.get(location, {})
            for field in (key, ("all",)):
                if (location, field) in self.unsafe or (
                    field == ("attr",) and (location, "attr") in self.unsafe
                ):
                    return None
                mask |= values.get(field, 0)
            stack.extend(self.spreads[location])
        return mask

    def compare_mask(self, path, old, new):
        site = (path, old.lineno, old.col_offset)
        if site not in self.comparisons or not isinstance(new, ast.Compare):
            return None
        if len(new.ops) != 1 or not equivalent(old.left, new.left) or not equivalent(
            old.comparators, new.comparators
        ):
            return None
        before = type(old.ops[0]).__name__
        after = type(new.ops[0]).__name__
        mask = self.children & self.line_coverage.get(path, {}).get(old.lineno, self.children)
        for relation, tests in self.relations.get(site, {}).items():
            a = result_of(before, relation)
            b = result_of(after, relation)
            if a is None or b is None or a != b:
                mask |= tests
        return mask

    def refine(self, path, old_tree, new_tree, current_lines):
        if len(current_lines) != 1:
            return {}
        line, = current_lines
        old_nodes = [(node, klass) for node, klass in nodes_on_line(old_tree, line)]
        new_nodes = {(type(node), node.col_offset, klass): node
                     for node, klass in nodes_on_line(new_tree, line)}
        for node, klass in old_nodes:
            fresh = new_nodes.get((type(node), node.col_offset, klass))
            if fresh is None:
                continue
            if isinstance(node, ast.Compare):
                mask = self.compare_mask(path, node, fresh)
                if mask is None or equivalent(node.ops, fresh.ops):
                    continue
                saved = fresh.ops
                fresh.ops = node.ops
                same = equivalent(old_tree, new_tree)
                fresh.ops = saved
                if same:
                    return {line: mask}
            elif klass and isinstance(node, (ast.AnnAssign, ast.Assign)):
                targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
                if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                    continue
                declaration = (path, klass, targets[0].id)
                if declaration not in self.declarations:
                    continue
                old, new = node.value, fresh.value
                key = None
                if isinstance(old, ast.Dict) and isinstance(new, ast.Dict) and equivalent(old.keys, new.keys):
                    changes = [i for i, (a, b) in enumerate(zip(old.values, new.values)) if not equivalent(a, b)]
                    if len(changes) == 1:
                        i = changes[0]
                        value = old.values[i]
                        pure = (isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
                                and (path, value.func.id) in self.factories)
                        if passive(value) and passive(new.values[i]) or (pure and closed_factory(value, new.values[i])):
                            key = node_key(old.keys[i])
                elif passive(old) and passive(new):
                    key = ("attr",)
                if key is None:
                    continue
                fresh.value = old
                same = equivalent(old_tree, new_tree)
                fresh.value = new
                if same:
                    mask = self.data_mask(declaration, key)
                    if mask is not None:
                        return {line: mask}
        return {}


def nodes_on_line(tree, line, klass=""):
    for node in ast.iter_child_nodes(tree):
        if hasattr(node, "lineno") and not node.lineno <= line <= node.end_lineno:
            continue
        if isinstance(node, ast.ClassDef):
            child_class = ".".join(filter(None, (klass, node.name)))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            child_class = ""
        else:
            child_class = klass
        if isinstance(node, (ast.Compare, ast.AnnAssign, ast.Assign)):
            yield node, klass
        yield from nodes_on_line(node, line, child_class)
