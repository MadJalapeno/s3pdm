"""
S3 Vault - manual, versioned check-in of files to S3-compatible storage.
Run:   python s3vault_gui.py        (pythonw to hide the console on Windows)
Build: python -m PyInstaller --onefile --windowed --name S3Vault s3vault_gui.py
"""
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path
import json
from tkinter import filedialog, font as tkfont, messagebox, simpledialog, ttk

from s3vault_core import (APP_URL, CONFIG_DIR, DEFAULT_CONFIG, __version__, ERROR_LOG, HashCache, Vault, VaultError, human_size,
                          is_ignored, load_profiles, local_time, save_profiles)

APP_NAME = "S3 Vault"
STATUS_COLORS = {
    "modified": "#b35c00",
    "untracked": "#777777",
    "missing": "#999999",
    "older version": "#1f5fbf",
    "unreadable": "#b00020",
}
# Folder colour follows the most important status inside it.
STATUS_PRIORITY = ["unreadable", "modified", "missing", "older version", "untracked", "up to date"]
CHECKIN_STATUSES = ("modified", "untracked", "older version")
IS_MAC = sys.platform == "darwin"


def open_path(path: Path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    elif IS_MAC:
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def reveal_path(path: Path):
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", str(path)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path.parent)])


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1150x680")
        self.minsize(850, 450)
        self.profiles = load_profiles()
        self.cfg = self.profiles["profiles"][self.profiles["active"]]
        self.cache = HashCache()
        self.vault = None
        self.rows = {}           # rel path -> row
        self.visible = set()     # rel paths currently shown
        self.open_dirs = None    # expanded folders (None = first fill, expand all)
        self.commits = []
        self.q = queue.Queue()
        self.busy = False
        self.busy_label = ""
        self.generation = 0          # bumped on every profile connect; stale results are discarded
        self.pending_refresh = False
        self.show_untracked = tk.BooleanVar(value=True)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.last_refresh = 0.0
        self.bind("<Activate>", self._on_activate)
        if IS_MAC:
            self.createcommand("tk::mac::ShowAbout", self.show_about)  # app menu → About
        self.after(100, self._poll)
        self.after(150, self._startup)

    # ------------------------------------------------------------ layout
    def _build_ui(self):
        bar = ttk.Frame(self, padding=(6, 6, 6, 0))
        bar.pack(fill="x", side="top")
        for text, cmd in [("Check In…", self.check_in_dialog),
                          ("Check In Selected", self.check_in_selected),
                          ("History…", self.show_history),
                          ("Rename/Move…", self.rename_selected),
                          ("Untrack…", self.untrack_selected),
                          ("Get Latest", self.get_latest),
                          ("Refresh", self.refresh)]:
            ttk.Button(bar, text=text, command=cmd).pack(side="left", padx=(0, 4))
        more = ttk.Menubutton(bar, text="More ▾")
        menu = tk.Menu(more, tearoff=False)
        menu.add_command(label="Open Vault Folder", command=self.open_folder)
        menu.add_command(label="Expand All", command=lambda: self._expand_all(True))
        menu.add_command(label="Collapse All", command=lambda: self._expand_all(False))
        menu.add_separator()
        menu.add_command(label="Rebuild current/ in Bucket", command=self.rebuild_current)
        menu.add_command(label="Reload History from Bucket", command=self.reload_history)
        menu.add_command(label="Recheck All Local Files", command=self.recheck_files)
        menu.add_separator()
        menu.add_command(label="Settings…", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label=f"About {APP_NAME}…", command=self.show_about)
        more["menu"] = menu
        more.pack(side="left", padx=(0, 4))
        ttk.Checkbutton(bar, text="Show untracked", variable=self.show_untracked,
                        command=self._fill_files).pack(side="right")
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(bar, textvariable=self.profile_var, state="readonly", width=22)
        self.profile_combo.pack(side="right", padx=(0, 12))
        # Watch the variable itself rather than relying on <<ComboboxSelected>>,
        # which isn't delivered reliably on every platform / Tk build.
        self.profile_var.trace_add("write", lambda *_: self.after_idle(self._switch_profile))
        ttk.Label(bar, text="Profile:").pack(side="right", padx=(0, 4))
        self._update_profile_combo()

        sb = ttk.Frame(self, padding=(6, 0, 6, 6))
        sb.pack(fill="x", side="bottom")
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(sb, textvariable=self.status_var).pack(side="left")
        self.pb = ttk.Progressbar(sb, mode="indeterminate", length=160)  # shown only while busy

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        # Files tab (folder tree)
        ff = ttk.Frame(self.nb)
        self.nb.add(ff, text="Files")
        self.files = ttk.Treeview(ff, columns=("status", "ver", "size", "date", "user", "comment"),
                                  selectmode="extended")
        for col, text, width, anchor in [("#0", "Name", 340, "w"), ("status", "Status", 130, "w"),
                                         ("ver", "Latest", 55, "w"), ("size", "Size", 80, "e"),
                                         ("date", "Checked in", 125, "w"), ("user", "By", 80, "w"),
                                         ("comment", "Last comment", 300, "w")]:
            self.files.heading(col, text=text, anchor=anchor)
            self.files.column(col, width=width, anchor=anchor, stretch=(col in ("#0", "comment")))
        for st, color in STATUS_COLORS.items():
            self.files.tag_configure(st, foreground=color)
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.files.tag_configure("folder", font=bold)
        ys = ttk.Scrollbar(ff, orient="vertical", command=self.files.yview)
        self.files.configure(yscrollcommand=ys.set)
        self.files.pack(side="left", fill="both", expand=True)
        ys.pack(side="right", fill="y")
        self.files.bind("<Double-1>", self._on_double_click)
        self.files.bind("<Button-2>" if IS_MAC else "<Button-3>", self._context_menu)
        if IS_MAC:
            self.files.bind("<Control-Button-1>", self._context_menu)

        self.ctx = tk.Menu(self, tearoff=False)
        self.ctx.add_command(label="Check In…", command=self.check_in_selected)
        self.ctx.add_command(label="History…", command=self.show_history)
        self.ctx.add_command(label="Rename/Move…", command=self.rename_selected)
        self.ctx.add_command(label="Untrack…", command=self.untrack_selected)
        self.ctx.add_separator()
        self.ctx.add_command(label="Open", command=self.open_selected)
        self.ctx.add_command(label="Show in Folder", command=self.reveal_selected)

        # Log tab
        lf = ttk.Frame(self.nb)
        self.nb.add(lf, text="Log")
        pw = ttk.PanedWindow(lf, orient="vertical")
        pw.pack(fill="both", expand=True)
        top, bot = ttk.Frame(pw), ttk.Frame(pw)
        pw.add(top, weight=3)
        pw.add(bot, weight=1)
        self.log = ttk.Treeview(top, columns=("date", "action", "user", "n", "comment"),
                                show="headings", selectmode="browse")
        for col, text, width in [("date", "Date", 125), ("action", "Action", 80), ("user", "By", 80),
                                 ("n", "Files", 50), ("comment", "Comment", 600)]:
            self.log.heading(col, text=text, anchor="w")
            self.log.column(col, width=width, stretch=(col == "comment"))
        ys2 = ttk.Scrollbar(top, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys2.set)
        self.log.pack(side="left", fill="both", expand=True)
        ys2.pack(side="right", fill="y")
        self.log.bind("<<TreeviewSelect>>", self._on_commit_select)
        self.logfiles = ttk.Treeview(bot, columns=("ver", "size"), selectmode="none")
        self.logfiles.heading("#0", text="Files", anchor="w")
        self.logfiles.heading("ver", text="Version", anchor="w")
        self.logfiles.heading("size", text="Size", anchor="e")
        self.logfiles.column("ver", width=70, stretch=False)
        self.logfiles.column("size", width=90, stretch=False, anchor="e")
        self.logfiles.pack(fill="both", expand=True)

    # ------------------------------------------------------------ background work
    def run_bg(self, label, fn, on_done):
        """Run fn(progress) in a thread; on_done(result) runs back on the UI thread."""
        if self.busy:
            messagebox.showinfo(APP_NAME, "Please wait for the current operation to finish.", parent=self)
            return
        self.busy = True
        self.busy_label = label
        gen = self.generation
        self.status_var.set(label + "…")
        self.pb.pack(side="right")
        self.pb.start(12)
        self.config(cursor="watch")

        def worker():
            try:
                res = fn(lambda msg: self.q.put(("status", msg, gen)))
                self.q.put(("done", on_done, res, None, gen))
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
                self.q.put(("done", on_done, None, e, gen))

        threading.Thread(target=worker, daemon=True).start()

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "status":
                    if item[2] == self.generation:
                        self.status_var.set(item[1])
                    continue
                _, cb, res, err, gen = item
                self.busy = False
                self.busy_label = ""
                self.pb.stop()
                self.pb.pack_forget()
                self.config(cursor="")
                self.cache.save()
                if gen != self.generation:
                    # Finished after a profile switch: belongs to the old profile, so discard it.
                    self.pending_refresh = True
                elif err:
                    self.status_var.set("Error")
                    messagebox.showerror(APP_NAME, str(err), parent=self)
                else:
                    cb(res)
                if self.pending_refresh and not self.busy:
                    self.pending_refresh = False
                    self.refresh()
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
        name = self.profiles["active"]
        self.generation += 1
        self.vault = None                 # never leave the previous profile connected
        self._clear_view()
        try:
            self.vault = Vault(self.cfg, self.cache)
            self.title(f"{APP_NAME} — {name} — {self.vault.root}")
            return True
        except Exception as e:  # noqa: BLE001
            if not isinstance(e, VaultError):
                self._log_error(f"Connecting profile {name}")
                e = f"{type(e).__name__}: {e}"
            self.vault = None
            self.title(f"{APP_NAME} — {name}")
            self.status_var.set("Not connected")
            if not quiet:
                messagebox.showerror(APP_NAME, str(e), parent=self)
            return False

    def _clear_view(self):
        """Empty the lists so files from another profile are never shown."""
        self.rows, self.visible, self.commits = {}, set(), []
        self.open_dirs = None
        self.files.delete(*self.files.get_children())
        self.log.delete(*self.log.get_children())
        self.logfiles.delete(*self.logfiles.get_children())

    def _update_profile_combo(self):
        self.profile_combo["values"] = sorted(self.profiles["profiles"], key=str.lower)
        self.profile_var.set(self.profiles["active"])

    def _switch_profile(self, _event=None):
        name = self.profile_var.get()
        if name == self.profiles["active"]:
            return
        if self.busy and self.busy_label != "Refreshing":
            self.profile_var.set(self.profiles["active"])
            messagebox.showinfo(APP_NAME, "Please wait for the current operation to finish.", parent=self)
            return
        self.cache.save()
        self.profiles["active"] = name
        save_profiles(self.profiles)
        self.cfg = self.profiles["profiles"][name]
        if self._connect():
            self.refresh()

    def _need_vault(self):
        if self.vault:
            return True
        messagebox.showinfo(APP_NAME, "Set up the connection first.", parent=self)
        self.open_settings()
        return False

    # ------------------------------------------------------------ refresh / display
    def _on_activate(self, event):
        """Refresh when switching back to the app, at most every 10 seconds, if no dialog is open."""
        if event.widget is not self or self.busy or not self.vault:
            return
        if any(isinstance(w, tk.Toplevel) for w in self.winfo_children()):
            return
        if time.monotonic() - self.last_refresh > 10:
            self.refresh()

    def refresh(self):
        if not self.vault:
            return
        if self.busy:
            self.pending_refresh = True   # run it as soon as the current task finishes
            return
        self.last_refresh = time.monotonic()
        v = self.vault

        def done(rows):
            self.rows = {r["path"]: r for r in rows}
            self.commits = v.commits()
            self._fill_files()
            self._fill_log()
            count = lambda s: sum(1 for r in rows if r["status"] == s)
            tracked = sum(1 for r in rows if r["index"])
            self.status_var.set(f"{tracked} tracked · {count('modified')} modified · "
                                f"{count('untracked')} untracked · {count('missing')} missing")

        self.run_bg("Refreshing", v.scan, done)

    def _fill_files(self):
        t = self.files
        if self.open_dirs is not None:
            self.open_dirs = {i for i in self._all_items() if i.startswith("d:") and t.item(i, "open")}
        selected = set(t.selection())
        t.delete(*t.get_children())

        rows = [r for r in self.rows.values()
                if self.show_untracked.get() or r["status"] != "untracked"]
        self.visible = {r["path"] for r in rows}

        # Build a nested structure: {"dirs": {name: node}, "files": [row]}
        root = {"dirs": {}, "files": []}
        for r in rows:
            node = root
            for part in r["path"].split("/")[:-1]:
                node = node["dirs"].setdefault(part, {"dirs": {}, "files": []})
            node["files"].append(r)

        def summarize(node):
            size, statuses = 0, {}
            for child in node["dirs"].values():
                s, st = summarize(child)
                size += s
                for k, n in st.items():
                    statuses[k] = statuses.get(k, 0) + n
            for r in node["files"]:
                size += r["size"] or 0
                statuses[r["status"]] = statuses.get(r["status"], 0) + 1
            node["size"], node["statuses"] = size, statuses
            return size, statuses

        summarize(root)

        def insert(node, parent_iid, prefix):
            for name in sorted(node["dirs"], key=str.lower):
                child = node["dirs"][name]
                path = prefix + name
                iid = "d:" + path
                sts = child["statuses"]
                parts = [f"{sts[s]} {s}" for s in STATUS_PRIORITY if s in sts and s != "up to date"]
                worst = next(s for s in STATUS_PRIORITY if s in sts)
                is_open = True if self.open_dirs is None else iid in self.open_dirs
                t.insert(parent_iid, "end", iid=iid, text=name, open=is_open,
                         values=(", ".join(parts) or "up to date", "", human_size(child["size"]), "", "", ""),
                         tags=("folder", worst))
                insert(child, iid, path + "/")
            for r in sorted(node["files"], key=lambda r: r["path"].lower()):
                latest = r["index"]["versions"][-1] if r["index"] and r["index"].get("versions") else None
                t.insert(parent_iid, "end", iid="f:" + r["path"], text=r["path"].rsplit("/", 1)[-1],
                         values=(r["status"],
                                 f"v{latest['version']}" if latest else "",
                                 human_size(r["size"]),
                                 local_time(latest["time"]) if latest else "",
                                 latest["user"] if latest else "",
                                 latest["comment"].splitlines()[0] if latest else ""),
                         tags=(r["status"],))

        insert(root, "", "")
        if self.open_dirs is None:
            self.open_dirs = set()
        keep = [i for i in selected if t.exists(i)]
        if keep:
            t.selection_set(keep)

    def _all_items(self, parent=""):
        for i in self.files.get_children(parent):
            yield i
            yield from self._all_items(i)

    def _expand_all(self, state):
        for i in self._all_items():
            if i.startswith("d:"):
                self.files.item(i, open=state)

    def _fill_log(self):
        self.log.delete(*self.log.get_children())
        self.logfiles.delete(*self.logfiles.get_children())
        for c in self.commits:
            self.log.insert("", "end", iid=c["id"], values=(
                local_time(c["time"]), c.get("action", "checkin").replace("checkin", "check in"),
                c.get("user", ""), len(c["files"]), c["comment"].replace("\n", "  ")))

    def _on_commit_select(self, _event=None):
        self.logfiles.delete(*self.logfiles.get_children())
        sel = self.log.selection()
        commit = next((c for c in self.commits if sel and c["id"] == sel[0]), None)
        if not commit:
            return
        for f in commit["files"]:
            text = f"{f['from']}  →  {f['path']}" if f.get("from") else f["path"]
            self.logfiles.insert("", "end", text=text,
                                 values=(f"v{f['version']}" if f.get("version") else "",
                                         human_size(f.get("size"))))

    # ------------------------------------------------------------ selection helpers
    def _selected_rels(self, statuses=None):
        """Files selected directly or inside selected folders (only those shown)."""
        rels, seen = [], set()
        for iid in self.files.selection():
            if iid.startswith("f:"):
                cands = [iid[2:]]
            else:
                pfx = iid[2:] + "/"
                cands = sorted((r for r in self.visible if r.startswith(pfx)), key=str.lower)
            for r in cands:
                if r not in seen and r in self.rows and (statuses is None or self.rows[r]["status"] in statuses):
                    seen.add(r)
                    rels.append(r)
        return rels

    def _single_selection(self):
        sel = self.files.selection()
        return sel[0] if len(sel) == 1 else None

    def _context_menu(self, event):
        iid = self.files.identify_row(event.y)
        if iid and iid not in self.files.selection():
            self.files.selection_set(iid)
        if self.files.selection():
            self.ctx.tk_popup(event.x_root, event.y_root)

    def _on_double_click(self, event):
        iid = self.files.identify_row(event.y)
        if iid.startswith("f:"):
            self.show_history()

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
        rels = [r for r in self._selected_rels() if self.rows[r]["status"] != "missing"]
        if not rels:
            messagebox.showinfo(APP_NAME, "Select the files or folders you want to check in.", parent=self)
            return
        v = self.vault

        def work(progress):
            """Re-check the selected files on disk now, rather than trusting the last refresh."""
            changed = []
            for r in rels:
                progress(f"Checking {r}")
                st, _ = v.status(r, v.hist.indexes.get(r))
                if st in CHECKIN_STATUSES:
                    changed.append(r)
            return changed

        def done(changed):
            if not changed:
                messagebox.showinfo(APP_NAME, "Nothing to check in: the selected files are identical "
                                              "to their latest checked-in versions.", parent=self)
                self.refresh()
                return
            self._check_in([v.local(r) for r in changed])

        self.run_bg("Checking for changes", work, done)

    def _check_in(self, paths):
        v = self.vault
        try:
            rels = [v.rel(p) for p in paths if not is_ignored(p.name)]
        except VaultError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        if not rels:
            return
        dlg = CommentDialog(self, "Check In", f"{len(rels)} file(s) to check in:", rels, ok_text="Check In")
        self.wait_window(dlg)
        if dlg.result is None:
            return

        def done(res):
            commit, skipped = res
            if commit is None:
                messagebox.showinfo(APP_NAME, "Nothing to check in: the selected files are identical "
                                              "to their latest versions.", parent=self)
            else:
                lines = [f"{f['path']}  →  v{f['version']}" for f in commit["files"]]
                msg = "\n".join(lines[:25]) + (f"\n… and {len(lines) - 25} more" if len(lines) > 25 else "")
                if skipped:
                    msg += f"\n\n{len(skipped)} unchanged file(s) skipped."
                messagebox.showinfo(APP_NAME, "Checked in:\n\n" + msg, parent=self)
            self.refresh()

        paths = [v.local(r) for r in rels]
        self.run_bg("Checking in", lambda p: v.check_in(paths, dlg.result, p), done)

    def show_history(self):
        iid = self._single_selection()
        if not iid or not iid.startswith("f:"):
            messagebox.showinfo(APP_NAME, "Select a single file to see its history.", parent=self)
            return
        row = self.rows.get(iid[2:])
        if not row or not row["index"]:
            messagebox.showinfo(APP_NAME, "This file hasn't been checked in yet.", parent=self)
            return
        HistoryWindow(self, row)

    def rename_selected(self):
        if not self._need_vault():
            return
        iid = self._single_selection()
        if not iid:
            messagebox.showinfo(APP_NAME, "Select one file or one folder to rename or move.", parent=self)
            return
        is_dir = iid.startswith("d:")
        old = iid[2:]
        if not is_dir and not (self.rows.get(old) or {}).get("index"):
            messagebox.showinfo(APP_NAME, "This file isn't tracked, so just rename it normally.", parent=self)
            return
        dlg = RenameDialog(self, old, is_dir)
        self.wait_window(dlg)
        if not dlg.result:
            return
        new, comment = dlg.result
        v = self.vault
        if is_dir:
            work = lambda p: v.rename_folder(old, new, comment, p)
        else:
            work = lambda p: v.rename([(old, new)], comment, p)

        def done(commit):
            n = len(commit["files"]) if commit else 0
            self.status_var.set(f"Renamed {n} tracked file(s).")
            self.refresh()

        self.run_bg("Renaming", work, done)

    def untrack_selected(self):
        if not self._need_vault():
            return
        rels = [r for r in self._selected_rels() if self.rows[r]["index"]]
        if not rels:
            messagebox.showinfo(APP_NAME, "Select tracked files or folders to untrack.", parent=self)
            return
        dlg = CommentDialog(self, "Untrack", f"Stop tracking {len(rels)} file(s)? Their history is kept "
                                             "in the bucket's archive.", rels, ok_text="Untrack",
                            checkbox="Also delete the local copies")
        self.wait_window(dlg)
        if dlg.result is None:
            return
        v, delete_local = self.vault, dlg.checked

        def done(res):
            _, errors = res
            if errors:
                messagebox.showwarning(APP_NAME, "Untracked, but some local files couldn't be deleted:\n\n"
                                       + "\n".join(errors), parent=self)
            self.refresh()

        self.run_bg("Untracking", lambda p: v.untrack(rels, dlg.result, delete_local, p), done)

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

    def reload_history(self):
        if not self._need_vault():
            return
        self.vault.clear_history_cache()
        self.refresh()

    def recheck_files(self):
        """Forget cached file hashes so every file is re-read on the next refresh."""
        with self.cache.lock:
            self.cache.data.clear()
            self.cache.dirty = True
        self.refresh()

    def open_folder(self):
        if self._need_vault():
            open_path(self.vault.root)

    def _selected_path(self):
        iid = self._single_selection()
        return self.vault.local(iid[2:]) if iid and self.vault else None

    def open_selected(self):
        p = self._selected_path()
        if p and p.exists():
            open_path(p)

    def reveal_selected(self):
        p = self._selected_path()
        if p and p.exists():
            reveal_path(p)

    def open_settings(self):
        if self.busy and self.busy_label != "Refreshing":
            messagebox.showinfo(APP_NAME, "Please wait for the current operation to finish.", parent=self)
            return
        dlg = SettingsDialog(self, self.profiles)
        self.wait_window(dlg)
        if dlg.saved:
            self.profiles = dlg.saved
            save_profiles(self.profiles)
            self.cfg = self.profiles["profiles"][self.profiles["active"]]
            self._update_profile_combo()
            if self._connect():
                self.refresh()

    def _log_error(self, label):
        try:
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}  {label}\n")
                f.write(traceback.format_exc())
        except OSError:
            pass

    def report_callback_exception(self, exc, val, tb):
        """Tk calls this for errors in button/menu/event handlers. Without it they vanish
        silently under pythonw or a packaged app."""
        try:
            ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
            with open(ERROR_LOG, "a", encoding="utf-8") as f:
                f.write(f"\n=== {datetime.now():%Y-%m-%d %H:%M:%S}  UI callback\n")
                f.write("".join(traceback.format_exception(exc, val, tb)))
        except OSError:
            pass
        messagebox.showerror(APP_NAME, f"Unexpected error: {exc.__name__}: {val}\n\n"
                                       f"Details were written to:\n{ERROR_LOG}", parent=self)

    def show_about(self):
        AboutDialog(self)

    def on_close(self):
        self.cache.save()
        self.destroy()


class AboutDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title(f"About {APP_NAME}")
        self.transient(parent)
        self.resizable(False, False)
        frm = ttk.Frame(self, padding=(24, 18))
        frm.pack(fill="both", expand=True)
        title_font = tkfont.nametofont("TkDefaultFont").copy()
        title_font.configure(size=title_font.cget("size") + 6, weight="bold")
        ttk.Label(frm, text=APP_NAME, font=title_font).pack()
        ttk.Label(frm, text=f"Version {__version__}").pack(pady=(2, 10))
        ttk.Label(frm, text="Manual, versioned check-in of design files\n"
                            "to S3-compatible storage or a network drive.",
                  justify="center").pack()
        link_font = tkfont.nametofont("TkDefaultFont").copy()
        link_font.configure(underline=True)
        link = ttk.Label(frm, text=APP_URL.replace("https://", ""), foreground="#1f5fbf",
                         cursor="hand2", font=link_font)
        link.pack(pady=(10, 12))
        link.bind("<Button-1>", lambda e: webbrowser.open(APP_URL))
        cfg = parent.cfg
        if cfg.get("backend") == "folder":
            storage = f"Folder: {cfg.get('storage_path', '')}"
        else:
            storage = f"S3: {cfg.get('endpoint_url') or 'AWS'} / {cfg.get('bucket', '')}"
        if cfg.get("prefix"):
            storage += f" / {cfg['prefix']}"
        in_use = parent.vault.root if parent.vault else "(not connected)"
        ttk.Label(frm, text=f"Profile: {parent.profiles['active']}\nLocal folder: {in_use}\n{storage}",
                  justify="center").pack(pady=(0, 10))
        details = (f"Python {sys.version.split()[0]} · Tk {self.tk.call('info', 'patchlevel')}\n"
                   f"Settings: {CONFIG_DIR}")
        ttk.Label(frm, text=details, foreground="#777", justify="center").pack()
        ttk.Button(frm, text="Close", command=self.destroy).pack(pady=(14, 0))
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self.destroy())
        self.grab_set()


