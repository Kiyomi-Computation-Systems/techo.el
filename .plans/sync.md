# Local storage and sync

## Agreed workflow

Saving writes the Org file locally. A background sync processes changes per node, including individual Task or Contact headings in shared files. Saved edits remain available offline and across Emacs restarts. Users can reconcile a conflict in Emacs and sync the resolved node.

## First sync

A fresh installation downloads every active node in the account, including its full content, so the entire active notebook is available offline. Before creating local files or headings, match existing managed nodes by ID within the [configured directory and capture files](org-format.md#local-files). If a destination filename is already occupied by unrelated content, choose an unused filename and preserve the existing file. On a returning installation, routine pulls respect the missing-local records described below.

The [bootstrap machine](bootstrap.md) covers concurrent server writes during the initial scan, restart, and handoff to the incremental feed. Its high-water cursor contract is a proposal for the server.

## Verification requirement

Model sync state transitions as explicit, verifiable state machines during protocol design. Specify events, guards, effects and invariants. Include failure and restart sequences in verification, such as a lost upload response followed by a newer local save. Acknowledging the earlier upload must leave the newer edit pending.

The [offline Note creation machine](note-create.md) establishes the first agreed server revision. The [existing Note upload machine](note-upload.md) handles subsequent content edits. Each has a transition diagram, an executable model and bounded verification results.

## Incoming changes

The [incremental incoming content machine](incoming.md) models cursor advancement, shared-file deferral and recovery across the SQLite and Org file writes.

Pull remote changes in the background. For a closed file or an unmodified buffer, apply incoming changes to the affected nodes, reconciling any saved local changes against their common base first. In an open buffer, preserve point and outline visibility where possible.

While a buffer has unsaved edits, retain incoming changes for that file in SQLite and defer applying them. This covers every node in a shared file such as `todos.org`. After the user saves or discards the buffer edits, reconcile the waiting changes per node against the latest saved file. Other files continue syncing.

Show a quiet buffer-local indicator such as `Techo ↓2` while incoming changes wait. Count distinct nodes awaiting application in that buffer. Two remote revisions of the same Task count once. Hide the indicator when the queue for that buffer is empty.

## Local removal and server trash

When a managed file or heading disappears, record its node ID as missing locally in SQLite. Preserve that state across restarts, reindexing and routine pulls. The server copy stays intact. If the same ID reappears after a rename or refile, update its location and resume reconciliation. The user can also retrieve a missing copy explicitly with `techo-restore-local-copy`.

Trashing a node on the server requires an explicit `techo-trash` command. The command can select a missing node by title and queue the operation while offline.

If a remote trash meets local edits, preserve the edits and offer two resolutions. The user may untrash the node and sync the reconciled edits, or accept the remote trash. Defer changes to a modified buffer until the user saves or discards its edits.

## Proposed SQLite responsibilities

- Index node IDs, file locations, titles, kinds, parents and references for local lookup and completion.
- Keep the last agreed server representation and revision per node as a three-way merge base. A server move can advance the revision without creating a retrievable content snapshot at that revision.
- Track pending work durably. Distinguish the saved version sent in a request from later local saves. Acknowledging the request advances the base and leaves any later edit pending. Recover changes by rescanning files if Emacs stops between the file save and the SQLite transaction.
- Retain unresolved conflict inputs across restarts. Rebuilding the lookup index must preserve merge bases, pending operations and conflict records.

### Save recovery probe

The [batch Emacs probe](probe-save-recovery.el) saves a real Org Note, kills its disposable writer process with SIGKILL, and reopens the SQLite database. It runs two fixtures at three interruption points. One fixture captures a new Note offline. The other starts with server revision 7 and a frozen upload of draft A, then saves draft B under the same UUID.

| Interruption | Local row on reopening SQLite | After rescanning the Org file |
|---|---|---|
| After file save, before indexing | Previous row, or absent for a new capture | B indexed under the saved UUID |
| After the index update, before transaction commit | Previous row, or absent for a new capture | B indexed under the saved UUID |
| Immediately after commit | B already indexed | Same generation of B |

All six cases passed on Emacs 30.2 with SQLite 3.54.1 in WAL mode. Each case rescanned twice. The existing Note kept revision 7, its base content and the original frozen upload. The new Note kept its ID and had an empty server base. An unrelated Org file carrying an ID stayed outside the index.

Run `emacs -Q --batch -l .plans/probe-save-recovery.el -f techo-probe-save-recovery` from the repo root. The fixture uses whole-file Notes, a minimal body reader and three provisional tables. This checks process termination after a successful save. The [Task promotion probe](task-promotion.md) covers recovery between two file saves. Power loss and interruption during file replacement remain open.

## Reconciling a stale write

On a stale write (HTTP 412), fetch the current node and compare it with the saved base and the local node. If a prior request succeeded but its response was lost, recognize an already-applied change before offering a merge. Other nodes can continue syncing.

Compare each field against the common base. Keep a change made on only one side and accept a value both sides reached independently. Sync compatible changes automatically. For example, keep a remote Project move alongside a local prose edit.

When both sides changed the prose to different values, flag the node for review. The user opens a three-way Ediff session on request and approves the result before it syncs, even when the text edits touch different lines. Present competing changes to a structured field, such as project membership or Task status, as field choices. A resolution updates only the affected node in its Org file. Check for newer local edits, including unsaved buffer changes, before applying it.

Queue the accepted resolution against the remote revision just reviewed. Send content through the content endpoint and use the appropriate commands for moves and transitions, using each successful response's revision for the next operation. If the server advances again, preserve the user's result and reconcile against the newer state. A node is clean once its latest saved version agrees with the server.
