"""Bounded model of incremental content changes. Python 3.10+."""
from collections import deque
from dataclasses import dataclass, replace

# Two headings share one file; a third node lives in a separate file.
FILES = (0, 0, 1)
FEED = ((0, 2), (1, 2), (2, 2), (0, 3))
NODES = range(len(FILES))


def put(values, i, value):
    return values[:i] + (value,) + values[i + 1:]


@dataclass(frozen=True)
class State:
    cursor: int = 0
    inbox: tuple = (0, 0, 0)   # Highest required content revision per node.
    base: tuple = (1, 1, 1)    # In this model, revision n has distinct content n.
    disk: tuple = (1, 1, 1)    # 0 represents the user's own edit.
    draft: int = -1            # Node with unsaved edits, or -1 for none.
    intent: tuple = (0, 0, 0)  # Durable target snapshot; base is pinned until settled.
    ready: tuple = (False,) * 3  # Volatile permission to perform a prepared write.
    review: tuple = (0, 0, 0)  # Remote revision durably handed to reconciliation.


def dirty(s, node):
    return s.draft != -1 and FILES[s.draft] == FILES[node]


def accept_page(s, after, size):
    # One SQLite transaction stores all item obligations and the cursor.
    if after != s.cursor:
        return s
    inbox = s.inbox
    page = FEED[after:after + size]
    for node, rev in page:
        if rev > max(s.base[node], s.review[node]):
            inbox = put(inbox, node, max(inbox[node], rev))
    return replace(s, inbox=inbox, cursor=after + len(page))


def finish(s, i, target, review=False):
    inbox = put(s.inbox, i, 0) if s.inbox[i] <= target else s.inbox
    updates = dict(intent=put(s.intent, i, 0), ready=put(s.ready, i, False), inbox=inbox)
    if review:
        updates["review"] = put(s.review, i, target)
    else:
        updates["base"] = put(s.base, i, target)
    return replace(s, **updates)


def edges(s):
    for size in (1, 2):
        if s.cursor + size <= len(FEED):
            yield f"page {size}", accept_page(s, s.cursor, size)
    if s.cursor:
        yield "stale page", accept_page(s, s.cursor - 1, 1)
    # A sync-worker restart loses only volatile write permission.
    yield "restart", replace(s, ready=(False,) * 3)
    if s.draft != -1:
        i = s.draft
        yield "save", replace(s, disk=put(s.disk, i, 0), draft=-1)
        yield "discard", replace(s, draft=-1)
    for i in NODES:
        if s.draft == -1:
            yield f"edit {i}", replace(s, draft=i)
        if dirty(s, i):
            continue
        target = s.intent[i]
        if target:
            if s.disk[i] == target:
                yield f"ack {i}", finish(s, i, target)
            elif s.ready[i] and s.disk[i] == s.base[i]:
                yield f"write {i}", replace(s, disk=put(s.disk, i, target),
                                             ready=put(s.ready, i, False))
            else:
                yield f"review {i}", finish(s, i, target, review=True)
        elif s.inbox[i]:
            # A current-node GET may already see a later change than this page.
            for rev in sorted({r for n, r in FEED if n == i and r >= s.inbox[i]}):
                if s.review[i] or s.disk[i] != s.base[i]:
                    yield f"review {i}@{rev}", finish(s, i, rev, review=True)
                else:
                    yield f"prepare {i}@{rev}", replace(s, intent=put(s.intent, i, rev),
                                                         ready=put(s.ready, i, True))


def check(s, event, t):
    assert s.cursor <= t.cursor <= len(FEED)
    if event == "stale page":
        assert t == s
    for i, rev in FEED[:t.cursor]:
        assert max(t.inbox[i], t.base[i], t.review[i]) >= rev, "lost feed obligation"
    for i in NODES:
        assert s.base[i] <= t.base[i] and s.review[i] <= t.review[i]
        if t.disk[i] != s.disk[i] and event != "save":
            assert event == f"write {i}" and not dirty(s, i)
            assert s.disk[i] == s.base[i] and s.ready[i] and t.disk[i] == s.intent[i]
        if t.base[i] != s.base[i]:
            assert t.base[i] == t.disk[i] == s.intent[i]
        if t.intent[i]:
            assert t.inbox[i] > 0
    if event == "save":
        assert t.disk == put(s.disk, s.draft, 0)
    if event.startswith("page"):
        assert t.disk == s.disk and t.draft == s.draft


def step(s, event):
    for name, t in edges(s):
        if name == event:
            check(s, name, t)
            return t
    raise AssertionError(f"Unavailable {event}: {s}")


def trace(*events):
    s = State()
    for e in events:
        s = step(s, e)
    return s


def indicator(s, file):
    return sum(bool(s.inbox[i]) for i in NODES if FILES[i] == file)


def cases():
    s = trace("edit 1", "page 2", "page 2", "prepare 2@2", "write 2", "ack 2")
    assert s.disk == (1, 1, 2) and indicator(s, 0) == 2 and s.cursor == 4
    assert accept_page(s, 0, 2) == s
    for e in ("save", "prepare 0@3", "write 0", "ack 0", "review 1@2"):
        s = step(s, e)
    assert s.disk == (3, 0, 2) and s.review == (0, 2, 0) and indicator(s, 0) == 0

    s = trace("page 1", "prepare 0@2", "edit 1")
    assert "write 0" not in dict(edges(s))
    for e in ("discard", "write 0", "page 2", "page 1", "ack 0"):
        s = step(s, e)
    assert s.base[0] == 2 and s.inbox[0] == 3

    s = trace("page 1", "prepare 0@2", "restart", "review 0")
    assert s.disk[0] == 1 and s.review[0] == 2
    s = trace("page 1", "prepare 0@2", "write 0", "restart", "ack 0")
    assert s.base[0] == 2 and s.inbox[0] == 0
    s = trace("page 1", "prepare 0@2", "write 0", "edit 0", "save", "review 0")
    assert s.disk[0] == 0 and s.base[0] == 1 and s.review[0] == 2

    s = trace("page 1", "prepare 0@3", "write 0", "ack 0", "page 2", "page 1")
    assert s.cursor == 4 and s.inbox == (0, 2, 2)


def drain(s, settled):
    path = set()
    while s not in settled:
        assert s not in path and len(path) < 32, "recovery failed to settle"
        path.add(s)
        if s.draft != -1:
            event = "save"
        elif s.cursor < len(FEED):
            event = "page 1"
        elif any(s.inbox):
            event = next(n for n, _ in edges(s) if n.startswith(("prepare", "write", "ack", "review")))
        else:
            assert all(max(s.base[i], s.review[i]) >= rev for i, rev in FEED)
            break
        s = step(s, event)
    settled.update(path)


def explore(depth):
    start = State()
    queue = deque([(start, 0)])
    parents = {start: None}
    transitions = 0
    while queue:
        s, level = queue.popleft()
        if level == depth:
            continue
        for event, t in edges(s):
            transitions += 1
            try:
                check(s, event, t)
            except AssertionError:
                path = [event]
                while parents[s]:
                    s, event = parents[s]
                    path.append(event)
                raise AssertionError("Counterexample: " + " -> ".join(reversed(path)))
            if t not in parents:
                parents[t] = (s, event)
                queue.append((t, level + 1))
    settled = set()
    for s in parents:
        drain(s, settled)
    return len(parents), transitions


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Run without -O so assertions are enabled.")
    cases()
    states, transitions = explore(10)
    print(f"Depth 10: {states:,} states, {transitions:,} transitions. Cases and recovery passed.")
