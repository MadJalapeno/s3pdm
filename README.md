# S3 Vault

Manual, versioned check-in of files to S3-compatible storage (Backblaze B2, Garage, Wasabi, AWS …)
or to a folder (network drive, NAS, USB drive, or a folder synced by Sync.com / Box Drive / OneDrive).
Nothing syncs automatically: you pick files, write a comment, and each check-in creates a new version.
Runs on Windows and macOS.

## Install

1. Install Python 3.11+ from python.org (it includes Tkinter).
2. `python -m pip install -r requirements.txt`
3. Run: `python s3vault_gui.py` (on Windows, `pythonw` hides the console once it's working)

Standalone app: `python -m pip install pyinstaller`, then
`python -m PyInstaller --onefile --windowed --name S3Vault s3vault_gui.py` → `dist/`.

## Profiles

Each profile is a complete set of settings: storage (S3 or folder), prefix, local vault folder and
your name. Switch profiles with the **Profile** dropdown in the toolbar. In **Settings** you can
create, duplicate, rename and delete profiles; the profile shown when you click Save becomes active.
Deleting a profile only removes its settings, never any files.

Existing settings from earlier versions are converted into a profile called "Default".

## Folder / network drive storage

Choose **Folder / network drive** in Settings and pick the storage folder, e.g.
`\\server\share\vaults` on Windows or `/Volumes/share/vaults` on macOS. The layout inside it is
identical to the bucket layout. Every write goes to a temporary `.part` file first and is then
renamed, so a dropped connection never leaves a half-written file.

- The storage folder and the local vault folder must be separate (neither inside the other).
- Several projects can share one storage folder by giving each profile a different prefix.
- With a sync-client folder (Sync.com, Box Drive, OneDrive), a check-in is only off your machine
  once the sync client has finished uploading it.

## Backblaze setup

Create a private bucket and an Application Key restricted to it. In Settings:
endpoint `https://s3.<region>.backblazeb2.com`, region e.g. `us-east-005`, plus the key ID and key.

## Using it

The Files tab is a folder tree with sizes and a status for every file. Folders show a summary
(e.g. "2 modified, 1 untracked"), are coloured by the most important status inside, and show total size.

- **Check In…** – pick files with a file dialog. A comment is required.
- **Check In Selected** – checks in the modified/untracked files you've selected. Select a folder to
  check in everything that changed inside it (untracked files only if "Show untracked" is on).
- **History…** / double-click a file – every version; restore one or save a copy.
- **Rename/Move…** – one file or one folder. Enter the new path relative to the vault folder.
  - If the file still has its old name, the app moves it locally and moves its history.
  - If you already renamed it (SOLIDWORKS Rename / Pack and Go), enter the new name and only the
    history is moved ("linked").
  - Renaming a folder moves everything in it, including untracked files.
- **Untrack…** – stop tracking files or whole folders. Their history is kept in the bucket's
  `archive/`, and you can optionally delete the local copies. Checking the same path in again
  later starts a fresh history.
- **Get Latest** – downloads newer versions of tracked files. Local changes that aren't checked in
  are never overwritten.
- **Right-click** a file or folder for the same actions, plus Open and Show in Folder.
- **Log** tab – every check-in, rename and untrack, with the files involved.
- **More ▾** – open the vault folder, expand/collapse all, rebuild current/, reload history,
  recheck all local files, settings.
- The list refreshes automatically when you switch back to the app (at most every 10 seconds).

## Speed: local history cache

History is cached locally (`history/` in the settings folder). A refresh lists the bucket and
downloads only index records that changed and log entries it hasn't seen, so it stays fast as the
history grows. **More ▾ → Reload History from Bucket** rebuilds the cache from scratch.

## Storage layout

    <prefix>/blobs/<sha256>              file contents, stored once per unique content
    <prefix>/commits/<id>.json           one per action: check-in, rename or untrack
    <prefix>/index/<path>.json           version history for each tracked file
    <prefix>/current/<path>              latest version of each tracked file under its real name
    <prefix>/archive/<path>.<id>.json    history of untracked files

## Notes and limits

- No locking; designed for one user (or people who coordinate).
- Renaming SOLIDWORKS files here doesn't update references in assemblies and drawings. Rename with
  SOLIDWORKS first, then use Rename/Move… to link the history.
- The app never deletes file contents from the bucket.
- Settings (including the secret key) are stored in plain text in the settings folder
  (`%APPDATA%\S3Vault` on Windows, `~/Library/Application Support/S3Vault` on macOS).
- Lock files (`~$…`), Thumbs.db, desktop.ini and .DS_Store are ignored.
- The current/ copy uses a single request, which works for files up to 5 GB.
- Errors are logged to `error.log` in the settings folder.


