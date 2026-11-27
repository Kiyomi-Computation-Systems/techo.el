# Task promotion across two Org files

Promotion moves the selected TODO subtree from a Note into the configured capture file and leaves a standard Org ID link at its original position. The two file saves and the SQLite commit can complete separately.

## Recovery protocol tested

1. Prepare the extracted Task and the replacement link using the same UUIDv7. Commit an intent to SQLite containing that ID and enough information to recognize both files before and after the edit. In the probe this is the complete text of each file.
2. Save the Task in the capture file first. A crash here leaves two copies of the prose, both recoverable. An active intent reserves the affected nodes. Production save hooks and reindexing must respect that reservation before dispatching sync work.
3. Replace the original subtree with its link and save the Note. On recovery, compare both files with the intent before making either change. Skip edits already present. Preserve both files for review if either has unexpected text or the link exists while the target is still absent.
4. In one SQLite transaction, enqueue the Task create and Note update and retire the intent. This publishes local sync work. Server delivery, including subsequent graph edge publication, is a separate step.

## Process-crash results

Run `emacs -Q --batch -l .plans/probe-task-promotion.el -f techo-probe-task-promotion` from the repo root. The [probe](probe-task-promotion.el) uses temporary files, Emacs Org subtree operations, SQLite in WAL mode, and SIGKILL in a disposable writer process.

| Writer interruption | Expected recovery | Result |
|---|---|---|
| Before the intent commits | Both original files survive. A new invocation can promote the TODO. | Passed |
| After the intent commits, before either file save | Resume with the recorded ID and complete both saves. | Passed |
| After the Task save, before the Note save | Recognize the Task and replace only the source subtree. | Passed |
| After the Note save, before finalization | Recognize both edits and publish the two sync operations. | Passed |
| During the final SQLite transaction | Partial publication rolls back. Recovery publishes both operations. | Passed |
| After the final commit | Reopening and rescanning the intent table finds completed work. | Passed |
| After the Task save, then again after the Note save during recovery | A second restart completes finalization. | Passed |
| After the Task save, with a subsequent edit to either file | Preserve both files, retain the intent and pause publication for review. | Both cases passed |

The normal path was recovered twice in each fixture. The checks require exactly one extracted Task, one link resolvable by Org ID lookup, one queued create and one queued update. They check the TODO state, tags, deadline, custom property, Unicode body and nested checklist, including relative heading levels after extraction. A second TODO with the same title stays in the source and an unrelated TODO stays in the capture file.

The probe uses one existing, synced Note, a fixed UUIDv7 fixture, a top-level capture target, and serialized file access. Org realigns the heading tags during insertion. The whole-file comparison is deliberately conservative: even appending an unrelated line while Emacs is stopped triggers review.

## Interaction with sync-on-save

The [save-hook probe](probe-promotion-hooks.el) runs actual Emacs `after-save-hook` callbacks and a startup rescan against the promotion fixture. An intent records the source Note ID and target Task ID. Both indexing paths consult this durable reservation before adding work. The dispatcher's selection also excludes reserved IDs because the source Note may already have a queued edit when promotion starts. Keep that edit in the queue. Finalization coalesces the source update, publishes the Task create and releases both IDs in one SQLite transaction.

Run `emacs -Q --batch -l .plans/probe-promotion-hooks.el -f techo-probe-promotion-hooks`. Both cases passed:

- **Resume.** Queue a genuine Note edit, promote, kill the writer after the Task save, and rescan twice before recovery. Neither reserved node becomes eligible through the target or source save hooks. After finalization and two further scans, exactly one Note update and one Task create are eligible.
- **Edit a sibling.** At the same interruption point, save an edit to another managed Task in `todos.org`. Only that sibling becomes eligible. The current whole-file recovery guard sends the interrupted promotion to review and preserves the user's edit. The two reserved nodes stay held.

The dispatcher is represented by a SQLite eligibility query. An actual worker must claim work atomically with its reservation check. An upload already in flight before promotion retains its frozen attempt and needs separate coordination. Live modified buffers, concurrent file writers, power loss and HTTP delivery remain integration cases.
