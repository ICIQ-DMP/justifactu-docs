# Justifactu — OneDrive sync strategy and pipeline orchestration

## Contents

1. [Purpose and scope](#1-purpose-and-scope)
2. [Background: one channel to SharePoint](#2-background-one-channel-to-sharepoint)
3. [Problem statement](#3-problem-statement)
4. [Constraints that shape the design](#4-constraints-that-shape-the-design)
5. [Why not keep the docker-compose sidecar](#5-why-not-keep-the-docker-compose-sidecar)
6. [Chosen design](#6-chosen-design)
7. [Execution schema (one pipeline run)](#7-execution-schema-one-pipeline-run)
8. [What has to be built (migration checklist)](#8-what-has-to-be-built-migration-checklist)
9. [Failure modes and recovery runbook](#9-failure-modes-and-recovery-runbook)
10. [Operational monitoring](#10-operational-monitoring)
11. [Decision log](#11-decision-log)
12. [Glossary](#12-glossary)
13. [References](#13-references)

> [!NOTE]
> Every design decision, with its rationale and consequences, is in [Section 11, Decision log](#11-decision-log). The
> decision log is authoritative — if the prose and a decision disagree, the decision wins. **As of this update, several
> early decisions (D8, D19) are amended rather than withdrawn: the shape they describe shipped, but not exactly as
> written — see D26–D31 for what's actually in `develop` today.** The draft artifacts referred to here live in the
> **source** repo: `Jenkinsfile` (repo root) and `service/agent/dockerfile/Dockerfile`. Both are still marked `DRAFT`
> and are not wired into any running Jenkins job yet.

---

## 1. Purpose and scope

This document explains **how the justifactu pipeline exchanges data with SharePoint**, why the design looks the way it
does, and what a junior engineer has to build to put it into production.

Read it start to finish. It assumes you have read
[phase1_2_individual_bills.md](phase1_2_individual_bills.md), which describes what the pipeline *does* with the data
once it has it. (Earlier revisions of this document linked to a file named `phase1_individual_bills.md` — that file
never existed under that name; the real filename has always been `phase1_2_individual_bills.md`. Every link below has
been corrected.)

The target production deployment is:

- a single Jenkins job, triggered by cron, pinned to one dedicated agent;
- **no** long-running OneDrive container — but, as of commit `d374242`, a direct Microsoft Graph call *is* back in the
  app, strictly as a download-only fallback for when the OneDrive-for-Linux sync itself fails (see D26, Section 6.5);
- `python -m justifactu` runs the whole sequence itself: one `onedrive` download, the processing stages, one `onedrive`
  upload.

The current setup — `compose.yml` with a long-running `onedrive` sidecar — is still kept for local dev (D18), but is
**not** the production design described here.

---

## 2. Background: one channel to SharePoint

Everything to and from SharePoint goes **primarily** through **one tool**:
[`abraunegg/onedrive`](https://github.com/abraunegg/onedrive) (OneDrive-for-Linux). `main()` invokes it **twice per
run**, each time as a one-shot sync that does a single direction and exits:

| Step | Command (run by `main()`, via `onedrive_sync.py::run_onedrive_sync`) | Direction | Purpose |
|---|---|---|---|
| **1 — download** | `onedrive --confdir <conf> --syncdir <data> --single-directory justifactu/runtime --sync --download-only --cleanup-local-files [--dry-run] --verbose` | SharePoint → local disk | Bring the local `justifactu/runtime` copy fully up to date before any phase runs. |
| **3 — upload** | `onedrive --confdir <conf> --syncdir <data> --single-directory justifactu/runtime --sync --upload-only [--dry-run] --verbose` | local disk → SharePoint | After the phases, make SharePoint match the local tree: upload the new merged-output folder, and propagate the renames and deletions the phases made under `justifactu/runtime`. |

**The scoped remote directory is `justifactu/runtime`, not a separate `_input`/`_output` pair.** The input folders
(`FACTURES`/`Remeses`, or their test-mode equivalents under `carpeta_proves` — see D29) and the merged-output folder
(`_output/FACTURES+PAGAMENTS/...`) all live as subfolders *inside* `justifactu/runtime`, synced as one scope. This
replaced an earlier design where `_input` and `_output` were synced as two separate top-level entries in a
`sync_list` file — that file is gone entirely (D24); `--single-directory justifactu/runtime` is now the only scope
filter, and it covers both input and output in one pass.

```mermaid
flowchart LR
    APP["python -m justifactu (main)<br/>orchestrates: 1 download, 2 phases, 3 upload"]
    OD["OneDrive-for-Linux<br/>one-shot, one direction per call"]
    GAPI["Microsoft Graph API<br/>download-only fallback (D26)"]
    LC[(Local tree<br/>onedrive-data-folder<br/>justifactu/runtime/...)]
    SP[(SharePoint<br/>document library)]

    APP -- "step 1: invoke download sync" --> OD
    APP -- "step 1b: ONLY if step 1 raises" --> GAPI
    APP -- "step 3: invoke upload sync" --> OD
    SP -- "step 1: SharePoint to disk" --> OD
    SP -- "step 1b: fallback download" --> GAPI
    OD -- "step 1: writes runtime/" --> LC
    GAPI -- "step 1b: writes runtime/" --> LC
    LC -- "step 2: read bills/payments, write merged output, local renames + deletes" --> APP
    OD -- "step 3: disk to SharePoint" --> SP
    LC -- "step 3: local diff" --> OD
```

What this shape means:

- **`python -m justifactu` doesn't normally touch the network directly** — stages read and write files under the
  resolved runtime folder and nothing else, *except* on a download failure, where the Graph fallback (D26) makes one
  direct API call chain to recover. The upload side has no such fallback: if `onedrive --upload-only` fails, the run
  fails — there is still only one channel for writing SharePoint.
- **A one-shot `--sync` is self-verifying.** When the command exits `0`, the local copy is fully reconciled with
  SharePoint. If it cannot finish, it exits non-zero; `run_onedrive_sync` wraps that into a `MainCriticalError`, which
  `main()` either routes to the Graph fallback (download only) or lets abort the run (upload).
- **SharePoint is written exactly once per run**, by the upload step, after `run_all_phases()` has completed cleanly.
  If any phase raises, `main()` never reaches the upload step, so a failed run leaves SharePoint untouched.
- **The upload step is destructive by nature.** `--upload-only` makes the remote match local within the synced scope.
  See D20. The safeguard is that the download step (with `--cleanup-local-files`, D21) makes the local tree a
  faithful mirror first, so at upload time the only differences are the ones the phases made on purpose.
- **Two credentials now exist for the download side, not one** — OneDrive-for-Linux's own delegated-user OAuth token
  (Section 6.4), *and* a second, independent Microsoft Graph app-only credential set
  (`CLIENT_ID`/`CLIENT_SECRET`/`TENANT_ID`) used only by the fallback path (D26). This is a deliberate, documented
  regression from the original "one credential" goal — see D26 for why, and for the operational cost of carrying two
  credential sets instead of one.

---

## 3. Problem statement

The remote synced tree is large (hundreds of GB in production). The design has to answer three questions:

1. **State between runs.** How does the OneDrive client run, and how are its downloaded files, its database and its
   auth token kept between runs so that only the first run is slow?
2. **Exposure.** The upload step makes SharePoint match local. How do we make sure it only ever propagates *intended*
   changes, never a corruption or a half-finished run?
3. **Stage ordering.** Do phases 1–2 (and, eventually, phase 3) need any synchronisation between them?

Question 3's answer is still **no.** `rename_payments` (phase 1) and `merge_bills_and_payments` (phase 2) run in the
**same Python process**, one after another, over the **same local directory**, via `process.py::run_all_phases`'s
`_build_phase_map` dispatch. Each phase sees the previous phase's changes because they are already on disk. The only
things that touch the network are the two `onedrive` calls (plus, conditionally, the Graph fallback), and they run
strictly before phase 1 and strictly after the phases complete.

---

## 4. Constraints that shape the design

### 4.1 The OneDrive client is a single-writer

The client keeps its state in a SQLite database (`items.sqlite3`) in its config directory. **Only one `onedrive`
process may use a config directory at a time.** This design runs `onedrive` twice per run but **sequentially** — the
download exits before stage 1, stage 3 exits before the upload. There is never a second live `onedrive` process.

### 4.2 The authorization needs a human, but through a dedicated bootstrap command now

The original design assumed a human runs `onedrive --confdir ...` directly, interactively, once. **That's no longer
how it works.** `main.py` now has a dedicated `--auth` flag (`args.auth and args.location == InputLocation.SHAREPOINT`)
that calls `onedrive_sync.py::run_onedrive_auth(confdir, data_folder)` — a separate function from the sync wrapper,
shelling out to `onedrive --confdir <confdir> --syncdir <data_folder> --verbose` with **no** `--single-directory`,
`--sync`, or `--monitor` flags at all. On a `confdir` with no existing refresh token, this drives the interactive
OAuth flow, persists the token, and the program exits `0` immediately — no file listing, no transfer. See D27 for the
full history (this replaced an earlier version that scoped a real `--sync --download-only` pass to a dedicated empty
remote folder, which no longer exists).

**Known limitation, accepted as intentional:** re-running `--auth` after a token already exists for that `confdir`
now exits non-zero ("missing switches"), instead of the old design's harmless no-op — the fast-fail path in
OneDrive-for-Linux only triggers when a token is already present. `--auth` is meant to be run exactly once per new
`confdir`.

Everything after the bootstrap is headless, same as originally designed: the client exchanges the refresh token for
short-lived access tokens on its own.

### 4.3 The refresh token has a sliding expiry

Microsoft refresh tokens for this flow last roughly **90 days**, and the clock resets
every time the token is used. As long as the pipeline runs at least once inside any 90-day window, the token lives
indefinitely. If it goes dormant longer, a human must re-authorize via `--auth`. The token is also revoked, outside
our control, by: the account password changing; a tenant admin revoking sessions or changing a Conditional Access /
MFA policy; the "OneDrive Client for Linux" enterprise app being disabled. **Unlike the original design, this no
longer stops both directions outright for the download side** — a revoked OneDrive token still triggers the Graph
fallback (D26), which has its own, independent credential set. The upload side has no fallback and still stops
completely if its sync fails.

### 4.4 The first run is slow; later runs are fast

The first run downloads the entire `_input` tree — hundreds of GB, several hours. Every later run is a **delta**: the
client asks SharePoint "what changed since last time" and transfers only that. This only works if the `data` volume
keeps the files **and** `items.sqlite3` between runs (Section 6.3). There is no timeout: the first run is allowed to
take as long as it takes.

### 4.5 The upload step is destructive and unfiltered

`onedrive --upload-only` computes a diff between local and remote and applies it. A locally missing file is
indistinguishable from an intentional delete. There is no per-file confirmation like the old Graph calls gave. The
design contains this with: a faithful local mirror before processing (D21); no upload on a failed run
(D15); and an optional `--dry-run` line whose output is logged so a surprising diff is visible
(Section 6.5).

### 4.6 One agent, one run at a time

The production runner is Jenkins. `disableConcurrentBuilds()` guarantees one build of the job at any moment. That is
the serialization mechanism — no lock file, no home-grown mutex.

---

## 5. Why not keep the docker-compose sidecar

`compose.yml` today runs `onedrive --monitor` as a long-running container the `app` depends on. Three reasons we drop
it:

- **The monitor and the processing fight over the same tree.** A background two-way sync touching `_input` while stages
  1–3 rename and delete files in it is a race. Running `onedrive` as two one-shot calls that bracket the processing
  removes the race entirely.
- **A one-shot `--sync` gives a hard checkpoint for free.** Exit `0` means "fully reconciled"; the monitor never gives
  you that clean a signal.
- **Fewer moving parts.** No container to keep alive, no `depends_on: healthy`, no healthcheck that only proves the
  process is running, no Docker socket exposed so one container can stop another.

The cost we accept: the first run has to do the full initial download inline (Section 4.4). Because the `data` volume
persists, that cost is paid once.

---

## 6. Chosen design

### 6.1 Topology

```mermaid
flowchart TD
    CRON["cron (target, not yet configured)"] --> CTRL["Jenkins controller"] --> PY

    subgraph Agent["Dedicated agent (pinned by label, target — not yet configured)"]
        PY["python -m justifactu (main)<br/>orchestrates the whole run"]
        BIN["onedrive binary<br/>installed via OBS apt repo (amended D6)"]
        GAPI["Graph API client<br/>download fallback only (D26)"]
        CONF[("conf volume<br/>config, refresh_token, items.sqlite3<br/>drive_id injected per call, not stored (D28)")]
        DATA[("data volume<br/>justifactu/runtime/... (bills, payments, merged output)")]
    end

    SP[(SharePoint<br/>document library)]

    PY -- "step 1: invoke download sync, wait for exit" --> BIN
    PY -- "step 1b: on failure only" --> GAPI
    PY -- "step 2: run phases (local filesystem only)" --> DATA
    PY -- "step 3: invoke upload sync, wait for exit" --> BIN
    BIN -- "reads / writes sync state" --> CONF
    BIN -- "step 1: writes runtime/ to disk" --> DATA
    SP -- "step 1: SharePoint to disk" --> BIN
    SP -- "step 1b: fallback download" --> GAPI
    GAPI -- "step 1b: writes runtime/ to disk" --> DATA
    BIN -- "step 3: disk to SharePoint" --> SP
    DATA -- "step 3: local diff" --> BIN
```

Key points:

- **`main()` is the orchestrator.** It calls `onedrive` for the download, runs the three stages against the local tree,
  then calls `onedrive` for the upload. Jenkins only schedules it and runs `make run`.
- **`justifactu` has no edge to SharePoint.** Its only interactions are with the `data` volume.
- **`onedrive` is one binary invoked twice**, sequentially, sharing the `conf` volume and its database.
- **One dedicated agent.** The job is pinned by label (for example `agent { label 'justifactu' }`). Only that agent
  has the `onedrive` binary and the two volumes; any other agent fails the build, which is the intended safety net.
- **The volumes are node-local and outside the Jenkins workspace.** A Docker volume on agent A is not the storage on
  agent B, so the job must always land on the same agent. The workspace is wiped by `checkout scm`, so the synced data
  must not live under it. Use named Docker volumes (or bind mounts to a fixed host path outside the workspace).

### 6.2 The pinned Jenkins agent image

`abraunegg/onedrive` is written in D and has **no official pre-built binary** — the "release tarball" is source, built
with `./configure && make && make install`. Bake it into the agent image with a multi-stage build so the D toolchain
does not bloat the final image, and **pin the version**: an unpinned `apt install onedrive` would let a base-image
rebuild bump the client silently, and a database-schema change between versions can force a slow `--resync`.

```dockerfile
# ---- build stage: compile onedrive from a pinned release tag ----
FROM debian:12 AS onedrive-build
ARG ONEDRIVE_VERSION=v2.5.6
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential curl pkg-config libcurl4-openssl-dev libsqlite3-dev ldc \
 && rm -rf /var/lib/apt/lists/*
RUN mkdir /src && curl -fsSL \
      "https://github.com/abraunegg/onedrive/archive/refs/tags/${ONEDRIVE_VERSION}.tar.gz" \
    | tar xz --strip-components=1 -C /src
WORKDIR /src
RUN ./configure && make && make install     # -> /usr/local/bin/onedrive

# ---- agent stage: extend the existing justifactu Jenkins agent ----
FROM jenkins/ssh-agent:latest
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-pip python3-venv locales \
      libcurl4 libsqlite3-0 ca-certificates \
 && sed -i '/es_ES.UTF-8/s/^# //' /etc/locale.gen && locale-gen \
 && update-locale LANG=es_ES.UTF-8 \
 && rm -rf /var/lib/apt/lists/*
ENV LANG=es_ES.UTF-8 LANGUAGE=es_ES:es LC_ALL=es_ES.UTF-8
COPY --from=onedrive-build /usr/local/bin/onedrive /usr/local/bin/onedrive
# ... existing entrypoint / justifactu setup from service/agent/ ...
```

`ONEDRIVE_VERSION` is the single upgrade knob. Bump it deliberately; expect the next run to take longer if the new
version triggers a re-scan.

> [!NOTE]
> The working draft lives at `service/agent/dockerfile/Dockerfile` (`DRAFT`). The `onedrive` build stage is added on top
> of the current agent image and changes nothing about the existing Python/locale setup.

### 6.3 The two persistent volumes


| Volume | Container path | Contents | If you lose it |
|---|---|---|---|
| **conf** | `/onedrive/conf` | `config`, `refresh_token`, `items.sqlite3` (+ `-wal`, `-shm`), `.resume` state | Human re-authorization (`--auth`) **and** a full re-scan / re-download |
| **data** | `/onedrive/data` | `justifactu/runtime/...` — bills, payments (or their `carpeta_proves`-nested test equivalents), and the merged output | A full re-download (auth survives) |

`config` keys (from `service/onedrive/conf/config`) in the current model:

| Key | Keep? | Note |
|---|---|---|
| `sync_dir` | Physically present, but dead | `--syncdir` on every invocation overrides it regardless of what's written here. |
| `drive_id` | **No longer a static value** | Placeholder `""` in the git-tracked file. Resolved per-call from the `DRIVE_ID` Vault secret and written into `config` immediately before each `onedrive` invocation, then erased again in a `finally` block after. See D28. |
| `cleanup_local_files` | **Removed from config; now a per-call CLI flag** | `--cleanup-local-files` is appended only on the *download* call, computed in code, not read from this file. |
| `bypass_data_preservation` | Not present in the current config — **unconfirmed whether this is still set**; verify against the live `service/onedrive/conf/config` before relying on this row. | |
| `sync_list` | **Deleted entirely** (D24) | `--single-directory justifactu/runtime` is the sole scope filter now. |
| `download_only` | Not present — correctly removed per the original D21 reasoning. | |
| `monitor_interval` | Not present — correctly removed; no monitor is used. | |
### 6.4 Authentication (one token)

```mermaid
sequenceDiagram
    participant H as Human (once)
    participant B as Browser
    participant OD as onedrive client
    participant MS as Microsoft identity
    participant V as conf volume

    H->>OD: run `justifactu --auth` (interactive, one time)
    OD->>H: prints login URL
    H->>B: open URL, sign in as the SharePoint service account, approve
    B->>H: redirect URL containing an auth code
    H->>OD: paste redirect URL
    OD->>MS: exchange code
    MS->>OD: refresh_token + access_token
    OD->>V: write refresh_token

    loop every onedrive call (twice per run), headless
        OD->>V: read refresh_token
        OD->>MS: refresh
        MS->>OD: new access_token + rotated refresh_token
        OD->>V: write rotated refresh_token
    end
```

Operational rules:

- **Do the interactive authorization once, on the pinned agent, into the `conf` volume**, before the first real run.
  The existing `service/agent/entrypoint/entrypoint.sh` already seeds `refresh_token` from
  `${SECRETS_DIR}/ONEDRIVE_TOKEN` when the file is absent — obtain the token once and put it there.
- **The `conf` volume is the source of truth for the token.** The client rotates it on every call and writes the new
  value back. If you re-seed a stale token from a secret on every run and never write the rotation back, auth breaks
  within one cycle. Use the secret only for the initial seed and for backups.
- **Use a dedicated service account with write access to the library**, not a person's account — otherwise the sync
  dies the day that employee changes their password or leaves. Ask the tenant admin for a Conditional Access exclusion
  so the headless refresh is allowed (D9).
- **`refresh_token` must be `chmod 600`, owned by the build user.** It is now a bearer credential for *writing* the
  account's files, not just reading.

### 6.5 How a run is orchestrated

**`main()` (source repo — to be implemented):**

```python
def main() -> None:
    qa_report_path = ADMIN_LOG_FOLDER / (NOW + "_qa_report.log")
    configure_logging_from_settings(qa_files_log_file=qa_report_path)

    args = process_parse_arguments()
    if args.phase is not None:
        run_phase(args.phase, args.runtime_location, args.mode)   # isolated single-phase test path — see D29
        return                                                     # no sync, no QA mail, no --auth check at all

    confdir = args.onedrive_conf_folder

    try:
        if args.auth and args.location == InputLocation.SHAREPOINT:
            run_onedrive_auth(confdir, data_folder=args.onedrive_data_folder)
            exit(0)

        try:
            run_onedrive_sync(confdir, direction="download", single_dir=args.sharepoint_sync_folder,
                               data_folder=args.onedrive_data_folder, dry_run=args.dry_run)
        except MainCriticalError as e:
            # Graph API fallback — download only, see D26. No equivalent exists for the upload call below.
            token_manager, _, drive_id = connect_sharepoint()
            download_input_folder(token_manager, drive_id, args.sharepoint_sync_folder, args.runtime_location)

        run_all_phases(args.runtime_location, args.mode)           # Mode threaded through — see D29

        qa_folder = qa_errors_folder(merged_output_folder(args.runtime_location))
        qa_folder.mkdir(parents=True, exist_ok=True)
        copy_file(qa_report_path, qa_folder)
        copy_file(ADMIN_LOG_FOLDER / (NOW + ".log"), qa_folder)

        run_onedrive_sync(confdir, direction="upload", single_dir=args.sharepoint_sync_folder,
                           data_folder=args.onedrive_data_folder, dry_run=args.dry_run)

    except MainCriticalError as e:
        log.critical(e)
        exit(1)                                                    # fixed in ea6b710 — see D32

    finally:
        send_qa_report_mail(...)                                   # always runs, success or failure
```

**Jenkinsfile (repo root — to be rewritten to this shape):**

```groovy
pipeline {
    agent { label 'justifactu' }          // the one agent with the onedrive binary + the two volumes (D5)

    triggers { cron('H 3 * * *') }         // nightly; SCM-push triggers disabled on the job (D13)

    options {
        disableConcurrentBuilds()          // one run at a time; our substitute for a lock file (D4)
        buildDiscarder(logRotator(numToKeepStr: '30'))
        timestamps()
    }

    stages {
        stage('Checkout') { steps { checkout scm } }

        stage('Run') {
            steps {
                sh '''
                    set -eu
                    make install
                    make run CMD=""       # python -m justifactu:
                                          #   1. onedrive --sync --download-only  (one-shot)
                                          #   2. stage 1 -> stage 2 -> stage 3    (local only)
                                          #   3. onedrive --sync --upload-only    (one-shot)
                '''
            }
        }
    }

    post {
        failure {
            emailext to: 'digitalitzacio@iciq.es',
                     subject: "FAILED: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                     body: "Build failed. Console: ${env.BUILD_URL}console", attachLog: true
        }
        aborted {
            emailext to: 'digitalitzacio@iciq.es',
                     subject: "ABORTED: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                     body: "Build aborted (e.g. a hung run killed by hand). Console: ${env.BUILD_URL}console",
                     attachLog: true
        }
    }
}
```

Notes:

- **No sync stages, no freshness-check stage, no build timeout.** `main()` owns the `onedrive` calls; a one-shot
  `--sync` that exits `0` is already "in sync" (Section 2); the first run is allowed to run for hours (Section 4.4).
- **A failed stage stops the run before the upload.** Because `main()` calls the upload only after stages 1–3 return,
  any stage raising means SharePoint is never written (D15, Section 7.3).
- **`--confdir` must be identical** in the two `main()` calls and in the one-time interactive authorization, or you get
  a second, empty database. `OD_CONFDIR` (default `/onedrive/conf`) is the single place to set it.

> [!NOTE]
> The draft `Jenkinsfile` still has the old multi-stage shape (separate sync stage, readiness-gate stage, 20 h
> timeout) and cites decision IDs that are now withdrawn (D10, D12, D14). Replacing it with the
> shape above is part of the migration (Section 8).

### 6.6 First run vs. later runs

| Run | Download step (1) | Whole run |
|---|---|---|
| First run on a fresh `data` volume | Full download of `_input` — hundreds of GB, several hours | Hours; runs to completion, no timeout |
| Every later run | Delta — only what changed since last time, using the retained `items.sqlite3` | Minutes |
| After an `ONEDRIVE_VERSION` bump that changes the DB schema | One slow re-scan (`resync_auth` auto-accepts) | Longer once, then back to minutes |

The upload step (3) is always quick: a run produces a small `_output` plus a handful of renames/deletes.

### 6.7 Concurrency

`disableConcurrentBuilds()` guarantees one run at a time. Within a run the two `onedrive` calls are sequential, so
there is never a second writer against `items.sqlite3`. If the first (long) run is still going when the next cron
fires, Jenkins **queues** the new build instead of running it in parallel.

> [!NOTE]
> This design is single-branch: the job builds `master`, cron is the only trigger. `disableConcurrentBuilds()` is
> per-job, so if this were ever made a multibranch pipeline a `develop` build and a `master` build could run against
> the same volume at once. That would need a `lock('justifactu-onedrive')` step (Lockable Resources plugin). Called
> out so a future maintainer does not make it multibranch without adding the lock (D17).

### 6.8 Failure handling and notification

Let stages throw and fail the build; notify once in `post { failure { ... } }` (and `post { aborted { ... } }` for a
run killed by hand). Do **not** wrap the processing in `catchError` — a failed stage *must* stop the run before the
upload step. `emailext` needs the *Email Extension* plugin; the built-in `mail` step also works.

A failed nightly run is acceptable: the email notifies the responsible people, SharePoint is untouched, and the next
scheduled run reprocesses from a re-downloaded `_input`.

---

## 7. Execution schema (one pipeline run)

### 7.1 End-to-end flow

```mermaid
flowchart TD
    START([cron fires ~ 03:00]) --> CHK{another build<br/>running?}
    CHK -- yes --> Q[[wait in queue]] --> CHK
    CHK -- no --> A["checkout scm; make install"]
    A --> B["make run -> python -m justifactu (main)"]

    B --> D1["step 1: onedrive one-shot download sync; wait for exit"]
    D1 --> D1c{exit 0?}
    D1c -- no --> F["main raises -> non-zero exit"]
    D1c -- yes --> S1["stage 1: rename_payments (local)"]
    S1 --> S2["stage 2: merge_bills_and_payments (local)<br/>merge PDFs into _output, rename processed payments,<br/>delete processed bills"]
    S2 --> S3["stage 3: credit-card payments (local)<br/>write results into _output"]
    S3 --> QW["write QA report + run log into _output/QA_ERRORS"]
    QW --> U1["step 3: onedrive one-shot upload sync; wait for exit"]
    U1 --> U1c{exit 0?}
    U1c -- no --> F
    U1c -- yes --> G["main returns 0"]

    F --> QM["send QA report e-mail (SMTP, in finally)"]
    G --> QM
    QM --> R{main exit code}
    R -- 0 --> OK([build SUCCESS])
    R -- non-zero --> PF["post failure: emailext -> digitalitzacio@iciq.es"] --> KO([build FAILURE])
```
### 7.1a The isolated `--phase` path (not in the original design)

`justifactu --phase {1,2,3} [--mode {test,prod}]` bypasses the entire diagram above. `main()` checks `args.phase`
*before* touching `confdir`, the `--auth` branch, or either `onedrive` call, and returns immediately after running
just that one phase via `process.py::run_phase`. No sync, no QA-report email (the `return` happens before entering
the `try`/`finally` that sends it), no `--auth` check. This exists purely to exercise one phase's logic against
whatever is already present locally, without touching SharePoint at all. See D29 for the `--mode` flag that decides
which folder names that one phase reads.

### 7.2 Step by step

| # | Where | Action | Success criterion | Typical duration | On failure |
|---|---|---|---|---|---|
| 1 | Controller | `cron('H 3 * * *')` fires; `disableConcurrentBuilds()` check | Build created, no other build running | — | No agent online → stays queued |
| 2 | Agent workspace | `checkout scm`; `make install` | Working tree at `master` HEAD; `justifactu` installed in the venv | seconds–1 min | Build fails → e-mail |
| 3 | Agent (`main`, step 1) | `onedrive --confdir $OD_CONFDIR --sync --download-only --verbose` | Process exits `0` (⇒ local `_input` fully reconciled) | First run: hours. Later: seconds–minutes | Non-zero exit → `main` raises → build fails → e-mail. `.resume` + DB cursor are saved; the next run continues from there |
| 4 | Agent (`main`, step 2) | stage 1 `rename_payments` → stage 2 `merge_bills_and_payments` → stage 3 credit-card payments → write QA report | `main` reaches step 3 without raising | minutes | Any stage raises → build fails → e-mail. **No SharePoint change has happened** — all local. See 7.3 |
| 5 | Agent (`main`, step 3) | `onedrive --confdir $OD_CONFDIR --sync --upload-only --verbose` (optionally a `--dry-run` pass first, logged) | Process exits `0` | seconds–minutes | Non-zero exit → build fails → e-mail. SharePoint may be **partially** updated. See 7.3 |
| 6 | Agent (`main`, `finally`) | `send_qa_report_mail` (SMTP) | QA report + run log e-mailed | seconds | Logged as an error; build result unchanged |
| 7 | Controller | `post { failure }` / `post { aborted }` | E-mail to `digitalitzacio@iciq.es` with console log | seconds | SMTP error logged only |

### 7.3 Idempotency and partial failure

The pipeline is **re-runnable, not transactional**. There is no rollback.

- **A run that fails before step 5 leaves SharePoint untouched.** The next run re-downloads `_input` (still holding the
  un-renamed payments and un-deleted bills, because nothing was pushed), reprocesses, and produces a fresh QA report.
- **If step 5 itself dies part-way**, SharePoint is left with some changes applied and some not. Recovery is automatic:
  the next run's download step reconciles `_input` (with `cleanup_local_files` it re-pulls whatever step 5 did manage
  to change and drops what it didn't), reprocesses, and the upload step runs again. `_output` is push-only — a
  partially uploaded `_output` is simply completed on the next successful upload because the local copy is still there.
- **Output filenames are deterministic** (`SAP_F_P.pdf` per pair), so a re-run overwrites rather than duplicates.
- **Deletions are irreversible** from the pipeline's side — recoverable only from SharePoint's own version history /
  recycle bin (see [phase1_individual_bills.md](phase1_individual_bills.md), "Known limitations").

Detailed matching/cleanup semantics for stages 1–2 are in [phase1_individual_bills.md](phase1_individual_bills.md); the
code of record is `src/justifactu/process.py`.

---

## 8. What has to be built (migration checklist)

### 8.1 Source repo (`iciq-dmp/justifactu`)

1. ~~Remove the Microsoft Graph path entirely~~ — **superseded.** Graph is back as a download-only fallback (D26).
   `token_manager.py` is live again (it's the fallback's only consumer of `TokenManager`). `sharepoint.py` was
   recreated with a **narrower** surface than before: `get_site_id`, `get_drive_id`, `list_folder_contents`,
   `download_file`, `download_folder_recursive`, `download_input_folder`, `connect_sharepoint` — no
   `rename_remote_item`/`delete_remote_item`/`upload_folder_recursive` at all; the upload path remains
   `onedrive`-only, with no Graph equivalent. ✅ `--download-input` was removed (`aa7dd21`). `--location` was **not**
   removed — it's still a real flag, gating the `--auth` branch.
2. ✅ **Done.** `onedrive_sync.py::run_onedrive_sync(confdir, direction, single_dir, data_folder, dry_run)` exists,
   plus the separate `run_onedrive_auth(confdir, data_folder)` for the bootstrap (D27), and
   `_write_drive_id`/`erase_drive_id` for per-call secret injection (D28).
3. ✅ **Done**, but not exactly as drafted — see Section 6.5 for the real shape, including the `--phase` early-return
   and `--auth` branch that weren't part of the original plan at all.
4. ⬜ **Still open.** `Phase.PHASE_3` exists in `defines.py` and is reachable via `--phase 3` for isolated testing, but
   its implementation is `process.py::mock_phase3()` — a placeholder that just logs a message. It's deliberately
   excluded from `FULL_RUN_PHASES`, so a full (non-`--phase`) run never reaches it. The real credit-card-payments
   logic described in D22 still needs its matching/naming rules written down before implementation — see Section
   11.2 for the still-open items, now including this one explicitly.

### 8.2 OneDrive config (`service/onedrive/conf/`)

1. ✅ **Done**, via a different mechanism than drafted: rather than hand-editing `config`, the relevant flags
   (`--cleanup-local-files`, `--dry-run`) are now computed per-call in `onedrive_sync.py`, not static config keys.
   `drive_id` resolution is also per-call now (D28), not a static key either.
2. ✅ **Done**, also via a different mechanism: `sync_list` wasn't edited to add a second path — it was **deleted
   entirely** (D24), replaced by `--single-directory justifactu/runtime` as the sole scope filter, which already
   covers both the input folders and the merged-output folder in one pass.

### 8.3 Infrastructure

1. ⬜ **Partially done.** `service/agent/dockerfile/Dockerfile` exists and installs `onedrive`, but via the OBS apt
   repo (amended D6), not the multi-stage source compile this document originally specified. `ONEDRIVE_VERSION`
   pinning as described doesn't apply to this approach — confirm what version the OBS repo actually resolves to and
   whether it needs separate pinning.
2. ⬜ **Still open.** The two named volumes exist in `service/agent/compose.yml`'s definition, but there's no
   confirmation here that they're actually provisioned and mounted on a real, running agent host yet.
3. ⬜ **Still open.** `Jenkinsfile` is still the single-stage draft described in Section 6.5 — no agent label pin, no
   cron trigger, no `disableConcurrentBuilds()`, no `checkout scm` stage.

### 8.4 One-time setup and cutover

⬜ **Still entirely open** — service account provisioning (D9), the first interactive `--auth` run on the real pinned
agent, the first real production run, and retiring the local-dev-only `compose.yml` sidecar path are all unconfirmed
as of this update.

---

## 9. Failure modes and recovery runbook

| Symptom | Likely cause | Recovery |
|---|---|---|
| Download step fails with an auth error | Refresh token expired (>90 days idle) or revoked — password change, CA policy, enterprise app disabled (4.3) | A human re-runs the interactive authorization as the service account and replaces `refresh_token` in the `conf` volume. No Graph fallback — both directions are down until fixed. |
| A step demands `--resync` | Client version changed, or the database is inconsistent | `resync_auth = "true"` auto-accepts it. Expect a much longer run (full re-scan). If it recurs, the `conf` volume database may be damaged — see next row. |
| `items.sqlite3` corrupted (`-wal` left behind, agent killed mid-write) | Hard kill during a DB write | Delete `items.sqlite3*` from the `conf` volume, keep `refresh_token` and `config`, run the job — the client rebuilds the DB by re-scanning existing files (slow, not a re-download). |
| Upload step ran but `_output` is not on SharePoint | `sync_list` missing `/justifactu/_output`, or `_output` written outside `sync_dir` | Add the line to `sync_list` in the `conf` volume; confirm the app writes `_output` under `/onedrive/data`. Re-run the job. |
| Upload step deleted files on SharePoint that should not have been | A stage exited `0` leaving the local tree wrong (partial write), and `main()` still reached the upload step | Restore from SharePoint version history / recycle bin. Investigate the stage. Consider `--no-remote-delete` or a `--dry-run` deletion-count guard (Section 11.2). |
| Upload step failed part-way | Network drop, throttling, token revoked mid-run | Automatic: the next run's download step reconciles `_input`; `_output` is still local and is completed by the next successful upload. No manual step unless it keeps failing. |
| First run never seems to finish | It is genuinely downloading hundreds of GB | Let it run. Check progress in the `--verbose` log. Only intervene if throughput is zero for a long time (then it is a network/auth problem, not slowness). |
| A run is truly hung (zero progress, not just slow) | Stuck `onedrive` process or a wedged network mount | Abort the build by hand; the next run resumes from `.resume` + the DB cursor. Optionally add a generous Jenkins `timeout` (e.g. 24 h) purely as a safety net — it is not part of the sync design. |
| `conf` volume lost entirely | Volume deleted, agent rebuilt without it | Restore `config` + `sync_list` from the repo, re-authorize (human), then a full re-scan/re-download. |
| `data` volume lost entirely | Volume deleted | Let the next run re-download `_input` (hours). Any `_output` not yet uploaded is lost — reprocess. Auth is fine if `conf` survived. |
| Job ran on the wrong agent | Label not pinned, or another agent carries the label | Fix `agent { label ... }`; ensure only the intended node carries that label and the volumes. |

> [!TIP]
> Back up the small `conf` volume regularly (`config`, `sync_list`, `refresh_token`, `items.sqlite3`). Losing it costs
> a human re-authorization *and* a multi-hour re-download. The large `data` volume is not worth backing up — `_input`
> can always be re-fetched; only an un-uploaded `_output` is at risk, and that is one reprocess.

---

## 10. Operational monitoring

- **Token liveness.** A weekly throwaway `onedrive --confdir /onedrive/conf --sync --download-only --verbose` run (or
  just a token refresh) surfaces revocation early, while there is still time to re-authorize before a nightly run
  fails.
- **The failure / aborted e-mails.** One failure is tolerable — the next run retries. Failures several nights running
  mean the token is gone or the first download is not making progress.
- **The upload diff.** If the optional `--dry-run` line ever reports a large number of pending deletions, treat it as
  an incident — a normal run deletes only the bills it just processed.
- **After an `ONEDRIVE_VERSION` bump**, watch the first run: one longer-than-usual download step is expected; a
  *repeated* long download means every run is re-scanning and something is wrong with `data`/`conf` persistence.
- **Fallback usage.** If the Graph fallback (D26) is triggering regularly rather than only in rare emergencies, that's
  itself a signal worth monitoring — it means the primary OneDrive sync is unreliable, not that the fallback is
  doing its job as designed.

---

## 11. Decision log

Authoritative. Read this before changing anything. Decision IDs are stable; a few earlier decisions are marked
**withdrawn** and kept so old references still resolve.

### 11.1 Decisions made

#### D1 — Use OneDrive-for-Linux to move the input, instead of a per-run download in the app

- **Context:** `_input` is hundreds of GB; a home-grown per-run download plus caching in the app is a lot of code to
  write and maintain.
- **Decision:** use OneDrive-for-Linux delta sync.
- **Why:** purpose-built for delta sync against SharePoint; steady-state cost drops to a delta; nothing to maintain in
  the app.
- **Consequences:** introduces a *user* OAuth token to obtain and keep alive (D8).

#### D2 — One mechanism, two one-shot passes, orchestrated by the app

- **Context:** the pipeline both reads a large input tree and writes results and mutations back.
- **Alternatives:** read via OneDrive, write via Microsoft Graph (two mechanisms, two credentials); a bidirectional
  `--monitor` running during processing.
- **Decision:** OneDrive-for-Linux only, invoked twice by `main()` — `--sync --download-only` before stages 1–3,
  `--sync --upload-only` after. No monitor (D3), no Graph (D19).
- **Why:** one client, one credential, one thing to operate. The passes are sequential and bracket the processing, so
  there is no concurrency to manage (4.1, 4.6). A one-shot `--sync` that exits `0` is self-verifying, which removes the
  need for any freshness gate (D23).
- **Consequences:** the local tree is authoritative between the two passes, so the upload pass is destructive by nature
  (4.5, D20); `download_only` can no longer be static config (D21); a revoked token blocks both
  directions with no fallback (4.3).

#### D3 — No long-running monitor; drop the docker-compose sidecar

- **Context:** `compose.yml` runs `onedrive --monitor` as a sidecar the `app` depends on.
- **Decision:** no monitor. `main()` runs two one-shot `--sync` calls.
- **Why:** a background sync racing stages 1–3 over `_input` is unsafe; a one-shot exit code is a cleaner checkpoint
  than any monitor signal; fewer moving parts (no container lifecycle, no healthcheck, no Docker socket).
- **Consequences:** the first run does the full initial download inline (4.4). Paid once, because the `data` volume
  persists.

#### D4 — No lock file; rely on Jenkins `disableConcurrentBuilds()`

- **Context:** two `onedrive` processes against one database corrupt it.
- **Decision:** `disableConcurrentBuilds()` in the Jenkinsfile.
- **Why:** the production runner *is* Jenkins and already guarantees one build per job. Within a run the two passes are
  sequential. A lock file would be redundant.
- **Consequences:** valid only while this is a single job — see D17.

#### D5 — Pin the job to one dedicated agent by label

- **Context:** the `conf`/`data` volumes are node-local; the `onedrive` binary is only in one agent image.
- **Decision:** `agent { label 'justifactu' }` — one node, always.
- **Why:** a volume on another node is different storage, so a floating job would lose the retained state and
  re-download from scratch. Other agents lack the binary and fail the build — the intended safety net.
- **Consequences:** that agent is a single point of failure; rebuilding it means re-auth + re-download unless the
  volumes are preserved (D7, Section 9).

#### D6 — Build the `onedrive` client from a pinned source tag in the agent image

- **Context:** no official pre-built binary; a client-version change can force a slow `--resync`.
- **Decision:** multi-stage Dockerfile: compile from the pinned `${ONEDRIVE_VERSION}` tarball, copy the binary into the
  agent stage.
- **Why:** `ONEDRIVE_VERSION` becomes the single explicit upgrade knob; an unpinned `apt` package would let a base
  image rebuild bump the client silently.
- **Consequences:** the image build needs the D toolchain (throw-away build stage); upgrades are deliberate and cost
  one slow run.

#### D7 — `conf` and `data` as persistent named Docker volumes, outside the Jenkins workspace

- **Context:** `checkout scm` wipes the workspace; delta sync is only fast if the database survives (4.4).
- **Decision:** named Docker volumes (or bind mounts to a fixed host path *not* under the workspace).
- **Why:** persisting `items.sqlite3` keeps later runs to minutes; persisting `refresh_token` keeps the client
  authenticated; keeping them out of the workspace stops `checkout scm` destroying them.
- **Consequences:** the volumes are operational state to back up (`conf`) and monitor (Section 9).

#### D8 — One interactive authorization; the `conf` volume is the source of truth for the rotating token

- **Context:** OneDrive-for-Linux authenticates as a *user*; the token rotates on every call and the client writes the
  new value back.
- **Decision:** authorize once as the service account into the `conf` volume; that copy is authoritative thereafter.
  The secret store is for the initial seed and backups only.
- **Why:** re-seeding a stale token from a secret every run, without writing the rotation back, breaks auth within one
  cycle.
- **Consequences:** losing the `conf` volume costs a human re-authorization; the token also dies after ~90 days idle or
  on out-of-band revocation (4.3).

#### D9 — Dedicated SharePoint service account with write access (prerequisite, not yet provisioned)

- **Context:** the client logs in as a real account and now *writes* the library.
- **Decision:** provision a licensed dedicated account with write access; get a Conditional Access exclusion for the
  headless refresh.
- **Why:** decouples the pipeline's lifetime from any individual's account.
- **Status:** open task — see Section 11.2.

#### D10 — *(withdrawn)* Readiness gate on `--display-sync-status`

- **Was:** a pipeline stage that ran `onedrive --display-sync-status` and failed the build unless it reported
  `IN SYNC`, to avoid processing a partial tree.
- **Withdrawn because:** `main()` now runs the download as a one-shot `--sync`. If it exits `0` the local copy is fully
  reconciled; if it cannot finish it exits non-zero and `main()` aborts before stage 1. The exit code *is* the gate, so
  a separate check adds nothing. `--display-sync-status` is not used anywhere in the design (D23).

#### D11 — No sync barrier between pipeline stages

- **Context:** whether stages 1, 2 and 3 need any synchronisation between them.
- **Decision:** no. They run in one process, one after another, over the same local directory; each reads the previous
  one's writes straight from disk. Nothing reaches SharePoint until the upload step, so there is nothing to wait for.
- **Consequences:** none — this removes a step that would only add latency and false confidence.

#### D12 — *(withdrawn)* Skip the manual bootstrap; converge the initial download over several nights

- **Was:** run each early build under a 20 h timeout, let Jenkins abort it, resume next night, and rely on the
  readiness gate to block processing until converged.
- **Withdrawn because:** there is no timeout and no readiness gate any more. The first run simply runs the download to
  completion, however many hours that takes, then proceeds (D14, D23).

#### D13 — Trigger by cron in the Jenkinsfile; disable SCM-push triggers; build `master`

- **Decision:** `triggers { cron('H 3 * * *') }` on `master`, SCM-push triggers off.
- **Why:** self-contained, one place for logs and history. GitHub Actions already handles lint/test/image on push; this
  job is purely the scheduled data run.
- **Consequences:** code lands via the normal PR flow; the job always picks up whatever is on `master`.

#### D14 — *(withdrawn)* Build timeout shorter than the cron interval

- **Was:** `timeout(time: 20, unit: 'HOURS')` so a stuck build yields before the next one is due.
- **Withdrawn because:** the first run legitimately takes hours and must not be aborted. `disableConcurrentBuilds()`
  already queues an overlapping run. A genuinely hung run is aborted by hand; a generous safety-net `timeout` may be
  added as ops hygiene but is not part of the design (Section 9).

#### D15 — Notify with `post { failure }`, not `try`/`catch` around stages; a failed stage stops the run before the upload

- **Decision:** let stages throw; handle notification once in `post { failure }` / `post { aborted }`. Never wrap the
  processing in `catchError`.
- **Why:** idiomatic declarative-pipeline style, and "a failed stage stops the run" is exactly the property the upload
  step depends on — `main()` calls the upload only after stages 1–3 return, so a raise means SharePoint is untouched
  (7.3).
- **Consequences:** a failed nightly run is expected and acceptable; the next run retries.

#### D16 — There is exactly one upload, and it is the `--upload-only` step in `main()`

- **Context:** the previous design had the app push results itself through Graph.
- **Decision:** stages write results only to local `_output`; the single `onedrive --sync --upload-only` call after
  stage 3 is what puts them — and every other local change — on SharePoint.
- **Why:** with Graph gone (D19) there is no other channel. One upload path, one place it can fail, one exit
  code to check.
- **Consequences:** the upload is a batch, not per-file; coarser confirmation than Graph gave (4.5), mitigated by the
  faithful-mirror download (D21) and the optional `--dry-run` log.

#### D17 — Single-branch pipeline, not multibranch

- **Decision:** one Jenkins job building `master`.
- **Why:** `disableConcurrentBuilds()` is per-job; a multibranch pipeline would let two branches run against the same
  volume at once — corruption.
- **Consequences:** if multibranch is ever wanted, a `lock('justifactu-onedrive')` around the run is mandatory
  (cross-referenced from D4, Section 6.7).

#### D18 — Keep `compose.yml` for local development

- **Decision:** keep `compose.yml` for local dev against test data; only remove its `onedrive` service if it stops
  being useful.
- **Why:** still a convenient way to run the app locally; not on the production path.
- **Consequences:** two deployment descriptions coexist — this document is authoritative for production.

#### D19 — Drop Microsoft Graph entirely

- **Context:** the app used Graph (app-only credentials) for input download, per-file renames and deletes, and the
  results upload.
- **Alternatives:** keep Graph for the mutations only; keep it as a fallback for when OneDrive is behind.
- **Decision:** remove the Graph client from the app. Stages 1–2 do renames and deletes on the **local** filesystem;
  the `--upload-only` step carries every change to SharePoint.
- **Why:** one mechanism and one credential to operate, authorize, rotate and monitor instead of two; no app code that
  needs SharePoint knowledge.
- **Consequences:** no per-file API audit trail — the upload is a batch (4.5, D16, D20); a revoked
  token blocks both directions (4.3); `sharepoint.py` (remote half), `token_manager.py` and the `--location` /
  `--download-input` switches are removed (Section 8).

#### D20 — Renames and deletes reach SharePoint through the upload step, not through explicit API calls

- **Context:** with Graph gone, stage renames and bill deletions have to propagate somehow.
- **Decision:** stages mutate local files; `onedrive --upload-only` propagates the diff. A rename becomes *upload new
  name + delete old name* (no rename primitive on this path); a local deletion becomes a remote deletion (default
  `--upload-only` behaviour; `--no-remote-delete` would suppress it).
- **Why:** it is the only channel left, and for the volumes involved (a handful of small PDFs per run) re-uploading a
  renamed file is negligible.
- **Consequences:** (1) renames cost a re-upload of the file bytes, not a metadata op; (2) the upload step is
  **destructive** — any locally missing file is deleted remotely, so a crashed stage that leaves the tree wrong could
  delete real data (mitigated by D21, D15, and the `--dry-run` log); (3) there is a brief moment in the
  pass where a renamed file exists under neither name remotely.

#### D21 — Keep `cleanup_local_files`; remove `download_only` and `monitor_interval` from the config

- **Context:** `service/onedrive/conf/config` currently hardcodes `download_only = "true"`,
  `cleanup_local_files = "true"`, `monitor_interval = "60"`.
- **Decision:** remove `download_only` (direction is per-call now) and `monitor_interval` (no monitor). **Keep**
  `cleanup_local_files = "true"`.
- **Why:** `download_only` in the file would make the upload step a no-op. But `cleanup_local_files` must stay: in
  `--download-only` mode the client does **not** apply remote deletions locally unless it is set, so without it a file
  deleted on SharePoint would linger in local `_input` and the upload step would re-upload it — resurrecting deleted
  files. With it, local `_input` is a true mirror and the upload diff contains only the stages' intended changes.
- **Consequences:** a file deleted from `_input` on SharePoint by someone outside the pipeline is also removed locally
  on the next download — expected, and `_input` churn from outside the pipeline is small.

#### D22 — Stage 3 (credit-card payments) runs in-process after stage 2, over the same local tree

- **Context:** a third processing step handles payments made to credit cards. Not yet implemented in `process.py`.
- **Decision:** call it from `main()` immediately after `merge_bills_and_payments`, in the same process, reading and
  writing the same local `_input` / `_output`. Its input arrives via the same download step; its output leaves via the
  same upload step.
- **Why:** it consumes the same `_input` tree and produces the same kind of `_output`; a second set of sync passes
  would double the round-trips and re-open the ordering question D11 closes.
- **Consequences:** stage 3's local renames/deletes are swept up by the one upload step with stages 1–2's; a stage-3
  failure stops the run before the upload, like any other stage (D15).

#### D23 — No freshness gate anywhere; the one-shot exit code is the checkpoint

- **Context:** the earlier design used `onedrive --display-sync-status` / `IN SYNC` as a readiness gate and a
  post-upload check.
- **Decision:** do not use `--display-sync-status` at all. A one-shot `onedrive --sync` either exits `0` — meaning the
  requested direction is fully reconciled — or exits non-zero, in which case `main()` aborts.
- **Why:** with no background monitor, "sync finished" and "process exited 0" are the same event. A separate status
  check is redundant and was a source of confusing extra states (`INDETERMINATE`, gate-fails-during-convergence).
- **Consequences:** D10 and D12 are withdrawn; the pipeline has no gate stage; correctness rests on
  `subprocess.run(..., check=True)` around each `onedrive` call.

#### D24 — Retire `sync_list`; `--single-directory` is the only scope filter

**Context:** _input is retired. sync_list previously listed `/justifactu/_input`
as the only synced path — an inclusion whitelist. The pipeline now scopes to
`justifactu/runtime` exclusively via the `--single-directory` CLI flag
instead.

**Decision:** delete `service/onedrive/conf/sync_list` from the repo
entirely (`git rm`), not just empty it. `--single-directory` becomes the
sole scoping mechanism on every onedrive invocation.

**Why:**
sync_list and --single-directory are independent filters that don't defer
to each other. With sync_list still listing only `/justifactu/_input`,
a `--single-directory justifactu/runtime` run logged:
  "Skipping path - excluded by sync_list config: justifactu/runtime"
  "Flagging to delete item locally as this is now an unwanted item..."
  "Deleting local directory: justifactu/runtime"
followed by a crash (`std.file.FileException`) — onedrive deleted the very
directory --single-directory was scoping to, then choked trying to
operate on it afterward. Two filters saying contradictory things doesn't
degrade gracefully; it actively destroys local data given this config's
cleanup_local_files=true + bypass_data_preservation=true.
Also worth noting: leaving sync_list present-but-*empty* isn't a safe
middle ground either — the analogous skip_file setting has a documented
bug where an empty (but present) value forces perpetual --resync. Full
removal is the only option that reliably resolves to "no whitelist active."

**Consequences:** anyone reintroducing a sync_list file for a different
reason later needs to know it will silently exclude/delete anything not
listed, even if --single-directory also targets it. Flag this prominently,
not just in a decision log entry — maybe a comment pointer from
compose.yml or the onedrive conf directory's README/.gitignore too.

#### D25 — CLI-args-driven local paths replace hardcoded Docker paths

**Context:** confdir came from `OD_CONFDIR` env var, defaulting to the
Docker-only `/onedrive/conf`; sync_dir was hardcoded in
`service/onedrive/conf/config` to `/onedrive/data`. Neither existed on a
bare local machine, and running main.py outside Docker failed at startup
(`Permission denied` trying to create `/onedrive`).

**Decision:** five new arguments.py flags, all defaulting to portable
paths computed from `ROOT_FOLDER` (`Path(__file__).resolve().parent.parent.parent`),
overridable via CLI:
  --onedrive_conf_folder   → passed as --confdir
  --onedrive_data_folder   → passed as --syncdir (overrides config's sync_dir)
  --sharepoint_sync_folder → passed as --single-directory (relative to syncdir)
  --onedrive_logs_folder   → (note if this made it into the subprocess call
                              or is currently unused — check onedrive_sync.py)
  --runtime_location       → computed as onedrive_data_folder / sharepoint_sync_folder,
                              auto-created via parse_directory if missing

**Why:** the previous defaults weren't just inconvenient for testing — they made running
`main()` outside Docker impossible outright. `confdir` came from `OD_CONFDIR`, defaulting
to the literal string `/onedrive/conf`; `sync_dir` was hardcoded in
`service/onedrive/conf/config` to `/onedrive/data`. Both are absolute paths that only
resolve inside the Docker container's filesystem. Run `main()` on a bare host and `confdir`
points at a directory that doesn't exist; `onedrive` then tries to create `sync_dir` at
`/onedrive` — filesystem root — and fails with a plain permission error, since no non-root
user can create directories there. This wasn't a "slower to test" problem, it was a hard
blocker: there was no way to exercise the sync path locally at all before this change, only
inside a container.

Putting these behind CLI flags rather than just fixing the constants in place adds a
second benefit: a developer testing against disposable mock data
(rather than the real SharePoint account) can redirect `confdir`/`sync_dir`/the sync
scope to an isolated sandbox location with one flag, without editing source or
maintaining a parallel "test" branch of the defaults.

**Consequences:** --confdir and --syncdir must be passed identically on
every onedrive invocation across the whole run (download and upload
passes alike) or the local sync-state database has no continuity between
calls — this isn't optional plumbing, it's required for the delta-sync
mechanism (see existing Glossary entry for items.sqlite3) to work at all.

#### D26 — Reintroduce Microsoft Graph as a download-only fallback (amends D19)

- **Context:** a production run failing outright whenever OneDrive-for-Linux's sync has a bad day — network blip,
  a stuck `--resync`, a transient SharePoint throttling response — was judged too fragile for a nightly, mostly
  unattended job.
- **Decision:** on a download-sync failure only, `main()` catches the resulting `MainCriticalError` and falls back to
  a direct Microsoft Graph download (`connect_sharepoint()` + `download_input_folder()`), scoped to the same remote
  path and local destination the OneDrive sync would have used. If the fallback *also* fails, that exception is
  wrapped into a fresh `MainCriticalError` and re-raised, so the run still fails cleanly — not silently.
- **Why:** a working fallback is better than a hard stop for a failure mode that's plausibly transient. Scoping it to
  download-only keeps the upload side's "one channel, one credential" property from D16/D19 intact.
- **Consequences:** this reintroduces a **second, independent SharePoint credential path** — Graph app-only auth via
  `TokenManager`/`CLIENT_ID`/`CLIENT_SECRET`/`TENANT_ID` — alongside OneDrive-for-Linux's own delegated-user OAuth
  token. That's exactly the "two credentials to operate, authorize, rotate and monitor" situation D19 originally
  argued against. A fallback whose own credentials nobody is actively rotating is a fallback that can silently stop
  working — this hasn't been verified against the tenant's current app registration as of this update. `token_manager.py`
  and the Graph-reading half of `sharepoint.py` are live again, with a narrower surface than before (8.1).

#### D27 — `--auth` becomes a dedicated bootstrap command, not an interactive raw `onedrive` invocation

- **Context:** the original design assumed a human runs `onedrive --confdir ...` directly. In practice the app needed
  a supported, documented entry point for this that didn't require knowing the client's own CLI.
- **Decision:** `main.py` gates a `--auth` flag on `args.location == InputLocation.SHAREPOINT`, calling a dedicated
  `onedrive_sync.py::run_onedrive_auth(confdir, data_folder)` — not the same function used for the regular sync.
  It shells out to `onedrive --confdir <confdir> --syncdir <data_folder> --verbose`, with no `--single-directory`,
  `--sync`, or `--monitor` flags. `exit(0)` immediately after, regardless of outcome signal beyond the subprocess's
  own exit code.
- **Why:** confirmed against upstream `onedrive` source (not just docs) that `initialise()` — where OAuth and token
  persistence happen — runs unconditionally, regardless of `--sync`/`--monitor`. On a genuinely fresh `confdir`, a
  plain invocation with none of those flags completes OAuth, persists the token, and exits `0` — no sync engine, no
  file transfer at all (one caveat: `initialise()` itself still makes two small Graph metadata calls — drive details
  and root details — not a bulk download).
- **Consequences:** re-running `--auth` after a token already exists for that `confdir` now exits non-zero ("missing
  switches"), since the client's fast-fail path only triggers when a token is already present — a known, accepted
  limitation since `--auth` is meant to be one-time, per-`confdir` setup. This superseded an earlier version of the
  bootstrap that scoped a real `--sync --download-only` pass to a dedicated empty remote folder
  (`justifactu/_auth_sync`) specifically to avoid a bulk download during auth — that folder/path constant has been
  removed from the codebase; the `--auth` branch no longer uses `--single-directory` at all, so it's moot.

#### D28 — `drive_id` resolved per-call from Vault, never stored permanently in the tracked config file

- **Context:** `service/onedrive/conf/config` is git-tracked. Permanently storing a live `drive_id` secret in it was a
  standing exposure.
- **Decision:** `service/onedrive/conf/config` now has `drive_id = ""` as a placeholder. `onedrive_sync.py` resolves
  the real value via `read_secret(SecretNames.DRIVE_ID.value)` and rewrites the `drive_id` line in the config file
  immediately before each `onedrive` subprocess invocation (`_write_drive_id`), then blanks it back to `""`
  (`erase_drive_id`) afterward — wrapping each individual invocation, not once per whole process.
- **Why:** keeps the real secret off disk except for the brief window an `onedrive` subprocess actually needs it.
- **Consequences:** the erase must be guaranteed on every exit path, including an exception mid-call — it's called
  from a `finally` on the outer `try` in both `run_onedrive_sync` and `run_onedrive_auth`. An earlier version called
  it as plain sequential code *after* a successful `subprocess.run`, which meant a failed sync skipped the erase
  entirely and left the secret sitting in the tracked file — fixed; don't reintroduce the sequential-call version.

#### D29 — `--mode {prod,test}` replaces an env-var-only switch; `--phase` adds an isolated single-stage test path

- **Context:** the bills/payments source folder names were a single hardcoded test pair
  (`FACTURES_prova`/`Remeses_prova`) with a `TODO: Remove when entering into production` comment and no actual
  production pair defined anywhere.
- **Decision:** `FolderName` now has explicit `BILLS_INPUT_PROD`/`BILLS_INPUT_TEST`/`PAYMENTS_INPUT_PROD`/
  `PAYMENTS_INPUT_TEST` pairs (`"FACTURES"`/`"FACTURES_prova"`/`"Remeses"`/`"Remeses_prova"`), and a new `Mode(StrEnum)`
  (`PROD`/`TEST`) selected by a `--mode` CLI flag, default `Mode.TEST`. `src/justifactu/paths.py::bills_folder()`/
  `payments_folder()` take `(input_folder, mode)` and additionally nest test-mode paths one level deeper, under a
  `carpeta_proves` folder (`FolderName.TEST_FOLDER`) — production mode reads directly from `FACTURES`/`Remeses` with
  no such wrapper.
- **Why:** makes the test/production switch an explicit, visible, per-invocation choice instead of a silent default
  baked into source, and keeps local test fixtures (`carpeta_proves/...`) structurally separate from where production
  data would ever live, so a stray local test folder can never be mistaken for real synced SharePoint content.
- **Consequences:** separately, `--phase {1,2,3}` was reworked to live inside `main()` itself (not a branch in
  `__main__.py`, which is now a one-line `if __name__ == "__main__": main()` wrapper) — see Section 7.1a. Production
  Jenkins invocations must explicitly pass `--mode prod` once this is deployed, or the job silently stays in test
  mode regardless of the agent image's environment. The agent Dockerfile's `BILLS_INPUT_FOLDER`/`PAYMENTS_INPUT_FOLDER`
  `ENV` lines (Section 6.2) are leftovers from an earlier, env-var-only version of this same switch and should be
  removed.

#### D30 — `notify.py` replaces `emailext` for Jenkinsfile failure/abort notifications

- **Context:** the original Jenkinsfile design used the `emailext` step for `post { failure }`/`post { aborted }`.
- **Decision:** a new standalone entry point, `src/justifactu/notify.py` (`justifactu-notify` console script, `make
  notify` target), parses `--to`/`--subject`/`--body`/repeatable `--attach`, and calls
  `mail.py::send_mail_authenticated`, letting exceptions propagate (unlike `main()`'s own error handling). The
  Jenkinsfile's `post` blocks now call `make notify CMD="..."` via `sh` steps instead of `emailext(...)`.
- **Why:** reuses the same authenticated SMTP path and secret-resolution chain the rest of the app already has,
  rather than depending on the Jenkins *Email Extension* plugin being installed and configured on the agent.
- **Consequences:** `--to` is optional, falling back to the `SMTP_ADMIN_EMAIL` secret when omitted — both current
  Jenkinsfile `post` blocks rely on that fallback rather than passing `--to` explicitly.

#### D31 — `OD_CONF`/`OD_DATA` env vars resolve through `defaults.py`, not inline literals in `arguments.py`

- **Context:** following D25's CLI-args-driven path design, the `ROOT_FOLDER`-relative fallback values for
  `--onedrive-conf-folder`/`--onedrive-data-folder` were still written as inline `Path` expressions directly inside
  `arguments.py`, duplicating logic that also appeared (differently) elsewhere.
- **Decision:** `src/justifactu/defaults.py` now exposes `default_onedrive_data_folder()`/
  `default_onedrive_conf_folder()`/`default_secrets_folder()` — zero-argument functions composed from `ROOT_FOLDER`
  and `FolderName` segments only, never a raw string literal. `arguments.py` wraps their results in the same
  `os.environ.get("OD_DATA"/"OD_CONF", str(default_...()))` pattern as before — the env-var override behavior from
  D25 is unchanged, only where the fallback value comes from.
- **Why:** part of a broader cleanup (`defines.py` → bare-name enums only; `paths.py` → runtime path composition;
  `defaults.py` → CLI-flag fallback composition) that removed every hardcoded folder/file-name string literal from
  the codebase outside `defines.py`'s own enums.
- **Consequences:** `secret.py`/`vault.py` were also switched from inlining `ROOT_FOLDER / "secrets"` to calling
  `default_secrets_folder()`. `defines.py`'s `FolderPaths` enum (and its two dead module-level `_SHAREPOINT_INPUT_PATH`/
  `_SHAREPOINT_OUTPUT_PATH` helpers) were deleted outright; the one member that was actually used,
  `SHAREPOINT_SYNC_FOLDER`, moved into `paths.py::sharepoint_sync_folder()`.

#### D32 — `main()`'s outer exception handler now actually exits non-zero

- **Context:** `except MainCriticalError` originally only logged the error — every failure (a failed sync, a failed
  phase, a missing `onedrive` binary) produced exit code `0`, identical to success.
- **Decision:** add `exit(1)` to the outer `except MainCriticalError` block.
- **Why:** this is the thing that makes every claim in this document about "a failed run fails the Jenkins build" and
  trips `post { failure }` actually true. Before this fix, the whole premise this document is built on — failures
  being detectable by exit code — was describing intended behavior the code didn't have yet.
- **Consequences:** confirmed `finally` still runs even when `exit()` is called inside `except` (it raises
  `SystemExit`, which doesn't skip `finally`) — the QA report mail still sends on a failed run.
### 11.2 Decisions still open

- **Stage 3 specification.** The credit-card payments step (D22) needs its matching / naming / cleanup rules
  written down — a phase-2 companion to [phase1_individual_bills.md](phase1_individual_bills.md) — before it can be
  implemented.
- **`--no-remote-delete` on the upload step?** It would make the upload non-destructive (never delete on SharePoint),
  at the cost of the pipeline never removing processed bills remotely. Probably not wanted, but decide explicitly.
- **`--dry-run` guard.** Baseline logs the preview only. Alternative: parse the pending-deletion count and fail the run
  if it exceeds a threshold, to catch a bad local state before it propagates.
- **Backup automation.** Is the weekly `conf`-volume backup a manual runbook step (Section 9) or its own small
  scheduled job?
- **Service account provisioning (D9).** Needs a licensed account with library write access and a Conditional
  Access exclusion. Prerequisite, not done.
- **`ONEDRIVE_VERSION` value.** The Dockerfile draft pins a placeholder; confirm the current `2.5.x` release before the
  first build.
- **Production Jenkins invocation never passes `--mode prod`.** Until the `Jenkinsfile`'s `make run` line is
  updated to include it, a "production" run of this pipeline silently stays in test mode (D29).

---

## 12. Glossary

| Term | Definition |
|---|---|
| **OneDrive-for-Linux** | The `abraunegg/onedrive` open-source client. The **only** channel to SharePoint here, invoked twice per run as one-shot syncs. |
| **Download step / upload step** | The two `onedrive --sync` calls `main()` makes: `--download-only` before stages 1–3, `--upload-only` after them. |
| **One-shot sync** | `onedrive --sync` — one full sync cycle in the requested direction, then exit. The opposite of `--monitor`. Exit `0` ⇒ that direction is fully reconciled. |
| **`--monitor`** | Runs `onedrive` continuously. **Not used** in this design. |
| **`--download-only` / `--upload-only`** | Restrict a `--sync` run to one direction. One of each per run. |
| **`--no-remote-delete`** | Modifier for `--upload-only` that suppresses propagating local deletions as remote deletions. Not used by default (see Section 11.2). |
| **`--dry-run`** | Makes `onedrive` report what it *would* do without doing it. Optionally run before the real upload step and logged. |
| **Delta sync** | A sync that transfers only what changed since the last one, using a cursor in `items.sqlite3`. What makes every run after the first one fast. |
| **`cleanup_local_files`** | Config key (`"true"`). Makes `--download-only` apply remote deletions to the local copy, so local `_input` is a true mirror. Essential here — see D21. |
| **`items.sqlite3`** | The client's local state database: item IDs, hashes, parent tree, delta cursor. Persisting it between runs is what keeps runs fast. |
| **`refresh_token`** | Long-lived credential (~90-day sliding expiry) the client uses to get short-lived access tokens headlessly. Rotated on every call. The pipeline's only SharePoint credential. |
| **`--resync`** | Forces the client to discard its delta position and re-scan everything. Slow. Triggered by config or client-version changes; `resync_auth = "true"` auto-accepts the prompt. |
| **`sync_list`** | File in the `conf` directory listing which remote paths to sync. Must contain **both** `/justifactu/_input` and `/justifactu/_output`. |
| **`disableConcurrentBuilds()`** | Jenkins option preventing two builds of the same job at once. Our substitute for a lock file. |
| **Pinned agent** | A Jenkins agent selected by a fixed label. The job always runs there because only that node has the `onedrive` binary and the volumes. |
| **Stage 1 / 2 / 3** | The three processing steps inside `python -m justifactu`: `rename_payments`; `merge_bills_and_payments` (+ `cleanup_processed_files`); credit-card payments. Same process, same local tree, in order. |
| **Microsoft Graph API** | Microsoft's REST API for Microsoft 365. Used by the *previous* design for every SharePoint change; **removed** here (D19). |
| **`--auth`** | CLI flag gating the one-time OAuth bootstrap (D27), via `run_onedrive_auth` — distinct from the regular sync wrapper. |
| **`--phase`** | CLI flag (`1`/`2`/`3`) that runs exactly one pipeline phase in isolation, skipping the OneDrive sync and the `--auth` check entirely. Lives inside `main()` itself (D29). |
| **`Mode` / `--mode`** | `StrEnum` (`PROD`/`TEST`) and matching CLI flag, default `TEST`, selecting which `FolderName` pair (`*_PROD` vs `*_TEST`) the bills/payments folders resolve to (D29). |
| **`carpeta_proves`** | `FolderName.TEST_FOLDER` — the extra path segment inserted only in `Mode.TEST`, between the runtime folder and the test-mode bills/payments folders. Never present in `Mode.PROD`. |
| **Graph fallback** | The reintroduced, download-only Microsoft Graph path (D26) that `main()` falls back to if the primary OneDrive sync fails to download. No equivalent exists for uploads. |
| **`notify.py` / `justifactu-notify`** | Standalone CLI entry point for sending a one-off notification email with optional attachments, used by the Jenkinsfile's `post` blocks instead of `emailext` (D30). |
---

## 13. References

- [abraunegg/onedrive — usage documentation](https://github.com/abraunegg/onedrive/blob/master/docs/usage.md)
- [abraunegg/onedrive — application config & CLI options (`--download-only`, `--upload-only`, `--no-remote-delete`, `--dry-run`, `cleanup_local_files`)](https://github.com/abraunegg/onedrive/blob/master/docs/application-config-options.md)
- [phase1_individual_bills.md](phase1_individual_bills.md) — what the pipeline does with the input once it has it
- `service/onedrive/conf/config`, `service/onedrive/conf/sync_list` — the OneDrive client configuration
- `service/agent/` — the Jenkins agent image and entrypoint
- `compose.yml` — the current (pre-migration) two-container setup
- `src/justifactu/main.py`, `src/justifactu/process.py` — the code of record for the orchestration and stages 1–3
