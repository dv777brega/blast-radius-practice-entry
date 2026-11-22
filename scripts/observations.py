"""Record baseline operand relations and class-data reads on the pinned source.

Usage: python observations.py SOURCE OUTPUT [PYTEST_ARGS...]
Run from the locked lab image. The original files remain byte-for-byte intact.
"""

import ast
import atexit
import builtins
import collections
import contextvars
import enum
import functools
import hashlib
import importlib.abc
import importlib.machinery
import marshal
import operator
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import zlib
import uuid


REVISION = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
OPERATORS = {
    "Eq": operator.eq, "NotEq": operator.ne, "Is": operator.is_,
    "IsNot": operator.is_not, "Lt": operator.lt, "LtE": operator.le,
    "Gt": operator.gt, "GtE": operator.ge,
}
active_test = None
child_tests = 0
child_launches = {}
launch_in_progress = contextvars.ContextVar("observed_launch", default=False)
executed_sources = set()
starting_pool = False
child_pid = None
seen = set()
relations = collections.defaultdict(dict)
reads = collections.defaultdict(dict)
declarations = {}
source_sites = {}
scalar_names = set()
dict_names = set()
test_numbers = {}
unsafe_reads = set()
unsafe_compares = set()
spread_edges = set()
derived_names = set()
derived_sources = collections.defaultdict(set)
pure_factories = set()


def record(kind, location, observation):
    if active_test is None:
        return
    key = (kind, location, observation)
    if key not in seen:
        seen.add(key)
        output = relations if kind == 0 else reads
        values = output[location]
        values[observation] = values.get(observation, 0) | active_test


def plain_ordered(value):
    if type(value) in (int, bool, str, bytes):
        return True
    return type(value) is tuple and len(value) < 12 and all(
        type(part) in (int, bool, str, bytes) for part in value
    )


def compare(site, operation, left, right, observed_side):
    try:
        result = OPERATORS[operation](left, right)
    except Exception:
        # Tests may deliberately catch a comparison error.
        unsafe_compares.add(site)
        raise
    if active_test is None:
        unsafe_compares.add(site)
        return result
    # Store a bounded set of exact, side-tagged primitive operands for
    # literal comparisons. A saturated site keeps its overflow tests.
    if observed_side is not None:
        value = left if observed_side == 0 else right
        bucket = (site, observed_side)
        if type(value) in (int, str) and (type(value) is int or len(value) <= 48):
            entry = (type(value).__name__, value)
            known = relations.get(bucket, {})
            if len(known) >= 48 and entry not in known:
                entry = None
        else:
            entry = None
        record(0, bucket, entry)
    if left is right:
        # Arbitrary user classes (and NaN) can be unequal to themselves.
        if type(left) in (type(None), bool, int, str, bytes) or (
            isinstance(left, enum.Enum) and type(left).__eq__ in (str.__eq__, int.__eq__, object.__eq__)
        ):
            relation = "same"
        else:
            relation = "unknown"
    elif left is None or right is None:
        other = right if left is None else left
        if type(other).__eq__ in (object.__eq__, int.__eq__, str.__eq__, bytes.__eq__,
                                  tuple.__eq__, list.__eq__, dict.__eq__, set.__eq__):
            relation = "distinct"
        else:
            relation = "unknown"
    elif type(left) is type(right) and plain_ordered(left) and plain_ordered(right):
        try:
            relation = "lt" if left < right else "gt" if left > right else "equal"
        except TypeError:
            relation = "unknown"
    elif isinstance(left, enum.Enum) and type(left) is type(right) and (
        type(left).__eq__ in (object.__eq__, str.__eq__, int.__eq__)
    ):
        relation = "equal" if left == right else "distinct"
    else:
        relation = "unknown"
    record(0, site, relation)
    return result


@functools.lru_cache(maxsize=None)
def definition(owner, name):
    for parent in owner.__mro__:
        if name in vars(parent):
            return declarations.get((parent.__module__, parent.__qualname__, name))
    return None


def attr(obj, name):
    value = getattr(obj, name)
    owner = obj if isinstance(obj, type) else type(obj)
    if name in derived_names and active_test is not None:
        for parent in owner.__mro__:
            for source in derived_sources.get((parent, name), ()):
                record(1, source, ("all",))
            if name in vars(parent):
                break
    where = definition(owner, name)
    if where:
        if active_test is None:
            unsafe_reads.add((where, "attr"))
        else:
            record(1, where, ("attr",))
    return value


def key_name(key):
    if type(key) in (str, int):
        return ("key", key)
    if isinstance(key, type):
        return ("type", key.__name__)
    if isinstance(key, enum.Enum):
        return ("enum", key.name)
    return ("all",)


