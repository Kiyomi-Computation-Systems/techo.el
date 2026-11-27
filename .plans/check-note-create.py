"""Offline Note creation model. Run with Python 3.10+ from any directory."""

from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from runpy import run_path


NOTE_ID = "019ae000-0000-7000-8000-000000000001"
PROJECT_ID = "019ae000-0000-7000-8000-000000000002"


@dataclass(frozen=True)
class Payload:
    body: str = "A"
    title: str = "Studio"
    parent: str | None = None
    kind: str = "note"
    content_type: str = "text/org"
    props: tuple = ()  # Canonical members for semantic comparison.


@dataclass(frozen=True)
class Node:
    payload: Payload
    revision: int = 1
    state: str = "active"


@dataclass(frozen=True)
class Message:
    kind: str
    token: int
    uid: str
    data: Payload | Node | None = None


@dataclass(frozen=True)
class Client:
    phase: str = "empty"
    uid: str | None = None
    local: Payload | None = None
    frozen: Payload | None = None
    base: Node | None = None
    detail: str | Node | None = None
    serial: int = 0
    live: int | None = None

    def issue(self):
        token = self.serial + 1
        if self.phase == "checking":
            return replace(self, serial=token, live=token), Message("get", token, self.uid)
        frozen = self.frozen or self.local
        client = replace(self, phase="creating", frozen=frozen, serial=token, live=token)
        return client, Message("create", token, self.uid, frozen)

    def receive(self, msg):
        if msg.token != self.live or msg.uid != self.uid:
            return self
        c = replace(self, live=None)
        if msg.kind == "412":
            return replace(c, phase="checking")
        if msg.kind in ("404", "invalid_parent"):
            return replace(c, phase="review", detail=msg.kind)
        node = msg.data
        if node.state == "active" and node.payload == c.frozen:
            return replace(c, phase="established", base=node, frozen=None)
        return replace(c, phase="review", detail=node)


@dataclass(frozen=True)
class Server:
    node: Node | None = None
    reserved: bool = False  # Other account or purged ID.
    parent_valid: bool = True

    def handle(self, msg):
        assert msg.uid == NOTE_ID
        if msg.kind == "get":
            kind = "read" if self.node else "404"
            return self, replace(msg, kind=kind, data=self.node)
        assert msg.kind == "create"  # PUT with If-None-Match: *
        if self.node or self.reserved:
            return self, replace(msg, kind="412", data=None)
        if msg.data.parent and not self.parent_valid:
            return self, replace(msg, kind="invalid_parent", data=None)
        node = Node(msg.data)
        return replace(self, node=node), replace(msg, kind="201", data=node)


@dataclass(frozen=True)
class World:
    client: Client = Client()
    server: Server = Server()
    online: bool = False
    parent_known: bool = False
    wire: tuple = ()
    peer_edited: bool = False


def enabled(w):
    c = w.client
    if c.phase == "empty":
        for label, parent in (("capture", None), ("capture child", PROJECT_ID)):
            yield label, replace(w, client=Client(phase="queued", uid=NOTE_ID,
                                                    local=Payload(parent=parent)))
    elif c.phase in ("queued", "creating", "checking"):
        for body in ("A", "B"):
            if body != c.local.body:
                yield "save " + body, replace(w, client=replace(c, local=replace(c.local, body=body)))
    yield "disconnect" if w.online else "connect", replace(w, online=not w.online)
    if c.local and c.local.parent and not w.parent_known:
        yield "parent ready", replace(w, parent_known=True)
    if c.live is not None:
        # Drop volatile callback ownership. Already queued requests may still commit.
        yield "restart/timeout", replace(w, client=replace(c, live=None))
    eligible = c.phase in ("checking", "creating") or (
        c.phase == "queued" and (c.local.parent is None or w.parent_known))
    if w.online and eligible and c.live is None and len(w.wire) < 2:
        client, msg = c.issue()
        yield "dispatch", replace(w, client=client, wire=w.wire + (msg,))
    if w.server.node and not w.peer_edited:
        head = w.server.node
        node = replace(head, revision=head.revision + 1, payload=replace(head.payload, body="peer"))
        yield "peer edit", replace(w, server=replace(w.server, node=node), peer_edited=True)
    for i, msg in enumerate(w.wire):
        rest = w.wire[:i] + w.wire[i + 1:]
        yield f"drop {msg.token}", replace(w, wire=rest)
        if msg.kind in ("create", "get"):
            server, reply = w.server.handle(msg)
            yield f"serve {msg.token}", replace(w, server=server, wire=rest + (reply,))
        elif w.online:
            yield f"deliver {msg.token}", replace(w, client=c.receive(msg), wire=rest)


def check(w, event, v):
    c, d = w.client, v.client
    if c.uid:
        assert d.uid == c.uid == NOTE_ID
    if event.startswith("capture"):
        assert not c.uid and d.local and not d.base and d.phase == "queued"
    elif event.startswith("save "):
        assert d.local == replace(c.local, body=event[5:])
    else:
        assert d.local == c.local
    if c.frozen and d.phase != "established":
        assert d.frozen == c.frozen
    if event == "dispatch":
        msg = v.wire[-1]
        assert msg.uid == c.uid and msg.token > c.serial
        if msg.kind == "create":
            assert msg.data == (c.frozen or c.local)
            assert not msg.data.parent or w.parent_known
    if event.startswith("serve") and (w.server.node or w.server.reserved):
        assert v.server == w.server  # Replays cannot change any occupied slot.
    if event.startswith("deliver"):
        msg = next(m for m in w.wire if event == f"deliver {m.token}")
        if msg.token != c.live:
            assert c == d
        elif msg.kind == "412":
            assert d.phase == "checking" and d.base is None
        elif msg.kind == "404":
            assert c.phase == "checking" and d.phase == "review"
        if d.phase == "established" and c.phase != d.phase:
            assert msg.kind in ("201", "read") and msg.data == d.base
            assert d.base.payload == c.frozen and d.base.state == "active"
    assert (d.base is not None) == (d.phase == "established")
    if d.phase == "review":
        assert d.detail and d.frozen and d.local and d.live is None


