"""Fresh-install discovery and change-feed cutover. Python 3.10+."""
from collections import deque
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Head:
    revision: int
    state: str
    body: str = ""


# Ordered IDs stand in for UUIDs. ID 0 is created behind the first page.
ABSENT = Head(0, "absent")
HISTORY = (
    (1, Head(1, "active", "one")), (3, Head(1, "active", "three")),
    (0, Head(1, "active", "late low ID")), (1, Head(2, "active", "edited")),
    (3, Head(2, "trashed", "three")), (3, Head(3, "purged")),
    (2, Head(1, "active", "two")), (2, Head(2, "archived", "two")),
    (2, Head(3, "active", "two again")),
)
EMPTY = (ABSENT,) * 4


def dataset(seq):
    nodes = list(EMPTY)
    for node, head in HISTORY[:seq]:
        nodes[node] = head
    return tuple(nodes)


@dataclass(frozen=True)
class Client:
    phase: str = "start"
    after: int = -1
    cursor: int = 0
    fence: int = 0
    needed: tuple = (0,) * 4
    copies: tuple = EMPTY


@dataclass(frozen=True)
class State:
    client: Client = Client()
    seq: int = 2
    # One volatile staged response per serialized worker. Dropping it models a
    # crash before commit. Each installation has a single checkpoint owner.
    staged: Client | None = None


def need(c, node, revision):
    values = list(c.needed)
    values[node] = max(values[node], revision)
    return replace(c, needed=tuple(values))


def response(s):
    c = s.client
    if c.phase == "start":
        return replace(c, phase="list", cursor=s.seq)
    if c.phase == "list":
        nodes = dataset(s.seq)
        page = [i for i, h in enumerate(nodes) if i > c.after and h.state == "active"][:1]
        if not page:
            return replace(c, phase="fence")
        i = page[0]
        return replace(need(c, i, nodes[i].revision), after=i)
    if c.phase == "fence":
        return replace(c, phase="catchup", fence=s.seq)
    if c.phase in ("catchup", "live"):
        if c.phase == "catchup" and c.cursor == c.fence:
            return replace(c, phase="live")
        if c.cursor < s.seq:
            i, head = HISTORY[c.cursor]
            return replace(need(c, i, head.revision), cursor=c.cursor + 1)
    return None


def fetch_body(s, i):
    c = s.client
    if c.copies[i].revision >= c.needed[i]:
        return None
    head = dataset(s.seq)[i]
    if head.state == "purged":
        # A node GET would be 404. Only the consumed purge event authorizes
        # recording the tombstone at its revision.
        if (i, head) not in HISTORY[:c.cursor] or c.needed[i] < head.revision:
            return None
    copies = list(c.copies)
    copies[i] = head if head.state == "active" else Head(head.revision, head.state)
    return replace(c, copies=tuple(copies))


def edges(s):
    if s.seq < len(HISTORY):
        yield "server write", replace(s, seq=s.seq + 1)
    if s.staged is not None:
        yield "commit", replace(s, client=s.staged, staged=None)
        yield "crash", replace(s, staged=None)
        return
    nxt = response(s)
    if nxt is not None:
        yield s.client.phase, replace(s, staged=nxt)
    for i in range(4):
        nxt = fetch_body(s, i)
        if nxt is not None:
            yield f"body {i}", replace(s, staged=nxt)


def check(s, action, t):
    before, c = s.client, t.client
    assert before.cursor <= c.cursor <= t.seq
    if action != "commit":
        assert before == c
    assert all(a <= b for a, b in zip(before.needed, c.needed))
    if c.phase == "live":
        assert c.cursor >= c.fence
        # At every consumed prefix, every active ID must be covered, even
        # if there is already a later observed revision for that ID.
        for i, head in enumerate(dataset(c.cursor)):
            if head.state == "active":
                assert c.needed[i] >= head.revision


def step(s, action):
    for label, t in edges(s):
        if label == action:
            check(s, label, t)
            return t
    raise AssertionError(f"Unavailable {action} in {s}")


def settle(s, known):
    path = set()
    while s not in known:
        assert s not in path and len(path) < 48
        path.add(s)
        choices = dict(edges(s))
        if "commit" in choices:
            action = "commit"
        elif s.client.phase in choices:
            action = s.client.phase
        else:
            action = next((key for key in choices if key.startswith("body ")), None)
            if action is None:
                c = s.client
                assert c.phase == "live" and c.cursor == s.seq
                for copy, head in zip(c.copies, dataset(s.seq)):
                    if head.state == "active":
                        assert copy == head, "active body missing after drain"
                    else:
                        assert copy.state != "active", "stale active body after drain"
                break
        s = step(s, action)
    known.update(path)


def cases():
    s = State()
    for event in ("start", "commit", "list", "server write", "crash", "list", "commit"):
        s = step(s, event)
    assert s.client.after == 0  # Replayed page saw the concurrent insertion.

    s = State()
    for event in ("start", "commit", "list", "commit", "server write",
                  "list", "commit", "list", "commit", "fence", "commit"):
        s = step(s, event)
    assert s.client.needed[0] == 0 and s.client.fence == 3
    for event in ("catchup", "server write", "crash", "catchup", "commit",
                  "catchup", "commit"):
        s = step(s, event)
    assert s.client.phase == "live" and s.client.cursor == 3
    assert s.client.needed[0] == 1 and s.seq == 4
    settle(s, set())

    # A listing can observe a higher revision before replaying an older event.
    s = State()
    for event in ("start", "commit", "server write", "server write", "list", "commit",
                  "list", "commit"):
        s = step(s, event)
    assert s.client.needed[1] == 2
    settle(s, set())

    # The listing saw ID 3, but its body was purged before the GET.
    s = State()
    for event in ("start", "commit", "list", "commit", "list", "commit",
                  "server write", "server write", "server write", "server write"):
        s = step(s, event)
    assert s.client.needed[3] == 1 and fetch_body(s, 3) is None
    for event in ("list", "commit", "fence", "commit", *(("catchup", "commit") * 4),
                  "body 3", "crash", "body 3", "commit"):
        s = step(s, event)
    assert s.client.copies[3] == Head(3, "purged")


def explore(depth):
    initial = State()
    queue = deque([(initial, 0)])
    paths = {initial: None}
    transitions = 0
    while queue:
        s, level = queue.popleft()
        if level == depth:
            continue
        for name, t in edges(s):
            transitions += 1
            try:
                check(s, name, t)
            except AssertionError:
                trace = [name]
                while paths[s]:
                    s, name = paths[s]
                    trace.append(name)
                raise AssertionError("Counterexample: " + " -> ".join(reversed(trace)))
            if t not in paths:
                paths[t] = (s, name)
                queue.append((t, level + 1))
    known = set()
    for s in paths:
        settle(s, known)
    return len(paths), transitions


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Run without -O so assertions are enabled.")
    cases()
    n, steps = explore(16)
    print(f"Depth 16: {n:,} states; {steps:,} transitions. Named traces and offline-copy drain passed.")
