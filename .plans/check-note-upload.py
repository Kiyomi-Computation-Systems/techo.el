"""Executable design model for one existing Note. Run with Python 3.10+."""

from collections import Counter, deque
from dataclasses import dataclass, replace
from typing import NamedTuple


class Version(NamedTuple):
    revision: int
    text: str


INITIAL = Version(7, "original")


class Attempt(NamedTuple):
    generation: int
    text: str
    expected: int


class Message(NamedTuple):
    kind: str
    token: int
    data: Attempt | Version | None = None


@dataclass(frozen=True)
class Client:
    local: str = INITIAL.text
    generation: int = 0
    base: Version = INITIAL
    attempt: Attempt | None = None
    phase: str = "idle"
    token: int = 0
    conflict: Version | None = None

    @property
    def state(self):
        if self.phase == "idle":
            return "clean" if self.local == self.base.text else "pending"
        return self.phase

    def save(self, text):
        return replace(self, local=text, generation=self.generation + 1)

    def issue(self, kind):
        token = self.token + 1
        if kind == "get":
            return replace(self, phase="checking", token=token), Message(kind, token)
        attempt = self.attempt or Attempt(self.generation, self.local, self.base.revision)
        return (replace(self, phase="waiting", token=token, attempt=attempt),
                Message(kind, token, attempt))

    def receive(self, msg):
        if self.attempt is None or msg.token != self.token or self.phase == "reconciling":
            return self, None
        if msg.kind == "stale":
            return self.issue("get")
        if msg.kind == "read" and msg.data.revision == self.attempt.expected:
            return self.issue("put")
        if msg.kind == "ok" or msg.data.text == self.attempt.text:
            return replace(self, phase="idle", base=msg.data, attempt=None), None
        return replace(self, phase="reconciling", conflict=msg.data), None


def serve(history, msg):
    """Independent server: GET snapshots its head; PUT uses compare-and-swap."""
    head = history[-1]
    if msg.kind == "get":
        return history, Message("read", msg.token, head)
    if msg.data.expected != head.revision:
        return history, Message("stale", msg.token)
    head = Version(head.revision + 1, msg.data.text)
    return history + (head,), Message("ok", msg.token, head)


@dataclass(frozen=True)
class World:
    client: Client = Client()
    history: tuple = (INITIAL,)
    wire: tuple = ()
    peer_wrote: bool = False


def enqueue(wire, effect):
    return wire if effect is None else wire + (effect,)


def edges(w):
    """Requests and replies may be dropped. Server execution and delivery are separate."""
    c = w.client
    for value in ("A", "B", INITIAL.text):
        if c.local != value:
            yield "save " + value, replace(w, client=c.save(value))
    if not w.peer_wrote:
        for value in ("A", "peer"):
            head = Version(w.history[-1].revision + 1, value)
            yield "peer " + value, replace(w, history=w.history + (head,), peer_wrote=True)
    if len(w.wire) < 2:
        if c.state == "pending":
            client, put = c.issue("put")
            yield "dispatch", replace(w, client=client, wire=enqueue(w.wire, put))
        if c.phase in ("waiting", "checking"):
            # Restart and timeout retain the same durable data. Start a fresh
            # read; a request queued before the restart can still commit.
            client, get = c.issue("get")
            yield "recover", replace(w, client=client, wire=enqueue(w.wire, get))
    for i, msg in enumerate(w.wire):
        rest = w.wire[:i] + w.wire[i + 1:]
        yield f"drop {msg.token}", replace(w, wire=rest)
        if msg.kind in ("put", "get"):
            history, reply = serve(w.history, msg)
            yield f"serve {msg.token}", replace(w, history=history, wire=enqueue(rest, reply))
        else:
            client, effect = c.receive(msg)
            yield f"deliver {msg.token}", replace(w, client=client, wire=enqueue(rest, effect))


def step(w, event):
    for name, after in edges(w):
        if name == event:
            check(w, name, after)
            return after
    raise AssertionError(f"Unavailable event {event} in {w}")