class ObservedDict(dict):
    def __init__(self, values, where):
        super().__init__(values)
        self.where = where

    def read_value(self, key):
        if active_test is None:
            unsafe_reads.add((self.where, key))
        else:
            record(1, self.where, key)

    def __getitem__(self, key):
        self.read_value(key_name(key))
        return super().__getitem__(key)

    def get(self, key, default=None):
        self.read_value(key_name(key))
        return super().get(key, default)

    def items(self):
        self.read_value(("all",))
        return super().items()

    def values(self):
        self.read_value(("all",))
        return super().values()

    def copy(self):
        return ObservedDict(super().copy(), self.where)


def spread(source, destination):
    if isinstance(source, ObservedDict):
        spread_edges.add((source.where, destination))
    return source


def derived_items(source, owner, destination):
    if isinstance(source, ObservedDict) and isinstance(owner, type):
        derived_sources[owner, destination].add(source.where)
        return dict.items(source)
    return source.items()


def constant_side(node):
    return isinstance(node, ast.Constant) or (
        isinstance(node, ast.Attribute) and node.attr.isupper()
    ) or (isinstance(node, ast.Tuple) and all(isinstance(i, ast.Constant) for i in node.elts))


def call(name, args, node):
    return ast.copy_location(ast.Call(ast.Name(name, ast.Load()), args, []), node)


