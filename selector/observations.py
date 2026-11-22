"""Interpret source-derived observations of the unchanged, pinned test suite."""

import ast
import collections
import marshal
import operator
import zlib


LITERAL_OPERATORS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Lt: operator.lt,
    ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
}


_MEMBERSHIP = {ast.In: lambda value, options: value in options,
               ast.NotIn: lambda value, options: value not in options}


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


def _assign_target_name(node):
    if not isinstance(node, (ast.AnnAssign, ast.Assign)):
        return None
    targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
        return None
    return targets[0].id


def _find_class_assign(tree, target_klass, target_name, klass=""):
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            child_class = ".".join(filter(None, (klass, node.name)))
            found = _find_class_assign(node, target_klass, target_name, child_class)
            if found is not None:
                return found
        elif klass == target_klass and isinstance(node, (ast.AnnAssign, ast.Assign)):
            if _assign_target_name(node) == target_name:
                return node
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
        self.child_modules = data.get("child_modules", {})
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
        mask = self.children | self.child_modules.get(declaration[0], 0)
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
        if len(old.ops) != 1 or len(new.ops) != 1:
            return None
        if not equivalent(old.left, new.left) or not equivalent(old.comparators, new.comparators):
            return self.literal_mask(path, site, old, new)
        before = type(old.ops[0]).__name__
        after = type(new.ops[0]).__name__
        mask = (self.children | self.child_modules.get(path, 0)) & self.line_coverage.get(path, {}).get(old.lineno, self.children)
        for relation, tests in self.relations.get(site, {}).items():
            a = result_of(before, relation)
            b = result_of(after, relation)
            if a is None or b is None or a != b:
                mask |= tests
        return mask

    def literal_mask(self, path, site, old, new):
        original_op = LITERAL_OPERATORS.get(type(old.ops[0]))
        if original_op is None:
            return None
        before = (old.left, old.comparators[0])
        after = (new.left, new.comparators[0])
        for side, (original, replacement) in enumerate(zip(before, after)):
            if not isinstance(original, ast.Constant) or type(original.value) not in (int, str):
                continue
            other = 1 - side
            if not equivalent(before[other], after[other]):
                continue
            a = original.value
            new_op = LITERAL_OPERATORS.get(type(new.ops[0]))
            if side == 1 and isinstance(new.ops[0], (ast.In, ast.NotIn)):
                if not isinstance(replacement, (ast.List, ast.Tuple)) or not all(
                    isinstance(el, ast.Constant) and type(el.value) is type(a)
                    for el in replacement.elts
                ):
                    continue
                b = tuple(el.value for el in replacement.elts)
                new_op = _MEMBERSHIP[type(new.ops[0])]
            else:
                if new_op is None or not isinstance(replacement, ast.Constant):
                    continue
                b = replacement.value
                if type(a) is not type(b):
                    continue
            seen = self.relations.get((site, other))
            if not seen:
                return None
            mask = self.children | self.child_modules.get(path, 0)
            for item, tests in seen.items():
                if item is None or item[0] != type(a).__name__:
                    mask |= tests
                elif side == 0 and original_op(a, item[1]) != new_op(b, item[1]):
                    mask |= tests
                elif side == 1 and original_op(item[1], a) != new_op(item[1], b):
                    mask |= tests
            return mask
        return None

    def _dict_keys_mask(self, path, declaration, old, new):
        if not (isinstance(old, ast.Dict) and isinstance(new, ast.Dict)):
            return None
        if equivalent(old.keys, new.keys):
            combined = 0
            for key_node, a, b in zip(old.keys, old.values, new.values):
                if equivalent(a, b):
                    continue
                key = node_key(key_node)
                pure = (isinstance(a, ast.Call) and isinstance(a.func, ast.Name)
                        and (path, a.func.id) in self.factories and closed_factory(a, b))
                if key is None or not ((passive(a) and passive(b)) or pure):
                    return None
                bits = self.data_mask(declaration, key)
                if bits is None:
                    return None
                combined |= bits
            return combined
        # A key addition, deletion, or reorder can affect membership and iteration,
        # even if no test fetched either key's value.
        return None

    def refine(self, path, old_tree, new_tree, current_lines):
        if not current_lines:
            return {}
        if len(current_lines) == 1:
            line, = current_lines
            old_nodes = [(node, klass) for node, klass in nodes_on_line(old_tree, line)]
            new_nodes = {(type(node), node.col_offset, klass): node
                         for node, klass in nodes_on_line(new_tree, line)}
            for node, klass in old_nodes:
                if isinstance(node, ast.Compare):
                    fresh = new_nodes.get((type(node), node.col_offset, klass))
                    if fresh is None:
                        continue
                    mask = self.compare_mask(path, node, fresh)
                    if mask is None or equivalent(node, fresh):
                        continue
                    saved = fresh.left, fresh.ops, fresh.comparators
                    fresh.left, fresh.ops, fresh.comparators = node.left, node.ops, node.comparators
                    same = equivalent(old_tree, new_tree)
                    fresh.left, fresh.ops, fresh.comparators = saved
                    if same:
                        return {line: mask}
                elif klass and isinstance(node, (ast.AnnAssign, ast.Assign)):
                    target_name = _assign_target_name(node)
                    if target_name is None:
                        continue
                    declaration = (path, klass, target_name)
                    if declaration not in self.declarations:
                        continue
                    fresh = _find_class_assign(new_tree, klass, target_name)
                    if fresh is None or fresh.value is None:
                        continue
                    old, new = node.value, fresh.value
                    mask = self._dict_keys_mask(path, declaration, old, new)
                    if mask is None and passive(old) and passive(new):
                        mask = self.data_mask(declaration, ("attr",))
                    if mask is None:
                        continue
                    fresh.value = old
                    same = equivalent(old_tree, new_tree)
                    fresh.value = new
                    if same:
                        return {line: mask}

        # Handle multi-line / multi-entry edits within a single tracked class dict.
        anchor = min(current_lines)
        for node, klass in nodes_on_line(old_tree, anchor):
            if not klass or not isinstance(node, (ast.AnnAssign, ast.Assign)):
                continue
            if not all(node.lineno <= ln <= node.end_lineno for ln in current_lines):
                continue
            target_name = _assign_target_name(node)
            if target_name is None:
                continue
            declaration = (path, klass, target_name)
            if declaration not in self.declarations:
                continue
            fresh = _find_class_assign(new_tree, klass, target_name)
            if fresh is None or fresh.value is None:
                continue
            mask = self._dict_keys_mask(path, declaration, node.value, fresh.value)
            if mask is None:
                continue
            saved = fresh.value
            fresh.value = node.value
            same = equivalent(old_tree, new_tree)
            fresh.value = saved
            if same:
                return {ln: mask for ln in current_lines}
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
