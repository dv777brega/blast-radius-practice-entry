"""Python 3.13 line and read tracing for the pristine sqlglot checkout."""

import atexit
import marshal
import os
import sys
import tempfile

child_tracker = None


def merge_masks(destination, source):
    for key, mask in source.items():
        destination[key] = destination.get(key, 0) | mask


class Tracker:
    def __init__(self, root):
        self.prefix = os.path.realpath(root) + os.sep
        self.mask = 0
        self.lines = {}
        self.init = {}
        self.modules = {}
        self.reads = {}
        self.sensitive = {}
        self.collect_reads = set()
        self.collect_sensitive = set()
        self.code_paths = {}
        self.active = True
        self.mon = sys.monitoring
        self.tool = self.mon.COVERAGE_ID
        self.mon.use_tool_id(self.tool, "blast-radius-map")
        self.mon.register_callback(self.tool, self.mon.events.PY_START, self.start_code)
        self.mon.register_callback(self.tool, self.mon.events.LINE, self.line)
        self.mon.set_events(self.tool, self.mon.events.PY_START)
        sys.addaudithook(self.audit)

    def set_mask(self, bits):
        if bits != self.mask:
            self.mask = bits
            self.mon.restart_events()

    def start_code(self, code, _offset):
        filename = code.co_filename
        if filename.startswith(self.prefix):
            path = filename[len(self.prefix):]
            if path.endswith(".py"):
                self.code_paths[id(code)] = (code, path)
                self.mon.set_local_events(self.tool, code, self.mon.events.LINE)
        return self.mon.DISABLE

    def line(self, code, lineno):
        path = self.code_paths[id(code)][1]
        mask = self.mask
        # Module and class bodies are built once. A lambda or method declared
        # on the same physical line has its own optimized code object.
        if not mask or not code.co_flags & 1:
            self.init.setdefault(path, set()).add(lineno)
        else:
            hits = self.lines.setdefault(path, {})
            hits[lineno] = hits.get(lineno, 0) | mask
        if mask:
            self.modules[path] = self.modules.get(path, 0) | mask
        return self.mon.DISABLE

    def audit(self, event, args):
        if event != "open" or not self.active:
            return
        file, mode, flags = args
        if not isinstance(file, str) or flags & os.O_ACCMODE == os.O_WRONLY:
            return
        absolute = os.path.abspath(file)
        if not absolute.startswith(self.prefix):
            return
        path = absolute[len(self.prefix):]
        if "/__pycache__/" in path or path.startswith((".git/", ".pytest_cache/")):
            return
        # A loader opening .py is an import dependency, but an ordinary
        # import cannot make a test sensitive to the text of a comment.
        sensitive = path.endswith(".py")
        owner = None
        if sensitive or not self.mask:
            frame = sys._getframe(1)
            while frame:
                source = frame.f_code.co_filename
                if "importlib._bootstrap_external" in source and frame.f_code.co_name == "get_data":
                    sensitive = False
                if (frame.f_globals.get("__name__") == "_pytest.assertion.rewrite"
                        and frame.f_code.co_name == "_rewrite_test"):
                    sensitive = False
                if source.startswith(self.prefix + "tests/") and owner is None:
                    owner = source[len(self.prefix):]
                frame = frame.f_back
        if path.endswith(".py") and not sensitive:
            if self.mask:
                self.modules[path] = self.modules.get(path, 0) | self.mask
            return
        if self.mask:
            self.reads[path] = self.reads.get(path, 0) | self.mask
            if sensitive:
                self.sensitive[path] = self.sensitive.get(path, 0) | self.mask
        else:
            self.collect_reads.add((path, owner))
            if sensitive:
                self.collect_sensitive.add((path, owner))

    def stop(self):
        self.active = False
        self.mon.set_events(self.tool, 0)
        for code, _ in self.code_paths.values():
            self.mon.set_local_events(self.tool, code, 0)
        self.mon.free_tool_id(self.tool)

    def data(self):
        return (self.lines, self.init, self.modules, self.reads, self.sensitive)

    def merge(self, data):
        lines, initial, modules, reads, sensitive = data
        for path, hits in lines.items():
            merge_masks(self.lines.setdefault(path, {}), hits)
        for path, values in initial.items():
            self.init.setdefault(path, set()).update(values)
        merge_masks(self.modules, modules)
        merge_masks(self.reads, reads)
        merge_masks(self.sensitive, sensitive)


def trace_child():
    global child_tracker
    bits = os.environ.get("BLAST_MAP_MASK")
    if not bits:
        return
    trace = Tracker(os.environ["BLAST_MAP_ROOT"])
    child_tracker = trace
    trace.set_mask(int(bits, 16))
    output = os.environ["BLAST_MAP_CHILDREN"]

    def flush():
        trace.stop()
        with tempfile.NamedTemporaryFile(dir=output, delete=False) as stream:
            marshal.dump(trace.data(), stream)

    atexit.register(flush)


def run_pool_task(mask, payload):
    """Switch context before unpickling the arguments of each reusable pool task."""
    from multiprocessing.reduction import ForkingPickler

    if child_tracker is None:
        raise RuntimeError("a map-building pool worker started without tracing")
    child_tracker.set_mask(mask)
    function, args, kwargs = ForkingPickler.loads(payload)
    return function(*args, **kwargs)


def track_pool_submissions(tracker):
    from concurrent.futures import ProcessPoolExecutor
    from multiprocessing.reduction import ForkingPickler

    submit = ProcessPoolExecutor.submit

    def submit_with_context(executor, function, /, *args, **kwargs):
        payload = bytes(ForkingPickler.dumps((function, args, kwargs)))
        return submit(executor, run_pool_task, tracker.mask, payload)

    ProcessPoolExecutor.submit = submit_with_context