class CommentDialog(tk.Toplevel):
    """Lists files and asks for a required comment (and optionally a checkbox)."""

    def __init__(self, parent, title, heading, rels, ok_text="OK", checkbox=None, comment=""):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.geometry("600x460")
        self.result = None
        self.checked = False
        ttk.Label(self, text=heading, wraplength=570).pack(anchor="w", padx=10, pady=(10, 2))
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=10)
        lb = tk.Listbox(frame, height=min(12, max(3, len(rels))))
        sb = ttk.Scrollbar(frame, orient="vertical", command=lb.yview)
        lb.configure(yscrollcommand=sb.set)
        for r in rels:
            lb.insert("end", r)
        lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ttk.Label(self, text="Comment (required):").pack(anchor="w", padx=10, pady=(10, 2))
        self.txt = tk.Text(self, height=5, wrap="word")
        self.txt.insert("1.0", comment)
        self.txt.pack(fill="x", padx=10)
        self.check_var = tk.BooleanVar(value=False)
        if checkbox:
            ttk.Checkbutton(self, text=checkbox, variable=self.check_var).pack(anchor="w", padx=10, pady=(8, 0))
        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=10, pady=10)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text=ok_text, command=self._ok).pack(side="right", padx=6)
        hint = "⌘+Return" if IS_MAC else "Ctrl+Enter"
        ttk.Label(btns, text=f"{hint} to confirm", foreground="#777").pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Control-Return>", lambda e: (self._ok(), "break")[1])
        if IS_MAC:
            self.bind("<Command-Return>", lambda e: (self._ok(), "break")[1])
        self.txt.focus_set()
        self.grab_set()

    def _ok(self):
        comment = self.txt.get("1.0", "end").strip()
        if not comment:
            messagebox.showwarning(self.title(), "Please enter a comment.", parent=self)
            return
        self.result = comment
        self.checked = self.check_var.get()
        self.destroy()


