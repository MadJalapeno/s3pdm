# S3 Vault vs SOLIDWORKS PDM Standard

*Compared: S3 Vault 1.0.3 and SOLIDWORKS PDM Standard 2025/2026. September 2026.*

## Summary

**SOLIDWORKS PDM Standard** is a real product data management system: reference-aware check-in and
check-out inside SOLIDWORKS, a searchable database, workflows and revision control. The cost of that
capability is infrastructure. It needs a Windows server running SQL Server Express on a fast local
network, it's administered as a server product, and it's only included with the higher SOLIDWORKS
license tiers.

**S3 Vault** is a small, deliberately simple tool for one person, or a few who coordinate, who want
the core benefit of a PDM: a safe, versioned, commented history of every file, stored off the
machine. It needs no server at all. Storage is a cloud bucket or a folder, and the app runs on
Windows and macOS.

In short: PDM Standard manages *SOLIDWORKS data*. S3 Vault manages *files*. For a solo designer
whose main need is "never lose work, know what changed, get any version back," S3 Vault covers that
without a server. Once a team needs reference-aware check-in, approvals or controlled revisions,
PDM Standard (or PDM Professional) is the right tool.

## At a glance

| | S3 Vault | SOLIDWORKS PDM Standard |
|---|---|---|
| Intended for | 1 user, or a few who coordinate | Small teams, fewer than ~10 users, in one location |
| Server required | No | Yes: archive server, database server and SQL Server Express |
| Server OS | None | Windows Server (desktop Windows possible but not recommended for production) |
| Database | None: history is plain JSON files in storage | Microsoft SQL Server Express |
| Storage | Any S3-compatible bucket (Backblaze B2, Wasabi, AWS, self-hosted Garage) or a folder / network drive / NAS / synced folder | Local disks on the archive server (network storage not supported) |
| Client OS | Windows and macOS | Windows only |
| Works away from the office | Yes, anywhere with internet (S3) or access to the share (folder) | Designed for LAN use; remote sites need replication, which is PDM Professional only |
| SOLIDWORKS integration | None (works on files) | Add-in inside SOLIDWORKS, plus Windows Explorer integration |
| Reference awareness | No | Yes: checks in assemblies with their parts and drawings, tracks where-used |
| Licensing | Free, open source code you own | Included with SOLIDWORKS Professional, Premium and Ultimate |
| Running cost | Storage only, typically a few dollars per TB per month | Server hardware, a Windows Server license, power, backups and admin time |
| Setup time | Minutes | Hours to days, often with reseller help |

## Infrastructure

### PDM Standard

A PDM installation has three server components, per GoEngineer's system resources guide:

- **Archive server:** holds the vault files, sends files to and from clients, and validates logins.
- **Database server:** a helper service that processes jobs such as notifications.
- **SQL Server:** stores all the metadata and does most of the work. It's the component that
  determines performance.

GoEngineer's starting-point recommendations for a small, combined archive and SQL server are **32 GB
of RAM and at least 500 GB of disk**, with the archive on **fast, locally attached SSDs**. Network
storage such as a SAN isn't supported for the archive. They also recommend that the server be
**dedicated to PDM** and not host other applications.

PDM Standard uses **SQL Server Express**, which brings hard limits regardless of the hardware:

- a maximum database size of 10 GB per database for SQL 2022 and earlier (50 GB for SQL 2025 and later),
- 1 GB of RAM per SQL instance,
- at most 1 CPU socket or 4 cores.

These limits are why PDM Standard is aimed at small environments of fewer than about 10 users with
modest activity.

**Network:** clients are expected to be on the same LAN as the archive server, over gigabit
connections, with latency under roughly 100 ms. Remote sites need replication, which requires PDM
Professional. The server must be a full machine with an installed OS. Hosted SQL services such as AWS
or Azure SQL aren't supported.

**Operating system:** SOLIDWORKS supports Windows Server (2022 or 2025 for the 2026 release) for
PDM server components. Installing them on desktop Windows 11 is technically possible but strongly
discouraged for production. Clients are Windows only.

**Version lock-step:** PDM Standard is only supported with the same SOLIDWORKS release, so the
server has to be upgraded alongside SOLIDWORKS every year.

### S3 Vault

- **No server, no database, nothing to patch or upgrade.**
- Storage is either an S3 bucket (Backblaze B2, Wasabi, AWS, or a self-hosted Garage or MinIO
  cluster) or any folder: a network drive, NAS, USB drive, or a folder synced by Sync.com, Box Drive
  or OneDrive.
- The app is two Python files. It runs on Windows and macOS and can be packaged as a single
  executable.
- A new machine is set up by installing Python, copying the app and entering the storage settings.
  Get Latest then restores every file.
- History is stored as plain JSON files beside the file contents, so it stays readable even without
  the app. The latest version of every file is also kept under its real name in `current/`.

## Features