class Rewrite(ast.NodeTransformer):
    def __init__(self, path, sites):
        self.path = path
        self.sites = sites

    def visit_Compare(self, node):
        candidate = len(node.ops) == 1 and type(node.ops[0]).__name__ in OPERATORS and (
            constant_side(node.left) or constant_side(node.comparators[0])
        )
        observed_side = None
        if candidate and isinstance(node.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            for side, operand in enumerate((node.left, node.comparators[0])):
                if isinstance(operand, ast.Constant) and type(operand.value) in (int, str):
                    observed_side = 1 - side
                    break
        self.generic_visit(node)
        if candidate:
            site = (self.path, node.lineno, node.col_offset)
            source_sites[site] = True
            return call("_br_compare", [ast.Constant(site), ast.Constant(type(node.ops[0]).__name__),
                                        node.left, node.comparators[0], ast.Constant(observed_side)], node)
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and (node.attr in scalar_names or node.attr in derived_names):
            return call("_br_attr", [node.value, ast.Constant(node.attr)], node)
        return node

    def visit_Assign(self, node):
        target = node.targets[0] if len(node.targets) == 1 else None
        if (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                and target.attr in derived_names and isinstance(node.value, ast.SetComp)):
            for gen in node.value.generators:
                source = gen.iter
                if (isinstance(source, ast.Call) and not source.args and not source.keywords
                        and isinstance(source.func, ast.Attribute) and source.func.attr == "items"
                        and isinstance(source.func.value, ast.Attribute)
                        and isinstance(source.func.value.value, ast.Name)
                        and source.func.value.value.id == target.value.id):
                    gen.iter = call("_br_derived", [source.func.value, ast.Name(target.value.id, ast.Load()),
                                                   ast.Constant(target.attr)], source)
        return self.generic_visit(node)

    def visit_Dict(self, node):
        self.generic_visit(node)
        site = self.sites.get((node.lineno, node.col_offset))
        if site is None:
            return node
        for index, key in enumerate(node.keys):
            if key is None:
                value = node.values[index]
                node.values[index] = call("_br_spread", [value, ast.Constant(site)], value)
        return call("_br_dict", [node, ast.Constant(site)], node)


def declarations_in_module(tree, path, module):
    result = {}

    def visit_classes(body, prefix):
        for stmt in body:
            if not isinstance(stmt, ast.ClassDef):
                continue
            name = ".".join(filter(None, (prefix, stmt.name)))
            for member in stmt.body:
                if isinstance(member, ast.Assign) and len(member.targets) == 1:
                    target = member.targets[0]
                elif isinstance(member, ast.AnnAssign):
                    target = member.target
                else:
                    continue
                if not isinstance(target, ast.Name) or member.value is None:
                    continue
                value = member.value
                site = (path, name, target.id)
                if isinstance(value, ast.Dict):
                    result[value.lineno, value.col_offset] = site
                    dict_names.add(target.id)
                elif isinstance(value, (ast.Constant, ast.UnaryOp)) and target.id.isupper():
                    scalar_names.add(target.id)
                else:
                    continue
                declarations[module, name, target.id] = site
            visit_classes(stmt.body, name)

    visit_classes(tree.body, "")
    return result


def factory_names(modules):
    pure = set()
    for module, (_, tree, _, _) in modules.items():
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            body = [i for i in node.body if not (
                isinstance(i, ast.Expr) and isinstance(i.value, ast.Constant) and isinstance(i.value.value, str))]
            if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
                pure.add((module, node.name))
            if (len(body) == 2 and isinstance(body[0], ast.FunctionDef) and not body[0].decorator_list
                    and not body[0].args.defaults and not any(body[0].args.kw_defaults)
                    and isinstance(body[1], ast.Return) and isinstance(body[1].value, ast.Name)
                    and body[1].value.id == body[0].name):
                pure.add((module, node.name))
    for module, (_, tree, _, path) in modules.items():
        for origin, name in pure:
            if origin == module:
                pure_factories.add((path, name))
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                for name in node.names:
                    if (node.module, name.name) in pure:
                        pure_factories.add((path, name.asname or name.name))


class SourceFinder(importlib.abc.MetaPathFinder):
    def __init__(self, files):
        self.files = files

    def find_spec(self, fullname, path, target=None):
        if fullname not in self.files:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if not spec or not spec.origin:
            return None
        file, code = self.files[fullname]
        if Path(spec.origin).resolve() != file:
            return None
        loader = importlib.machinery.SourceFileLoader(fullname, str(file))
        loader.get_code = lambda unused: code
        execute = loader.exec_module
        def in_module_context(module):
            global active_test
            previous = active_test
            active_test = None
            try:
                execute(module)
            finally:
                active_test = previous
        loader.exec_module = in_module_context
        spec.loader = loader
        return spec


def audit(event, args):
    global child_tests
    if event == "exec" and child_pid == os.getpid():
        executed_sources.add(args[0].co_filename)
    if (active_test is not None and not starting_pool and not launch_in_progress.get()
            and event in ("os.fork", "subprocess.Popen", "os.posix_spawn", "os.exec")):
        child_tests |= active_test


def track_python_launches():
    """Attribute a fresh interpreter only after its shutdown report confirms tracing."""
    original = subprocess.Popen.__init__

    def start(process, args, *positional, **kwargs):
        if (active_test is None or starting_pool or positional or kwargs.get("shell")
                or kwargs.get("env") is not None or kwargs.get("executable") is not None
                or not isinstance(args, (list, tuple)) or len(args) != 3
                or args[1] != "-c" or args[0] not in ("python", "python3", sys.executable)):
            return original(process, args, *positional, **kwargs)
        token = uuid.uuid4().hex
        child_launches[token] = active_test
        env = dict(os.environ, BR_CHILD_TOKEN=token)
        mark = launch_in_progress.set(True)
        try:
            return original(process, args, env=env, **kwargs)
        finally:
            launch_in_progress.reset(mark)

    subprocess.Popen.__init__ = start


def install(files):
    builtins._br_compare = compare
    builtins._br_dict = ObservedDict
    builtins._br_attr = attr
    builtins._br_spread = spread
    builtins._br_derived = derived_items
    sys.meta_path.insert(0, SourceFinder(files))
    sys.addaudithook(audit)


def begin_child():
    global child_pid
    if child_pid == os.getpid():
        return
    child_pid = os.getpid()
    from multiprocessing.util import Finalize
    written = False
    pid = os.getpid()

    def flush():
        nonlocal written
        if written or os.getpid() != pid:
            return
        written = True
        path = Path(os.environ["BR_OBSERVATIONS_TEMP"]) / f"worker-{pid}"
        token = os.environ.get("BR_CHILD_TOKEN")
        if token:
            (path.parent / ("launch-" + token)).write_bytes(marshal.dumps(sorted(executed_sources)))
        path.write_bytes(marshal.dumps((dict(relations), dict(reads), tuple(unsafe_reads),
                                        tuple(unsafe_compares), tuple(spread_edges), child_tests)))
    Finalize(None, flush, exitpriority=-10)
    atexit.register(flush)


def bootstrap_child():
    global active_test
    folder = Path(os.environ["BR_OBSERVATIONS_TEMP"])
    files, defs, names, derived = marshal.loads((folder / "compiled").read_bytes())
    declarations.update(defs)
    scalar_names.update(names)
    derived_names.update(derived)
    install({name: (Path(path), code) for name, (path, code) in files.items()})
    value = os.environ.get("BR_OBSERVATIONS_MASK")
    active_test = int(value, 16) if value else None
    begin_child()


def merge_children(folder):
    global child_tests
    for file in folder.glob("worker-*"):
        comp, data, unsafe, initial, spread, children = marshal.loads(file.read_bytes())
        for store, observations in ((relations, comp), (reads, data)):
            for site, values in observations.items():
                target = store[site]
                for value, mask in values.items():
                    target[value] = target.get(value, 0) | mask
        unsafe_reads.update(unsafe)
        unsafe_compares.update(initial)
        spread_edges.update(spread)
        child_tests |= children


def main():
    global child_tests
    import pytest
    from time import monotonic

    started = monotonic()
    root = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    head = (root / ".git" / "HEAD").read_text().strip()
    if head.startswith("ref:") or len(head) != 40:
        head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    if head != REVISION:
        raise ValueError("upstream revision differs")
    os.chdir(root)
    sys.path.insert(0, str(root))
    modules = {}
    hashes = {}
    for file in sorted((root / "sqlglot").rglob("*.py")):
        relative = file.relative_to(root)
        path = relative.as_posix()
        module = ".".join(relative.with_suffix("").parts).removesuffix(".__init__")
        contents = file.read_bytes()
        hashes[path] = hashlib.sha256(contents).digest()
        tree = ast.parse(contents, str(file))
        sites = declarations_in_module(tree, path, module)
        modules[module] = file, tree, sites, path
        for stmt in ast.walk(tree):
            if (isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Attribute) and isinstance(stmt.value, ast.SetComp)):
                derived_names.add(stmt.targets[0].attr)
    factory_names(modules)
    files = {}
    for module, (file, tree, sites, path) in modules.items():
        tree = Rewrite(path, sites).visit(tree)
        files[module] = file, compile(ast.fix_missing_locations(tree), str(file), "exec")
    del modules
    scratch = tempfile.TemporaryDirectory(prefix="sqlglot-observations-")
    folder = Path(scratch.name)
    code = {name: (str(file), bytecode) for name, (file, bytecode) in files.items()}
    (folder / "compiled").write_bytes(marshal.dumps((code, declarations, scalar_names, derived_names)))
    (folder / "sitecustomize.py").write_text("import observations\nobservations.bootstrap_child()\n")
    os.environ["BR_OBSERVATIONS_TEMP"] = str(folder)
    os.environ["PYTHONPATH"] = os.pathsep.join((str(folder), str(Path(__file__).resolve().parent),
                                              os.environ.get("PYTHONPATH", "")))
    install(files)
    track_python_launches()
    from observation_worker import track_submissions
    sys.modules["observations"] = sys.modules[__name__]
    track_submissions(sys.modules[__name__])

    class Plugin:
        groups = {}

        def pytest_collection_finish(self, session):
            ids = sorted({item.nodeid for item in session.items})
            test_numbers.update({name: 1 << index for index, name in enumerate(ids)})
            for item in session.items:
                for parent in item.listchain():
                    self.groups[parent.nodeid] = self.groups.get(parent.nodeid, 0) | test_numbers[item.nodeid]

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            global active_test
            active_test = test_numbers[item.nodeid]
            os.environ["BR_OBSERVATIONS_MASK"] = hex(active_test)
            seen.clear()
            try:
                return (yield)
            finally:
                active_test = None
                os.environ.pop("BR_OBSERVATIONS_MASK", None)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_fixture_setup(self, fixturedef, request):
            global active_test, seen
            previous, previous_seen = active_test, seen
            active_test = self.groups.get(request.node.nodeid, previous)
            if active_test:
                os.environ["BR_OBSERVATIONS_MASK"] = hex(active_test)
            seen = set()
            try:
                return (yield)
            finally:
                active_test, seen = previous, previous_seen
                if active_test:
                    os.environ["BR_OBSERVATIONS_MASK"] = hex(active_test)

    rc = pytest.main(["-q", "-p", "no:cacheprovider", *sys.argv[3:]], plugins=[Plugin()])
    if rc:
        raise SystemExit(int(rc))
    merge_children(folder)
    child_modules = {}
    for token, mask in child_launches.items():
        report = folder / ("launch-" + token)
        if not report.is_file():
            child_tests |= mask
            continue
        for filename in marshal.loads(report.read_bytes()):
            path = Path(filename)
            if path.is_relative_to(root):
                relative = path.relative_to(root).as_posix()
                child_modules[relative] = child_modules.get(relative, 0) | mask
    scratch.cleanup()
    output.parent.mkdir(parents=True, exist_ok=True)
    data = dict(version=1, revision=head, sha256=hashes, tests=sorted(test_numbers),
                declarations=sorted(declarations.values()), comparisons=sorted(source_sites),
                relations=dict(relations), reads=dict(reads), unsafe=sorted(unsafe_reads,key=repr),
                spreads=sorted(spread_edges), children=child_tests, child_modules=child_modules, factories=sorted(pure_factories),
                import_comparisons=sorted(unsafe_compares))
    output.write_bytes(zlib.compress(marshal.dumps(data), 9))
    print(f"{output.stat().st_size} bytes, {len(source_sites)} comparisons, "
          f"{len(declarations)} data declarations, {monotonic()-started:.1f}s", file=sys.stderr)


if __name__ == "__main__":
    main()