class RenameDialog(tk.Toplevel):
    def __init__(self, app, old, is_dir):
        super().__init__(app)
        self.app, self.old, self.is_dir = app, old, is_dir
        kind = "folder" if is_dir else "file"
        self.title(f"Rename/Move {kind}")
        self.transient(app)
        self.resizable(True, False)
        self.result = None
        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)
        ttk.Label(frm, text="Current path:").grid(row=0, column=0, sticky="w")
        ttk.Label(frm, text=old).grid(row=0, column=1, columnspan=2, sticky="w")
        ttk.Label(frm, text="New path:").grid(row=1, column=0, sticky="w", pady=6)
        self.new_var = tk.StringVar(value=old)
        entry = ttk.Entry(frm, textvariable=self.new_var, width=70)
        entry.grid(row=1, column=1, sticky="ew", pady=6)
        if not is_dir:
            ttk.Button(frm, text="Browse…", command=self._browse).grid(row=1, column=2, padx=(6, 0))
        ttk.Label(frm, text="Comment:").grid(row=2, column=0, sticky="nw")
        self.txt = tk.Text(frm, height=3, width=60, wrap="word")
        self.txt.grid(row=2, column=1, columnspan=2, sticky="ew")
        note = ("Paths are relative to the vault folder. Use / to move into another folder.\n"
                "If you already renamed it (e.g. with SOLIDWORKS Rename or Pack and Go), enter the new "
                "name and only the history is moved.\n"
                "Renaming SOLIDWORKS files here does not update references in assemblies and drawings.")
        ttk.Label(frm, text=note, foreground="#777", wraplength=560, justify="left").grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Rename", command=self._ok).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Escape>", lambda e: self.destroy())
        entry.focus_set()
        entry.icursor("end")
        self.grab_set()

    def _browse(self):
        start = self.app.vault.local(self.old)
        kwargs = dict(parent=self, title="New name / location", initialdir=str(start.parent),
                      initialfile=start.name)
        try:
            path = filedialog.asksaveasfilename(confirmoverwrite=False, **kwargs)
        except tk.TclError:
            path = filedialog.asksaveasfilename(**kwargs)
        if path:
            try:
                self.new_var.set(self.app.vault.rel(path))
            except VaultError as e:
                messagebox.showerror(self.title(), str(e), parent=self)

    def _ok(self):
        new = self.new_var.get().strip().replace("\\", "/").strip("/")
        if not new or new == self.old:
            messagebox.showwarning(self.title(), "Enter a new path.", parent=self)
            return
        try:
            new = self.app.vault.rel(self.app.vault.root / new)  # normalizes and checks it stays inside
        except VaultError as e:
            messagebox.showerror(self.title(), str(e), parent=self)
            return
        if not self.is_dir and Path(new).suffix.lower() != Path(self.old).suffix.lower():
            if not messagebox.askyesno(self.title(), "The file extension is changing. Continue?", parent=self):
                return
        comment = self.txt.get("1.0", "end").strip() or f"Renamed {self.old} → {new}"
        self.result = (new, comment)
        self.destroy()