def step(w, event):
    for label, v in enabled(w):
        if label == event:
            check(w, event, v)
            return v
    raise AssertionError(f"Unavailable {event} in {w}")


def trace(*events, server=Server()):
    w = World(server=server)
    for event in events:
        w = step(w, event)
    return w


def check_upload_handoff(c):
    """Feed the established base and latest save to the existing upload machine."""
    upload = run_path(str(Path(__file__).with_name("check-note-upload.py")))
    base = upload["Version"](c.base.revision, c.base.payload.body)
    client = upload["Client"](local=c.local.body, generation=2, base=base)
    world = upload["World"](client=client, history=(base,))
    assert client.state == "pending"
    for event in ("dispatch", "serve 1", "deliver 1"):
        world = upload["step"](world, event)
    assert world.client.state == "clean" and world.history[-1] == (2, "B")


def scenarios():
    start = ("capture", "connect", "dispatch")
    w = trace("capture", "save B", "connect", "dispatch", "serve 1", "deliver 1")
    assert w.client.base.payload.body == "B" and w.client.base.payload == w.client.local

    w = trace(*start, "serve 1", "drop 1", "disconnect", "save B", "restart/timeout",
              "connect", "dispatch", "serve 2", "deliver 2", "dispatch", "serve 3", "deliver 3")
    assert w.client.base == Node(Payload()) and w.client.local.body == "B"
    check_upload_handoff(w.client)

    w = trace(*start, "restart/timeout", "dispatch", "serve 2", "deliver 2", "serve 1", "deliver 1")
    assert w.server.node.revision == 1 and w.client.base == w.server.node

    w = trace(*start, "drop 1", "restart/timeout", "save B", "dispatch", "serve 2", "deliver 2")
    assert w.client.base == Node(Payload()) and w.client.local.body == "B"

    w = trace(*start, "serve 1", "peer edit", "drop 1", "restart/timeout", "dispatch",
              "serve 2", "deliver 2", "dispatch", "serve 3", "deliver 3")
    assert w.client.phase == "review" and w.client.detail.revision == 2

    occupied = (*start, "serve 1", "deliver 1", "dispatch", "serve 2", "deliver 2")
    w = trace(*occupied, server=Server(node=Node(Payload(), 3)))
    assert w.client.base == Node(Payload(), 3)
    w = trace(*occupied, server=Server(reserved=True))
    assert w.client.detail == "404" and w.server.reserved

    for other in (Node(Payload(title="Other")), Node(Payload(parent=PROJECT_ID)),
                  Node(Payload(kind="task")), Node(Payload(content_type="text/plain")),
                  Node(Payload(props=(("at", "2026-01-02"),))), Node(Payload(), 2, "trashed")):
        w = trace(*occupied, server=Server(node=other))
        assert w.client.phase == "review" and w.client.detail == other

    w = trace("capture child", "connect")
    assert "dispatch" not in dict(enabled(w))
    for event in ("parent ready", "dispatch", "serve 1", "deliver 1"):
        w = step(w, event)
    assert w.client.base.payload.parent == PROJECT_ID

    w = trace("capture child", "connect", "parent ready", "dispatch", "serve 1", "deliver 1",
              server=Server(parent_valid=False))
    assert w.client.detail == "invalid_parent" and w.server.node is None


def settle(w, known):
    """Restore connectivity and parent availability, then drain FIFO without new edits."""
    path = set()
    while w not in known:
        assert w not in path and len(path) < 32, "Recovery failed to terminate"
        path.add(w)
        c = w.client
        if not w.online:
            event = "connect"
        elif w.wire:
            msg = w.wire[0]
            verb = "serve" if msg.kind in ("get", "create") else "deliver"
            event = f"{verb} {msg.token}"
        elif c.phase in ("empty", "established", "review"):
            break
        elif c.live is not None:
            event = "restart/timeout"
        elif c.phase == "queued" and c.local.parent and not w.parent_known:
            event = "parent ready"
        else:
            event = "dispatch"
        w = step(w, event)
    known.update(path)


def explore(depth):
    seeds = [World(server=s) for s in (Server(), Server(reserved=True),
             Server(node=Node(Payload())), Server(node=Node(Payload(title="Other"))),
             Server(parent_valid=False))]
    paths = {w: None for w in seeds}
    queue = deque((w, 0) for w in seeds)
    transitions = 0
    while queue:
        w, level = queue.popleft()
        if level == depth:
            continue
        for event, v in enabled(w):
            transitions += 1
            try:
                check(w, event, v)
            except AssertionError:
                path = [event]
                while paths[w]:
                    w, event = paths[w]
                    path.append(event)
                raise AssertionError("Counterexample: " + " -> ".join(reversed(path)))
            if v not in paths:
                paths[v] = (w, event)
                queue.append((v, level + 1))
    known = set()
    for w in paths:
        settle(w, known)
    return len(paths), transitions


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("Run without -O so assertions are enabled.")
    scenarios()
    states, transitions = explore(12)
    print("Offline capture, replay, conflict, identity and parent scenarios passed.")
    print(f"Depth 12: {states:,} states, {transitions:,} transitions; FIFO recovery passed.")
