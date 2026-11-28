# Org file format and editing

Working notes from the design discussion, 2026-11-27.

## Local files

Each installation has one configurable Techo directory, defaulting to `techo/` under `org-directory`. Note, Project and Journal files live in that tree. Explicitly configured capture files, such as an existing `todos.org` or `contacts.org`, may live outside it. These paths are local to each installation.

Discovery is limited to this tree and the named capture files. Capture or enrollment marks a participating file or heading with a Techo kind and node ID. Unmarked files and headings remain ordinary Org content. Syncing a heading in a shared file preserves the surrounding content.

## Notes and links

Each Note is an individual Org file. Its headings organize the text. A Note can belong to one Project or Journal and reference other nodes, including Notes and Contacts. A reference can cross project boundaries.

## Completing a reference

In a Techo buffer, typing `[[id:` makes node completion available at point. The text after the colon is a title query. For example, completing `[[id:may` and choosing Maya Chen inserts an ordinary Org link:

```org
[[id:019ae000-0000-7000-8000-000000000003][Maya Chen]]
```

The inline completion function and interactive link command use the same local node index. Candidates retain their IDs and show enough context to distinguish duplicate titles. Manual completion works immediately after the colon. Company can use its CAPF backend and the user's usual automatic-popup settings.

## Working proposal for project membership

Assign the parent from inside the note using title completion. One possible spelling in the file-level property drawer is:

```org
:TECHO_PARENT: [[id:019ae000-0000-7000-8000-000000000002][Winter edition]]
```

With this spelling, the property supplies the parent ID. Body links supply references. Moving the file between local directories preserves its parent. The exact property representation and the server protocol for reference edges remain open.

## Projects and Journals

Each Project or Journal has its own Org file. Start with a title and a brief description, then add items from the collection view. The title and description are editable. The member list is derived from the local index using each item's parent ID, so filing an item from its own buffer updates the same list.

- A Project groups its direct members by kind. It can contain Notes, Tasks, Contacts, Journals and other Projects.
- A Journal lists its Notes newest first by entry date (`at`). Capture defaults the entry date to today's local date and allows backdating. Notes without an entry date appear in an **Undated** group at the bottom.

## Promoting a TODO

Promotion extracts a TODO from a Note into a separately stored Task and leaves an Org `id:` link in its place. The Task owns the editable heading, planning lines and subtree text. An ordinary TODO in a Note stays part of that Note until promoted.

Promotion uses the user's configured Org task capture target, such as `todos.org`. One file can hold Tasks from several Projects. Direct capture and promotion produce the same heading format. Each participating heading carries its own `ID` (a UUIDv7) and `TECHO_KIND` set to `task`. It syncs as one Task. An ordinary TODO at the same target remains local unless explicitly enrolled.

The [two-file promotion probe](task-promotion.md) tests extraction, link resolution and recovery after interruption between saves.

## Contacts

Accepted 2026-11-27.

Each Contact has one heading in `contacts.org`. The heading supplies the display name, and the body holds ordinary Org prose. A folded block stores the structured Contact props locally. Capture and routine edits use a form that preserves entry IDs and any fields it does not expose.

Expanding the heading immediately shows the preferred email address and phone number on one line above the prose. Omit missing fields. For email and phone separately, select the lowest explicit `pref`. Use stable entry ID order to break ties or select an entry when none has a preference. Derive the summary from the local card for display only, including while offline.
