# Sanitizer Bridge — standalone package

Use this package for **multiple projects coordinated by a separate GitHub repository**.
The companion embedded package manages exactly one pairing inside its internal repository.
Both contain the same engine. The bridge stores configuration, workflows and synchronization
metadata; project history stays in the internal and customer repositories.

## Install

1. Copy this folder, including `.github`, into the bridge repository's main branch.
2. Copy `projects/example/` to `projects/<project>/`. Set repository names, branches,
   the exact internal bootstrap commit and sanitizer path, then set `enabled: true`.
   Projects have independent state and generated branches. Multiple projects can share a sanitizer.
3. Add the access-token secrets named in the project's `secrets` mapping. Tokens need
   Contents read/write on their respective repositories, including temporary staging branches.
   Workflows write may also be needed when publishing history containing workflow files.
   You may use one token or separate tokens. Tokens used for workflow dispatch need Actions write.
4. Install `templates/internal-export.yml` as `.github/workflows/bridge-export.yml` in
   the **internal** repository. Set bridge repository, ref and project; add its
   `BRIDGE_DISPATCH_TOKEN` secret. Its manual action requests export of the latest
   configured internal branch head. Check the corresponding bridge run for completion.
5. Optionally install `templates/customer-notify.yml` as `.github/workflows/bridge-notify.yml`
   in the **customer** repository. Configure the bridge/project and customer push branch;
   add `BRIDGE_DISPATCH_TOKEN` there. No customer-provided SHA is trusted.
6. Enable polling, notification, or both in configuration. The bridge polls at minutes
   17 and 47 each hour. A manual `poll` with an empty project selects all polling projects.
   Notification and export require a specific project.

The bundled sanitizer excludes `.github`, `.sanitizer-bridge`, `private`, and `customer-private`. Extend it
for your project. Retained file bytes and Git modes must remain unchanged. Sanitizers
are trusted bridge-owned scripts; customer scripts are never executed.

## State and recovery

State is on `bridge-state-v2/<project>`. Its ancestry contains only `state.json` commits,
with no source-history parents. It records the export checkpoint pair, last observed
customer revision, generated internal tip, expected branch, branch sequence, suspension,
and pending publication/cleanup. Commit hashes in JSON do not retain project objects.

Customer history loss suspends imports until a successful explicit export. The export
preserves the customer's current snapshot on an internal import branch before replacing
included customer files. Missing internal commits cause a clear failure: retaining those
commits is your team's responsibility. See [OPERATIONS.md](OPERATIONS.md) for details,
including interruption recovery and temporary staging references.

An empty customer commit, or changes only to excluded files, creates **no import commit**.
The bridge records the observation in metadata. Repeated identical notifications are no-ops.
There are no PRs, acceptance/rejection tracking, merge detection, or export blockers based
on outstanding imports.

## Upgrade from the archive-based version

The v2 engine detects `bridge-state/<project>` and requires an explicit migration rather
than silently bootstrapping again. Finish any pending v1 operation using v1 first. Deploy
v2, then run the bridge workflow with `operation: migrate` and the project name, or:

```sh
python -m bridge migrate --project my-project
```

Migration writes a new, independent metadata-only root on `bridge-state-v2/<project>`
and preserves the checkpoint and generated-chain mapping. It does not delete or rewrite
legacy archive branches. Those branches are no longer used by v2; removing old archives
is a separate repository-maintenance decision. Migrating does not itself purge previously
uploaded history from a Git host.

## Run and verify

Requires Linux, Python 3.12+, Git and Bash. Install `requirements.txt` in your environment.
For CLI use, supply `BRIDGE_TOKEN`, `INTERNAL_TOKEN`, `CUSTOMER_TOKEN` and
`BRIDGE_URL=https://github.com/owner/bridge.git` as process-level environment variables:

```sh
python -m bridge import --project my-project
python -m bridge export --project my-project
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

The `Verify bridge` workflow runs the same integration suite with the pinned dependency.
The CLI uses temporary object databases and materializations, and does not modify your
credential store or user Git configuration. `--local` permits local synthetic repositories
for tests. See [OPERATIONS.md](OPERATIONS.md) for the complete shared contract and limits.
