# Changelog

All notable changes to S3 Vault. Versions follow [Semantic Versioning](https://semver.org):
MAJOR for changes that break compatibility with existing vaults or settings, MINOR for new
features, PATCH for fixes.

## [1.0.0] – 2026-09-28

First versioned release.

### Features
- Manual check-in with a required comment; each check-in creates a new version of every changed file.
- Storage backends: S3-compatible (Backblaze B2, Garage, Wasabi, AWS) and folder / network drive.
- Profiles: separate settings per project, switchable from the toolbar.
- Folder tree with status, sizes and folder summaries; check in whole folders.
- Version history per file: restore to the working folder or save a copy.
- Rename/move files and folders with history kept, including linking after a SOLIDWORKS rename.
- Untrack files and folders (history archived, never deleted).
- Get Latest for a second machine, never overwriting local changes that aren't checked in.
- `current/` copy of the latest version of every file under its real name.
- Local history cache so refreshes only download what changed.
- Auto-refresh when switching back to the app; Check In Selected re-checks files on disk.
- Log of every check-in, rename and untrack; each record notes the app version that wrote it.
- Windows and macOS.
