# holoclip

Zero-cost provider comparisons and evidence-bound qualification are documented in
[the real-media benchmark workflow](docs/provider-benchmark.md). A free API smoke
response does not grant production quality qualification.

Holoclip aims to learn selection and editing from human-made hololive clips, understand
unseen streams through audio and video, and drive deterministic rendering with automatic
QA and repair. Its cloud AI requests must stay within verified zero-cost routes.

The first milestone is deliberately narrow:

> Given one long stream, surface a small set of moments that a human would genuinely consider worth clipping, and explain the story structure behind each one.

Video editing, thumbnails and publishing are intentionally deferred until this ranking problem is proven.

## Current v0.1 flow

    local media file
        -> FFmpeg audio chunks
        -> qualified ASR segment timestamps
        -> overlapping transcript chunks
        -> broad discovery with qualified text_reasoning_fast + structured_json
        -> overlap dedupe
        -> editorial selection with qualified text_reasoning_strong + structured_json
        -> measured boundary / multi-cut plan when learning is ready
        -> saved candidates and automatic evaluation artifacts

Production AI access uses capability-based, quality-qualified routing with a persistent
quota ledger and bounded stage timeouts. Missing qualified/free routes stop processing
with preserved artifacts. See [production provider setup](docs/production-provider-runtime.md).

## Public-video validation is local-only

