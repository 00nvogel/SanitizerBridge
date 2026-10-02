# Synchronization and recovery contract

This contract applies to the standalone and embedded packages. Standalone installations
select a project; embedded installations always use their single configured pairing.

## Bootstrap, import and export

The internal bootstrap anchor is an exact configured commit. Customer main is captured
automatically. Equal included trees establish the relationship without an import commit.
A populated differing customer creates an internal import based on that anchor. An empty
or filtered-empty customer waits for explicit export; polling never writes outbound.
Export captures the latest configured internal head at run start, even when newer than
the bootstrap anchor. Internal and customer histories remain independent.

Each import continues from the bridge's last generated internal commit, with the first
based on the current internal epoch base. Included customer files are added, updated, or
deleted; excluded destination files remain untouched. This comparison uses the current
customer projection and the bridge-generated tree, so newly included existing files
participate without requiring a new customer commit or storing sanitizer versions.
Newly excluded paths remain untouched. Manual changes to internal main do not change the
pinned epoch until export.

Only changes to included files create import commits. Empty customer commits and changes
limited to excluded files update observation metadata without creating an import commit.
They also do not allocate another import branch unless the previous published branch was
moved/deleted and needs its existing bridge-owned commit republished. That uses the same
commit, not an empty replacement commit. In embedded mode metadata commits live on the
separate state branch, never on internal main or the import chain.

Generated branches are `bridge/<project>/import-N` (standalone) or
`sanitizer-bridge/import-N` (embedded). If a published branch moves or disappears, its
contents are left alone and a fresh name continues from the recorded generated commit,
provided your internal repository still retains that commit. Exact expected-head writes
protect against races at the publication boundary.

Explicit export first preserves outstanding customer changes on an internal import
branch. It then applies the current internal included projection to the current customer
tree. Included unmerged customer changes may be removed; excluded customer files survive.
The customer commit has only the observed customer head as parent and a generic message,
so internal ancestry is not exported. A no-content export can advance the checkpoint
without an empty customer commit. Successful export starts a fresh import epoch; old
import branches remain available for manual integration or later squash/cherry-pick.
There is no PR machinery or requirement to integrate imports before exporting.

## History ownership

The bridge stores **metadata only**. Its state commits have only the previous metadata
commit as parent; the initial state commit is independent. Project commit identifiers
appear as JSON strings, not Git parent references. The runner fetches project objects
into a temporary database while processing a sync and removes that database on exit.

The internal team is responsible for retaining the pinned internal base, generated
commits, and old branches it may want to integrate. An ordinary manual append retains
the earlier commit. Replacing/deleting the last reference may eventually make it
unavailable. A missing required internal commit fails with its SHA; the bridge never
substitutes the current main or user-modified branch. Restore the commit on an internal
branch/tag, then retry. Authentication/network failures also fail clearly; they do not
silently reset state.

If customer history is rewritten/deleted or the current history no longer contains a
required checkpoint/observed revision, automatic imports become suspended. The reason
is persisted and reported by subsequent polls. Merely restoring customer ancestry does
not clear suspension: the next **successful explicit export** does. Already journaled
operations are recovered before processing new observations.

That recovery export preserves the customer's current included snapshot on a usable
internal branch before replacing it. It does not need the missing customer history, but
it does need the internal base/generated commit. Customer revisions already lost cannot
be reconstructed. Failed publication or checkpoint recording does not clear suspension.
If checkpoint recording succeeds but staging cleanup fails, export is already complete;
the reported error asks for cleanup retry. A successful no-content export also establishes
the new checkpoint and resumes imports.

To deliberately reinitialize instead of restoring missing internal history, stop the
project's jobs, ensure there is no unfinished publication, retain any needed branches,
and explicitly archive/remove its active metadata branch. Update the bootstrap anchor
and then request export. This starts a new relationship; it is an operator decision,
never an automatic error fallback. In standalone mode a new project identifier also
creates a separate relationship. Existing numbered branch names are never overwritten.

## Recoverable publication

1. Create the destination commit locally and push it to a unique
   `sanitizer-bridge-stage/<project-or-embedded>/<operation-id>` branch **in its destination
   repository**. This temporarily retains the exact prepared commit and its ancestry.
2. Save a pending operation in metadata, including the staging branch, source SHA,
   prepared commit, destination branch, expected head, and next checkpoint/state.
3. Publish with an exact expected-head comparison. If a user moved an internal import
   branch, allocate a new name. Customer races trigger recapture and snapshot preservation
   before a bounded export retry; no competing customer update is force-overwritten.
4. Save completion plus a cleanup record in metadata. Delete only that exact staging
   reference with an expected-head check, then clear the cleanup record.

A new runner fetches the prepared commit from the destination staging reference. If
publication already succeeded, it recognizes that commit (including a destination tip
that descends from it) and finalizes metadata without duplicating the change. Cleanup
failure is retryable without republishing. Do not alter staging branches for pending
operations. Missing/changed staging references fail clearly rather than guessing.

A crash or rejected metadata write between steps 1 and 2 can leave an orphan staging
branch, but cannot publish an unjournaled update to the user-facing branch. With all jobs
for that project stopped, an operator can remove orphan staging references only after
checking they are absent from current `pending` and `cleanup` metadata. The bridge does
not indiscriminately delete staging branches, which could belong to another active run.
There are no permanent archive/retention refs created for completed operations.

Workflows serialize by project (one fixed group in embedded mode), queue up to 100
pending jobs, and never cancel the running job. Metadata writes are also compare-and-swap,
so independently invoked CLIs fail safely on contention and can be retried. Source SHA
capture stays fixed within an export even if internal main advances. Both repeated
notifications and export-generated customer notifications converge without echo commits.

## Sanitizers and supported Git files

The trusted script is run as `bash /absolute/script/path /temporary/tree`, with cwd set
to the script's own directory. Companion files can be referenced there. It may delete
whole files only; added files, rewritten bytes, changed executable modes/symlink targets,
unsupported entry types, and nonzero exit statuses fail the operation. Do not follow
symlinks supplied by repository trees. The script never runs on a live checkout.

Embedded mode always excludes `.sanitizer-bridge` and `.github` before invoking the
script, protecting installation files even with a permissive custom sanitizer. Standalone
mode uses the configured script's exclusions; the example protects `.github`. Consistent
pathname-based filtering is supported. Content-dependent rules that conflict about
whether a destination path is excluded fail rather than overwrite that path. A file/directory
collision involving a preserved excluded path also fails.

Binary files, executable bits, symlinks, unusual safe filenames, additions/deletions,
and renames represented as delete/add are supported. Git LFS pointer files and submodules
are explicitly rejected. Only GitHub remote hosting is implemented; local bare repositories
are supported for tests. Tokens/branch policies must permit main, import, staging and
metadata writes where appropriate. The bridge never broadens permissions or disables
branch protections. Customer notification templates only trigger for the configured
customer branch, not temporary staging branches.
