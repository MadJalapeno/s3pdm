"""
s3vault_core - storage logic for S3 Vault (no GUI code here).

Bucket layout (under an optional prefix):
    blobs/<sha256>                  file contents, content-addressed (identical files stored once)
    commits/<commit-id>.json        one record per action: check-in, rename or untrack
    index/<rel/path>.json           version history of each tracked file
    current/<rel/path>              latest version of each tracked file under its real name
    archive/<rel/path>.<id>.json    history of files that were untracked (kept, never deleted)

Versioning is done by the app itself, so the bucket does NOT need S3 object
versioning. A local cache of the history means a refresh only downloads what changed.
"""
from __future__ import annotations

__version__ = "1.0.0"
APP_URL = "https://s3pdm.com"

import getpass
import hashlib
import json
import os
import shutil
import socket
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

if os.environ.get("APPDATA"):                      # Windows
    CONFIG_DIR = Path(os.environ["APPDATA"]) / "S3Vault"
elif sys.platform == "darwin":                     # macOS
    CONFIG_DIR = Path.home() / "Library" / "Application Support" / "S3Vault"
else:                                              # Linux
    CONFIG_DIR = Path.home() / ".config" / "S3Vault"
CONFIG_FILE = CONFIG_DIR / "config.json"
HASH_CACHE_FILE = CONFIG_DIR / "hashcache.json"
HISTORY_DIR = CONFIG_DIR / "history"
ERROR_LOG = CONFIG_DIR / "error.log"

# Lock files, Explorer clutter and our own temp files are never checked in.
IGNORED_NAME_PREFIXES = ("~$", ".~")
IGNORED_SUFFIXES = (".s3vault-tmp",)
IGNORED_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

DEFAULT_CONFIG = {
    "backend": "s3",          # "s3" or "folder"
    "storage_path": "",       # folder backend: network drive / NAS / USB / synced folder
    "endpoint_url": "",
    "region": "",
    "bucket": "",
    "prefix": "",
    "access_key": "",
    "secret_key": "",
    "local_root": "",
    "user": getpass.getuser(),
}


class VaultError(Exception):
    """An error meant to be shown to the user as-is."""


# ---------------------------------------------------------------- helpers

def load_profiles() -> dict:
    """Returns {"active": name, "profiles": {name: settings}}.
    An old single-settings config.json is converted into a profile called "Default"."""
    try:
        raw = json.loads(CONFIG_FILE.read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        raw = {}
    if "profiles" not in raw:
        old = dict(DEFAULT_CONFIG)
        old.update(raw)
        raw = {"active": "Default", "profiles": {"Default": old}}
    profiles = {name: {**DEFAULT_CONFIG, **cfg} for name, cfg in raw["profiles"].items()} \
        or {"Default": dict(DEFAULT_CONFIG)}
    active = raw.get("active") if raw.get("active") in profiles else sorted(profiles)[0]
    return {"active": active, "profiles": profiles}


def save_profiles(data: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), "utf-8")


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), "utf-8")
    os.replace(tmp, path)


def is_ignored(name: str) -> bool:
    return (name.startswith(IGNORED_NAME_PREFIXES) or name.endswith(IGNORED_SUFFIXES)
            or name.lower() in IGNORED_NAMES)


def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_commit_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:6]


