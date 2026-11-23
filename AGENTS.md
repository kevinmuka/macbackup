# macbackup contributor instructions

These instructions apply to the standalone macbackup repository.

## Start with the checkout in use

- Identify which repository and branch contain the implementation before editing. Use the current code and tests to confirm behavior. Read the relevant manual section, then the policy guide or changelog when you need the reason for a decision.
- The dotfiles repository can pin this checkout under `tools/macbackup/`. Keep the dotfiles launcher and Zsh bridge small. When updating that integration, pin a commit that other checkouts can fetch.

The implementation lives in `app/` and tests live in `tests/macbackup/`. The launcher is `bin/macbackup`, both Zsh completions live in `completions/zsh/`, and the release builders live in `scripts/`. Read `README.md` for the English manual, `README.it.md` for Italian, and `docs/` for the policy guide, technical history, and roadmap. If you have access to the shared Notion project page, use it for goals and decision context. Keep implementation instructions and specifications in Git so a checkout is sufficient to work on the project.

## Preserve backup and recovery guarantees

- Keep selection in macbackup. Use the same policy for local selection and remote classification, and give rclone an exact file manifest. Preserve typed paths, rule precedence, explicit-only handling, and the protections for credentials, state, symlinks, and hard-link aliases. A missing active source or unreadable selected tree must fail safely before an archive plan can treat it as empty.
- Keep `check` and `preview` read-only for backup sources and cloud copies. Remote preview describes a plan at the time it was read. A run must recalculate it and enforce `max_delete` before writing. Verify checks the selected sources against `current`.
- Keep readable `plain` and encrypted `private` areas separate. Archive the old current copy before uploading a file to the other area. An interrupted move can leave the previous copy in history. History is sparse, and moving a file to private does not encrypt its previous plain history. Retained copies are removed only through an explicit, scoped operation such as `forget`.
- Treat incomplete work as incomplete. Preserve operation locks, path validation, and the distinction between planned work and confirmed work in saved reports. Keep destructive actions behind the existing plan and apply checks. Preserve read-only mounts and explicit restore safeguards for overwrites and symlinks.
- Keep user settings, credentials, crypt keys, real paths, inventories, and reports out of examples, fixtures, and release files. Use synthetic files and disposable local or crypt remotes for development. Run commands against the user's live storage only when the user requests that operation.

## Change the CLI and documentation together

- Keep the runtime compatible with Python 3.11 and later and the Python standard library. Put parsing in `cli.py`, policy and traversal in their existing modules, remote operations in `commands/`, and shared inventory, transport, and state logic in their respective modules. Reuse checks shared by multiple commands.
- English is the source language. Use `app/locales/it.py` for Italian and keep `MACBACKUP_LANG` as the language selector. Preserve command and flag names, JSON field names and types, exit statuses, and the ability to read existing reports unless the change explicitly introduces a compatibility break.
- When commands, flags, or accepted values change, check the parser, both localized help paths, and both Zsh completion files. Completion must work offline without executing macbackup or rclone.
- When configuration changes, check the `init` templates, built-in defaults, validation, and compatibility with existing setups. Keep documented examples usable and clearly distinguished from defaults.
- Update the English manual first when user-visible behavior changes, then update the Italian manual in the same change. Cover relevant defaults, errors, interruption, and recovery. Update `POLICY_DESIGN.md` when rule precedence, safety guarantees, or their rationale change.
- Use `CHANGELOG.md` as a technical history for maintainers. Record material changes and their rationale, compatibility or recovery implications, checks actually run, and open limits. The manuals describe current behavior. Keep unfinished work in `TODO.md` and update it when the work is completed or its status changes.
- On a release version change, keep the CLI version, both manuals, and release metadata aligned. Generate the Homebrew formula, archive, and checksums from the same build. Verify the install from outside the checkout and distinguish a local package test from installation through a published tap. Check any affected public documentation and report if it still needs updating.

## Verify at the right scope

Run focused tests for the affected behavior, including failure or interruption when relevant. Use the full macbackup suite for changes to selection, remote planning, recovery, or shared contracts:

```sh
python3 -B -m unittest discover -s tests/macbackup -t tests -p 'test_*.py'
```

For CLI changes, exercise the launcher with both languages and compare text, JSON and exit behavior as applicable. For completion changes, use the existing Zsh tests that send Tab to an interactive shell. For packaging or layout changes, run the package tests and exercise the installed entry point from outside the checkout. Review `git diff --check` and report any skipped checks or live-provider behavior that remains unverified.