The experimental GitHub-hosted real-video workflow has been **deleted**.
The two initial hosted attempts, [run #37946788956](https://github.com/Kataage/holoclip/actions/runs/37946788956)
and [run #37948045092](https://github.com/Kataage/holoclip/actions/runs/37948045092),
searched public metadata but **all five full clip probes in each run failed**;
the second run classified all five as
`YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE`. Neither acquired a finished/source
video section or demonstrated real editor alignment. Retrying real yt-dlp
downloads on GitHub Actions is therefore not a useful continuous quality
signal. **GitHub CI runs offline tests and mocks only**; no public YouTube
download is attempted by any configured workflow, including pushes to main.

Real media acquisition belongs to the existing, opt-in **native Windows**
collector/Task Scheduler path. The Windows job can run without uploading
video, providing URLs, Docker, a paid AI service or hand-annotated gold.

Local metadata resolution now halts after **three consecutive**
YouTube bot/interactivity challenges or HTTP 429 errors during one
invocation. It preserves every pending candidate and the round-robin
cursor, then allows the next scheduled execution to retry normally; it
does not circumvent YouTube restrictions, invent positive video data
or automatically request credentials. Other errors do not trigger the
hostwide pause. The collector reports
`metadata_access_paused`, `metadata_access_pause_reason` and sanitized
`metadata_failure_category_counts`. Offline
`examples-collector-doctor --no-check-tools` displays these along
with `local_retryable_metadata_failure_summary` without emitting stored
YouTube URLs or raw stderr in the diagnostic output.
A pause is never an editor mismatch; `ready=false` stays mandatory.

## Collect real public-source constructed pattern measurements

The source-pattern collector combines **metadata-only public clip discovery**
with rotating, bounded yt-dlp video checks. Each scheduled invocation targets
**new** distinct candidate streams measured **in that invocation**, not the
lifetime corpus total; previously achieved coverage cannot permanently stop
subsequent daily runs. A run with only old historical measurements reports
`UNKNOWN_NO_NEW_PUBLIC_VIDEO_PATTERN_MEASUREMENTS` instead of new success.
Public metadata discovery also wraps back to the first relevance-search page
after 50 results or an empty page to detect newly published public clips
without unbounded `ytsearchN` requests. It reuses the acquired sections,
persists the existing SHA-bound five-pattern histories for each sampled
source candidate (up to two per clip per cycle), and returns a
source-diverse report with explicit unknowns. No cloud model, GPU, URLs, gold
labels or paid service are required:

```powershell
uv run --extra media holoclip examples-collect-source-patterns --cycles 3
uv run --extra media holoclip examples-source-controls-report
```

Use `--no-discover` to work from an existing pending shortlist without additional
searches. Bounded network media retrieval still requires yt-dlp/FFmpeg and access
to public ended videos. The command is an **invoked** bounded batch, not a
persistent background daemon or proof that live media has already been collected.
Output may contain zero measured cases; unsupported or failed acquisitions remain
unknown, never measured negatives or human-editor gold. `ready=false` holds.

### Offline unattended collector diagnosis

The no-download, no-API doctor can be run **before installing the scheduled
task** and again after any unattended execution:

```powershell
uv run --extra media holoclip examples-collector-doctor
# To inspect saved evidence without checking installed tools:
uv run holoclip examples-collector-doctor --no-check-tools
```

It checks whether `uv`, FFmpeg, FFprobe, and a supported Deno/Node runtime
are installed (no network or media download), summarizes the persisted
pending queue, most recent probe failure categories, recorded new samples
vs cumulative totals, and the latest local launch/collector failure. It
never claims real editor precision, verified source identity or readiness.
The launcher writes a **separate**
`data/examples/source-pattern-launcher-last-run.json` even when `uv`
or FFmpeg is unavailable, so a previous successful collector report cannot
hide the most recent failed scheduled invocation.

### Independent real-media stage diagnosis

The collector's saved `cycle_diagnostics[].stage_diagnostics` now separates
**finished-video checksum records**, sampled original-section records,
source/finished **visual correspondence statuses**, constructed-pattern
control statuses/errors, and cross-window revisit probe modes. Previously
the combined `source_pattern_attempt_outcomes` preferred a successful
constructed-control status and could hide a simultaneous
`UNKNOWN_NO_VISUAL_SEQUENCE` outcome. That combined legacy field remains
for compatibility, but it must never be interpreted as an original match.

The read-only `examples-collector-doctor --no-check-tools` command aggregates
the new funnel under `last_run_stage_funnel` and provides
`last_run_stage_evidence_gap` to distinguish no finished video record, no
source-video section, missing pattern control, and missing visual-source
corroboration. Old audits without this schema return an explicit
`UNKNOWN_NO_STAGE_DIAGNOSTICS`. No URL, media, model or operator-supplied
label is required to inspect saved evidence. **The raw stage counts are
persisted records, not independent proof that live public media was
downloaded**, original identity verified, or quality achieved; the existing
deduplicated measured-media summary remains separate. Every new field
preserves `ready=false`.

### Confirm saved public-source video bytes locally (no download)

The unattended collector now performs a **bounded post-run cache attestation**
before writing its last-run report. It reads the latest source window
observations, locates existing yt-dlp `section-integrity.json` receipts,
recomputes SHA-256 from the exact stored bounded video bytes, and checks the
persisted source URL, ID, requested interval, filename and SHA. Missing files,
changed bytes, forged receipts and symlinks are reported as
`UNKNOWN_*`/`BLOCKED_*` instead of a successful real-video measurement.
The collector report includes `source_cache_byte_attestation` even when no
matching real clip/source pair has been established.

The read-only doctor independently rechecks **current disk bytes** and
shows `current_source_cache_byte_attestation` alongside the **historical**
`last_run_saved_cache_attestation`:

```powershell
uv run --extra media holoclip examples-collector-doctor --no-check-tools
# For a customized media directory:
uv run --extra media holoclip examples-collector-doctor --no-check-tools --media-output "D:\\video-cache"
```

The unattended worker audits up to four **recently attempted** clips rather than a stale first-in-queue prefix; the independent doctor uses a deterministic sorted prefix. Each audit checks at most 48 unique sections;
if the queue exceeds the budget, `UNKNOWN_PARTIAL_BOUNDED_CACHE_AUDIT`
explicitly prevents claiming full coverage. `--no-audit-media-cache` skips
disk rehashing when only metadata status is needed. Neither a matching
receipt nor stored bytes prove that the footage is the actual Hololive
original, that YouTube is reachable right now, that video timestamps are
precise, or that edited cuts are correct. No GPU/cloud model, user-provided
URLs or ground-truth labels are required. `ready=false` remains mandatory.

### Native extraction deadlines and unattended recovery

All yt-dlp subprocess invocations now use explicit deadlines instead of
waiting forever: 30 seconds for runtime-version checks, 180 seconds for
yt-dlp installation/refresh, 240 seconds for metadata searches/probes,
20 minutes for a bounded video section, and 30 minutes for a full audio/media
acquisition. A timed-out native process produces a diagnostic
`YtDlpTimeout` and is **not retried** just because a fresh nightly yt-dlp
version might exist. The next scheduled run can resume from persisted
cursors when the direct process has ended; no timeout counts as a negative
source match or reviewed gold. The subprocess runner now creates a private POSIX process group or
Windows process group, and on timeout sends a group kill (POSIX) or
`taskkill /PID /T /F` (Windows) to target the original process **and
its descendants** before bounded pipe draining. A failed cleanup is
reported as `TREE_CLEANUP_UNCONFIRMED` instead of claiming recovery.
This has real OS-process regression tests on Ubuntu and Windows that
spawn an offline grandchild and verify it cannot execute after timeout.
Descendants that intentionally detach into a separate session or escape
the Windows parent tree remain outside the guarantee; no user or
unrelated process groups are targeted.

### Persistent fair metadata resolution (2026-10-09)

Autonomous public-clip discovery now rotates **metadata probe attempts**
across the `discovered` and metadata-`retryable` backlog using a
persisted `corpus_metadata_pending_round_robin` cursor. The cursor is
checkpointed before each network probe; a permanently failing clip cannot
occupy the first `--scan-limit` candidates on every unattended run.
Already-`queued` learning clips do not consume probe slots. When running
metadata-only evidence collection, ingestion failures that already have
canonical source URLs/stream assignments stay in the learning queue and
are not reassigned to unverified visual probes. The report distinguishes
`metadata_resolution_attempted`, `metadata_retryable_attempted` and
`metadata_resolution_pending`. These count metadata work only, not real
video downloads, human gold or successful original matches.

### Public source links reach the unreviewed measurement queue

The unattended `examples-collect-source-patterns` command uses
`discover_only=True`. Newly discovered public human clips with **one
clearly cited original-stream link** now enter the existing
`source_visual_pending` queue, just like ambiguous description links:
the original link is persisted as an **unverified metadata-only candidate**
until bounded real video has actually been acquired. Previously, this
common single-link case was sent directly to the separate corpus
training/holdout queue and **never reached the source-pattern collector**.
This change is scoped to metadata-only discovery; normal human example
ingestion still uses its existing split and policy checks. No link is
declared a verified original and no reviewer labels are invented.
Counters `source_visual_newly_pending` and
`explicit_source_visual_newly_pending` distinguish newly queued
visual evidence work from existing pending clips. Existing training/holdout
assignments are not rewritten or silently migrated.

### Finite output/source pairing revisits after original-source scans

After every saved original-source scan reaches its deterministic budget,
the collector attempts a **finite** second pass against other finished-clip
windows. For each eligible finished window and source-candidate batch it
checks only the first two groups of three original windows (at most six
source intervals per candidate/finished combination). Separate durable
`visual_cross_revisit_step` and layout metadata resume across runs without
resetting old source-scan cursors, replacing saved source hashes or falsely
extending original-search coverage. A changed shortlist, finished-window
layout or per-run candidate batch size is explicitly blocked instead of
reinterpreting the old cursor. All media acquisition, identity, and current
rights checks remain in force.

Once the finite paired-revisit budget is consumed, the collector rechecks
candidate public metadata and policy before declining further media
downloads with `UNKNOWN_ALL_CANDIDATE_SOURCE_SCANS_EXHAUSTED`. The new
revisit fields remain **sample-only** observations, not a full source by
output Cartesian search, an editor negative, gold source identity, recall
or permission to change `ready=false`. Missing or unsafe current metadata
does not count as successful pair verification.

### Opt-in shared-IP yt-dlp coordination across Windows PCs (free, no Docker)

Two computers behind one router/public IP must **not** assume their
independent local SQLite gates enforce a combined access rate.
Holoclip now includes an **optional Python standard-library LAN
coordinator**. On the PC designated as coordinator (PC-A), a single
service owns a durable SQLite exclusive lease and a 20-second inter-job
gap for **all configured clients**, plus the shared six-hour YouTube
429/bot-challenge cooldown. The coordinator is not a video downloader;
no URLs, clips, titles, cookies or account data are sent to it.

One-time setup on the trusted private network:

1. On PC-A, from the Holoclip repository, run
   `uv run holoclip examples-ytdlp-lan-init`. This creates a 256-bit
   random secret at `data/runtime/yt-dlp-lan-secret.txt`; the secret
   is **never printed** and existing secrets are never overwritten.
2. Copy this file **securely** to PC-B (same restrictive access rights).
   Protect it as a credential; do not commit or upload it.
3. Start the coordinator on PC-A using its **specific private-LAN IP**:
   `uv run holoclip examples-ytdlp-lan-serve --bind-ip 192.168.1.20`
   (replace the address with PC-A's real LAN IPv4). Listening on a
   public address or `0.0.0.0` is rejected. Only permit the other
   trusted PC's private address through the Windows Firewall on port
   8878. Keep this service running during collector jobs; if it stops,
   all configured clients stop accessing YouTube.
4. On **BOTH** PCs (including PC-A), set the local Holoclip process
   environment variables below. Replace the example IP and absolute
   paths with each PC's real values. The server and clients share the
   **same secret bytes**, while the file path is local to each PC.

```powershell
$env:HOLOCLIP_YTDLP_COORDINATOR_URL = "http://192.168.1.20:8878"
$env:HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE = "D:\\AI\\holoclip\\data\\runtime\\yt-dlp-lan-secret.txt"
uv run holoclip examples-ytdlp-lan-status
```

For unattended Windows Task Scheduler invocations, supply the same
environment via the user's normal local Holoclip `.env` configuration
(the native collection CLI loads it before constructing the yt-dlp
provider); environment variables set in one interactive PowerShell
window do **not** automatically reach future scheduled sessions.
Do not put secrets in command-line flags, task arguments, GitHub
repository files or CI secrets. To stop all network collection safely,
stop the coordinator or remove its outbound availability; clients
configured to require it will refuse to run yt-dlp.

All client requests/responses are HMAC-SHA256 authenticated with a
timestamp and one-use nonce; a request cannot be replayed. Secret
material is never sent across the network. The connection uses
**unencrypted HTTP on a trusted private LAN only** (no external/public
hosting); HMAC protects request integrity/authentication, **not**
confidentiality or availability. Client and coordinator clocks need
to be synchronized within 90 seconds. The client explicitly ignores
HTTP proxies and redirects. Failed/invalid/unreachable coordinator
access **never falls back to a local-only gate**. A crashed coordinator
retains unexpired leases via its own SQLite; an abandoned lease
eventually expires based on the bounded yt-dlp process deadline.

#### One-command per-PC LAN client setup (no secret in settings)

After PC-A's coordinator has been configured, copy the **same** shared
credential through a secure local channel to PC-B. On **both PCs**, run
the following from the Holoclip directory (use the actual PC-A private
IPv4 address and the local credential path on each PC):

```powershell
uv run holoclip examples-ytdlp-lan-configure --url http://192.168.1.20:8878 --secret-file "D:\AI\holoclip\data\runtime\yt-dlp-lan-secret.txt"
uv run holoclip examples-ytdlp-lan-status
```

This writes `data/runtime/yt-dlp-lan-client.json` (Git-ignored) containing
**only** the private coordinator URL and the absolute path to the existing
credential file. It never copies, transfers, or prints secret bytes.
Identical repeated setup is idempotent; replacing a different saved profile
requires `--replace`. Missing or invalid credentials and unsafe public
coordinator addresses are rejected without touching YouTube.

Existing deployments with both `HOLOCLIP_YTDLP_COORDINATOR_URL` and
`HOLOCLIP_YTDLP_COORDINATOR_SECRET_FILE` still work and take precedence
over saved JSON. Partial environment overrides fail closed, even with a
healthy saved profile. To choose an alternate config location, use
`--config-file` during setup **and** set `HOLOCLIP_YTDLP_COORDINATOR_CONFIG`
to its **absolute path** in each client process / scheduled task environment.
Missing or invalid explicitly selected config files fail closed.

The authenticated status command now loads `.env` like the collector does,
so the two commands read the same environment/profile settings. A status
response only proves the addressed broker authenticated, not that all PCs
are enrolled or YouTube access/clip quality has been established.

Configuring a PC also writes an adjacent **non-secret required marker**.
If the saved profile is later deleted or corrupted while that marker remains,
Holoclip fails closed instead of switching silently to local-only pacing.
The marker is not a credential. An explicit custom config path likewise
fails closed when missing. Removing both profile and marker opts out and
must never be done on PCs intended to share pacing.
`ready=false` remains mandatory.
#### Trusted local secret import and offline LAN connection diagnosis

Use an independently trusted way to **transport** the PC-A credential to PC-B
(for example, a USB drive under your control or an already authenticated,
encrypted file-transfer session with its host identity verified). The
Holoclip coordinator's LAN HTTP protocol is **not encrypted** and must
**never** be used as a secret-transfer channel or host-identity bootstrap.
After delivery, on PC-B, import the existing credential without copying
its bytes into a terminal command, repository, logs, or JSON settings:

```powershell
uv run holoclip examples-ytdlp-lan-import-secret --source-file "E:\transfer\yt-dlp-lan-secret.txt" --secret-file "D:\AI\holoclip\data\runtime\yt-dlp-lan-secret.txt"
uv run holoclip examples-ytdlp-lan-configure --url http://192.168.1.20:8878 --secret-file "D:\AI\holoclip\data\runtime\yt-dlp-lan-secret.txt"
uv run holoclip examples-ytdlp-lan-doctor
```

Replace all example paths/PC-A address with real local values. The import
validates the existing source file before writing, refuses symbolic links,
never overwrites a different destination credential, and treats an
identical previously imported credential as already complete. It does **not**
contact PC-A, read YouTube, transfer secrets over the network or delete the
source. Restrict access to the source and destination on Windows using
appropriate NTFS ACLs; Python file mode 0600 alone is **not proof** of
Windows ACL isolation. Remove the extra transport copy securely after
confirming setup. Do not put either secret file in Git or cloud storage.

The read-only `examples-ytdlp-lan-doctor` produces safe JSON codes:
`NOT_CONFIGURED`, `LOCAL_YTDLP_COORDINATOR_CONFIGURATION_INVALID`,
`LOCAL_YTDLP_COORDINATOR_UNAVAILABLE`,
`LOCAL_YTDLP_COORDINATOR_RESPONSE_UNTRUSTED` or
`AUTHENTICATED_LOCAL_LAN_COORDINATOR` with a recognized remote gate state
(`AVAILABLE`, `ACTIVE`, `WAIT_GAP`, `COOLDOWN`). It exits nonzero unless
authentication succeeds, never prints secrets, paths or the URL, never
requests a reservation and never starts yt-dlp. The existing
`examples-ytdlp-lan-status` command uses this same result format.

**Important:** authenticated status from each machine is independent:
it does **not** prove the other machine is enrolled, that firewall
policies are correct, or that both Windows PCs have actually completed
two-host testing. Only a real separately run PC-A / PC-B verification
can establish deployment behavior. Always retain `ready=false` until
separate original-video/editor reference quality gates are satisfied.
#### SSH public-key-only LAN secret pull and Windows NTFS ACL audit

For Windows-to-Windows transfer without moving a USB drive, **opt in** to
an already configured OpenSSH SFTP server on PC-A. You must independently
verify the PC-A SSH host-key fingerprint and put that trusted key in a
dedicated `known_hosts` file on PC-B **before** running the command.
Copying unverified `ssh-keyscan` output into known_hosts is NOT safe
host identity verification. Configure public-key authentication to PC-A
(password prompts and unknown-host prompts are deliberately disabled).
Do not expose SSH on the public internet; allow only the trusted PC-B
address through PC-A's local firewall. This process only transfers the
small existing secret file, never downloads public video.

On PC-B, from the Holoclip directory (replace example IP, username,
known-hosts path, remote Windows SFTP path and local secret path):

```powershell
uv run holoclip examples-ytdlp-lan-pull-secret --host 192.168.1.20 --user holoclip --remote-path D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt --known-hosts "D:\AI\holoclip\data\runtime\pc-a-known-hosts" --secret-file "D:\AI\holoclip\data\runtime\yt-dlp-lan-secret.txt"
uv run holoclip examples-ytdlp-lan-secret-acl --secret-file "D:\AI\holoclip\data\runtime\yt-dlp-lan-secret.txt"
uv run holoclip examples-ytdlp-lan-configure --url http://192.168.1.20:8878 --secret-file "D:\AI\holoclip\data\runtime\yt-dlp-lan-secret.txt"
uv run holoclip examples-ytdlp-lan-doctor
```

The SSH transfer requires `sftp` from OpenSSH in PATH and **fails closed**
when the known-hosts file is absent, host key is untrusted/changed, SSH
authentication fails, the remote path is unsafe or the destination differs
from an existing secret. It uses `StrictHostKeyChecking=yes`, disables
password/keyboard-interactive prompts, SSH agent forwarding, SSH proxies
and jump hosts, and imposes a transfer timeout. The remote path must be
an absolute SFTP path without spaces, wildcards or traversal components.
Depending on PC-A's OpenSSH SFTP root, Windows drive paths may use
`D:/...`; test the path using an already authenticated SFTP session.

On Windows the command first restricts the temporary staging directory
to the **current user, SYSTEM and Administrators**, then imports the
credential and restricts its NTFS ACL the same way. It never prints
secret bytes, creates GitHub secrets, or sends a credential through
the coordinator's *unencrypted* HTTP. A PowerShell 5.1 ACL helper
rejects inherited or unexpected allow rules and refuses unexpected
file owners/symlinks. To audit a pre-existing local credential, run
`examples-ytdlp-lan-secret-acl` without options other than its path;
use the explicit `--apply` only if you want to reset that file's
access rules. On non-Windows systems the ACL doctor reports unsupported;
it does not falsely claim NTFS protection.

SSH transport success and secure NTFS ACLs do **not** prove that both
PCs have actually enrolled, that the coordinator is reachable from both,
or that the eventual video/edit quality is production ready. Only
site-free loopback/CI simulations have been exercised so far;
`ready=false` remains unchanged.
#### Local CI preflight when GitHub hosted private runners are blocked

The private GitHub Actions budget can be exhausted despite clean code.
This repository retains **mandatory** Linux full-Pytest + Windows offline
CI before merging any code PR. A failed GitHub job with zero steps
and no assigned runner is **not** a failing Ruff/Pytest test run.

An additional optional, safe one-command fallback can run the CI checks
available on a Windows development PC, **without Docker or accessing
YouTube**. It requires the repo's existing dev/media environment to
have been installed earlier, and verifies the checkout is clean at the
specified exact PR commit.

```powershell
# Example only: replace the SHA with the current PR head from GitHub
uv run --offline --no-sync python scripts/verify-ci-local.py
```

The script always runs Ruff. On Windows, it runs the same focused
Windows Pytest suites (including LAN pair-collection tests), native
process-tree tests and PowerShell 5.1 mock/sandbox smoke checks used
in GitHub Actions. On Linux it runs full Pytest. Test subprocesses
are constrained to local, offline uv usage; no dependencies are
automatically downloaded. Use `--plan` to list checks without
running them. The optional `--full-windows-pytest` flag also
tries Linux-oriented full Pytest on Windows.

The script emits only safe JSON status codes and exit statuses without
echoing potentially sensitive local test output. It checks Python 3.12,
a clean tracked worktree, available uv, and exact SHA if supplied. A
successful **local** outcome is deliberately named
`LOCAL_PREFLIGHT_PASSED_NOT_GITHUB_CI`; flags
`github_actions_ci_verified=false`,
`both_platforms_verified=false` and
`ready=false` are always set. A local check **never**
authorizes merge in place of both GitHub Linux and Windows gates.

For private GitHub Actions that finish in seconds with zero steps,
the repo owner must check account **Settings → Billing and licensing /
Usage → Actions / Budgets and alerts** (UI labels may vary) and the
specific failed run's check annotations. GitHub Free's included
private-repository runner minutes may be depleted. Do not enable
spending, make the repository public, or reroute CI to a user-PC runner
merely to bypass the gate without the owner's explicit approval.
The workflow already cancels superseded PR runs to reduce future waste.

#### Strict Windows Firewall scope review for LAN coordinator

The `examples-ytdlp-lan-setup-doctor`,
`examples-ytdlp-lan-onboard-check`, and
`examples-ytdlp-lan-task-prepare` commands share the Windows
ActiveStore firewall rule inspector. It now **rejects certification**
if an additional enabled inbound allow rule covers the coordinator
TCP port with a scope broader than PC-B's exact literal IPv4, even
when a separate correct rule also exists. CIDR/ranges,
`LocalSubnet`, `Any`, IPv6 addresses, other destination
addresses, protocol `Any`, wildcard/multiple TCP ports and
unknown rule selectors are all treated conservatively. Known
unrelated TCP ports or UDP-only rules do not themselves block an
exact rule. Missing or unrecognized fields fail closed with
`INCOMPLETE_RULE_INSPECTION`; broad scopes report
`BROAD_INBOUND_ALLOW_RULE_OBSERVED`.

This is intentionally strict; a different application's matching
allow rule can produce a **false alarm**, but must never silently be
accepted as PC-B-only. To resolve a warning, review the active
Windows Firewall inbound rules for the exact coordinator TCP port.
Do not add blanket allow rules or disable the firewall. No Holoclip
inspector command edits the firewall; Windows Firewall policy
effectiveness is still **unverified**, and production stays
`ready=false` pending real two-PC testing. GitHub CI exercises
synthetic PowerShell firewall rules only, without opening ports.

#### One-command PC-B LAN setup using preverified PC-A host key

On Windows **PC-B only**, this command composes the existing pinned-key
SFTP credential import, NTFS credential ACL protection, fail-closed
client profile creation and signed status-only coordinator check. It does
**not** set up SSH trust or copy a new credential from PC-B to PC-A.

Before running it, independently verify PC-A's SSH host-key fingerprint,
place the verified key in a dedicated `known_hosts` file on **PC-B**, and
configure noninteractive public-key SSH login to PC-A. PC-A must have
already generated the shared credential. A found host-key entry is **not
proof** the fingerprint was independently authenticated. The program
never accepts a new host key automatically.

First, on PC-B, preview **without SFTP connection or filesystem writes**:

```powershell
uv run holoclip examples-ytdlp-lan-pc-b-prepare --coordinator-ip 192.168.1.10 --ssh-user pc_a_account --known-hosts "D:\AI\holoclip\data\runtime\pc-a-known-hosts" --remote-path "D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt"
```

After reviewing the checks, explicitly opt in to import/configuration:

```powershell
uv run holoclip examples-ytdlp-lan-pc-b-prepare --coordinator-ip 192.168.1.10 --ssh-user pc_a_account --known-hosts "D:\AI\holoclip\data\runtime\pc-a-known-hosts" --remote-path "D:/AI/holoclip/data/runtime/yt-dlp-lan-secret.txt" --apply
```

Replace all example values. The host and coordinator use the **same**
literal PC-A RFC1918 address; optional overrides are `--ssh-port`,
`--coordinator-port`, `--secret-file`, and `--config-file`.
The remote SFTP path currently accepts only safe absolute slash-style
paths **without spaces** or wildcard characters. A missing key pin,
malformed destination path, legacy coordinator env override, different
existing secret/profile, or failed NTFS ACL check blocks the sequence.
Existing secret bytes are never overwritten, rotated or printed.

Explicit apply first checks the pinned host entry and validates the
local bootstrap preview, then performs only a **public-key-only pinned
SFTP download** into ACL-protected staging, imports the shared key into
a protected local credential file, and writes the latched client
profile. If these local steps succeed, a signed **status-only** request
checks coordinator authentication; it neither reserves a yt-dlp lease
nor accesses YouTube. If PC-A is not running, the command reports
`LAN_PC_B_PREPARED_STATUS_UNVERIFIED` rather than claiming the peer
was authenticated. In that case the CLI returns a nonzero exit status\nso unattended setup cannot treat missing authentication as success. It\nmay be rerun after PC-A is online. On partial
failure, inspect the completed stages before retrying: it does not
silently roll back existing credentials or client profiles.

This command does not install an SSH server, generate an SSH key,
disable Windows Firewall, start tasks/coordinator, prove physical
PC-A/PC-B identities, or qualify effective firewall policy or media
readiness. All outcomes preserve `two_host_verified=false` and
`ready=false`. In CI, SFTP and status are mocked: no real PC or
YouTube connections are made.

#### One-command PC-A LAN setup (safe staged preview, opt-in apply)

For a new Windows PC-A, the combined setup command reduces three
independent preparations to one **dry-run by default**. It reuses the
same guarded operations and does not bypass their validations:

    uv run holoclip examples-ytdlp-lan-pc-a-prepare --bind-ip 192.168.1.10 --peer-ip 192.168.1.21

Replace both example IPs with PC-A and PC-B's **actual, distinct**
private IPv4 addresses. Optional deployment settings: --port,
--task-name, --secret-file, --config-file and --state-path.
The JSON report shows the preview outcome of local credential and
client-profile setup, exact-peer Windows Firewall rule preparation,
and limited current-user Task Scheduler registration. In a fresh
installation, missing credentials and firewall allow rules are normal
**task preview blockers** that preceding stages would address.

Only after reviewing the preview, rerun on **PC-A with an elevated
PowerShell for the same Windows user** and explicitly pass --apply:

    uv run holoclip examples-ytdlp-lan-pc-a-prepare --bind-ip 192.168.1.10 --peer-ip 192.168.1.21 --apply

The apply path first validates *all three* stages before any changes,
then locally creates/protects a secret and persistent client profile,
adds a one-PC-B-only Private-profile inbound TCP allow rule, and
registers a non-elevated current-user logon task **without starting it**.
Each underlying step revalidates safety before its own write; missing
PowerShell/permissions, uninspectable or broad firewall rules,
existing task-name conflicts, unsafe paths, legacy env overrides,
public addresses and a changed preflight prevent unsafe continuation.
An already-scoped firewall without a conflicting reserved rule name
does not require a new allow rule.

A failed step may leave preceding protected local state in place:
Holoclip **does not delete an existing credential or roll back a
firewall rule** during an interrupted setup. Instead it reports the
last completed stage and a conservative partial-change warning.
Manually review that stage before retrying; the existing lower-level
diagnostic commands remain available for recovery. Re-running on a
configured system is intentionally not a destructive reset.

The command does not start/verify the coordinator, configure SSH,
trust a host key, import the secret to PC-B, transfer credentials,
contact YouTube or certify a physical LAN. Separate PC-B secure
credential enrollment, two-host observations and actual firewall
effective-policy verification remain required. Regardless of how
many stages succeed, ready=false and two_host_verified=false.
GitHub CI tests simulate only the Windows operations and never
modify users' machines.

#### Preview-first PC-A Windows Firewall preparation (explicit opt-in)

On **PC-A only**, Holoclip can preview and explicitly create a single
narrow inbound allow rule without replacing existing rules. The command
requires **distinct literal RFC1918 PC-A/PC-B IPv4 addresses** and
refuses wildcard/public/loopback addresses. PC-A's bind address must
actually be present on one local network interface with a **Private**
Windows network profile, and that firewall profile must be enabled.

Run the preview first, without administrator rights:

    uv run holoclip examples-ytdlp-lan-firewall-prepare --bind-ip 192.168.1.10 --peer-ip 192.168.1.21

After reviewing the reported rule and confirming the PC-B host identity,
use an **administrator PowerShell window on PC-A** to apply:

    uv run holoclip examples-ytdlp-lan-firewall-prepare --bind-ip 192.168.1.10 --peer-ip 192.168.1.21 --apply

Replace those sample IPs; the optional --port defaults to 8878.
The fixed rule name is HoloclipYtDlpLanTcp-<port>.
The created rule uses **TCP + exact local PC-A IP + exact remote PC-B IP +
one port + Private profile only + edge traversal blocked**.
The command makes *no changes* in preview mode and refuses to overwrite
a same-name rule. If an exact-peer rule already exists, it reports
LAN_FIREWALL_ALREADY_SCOPED without writing anything. Any broad or
ambiguous inbound allow rule, unavailable inspection, name conflict,
non-Private interface, disabled Private firewall profile, or a changed
preflight blocks creation. The command does not try to remove/repair
existing rules or disable the firewall. A failed/uncertain apply requires
manual reinspection; **do not assume that no rule was created**.

The helper is intentionally separate from the read-only setup inspectors.
It never changes Task Scheduler, SSH trust, secrets, or yt-dlp pacing.
New-rule verification is still **syntactic observation of ActiveStore
rules**, not effective Windows firewall-policy attestation or proof that
PC-B is the only host on the physical LAN. It does not test real host
reachability or actual packet filtering. ready=false,
firewall_effective_policy_verified=false and
two_host_verified=false remain mandatory. CI uses synthetic
PowerShell cmdlets only; no real firewall rules are opened.

#### Safe PC-A Task Scheduler registration: preview before apply

After PC-A's local credential/client setup and **independent review** of the
Windows Firewall rule that allows **only PC-B's private LAN IPv4** to access
PC-A's coordinator TCP port, the guarded task installer previews all gates:

```powershell
# Replace these example addresses with the actual LAN addresses on PC-A / PC-B
uv run holoclip examples-ytdlp-lan-task-prepare --bind-ip 192.168.1.10 --peer-ip 192.168.1.21

# Explicitly allow a non-elevated logon task registration only after the
# credential ACL, exact-peer allow rule and absent task are verified:
uv run holoclip examples-ytdlp-lan-task-prepare --bind-ip 192.168.1.10 --peer-ip 192.168.1.21 --apply
```

For custom deployment settings, use `--port`, `--task-name`,
`--secret-file` and `--state-path`. Bind and peer must be
**distinct literal RFC1918 IPv4 addresses**, not a wildcard, loopback or
public endpoint. Preview is **read-only**; it returns fixed-code check
results for secret validity, read-only Windows ACL verification, scoped
inbound firewall rule and task name availability. Missing permissions or
ambiguous observations block an apply, rather than silently assuming a
task does not exist.

Apply only registers a **limited-privilege, current-user logon task**;
it never starts it or downloads video. The underlying PowerShell
installer no longer uses `Register-ScheduledTask -Force`, so an
existing same-name task cannot be overwritten, even if one appears
during registration. A second read-only inventory probe just before
install reduces accidental conflicts. An installation failure does not
delete the existing credential or mutate firewall rules.

This command **does not create a firewall rule or change SSH host trust**.
It only checks whether a narrow inbound allow rule was observed.
Windows firewall active-store inspection is **not** proof of the
effective OS firewall policy; a successful task preview or registration
is never production qualification. The task is not started until the
owner separately verifies bind address, peer restriction and network
scope. No user-PC task is actually created in CI; Windows and Linux
tests mock the registration and use disposable credentials only.
All reports retain `ready=false` and `two_host_verified=false`.

#### Preview-first local LAN client bootstrap (Windows, opt-in)

A two-PC deployment can now prepare *local* coordinator client state in
one command on each Windows PC without manually invoking secret creation,
NTFS ACL hardening and client profile configuration as separate steps.
The **default is read-only preview**:

```powershell
# Run on PC-A first (replace with PC-A's actual private IPv4)
uv run holoclip examples-ytdlp-lan-bootstrap-local --role pc-a --coordinator-ip 192.168.1.10

# After reviewing the plan, explicitly opt in to local writes on PC-A
uv run holoclip examples-ytdlp-lan-bootstrap-local --role pc-a --coordinator-ip 192.168.1.10 --apply

# On PC-B, first import the *existing same* PC-A credential by independently
# verified pinned SFTP / trusted local file; do NOT create a different key
uv run holoclip examples-ytdlp-lan-bootstrap-local --role pc-b --coordinator-ip 192.168.1.10
uv run holoclip examples-ytdlp-lan-bootstrap-local --role pc-b --coordinator-ip 192.168.1.10 --apply
```

For custom storage, use `--secret-file` and
`--config-file`; for a nondefault server port,
`--coordinator-port`. Existing credentials and differing saved
profiles are **never overwritten or silently rotated**. On PC-A, a new
credential is created only after `--apply` and immediately
locked down with the existing Windows NTFS ACL safeguard; if this
verification fails, the newly created credential is removed.
PC-B refuses to create its own independent credential: the existing
credential must be securely imported first. The existing required-client
marker/latch is retained to prevent accidental local-only yt-dlp fallback.

The default paths are in the Git-ignored `data/runtime` tree;
the bootstrap refuses to write within tracked checkout locations other
than its ignored `data` runtime directory. Public Git commits
must never contain credentials, known_hosts, or real local settings.
Reports use fixed codes, no secret bytes or file paths and always retain
`ready=false`, `two_host_verified=false`.

This only prepares the **local credential and client profile**.
It deliberately does *not* install Task Scheduler jobs, enable a
listener, modify Windows firewall, install SSH, accept unknown host keys,
move secrets across PCs or download media. On PC-A, narrow firewall rules
and task registration still need explicit owner action. Use the existing
`examples-ytdlp-lan-onboard-check` for read-only two-PC
diagnostics and further guidance. A successful local bootstrap alone
does not prove shared-IP pacing or production qualification.

#### Bounded, safe-code-only LAN evidence history and repeated-risk checks

PC-A can now keep a local, small, Git-ignored audit of actual LAN
diagnostic observations, without saving PC IPs, SSH usernames,
known_hosts, shared credentials, SSH stderr, file paths, raw remote
reports, coordinator identities, or media metadata. The default command
performs **only the existing read-only paired diagnosis**, and does not
write a file or acquire a media/coordination lease:

    uv run holoclip examples-ytdlp-lan-evidence-capture --host 192.168.1.21 --user pc_b_account --known-hosts "D:\AI\holoclip\data\runtime\pc-b-known-hosts" --remote-repo "D:\AI\holoclip"

Add **--record** to explicitly save one sanitized, fixed-code observation:

    uv run holoclip examples-ytdlp-lan-evidence-capture --host 192.168.1.21 --user pc_b_account --known-hosts "D:\AI\holoclip\data\runtime\pc-b-known-hosts" --remote-repo "D:\AI\holoclip" --record

Replace the examples with actual private-LAN settings. The default
history lives in data/runtime/yt-dlp-lan-evidence.sqlite3, separate
from the coordinator lease/cooldown SQLite store. An optional
--evidence-db selects another safe local .sqlite3/.db file; it never
writes over a LAN secret, profile, required marker or lease state.
Saved values are restricted to a timestamp, fixed diagnostic/trial
status codes, observation scope and a bounded set of known check names.
Consecutive observations retain at most 90 snapshots.

To additionally exercise the **real live lease and 20-second gap**
from PR #197, specify BOTH --record and --include-pacing-trial.
This second flag is never implied by routine evidence capture.
The extra trial only runs after matching paired onboarding evidence;
otherwise a negative diagnosis is saved without taking a lease.
Do not include the trial while source collection is in progress.
An interrupted trial may leave a bounded lease until expiry.

Read the history offline, without SSH, YouTube, or an active broker:

    uv run holoclip examples-ytdlp-lan-evidence-history

Reports identify repeated identical check failures over the latest
three **recorded observations**, a trailing incomplete-observation
count, and the latest change compared to the previous observation.
Comparisons across different observation scopes (for example,
PC-A-only vs both PCs) are deliberately INCOMPARABLE, not
improvements. These are per-run observations, **not continuous
uptime measurements** or calendar-time guarantees. Captures do not
automatically install Task Scheduler monitoring or rerun themselves.

An existing corrupted or untrusted history is rejected; the tool
never silently erases the file or overwrites the central broker DB.
All stored reports and summaries preserve ready=false,
production_ready=false and real_two_host_pacing_qualified=false.
Even a successful signed observation and no-media pacing trial do
not independently attest physical PC identity, Windows Firewall
effective-policy filtering, YouTube behavior or clip quality.
CI uses mock SSH and disposable local files only.

#### Explicit no-media two-PC shared-pacing live trial

After configuring both PCs and passing the existing strict paired
onboarding check, PC-A can verify that **PC-B cannot acquire a shared
coordinator lease while PC-A holds it, or during the normal gap
immediately after release**. The trial uses pre-pinned, public-key-only
SSH to run the fixed PC-B witness commands. No YouTube, yt-dlp,
media, URLs, Task Scheduler or firewall changes are involved.

Start with a local **preview**, which only validates the pinned SSH
client and known_hosts file and does not send a coordinator request:

    uv run holoclip examples-ytdlp-lan-pacing-trial --host 192.168.1.21 --user pc_b_account --known-hosts "D:\AI\holoclip\data\runtime\pc-b-known-hosts" --remote-repo "D:\AI\holoclip"

After manually reviewing the PC-B SSH key fingerprint out of band,
configuring both client profiles and observing PC-A as available,
run the same command with the explicit --apply flag **on PC-A**.

    uv run holoclip examples-ytdlp-lan-pacing-trial --host 192.168.1.21 --user pc_b_account --known-hosts "D:\AI\holoclip\data\runtime\pc-b-known-hosts" --remote-repo "D:\AI\holoclip" --apply

Nondefault ports can be set with --ssh-port and --coordinator-port.
An alternate Task Scheduler name uses --task-name. The explicit
trial first requires successful strict paired onboarding and an
AVAILABLE signed shared coordinator status. It then reserves one
**real, short-lived production coordinator lease**, runs PC-B's
signed status + denied-reservation witness over pinned SSH while
that lease is held, confirms release, and runs PC-B's second
witness during the normal **20-second shared gap**. Witnesses must
report the same persistent coordinator instance identity and fixed
negative lease codes. Incomplete, delayed, replayed, mismatched,
unavailable or corrupted observations are **not** successful.

**Operational side effect:** Even though no media is accessed, the
trial consumes a real coordinator lease and imposes its regular
20-second gate gap. Run it only when the coordinator is otherwise
idle, not during planned media acquisition. If the client crashes
or release confirmation fails, the gate may remain held until
its bounded lease expiry; the command never deletes/reset a
production SQLite gate or overrides a cooldown. Running it
does not bypass existing yt-dlp pacing or YouTube access restrictions.

A positive LAN_PACING_CROSS_SSH_LEASE_GAP_OBSERVED reports only
this one authenticated, bounded SSH witness trial. It does not
establish independent physical PC identity, verified fingerprint
provenance, effective Windows Firewall policy, continuous uptime,
future no-BAN guarantees, real YouTube access, rendered clip quality
or production eligibility. It leaves two_host_verified=false,
real_two_host_pacing_qualified=false and ready=false. Linux and
Windows CI use an **isolated loopback broker and fake SSH only**;
they do not perform a real two-PC or media test.

#### Read-only two-PC deployment diagnosis with ordered next actions

Run on PC-A after independently pinning PC-B's SSH host key and provisioning
public-key-only SSH access. This command never applies configuration changes:

    uv run holoclip examples-ytdlp-lan-deployment-diagnose --host 192.168.1.21 --user pc_b_account --known-hosts "D:\AI\holoclip\data\runtime\pc-b-known-hosts" --remote-repo "D:\AI\holoclip"

Replace example host settings. Optional flags: --ssh-port, --coordinator-port
and --task-name. The diagnostic reuses the strict pre-pinned SSH onboarding
inspection, signed coordinator receipts and fixed-code local/remote results.
It orders observed deficits by deployment dependency: PC-A credential and
ACL, exact-peer firewall, task/listener and signed status; PC-B credential,
ACL and signed status; then paired observation. Each plan entry contains
one static recommended CLI command name, never a credential or shell
fragment. Nothing is automatically repaired.

If PC-B SSH is unavailable, its report malformed or its signature untrusted,
the command still returns a fresh, independently sanitized local PC-A
inspection where possible. It emits pc_b=null and a fixed failure code;
it does not guess PC-B state or use remote-supplied actions. A blocked
or incomplete diagnosis returns a nonzero exit code.

Even when both sets of codes and signed pair reports match, this is only
consistent setup observation. The tool does not prove actual hardware
identity, out-of-band SSH fingerprint authenticity, effective firewall
packet filtering, simultaneous two-PC pacing or editorial/media quality.
The response separately lists required real-host/manual qualification
gates; ready=false, two_host_verified=false and
firewall_effective_policy_verified=false always. CI uses mocked SSH
and never contacts the owner's PCs or YouTube.

#### One-command read-only PC-A/PC-B setup inspection

The paired setup doctor combines both PCs' local prerequisites with the
signed LAN coordinator comparison using **one command from PC-A**, after
PC-B's OpenSSH public-key access and independently verified host pin have
already been configured. It **does not** auto-enable a firewall, create
credentials, trust unknown host keys, install sshd or start services.

```powershell
uv run holoclip examples-ytdlp-lan-onboard-check --host 192.168.1.21 --user pc_b_account --known-hosts "D:\AI\holoclip\data\runtime\pc-b-known-hosts" --remote-repo "D:\AI\holoclip"
```

The PC-B directory is configurable, including spaces; nonstandard
ports can be set with `--ssh-port` or
`--coordinator-port`. The PC-A task can be set with
`--task-name`. Replace the example values with private network
values; the command explicitly rejects loopback for PC-B. SSH is
noninteractive, pinned-key-only, with forwarding, proxies and session
sharing disabled. On PC-B, the command executes the built-in
`examples-ytdlp-lan-onboard-node` through an encoded PowerShell
command and uses offline/no-sync uv.

The combined JSON report shows fixed, safe code-level observations of
both PCs' credential/NTFS ACL, signed remote coordinator status, Windows
Task Scheduler status, listener and firewall rule scope. Missing required
items are combined into a list of actionable remediations. The output
also validates fresh signed observations from both PCs when available.
PC-B can report an incomplete setup without interrupting the collection;
SSH failures, malformed/tampered responses and untrusted status codes
fail closed. No secrets, raw SSH stderr, raw IP-derived tag, arbitrary
remote messages or credential file bytes are copied to the output.

`LAN_ONBOARD_OBSERVATIONS_COMPLETE` **does not** establish
production readiness. Both reporters share a signing key, so physical
hardware identities cannot be independently attested; NAT may make
two PCs' TCP-source tags identical. Observing scoped firewall rules is
not effective firewall policy validation. All reports retain
`two_host_verified=false`, `ready=false`, no downloaded
media and no configuration changes. The real two-PC setup and eventual
media/editorial performance still require qualification. GitHub tests
exercise loopback and mock SSH, **never actual PC-B or YouTube**.

#### One-command cross-PC LAN status collection over pinned SSH

After **both Windows PCs** already have working Holoclip LAN coordinator
client configurations and PC-B has a trusted OpenSSH server, a single
command on PC-A can run PC-B's **read-only** status probe remotely and
compare it to a fresh PC-A status probe **without report files or secret
transfer**:

```powershell
uv run holoclip examples-ytdlp-lan-pair-collect --host 192.168.1.21 --user pc_b_account --known-hosts "D:\AI\holoclip\data\runtime\pc-b-known-hosts" --remote-repo "D:\AI\holoclip"
```

Replace the IP, SSH account, PC-B known_hosts file and PC-B Holoclip
directory with your own settings. Nondefault SSH ports use
`--ssh-port`. **PC-B's SSH host-key fingerprint must be
verified independently** and pinned in the specified file. The command
does not provision an SSH server, keys, firewall access, PC-B environment
or SSH trust. It refuses missing known_hosts, external IPs, untrusted SSH,
interactive password authentication, SSH agent forwarding, proxies and
jump hosts. The remote shell command is encoded rather than concatenated
with untrusted input, and the PC-B working directory is validated and
configurable, including spaces.

The collector uses a noninteractive SSH connection to PC-B and invokes
`uv run --no-sync holoclip examples-ytdlp-lan-pair-observe --role pc-b`
via remote Windows PowerShell, while obtaining PC-A's signed observation
locally. It receives one bounded JSON receipt directly over SSH and
compares both reports in memory, never saving/transporting the shared
credential or persisting report files. Remote and local status calls
never request a pacing lease or contact YouTube.

The report explicitly shows only authenticated observation consistency.
Two registered clients knowing the shared credential can still produce
forged evidence; NAT and other network configurations can collapse
TCP-peer tags. SSH host-key pinning verifies the identity *assigned
during the manual out-of-band enrollment*, not the independent identity
of physical hardware. If the coordinator/SSH connection fails or the
response is invalid, the command fails closed. Even when pair observations
match, it **never** declares two physical machines independently verified
or production-ready: `two_host_verified=false` and
`ready=false`. This is not a real media-pacing qualification.

The original `examples-ytdlp-lan-pair-export` and
`examples-ytdlp-lan-pair-verify` commands remain available
for environments without remotely configured SSH.

#### Signed cross-PC LAN coordinator pairing observations (no media)

The optional pair-export command queries only coordinator's HMAC-authenticated
status; it never reserves a lease, downloads media, runs yt-dlp, changes the
firewall or modifies the pacing gate. A random durable coordinator instance
ID survives restarts in the coordinator SQLite database, and signed status
also carries an opaque keyed tag of the observed TCP source address.

Run one command on each **already enrolled** Windows PC:

```powershell
# On PC-A
uv run holoclip examples-ytdlp-lan-pair-export --role pc-a --output "data/runtime/pair-pc-a.json"
# On PC-B
uv run holoclip examples-ytdlp-lan-pair-export --role pc-b --output "data/runtime/pair-pc-b.json"
```

Transfer the two generated report files (not the secret) to one trusted
machine by USB or an independently authenticated file transfer, then:

```powershell
uv run holoclip examples-ytdlp-lan-pair-verify --pc-a "data/runtime/pair-pc-a.json" --pc-b "data/runtime/pair-pc-b.json"
```

Generate fresh reports for each check: export never overwrites an existing
report. Reports are purpose-separated HMAC signed under the common LAN
credential, limited to 15 minutes of age (90-second future clock skew) and
4096 bytes. Unknown versions, tampering, duplicate filenames/nonces,
different coordinator SQLite identities and identical observed TCP peer
tags do not pass. On coordinator failure, new export fails closed and the
existing provider cannot bypass its coordinator to start yt-dlp.

**Limits:** An authorized holder of the *shared* secret can forge reports,
and a reported role is not a verifiable machine identity. NAT/proxies may
make physical devices appear as the same peer. Matching persistent SQLite
state is not proof of one running server process. A match proves only
consistency of submitted authenticated observations with one durable state
and distinct TCP peers, not independent physical-PC attestation or global
pacing performance. The offline verifier does not contact the coordinator;
stored reports do not prove it is online now. Always retain
two_host_verified=false and ready=false. No GitHub-hosted YouTube access
or real two-PC tests occur.

#### One-command PC-A / PC-B LAN setup preflight (read-only)

Run this after the earlier secret import/client enrollment. Every diagnostic
is read-only: it does not install OpenSSH, register or start tasks, open a
firewall, change ACLs, reserve a coordinator lease, run yt-dlp, or contact
YouTube. It prints fixed status codes and repair guidance, never paths,
host keys or credential bytes.

On PC-A (replace PC-B's private IPv4):

```powershell
uv run holoclip examples-ytdlp-lan-setup-doctor --role pc-a --peer-ip 192.168.1.21
```

On PC-B, after client configuration and optionally SSH host pinning:

```powershell
uv run holoclip examples-ytdlp-lan-setup-doctor --role pc-b --ssh-host 192.168.1.20 --known-hosts "D:\AI\holoclip\data\runtime\pc-a-known-hosts"
```

Only pass `--ssh-host` / `--known-hosts` if SFTP onboarding is used. The
presence check uses local `ssh-keygen -F` (including hashed known_hosts);
it does **not** establish host authenticity or create/trust keys. Verify the
fingerprint independently first. USB/manual import does not require SSH.

For custom deployments, use `--secret-file`, `--port`, `--task-name`, and
`--ssh-port` as needed. PC-A checks a locally valid secret, NTFS ACL,
authenticated signed coordinator status, coordinator logon-task state,
private listening port, inbound TCP firewall rule restricted to the
specified peer, and optionally sshd service presence. PC-B checks
client config, signed coordinator status, credential/ACL and optional
pinned SSH host entry. Each failed check contains suggested next steps.

The firewall observation is only a **matching Windows ActiveStore allow-rule**
inspection. A broad matching allow rule is flagged, but this is not
exhaustive GPO, adapter, router, or effective security-policy verification.
Task state and listening port alone cannot prove the process belongs to
Holoclip. Both machines must each run diagnostics on their own system.
Even `LOCAL_SETUP_OBSERVATIONS_COMPLETE` is local evidence only;
`two_host_verified=false`, `firewall_effective_policy_verified=false`,
and production `ready=false` are always retained.
#### Optional PC-A Windows autostart and crash recovery

After creating the shared secret, install the **PC-A coordinator only** as
a Windows scheduled task. From PC-A's Holoclip directory run `uv sync`
once to install the local environment. Then in Windows PowerShell:

```powershell
.\scripts\install-ytdlp-lan-task.ps1 -BindIp 192.168.1.20
```

Replace the example address with PC-A's **actual static private LAN IPv4**.
The installer checks the secret format, environment, bind address and absolute
paths. It only registers a **limited-privilege current-user logon task**;
it does NOT start the server, access media, or open Windows Firewall.
Restrict inbound TCP 8878 on PC-A to **PC-B's trusted LAN address only**
before starting. Do not port-forward or use an untrusted network.

```powershell
Start-ScheduledTask -TaskName 'Holoclip yt-dlp LAN Coordinator'
Get-ScheduledTask -TaskName 'Holoclip yt-dlp LAN Coordinator' | Get-ScheduledTaskInfo
# To stop the coordinator:
Stop-ScheduledTask -TaskName 'Holoclip yt-dlp LAN Coordinator'
# To remove the startup registration:
Unregister-ScheduledTask -TaskName 'Holoclip yt-dlp LAN Coordinator' -Confirm:$false
```

The task starts **after the same Windows user logs on**, not before login.
The supervised process uses `uv run --offline --no-sync` so Task Scheduler
never updates/downloads Python packages. Unexpected server exits are retried
with bounded exponential backoff (at most five minutes); Task Scheduler also
provides three one-minute restarts if the supervisor itself exits unexpectedly.
A local mutex prevents duplicate supervisors and the TCP bind prevents
duplicate servers. The previous SQLite lease and cooldown are never wiped
on restart. Configured clients continue to **fail closed** during downtime.

Windows sleep, logoff, missing local environments, credential errors, network
changes and long outages can still prevent automatic service availability.
Both client PCs must have their own coordinator environment configured; this
installer does not enroll them or propagate secrets. Check
`examples-ytdlp-lan-status` separately on both PCs. To change the bind IP,
port or credential/state locations, re-register this same task with new
arguments; do not run another coordinator concurrently.

Windows CI only mocks scheduler cmdlets and a failing local executable.
It never registers a real task, downloads media, or verifies actual
two-PC deployment. These features do not establish `ready=true`.

This is an **opt-in** capability: if the coordinator variables are
absent, the pre-existing local-only pacing remains in effect and
does **not** guarantee shared-IP coordination. If just one of the
two variables is configured, collection fails closed instead of
bypassing the coordinator. Even in coordinated mode, YouTube may
rate-limit or restrict access for other reasons; neither the
coordination status command nor a permit proves actual media access
or `ready=true`. This feature has only been verified with
loopback/offline tests, **not a real two-Windows-PC LAN deployment**.

### Safe pause and unattended resume of native source collection

The Windows native collector now checks the local yt-dlp gate *before*
discovering clips, inspecting original video sections or touching the
learning corpus. A recorded YouTube 429/bot challenge or another active
yt-dlp process produces `DEFERRED_LOCAL_YTDLP_ACCESS`; the collected
sample/stream counts are **null (unmeasured)**, **not zero**. The
timestamp-bounded local worker report is saved with
`collection_started=false`, the fixed `local_access_state` code, and
the earliest recorded `next_attempt_not_before_utc`. This is never
human-original match evidence or an editor-negative label.

The scheduled task **naturally retries on its next already-configured
recurrence after the local gate expires**. No additional task, permanent
background poller, retry storm or manual video URL is needed. By
default the opt-in native Windows task runs daily; a six-hour cooldown
ending after today's task **does not launch a same-day retry**. The next
existing task invocation resumes the bounded collector if local storage
is healthy, and the yt-dlp gate **still checks the live lease/cooldown
atomically** before every network process. The status-only CLI never
launches the collector.

If the pacing SQLite DB is corrupt/unreadable, the worker does not
delete it or silently disable throttling. It saves
`BLOCKED_LOCAL_YTDLP_PACING_STATE`, starts no video collection, and
returns a **nonzero exit** to Windows Task Scheduler. The compact doctor
displays this as a repairable blocker only if the blocked audit belongs
to the latest launcher; stale audits cannot hide unrelated task failures.
Intentional paused invocations do **not** count as zero-new-video runs
or consecutive source-pairing failures in the 90-run operational history.
All audit flags retain `ready=false`.

**Multi-PC limitation:** this gate coordinates processes using the
*same local SQLite file*. Two PCs behind the same home/router IP with
separate files **do not share a rate budget**. Do not assume automatic
cross-PC safety or place the SQLite database on an unverified SMB/NAS
share: dependable distributed ownership/clock coordination needs a
separately designed and tested mechanism. Schedule only one Holoclip
public media collector per shared outward IP until that is implemented.

### Inspect local yt-dlp cooldown without any network request

`uv run holoclip examples-ytdlp-access-status` displays a compact,
**read-only** JSON snapshot of the shared native yt-dlp gate. It reports a
fixed `pacing_status` (active six-hour site cooldown, another local
process holding the gate, waiting for the 20-second minimum gap, gate
available, missing history, or unreadable state), a sanitized
`cooldown_reason` such as `HTTP_RATE_LIMIT` or
`YOUTUBE_BOT_OR_INTERACTIVE_CHALLENGE`, and
`next_attempt_not_before_utc` with `seconds_until_not_before`. A custom
local SQLite file can be inspected via `--pacing-path`. The existing
`examples-collector-health` output now includes the same information as
`yt_dlp_access`.

**A displayed time is not a promise that YouTube access will succeed**:
it is the earliest retry estimate from the current local gate, and a
separate process may take the gate in the meantime. When a different
Holoclip invocation is in flight, the reported lease deadline is a
conservative upper bound that may be released earlier. When the
database is missing, the answer is explicitly unknown; no file is
created by a status check. Existing pre-upgrade cooldown records remain
valid and retain the fixed `UNKNOWN_HISTORICAL_COOLDOWN_REASON` category
until the writer migrates them on its next authorized access. Corrupted
or unsafe state is reported as blocked, not silently treated as available.

This command does not launch yt-dlp, access YouTube, change the delay,
lift a cooldown, download anything, or modify a task. A common query:

```powershell
uv run holoclip examples-ytdlp-access-status
```

### Conservative native YouTube request pacing


YouTube rate limiting is **not** a fixed universally safe requests-per-hour
threshold. Holoclip now applies conservative request pacing to public
yt-dlp calls on the native collector PC: `--sleep-requests 2` inside
each extraction, a randomized **10–20 second wait before downloading**,
`--concurrent-fragments 1`, and small finite request/fragment retry budgets.
This uses yt-dlp's documented sleep switches, not CAPTCHA, proxy, account
or identity bypass.

Managed yt-dlp providers also share a **durable OS-process gate** at
`data/runtime/yt-dlp-access.sqlite3`. It enforces at least **20 seconds
between successive separate yt-dlp invocations**, blocks simultaneous
Holoclip yt-dlp processes across different terminals/tasks, and pauses
future local requests for **at least six hours** when yt-dlp reports an
HTTP 429, bot/interactivity challenge, or the common YouTube
`This content isn't available, try again later` rate-limit message.
A blocked request is reported as a local deferral rather than retried,
and yt-dlp runtime refresh is never used to immediately retry a
challenged site. A broken pacing database fails closed rather than
allowing unlimited traffic. Process crash reservations have a finite
timeout tied to the configured downloader deadline.

**These are conservative operational defaults, not a guarantee against
temporary restrictions or a YouTube account ban.** The system uses
public videos only; credentials/cookies are not automatically imported.
If YouTube imposes an interactive restriction, Holoclip stops rather
than trying to defeat it. This also applies to metadata discovery,
not just finished video downloads, and does not require the user to
provide clip URLs or operate an external paid service. Manual CLI
commands that use the standard managed provider share the same local
gate. Unmanaged explicit-executable library integrations must opt into
the gate with `pacing_path`; callers not using the provider cannot
be coordinated. GitHub Actions remains fully offline for media.

### Windows unattended source-pattern collection

An opt-in Windows Task Scheduler installation is available for machines running
Holoclip locally. After **one** installation, it launches the same bounded CPU-only
public-video collector once a day, from the actual repository working directory,
with no clip URLs or labels to prepare. It does **not** run automatically just
because the code was merged:

```powershell
# Run once from the cloned repository on the Windows collector PC:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-source-pattern-task.ps1
# Optional: customize the local daily start time, e.g. 04:30:
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-source-pattern-task.ps1 -At "04:30"
```

#### Repeated Windows acquisition and pairing stalls (local only)

`examples-collector-health` now derives `native_launcher_history.operational_stall`
from **consecutive, actually recorded native invocations**. It surfaces
separate diagnostic signals when:

- **3 consecutive Windows launch failures** occur (including prelaunch uv or
  Task Scheduler toolchain problems);
- **5 consecutive successful, fresh-audit-correlated runs yield zero new
  constructed-pattern samples**;
- **5 consecutive fresh runs measure patterns but add zero new candidate
  source-stream IDs**, indicating stagnant diversity rather than outright
  download failure;
- **3 consecutive fresh native runs inspect at least one finished/source
  pairing with saved original sections but report no bounded visual
  corroboration**.

The per-run pairing, original-section and visual-corroboration counters
are recorded from the Python collector report **only if that report
finished within the same Windows launch** and every stage has a strict
non-gold schema. Historic runs saved before this feature remain valid,
but their missing stage metrics are **unknown**, never reconstructed or
counted as failed comparisons. Missing audits, tool failures or a run
without acquired source sections break the relevant progress/visual
streak rather than producing invented negatives.

`operational_stall` returns fixed signal/action codes, exact qualifying
run counts and explicit thresholds. It is **not** a prediction about
calendar days: task scheduling cadence is independently configurable.
A lack of visual corroboration from bounded samples cannot establish a
false match or a model recall estimate; public source identity, editor
gold, and `ready` all remain unverified. Signals never start downloads,
modify scheduled tasks or automatically bypass rate limits.

#### Automatic bounded Windows launch history (offline)

The native `run-source-pattern-worker.ps1` now automatically retains the
most recent **90 scheduled/manual launch outcomes** in
`data/examples/source-pattern-launcher-history.json`, next to its existing
`source-pattern-launcher-last-run.json`. When the optional Windows JSON
settings changes `launcher_audit_path`, the history lives in that same
configured directory. **No new setup, background service, cloud storage or
GitHub Actions downloader is needed.**

Even a missing uv executable, an invalid local configuration, or a failed
collector process results in a history entry, provided the local history
disk is writable. Each entry records timestamp, status, exit code and safe
exception class. Per-run counts of new constructed source-pattern samples
and new candidate stream IDs are saved **only when a bounded, non-gold Python
collector report completed inside that exact Windows launch**. An old report
is never copied forward as new success; missing/mismatched reports retain
null counts. The launcher uses an OS-exclusive lock and atomic file
replacement to avoid partial writes, and rejects corrupt/unsafe existing
history instead of silently overwriting it.

The read-only `examples-collector-health` response now includes
`native_launcher_history`, with the latest and previous runs, **last
seven** sanitized outcomes, consecutive failure count, status frequencies,
and changes between **comparable per-run increments**. Null differences
mean insufficient evidence and must not be treated as zero growth.
A full 90-run ring explicitly reports the retention cap; older runs
are not part of subsequent trends. History is local-only and contains
no clip URL, media file, credentials, raw error message or human gold.

#### One-command offline Windows collection health

To see a **compact** operational diagnosis after a native scheduled run,
or before installing a task, run this from the Holoclip repository:

```powershell
uv run --extra media holoclip examples-collector-health
```

The command automatically reads the same optional
`data/examples/source-pattern-local-settings.json` as the Windows launcher.
For a custom file pinned with `-ConfigPath` during task installation, pass
`--config-path "D:\\Holoclip Data\\local-source-settings.json"`. You can
also use `--no-check-tools` to skip checks for uv, FFmpeg and JavaScript.

The output is **one small JSON summary** with `primary_blocker` and
`next_action_code`, plus the latest clip/section attempt counts and
independent on-disk cached-video SHA checks. It distinguishes a failed
Task Scheduler launch, missing tools, YouTube rate/bot restrictions,
missing finished/source video records, damaged or partial media cache,
absent visual corroboration, absent constructed controls, and missing
independent human/editor gold. The legacy
`examples-collector-doctor` still provides the complete detailed report,
now also including `local_collection_health`.

This is read-only: no network, downloads, runtime models, corpus edits,
cookies, clip titles or video URLs are included in the compact output.
A saved source-window **record** and a verified **local-byte** receipt
are not proof of public footage identity or correct original-to-finished
editing; `ready=false` remains enforced even if all operational checks
were measured. For safety, missing or incomplete measurements remain
unknown, and a partial bounded cache audit cannot satisfy full coverage.

#### Optional Windows local settings (no code edits)

The native runner now reads
`data/examples/source-pattern-local-settings.json` **only if it exists**.
No file is required: with no settings file, all previous CLI defaults
remain unchanged. To customize local folders or bounded collection work,
copy the safe, versioned
[`config/source-pattern.local.example.json`](config/source-pattern.local.example.json)
template to the above path and edit it locally:

```powershell
Copy-Item .\config\source-pattern.local.example.json .\data\examples\source-pattern-local-settings.json
# The next scheduled run automatically uses the new settings.
# No task reinstallation or manually supplied clip URLs required.
```

The JSON accepts `store`, `state`, `output`, `policy_file`,
`report_path`, `launcher_audit_path` (absolute or repository-relative
paths), `cycles` (1–20), `max_clips` (1–2),
`max_candidates` (1–2), `scan_limit` (1–100),
`target_distinct_streams` (1–50) and boolean `discover`.
Paths containing spaces are passed as separate native arguments, not
interpolated into a command string. Configurations larger than 32 KiB,
invalid types, out-of-range budgets, unsupported keys, symlinks and invalid
JSON fail at the **Windows launcher preflight**, before yt-dlp is invoked.
The existing launcher audit records `FAILED_PRELAUNCH` rather than
claiming evidence or readiness. Changing `state` or other paths starts
using the newly configured corpus location; it does **not** migrate prior
stored evidence or merge disjoint corpora automatically. Keep `state`,
`store`, `output` and reports on appropriate persistent disks.

A custom config filename can also be pinned during one-time installation:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install-source-pattern-task.ps1 -ConfigPath "D:\Holoclip Data\local-source-settings.json"
```

For the default settings path, the scheduled task requires **no**
reinstallation when the JSON is edited. GitHub CI only tests mocked
Windows launchers and never downloads YouTube media.

During one-time installation, the script checks `uv`, FFmpeg, FFprobe,
and **Deno >=2.3 or Node >=22**, rejecting missing/unsupported JavaScript
runtimes *before* registering any repeating task. It pins absolute executable
paths for the chosen tools in the task action and adds their directories to
the scheduled subprocess PATH; a different interactive vs Task Scheduler
PATH cannot by itself hide these selected executables. If a pinned tool is
moved or removed after installation, the launcher fails closed and records
`FAILED_PRELAUNCH` rather than fabricating new video evidence. The offline
doctor remains available to check actual runtime readiness. Re-run the
installer after moving/updating tools or changing their paths.

The scheduled action runs `scripts/run-source-pattern-worker.ps1`, invoking
`uv run --extra media holoclip examples-collect-source-patterns --cycles 3`.
Install is **opt-in**: it changes the local user's scheduled tasks, not GitHub
Actions or a cloud service. The PC must be on, the installing user logged on,
and public YouTube videos accessible. Requires Windows Task Scheduler, `uv`,
FFmpeg/FFprobe, Deno >=2.3 or Node >=22; yt-dlp itself is managed by uv.
The installer does not start a live collection by itself.

A concurrent collector invocation exits with a nonzero code (rather than
reporting a skipped scheduled run as success).

The collector now holds an OS-released cross-process advisory lock on its state
file directory: the collector, `examples-grow-corpus` and
`examples-verify-source-visual` now share a single nonblocking lock on their
state-file directory. Overlapping CLI operations exit nonzero before accessing
public video, so the unattended collector cannot race these manual commands.
A crashed process releases its OS lock. Other direct Python callers that write
the same corpus state must explicitly use the shared lock. A compact
last-run audit, including failures, is saved at
`data/examples/source-pattern-last-run.json`. It remains unreviewed and is
never proof of cut precision, source identity or production readiness. Standard
output and errors are logged under `data/logs/source-pattern-*.log`, with the
most recent 30 retained. In Task Scheduler, disable the task to stop automatic
future runs, or unregister it:

```powershell
Unregister-ScheduledTask -TaskName "Holoclip Source Patterns" -Confirm:$false
```

CI now includes a **Windows runner** that checks actual Windows advisory-lock
behavior and Windows PowerShell 5.1 launcher execution. It invokes only mock
`uv`/`ffmpeg`/`ffprobe` executables in an isolated temporary directory,
and tests prelaunch failure, nonzero collector process, zero-exit status,
pinned uv/FFmpeg/FFprobe/JavaScript runtime paths (including spaces), and
invalid pinned paths.
including durable launcher audit replacement. A separate Windows PowerShell 5.1 test invokes the installer under
**mocked ScheduledTasks cmdlets** to verify preflight, paths, principal,
daily schedule and no-overlap policy without registering a task.
GitHub CI neither downloads
a real Hololive video nor registers a scheduled task, so actual Task
Scheduler startup and real public-media acquisition still require a local run.

The installer configures a **single** daily local task at 03:00 by default,
runs only in the current user's interactive session, will not force-stop a
running instance at a fixed deadline, and skips overlapping starts. It cannot
collect media if the PC remains off or unavailable. No hosted task or recurring
cloud billing is created.

## Unattended data and evaluation policy

For measured clip-to-original correspondence (no cloud AI inference),
the optional `media` extra provides NumPy alongside PySceneDetect and OpenCV:

```powershell
uv run --extra media holoclip examples-grow-corpus --discover-only
uv run --extra media holoclip examples-prepare-holdout --max-clips 3
uv run --extra media holoclip examples-align-holdout --max-clips 3
```

The next optional holdout diagnostic can narrow acoustic edit transitions
to **conservative candidate time brackets**, not exact cut frames:

```powershell
uv run --extra media holoclip examples-refine-cut-candidates
```

The command uses earlier source-audio and measured source/finished video
evidence, without additional downloads or paid AI inference. Acoustic
silence, ambiguous matches, visual-only edits and unavailable absolute
source timestamps remain explicit unknowns.

### Real-media reserved holdout validation (one command)

Instead of manually invoking every 9-stage diagnostic, run the existing
reserved public human-made clip/source evidence pipeline in one bounded,
checkpointed local command (Windows `uv`, FFmpeg, local CPU; no paid AI):

```powershell
uv run --extra media holoclip examples-validate-holdout-e2e --preflight-only
uv run --extra media holoclip examples-validate-holdout-e2e --max-clips 3
# Autonomous bounded multi-batch validation, for reserved real clip/source evidence:
uv run --extra media holoclip examples-validate-holdout-e2e --all-batches --max-clips 3 --max-batches 8
# After interruption or reaching a finite batch budget, replay all previously
# successful reserved-video batches with full nine-stage validation, then
# continue up to the NEW total max-batches budget:
uv run --extra media holoclip examples-validate-holdout-e2e --all-batches --resume-checkpoint --max-clips 3 --max-batches 12
# Optional: examine other reserved clips even when one original audio cannot
# be uniquely matched. Other failures (policy, identity, media, exceptions)
# ALWAYS stop. Bounded to one clip per batch, up to 12 clips per invocation.
uv run --extra media holoclip examples-validate-holdout-e2e --all-batches --continue-on-insufficient-audio --max-batches 12
```

The first invocation checks local holdout reservations and learning-stream
conflicts **without downloading anything**. The second sequentially runs
finished-video preparation, source audio alignment, bounded source video
measurement, visual geometry, acoustic cut-bracket observations, original
section audio clock calibration, source↔finished frame links, candidate
two-sided cut evidence, and partial time-map candidates. It automatically
reuses each stage's verified cached media, but cannot continue through an
upstream stage without successful measurements. Real public media downloads
may take significant time; the input reservations must already exist.

If the reservation list contains more clips than `--max-clips`, each
invocation automatically processes one bounded batch, prioritizing clips
without a completed final-stage record. Re-run the same command to advance
through later batches; original corpus reservations and learning history
are not edited. The projected batch state is kept locally at
`data/provider-benchmark/holdout/holdout-e2e-batch-state.json`.
`--preflight-only` writes a **separate** `holdout-e2e-preflight.json`
without overwriting the latest real run audit. Overlapping runs fail
closed behind a local `.holdout-e2e.lock`; if an unclean crash leaves
that lock, inspect the process before removing it. If the original
reservation state changes during processing, the batch stops instead of
claiming that the old source assignment was validated.

For minimal intervention, use `--all-batches` to process distinct
reserved clips sequentially in one invocation, at most `--max-batches`
(default 8; maximum 12) times the `--max-clips` per-batch budget.
If a *single* clip lacks independently unique original audio evidence,
the optional `--continue-on-insufficient-audio` flag can defer **only**
`INSUFFICIENT_UNIQUE_ANCHORS` or `INSUFFICIENT_AUDIO` and examine another
reserved clip without any automatic retry. For truthful clip attribution,
this mode forces **one clip per batch**, regardless of `--max-clips`,
and the finite `--max-batches` budget still applies (maximum 12).
Source/clip rights restrictions, train/holdout split collision, original
identity mismatches, corrupt files, missing reports, unexpected exceptions,
and other failures remain immediate hard stops. Deferred clips remain
**unverified**, appear by ID in both run and offline diagnosis audits, and
never count as successful nine-stage measurements. The final status is
`UNREVIEWED_INSUFFICIENT_AUDIO_RECORDED`, not a complete/ready state,
if the bounded invocation examined every reservation but some audio
correspondence was insufficient. For ordinary use, leave this opt-in
disabled to retain the original fail-on-first-mismatch behavior.

If Windows restarts or a finite `--max-batches` budget is reached,
`--resume-checkpoint` can recover the last completed-batch selection
order from the prior `holdout-e2e-all-batches.json`. Crucially, it
**replays all nine real stage checks for every historical completed
batch**, including media SHA/policy/source guardrails, before attempting
a new reservation. Successful cached expensive work may be reused by
individual stages only after their own integrity checks. This is
**not** blind trust of the JSON audit or a shortcut to reviewer gold.

Checkpoint replay refuses changed reservation-state SHA, changed ordered
rights/policy rules, mismatched stage schema/execution mode, forged stage
tallies, duplicate clip IDs, or previous failed/deferred batches. Existing
audits from before this feature lack the policy digest and therefore must
be rerun normally; unsafe resume does **not** overwrite the saved audit
and writes `holdout-e2e-resume-refusal.json` instead. During replay of
prior successful batches, the full original checkpoint stays intact so
an unexpected second interruption does not erase it. The maximum of
12 batches applies to the *total revalidated + newly executed* batch
count; increase `--max-batches` only within that fixed upper bound.
When the checkpoint has failed/deferred clips, use normal diagnosis and
a fresh strict trial rather than skipping over them.

The run holds one exclusive lock across all batches, fails closed
at the first blocked/quarantined/insufficient step, and never retries a
failed batch indefinitely. It writes a **separate cumulative**
`data/provider-benchmark/holdout/holdout-e2e-all-batches.json`
checkpoint after every successful batch, recording all selected clip IDs,
remaining reservations, each batch's stage statuses and candidate-only
observed totals. If the configured invocation budget cannot cover all
reservations, the status is `BOUNDED_BATCH_LIMIT_REACHED` (not success).
Use `--preflight-only` separately, before full media execution.
This is an evidence/diagnostic trial, not a production qualification.

The `--all-batches` validation command now **automatically** runs offline
triage after success or a blocked batch, printing the first blocker and
next-action code without another media download or manual command.

After the real-media trial, get a source-state-checked offline failure diagnosis
without re-downloading or re-encoding any files:

```powershell
uv run --extra media holoclip examples-diagnose-holdout-e2e
```

This writes `data/provider-benchmark/holdout/holdout-e2e-diagnosis.json`
with the first failed stage, fixed next-action code, stage status totals,
original reserved clip IDs not yet attempted, and whether the saved audit
still matches the exact reservation-state SHA-256. When invoked from the CLI, offline diagnosis also checks the saved
rights-policy SHA-256 against the current `--policy-file` (default
`data/policy/clip_rules.jsonl`). A changed policy or legacy audit without
this fingerprint is flagged rather than presented as a current successful
measurement. The diagnostic prefers
the cumulative multi-batch report, never double-counts the latest
individual batch, and refuses missing, stale, tampered or older unauthenticated
audit schema. `SIMULATED_TRIAL_ONLY` explicitly differentiates fixture
tests from actual media runs. Diagnosis **only reads saved reports and
metadata**; it does not re-verify media files or imply reviewer/editor gold.
A diagnostic result is never production qualification.

The local `data/provider-benchmark/holdout/holdout-e2e-audit.json` records
each stage's *actual* per-clip status, observed candidate coverage, frame
links, candidate edit discontinuities and blockers. Precision/recall,
native timestamp error and event-independent quality remain null until
there is independently reviewed real-media gold. Successful stage
execution is **not** production acceptance. The Git repository includes
example metadata but does **not** include the user's locally downloaded
real source/video files or completed private holdout checkpoints.

Downstream bounded-original-video acquisition now additionally validates
the exact current acoustic sampling method (`bounded-observed-audio-islands-v3`)
and independently **reconstructs each claimed match range from its
underlying strong six-second anchors**. It rejects an old 25-second
unsupported gap even if saved JSON claims a continuous match, as well
as forged range endpoints/counts, before any source-video download.
Prior source-video pair caches lacking the sampling-method stamp are
invalidated and remeasured under current source rights and hash checks.
This protects direct CLI stage execution, not just the nine-stage runner.

The acoustic cut refinement used by the same nine-stage holdout trial now
preflights **all** coarse audio-supported regions against its bounded
sampling-point budget. When 1-second spacing would exceed the budget, it
can use 1.5- or 2-second spacing across *every* supported section, always
sampling each section's final eligible audio window. If even 2-second
sampling cannot cover all ranges, the entire refinement remains
`UNKNOWN_SAMPLING_BUDGET` with no partial cut claims. The saved
`refinement.sample_spacing_seconds`, `planned_points`, and
`sampling_strategy` make the tradeoff visible; wider spacing can still
miss short transitions. This is not native frame-accurate editing gold.

The refinement schema is now `bounded-acoustic-cut-brackets-v2`.
Previously measured v1 cut/source-side/time-map cache chains do not
qualify as v2 measurements; rerun the already documented bounded holdout
command to recompute them with current provenance guards.

The next audit stage measures **multiple constructed editing patterns on
real acquired bounded original video** without user data preparation. Each
real `YtDlpSourceProvider` source-video measurement can be tested on five
controlled changes to its SHA256-verified decoded frames: JPEG overlay,
brightness shift, small center zoom, reversed temporal order and adjacent
frame-pair time swaps (which cannot leave three source frames in order). ORB geometry plus independent DCT-temporal verification
are exercised for every case. Only explicit measured correspondence or
evaluated time-sequence noncorrespondence can count as a pattern outcome;
decoder failures, comparison limits and unrecognized statuses are
**UNEVALUABLE**, never successful negative samples.

All patterns share the **same already acquired source video**; they do
not cause extra yt-dlp downloads. A bounded SHA-keyed pattern history
is saved per pending clip, with strict anti-gold/schema checks before the
next media request. The existing
`uv run holoclip examples-source-controls-report` now also reports
per-pattern observed/evaluable/unevaluable counts, measured constructed
pass fractions, and **distinct original stream count**, with per-source pattern breakdowns showing
how many bounded samples from each original were actually evaluable. Identical
source-video bytes reached through different human clips count only
once across the report. Zero real samples mean null pattern pass rates,
not 100% success. Multiple windows from the *same* stream do not
provide independent stream diversity; no observed pattern metric is
an editor precision/recall or true source false-positive rate.
Production `ready=false` is unchanged.

The unattended `source_visual_pending` worker now also runs an
**actual-acquired-video constructed control audit** once per cycle
on an already downloaded, public/policy-allowed original video sample
when using the real `YtDlpSourceProvider` (never on synthetic/fake
provider objects). It SHA-verifies the measured re-encoded video and
up to four sampled decoded image files, produces a *known positive*
by JPEG recompression and a small generated overlay, and a *known
temporal-order decoy* by reversing those same images while keeping
timestamps ascending. Both go through the full ORB + independently
checked pixel-temporal matcher. This adds **no extra online video
downloads**, no model or paid API, and has a four-frame bound.
All output uses a distinct, non-learning control directory.

Completed control outcomes are persisted by source video SHA256 and
deduplicated across repeated runs. Use
`uv run holoclip examples-source-controls-report` to report the actual
number of observed video control samples and the fraction of constructed
positive/temporal-decoy controls recognized. With no actual saved
samples, counts stay at zero and fractions are NULL. Historical
records are fully checked (video hashes, ownership, no-gold flags)
before new media acquisition. These **constructed labels are not
human editing/source labels**; the report always leaves actual source
precision, recall and false-match rate NULL, regardless of whether
controls pass. Only running on actual public video provides empirical
codec/control behavior; it does not prove any clip came from a
particular Hololive livestream or make `ready=true`.

Real source/finished frame matches now have a **second independent
pixel-domain temporal check** before the visual scout records them as
a positive correspondence. ORB/RANSAC first proposes three or more
time-advancing image pairs; the CPU checker then independently reads
their checksum-verified PNG/JPEG bytes and compares low-frequency DCT
perceptual fingerprints (with bounded crop/scale views), verifies
non-static content across time, and checks each proposed aligned
frame against off-diagonal alternative frames. A proposed ORB match
that fails any additional check becomes explicit
`UNKNOWN_INDEPENDENT_PIXEL_NOT_CORROBORATED`, not a source match or
a negative label. Corrupt/deleted measured frame bytes likewise
fail closed. The independent check does not require an LLM, GPU,
network service, Docker or user-provided footage.

This is conservative: aggressive cropping/overlays can produce
UNKNOWN even for truly related imagery, and perceptual hashes alone
do not prove a source identity. Prior historical ledger observations
were ORB-only and are **not retroactively claimed** to have passed
the new pixel test. Actual real Hololive held-out precision/recall,
original native stream PTS, reviewer labels and production readiness
remain unverified (`ready=false`).

Saved original-stream comparisons now also have an **automatic candidate
evidence assessment**. Instead of ranking candidate originals by naive
image-match count, it recomputes each candidate's independent *two-sided*
finished/original window support from the strictly validated ledger:
`NO_SAVED_POSITIVE_VISUAL_WINDOWS`,
`SINGLE_DISTINCT_WINDOW_UNREVIEWED`, or
`MULTIPLE_DISTINCT_WINDOWS_UNREVIEWED`. Multiple matched original
candidates remain explicitly `UNKNOWN_COMPETING_VISUAL_ORIGINALS`,
even if one has more matches than the others. A missing match never
rejects an original; a more supported candidate is *not selected*.
No learned probability, source identity, human cut label, or confidence
number is fabricated.

This assessment is stored after each probe and revalidated against the
exact original video evidence and candidate roster before any new video
acquisition. It includes a checksum of the bounded observed event list,
distinct finished/source sampled contexts, missing-coverage limitations
and a concrete *autonomous next evidence step*. The raw ledger now also
rejects changes to one finished video's sampled bytes across different
candidate originals, and changes to one original video's sampled bytes
across finished sample intervals. None of this relaxes source rights,
learning/holdout isolation or the `ready=false` production gate.

For missing-original discovery, each completed sample can now contribute to a
**persistent reference-free visual evidence ledger**. A new corroboration
record requires at least three distinct coherent output/original frame pairs
and binds both 12-second sampled ranges, the original candidate ID and each
sampled video SHA256. Repeating the same two media ranges cannot increase the
count; changing bytes under an already counted range fails closed. An
independent correspondence requires both a *different nonoverlapping output
clip interval* and a *different nonoverlapping original stream interval*.
Multiple seemingly matched originals remain explicitly ambiguous.

To make independent evidence possible, finished-clip sampling now rotates
through up to six distinct, nonoverlapping 12-second clip intervals, instead
of always checking the clip center. When one-shot weak-match source
refinement is pending, the original finished interval is pinned until
completion so clip-byte checksum validation remains sound. Every
candidate's ledger contains only `UNREVIEWED_...` hypotheses, no source
identity proof, native PTS, learned original ID, held-out label, evaluated
editorial accuracy or production gate promotion. Stale/corrupted/forged
historical evidence blocks new media downloads before acquisition.

Long-original visual discovery now uses an adaptive **coarse-to-fine**
sampling policy. The original broad breadth-first source scan remains
the default. If exactly one already-decoded original section supplies
two distinct, unambiguous, time-advancing image matches (insufficient
to call a source match), the worker stores a **weak, unreviewed image
hint** tied to that candidate's stream ID, source duration, finished
SHA256 and broad-scan cursor. The *next* visit to that candidate uses
up to three new nearby 12-second source windows, with none overlapping
the coarse evidence. That hint is then consumed; the following visit
must resume breadth-first source coverage. Invalid, stale or promoted
hints are blocked before any original-video download. This preserves
the same bounded acquisition budget, avoids repeated scanning of
the same positions and does not assign source or editor gold.

Unresolved source shortlists now flow into an **automatic bounded visual
verification worker** during ordinary `examples-grow-corpus` cycles.
`uv run holoclip examples-verify-source-visual` also runs it separately
for diagnosis. It refreshes public/ended status and source-range
policy, samples at most two proposed originals per pending clip,
and requests only 12-second beginning/middle/end sections from each
candidate original plus one short finished-clip section. The range fetcher never falls back to full-original acquisition.
Durable per-candidate round-robin selection checks all shortlisted original
streams across successive cycles, while a per-original breadth-first scan
cursor chooses new non-overlapping
12-second positions on subsequent cycles (initially start/middle/end), so
repeated autonomous runs expand real sampled coverage instead of endlessly
re-decoding the same three timestamps. CPU ORB/RANSAC
matches at least three distinct, time-advancing image pairs within
one bounded original source section; repeated, static and ambiguous
matches remain UNKNOWN. Media SHA256, requested range and per-candidate
diagnostics are saved to the persistent corpus scout state.

Even a unique sampled match is `VISUAL_SEQUENCE_CORROBORATED_NOT_SOURCE_GOLD`.
The original source may occur outside sampled windows or multiple
streams may legitimately share footage. No sparse visual match is
promoted to source identity, exact stream timestamps, training,
holdout, editorial gold or `ready=true`. A missed match does not
rule out the candidate, and `source_visual_pending` remains an
automatic work item, not a demand for user-uploaded data.

The autonomous public human-clip corpus discovery now performs a
**bounded public-source candidate scout** when an editor omits a source
link or lists multiple equally plausible original streams. It queries
public metadata only (up to two deterministic YouTube search phrases,
five results per phrase and six individual metadata probes), checks
ended/public status, minimum plausible original duration and applicable
metadata policy, and stores an unverified shortlist in
`CorpusGrowthItem.source_scout` in the persistent corpus state.
Explicit source links are inspected first, without issuing extra broad
searches. No personal source links or manual preparation are needed.

**Critically, source scouting does NOT verify the original stream.** The
item enters `source_visual_pending` when a shortlist exists (otherwise
`review`), with `source_url=None` and `source_stream_id=None`;
it cannot join the learning/holdout split
or trigger source acquisition. Each record states
`visual_correspondence_tested=false` and
`source_identity_verified=false`. This is the *metadata candidate
retrieval* prerequisite for future bounded visual candidate
verification—not a claim that missing-source clips are solved. No
video download, paid API or GPU is added by the scout.

The same guarded local cut-side stage now also runs an
**audio-offset-independent visual frame-sequence search**. It reuses the
already hash-verified source and finished frame manifests, compares
CPU ORB/RANSAC image geometry across up to five reserved original
video sections, and requires **two distinct, time-advancing images
on each side** of an independently detected output change. It also
rejects spatially repeated/ambiguous original matches and static
frame hashes. If visual sequences appear to skip original source
content, continue directly, or switch source sections, these remain
`VISUAL_ONLY_..._HYPOTHESIS` and not confirmed human cuts.

Search is capped at three output-only changes and 2,400 image
comparisons, with explicit UNKNOWN for sparse sampling, repeated
scenes, exceeded budgets or decoding failure. It uses **no audio
offset to rank frame matches**, but the *existing guarded source
section inventory* still requires prior audio reservation; it
cannot identify an entirely unknown livestream from scratch.
No extra original-video downloads, prompts, labels, GPU, paid
provider or Docker. The sample-image timestamps remain re-encoded,
local clocks; exact source/finished frame PTS and gold do not emerge.
The saved reference-free audit includes image-only hypothesis counts
but does not grant `ready=true`.

The existing ninth-stage local media audit now also performs a **bounded
source-side visual crosscheck** for output-only changes that did not overlap
acoustic cut candidates. It uses only already verified original-video
sections and *two neighbouring same-group, continuous audio anchors* to
derive an approximate local source-video time. Source section-clock quality,
source/finished hashes and current clip policy are checked first.
At most six output-only visual peaks are tested; if the probe budget or
time correspondence fails, the result remains **UNKNOWN**, without
decoding arbitrary original livestream sections.

The source-side detector distinguishes `SOURCE_VISUAL_CHANGE_ALSO_OBSERVED`
from `SOURCE_NO_ISOLATED_CHANGE_OBSERVED` as descriptive hypotheses,
not a declaration of human-edit causation. The results are persisted in
`independent_visual_source_checks`, included in the real-cut diagnostic
totals and the existing offline triage. They do not provide verified
native source-frame PTS, an exact cut time, or human/editor gold, and
`ready=false` remains.

The bounded holdout audio-cut stage also performs an **independent visual
survey of the already-downloaded finished video**. Up to six short,
deterministic FFmpeg windows (default six seconds each) are examined,
including when there is *no acoustic cut candidate*. Output-only visual
change peaks that do not overlap acoustic candidates are stored under
`independent_output_visual_survey` and surfaced in
`observations.real_cut_observations` and the existing offline diagnosis.
The survey records actual successfully decoded coverage and the unexamined
duration. A failed window gives an explicit UNKNOWN and **never counts as
zero changes**. No new URLs, manual labeling, paid service or GPU are
needed, and a cache fingerprint change causes the survey to run when
the measured clip is next processed.

These output-only peaks are **not verified editor cuts**: a gameplay scene
change, camera move, overlay or transcode can have the same appearance.
They do not automatically enter source-side cut evidence, native frame
correspondence, human editor precision/recall, or production routing.
Some long-video intervals are deliberately unsampled; no complete cut
recall can be inferred from this bounded scan. Videos without the
pipeline's initial trusted source-audio reservation/anchors still need
upstream evidence before this specific holdout stage can execute.

For **actual unreviewed human-edited holdout clips**, the existing
`examples-validate-holdout-e2e` nine-stage command now also emits
`observations.real_cut_observations` (per selected clip) and
`observations.real_cut_observation_totals` inside its saved
`holdout-e2e-audit.json`; `--all-batches` also preserves every
successfully measured clip and cumulative reference-free totals in
`holdout-e2e-all-batches.json` instead of keeping only the latest batch.
This describes *measured candidate signals*:
acoustic candidate counts, unknown audio transitions, median/P90 bracket
width, overbroad brackets (>8s), output-video change corroboration, and
independently matched original/finished two-sided visual candidates.
Nonunique/missing acoustic support or an exhausted audio-sampling budget
is **unmeasured** (null counts), not evidence of zero detected cuts.
A missing/old method or stale/mismatched stage provenance gives a
separate unverified/null result, **never a zero or a perfect score**.
Only reports for stages executed in the current invocation are included,
so an old successful visual result cannot validate a new cut result.

These real-video descriptive counts are **not** measured human-editing
precision/recall, independent reviewer gold or frame-accurate cut time;
they leave `editor_cut_precision`, `editor_cut_recall`,
`ready`, and all production/gold claims null or false. They do not
require a user-provided URL, manually labeled cut list, additional
download, extra model call, or a new Windows command. See the
[autonomous evidence policy](docs/autonomous-evidence-policy.md).

After a real or interrupted `--all-batches` run, the existing automatic
offline diagnosis now includes `cut_signal_triage` in
`holdout-e2e-diagnosis.json`: bounded, deterministic per-clip codes for
unsupported audio, >8-second candidate intervals, unknown transitions,
missing output-video support and missing original/finished two-sided
corroboration. It validates recorded clip IDs, non-promotion flags,
per-batch vs cumulative counts, and plausible measurements **before**
reporting the codes. Mutated or incomplete #139 diagnostics fail closed
as `BLOCKED_INVALID_AUDIT`; older audit files without #139 fields say
`LEGACY_NO_CUT_OBSERVATIONS`. Simulated tests remain labeled simulated.
This is inspection of saved JSON only; it does **not** reopen media,
self-calibrate thresholds, change original audit files or provide
human-cut precision/recall. The next-action code for earlier stage
blockers retains precedence over descriptive cut-signal triage.

For quantitative validation of known **synthetic FFmpeg edit scripts**,
the offline `examples-score-scripted-cuts` command now compares scripted
hard-cut output times with saved unreviewed acoustic cut intervals. It
counts one-to-one matched scripted cuts, unmatched candidate
**false positives**, missed scripted **false negatives**, per-candidate
bracket width and absolute midpoint error. A broad interval over 8s is
counted as an **unmatched false positive**, even if it covers a true
scripted boundary; a single interval may never count as multiple hits.
A continuous, unedited synthetic excerpt provides a zero-cut negative
control. A missing denominator is reported as `null`, not perfect
precision or recall.

The reference file must explicitly declare its **synthetic construction**
origin. For example, on an actual locally executed 44-second two-piece
FFmpeg fixture with its scripted join at output t=22s:

```json
{
  "version": "scripted-ffmpeg-cut-reference-v1",
  "reference_kind": "SCRIPTED_FFMPEG_ONLY",
  "clip_id": "youtube:aaaaaaaaaaa",
  "duration_seconds": 44,
  "cut_seconds": [22]
}
```

Save that exact file separately as a `scripted-reference.json` only when
the media was in fact constructed by that script, then run:

```powershell
uv run holoclip examples-score-scripted-cuts --reference scripted-reference.json --cut-report data/provider-benchmark/holdout/cut_brackets/holdout-cut-brackets.json --output data/provider-benchmark/holdout/scripted-cut-scores.json
```

This scorer performs **no downloads, model inference, FFmpeg or media**
processing, does not mutate the saved media audit or training corpus, and
**never reports human editor-gold precision/recall, production readiness
or native frame-exact cut positions**. Existing real Hololive clips still
require independent manual/editor-reference annotations for genuine
held-out quality estimation.

The acoustic matcher refuses to call an entire passage supported merely
because two 6-second independently matching fragments have the same
original-source offset: gaps exceeding four seconds without a verified
anchor form explicit separate acoustic islands. This guards against
equally long silence/narration/BGM replacement falsely appearing as a
complete uninterrupted original source range; source audio caches using
the previous 25-second gap allowance are invalidated.

The integration suite also exercises a reencoded **three-piece**
48-second video with test-only known edited boundaries at 16s and 32s.
This validates synthetic cut bracketing, not real Hololive accuracy.

The offline FFmpeg integration suite also covers a real re-encoded
**two-piece** source↔finished edit (original 9–31s followed by 44–66s)
with a known scripted cut at edited t=22s, with and without quiet BGM.
That controlled test must keep edit-cut hypotheses and timing-map outputs
non-promotable; it is **not** evidence about real human-edited Hololive
videos or production acceptance.

After gathering grounded original↔finished frame and cut evidence,
create a **strictly diagnostic** partial output/source time map. Short
interval candidates must be supported by independently unique audio
and two ordered visual frame links, while uncertain gaps are retained:

```powershell
uv run --extra media holoclip examples-build-partial-time-map --max-clips 3
```

The output is not a verified dense OutputSourceTimeMap and is never
used as frame-accurate edit instructions, learning gold or production
provider evidence. No manual clip URL/timestamps or paid AI models.

To distinguish strong candidate edit discontinuities from unsupported
scene-change guesses, inspect the *actual bounded original and finished
video pixels* on both sides of the previously observed acoustic cut:

```powershell
uv run --extra media holoclip examples-compare-cut-sides --max-clips 3
```

This uses only previously downloaded section media and checks independent
frame matches, repeated content, rights and time-consistency. It produces
candidate observations, **not** verified edit-cut frames, human editorial
intent, a dense OutputSourceTimeMap or production-quality gold.

After the bounded source-section clock has been checked, local ORB/RANSAC
can compare real original/finished **sampled image frames** against the
independent audio timeline and reject repeated visual content or time
contradictions:

```powershell
uv run --extra media holoclip examples-correlate-source-frames --max-clips 3
```

Partial links retain exact measured frame IDs and *approximate* source/
clip time evidence, not a dense source-to-output time map, confirmed cut
frames, human-reviewed gold or production readiness.

After original-video sections are captured and measured, their **actual
audio** can be independently matched against the source stream's cached
full-audio fingerprint, with bounded section native-frame PTS inspection:

```powershell
uv run --extra media holoclip examples-calibrate-source-clocks --max-clips 3
```

This removes blind reliance on yt-dlp's requested source seek position when
building *approximate* source-to-finished time provenance. Audio/video codec
offsets, absolute original native frame PTS, exact edit-cut time and editor
intent still require independent validation. The command does not certify
them or create semantic gold.

The audio cut-interval command now additionally performs **bounded,
finished-video visual jump checks** (8 fps, max 10-second padded interval)
as weak corroboration. Results include explicit unknowns for natural source
scene changes, exact frame PTS and unmatched or visually continuous edits;
no verified gold or edit-frame timestamp is asserted.

After the sparse source/finished visual observations, candidate cut
intervals can be refined using denser local acoustic correlations without a
paid inference service, manually provided timestamps, or full-source video:

```powershell
uv run --extra media holoclip examples-refine-cut-candidates --max-clips 3
```

This records **time ranges** where uniquely matched audio changes source
offset; it does not certify exact-frame edit cuts, time-map completeness,
visual confirmation or human-review quality.

Source-vs-finished visual geometry can now be inspected after bounded
source-pair preparation without a cloud AI key, user-authored labels or
manually selected frame IDs:

```powershell
uv run --extra media holoclip examples-inspect-visual-edits --max-clips 3
```

This uses measured ORB/RANSAC frame correspondences to propose zoom/reframe
candidates with geometry/inlier diagnostics. It **does not** certify an exact
cut map, text/subtitle recognition, verified edit intent, or production gold.

After audio anchors have been established, the optional
`examples-measure-source-pairs` stage requests only the identified bounded
original-video sections and saves checksum-verified frame/shot evidence paired
with measured finished-clip windows:

```powershell
uv run --extra media holoclip examples-measure-source-pairs --max-clips 3
```

Unverified absolute seek offsets, exact cuts, visual edit semantics and semantic
gold remain explicit unknowns. The paired measurements cannot promote a
production AI route.

`examples-align-holdout` downloads original-stream **audio**, rechecks
rights/source identity, then records unique FFT-matched audio anchors and
approximate clip-to-source ranges. It rejects ambiguous matches; it does
not claim frame-accurate edit cuts, visual truth or reviewed gold.

Once new holdout source streams have been reserved by
`examples-grow-corpus --discover-only`, run:

```powershell
uv run holoclip examples-prepare-holdout --max-clips 3
```

This automatically retrieves permitted public finished clips and creates
resumable, checksum-verified video/scene/frame measurements. No cloud AI key or
manual reference annotations are required for this stage. Measurements remain
unreviewed and cannot qualify a production model; corresponding original-source
edit mapping, cross-stream event isolation and calibrated gold still remain.


Holoclip discovers public human-made clips without requiring the operator to
supply training URLs. New source streams are automatically reserved for later
holdout evaluation **before** learning ingestion; clips from the same source
stream cannot be split across train and reserved holdout. The status can be
inspected offline with `uv run holoclip examples-holdout-status`.

Reservations are not reviewed gold and cannot enable production inference.
The source/event-leakage checks, real-media gold calibration and full autonomous
evaluation remain under development. See
[autonomous evidence policy](docs/autonomous-evidence-policy.md). If the
evidence is uncertain, Holoclip blocks rather than asking the operator to
fabricate labels or lowering the quality floor.

## Corpus editorial learning

Holoclip now learns an explicit corpus-level editorial model from trusted aligned human-made clips.
Individual example retrieval remains supporting evidence and is not called learned editorial intelligence.

The autonomous flow is:

    human-made clips -> source alignment / unchanged quality gate
        -> deterministic full learning cards and contextual omissions
        -> cached per-card contrast / measured beat observations (qualified fast reasoning)
        -> bounded batch pattern induction (qualified strong reasoning)
        -> bounded cross-batch mechanism consolidation (qualified strong reasoning)
        -> automatic support evidence assessment
        -> persisted Editorial Profile and recomputed readiness
        -> profile-guided broad discovery / final selection / measured edit plan

```powershell
uv run holoclip examples-learn-profile
uv run holoclip examples-learning-status
uv run holoclip examples-grow-corpus --cycles 20
```

The last command searches public human clips without requiring clip URLs, resolves their sources,
quarantines uncertain/policy-review examples, acquires audio, transcribes, aligns, enriches, indexes,
and refreshes the profile. Each cycle is bounded and resumable. Continue beyond the initial target
with `--target-trusted 100 --target-streams 50`. `--discover-only` performs metadata discovery only.

Normal `analyze` and `discover` refresh missing/stale profiles using stage caches after trusted
examples change. Profiles that are unready or have stale provenance are ignored automatically.

The minimum production gate is 30 trusted clips, 15 trusted source streams, 5 core patterns and
80% trusted-example coverage by core patterns. Core requires 3 trusted clips from 2 distinct streams;
code derives counts, checks cited IDs and automatically assesses actual supporting evidence.
Emerging mechanisms and unsupported principles do not drive production discovery.

The initial local corpus has **11 clips, 9 trusted, 2 review, 10 total source streams and 8 trusted
source streams**. It is **not ready**. Existing accept/maybe/reject labels are frozen validation
records; `label` and `review` refuse new labels while readiness is false.

Once learning is ready, run an automated comparison on an unseen transcript:

```powershell
uv run holoclip editorial-ab unseen.json --stream-id unseen-source-id
```

This checks training leakage and compares identical discovery/selector settings with profile ON/OFF.
Its blinded LLM diagnostic is not human acceptance or proof of generalization. Optional human
confirmation belongs at the end and should use only a small blind sample.

See [editorial learning](docs/editorial-learning.md) for provenance, cache, boundary and scaling contracts.

## Setup

Python 3.12+ and FFmpeg on PATH are required.

~~~powershell
git clone https://github.com/Kataage/holoclip.git
cd holoclip
uv sync --python 3.12 --extra dev
copy .env.example .env
~~~

Set these values in .env:

~~~text
NVIDIA_API_KEY=...
GROQ_API_KEY=...
~~~

AI requests enforce [ZERO_COST_ONLY](docs/zero-cost-runtime.md). Supply current
credential-bound free-account evidence and declare public workflow data before
running inference. Model/endpoint overrides must match the verified registry.
Missing evidence blocks safely; no billing activation or paid fallback exists.

## One-command discovery

For local media:

~~~powershell
uv run holoclip discover path\to\stream.mp4
~~~

This automatically:

1. extracts lightweight audio chunks with FFmpeg
2. transcribes them through a qualified ASR capability route
3. writes a timed transcript under data/transcripts/
4. runs NIM candidate discovery
5. writes an evaluation run under data/evaluation/runs/

Operator review is deferred until corpus learning is ready and an automatic unseen-stream ON/OFF comparison has run. Existing labels are retained for later validation. Optional final review commands are described below:

~~~powershell
uv run holoclip review data\evaluation\runs\stream.json
~~~

During review:

- `p`: play only the proposed source segment (with a small context pad) using FFplay
- `c`: show surrounding transcript
- `a`: accept
- `m`: maybe
- `r`: reject
- `s`: skip
- `q`: stop

New evaluation runs created by `discover` remember the source media path. For an older run, supply it explicitly:

~~~powershell
uv run holoclip review data\evaluation\runs\stream.json --media path\to\stream.mkv
~~~

## Transcription only

~~~powershell
uv run holoclip transcribe path\to\stream.mp4
~~~

By default, long media is converted to 15-minute 16 kHz mono MP3 chunks before upload. This keeps local compute low and keeps individual uploads comfortably small.

## Analyze an existing transcript

~~~powershell
uv run holoclip analyze path\to\transcript.json --top-k 5
~~~

The transcript format is:

~~~json
{
  "segments": [
    {
      "start": 0.0,
      "end": 5.2,
      "text": "今日はちょっと話したいことがあって"
    }
  ]
}
~~~

Timestamps are seconds from the start of the source.

## Evaluation

Calculate current metrics:

~~~powershell
uv run holoclip metrics --runs-dir data\evaluation\runs
~~~

See docs/evaluation.md for the validation protocol.

## What this does not do yet

- acquire arbitrary third-party YouTube media
- build the existing-clip human-sense database
- render videos
- publish videos

Source acquisition remains isolated from the ranking core so the project does not depend on one platform-specific extraction method.

See docs/architecture.md, docs/evaluation.md and docs/roadmap.md.


## Human-made clip example store

Existing human-made clips are used as weak supervision rather than model fine-tuning.

Import a JSON manifest:

~~~powershell
uv run holoclip examples-import data\examples\human_clips.example.json
~~~

The validated examples are stored by default in:

~~~text
data\examples\human_clips.jsonl
~~~

Inspect coverage:

~~~powershell
uv run holoclip examples-stats
~~~

The schema keeps clip/source provenance, resolved source timestamps, abstract story structure,
members/topics/traits, relationship context, normalized performance metadata, and alignment
confidence. Source timestamps may be omitted initially and filled by the planned automatic
clip-to-source transcript alignment stage.


## Align a human-made clip back to its source stream

For the first local learning test, supply a local human-made clip file and an already-created
transcript of the original source stream:

~~~powershell
uv run holoclip examples-align path\to\clip.mp4 data\transcripts\source-stream.json
~~~

The command:

1. transcribes the clip through a qualified ASR capability route (and caches that transcript)
2. matches multiple spoken-text anchors against the source transcript
3. estimates the original source start/end timestamps
4. prints confidence and a few anchor matches
5. does **not** write to the learning store by default

After the alignment looks correct, rerun with provenance and `--save`:

~~~powershell
uv run holoclip examples-align path\to\clip.mp4 data\transcripts\source-stream.json ^
  --clip-url "https://www.youtube.com/watch?v=CLIP_ID" ^
  --clip-title "clip title" ^
  --source-url "https://www.youtube.com/watch?v=SOURCE_ID" ^
  --stream-id "SOURCE_ID" ^
  --save
~~~

If confidence is below the default threshold (0.55), the example is stored without accepted
source timestamps so it cannot silently become trusted training data. The threshold can be
changed with `--min-confidence`.

This alignment stage is intentionally local-first and independent of YouTube acquisition.
A future SourceProvider can supply the same media/transcript inputs without changing the
alignment or retrieval logic.


## Enrich an aligned human clip into a learning example

After a real human-made clip has been aligned and saved, enrich it with the strong editorial
model:

~~~powershell
uv run holoclip examples-enrich <EXAMPLE_ID>
~~~

This adds:

- abstract setup / development / payoff
- why a human editor likely selected the event
- reusable structural traits
- members and topics when supported by the transcript
- relationship context
- context requirement
- adjacent source windows as **weak negatives**

Weak negatives are deliberately labeled weak: a nearby region not selected by this particular
clip editor is useful contrast, but it is not treated as proof that the region is objectively
uninteresting.

The enrichment records the model and extraction method so later retrieval decisions remain
auditable.


## Multi-cut alignment

Human-made clips often remove long stretches from the original source or join several source
moments into one clip. Alignment v2 therefore stores an explicit edit map instead of assuming
one continuous source range:

~~~text
clip 00:00-00:30 -> source 10:00-10:30
clip 00:30-01:00 -> source 20:00-20:30
~~~

`examples-align` prints every detected segment and, when saved, writes them to
`alignment_segments`. The legacy `source_start/source_end` fields remain as a bounding range
for backward compatibility.

During enrichment, source gaps between selected segments are preserved as
`removed_between_selected_segments` weak negatives. This lets later retrieval learn not only
what human editors selected, but also what they deliberately skipped between retained moments.


## Migrate existing local data to the canonical store

The current JSON/JSONL evaluation and human-example files remain valid during the migration
period. Import them into the canonical SQLite store without deleting or rewriting the originals:

~~~powershell
uv run holoclip migrate-legacy
~~~

Defaults:

~~~text
evaluation runs: data/evaluation/runs/
human examples: data/examples/human_clips.jsonl
canonical DB:   data/holoclip.db
~~~

Use `--strict` when migration diagnostics should fail the command on ambiguous or malformed
legacy rows. Migration is idempotent: rerunning the same import updates the same canonical IDs
rather than creating duplicate entities.

Legacy feedback JSONL remains in active use for the current review/metrics workflow because old
feedback records do not contain a trustworthy creation timestamp. Holoclip does not invent one
during migration.


## Structural retrieval from human-made clips

After an aligned human clip has been enriched, Holoclip automatically refreshes the derived
structural retrieval index. Positive examples are embedded from abstract editorial structure
(setup, development, payoff, selection reason, traits, topics and relationship context) rather
than clip title wording. Weak negatives are indexed separately.

Production selects a benchmark-qualified `embedding` capability route and pins the
`mesh:<provider>:<model>` vector-space namespace. The NVIDIA Nemotron Embed 1B endpoint
is a diagnostic candidate; a returned vector does not qualify it. Changing the model
requires a separate index rebuild. Vendor-specific model overrides cannot bypass
the production router or mix vector spaces.

Manual rebuild/debug commands:

~~~powershell
uv run holoclip examples-index
uv run holoclip examples-retrieve "unexpected confession with setup and payoff"
~~~

The index is derived state in `data/holoclip.db`; the HumanClipExample store remains the
source material for rebuilding it. Re-indexing unchanged examples reuses their existing
vectors, changed structures are re-embedded, and documents belonging to removed examples are
removed from the active derived index.

## URL-based human clip ingestion with yt-dlp

Holoclip can now use yt-dlp through the replaceable SourceProvider boundary to turn a public
human-made clip URL into a weak-supervision example.

For the common case:

~~~powershell
uv run holoclip examples-ingest-url "https://www.youtube.com/watch?v=CLIP_ID"
~~~

The command:

1. reads clip metadata with yt-dlp
2. looks for an unambiguous original/source YouTube link in the clip description
3. downloads the clip and original source using the training-audio profile
4. transcribes both with the existing resumable ASR capability pipeline
5. aligns the human clip back to source timestamps
6. writes/updates HumanClipExample
7. extracts setup/development/payoff/selection reason and weak negatives with the strong LLM
8. refreshes the structural retrieval index

If the description has no unique source, specify it explicitly:

~~~powershell
uv run holoclip examples-ingest-url "https://www.youtube.com/watch?v=CLIP_ID" ^
  --source-url "https://www.youtube.com/watch?v=SOURCE_ID"
~~~

Training acquisition intentionally downloads best audio rather than full-resolution video because
transcription/alignment do not need the full video stream. The source cache is separated by
profile:

~~~text
data/sources/examples/clips/<id>/training_audio/
data/sources/examples/sources/<id>/training_audio/
~~~

A future editing-video acquisition can coexist under editing_video/ without mistaking audio
for a completed video download.

Holoclip invokes yt-dlp with --ignore-config so user-global yt-dlp settings cannot silently
change the deterministic training pipeline. It does not enable browser cookies or authenticated
access by default.


### yt-dlp runtime freshness policy

yt-dlp is intentionally **not pinned as a normal Holoclip Python dependency**.

For normal source acquisition Holoclip uses uv/uvx as a managed tool runtime:

- default channel: `nightly`
- each new Holoclip source-ingest process refreshes yt-dlp package metadata
- the selected-channel yt-dlp requirement is then run from uv's offline warmed cache for the lifetime of that provider/job
- if extraction fails, Holoclip performs one forced runtime refresh and retries once
- the exact yt-dlp version, channel, and JavaScript runtime are stored in human-example acquisition provenance

This avoids keeping a stale downloader in a long-lived project environment while still making one
ingest job reproducible.

Check the source runtime without downloading media:

~~~powershell
uv run holoclip source-doctor
~~~

The default can be changed for debugging/reproducibility:

~~~powershell
set HOLOCLIP_YTDLP_CHANNEL=stable
uv run holoclip source-doctor
~~~

For reliable current YouTube extraction, Holoclip also checks for a supported JavaScript runtime.
Deno >= 2.3 is preferred by yt-dlp; Node >= 22 is also supported and is enabled explicitly by
Holoclip when selected. The managed yt-dlp package uses the `default` extra so the EJS component
is present.


A nightly build's displayed `yt-dlp --version` string is recorded as provenance only. Holoclip
does **not** feed that display string back into a `yt-dlp==...` package requirement, because
nightly display identifiers are not guaranteed to be valid registry version specifiers.


## Audit one human-made clip example

Inspect the exact alignment and learning state without calling any external model:

~~~powershell
uv run holoclip examples-show <EXAMPLE_ID>
~~~

For the second real validation example:

~~~powershell
uv run holoclip examples-show 2002038236448bab
~~~

The report shows:

- clip/source provenance
- aggregate alignment confidence and bounding range
- every clip -> source alignment segment
- transcript excerpts from both sides of each aligned segment
- source gaps between retained segments
- setup / development / payoff / why-selected structure
- weak negatives
- retrieval documents currently stored for that example
- acquisition/runtime provenance

Use JSON when the report needs to be inspected by another tool:

~~~powershell
uv run holoclip examples-show 2002038236448bab --json
~~~

New URL ingests also preserve the original resolved source URL (including a YouTube `?t=`
timestamp when present), source-resolution reason/score/context, and whether the source was
auto-detected. The audit report compares that declared timestamp hint with the first aligned
source segment when available.


## Clip policy gates

Holoclip treats clipping restrictions as first-class data rather than a hard-coded
member blacklist. Restrictions can be scoped to a source video, a channel, or an
exact time range, and each rule keeps a reason and optional evidence URL/text.

Before learning from a human-made clip, Holoclip now checks:

- explicit no-clipping text in the original source title or description
- yt-dlp availability metadata that indicates restricted/paid content
- source/channel rules in `data/policy/clip_rules.jsonl`
- range rules after transcript alignment
- possible spoken restrictions such as `ここは切り抜かないで` near an aligned range

Hard `block` findings cannot be overridden. Automatic spoken-marker findings are
`review`: they stop unattended processing, but can be continued after manual
inspection with `--allow-policy-review`.

Check a source before acquisition:

~~~powershell
uv run holoclip policy-check "https://www.youtube.com/watch?v=VIDEO_ID"
~~~

Add a source-level rule:

~~~powershell
uv run holoclip policy-add ^
  --id "example-source-ban" ^
  --decision block ^
  --reason "Talent explicitly requested that this stream not be clipped" ^
  --source-id "VIDEO_ID" ^
  --evidence-url "https://..."
~~~

Add an exact prohibited range:

~~~powershell
uv run holoclip policy-add ^
  --id "example-range-ban" ^
  --decision block ^
  --reason "Talent requested this section not be clipped" ^
  --source-id "VIDEO_ID" ^
  --start 1234 ^
  --end 1380 ^
  --evidence-url "https://..."
~~~

Rules should record the narrowest scope supported by the evidence. A stream-specific
request must not be promoted into a permanent member-wide ban without evidence for
that broader scope.


## Batch-ingest human-made clips

Put one JSON object per line in a manifest:

~~~jsonl
{"clip_url":"https://www.youtube.com/watch?v=CLIP_A"}
{"clip_url":"https://www.youtube.com/watch?v=CLIP_B","source_url":"https://www.youtube.com/watch?v=SOURCE_B"}
{"clip_url":"https://www.youtube.com/watch?v=CLIP_C","enabled":false,"note":"hold for later"}
~~~

Run the batch sequentially:

~~~powershell
uv run holoclip examples-ingest-batch data\examples\batch.jsonl
~~~

The batch intentionally does not parallelize model calls. Each item goes through the
same source resolution, clip-policy, alignment, enrichment and retrieval-index path as
`examples-ingest-url`.

A state file is written after every item (by default
`batch.jsonl.state.json`). Status values include:

- `succeeded`
- `needs_source`
- `policy_blocked`
- `policy_review`
- `alignment_rejected`
- `retryable`
- `failed`

Re-running the same command skips successful rows whose input fingerprint is unchanged.
If a row's `source_url` is corrected, that row becomes eligible automatically.
Use `--force` to re-run successful rows deliberately.

Source ambiguity, policy review/block, alignment rejection and transient provider
failures do not stop later rows. Authorization/configuration failures stop the batch
because they are likely to affect every remaining item.

Use `--strict` when CI/automation should receive a non-zero exit code if unresolved
items remain.
