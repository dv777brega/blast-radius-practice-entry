"""Read Git and plain unified diffs. Apply exact hunks against trusted base files."""
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
QUOTE = re.compile(r'\\([0-7]{1,3}|.)')


def path_name(text, strip_prefix=True):
    value = text.split("\t", 1)[0]
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        if not value.endswith('"'):
            raise ValueError("invalid quoted Git path")
        escapes = {"n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f", "v": "\v",
                   "a": "\a", '"': '"', "\\": "\\"}
        def unescape(match):
            code = match[1]
            return chr(int(code, 8)) if code[0] in "01234567" else escapes[code]
        value = QUOTE.sub(unescape, value[1:-1].encode("utf-8").decode("latin-1")).encode("latin-1").decode("utf-8")
    if strip_prefix and value.startswith(("a/", "b/")):
        value = value[2:]
    path = PurePosixPath(value)
    if not path.parts or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value or "\0" in value:
        raise ValueError("unsafe patch path")
    return path.as_posix()


@dataclass
class Hunk:
    old: int
    new: int
    body: list = field(default_factory=list)


@dataclass
class Change:
    old: str | None = None
    new: str | None = None
    hunks: list = field(default_factory=list)


def parse(text):
    rows = text.replace("\r\n", "\n").split("\n")
    changes = []
    active = None
    i = 0
    while i < len(rows):
        line = rows[i]
        i += 1
        if line.startswith("diff --git "):
            active = Change()
            changes.append(active)
        elif line.startswith("--- "):
            if i >= len(rows) or not rows[i].startswith("+++ "):
                raise ValueError("missing new file header")
            if active is None or active.hunks:
                active = Change()
                changes.append(active)
            active.old = path_name(line[4:])
            active.new = path_name(rows[i][4:])
            i += 1
        elif line.startswith(("rename from ", "copy from ", "rename to ", "copy to ")):
            if active is None:
                raise ValueError("missing rename header")
            _, direction, name = line.split(" ", 2)
            setattr(active, "old" if direction == "from" else "new", path_name(name, False))
        elif line.startswith("@@"):
            head = HEADER.match(line)
            if active is None or not head:
                raise ValueError("invalid hunk header")
            a, na, b, nb = head.groups()
            old_left, new_left = int(na or 1), int(nb or 1)
            old = int(a) - int(old_left != 0)
            new = int(b) - int(new_left != 0)
            if min(old, new) < 0:
                raise ValueError("invalid hunk offset")
            hunk = Hunk(old, new)
            active.hunks.append(hunk)
            while old_left or new_left:
                if i >= len(rows) or rows[i][:1] not in (" ", "+", "-"):
                    raise ValueError("truncated hunk")
                op, content = rows[i][0], rows[i][1:] + "\n"
                i += 1
                if i < len(rows) and rows[i] == "\\ No newline at end of file":
                    content = content[:-1]
                    i += 1
                old_left -= op != "+"
                new_left -= op != "-"
                if min(old_left, new_left) < 0:
                    raise ValueError("hunk exceeds header counts")
                hunk.body.append((op, content))
        elif line.startswith(("GIT binary patch", "Binary files ")):
            raise ValueError("binary patch")
        elif line.startswith(("index ", "similarity index ", "dissimilarity index ", "old mode ", "new mode ", "new file mode ", "deleted file mode ")):
            continue
        elif line.strip():
            raise ValueError("unexpected text outside a hunk")
    if not changes or any(not (c.old or c.new) for c in changes):
        raise ValueError("missing file headers")
    return changes


def apply(change, original):
    parts = original.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    result, groups = [], []
    cursor = 0
    for hunk in change.hunks:
        if not cursor <= hunk.old <= len(lines):
            raise ValueError("overlapping or misplaced hunk")
        result.extend(lines[cursor:hunk.old])
        cursor = hunk.old
        if len(result) != hunk.new:
            raise ValueError("new hunk offset mismatch")
        start = None
        for op, value in hunk.body:
            if op == " " and start is not None:
                groups.append((start[0], cursor, start[1], len(result)))
                start = None
            elif op != " " and start is None:
                start = (cursor, len(result))
            if op in ("-", " "):
                if cursor >= len(lines) or lines[cursor] != value:
                    raise ValueError("pinned source/context mismatch")
                cursor += 1
            if op in ("+", " "):
                result.append(value)
        if start is not None:
            groups.append((start[0], cursor, start[1], len(result)))
    result.extend(lines[cursor:])
    return "".join(result), groups