| Capability | S3 Vault | PDM Standard |
|---|---|---|
| Versioned history of every file | ✔ | ✔ |
| Required comment on check-in | ✔ | ✔ (configurable) |
| Restore or save any old version | ✔ | ✔ |
| Check out / exclusive locking | ✘ | ✔ |
| Reference-aware check-in (assembly with its parts and drawings) | ✘ | ✔ |
| Where-used / contains | ✘ | ✔ |
| Rename or move without breaking assembly references | ✘ Rename in SOLIDWORKS first, then link the history | ✔ |
| Revision scheme (A, B, C…) separate from versions | ✘ | ✔ |
| Workflow states and approvals (e.g. WIP → Released) | ✘ | ✔ (simplified compared with PDM Professional) |
| Data cards, custom properties, searchable metadata | ✘ | ✔ |
| Search across the vault | ✘ Folder tree with status only | ✔ |
| Users, groups and permissions | ✘ Whoever has the storage key has full access | ✔ |
| Integration inside SOLIDWORKS | ✘ | ✔ |
| Windows Explorer integration | ✘ Separate app window | ✔ |
| macOS | ✔ | ✘ |
| Use from anywhere | ✔ | ✘ LAN only without Professional replication |
| Multiple projects on different storage | ✔ Profiles | ✔ Multiple vaults on the server |
| Non-SOLIDWORKS files (Office, PDF, scans, images) | ✔ Any file | ✔ |
| Latest files readable without the app | ✔ `current/` with real names | ✘ Files are stored in the archive's own format |
| Deduplication of identical content | ✔ | Depends on configuration |
| Offline work | Work on local files; check in when connected | Work on local cache; check in when connected |

## Where PDM Standard is clearly better

- **Reference-aware operations.** This is the heart of a CAD PDM. Checking in an assembly with all
  its changed parts, renaming a part and having every assembly and drawing follow, and seeing where a
  part is used can't be matched by any file-level tool.
- **Controlled release.** Workflow states and revisions let a team separate work in progress from
  released drawings and control who can release them.
- **Locking.** Two people can't overwrite each other's work.
- **Searchable metadata.** Data cards and SOLIDWORKS custom properties become searchable across the
  vault.
- **Vendor support.** It's supported by SOLIDWORKS and resellers, with training and documentation.

## Where S3 Vault is better for solo work

- **No infrastructure.** No server, SQL, Windows Server license or dedicated hardware, and no yearly
  server upgrade tied to the SOLIDWORKS release.
- **Off-site by default.** Every check-in goes straight to cloud storage or another machine, so the
  history survives the loss of the workstation or the office.
- **Works anywhere.** Home, office or on the road, on Windows or macOS.
- **Any storage.** Low-cost cloud storage, a self-hosted cluster, a NAS or an end-to-end-encrypted
  sync service, chosen per project through profiles.
- **Transparent format.** Plain files and JSON, with the latest version of every file under its real
  name, so there's no lock-in and the vault can be recovered without the app.
- **Cost.** Storage only.
- **Simplicity.** Check in, add a comment, done. Nothing to administer.

## S3 Vault limitations to be aware of

- No locking. It's designed for one user at a time.
- Not reference-aware. Restoring an old assembly doesn't restore its matching parts automatically,
  although the Log shows which versions were checked in together.
- Renaming SOLIDWORKS files must be done in SOLIDWORKS (Rename or Pack and Go) to keep references
  intact. The app then links the history to the new name.
- Anyone with the storage key has full access. Keep keys private and restricted to one bucket.
- The storage key is stored in plain text in the local settings file.
- The `current/` copy uses a single request, which limits it to files up to 5 GB.
- It's a small, independently maintained tool, not a vendor-supported product.

## Which to choose

**Choose S3 Vault** if you work alone or with one or two collaborators who coordinate, don't want to
run a server, want off-site history by default, work on both Windows and macOS or away from the
office, or have a SOLIDWORKS license that doesn't include PDM Standard.

**Choose PDM Standard** if several people edit the same files, you need locking, released revisions
or approvals, you rely on renaming and reusing parts across assemblies, you have (or want) a
dedicated Windows server on a fast local network, and your SOLIDWORKS license already includes it.

**Consider PDM Professional or a cloud PDM** if the team is spread across locations, needs web
access, or will outgrow SQL Server Express.

## Sources

- GoEngineer, *SOLIDWORKS PDM System Resources Starting Point* (updated July 2026):
  https://www.goengineer.com/blog/solidworks-pdm-system-resources-starting-point
- SOLIDWORKS Help, *SOLIDWORKS PDM* (2025): https://help.solidworks.com/2025/english/WhatsNew/c_wn_pdm.htm
- Javelin, *Hardware Recommendations for SOLIDWORKS 2025 Data Management*:
  https://www.javelin-tech.com/blog/2025/07/hardware-recommendations-for-solidworks-2025-data-management/
- MLC CAD Systems, *SOLIDWORKS and SW PDM System Requirements*:
  https://www.mlc-cad.com/solidworks-help-center/solidworks-and-sw-pdm-system-requirements/

*PDM details are summarized from the sources above and may change with new SOLIDWORKS releases.
Check the current SOLIDWORKS system requirements before planning an installation.*
