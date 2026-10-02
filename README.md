# Sanitizer Bridge

One GitHub bridge repository synchronizes whole files between independent internal
and customer repositories for multiple projects. Customer updates produce internal
branches; explicit internal-side exports update customer main. There are no PRs,
merge detection, acceptance flags, or outstanding-import export blockers.

## Install

1. Copy this folder's contents (including `.github`) into the bridge repository's
   main branch. Use a private bridge when internal history is private: its state
   branches intentionally retain **complete internal and customer histories**.
2. Copy `projects/example/` to `projects/<project>/`. Set `enabled: true`, repository
   names, branch names, an exact internal bootstrap commit, and the sanitizer path.
   Project names use lowercase letters, digits, underscores and hyphens.
3. Add the Actions secrets referenced by that project's `secrets` mapping. A single
   access token can cover all three roles, or use separate tokens. Tokens need
   Contents read/write on their repositories. Workflows write is necessary if any
   published history contains workflow files; Actions write is needed on the bridge
   for dispatch tokens. Branch rules must permit the intended branch publications.
   Secret provisioning itself requires Secrets write for the operator's credential.
4. Install `templates/internal-export.yml` as
   `.github/workflows/bridge-export.yml` in the **internal** repository. Set its
   bridge repository, ref and project, and add `BRIDGE_DISPATCH_TOKEN` there.
5. For notifications, install `templates/customer-notify.yml` as
   `.github/workflows/bridge-notify.yml` in the **customer** repository. Set the same
   values, match its push branch to the configured customer branch, and add its
   dispatch secret. Protect `.github` in the sanitizer so these controls survive.
6. Set `detection.polling` and `detection.notification` independently. The bridge
   schedule polls at minutes 17 and 47 each hour. Its manual `poll` operation runs
   the same path; blank project polls all enabled polling projects. GitHub schedule
   timing is best effort. Notifications dispatch a project-specific authoritative
   fetch; they never supply trusted commits or paths.

The internal **Export shared files** action requests an export. The delegate action's
success means the request was submitted; check **Sanitizer bridge** in the bridge
repository for the synchronization outcome and exact exported internal SHA.
Access to dispatch export is privileged: use repository/token access controls.

## Bootstrap and ongoing behavior

The internal anchor is explicit; customer main is always discovered automatically.
Equal filtered trees record a checkpoint without an empty commit. A populated,
differing customer creates an import based on the configured anchor. An empty or
filtered-empty customer records `awaiting_export`: polling never writes outbound.
Run the internal export action to initialize it. That export uses the latest
configured internal branch head captured at operation start, even if it is newer
than the anchor. The configured anchor remains reachable in the bridge history.

Imports update `bridge/<project>/import-N`. They replace the included projection
with the current customer projection, preserving excluded internal files. The
first commit is based on the pinned internal epoch base; subsequent commits extend
only the bridge's generated chain. Since each established epoch has matching
included projections (or an explicit bootstrap import), this represents cumulative
customer changes without assuming shared Git ancestry. Manual integration into
internal main does not alter the pinned epoch.

A user-modified, deleted, or occupied import branch is preserved and the bridge
allocates a fresh number. The new branch continues from the last bridge-generated
commit. If there is no content change but the published branch moved, the bridge
republishes its existing commit on a fresh branch to keep it usable.

An export first preserves the captured customer history and publishes outstanding
imports. It then copies the latest internal included projection onto customer main,
including source additions, updates and deletions. **Unmerged included customer
changes can be removed by export.** Their old internal branches and complete
histories remain available for later merge, squash, cherry-pick or manual integration.
Excluded receiver files survive. Export creates an independent customer commit
parented only by customer main, with a generic message and no internal ancestry.

Successful export pins a new exact internal/customer checkpoint and starts a new
import epoch. Later imports do not replay earlier unmerged customer changes. A
no-content export may advance that checkpoint without an empty customer commit.
Repeated unchanged operations do not create commits or branches.

## Sanitizer contract

The bridge runs `bash /absolute/path/to/sanitization.sh /temporary/tree` with its
working directory set to the trusted script's own directory. Companion files may
be referenced relative to that directory. The same checked-out script and its
dependencies are used for every comparison within an operation. Scripts may be
shared across projects. Customer tree scripts are never executed.

