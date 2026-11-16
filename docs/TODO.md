# macbackup — Technical TODO & Review Findings

Open items from the code, test, and documentation review. Recheck each against the current script before implementation.

---

## 1. Repeated `os.path.realpath` Calls in `Policy.decide` *(Gemini performance hypothesis reviewed on 2026-11-13)*

- **Source:** Gemini (`0.14.3` review). The call is unconditional. `scan_policy` calls `decide` twice for most visited entries (a preliminary subtree decision and the typed decision), and remote planning calls it for each existing current file. This is real repeated work, but no profile establishes its share of the observed end-to-end time; long remote inventories and private comparisons are separate costs.
- **Safety constraint:** The physical path is needed at the *start* of every decision to exclude the state directory even through a symlinked parent. A synthetic plain inclusion through a symlink alias into `.local/state/macbackup` currently returns `skip`. Merely deferring `realpath` until the plain-credential check would lose that protection.
- **Recommended work:** Profile local scan and remote planning separately before optimizing. If the cost is material, remove the duplicate call per local entry or use carefully validated resolution caching while preserving state and credential checks for aliases and path changes. Add regression tests for a symlinked state-directory ancestor and a symlinked rclone-credential ancestor before changing this logic. Treat performance improvement as unproven until measured.

---

## 2. Future Roadmap Items (Tracked from Architecture Design)

- **Standalone release and Homebrew:** publish the downloads and checksum produced by `scripts/build_macbackup_release.py` as a versioned GitHub release, then publish the formula from that same build in `kevinmuka/homebrew-tap`. Verify installation from the public tap, and verify upgrades once a second release is published.
- **Proton Drive target setup & verification:** configure `pdrive:` and `pdrive-private:` (`crypt`) with a small pilot before enabling a full backup; test transfer, `verify`, restore, mount, and `forget --mode trash`. The [rclone Proton Drive backend](https://rclone.org/protondrive/) is documented as Beta and may be incompatible with some accounts; do not infer reliability from Google tests.
- **External volume identity verification:** verify volume UUID / mount identity when backing up paths under `/Volumes/...` so an unmounted or wrong volume is never mistaken for a valid source.
- **Full-directory / subtree restore:** extend `restore` (or add a dedicated command) to recompose a directory tree from `plain/current` and `private/current` into a chosen local folder. Gemini's note about recreating `.rclonelink` symlinks and optionally applying `macos-icons` snapshots is sound for a complete current-state restore. A historical directory snapshot is a separate, harder feature because each `history/<run_id>` is sparse rather than a full snapshot.
- **Scheduled automation (`launchd`):** after both targets and archive limits are settled, use the actual installed Python/rclone paths discovered at setup and report failures, including `max_delete` stops. Gemini's notification idea is useful; its example binary paths happen to match this Mac now but are not portable defaults. Define how credentials and noninteractive errors are handled before enabling unattended runs.
- **History retention / pruning (Gemini proposal):** an explicit inspect/plan/apply workflow could control unbounded history growth, but it must be designed separately from path-scoped `forget`. Never infer that `rclone cleanup` is safe for one history subtree: on Google Drive it empties the account's trash rather than pruning a selected `history/<run_id>`. Require a reviewed scope, a restore test, and clear treatment of provider trash/native revisions before any automatic retention.
- **Documentation site:** update the public site links and sync the English and Italian manuals when this repository becomes the source of releases.

---

## 3. To Discuss (No Implementation Agreed Yet)

- **Renamed files and directories:** decide whether a pure rename should create an entry in `history`, or whether macbackup should recognize and move the existing cloud copy while retaining the current backup contract. Define how to distinguish a rename from an unrelated deletion plus addition, what evidence is reliable for both plain and crypt remotes, and how `preview`, `status`, and interrupted runs would explain the result. No rename-detection change has been approved.
- **Atomicity and recovery of `run`:** the current run is not atomic across files, areas, or providers; `status` reports a partial or interrupted result after transfer has started. Decide whether stronger guarantees are needed, and at what scope, before proposing a transaction-like design. In particular, discuss interruption during planning or policy archiving, retries, and whether a complete multi-provider snapshot is actually required. This is a question for design, not a promise that a fully atomic cloud backup is feasible.
- **Viewing backups on a phone:** confirm the desired mobile workflow once the Proton target is configured. Plain files can be browsed through a provider app when their formats are supported; rclone-crypt private copies require a compatible decryption workflow and should not be presented as directly readable there. Decide whether phone access is needed only for plain `current`, whether historical copies matter, and how to keep the private area protected.
- **Following selected symlinks:** consider an explicit opt-in list of symlinks whose targets should also be backed up; keep the current link-only behavior for all others unless a different policy is deliberately chosen. Decide whether target contents should appear under the symlink path or their real path, how plain/private/exclude and explicit-only rules apply, and whether the link itself is still saved separately. Specify handling for cycles, duplicate targets, missing or changing targets, symlinks that escape selected sources, and external volumes. Do not enable following all symlinks by default without resolving these cases. No behavior change is planned now.