def check(before, event, after):
    """Independent safety checks on every explored transition."""
    c, d = before.client, after.client
    if event.startswith("save "):
        assert d.local == event[5:] and d.generation == c.generation + 1
        assert d.attempt == c.attempt
    else:
        assert (d.local, d.generation) == (c.local, c.generation)
    assert d.base in after.history and d.base.revision >= c.base.revision
    if d.base != c.base:
        assert c.attempt and d.base.text == c.attempt.text
    if event.startswith("deliver "):
        msg = next(m for m in before.wire if m.token == int(event.split()[1]))
        if msg.token != c.token or c.phase in ("idle", "reconciling"):
            assert d == c
    if c.attempt and d.attempt:
        assert d.attempt == c.attempt
    if d.phase == "reconciling":
        assert d.conflict in after.history and d.attempt is not None
    if d.state == "clean" and not after.peer_wrote:
        assert after.history[-1].text == d.local
    if d.state == "clean":
        # A delayed local request must no longer be able to change the head.
        assert all(m.kind != "put" or m.data.expected != after.history[-1].revision
                   for m in after.wire)
    assert (d.phase == "idle") == (d.attempt is None)


def trace(*events):
    w = World()
    for event in events:
        w = step(w, event)
    return w


def scenarios():
    start = ("save A", "dispatch")
    w = trace(*start, "save B", "serve 1", "deliver 1")
    assert (w.client.base, w.client.local, w.client.state) == (Version(8, "A"), "B", "pending")

    w = trace(*start, "serve 1", "drop 1", "save B", "recover", "serve 2", "deliver 2",
              "dispatch")
    assert w.client.attempt == Attempt(2, "B", 8)

    w = trace(*start, "save original", "serve 1", "deliver 1")
    assert w.client.local == INITIAL.text and w.client.state == "pending"

    w = trace(*start, "save B", "serve 1")
    duplicate = w.wire[0]
    w = step(step(w, "deliver 1"), "dispatch")
    w = replace(w, wire=w.wire + (duplicate,))
    assert step(w, "deliver 1").client == w.client

    w = trace(*start, "save original", "recover", "serve 2", "deliver 2", "serve 1",
              "serve 3", "deliver 1", "deliver 3", "serve 4", "deliver 4",
              "dispatch", "serve 5", "deliver 5")
    assert w.client.state == "clean" and w.history[-1] == Version(9, INITIAL.text)


def check_recovery(w, settled):
    """Stop new edits/failures. Deliver oldest messages first, recovering losses."""
    path = set()
    while w not in settled:
        assert w not in path, "Recovery loop after connectivity returned"
        path.add(w)
        if w.wire:
            msg = w.wire[0]
            verb = "serve" if msg.kind in ("put", "get") else "deliver"
            event = f"{verb} {msg.token}"
        elif w.client.state == "pending":
            event = "dispatch"
        elif w.client.state in ("waiting", "checking"):
            event = "recover"
        else:
            assert w.client.state in ("clean", "reconciling")
            break
        assert len(path) <= 32, "Recovery exceeded 32 steps"
        w = step(w, event)
    settled.update(path)


def explore(depth):
    initial = World()
    queue = deque([(initial, 0)])
    parents = {initial: None}
    states = Counter()
    transitions = 0
    while queue:
        w, level = queue.popleft()
        states[w.client.state] += 1
        if level == depth:
            continue
        for event, after in edges(w):
            transitions += 1
            try:
                check(w, event, after)
            except AssertionError:
                path = [event]
                while parents[w] is not None:
                    w, event = parents[w]
                    path.append(event)
                raise AssertionError("Counterexample: " + " -> ".join(reversed(path)))
            if after not in parents:
                parents[after] = (w, event)
                queue.append((after, level + 1))
    assert set(states) == {"clean", "pending", "waiting", "checking", "reconciling"}
    settled = set()
    for w in parents:
        check_recovery(w, settled)
    return len(parents), transitions


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Run without -O so assertions are enabled.")
    scenarios()
    states, steps = explore(10)
    print("Five named scenarios passed.")
    print(f"Depth 10: {states:,} distinct states, {steps:,} transitions; all checks passed.")
    print("Recovery reached clean or reconciliation from every explored state.")
