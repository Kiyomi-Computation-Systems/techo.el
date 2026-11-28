;;; probe-save-recovery.el --- Org and SQLite process-crash probe -*- lexical-binding: t; -*-

(require 'cl-lib)
(require 'org)
(require 'sqlite)

(defconst techo-probe-source (or load-file-name buffer-file-name))
(defconst techo-probe-id "019ae000-0000-7000-8000-000000000001")

(defun techo-probe-check (condition format-string &rest args)
  (unless condition (error "%s" (apply #'format format-string args))))

(defun techo-probe-open (dir)
  (let ((db (sqlite-open (expand-file-name "index.db" dir))))
    (sqlite-execute db "PRAGMA synchronous=FULL")
    db))

(defun techo-probe-init (db)
  (sqlite-select db "PRAGMA journal_mode=WAL")
  (dolist (sql '("CREATE TABLE local (id TEXT PRIMARY KEY, generation INTEGER, body TEXT)"
                 "CREATE TABLE base (id TEXT PRIMARY KEY, revision INTEGER, body TEXT)"
                 "CREATE TABLE attempt (id TEXT PRIMARY KEY, expected INTEGER, generation INTEGER, body TEXT)"))
    (sqlite-execute db sql)))

(defun techo-probe-read-note (file)
  ;; Minimal reader for the fixture's file drawer, title and body.
  (with-temp-buffer
    (insert-file-contents file)
    (org-mode)
    (goto-char (point-min))
    (when (equal "note" (org-entry-get nil "TECHO_KIND"))
      (let ((id (org-entry-get nil "ID")))
        (re-search-forward "^#\\+title:.*\n\n")
        (list id (buffer-substring-no-properties (point) (point-max)))))))

(defun techo-probe-scan (db dir)
  (dolist (file (directory-files dir t "\\.org\\'"))
    (when-let* ((row (techo-probe-read-note file)))
      (sqlite-execute db
                      "INSERT INTO local VALUES (?,1,?)
                       ON CONFLICT(id) DO UPDATE SET
                         body=excluded.body, generation=local.generation+1
                       WHERE local.body <> excluded.body"
                      row))))

(defun techo-probe-write (dir body)
  (let ((buffer (find-file-noselect (expand-file-name "note.org" dir))))
    (with-current-buffer buffer
      (erase-buffer)
      (insert ":PROPERTIES:\n:ID: " techo-probe-id
              "\n:TECHO_KIND: note\n:END:\n#+title: Study\n\n" body)
      (save-buffer))
    (kill-buffer buffer)))

(defun techo-probe-cut (dir selected point)
  (when (eq selected point)
    (with-temp-file (expand-file-name "checkpoint" dir) (insert (symbol-name point)))
    ;; The driver starts a disposable child and checks the named checkpoint.
    (signal-process (emacs-pid) 9)
    (error "Child survived SIGKILL")))

(defun techo-probe-run-to-cut (source form dir cut)
  (with-temp-buffer
    (let ((status (call-process (expand-file-name invocation-name invocation-directory)
                                nil (list (current-buffer) t) nil "-Q" "--batch" "-l" source
                                "--eval" (prin1-to-string form))))
      (techo-probe-check (stringp status) "Child failed to reach signal: %S %s" status (buffer-string))))
  (techo-probe-check (equal (symbol-name cut)
                            (with-temp-buffer
                              (insert-file-contents (expand-file-name "checkpoint" dir))
                              (buffer-string))) "Wrong checkpoint"))

(defun techo-probe-child (dir fixture cut)
  (let ((db (techo-probe-open dir))
        (make-backup-files nil)
        (create-lockfiles nil))
    (techo-probe-init db)
    (with-temp-file (expand-file-name "unmanaged.org" dir)
      (insert ":PROPERTIES:\n:ID: 019ae000-0000-7000-8000-000000000099\n:END:\n#+title: Private\n\nlocal only\n"))
    (when (eq fixture 'existing)
      (techo-probe-write dir "draft A\n")
      (techo-probe-scan db dir)
      (sqlite-execute db "INSERT INTO base VALUES (?,7,'original')" (list techo-probe-id))
      (sqlite-execute db "INSERT INTO attempt SELECT id,7,generation,body FROM local"))
    (techo-probe-write dir "draft B\n")
    (techo-probe-cut dir cut 'after-save)
    (with-sqlite-transaction db
      (techo-probe-scan db dir)
      (techo-probe-cut dir cut 'before-commit))
    (techo-probe-cut dir cut 'after-commit)))

(defun techo-probe-case (fixture cut)
  (let* ((dir (make-temp-file "techo-save-recovery-" t))
         (existing (eq fixture 'existing))
         (expected (list (list techo-probe-id (if existing 2 1) "draft B\n")))
         (previous (when existing (list (list techo-probe-id 1 "draft A\n"))))
         (select-local "SELECT id,generation,body FROM local")
         db)
    (unwind-protect
        (progn
          (techo-probe-run-to-cut techo-probe-source
                                  (list 'techo-probe-child dir (list 'quote fixture) (list 'quote cut))
                                  dir cut)
          (setq db (techo-probe-open dir))
          (techo-probe-check (equal (sqlite-select db select-local)
                                    (if (eq cut 'after-commit) expected previous))
                             "Unexpected state at reopen: %s %s" fixture cut)
          (dotimes (_ 2)
            (with-sqlite-transaction db (techo-probe-scan db dir))
            (techo-probe-check (equal (sqlite-select db select-local) expected)
                               "Recovery or repeat scan failed: %s %s" fixture cut)
            (techo-probe-check
             (equal (sqlite-select db "SELECT id,revision,body FROM base")
                    (when existing (list (list techo-probe-id 7 "original"))))
             "Base changed: %s %s" fixture cut)
            (techo-probe-check
             (equal (sqlite-select db "SELECT id,expected,generation,body FROM attempt")
                    (when existing (list (list techo-probe-id 7 1 "draft A\n"))))
             "Frozen attempt changed: %s %s" fixture cut))
          (princ (format "PASS %s / %s\n" fixture cut)))
      (when db (sqlite-close db))
      (delete-directory dir t))))

(defun techo-probe-save-recovery ()
  (unless (sqlite-available-p) (error "This Emacs needs built-in SQLite"))
  (dolist (fixture '(capture existing))
    (dolist (cut '(after-save before-commit after-commit))
      (techo-probe-case fixture cut))))

;;; probe-save-recovery.el ends here
