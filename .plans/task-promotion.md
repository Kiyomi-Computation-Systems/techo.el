# Task promotion across two Org files

Promotion moves the selected TODO subtree from a Note into the configured capture file and leaves a standard Org ID link at its original position. The two file saves and the SQLite commit can complete separately.

## Recovery protocol tested

1. Prepare the extracted Task and the replacement link using the same UUIDv7. Commit an intent to SQLite containing the source Note ID, the before/after source text and the exact promoted Task subtree with its assigned ID.
2. Before the first write to the capture file, persist a marker that the write is starting. Append the prepared Task to the current capture text. A crash here leaves two copies of the prose, both recoverable. An active intent reserves the affected nodes. Production save hooks and reindexing must respect that reservation before dispatching sync work.
3. Replace the original subtree with its link and save the Note. On recovery, compare the source Note with the prepared versions and locate the target by its direct Org ID. A single matching subtree completes the destination step even if other headings have changed. Preserve both files for review if the Task differs, the ID is duplicated, or it is absent after the write-start marker. Once that marker commits, a missing Task could be an intentional deletion. This includes the narrow crash window before the first file write.
4. In one SQLite transaction, enqueue the Task create and Note update and retire the intent. This publishes local sync work. Server delivery, including subsequent graph edge publication, is a separate step.

## Process-crash results

Run `emacs -Q --batch -l .plans/probe-task-promotion.el -f techo-probe-task-promotion` from the repo root. The [probe](probe-task-promotion.el) uses temporary files, Emacs Org subtree operations, SQLite in WAL mode, and SIGKILL in a disposable writer process.

| Writer interruption | Expected recovery | Result |
|---|---|---|
| Before the intent commits | Both original files survive. A new invocation can promote the TODO. | Passed |
| After the intent commits, before either file save | Resume with the recorded ID and complete both saves. | Passed |
| After the write-start marker, before the Task save | An absent target enters review. Both files and the intent survive. | Passed |
| After the Task save, before the Note save | Recognize the Task and replace only the source subtree. | Passed |
| After the Note save, before finalization | Recognize both edits and publish the two sync operations. | Passed |
| During the final SQLite transaction | Partial publication rolls back. Recovery publishes both operations. | Passed |
| After the final commit | Reopening and rescanning the intent table finds completed work. | Passed |
| After the Task save, then again after the Note save during recovery | A second restart completes finalization. | Passed |
| After the Task save, with an edit to the source Note or the promoted subtree | Preserve both files, retain the intent and pause publication for review. | Both cases passed |
| After the Task save, with the target deleted or its ID duplicated | Preserve the current capture file and source TODO. Require review. | Both cases passed |
| After the intent commit or after the Task save, with an edit to a sibling heading | Preserve the sibling edit and finish with one Task and one working link. | Both cases passed |

The normal path was recovered twice in each fixture. The checks require exactly one extracted Task, one link resolvable by Org ID lookup, one queued create and one queued update. They check the TODO state, tags, deadline, custom property, Unicode body and nested checklist, including relative heading levels after extraction. A second TODO with the same title stays in the source and an unrelated TODO stays in the capture file.

The probe uses one existing, synced Note, a fixed UUIDv7 fixture, a top-level capture target, and serialized file access. Org realigns the heading tags during insertion. The capture-side check is per Task. The source still uses the whole Note for comparison because the unpromoted TODO may lack a stable heading ID and a second TODO can share its title.

## Interaction with sync-on-save

The [save-hook probe](probe-promotion-hooks.el) runs actual Emacs `after-save-hook` callbacks and a startup rescan against the promotion fixture. An intent records the source Note ID and target Task ID. Both indexing paths consult this durable reservation before adding work. The dispatcher's selection also excludes reserved IDs because the source Note may already have a queued edit when promotion starts. Keep that edit in the queue. Finalization coalesces the source update, publishes the Task create and releases both IDs in one SQLite transaction.

Run `emacs -Q --batch -l .plans/probe-promotion-hooks.el -f techo-probe-promotion-hooks`. Both cases passed:

- **Resume.** Queue a genuine Note edit, promote, kill the writer after the Task save, and rescan twice before recovery. Neither reserved node becomes eligible through the target or source save hooks. After finalization and two further scans, exactly one Note update and one Task create are eligible.
- **Edit a sibling.** At the same interruption point, save an edit to another managed Task in `todos.org`. Only that sibling becomes eligible. Recovery finds the original target by ID, leaves the capture file byte-for-byte intact, and finishes the source link. After repeated startup scans, exactly three operations are eligible: one Note update, one promoted Task create and one sibling Task update.

The dispatcher is represented by a SQLite eligibility query. An actual worker must claim work atomically with its reservation check. An upload already in flight before promotion retains its frozen attempt and needs separate coordination. Live modified buffers, concurrent file writers, power loss and HTTP delivery remain integration cases.