class HistoryWindow(tk.Toplevel):
    def __init__(self, app, row):
        super().__init__(app)
        self.app, self.row, self.rel = app, row, row["path"]
        self.versions = row["index"]["versions"]
        self.title(f"History — {self.rel}")
        self.geometry("880x420")
        self.transient(app)

        renames = row["index"].get("renames") or []
        if renames:
            names = " → ".join([renames[0]["from"]] + [r["to"] for r in renames])
            ttk.Label(self, text=f"Renamed: {names}", foreground="#555", wraplength=850).pack(
                anchor="w", padx=10, pady=(10, 0))

        self.tree = ttk.Treeview(self, columns=("ver", "date", "user", "size", "comment"),
                                 show="headings", selectmode="browse")
        for col, text, width, anchor in [("ver", "Version", 80, "w"), ("date", "Date", 125, "w"),
                                         ("user", "By", 80, "w"), ("size", "Size", 80, "e"),
                                         ("comment", "Comment", 460, "w")]:
            self.tree.heading(col, text=text, anchor=anchor)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "comment"))
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
    S3_FIELDS = [("endpoint_url", "S3 endpoint URL"),
                 ("region", "Region"),
                 ("bucket", "Bucket"),
                 ("access_key", "Access key ID"),
                 ("secret_key", "Secret access key")]
    FOLDER_FIELDS = [("storage_path", "Storage folder")]
    COMMON_FIELDS = [("prefix", "Prefix / subfolder (optional)"),
                     ("local_root", "Local vault folder"),
                     ("user", "Your name (shown in history)")]
    HINTS = {
        "s3": "e.g. Backblaze: endpoint https://s3.us-east-005.backblazeb2.com, region us-east-005",
        "folder": ("A network drive or NAS share (e.g. " +
                   ("/Volumes/share/vaults" if IS_MAC else r"\\server\share\vaults") +
                   "), a USB drive, or a folder synced by Sync.com, Box Drive or OneDrive. "
                   "It must be separate from the local vault folder."),
    }

    def __init__(self, parent, profiles):
        super().__init__(parent)
        self.title("Settings")
        self.transient(parent)
        self.resizable(True, False)
        self.saved = None
        self.data = json.loads(json.dumps(profiles))  # work on a copy until Save
        self.current = self.data["active"]
        self.vars = {}
        self.rows = {}

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        # profile row
        ttk.Label(frm, text="Profile").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.profile_var = tk.StringVar()
        self.profile_combo = ttk.Combobox(frm, textvariable=self.profile_var, state="readonly")
        self.profile_combo.grid(row=0, column=1, sticky="ew")
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_select)
        pbtns = ttk.Frame(frm)
        pbtns.grid(row=1, column=1, columnspan=2, sticky="w", pady=(4, 0))
        for text, cmd in [("New…", self._new), ("Duplicate…", self._duplicate),
                          ("Rename…", self._rename), ("Delete", self._delete)]:
            ttk.Button(pbtns, text=text, command=cmd).pack(side="left", padx=(0, 4))
        ttk.Separator(frm).grid(row=2, column=0, columnspan=3, sticky="ew", pady=10)

        # storage type
        ttk.Label(frm, text="Storage").grid(row=3, column=0, sticky="w", padx=(0, 8))
        self.backend_var = tk.StringVar(value="s3")
        rb = ttk.Frame(frm)
        rb.grid(row=3, column=1, columnspan=2, sticky="w")
        ttk.Radiobutton(rb, text="S3-compatible cloud storage", value="s3", variable=self.backend_var,
                        command=self._show_backend).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(rb, text="Folder / network drive", value="folder", variable=self.backend_var,
                        command=self._show_backend).pack(side="left")

        r = 4
        for key, label in self.S3_FIELDS + self.FOLDER_FIELDS + self.COMMON_FIELDS:
            lab = ttk.Label(frm, text=label)
            lab.grid(row=r, column=0, sticky="w", pady=3, padx=(0, 8))
            var = tk.StringVar()
            ent = ttk.Entry(frm, textvariable=var, width=56, show="•" if key == "secret_key" else "")
            ent.grid(row=r, column=1, sticky="ew", pady=3)
            widgets = [lab, ent]
            if key in ("local_root", "storage_path"):
                btn = ttk.Button(frm, text="Browse…", command=lambda k=key: self._browse(k))
                btn.grid(row=r, column=2, padx=(6, 0))
                widgets.append(btn)
            self.vars[key], self.rows[key] = var, widgets
            r += 1
        self.hint = ttk.Label(frm, foreground="#777", wraplength=560, justify="left")
        self.hint.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=r + 1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Button(btns, text="Test Connection", command=self._test).pack(side="left")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Save", command=self._save).pack(side="right", padx=6)

        self._refresh_profile_list()
        self._load(self.current)
        self.grab_set()

    # ---- profile data
    def _refresh_profile_list(self):
        self.profile_combo["values"] = sorted(self.data["profiles"], key=str.lower)

    def _load(self, name):
        self.current = name
        cfg = {**DEFAULT_CONFIG, **self.data["profiles"][name]}
        for key, var in self.vars.items():
            var.set(cfg.get(key, ""))
        self.backend_var.set(cfg.get("backend") or "s3")
        self.profile_var.set(name)
        self._show_backend()

    def _store(self):
        cfg = self.data["profiles"][self.current]
        cfg.update({k: v.get().strip() for k, v in self.vars.items()})
        cfg["backend"] = self.backend_var.get()

    def _show_backend(self):
        backend = self.backend_var.get()
        for key, _ in self.S3_FIELDS:
            for w in self.rows[key]:
                w.grid() if backend == "s3" else w.grid_remove()
        for key, _ in self.FOLDER_FIELDS:
            for w in self.rows[key]:
                w.grid() if backend == "folder" else w.grid_remove()
        self.hint.configure(text=self.HINTS[backend])

    def _ask_name(self, title, initial="", allow_same=False):
        name = simpledialog.askstring(title, "Profile name:", initialvalue=initial, parent=self)
        name = (name or "").strip()
        if not name:
            return None
        if name in self.data["profiles"] and not (allow_same and name == initial):
            messagebox.showerror(title, f"A profile called \"{name}\" already exists.", parent=self)
            return None
        return name

    def _on_profile_select(self, _event=None):
        self._store()
        self._load(self.profile_var.get())

    def _new(self):
        name = self._ask_name("New Profile")
        if name:
            self._store()
            self.data["profiles"][name] = dict(DEFAULT_CONFIG)
            self._refresh_profile_list()
            self._load(name)

    def _duplicate(self):
        name = self._ask_name("Duplicate Profile", f"{self.current} copy")
        if name:
            self._store()
            self.data["profiles"][name] = dict(self.data["profiles"][self.current])
            self._refresh_profile_list()
            self._load(name)

    def _rename(self):
        old = self.current
        name = self._ask_name("Rename Profile", old, allow_same=True)
        if name and name != old:
            self._store()
            self.data["profiles"][name] = self.data["profiles"].pop(old)
            if self.data["active"] == old:
                self.data["active"] = name
            self._refresh_profile_list()
            self._load(name)

    def _delete(self):
        if len(self.data["profiles"]) == 1:
            messagebox.showinfo("Delete Profile", "You can't delete the only profile.", parent=self)
            return
        if not messagebox.askyesno("Delete Profile", f"Delete the profile \"{self.current}\"?\n\n"
                                   "Only the settings are removed. Files in storage and in the local "
                                   "folder are not touched.", parent=self):
            return
        del self.data["profiles"][self.current]
        if self.data["active"] not in self.data["profiles"]:
            self.data["active"] = sorted(self.data["profiles"], key=str.lower)[0]
        self._refresh_profile_list()
        self._load(sorted(self.data["profiles"], key=str.lower)[0])

    # ---- actions
    def _browse(self, key):
        title = "Choose the local vault folder" if key == "local_root" else "Choose the storage folder"
        d = filedialog.askdirectory(parent=self, title=title, initialdir=self.vars[key].get() or None)
        if d:
            self.vars[key].set(d)

    def _test(self):
        self._store()
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            Vault(self.data["profiles"][self.current], HashCache()).test_connection()
            where = "folder is writable" if self.backend_var.get() == "folder" else "bucket is reachable"
            messagebox.showinfo("Settings", f"Connected. The {where}.", parent=self)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Settings", f"Connection failed:\n\n{e}", parent=self)
        finally:
            self.config(cursor="")

    def _save(self):
        self._store()
        self.data["active"] = self.current   # the profile shown when saving becomes active
        self.saved = self.data
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
