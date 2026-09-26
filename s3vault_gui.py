"""
S3 Vault - manual, versioned check-in of files to S3-compatible storage.
Run:   pythonw s3vault_gui.py
Build: pyinstaller --onefile --windowed --name S3Vault s3vault_gui.py
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
import traceback
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from s3vault_core import (ERROR_LOG, HashCache, Vault, VaultError, human_size, is_ignored,
                          load_config, local_time, save_config)

APP_NAME = "S3 Vault"
STATUS_COLORS = {
    "modified": "#b35c00",
    "untracked": "#777777",
    "missing": "#999999",
    "older version": "#1f5fbf",
    "unreadable": "#b00020",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1100x650")
        self.minsize(800, 450)
        self.cfg = load_config()
        self.cache = HashCache()
        self.vault = None
        self.rows = {}
        self.commits = []
        self.q = queue.Queue()
        self.busy = False
        self.show_untracked = tk.BooleanVar(value=True)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self._poll)
        self.after(150, self._startup)

    # ------------------------------------------------------------ layout
    def _build_ui(self):
        bar = ttk.Frame(self, padding=(6, 6, 6, 0))
        bar.pack(fill="x", side="top")
        for text, cmd in [("Check In…", self.check_in_dialog),
                          ("Check In Selected", self.check_in_selected),
                          ("History…", self.show_history),
                          ("Get Latest", self.get_latest),
                          ("Refresh", self.refresh),
                          ("Open Folder", self.open_folder),
                          ("Rebuild current/", self.rebuild_current),
                          ("Settings…", self.open_settings)]:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Checkbutton(bar, text="Show untracked", variable=self.show_untracked,
                        command=self._fill_files).pack(side="right")

        sb = ttk.Frame(self, padding=(6, 0, 6, 6))
        sb.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(sb, textvariable=self.status_var).pack(side="left")
        self.pb = ttk.Progressbar(sb, mode="indeterminate", length=160)
        # shown only while work is running (Windows leaves a parked block after stop())

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        # Files tab
        ff = ttk.Frame(self.nb)
        self.nb.add(ff, text="Files")
        self.files = ttk.Treeview(ff, columns=("status", "ver", "date", "user", "comment"),
                                  selectmode="extended")
        for col, text, width in [("#0", "File", 380), ("status", "Status", 100), ("ver", "Latest", 60),
                                 ("date", "Checked in", 130), ("user", "By", 90),
                                 ("comment", "Last comment", 320)]:
            self.files.heading(col, text=text, anchor="w")
            self.files.column(col, width=width, stretch=(col in ("#0", "comment")))
        for st, color in STATUS_COLORS.items():
            self.files.tag_configure(st, foreground=color)
        ys = ttk.Scrollbar(ff, orient="vertical", command=self.files.yview)
        self.files.configure(yscrollcommand=ys.set)
        self.files.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.files.bind("<Double-1>", lambda e: self.show_history())

        # Log tab
        lf = ttk.Frame(self.nb)
        self.nb.add(lf, text="Log")
        pw = ttk.PanedWindow(lf, orient="vertical")
        pw.pack(fill="both", expand=True)
        top, bot = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(top, weight=3)
        pw.add(bot, weight=1)
        self.log = ttk.Treeview(top, columns=("date", "user", "n", "comment"),
                                show="headings", selectmode="browse")
        for col, text, width in [("date", "Date", 130), ("user", "By", 90),
                                 ("n", "Files", 50), ("comment", "Comment", 600)]:
            self.log.heading(col, text=text, anchor="w")
            self.log.column(col, width=width, stretch=(col == "comment"))
        ys2 = ttk.Scrollbar(top, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys2.set)
        self.log.pack(side="left", fill="both", expand=True)
        ys2.pack(side="right", fill="y")
        self.log.bind("<<TreeviewSelect>>", self._on_commit_select)
        self.logfiles = ttk.Treeview(bot, columns=("ver", "size"), selectmode="none")
        self.logfiles.heading("#0", text="Files in this check-in", anchor="w")
        self.logfiles.heading("ver", text="Version", anchor="w")
        self.logfiles.heading("size", text="Size", anchor="w")
        self.logfiles.column("ver", width=70, stretch=False)
        self.logfiles.column("size", width=90, stretch=False)
        self.logfiles.pack(fill="both", expand=True)

    # ------------------------------------------------------------ background work
    def run_bg(self, label, fn, on_done):
        """Run fn(progress) in a thread; on_done(result) runs back on the UI thread."""
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait for the current operation to finish.", parent=self)
            return
        self.busy = True
        self.status_var.set(label + "…")
        self.pb.pack(side="right")
        self.pb.start(12)
        self.config(cursor="watch")

        def worker():
            try:
                res = fn(lambda msg: self.q.put(("status", msg)))
                self.q.put(("done", on_done, res, None))
            except Exception as e:  # noqa: BLE001 - shown to the user
                try:
                    ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
                    with open(ERROR_LOG, "a", encoding="utf-8") as f:
                        f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}  {label}\n")
                        f.write(traceback.format_exc())
                except OSError:
                    pass
                if not isinstance(e, VaultError):
                    e = Exception(f"{type(e).__name__}: {e}\n\nDetails were written to:\n{ERROR_LOG}")
                self.q.put(("done", on_done, None, e))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "status":
                    self.status_var.set(item[1])
                    continue
                _, cb, res, err = item
                self.busy = False
                self.pb.stop()
                self.pb.pack_forget()
                self.config(cursor="")
                self.cache.save()
                if err:
                    self.status_var.set("Error")
                    messagebox.showerror(APP_NAME, str(err), parent=self)
                else:
                    cb(res)
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ------------------------------------------------------------ connection
    def _startup(self):
        if self._connect(quiet=True):
            self.refresh()
        else:
            self.open_settings()

    def _connect(self, quiet=False):
        try:
            self.vault = Vault(self.cfg, self.cache)
            self.title(f"{APP_NAME} — {self.vault.root}")
            return True
        except VaultError as e:
            self.vault = None
            if not quiet:
                messagebox.showerror(APP_NAME, str(e), parent=self)
            return False

    def _need_vault(self):
        if self.vault:
            return True
        messagebox.showinfo(APP_NAME, "Set up the connection first.", parent=self)
        self.open_settings()
        return False

    # ------------------------------------------------------------ refresh / display
    def refresh(self):
        if not self._need_vault():
            return
        v = self.vault

        def work(progress):
            rows = v.scan(progress)
            progress("Reading log")
            return rows, v.list_commits()

        def done(res):
            rows, commits = res
            self.rows = {r["path"]: r for r in rows}
            self.commits = commits
            self._fill_files()
            self._fill_log()
            count = lambda s: sum(1 for r in rows if r["status"] == s)
            tracked = sum(1 for r in rows if r["index"])
            self.status_var.set(f"{tracked} tracked · {count('modified')} modified · "
                                f"{count('untracked')} untracked · {count('missing')} missing")

        self.run_bg("Refreshing", work, done)

    def _fill_files(self):
        selected = set(self.files.selection())
        self.files.delete(*self.files.get_children())
        for rel, r in self.rows.items():
            if r["status"] == "untracked" and not self.show_untracked.get():
                continue
            latest = r["index"]["versions"][-1] if r["index"] and r["index"].get("versions") else None
            values = (r["status"],
                      f"v{latest['version']}" if latest else "",
                      local_time(latest["time"]) if latest else "",
                      latest["user"] if latest else "",
                      latest["comment"].splitlines()[0] if latest else "")
            self.files.insert("", "end", iid=rel, text=rel, values=values, tags=(r["status"],))
        keep = [i for i in selected if self.files.exists(i)]
        if keep:
            self.files.selection_set(keep)

    def _fill_log(self):
        self.log.delete(*self.log.get_children())
        self.logfiles.delete(*self.logfiles.get_children())
        for c in self.commits:
            self.log.insert("", "end", iid=c["id"], values=(
                local_time(c["time"]), c.get("user", ""), len(c["files"]),
                c["comment"].replace("\n", "  ")))

    def _on_commit_select(self, _event=None):
        self.logfiles.delete(*self.logfiles.get_children())
        sel = self.log.selection()
        commit = next((c for c in self.commits if sel and c["id"] == sel[0]), None)
        if commit:
            for f in commit["files"]:
                self.logfiles.insert("", "end", text=f["path"],
                                     values=(f"v{f['version']}", human_size(f["size"])))

    # ------------------------------------------------------------ actions
    def check_in_dialog(self):
        if not self._need_vault():
            return
        paths = filedialog.askopenfilenames(parent=self, title="Select files to check in",
                                            initialdir=str(self.vault.root))
        if paths:
            self._check_in([Path(p) for p in paths])

    def check_in_selected(self):
        if not self._need_vault():
            return
        rels = [i for i in self.files.selection()
                if self.rows.get(i, {}).get("status") in ("modified", "untracked", "older version")]
        if not rels:
            messagebox.showinfo(APP_NAME, "Select one or more modified or untracked files in the list.",
                                parent=self)
            return
        self._check_in([self.vault.local(r) for r in rels])

    def _check_in(self, paths):
        v = self.vault
        try:
            rels = [v.rel(p) for p in paths if not is_ignored(p.name)]
        except VaultError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        if not rels:
            return
        dlg = CheckInDialog(self, rels)
        self.wait_window(dlg)
        if dlg.result is None:
            return

        def done(res):
            commit, skipped = res
            if commit is None:
                messagebox.showinfo(APP_NAME, "Nothing to check in: the selected files are identical "
                                              "to their latest versions.", parent=self)
            else:
                msg = "\n".join(f"{f['path']}  →  v{f['version']}" for f in commit["files"])
                if skipped:
                    msg += "\n\nUnchanged, skipped:\n" + "\n".join(skipped)
                messagebox.showinfo(APP_NAME, "Checked in:\n\n" + msg, parent=self)
            self.refresh()

        self.run_bg("Checking in", lambda p: v.check_in(paths, dlg.result, p), done)

    def show_history(self):
        sel = self.files.selection()
        if len(sel) != 1:
            messagebox.showinfo(APP_NAME, "Select a single file to see its history.", parent=self)
            return
        row = self.rows.get(sel[0])
        if not row or not row["index"]:
            messagebox.showinfo(APP_NAME, "This file hasn't been checked in yet.", parent=self)
            return
        HistoryWindow(self, row)

    def get_latest(self):
        if not self._need_vault():
            return
        if not messagebox.askyesno(APP_NAME, "Download the latest version of every tracked file that is "
                                             "missing or out of date?\n\nFiles with changes that aren't "
                                             "checked in will be left alone.", parent=self):
            return

        def done(res):
            updated, conflicts, errors = res
            parts = [f"Updated {len(updated)} file(s)."]
            if conflicts:
                parts.append("Skipped (local changes not checked in):\n" + "\n".join(conflicts))
            if errors:
                parts.append("Errors:\n" + "\n".join(errors))
            messagebox.showinfo(APP_NAME, "\n\n".join(parts), parent=self)
            self.refresh()

        self.run_bg("Getting latest", self.vault.get_latest, done)

    def rebuild_current(self):
        if not self._need_vault():
            return
        if not messagebox.askyesno(APP_NAME, "Copy the latest version of every tracked file to "
                                             "current/ in the bucket, under its real name?", parent=self):
            return
        self.run_bg("Rebuilding current/", self.vault.rebuild_current,
                    lambda n: messagebox.showinfo(APP_NAME, f"Updated {n} file(s) in current/.", parent=self))

    def open_folder(self):
        if not self._need_vault():
            return
        if sys.platform == "win32":
            os.startfile(self.vault.root)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(self.vault.root)])
        else:
            subprocess.Popen(["xdg-open", str(self.vault.root)])

    def open_settings(self):
        dlg = SettingsDialog(self, self.cfg)
        self.wait_window(dlg)
        if dlg.saved:
            self.cfg = dlg.saved
            save_config(self.cfg)
            if self._connect():
                self.refresh()

    def on_close(self):
        self.cache.save()
        self.destroy()


class CheckInDialog(tk.Toplevel):
    def __init__(self, parent, rels):
        super().__init__(parent)
        self.title("Check In")
        self.transient(parent)
        self.geometry("580x440")
        self.result = None
        ttk.Label(self, text=f"{len(rels)} file(s) selected:").pack(anchor="w", padx=10, pady=(10, 2))
        lb = tk.Listbox(self, height=min(10, max(3, len(rels))))
        for r in rels:
            lb.insert("end", r)
        lb.pack(fill="both", expand=True, padx=10)
        ttk.Label(self, text="Comment (required):").pack(anchor="w", padx=10, pady=(10, 2))
        self.txt = tk.Text(self, height=6, wrap="word")
        self.txt.pack(fill="x", padx=10)
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=10)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Check In", command=self._ok).pack(side="right", padx=6)
        ttk.Label(btns, text="⌘+Return to check in" if sys.platform == "darwin" else "Ctrl+Enter to check in", foreground="#777").pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Control-Return>", lambda e: (self._ok(), "break")[1])
        if sys.platform == "darwin":
            self.bind("<Command-Return>", lambda e: (self._ok(), "break")[1])
        self.txt.focus_set()
        self.grab_set()

    def _ok(self):
        comment = self.txt.get("1.0", "end").strip()
        if not comment:
            messagebox.showwarning("Check In", "Please enter a comment.", parent=self)
            return
        self.result = comment
        self.destroy()


class HistoryWindow(tk.Toplevel):
    def __init__(self, app, row):
        super().__init__(app)
        self.app, self.row, self.rel = app, row, row["path"]
        self.versions = row["index"]["versions"]
        self.title(f"History — {self.rel}")
        self.geometry("860x400")
        self.transient(app)

        self.tree = ttk.Treeview(self, columns=("ver", "date", "user", "size", "comment"),
                                 show="headings", selectmode="browse")
        for col, text, width in [("ver", "Version", 80), ("date", "Date", 130), ("user", "By", 90),
                                 ("size", "Size", 80), ("comment", "Comment", 440)]:
            self.tree.heading(col, text=text, anchor="w")
            self.tree.column(col, width=width, stretch=(col == "comment"))
        for v in reversed(self.versions):
            mark = "● " if v["sha256"] == row.get("sha") else ""
            self.tree.insert("", "end", iid=str(v["version"]), values=(
                f"{mark}v{v['version']}", local_time(v["time"]), v.get("user", ""),
                human_size(v["size"]), v["comment"].replace("\n", "  ")))
        self.tree.pack(fill="both", expand=True, padx=10, pady=(10, 4))
        if self.versions:
            self.tree.selection_set(str(self.versions[-1]["version"]))

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(btns, text=f"● = version in your working folder   (local status: {row['status']})",
                  foreground="#777").pack(side="left")
        ttk.Button(btns, text="Close", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save Copy As…", command=self.save_copy).pack(side="right", padx=6)
        ttk.Button(btns, text="Restore to Working Folder", command=self.restore).pack(side="right")

    def _selected(self):
        sel = self.tree.selection()
        return next((v for v in self.versions if sel and str(v["version"]) == sel[0]), None)

    def restore(self):
        v = self._selected()
        if not v:
            return
        msg = f"Replace your working copy of\n{self.rel}\nwith version {v['version']}?"
        if self.row["status"] == "modified":
            msg += "\n\nWARNING: your local file has changes that are not checked in. They will be lost."
        if v is not self.versions[-1]:
            msg += ("\n\nTo make this the current version, check it in afterwards "
                    "(otherwise Get Latest will bring the newest version back).")
        if not messagebox.askyesno("Restore", msg, parent=self):
            return
        vault, rel = self.app.vault, self.rel

        def done(_):
            self.destroy()
            self.app.refresh()

        self.app.run_bg("Restoring", lambda p: vault.restore(rel, v, progress=p), done)

    def save_copy(self):
        v = self._selected()
        if not v:
            return
        p = Path(self.rel)
        dest = filedialog.asksaveasfilename(parent=self, title="Save copy as",
                                            initialfile=f"{p.stem}_v{v['version']}{p.suffix}")
        if not dest:
            return
        vault, rel = self.app.vault, self.rel
        self.app.run_bg("Downloading", lambda pr: vault.restore(rel, v, dest=dest, progress=pr),
                        lambda d: messagebox.showinfo(APP_NAME, f"Saved:\n{d}", parent=self))


class SettingsDialog(tk.Toplevel):
    FIELDS = [("endpoint_url", "S3 endpoint URL"),
              ("region", "Region"),
              ("bucket", "Bucket"),
              ("prefix", "Prefix in bucket (optional)"),
              ("access_key", "Access key ID"),
              ("secret_key", "Secret access key"),
              ("local_root", "Local vault folder"),
              ("user", "Your name (shown in history)")]

    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("Settings")
        self.transient(parent)
        self.resizable(True, False)
        self.saved = None
        self.vars = {}
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        for i, (key, label) in enumerate(self.FIELDS):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w", pady=3, padx=(0, 8))
            var = tk.StringVar(value=cfg.get(key, ""))
            ttk.Entry(frm, textvariable=var, width=52,
                      show="•" if key == "secret_key" else "").grid(row=i, column=1, sticky="ew", pady=3)
            self.vars[key] = var
            if key == "local_root":
                ttk.Button(frm, text="Browse…", command=self._browse).grid(row=i, column=2, padx=(6, 0))
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="e.g. Garage over Tailscale: endpoint http://100.x.y.z:3900, region garage",
                  foreground="#777").grid(row=len(self.FIELDS), column=0, columnspan=3, sticky="w", pady=(6, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=len(self.FIELDS) + 1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="Test Connection", command=self._test).pack(side="left")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right", padx=6)
        self.grab_set()

    def _values(self):
        return {k: v.get().strip() for k, v in self.vars.items()}

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="Choose the local vault folder",
                                    initialdir=self.vars["local_root"].get() or None)
        if d:
            self.vars["local_root"].set(d)

    def _test(self):
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            Vault(self._values(), HashCache()).test_connection()
            messagebox.showinfo("Settings", "Connected. The bucket is reachable.", parent=self)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Settings", f"Connection failed:\n\n{e}", parent=self)
        finally:
            self.config(cursor="")

    def _save(self):
        self.saved = self._values()
        self.destroy()


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)  # crisp text on high-DPI screens
        except Exception:  # noqa: BLE001
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
