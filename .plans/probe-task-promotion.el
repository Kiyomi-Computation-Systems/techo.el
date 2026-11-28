;;; probe-task-promotion.el --- Test crash recovery for Org task extraction -*- lexical-binding: t; -*-

(require 'org-id)
(defconst techo-promo-source (or load-file-name buffer-file-name))
(load (expand-file-name "probe-save-recovery.el" (file-name-directory techo-promo-source)) nil t)

;; Fixed UUIDv7 fixture for the single allocation performed at intent creation.
(defconst techo-promo-id "019ae000-0000-7000-8000-000000000002")
(defconst techo-promo-link (format "[[id:%s][Send proofs]]" techo-promo-id))
(defconst techo-promo-prefix
  (concat ":PROPERTIES:\n:ID: " techo-probe-id "\n:TECHO_KIND: note\n:END:\n"
          "#+title: Meeting\n\n* Work\nOpening remarks.\n"))
(defconst techo-promo-subtree
  "** TODO Send proofs :press:\nDEADLINE: <2026-12-01 Tue>\n:PROPERTIES:\n:CLIENT: Maya\n:END:\nUse crème stock.\n*** Samples\n- [ ] Blue sheet\n")
(defconst techo-promo-tail "** TODO Send proofs\nDifferent job, same title.\n")
(defconst techo-promo-todos "#+title: TODOs\n* TODO Pay printer\nKeep this local.\n")
(defconst techo-promo-intent-schema
  "CREATE TABLE intent(id TEXT PRIMARY KEY, source_id TEXT, src0 TEXT, src1 TEXT,
                       entry TEXT, capture_started INTEGER NOT NULL DEFAULT 0)")

(defun techo-promo-entries (text id)
  "Return the Org heading subtrees in TEXT with the direct ID property ID."
  (with-temp-buffer
    (insert text) (org-mode)
    (org-element-map (org-element-parse-buffer) 'headline
      (lambda (heading)
        (goto-char (org-element-property :begin heading))
        (when (equal (org-entry-get nil "ID") id)
          (buffer-substring-no-properties (point) (save-excursion (org-end-of-subtree t t))))))))

(defun techo-promo-text (file)
  (with-temp-buffer (insert-file-contents file) (buffer-string)))

(defun techo-promo-save (file text)
  (let ((make-backup-files nil)
        (create-lockfiles nil)
        (buffer (find-file-noselect file)))
    (unwind-protect
        (with-current-buffer buffer
          (erase-buffer) (insert text) (save-buffer))
      (kill-buffer buffer))))

(defun techo-promo-prepare (db dir cut)
  (let* ((before (techo-promo-text (expand-file-name "meeting.org" dir)))
         (todos (techo-promo-text (expand-file-name "todos.org" dir)))
         source-after target-after subtree)
    (with-temp-buffer
      (insert before) (org-mode) (goto-char (point-min))
      ;; Fixture selects the tagged heading. The real command starts at point.
      (re-search-forward "^\\*\\* TODO Send proofs :press:")
      (beginning-of-line)
      (let ((start (point)))
        (org-end-of-subtree t t)
        (setq subtree (delete-and-extract-region start (point)))
        (insert techo-promo-link "\n")
        (setq source-after (buffer-string))))
    (with-temp-buffer
      (insert todos) (org-mode) (goto-char (point-max))
      (let ((start (point)))
        (org-paste-subtree 1 subtree)
        (goto-char start)
        (org-entry-put nil "ID" techo-promo-id)
        (org-entry-put nil "TECHO_KIND" "task"))
      (setq target-after (buffer-string)))
    (let ((entries (techo-promo-entries target-after techo-promo-id)))
      (techo-probe-check (= 1 (length entries)) "Ambiguous target ID")
      (with-sqlite-transaction db
        (sqlite-execute db "INSERT INTO intent(id,source_id,src0,src1,entry) VALUES (?,?,?,?,?)"
                        (list techo-promo-id techo-probe-id before source-after (car entries)))
        (techo-probe-cut dir cut 'before-intent-commit)))
    (techo-probe-cut dir cut 'intent-committed)))

(defun techo-promo-state (text before after)
  (cond ((equal text before) 'before)
        ((equal text after) 'after)
        (t 'diverged)))

(defun techo-promo-apply (db dir &optional cut)
  (let ((row (car (sqlite-select db "SELECT id,source_id,src0,src1,entry,capture_started FROM intent"))))
    (when row
      (pcase-let* ((`(,id ,source-id ,src0 ,src1 ,entry ,started) row)
                   (src (expand-file-name "meeting.org" dir))
                   (dst (expand-file-name "todos.org" dir))
                   (from (techo-promo-state (techo-promo-text src) src0 src1))
                   (current (techo-promo-text dst))
                   (matches (techo-promo-entries current id))
                   (to (cond ((and (= started 1) (equal matches (list entry))) 'after)
                             ((and (zerop started) (null matches)) 'before)
                             (t 'diverged))))
        (if (or (eq from 'diverged) (eq to 'diverged)
                (and (eq from 'after) (eq to 'before)))
            'review
          (when (eq to 'before)
            ;; After this durable marker, absence could mean a user's deletion.
            (sqlite-execute db "UPDATE intent SET capture_started=1 WHERE id=?" (list id))
            (techo-probe-cut dir cut 'before-task-write)
            (techo-promo-save dst (concat current
                                          (unless (or (string-empty-p current)
                                                      (string-suffix-p "\n" current)) "\n")
                                          entry)))
          (techo-probe-cut dir cut 'task-saved)
          (when (eq from 'before) (techo-promo-save src src1))
          (techo-probe-cut dir cut 'link-saved)
          (with-sqlite-transaction db
            (sqlite-execute db "INSERT INTO pending VALUES (?, 'create')" (list id))
            ;; A save before promotion may already have queued the source update.
            (sqlite-execute db "INSERT INTO pending VALUES (?, 'update')
                                ON CONFLICT(id) DO UPDATE SET op=excluded.op" (list source-id))
            (sqlite-execute db "DELETE FROM intent WHERE id=?" (list id))
            (techo-probe-cut dir cut 'before-final-commit))
          (techo-probe-cut dir cut 'final-committed)
          'complete)))))

(defun techo-promo-child (dir cut)
  (let ((db (techo-probe-open dir)))
    (unless (car (sqlite-select db "SELECT id FROM intent"))
      (techo-promo-prepare db dir cut))
    (techo-promo-apply db dir cut)))

(defun techo-promo-interrupt (dir cut)
  (techo-probe-run-to-cut techo-promo-source (list 'techo-promo-child dir (list 'quote cut))
                          dir cut))

(defun techo-promo-verify (db dir &optional todos-prefix)
  (let ((file (expand-file-name "todos.org" dir)))
    (setq todos-prefix (or todos-prefix techo-promo-todos))
    (techo-probe-check (equal (techo-promo-text (expand-file-name "meeting.org" dir))
                              (concat techo-promo-prefix techo-promo-link "\n" techo-promo-tail))
                       "Incorrect source replacement")
    (with-temp-buffer
      (insert-file-contents file) (org-mode) (goto-char (point-min))
      (techo-probe-check (looking-at-p (regexp-quote todos-prefix)) "Unrelated Task changed")
      (goto-char (+ (point-min) (length todos-prefix)))
      (techo-probe-check (and (= (org-outline-level) 1)
                              (equal (org-get-todo-state) "TODO")
                              (equal (org-get-heading t t t t) "Send proofs")
                              (equal (org-get-tags nil t) '("press")))
                         "Heading or tags changed")
      (techo-probe-check (equal (org-entry-get nil "ID") techo-promo-id) "Wrong target ID")
      (techo-probe-check (equal (org-entry-get nil "TECHO_KIND") "task") "Wrong kind")
      ;; Remove just the two new properties and compare the entire extracted subtree.
      (org-entry-delete nil "ID")
      (org-entry-delete nil "TECHO_KIND")
      (beginning-of-line)
      (forward-line)
      (let ((actual (buffer-substring-no-properties (point) (point-max)))
            (expected (replace-regexp-in-string "^\\*" ""
                                                (substring techo-promo-subtree
                                                           (1+ (string-match "\n" techo-promo-subtree))))))
        (techo-probe-check (equal actual expected) "Target subtree mismatch: %S" actual)))
    ;; Resolve the actual link path through Org's ID lookup in the destination.
    (with-temp-buffer
      (insert-file-contents (expand-file-name "meeting.org" dir)) (org-mode)
      (let* ((links (org-element-map (org-element-parse-buffer) 'link #'identity))
             (link (car links))
             (marker (org-id-find-id-in-file (org-element-property :path link) file 'marker)))
        (techo-probe-check (and (= 1 (length links)) (equal (org-element-property :type link) "id")
                                (markerp marker)
                                (equal file (buffer-file-name (marker-buffer marker))))
                           "Source link did not resolve to the extracted Task")
        (kill-buffer (marker-buffer marker)))))
  (techo-probe-check (equal (sqlite-select db "SELECT id,op FROM pending ORDER BY op")
                            (list (list techo-promo-id "create") (list techo-probe-id "update")))
                     "Unexpected pending work")
  (techo-probe-check (null (sqlite-select db "SELECT * FROM intent")) "Intent still present"))

(defun techo-promo-case (cut &optional variant)
  (let* ((dir (make-temp-file "techo-promotion-" t))
         (src (expand-file-name "meeting.org" dir))
         (dst (expand-file-name "todos.org" dir))
         (original (concat techo-promo-prefix techo-promo-subtree techo-promo-tail))
         db todos-prefix)
    (unwind-protect
        (progn
          (techo-promo-save src original)
          (techo-promo-save dst techo-promo-todos)
          (setq db (techo-probe-open dir))
          (sqlite-select db "PRAGMA journal_mode=WAL")
          (sqlite-execute db techo-promo-intent-schema)
          (sqlite-execute db "CREATE TABLE pending(id TEXT PRIMARY KEY, op TEXT)")
          (sqlite-close db) (setq db nil)
          (techo-promo-interrupt dir cut)
          (when (eq variant 'second-crash) (techo-promo-interrupt dir 'link-saved))
          (setq db (techo-probe-open dir))
          (let ((linked (or (memq cut '(link-saved before-final-commit final-committed))
                            (eq variant 'second-crash)))
                (copied (memq cut '(task-saved link-saved before-final-commit final-committed))))
            (techo-probe-check (equal (techo-promo-text src)
                                      (if linked (concat techo-promo-prefix techo-promo-link "\n"
                                                         techo-promo-tail) original))
                               "Unexpected source state at %s" cut)
            (techo-probe-check (eq (null copied) (equal (techo-promo-text dst) techo-promo-todos))
                               "Unexpected capture file at %s" cut))
          (unless (eq cut 'final-committed)
            (techo-probe-check (null (sqlite-select db "SELECT * FROM pending")) "Partial publication"))
          (when (eq cut 'before-intent-commit)
            (techo-probe-check (and (equal (techo-promo-text src) original)
                                    (equal (techo-promo-text dst) techo-promo-todos)
                                    (null (sqlite-select db "SELECT * FROM intent")))
                               "Uncommitted intent changed files")
            ;; The user's command did not commit. Retry it explicitly.
            (techo-promo-prepare db dir nil))
          (pcase variant
            ('edit-source (append-to-file "New user edit.\n" nil src))
            ('edit-target (append-to-file "New user edit.\n" nil dst))
            ('remove-target (techo-promo-save dst techo-promo-todos))
            ('duplicate-target
             (let ((entry (car (techo-promo-entries (techo-promo-text dst) techo-promo-id))))
               (append-to-file entry nil dst)))
            ('edit-sibling
             (setq todos-prefix (string-replace "Pay printer" "Pay studio" techo-promo-todos))
             (techo-promo-save dst (string-replace techo-promo-todos todos-prefix (techo-promo-text dst)))))
          (if (or (eq cut 'before-task-write)
                  (memq variant '(edit-source edit-target remove-target duplicate-target)))
                (let ((left (techo-promo-text src)) (right (techo-promo-text dst)))
                  (dotimes (_ 2)
                    (techo-probe-check (eq 'review (techo-promo-apply db dir)) "Expected review")
                    (techo-probe-check (and (equal left (techo-promo-text src))
                                            (equal right (techo-promo-text dst))
                                            (sqlite-select db "SELECT id FROM intent")
                                            (null (sqlite-select db "SELECT * FROM pending")))
                                       "Divergent content or intent lost")))
            (dotimes (_ 2)
              (techo-promo-apply db dir)
              (techo-promo-verify db dir todos-prefix)))
          (princ (format "PASS %s%s\n" cut (if variant (format " / %s" variant) ""))))
      (when db (sqlite-close db))
      (delete-directory dir t))))

(defun techo-probe-task-promotion ()
  (dolist (cut '(before-intent-commit intent-committed before-task-write task-saved link-saved before-final-commit final-committed))
    (techo-promo-case cut))
  (dolist (variant '(second-crash edit-source edit-target remove-target duplicate-target edit-sibling))
    (techo-promo-case 'task-saved variant))
  (techo-promo-case 'intent-committed 'edit-sibling))

;;; probe-task-promotion.el ends here
