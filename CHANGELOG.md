# Changelog

All notable changes to S3 Vault. Versions follow [Semantic Versioning](https://semver.org):
MAJOR for changes that break compatibility with existing vaults or settings, MINOR for new
features, PATCH for fixes.

## [1.0.3] – 2026-09-28

### Fixed
- Switching to a profile whose S3 endpoint had no `https://` failed silently: the new settings
  were loaded but the previous profile stayed connected, so its files kept showing. Endpoints
  without a scheme now get `https://` added automatically.
- If a profile can't be connected, no vault stays connected (previously the old one did), the
  title shows the profile name and a clear error explains what's wrong.
- Unexpected errors in any button, menu or event handler are now shown and written to
  `error.log` instead of disappearing silently when running with `pythonw` or as a packaged app.

## [1.0.2] – 2026-09-28

### Fixed
- Choosing a profile from the toolbar dropdown could be ignored on some platforms, leaving the
  previous profile's files on screen. The switch now follows the dropdown's value directly.

### Added
- About shows the active profile, the local folder in use and the storage location.

## [1.0.1] – 2026-09-28

### Fixed
- After switching profiles (or saving Settings), the file list could show the previous profile's
  files: an auto-refresh started for the old profile finished after the switch. Results from a
  previous profile are now discarded, and a refresh requested while another task is running is
  queued instead of dropped.
- Switching profile or opening Settings no longer has to wait for a refresh to finish.

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
