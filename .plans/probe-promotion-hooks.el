;;; probe-promotion-hooks.el --- Exercise promotion with save hooks -*- lexical-binding: t; -*-

(defconst techo-hooks-source (or load-file-name buffer-file-name))
(load (expand-file-name "probe-task-promotion.el" (file-name-directory techo-hooks-source)) nil t)

(defconst techo-hooks-sibling "019ae000-0000-7000-8000-000000000003")
(defconst techo-hooks-todos
  (concat techo-promo-todos "* TODO Bind edition\n:PROPERTIES:\n:ID: " techo-hooks-sibling
          "\n:TECHO_KIND: task\n:END:\nSmyth sewn.\n"))

(defun techo-hooks-observe (consume)
  ;; Both the save hook and the startup scan use direct (non-inherited) markers.
  (org-with-wide-buffer
   (goto-char (point-min))
   (if (equal (org-entry-get nil "TECHO_KIND") "note")
       (funcall consume (org-entry-get nil "ID")
                (buffer-substring-no-properties (point-min) (point-max)))
     (org-map-entries
      (lambda ()
        (when (equal (org-entry-get nil "TECHO_KIND") "task")
          (funcall consume (org-entry-get nil "ID")
                   (buffer-substring-no-properties
                    (line-beginning-position) (save-excursion (org-end-of-subtree t t))))))))))

(defun techo-hooks-index (db)
  (with-sqlite-transaction db
    (techo-hooks-observe
     (lambda (id content)
       (let* ((row (car (sqlite-select db "SELECT digest FROM base WHERE id=?" (list id))))
              (op (if row "update" "create")))
         (unless (equal (car row) (secure-hash 'sha256 content))
           (sqlite-execute db "INSERT INTO pending(id,op) SELECT ?,?
                               WHERE NOT EXISTS (SELECT 1 FROM reserved WHERE id=?)
                               ON CONFLICT(id) DO UPDATE SET op=excluded.op"
                           (list id op id))))))))

(defun techo-hooks-save (db)
  (when (and buffer-file-name (derived-mode-p 'org-mode))
    (techo-hooks-index db)
    ;; Observe what a dispatcher could claim immediately after this save hook.
    (sqlite-execute db "INSERT INTO hook_log(file,eligible) VALUES
                        (?, (SELECT count(*) FROM eligible WHERE id IN (?,?)))"
                    (list (file-name-nondirectory buffer-file-name) techo-probe-id techo-promo-id))))

(defun techo-hooks-read-file (file function)
  (with-temp-buffer
    (insert-file-contents file)
    (org-mode)
    (funcall function)))

(defun techo-hooks-count (db sql)
  (caar (sqlite-select db sql)))

(defun techo-hooks-child (dir)
  (let* ((db (techo-probe-open dir))
         (after-save-hook (list (lambda () (techo-hooks-save db)))))
    (techo-promo-prepare db dir nil)
    (techo-promo-apply db dir 'task-saved)))

(defun techo-hooks-eligible (db)
  (mapcar #'car (sqlite-select db "SELECT id FROM eligible ORDER BY id")))

(defun techo-hooks-case (variant)
  (let* ((dir (make-temp-file "techo-promotion-hooks-" t))
         (src (expand-file-name "meeting.org" dir))
         (dst (expand-file-name "todos.org" dir))
         (files (list src dst))
         db)
    (unwind-protect
        (progn
          (techo-promo-save src (concat techo-promo-prefix techo-promo-subtree techo-promo-tail))
          (techo-promo-save dst techo-hooks-todos)
          (setq db (techo-probe-open dir))
          (sqlite-select db "PRAGMA journal_mode=WAL")
          (sqlite-execute db techo-promo-intent-schema)
          (dolist (sql '("CREATE TABLE base(id TEXT PRIMARY KEY, digest TEXT)"
                         "CREATE TABLE pending(id TEXT PRIMARY KEY, op TEXT)"
                         "CREATE TABLE hook_log(file TEXT, eligible INTEGER)"
                         "CREATE VIEW reserved AS SELECT id FROM intent UNION SELECT source_id FROM intent"
                         "CREATE VIEW eligible AS SELECT * FROM pending WHERE id NOT IN (SELECT id FROM reserved)"))
            (sqlite-execute db sql))
          (dolist (f files)
            (techo-hooks-read-file
             f (lambda () (techo-hooks-observe
                      (lambda (id content)
                        (sqlite-execute db "INSERT INTO base VALUES (?,?)"
                                        (list id (secure-hash 'sha256 content))))))))
          ;; Start with a genuine unsent source edit before promotion.
          (let ((after-save-hook (list (lambda () (techo-hooks-save db)))))
            (techo-promo-save src (concat (techo-promo-text src) "More notes.\n")))
          (techo-probe-check (equal (techo-hooks-eligible db) (list techo-probe-id))
                             "The pre-existing Note upload was never queued")
          (sqlite-execute db "DELETE FROM hook_log")
          (sqlite-close db)
          (setq db nil)
          (techo-probe-run-to-cut techo-hooks-source (list 'techo-hooks-child dir) dir 'task-saved)
          (setq db (techo-probe-open dir))
          (techo-probe-check
           (equal (sqlite-select db "SELECT file,eligible FROM hook_log") '(("todos.org" 0)))
           "Target hook did not run, or saw dispatchable promotion work")
          (let ((after-save-hook (list (lambda () (techo-hooks-save db)))))
            (dotimes (_ 2)
              (dolist (f files)
                (techo-hooks-read-file f (lambda () (techo-hooks-index db)))))
            (techo-probe-check (and (null (techo-hooks-eligible db))
                                    (equal (sqlite-select db "SELECT id FROM pending")
                                           (list (list techo-probe-id))))
                               "Restart scan published work or erased the queued source")
            (when (eq variant 'sibling)
              (techo-promo-save dst (string-replace "Smyth sewn." "Hand sewn." (techo-promo-text dst)))
              (techo-probe-check (equal (techo-hooks-eligible db) (list techo-hooks-sibling))
                                 "An unrelated saved Task should be dispatchable"))
            (let ((capture-before (techo-promo-text dst)))
              (techo-probe-check (eq 'complete (techo-promo-apply db dir)) "Recovery failed")
              (techo-probe-check (equal capture-before (techo-promo-text dst))
                                 "Recovery rewrote the capture file"))
            (techo-probe-check
             (equal (sqlite-select db "SELECT file,eligible FROM hook_log ORDER BY rowid")
                    (append '(("todos.org" 0)) (when (eq variant 'sibling) '(("todos.org" 0)))
                            '(("meeting.org" 0)))) "Save hooks saw partial publication")
            (dotimes (_ 2)
              (dolist (f files)
                (techo-hooks-read-file f (lambda () (techo-hooks-index db)))))
            (techo-probe-check
             (equal (sqlite-select db "SELECT id,op FROM eligible ORDER BY id")
                    (append (list (list techo-probe-id "update") (list techo-promo-id "create"))
                            (when (eq variant 'sibling) (list (list techo-hooks-sibling "update")))))
             "Unexpected eligible operations"))
          (princ (format "PASS promotion hooks / %s\n" variant)))
      (when db (sqlite-close db))
      (delete-directory dir t))))

(defun techo-probe-promotion-hooks ()
  (techo-hooks-case 'resume)
  (techo-hooks-case 'sibling))

;;; probe-promotion-hooks.el ends here