def local_time(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def human_size(n) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


class HashCache:
    """Remembers file hashes keyed by (size, mtime) so status checks don't re-read big files."""

    def __init__(self, path=HASH_CACHE_FILE):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.dirty = False
        try:
            self.data = json.loads(self.path.read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            self.data = {}

    def sha256(self, path) -> str:
        path = Path(path)
        key = str(path.resolve())
        st = path.stat()
        with self.lock:
            entry = self.data.get(key)
        if entry and entry[0] == st.st_size and entry[1] == st.st_mtime_ns:
            return entry[2]
        digest = sha256_file(path)
        st2 = path.stat()
        if (st2.st_size, st2.st_mtime_ns) == (st.st_size, st.st_mtime_ns):
            with self.lock:
                self.data[key] = [st.st_size, st.st_mtime_ns, digest]
                self.dirty = True
        return digest

    def save(self) -> None:
        with self.lock:
            if self.dirty:
                _atomic_write_json(self.path, self.data)
                self.dirty = False


class HistoryCache:
    """Local copy of the bucket's index and commit records, one file per vault."""

    def __init__(self, path: Path):
        self.path = path
        try:
            d = json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, ValueError):
            d = {}
        self.indexes = d.get("indexes", {})
        self.etags = d.get("etags", {})
        self.commits = d.get("commits", {})

    def save(self) -> None:
        _atomic_write_json(self.path, {"indexes": self.indexes, "etags": self.etags,
                                       "commits": self.commits})

    def clear(self) -> None:
        self.indexes, self.etags, self.commits = {}, {}, {}
        self.path.unlink(missing_ok=True)


# ---------------------------------------------------------------- vault

class S3Storage:
    """S3-compatible object storage (Backblaze B2, Garage, Wasabi, AWS …)."""

    def __init__(self, cfg: dict):
        if not cfg.get("bucket"):
            raise VaultError("No bucket configured. Open Settings.")
        self.bucket = cfg["bucket"]
        self.ident = f"s3|{cfg.get('endpoint_url', '')}|{self.bucket}"
        self.s3 = boto3.client(
            "s3",
            endpoint_url=cfg.get("endpoint_url") or None,
            region_name=cfg.get("region") or None,
            aws_access_key_id=cfg.get("access_key") or None,
            aws_secret_access_key=cfg.get("secret_key") or None,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path"},
                retries={"max_attempts": 5, "mode": "standard"},
                connect_timeout=10,
                # boto3 >= 1.36 adds checksums by default, which some
                # S3-compatible servers reject. Only send them when required.
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    @staticmethod
    def _missing(err: ClientError) -> bool:
        return err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")

    def get(self, key):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if self._missing(e):
                return None, None
            raise
        return obj["Body"].read(), obj.get("ETag")

    def put(self, key, data: bytes):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=data)

    def delete(self, key):
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if self._missing(e):
                return False
            raise

    def list(self, prefix):
        """Yields (key, etag) for every object under prefix."""
        pager = self.s3.get_paginator("list_objects_v2")
        for page in pager.paginate(Bucket=self.bucket, Prefix=prefix):
            for o in page.get("Contents", []):
                yield o["Key"], o.get("ETag")

    def upload_file(self, path, key):
        self.s3.upload_file(str(path), self.bucket, key)

    def download_file(self, key, path):
        self.s3.download_file(self.bucket, key, str(path))

    def copy(self, src_key, dst_key):
        # Single server-side CopyObject request (works up to 5 GB).
        self.s3.copy_object(Bucket=self.bucket, Key=dst_key,
                            CopySource={"Bucket": self.bucket, "Key": src_key})

    def test(self):
        self.s3.head_bucket(Bucket=self.bucket)


class FolderStorage:
    """Stores the vault in an ordinary folder: a network drive, NAS share, USB drive,
    or a folder that a sync client (Sync.com, Box Drive, OneDrive …) uploads."""

    def __init__(self, cfg: dict):
        path = (cfg.get("storage_path") or "").strip()
        if not path:
            raise VaultError("No storage folder configured. Open Settings.")
        self.base = Path(path).expanduser()
        if not self.base.is_dir():
            raise VaultError(f"The storage folder isn't available:\n{self.base}\n\n"
                             "Is the network drive connected?")
        self.ident = f"folder|{self.base.resolve()}"

    def _path(self, key) -> Path:
        return self.base.joinpath(*key.split("/"))

    @staticmethod
    def _etag(st) -> str:
        return f"{st.st_size}-{st.st_mtime_ns}"

    def _write_atomic(self, dest: Path, writer):
        """Write to a temporary file first, so a dropped connection never leaves a half-written file."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f"{dest.name}.{uuid.uuid4().hex[:8]}.part")
        try:
            writer(tmp)
            os.replace(tmp, dest)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def get(self, key):
        p = self._path(key)
        try:
            data = p.read_bytes()
            st = p.stat()
        except FileNotFoundError:
            return None, None
        return data, self._etag(st)

    def put(self, key, data: bytes):
        self._write_atomic(self._path(key), lambda t: t.write_bytes(data))

    def delete(self, key):
        p = self._path(key)
        p.unlink(missing_ok=True)
        parent = p.parent          # tidy up folders left empty
        while parent != self.base and self.base in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def exists(self, key) -> bool:
        return self._path(key).is_file()

    def list(self, prefix):
        folder = self._path(prefix.rstrip("/")) if prefix.strip("/") else self.base
        if not folder.is_dir():
            return
        for dirpath, _, files in os.walk(folder):
            for name in files:
                if name.endswith(".part"):
                    continue
                full = Path(dirpath, name)
                try:
                    st = full.stat()
                except FileNotFoundError:
                    continue
                yield full.relative_to(self.base).as_posix(), self._etag(st)

    def upload_file(self, path, key):
        self._write_atomic(self._path(key), lambda t: shutil.copyfile(path, t))

    def download_file(self, key, path):
        shutil.copyfile(self._path(key), path)

    def copy(self, src_key, dst_key):
        self._write_atomic(self._path(dst_key), lambda t: shutil.copyfile(self._path(src_key), t))

    def test(self):
        probe = self.base / f".s3vault-probe-{uuid.uuid4().hex[:8]}"
        probe.write_bytes(b"ok")
        probe.unlink()


def make_storage(cfg: dict):
    return FolderStorage(cfg) if cfg.get("backend") == "folder" else S3Storage(cfg)


class Vault:
    def __init__(self, cfg: dict, cache: HashCache):
        if not cfg.get("local_root"):
            raise VaultError("No local vault folder configured. Open Settings.")
        self.root = Path(cfg["local_root"]).expanduser().resolve()
        if not self.root.is_dir():
            raise VaultError(f"Local vault folder does not exist:\n{self.root}")
        self.store = make_storage(cfg)
        if isinstance(self.store, FolderStorage):
            b = self.store.base.resolve()
            if b == self.root or self.root in b.parents or b in self.root.parents:
                raise VaultError("The storage folder and the local vault folder must be separate "
                                 "(neither inside the other).")
        self.prefix = (cfg.get("prefix") or "").strip("/")
        self.user = cfg.get("user") or getpass.getuser()
        self.cache = cache
        ident = f"{self.store.ident}|{self.prefix}"
        self.hist = HistoryCache(HISTORY_DIR / (hashlib.sha1(ident.encode()).hexdigest()[:16] + ".json"))

    # ---- keys / paths
    def _key(self, *parts: str) -> str:
        return "/".join(([self.prefix] if self.prefix else []) + list(parts))

    def _index_key(self, rel: str) -> str:
        return self._key("index", rel + ".json")

    def _current_key(self, rel: str) -> str:
        return self._key("current", rel)

    def rel(self, path) -> str:
        p = Path(path).resolve()
        try:
            rel = p.relative_to(self.root).as_posix()
        except ValueError:
            raise VaultError(f"This is outside the vault folder:\n{p}\n\nVault folder: {self.root}")
        if rel in ("", "."):
            raise VaultError("That is the vault folder itself.")
        return rel

    def local(self, rel: str) -> Path:
        return self.root / Path(rel)

    # ---- storage primitives
    def _get_json_etag(self, key: str):
        data, etag = self.store.get(key)
        return (json.loads(data), etag) if data is not None else (None, None)

    def _get_json(self, key: str):
        return self._get_json_etag(key)[0]

    def _put_json(self, key: str, data) -> None:
        self.store.put(key, json.dumps(data, indent=2).encode("utf-8"))

    def _delete(self, key: str) -> None:
        self.store.delete(key)

    def _exists(self, key: str) -> bool:
        return self.store.exists(key)

    def test_connection(self) -> None:
        self.store.test()

    # ---- history (cached locally)
    def get_index(self, rel: str):
        """Always reads from the bucket (used before writing)."""
        return self._get_json(self._index_key(rel))

    def sync(self, progress=lambda msg: None) -> None:
        """Brings the local history cache up to date. Only changed records are downloaded:
        index files are compared by ETag, commit records never change once written."""
        h = self.hist
        progress("Checking for changes")
        ipfx = self._key("index") + "/"
        remote = {k[len(ipfx):-5]: etag for k, etag in self.store.list(ipfx) if k.endswith(".json")}
        for rel in [r for r in h.indexes if r not in remote]:
            h.indexes.pop(rel, None)
            h.etags.pop(rel, None)
        changed = [r for r, etag in remote.items() if r not in h.indexes or h.etags.get(r) != etag]
        if changed:
            progress(f"Downloading history for {len(changed)} file(s)")
            with ThreadPoolExecutor(8) as pool:
                results = pool.map(lambda r: self._get_json_etag(self._index_key(r)), changed)
                for rel, (data, etag) in zip(changed, results):
                    if data is None:
                        h.indexes.pop(rel, None)
                        h.etags.pop(rel, None)
                    else:
                        h.indexes[rel] = data
                        h.etags[rel] = etag

        cpfx = self._key("commits") + "/"
        ids = [k[len(cpfx):-5] for k, _ in self.store.list(cpfx) if k.endswith(".json")]
        new = [i for i in ids if i not in h.commits]
        if new:
            progress(f"Downloading {len(new)} log entries")
            with ThreadPoolExecutor(8) as pool:
                for cid, data in zip(new, pool.map(lambda i: self._get_json(self._key("commits", i + ".json")), new)):
                    if data:
                        h.commits[cid] = data
        h.save()

    def tracked(self) -> list[str]:
        return sorted(self.hist.indexes, key=str.lower)

    def commits(self) -> list[dict]:
        return sorted(self.hist.commits.values(), key=lambda c: c["id"], reverse=True)

    def clear_history_cache(self) -> None:
        self.hist.clear()

    # ---- local status
    def status(self, rel: str, index) -> tuple[str, str | None]:
        """Returns (status, local sha256 or None)."""
        p = self.local(rel)
        if not p.exists():
            return ("missing" if index else "untracked"), None
        try:
            sha = self.cache.sha256(p)
        except OSError:
            return "unreadable", None
        if not index or not index.get("versions"):
            return "untracked", sha
        versions = index["versions"]
        if sha == versions[-1]["sha256"]:
            return "up to date", sha
        if any(v["sha256"] == sha for v in versions):
            return "older version", sha
        return "modified", sha

    def scan(self, progress=lambda msg: None) -> list[dict]:
        """All tracked files plus untracked files found in the local folder."""
        self.sync(progress)
        rows = []
        tracked = self.tracked()
        for i, rel in enumerate(tracked, 1):
            index = self.hist.indexes[rel]
            progress(f"Checking {rel} ({i}/{len(tracked)})")
            st, sha = self.status(rel, index)
            try:
                size = self.local(rel).stat().st_size
            except OSError:
                versions = index.get("versions") or []
                size = versions[-1]["size"] if versions else None
            rows.append({"path": rel, "status": st, "sha": sha, "index": index, "size": size})
        known = {r.lower() for r in tracked}
        progress("Looking for new files")
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if is_ignored(name) or name.startswith("."):
                    continue
                full = Path(dirpath, name)
                rel = full.relative_to(self.root).as_posix()
                if rel.lower() not in known:
                    try:
                        size = full.stat().st_size
                    except OSError:
                        size = None
                    rows.append({"path": rel, "status": "untracked", "sha": None, "index": None, "size": size})
        return sorted(rows, key=lambda r: r["path"].lower())

    # ---- check in
    def check_in(self, paths, comment: str, progress=lambda msg: None):
        """Creates one commit containing a new version of each changed file.
        Returns (commit or None, list of unchanged rel paths that were skipped)."""
        comment = (comment or "").strip()
        if not comment:
            raise VaultError("A comment is required.")

        items, skipped = [], []
        for path in paths:
            p = Path(path)
            rel = self.rel(p)
            if not p.is_file():
                raise VaultError(f"Not a file: {p}")
            if is_ignored(p.name):
                continue
            progress(f"Hashing {rel}")
            sha = self.cache.sha256(p)
            index = self.get_index(rel) or {"path": rel, "versions": []}
            if index["versions"] and index["versions"][-1]["sha256"] == sha:
                skipped.append(rel)
                continue
            items.append((p, rel, sha, index))
        if not items:
            return None, skipped

        now, commit_id = utc_stamp(), new_commit_id()
        files = []
        for i, (p, rel, sha, index) in enumerate(items, 1):
            progress(f"Uploading {rel} ({i}/{len(items)})")
            blob = self._key("blobs", sha)
            if not self._exists(blob):
                self.store.upload_file(p, blob)
                # If the file was saved again mid-upload, the blob won't match its name.
                if self.cache.sha256(p) != sha:
                    self._delete(blob)
                    raise VaultError(f"{rel} changed while it was uploading. Check in again.")
            files.append({"path": rel, "version": len(index["versions"]) + 1,
                          "sha256": sha, "size": p.stat().st_size})

        commit = {"id": commit_id, "action": "checkin", "time": now, "user": self.user,
                  "host": socket.gethostname(), "app_version": __version__, "comment": comment, "files": files}
        progress("Writing history")
        self._put_json(self._key("commits", commit_id + ".json"), commit)
        for (p, rel, sha, index), f in zip(items, files):
            index["versions"].append({"version": f["version"], "sha256": sha, "size": f["size"],
                                      "time": now, "user": self.user, "comment": comment,
                                      "commit": commit_id})
            self._put_json(self._index_key(rel), index)
            progress(f"Updating current/{rel}")
            self._update_current(rel, sha)
        return commit, skipped

    # ---- untrack
    def untrack(self, rels, comment: str, delete_local=False, progress=lambda msg: None):
        """Stops tracking files. Their history is archived in the bucket, never deleted.
        Returns (commit or None, list of local-delete errors)."""
        comment = (comment or "").strip()
        if not comment:
            raise VaultError("A comment is required.")
        now, commit_id = utc_stamp(), new_commit_id()
        items = []
        for rel in rels:
            progress(f"Archiving history of {rel}")
            index = self.get_index(rel)
            if not index:
                continue
            self._put_json(self._key("archive", f"{rel}.{commit_id}.json"), index)
            versions = index.get("versions") or []
            items.append({"path": rel, "version": versions[-1]["version"] if versions else 0})
        if not items:
            return None, []
        commit = {"id": commit_id, "action": "untrack", "time": now, "user": self.user,
                  "host": socket.gethostname(), "app_version": __version__, "comment": comment, "files": items}
        self._put_json(self._key("commits", commit_id + ".json"), commit)
        errors = []
        for f in items:
            rel = f["path"]
            progress(f"Untracking {rel}")
            self._delete(self._index_key(rel))
            self._delete(self._current_key(rel))
            if delete_local:
                try:
                    self.local(rel).unlink(missing_ok=True)
                except OSError as e:
                    errors.append(f"{rel}: {e}")
        return commit, errors

    # ---- rename / move
    def rename(self, pairs, comment: str, progress=lambda msg: None):
        """Renames or moves tracked files, keeping their history.
        pairs: [(old_rel, new_rel), ...]. For each file:
          - old exists locally, new doesn't: the local file is moved too;
          - old is gone, new exists (already renamed, e.g. in SOLIDWORKS): history is linked to it.
        Returns the commit."""
        comment = (comment or "").strip()
        if not comment:
            raise VaultError("A comment is required.")
        plan = []
        for old, new in pairs:
            if old == new:
                continue
            index = self.get_index(old)
            if not index:
                raise VaultError(f"{old} is not tracked.")
            if self.get_index(new):
                raise VaultError(f"{new} is already tracked.")
            o, n = self.local(old), self.local(new)
            o_exists, n_exists = o.exists(), n.exists()
            same_file = o_exists and n_exists and os.path.samefile(o, n)  # case-only rename
            if o_exists and n_exists and not same_file:
                raise VaultError(f"Both of these exist locally, so I can't tell which is which:\n{old}\n{new}")
            if not o_exists and not n_exists:
                raise VaultError(f"Neither of these exists locally:\n{old}\n{new}")
            plan.append((old, new, index, o_exists))
        if not plan:
            raise VaultError("Nothing to rename.")

        # Move local files first (the step most likely to fail, e.g. file open in SOLIDWORKS).
        moved = []
        try:
            for old, new, _, move in plan:
                if move:
                    progress(f"Moving {old}")
                    self.local(new).parent.mkdir(parents=True, exist_ok=True)
                    os.rename(self.local(old), self.local(new))
                    moved.append((old, new))
        except OSError as e:
            for old, new in reversed(moved):
                try:
                    os.rename(self.local(new), self.local(old))
                except OSError:
                    pass
            raise VaultError(f"Couldn't move a local file:\n{e}\n\nIs it open in SOLIDWORKS or another program?")

        now, commit_id = utc_stamp(), new_commit_id()
        files = []
        for old, new, index, _ in plan:
            progress(f"Moving history {old} → {new}")
            index["path"] = new
            index.setdefault("renames", []).append({"from": old, "to": new, "time": now, "commit": commit_id})
            self._put_json(self._index_key(new), index)
            versions = index.get("versions") or []
            files.append({"path": new, "from": old, "version": versions[-1]["version"] if versions else 0})
        commit = {"id": commit_id, "action": "rename", "time": now, "user": self.user,
                  "host": socket.gethostname(), "app_version": __version__, "comment": comment, "files": files}
        self._put_json(self._key("commits", commit_id + ".json"), commit)
        for old, new, index, _ in plan:
            self._delete(self._index_key(old))
            versions = index.get("versions") or []
            if versions:
                self._update_current(new, versions[-1]["sha256"])
            self._delete(self._current_key(old))
        return commit

    def rename_folder(self, old_dir: str, new_dir: str, comment: str, progress=lambda msg: None):
        """Moves a whole local folder (including untracked files) and the history of
        every tracked file inside it."""
        old_dir, new_dir = old_dir.strip("/"), new_dir.strip("/")
        if not old_dir or not new_dir or old_dir == new_dir:
            raise VaultError("Nothing to rename.")
        if (new_dir + "/").lower().startswith(old_dir.lower() + "/"):
            raise VaultError("A folder can't be moved inside itself.")
        o, n = self.local(old_dir), self.local(new_dir)
        same = o.exists() and n.exists() and os.path.samefile(o, n)
        if o.exists() and (not n.exists() or same):
            progress(f"Moving folder {old_dir}")
            n.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.rename(o, n)
            except OSError as e:
                raise VaultError(f"Couldn't move the folder:\n{e}\n\nIs a file in it open in another program?")
        elif o.exists() and n.exists():
            raise VaultError(f"A folder named {new_dir} already exists.")
        elif not n.exists():
            raise VaultError(f"Neither folder exists locally:\n{old_dir}\n{new_dir}")
        self.sync(progress)
        pfx = old_dir + "/"
        pairs = [(r, new_dir + "/" + r[len(pfx):]) for r in self.tracked() if r.startswith(pfx)]
        if not pairs:
            return None
        return self.rename(pairs, comment, progress)

    # ---- current/ mirror
    def _update_current(self, rel: str, sha: str) -> None:
        """Copy of a blob to current/<real path> (server-side on S3, no re-upload)."""
        self.store.copy(self._key("blobs", sha), self._current_key(rel))

    def rebuild_current(self, progress=lambda msg: None) -> int:
        self.sync(progress)
        n = 0
        for rel in self.tracked():
            versions = self.hist.indexes[rel].get("versions") or []
            if versions:
                progress(f"Updating current/{rel}")
                self._update_current(rel, versions[-1]["sha256"])
                n += 1
        return n

    # ---- restore / get latest
    def restore(self, rel: str, version: dict, dest=None, progress=lambda msg: None) -> Path:
        """Downloads a version to dest (default: its place in the working folder)."""
        dest = Path(dest) if dest else self.local(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".s3vault-tmp")
        progress(f"Downloading {rel} v{version['version']}")
        self.store.download_file(self._key("blobs", version["sha256"]), tmp)
        if sha256_file(tmp) != version["sha256"]:
            tmp.unlink(missing_ok=True)
            raise VaultError(f"Downloaded data for {rel} failed its integrity check.")
        try:
            os.replace(tmp, dest)
        except PermissionError:
            tmp.unlink(missing_ok=True)
            raise VaultError(f"Couldn't replace {dest.name}.\nIs it open in SOLIDWORKS or another program?")
        return dest

    def get_latest(self, progress=lambda msg: None):
        """Downloads the latest version of tracked files that are missing or older.
        Files with uncommitted local changes are left alone."""
        self.sync(progress)
        updated, conflicts, errors = [], [], []
        for rel in self.tracked():
            index = self.hist.indexes[rel]
            if not index.get("versions"):
                continue
            st, _ = self.status(rel, index)
            if st == "modified":
                conflicts.append(rel)
            elif st in ("missing", "older version"):
                try:
                    self.restore(rel, index["versions"][-1], progress=progress)
                    updated.append(rel)
                except (VaultError, OSError, ClientError) as e:
                    errors.append(f"{rel}: {e}")
        return updated, conflicts, errors
