"""
s3vault_core - storage logic for S3 Vault (no GUI code here).

Bucket layout (under an optional prefix):
    blobs/<sha256>            file contents, content-addressed (identical files stored once)
    commits/<commit-id>.json  one record per check-in: time, user, comment, files + versions
    index/<rel/path>.json     per-file version history

Versioning is done by the app itself, so the bucket does NOT need S3 object
versioning (Garage, for example, doesn't support it).
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
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

# SOLIDWORKS / Office lock files and our own temp files are never checked in.
IGNORED_NAME_PREFIXES = ("~$", ".~")
IGNORED_SUFFIXES = (".s3vault-tmp",)
IGNORED_NAMES = {"thumbs.db", "desktop.ini"}  # Windows Explorer clutter
ERROR_LOG = CONFIG_DIR / "error.log"

DEFAULT_CONFIG = {
    "endpoint_url": "",
    "region": "garage",
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

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
    except (FileNotFoundError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), "utf-8")


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


def local_time(iso: str) -> str:
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return iso or ""


def human_size(n) -> str:
    n = float(n or 0)
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
            if not self.dirty:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), "utf-8")
            os.replace(tmp, self.path)
            self.dirty = False


# ---------------------------------------------------------------- vault

class Vault:
    def __init__(self, cfg: dict, cache: HashCache):
        if not cfg.get("bucket"):
            raise VaultError("No bucket configured. Open Settings.")
        if not cfg.get("local_root"):
            raise VaultError("No local vault folder configured. Open Settings.")
        self.root = Path(cfg["local_root"]).expanduser().resolve()
        if not self.root.is_dir():
            raise VaultError(f"Local vault folder does not exist:\n{self.root}")
        self.bucket = cfg["bucket"]
        self.prefix = (cfg.get("prefix") or "").strip("/")
        self.user = cfg.get("user") or getpass.getuser()
        self.cache = cache
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

    # ---- keys / paths
    def _key(self, *parts: str) -> str:
        return "/".join(([self.prefix] if self.prefix else []) + list(parts))

    def _index_key(self, rel: str) -> str:
        return self._key("index", rel + ".json")

    def rel(self, path) -> str:
        p = Path(path).resolve()
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            raise VaultError(f"This file is outside the vault folder:\n{p}\n\nVault folder: {self.root}")

    def local(self, rel: str) -> Path:
        return self.root / Path(rel)

    # ---- S3 primitives
    @staticmethod
    def _missing(err: ClientError) -> bool:
        return err.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")

    def _get_json(self, key: str):
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if self._missing(e):
                return None
            raise
        return json.loads(obj["Body"].read())

    def _put_json(self, key: str, data) -> None:
        self.s3.put_object(Bucket=self.bucket, Key=key,
                           Body=json.dumps(data, indent=2).encode("utf-8"),
                           ContentType="application/json")

    def _exists(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if self._missing(e):
                return False
            raise

    def _list_keys(self, prefix: str):
        pager = self.s3.get_paginator("list_objects_v2")
        for page in pager.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def test_connection(self) -> None:
        self.s3.head_bucket(Bucket=self.bucket)

    # ---- reading history
    def get_index(self, rel: str):
        return self._get_json(self._index_key(rel))

    def list_tracked(self) -> list[str]:
        pfx = self._key("index") + "/"
        rels = [k[len(pfx):-5] for k in self._list_keys(pfx) if k.endswith(".json")]
        return sorted(rels, key=str.lower)

    def load_indexes(self, rels) -> dict:
        with ThreadPoolExecutor(8) as pool:
            return dict(zip(rels, pool.map(self.get_index, rels)))

    def list_commits(self) -> list[dict]:
        keys = [k for k in self._list_keys(self._key("commits") + "/") if k.endswith(".json")]
        with ThreadPoolExecutor(8) as pool:
            commits = [c for c in pool.map(self._get_json, keys) if c]
        return sorted(commits, key=lambda c: c["id"], reverse=True)

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
        progress("Reading file list")
        tracked = self.list_tracked()
        indexes = self.load_indexes(tracked)
        rows = []
        for i, rel in enumerate(tracked, 1):
            progress(f"Checking {rel} ({i}/{len(tracked)})")
            st, sha = self.status(rel, indexes[rel])
            rows.append({"path": rel, "status": st, "sha": sha, "index": indexes[rel]})
        known = {r.lower() for r in tracked}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if is_ignored(name) or name.startswith("."):
                    continue
                rel = Path(dirpath, name).relative_to(self.root).as_posix()
                if rel.lower() not in known:
                    rows.append({"path": rel, "status": "untracked", "sha": None, "index": None})
        return sorted(rows, key=lambda r: r["path"].lower())

    # ---- writing
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

        now = utc_stamp()
        commit_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:6]
        files = []
        for i, (p, rel, sha, index) in enumerate(items, 1):
            progress(f"Uploading {rel} ({i}/{len(items)})")
            blob = self._key("blobs", sha)
            if not self._exists(blob):
                self.s3.upload_file(str(p), self.bucket, blob)
                # If the file was saved again mid-upload, the blob won't match its name.
                if self.cache.sha256(p) != sha:
                    self.s3.delete_object(Bucket=self.bucket, Key=blob)
                    raise VaultError(f"{rel} changed while it was uploading. Check in again.")
            files.append({"path": rel, "version": len(index["versions"]) + 1,
                          "sha256": sha, "size": p.stat().st_size})

        commit = {"id": commit_id, "time": now, "user": self.user,
                  "host": socket.gethostname(), "comment": comment, "files": files}
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

    def _update_current(self, rel: str, sha: str) -> None:
        """Server-side copy of a blob to current/<real path> (no re-upload).
        Uses a single CopyObject request (works up to 5 GB) rather than boto3's
        managed multipart copy, whose UploadPartCopy handling fails on Garage."""
        self.s3.copy_object(Bucket=self.bucket, Key=self._key("current", rel),
                            CopySource={"Bucket": self.bucket, "Key": self._key("blobs", sha)})

    def rebuild_current(self, progress=lambda msg: None) -> int:
        """Re-creates current/ from the version history (e.g. for files checked in
        before this feature existed). Returns the number of files written."""
        progress("Reading file list")
        tracked = self.list_tracked()
        indexes = self.load_indexes(tracked)
        n = 0
        for rel in tracked:
            index = indexes[rel]
            if index and index.get("versions"):
                progress(f"Updating current/{rel}")
                self._update_current(rel, index["versions"][-1]["sha256"])
                n += 1
        return n

    def restore(self, rel: str, version: dict, dest=None, progress=lambda msg: None) -> Path:
        """Downloads a version to dest (default: its place in the working folder)."""
        dest = Path(dest) if dest else self.local(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".s3vault-tmp")
        progress(f"Downloading {rel} v{version['version']}")
        self.s3.download_file(self.bucket, self._key("blobs", version["sha256"]), str(tmp))
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
        progress("Reading file list")
        tracked = self.list_tracked()
        indexes = self.load_indexes(tracked)
        updated, conflicts, errors = [], [], []
        for rel in tracked:
            index = indexes[rel]
            if not index or not index.get("versions"):
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