Scripts must deterministically **delete whole files only**, leaving all retained
bytes, symlink targets and executable modes unchanged. Added or rewritten files,
mode changes, unsupported file types and script failures abort the operation.
Use pathname-based rules consistent across both repositories. Content-dependent
filters that disagree about whether the same destination path is protected can
fail with an excluded-path collision. No attempt is made to overwrite that path.
Custom trusted scripts must not follow tree-provided symlinks or access the live
repositories. The bundled example excludes `.github`, `private`, `customer-private`.

Changing the rules takes effect on the next sync, without a sanitizer version,
fingerprint, or reconciliation gate. Imports compare the current customer projection
against the bridge-generated tree, so newly included pre-existing files participate
even when the customer SHA has not changed. Newly excluded paths remain untouched
in the receiving tree. A path that is still included and disappears from the source
is deleted. Conflicting file/directory transitions involving an excluded path fail.

Binary files, executable modes, symlinks, safe unusual filenames, renames as
delete/add, and ordinary Git file/directory transitions are supported. Submodules
and Git LFS pointer files are explicitly unsupported. Only GitHub is implemented;
authentication/dispatch are isolated in `bridge/github.py` for future adapters.

## State, concurrency, and recovery

Each project has `bridge-state/<project>` in the bridge repository. Its tree holds
`state.json`; commit parents preserve prior state, source revisions, epoch bases,
generated imports, and pending destination commits. These branches contain private
history, not just JSON. Do not rewrite or garbage-collect them or delete old import
branches as an automated cleanup step.

GitHub jobs serialize by project, and every state/destination update uses an exact
expected-head comparison. The engine durably saves a pending transaction **before**
publishing its exact destination commit. The next run replays or recognizes that
commit, then finalizes state. Failure after destination publication does not duplicate
the change. A destination push and a state push are separate transactions; state may
temporarily show `pending` after a failure. Rerun the operation with the same config.

Customer append races are fetched, preserved and retried up to five times. A run
that loses the bridge state comparison fails safely; rerun it. Independently launched
CLI processes use the same compare-and-swap rules, but can require retries. GitHub
may replace an older queued concurrency job with a newer one; polling converges on
the latest state. Verify that an explicit export actually completed.

Customer history rewrites/deletion and an internal head no longer descending from
the epoch base fail clearly. Restore the source history or register a new project
identifier after operator review. Changing repository, branch or bootstrap anchor
under an existing project also fails; use a new identifier. Do not manually edit
state to claim success. Existing history remains reachable on failure.

Permission errors leave the pending operation for retry. Check token Contents and
Workflows access and branch rules; the bridge does not change protections or broaden
credentials. Authentication/fetch/filter failures never advance a successful export
checkpoint. After fixing a failed sanitizer, rerun with the intended current rules.
An already journaled publication is recovered using its saved exact commit before
new filtering is applied.

## Local execution and verification

Requires Python 3.12+, Git and Bash on Linux. Install `requirements.txt` in a virtual
environment. Export `BRIDGE_TOKEN`, `INTERNAL_TOKEN`, `CUSTOMER_TOKEN` for this process
and set `BRIDGE_URL=https://github.com/owner/bridge.git`, then run:

```sh
python -m bridge import --project my-project
python -m bridge export --project my-project
python -m unittest discover -s tests -v
```

The CLI uses temporary bare object databases and isolated materializations, and
process-local Git authentication; it does not alter credential stores or user Git
configuration. `--local` allows absolute local repository paths for synthetic tests.
Project secret references select Actions secrets; CLI callers supply the three
standard environment variables directly. Run workflows from trusted bridge refs.

Tests cover independent ancestry and latest-head bootstrap, equal/filtered-empty
bootstrap, create/update/delete, manual branch edits/deletion, changed filters,
excluded files, old-import integration, project isolation, binary/mode/symlink and
filename semantics, compare-and-swap races, history rewrites, sanitizer failures,
and recovery after destination publication. Live deployment fixtures and run
evidence belong outside this reusable folder.
