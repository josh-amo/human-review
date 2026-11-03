---
name: review-guide
description: Build a browsable reading guide for a human reviewing a GitHub PR, branch, or working tree, with inline annotated diffs, background explanations, full file diffs, saved progress, and VS Code navigation. Use for understanding changes and conducting a personal review; automated findings and approval judgments are outside this workflow.
---

# Human Review

Produce a local browsable review document, organized by behavior and design decisions rather than file order. The human already has AI code review; do not add findings, severity labels, approval recommendations, risk checklists, or speculative defects.

The plugin root is two directories above this skill. Use its `scripts/review.py` and bundled viewer rather than regenerating a UI. It requires Python 3, Git, and (for PRs) authenticated `gh`; no pip or npm packages are needed.

## Capture the comparison

Run from the repository under review, using absolute paths for the script and output:

```sh
python3 <plugin>/scripts/review.py capture --repo <repo> --pr <PR-URL>
python3 <plugin>/scripts/review.py capture --repo <repo> --branch <branch> --base <intended-base>
python3 <plugin>/scripts/review.py capture --repo <repo> --mode all
```

Working-tree modes: `all` compares HEAD to current tracked files plus untracked files; `staged` compares HEAD to the index; `unstaged` compares the index to current files plus untracked files. Ignored files are excluded. Tell the user which mode is used; default to `all` for an unspecified working tree. For branches choose the intended parent, including stacked parents; ask only when it cannot be established. The PR mode uses GitHub's base and head, compares from their merge base, and rejects a PR that moves during capture.

The printed directory contains `snapshot.json` and exact source files. Defaults live under `~/.codex/human-review/<stable-review-id>`; `--output <directory>` overrides this. Reusing the directory retains progress and notes across regeneration. Capture reads Git and may fetch missing commit objects; it never checks out, commits, pushes, or modifies tracked files. If authentication fails, stop and ask the user to fix it. Obtain necessary sandbox access without switching authentication mechanisms.

## Write the reading guide

Read `snapshot.json`, all changed hunks, the PR description, and enough surrounding source to explain the change accurately. Read repository instructions scoped to inspected code. Distinguish author-stated purpose from inferred behavior, and identify material mismatches neutrally rather than treating the description as proof. Do not claim tests passed unless their run is independently verified; attributed PR testing claims are fine.

Use the snapshot's exact revision for background. To include an unchanged file in a PR or branch guide:

```sh
python3 <plugin>/scripts/review.py context <review-directory> <repo-relative-path>
```

This captures the file at the pinned head without checking anything out. For working-tree reviews, background source links are limited to captured files. Never cite the current checkout as the reviewed source when they differ.

Write `guide.json` in the review directory using [the guide format](references/guide-format.md). Match its `snapshot` to `snapshot.json`'s `fingerprint`.

- Start with purpose, a concrete before/after, source attribution, and a map of changes. Organize subsequent sections around coherent behavior changes. Use stable semantic section ids so reading notes survive regeneration.
- Explain substantive algorithms, ownership, persistence, lifecycle and failure behavior as needed to understand the code. Use flow blocks for useful data flows; avoid decorative diagrams.
- Place short inline explanations at the exact changed lines they explain. Diff and source blocks reference captured lines; never rewrite or fabricate an excerpt. Include enough context for the human to judge the code.
- Put unfamiliar existing design behind a background disclosure next to the relevant change. Source references should point to the code supporting the explanation, including unchanged dependencies when needed.
- Group mechanical wiring, generated files, routine build edits, and most tests into a supporting section. Inspect them first: promote a consequential contract, permission, configuration, schema, or lifecycle change into the main narrative regardless of file type. Every changed file must appear in a section's `files` list. A file may support more than one concept, but avoid repeating it in the file index unnecessarily.

## Validate and present

```sh
python3 <plugin>/scripts/review.py validate <review-directory>
python3 <plugin>/scripts/review.py serve <review-directory>
```

Keep the server running. For a PR, starting the viewer creates or reuses its exact local checkout as described below; obtain sandbox access for that operation when needed. It binds only to loopback and prints its URL. Open that URL with the available browser tools, verify a section jump, a checkbox, a background disclosure, a full file diff, and editor target selection. Verify persistence by reloading. Mark the browser tab as a deliverable if the tool supports it. Share the actual clickable URL, not a screenshot pretending to be interactive. No product build is needed for preparing this reading guide.

The viewer saves personal progress, notes, expanded sections, and reading position to `state.json`. After regenerating a guide, sections with changed content or mapped files require rereading; notes remain. Full diffs retain every captured textual hunk, including generated and supporting changes. Binary and metadata-only changes remain listed with their available Git diff metadata. Source and diff line numbers link to VS Code. Each editor target is verified against the checkout at click time; a mismatch opens the exact base/head snapshot with an explicit label.

## Questions and review comments

The real viewer has hover `+` actions on diff rows. **Ask agent** is available for PRs, branches, and every working-tree mode; section headers, explanation blocks, and inline annotations also offer questions. The composer previews the exact selected source and nearby explanation. Questions are queued to the launching Codex chat using `codex queue`; they are not answered in a separate hidden session. A busy chat processes the message when ready.

The server binds to `CODEX_THREAD_ID` (or `CODEX_SESSION_ID`) at launch. If launching outside a Codex chat, use `serve <review-directory> --thread-id <existing-chat-UUID>`. Use only the intended existing chat, never guess another destination or create a new chat automatically. When no destination is bound, the viewer explains how to relaunch rather than pretending a message was sent.

PR reviews additionally offer **Add PR comment**. Comments and the optional review summary are local drafts in `review-actions.json`; refreshing or restarting retains them. Shift-click another `+` on the same file and side to select a range. GitHub comments must fit one captured diff hunk. Removed lines are attached to the left/base side, and added/context lines to the right/head side.

Only the human's separate **Submit review** confirmation publishes the drafts as one GitHub COMMENT review. Do not submit test comments to a real PR or publish the human's drafts as part of preparing a guide. A changed or closed PR blocks submission without discarding drafts. Uncertain network results are reconciled against GitHub before any further action; the viewer does not blindly retry the POST. Authentication failures are surfaced without alternative credentials or workarounds.

## PR checkouts and cleanup

PR viewers reuse a clean existing worktree at the exact captured head commit, or create a detached worktree inside the review directory. A matching branch name is insufficient. Dirty, locked, missing, or differently pinned worktrees are left alone. Worktrees owned by another review are not borrowed, so one review cannot remove another's code checkout. Local branch and working-tree reviews use the existing checkout without creating another worktree. Current PR code opens in the full worktree; base/deleted lines use captured base source.

The registry `worktrees.json` records ownership, revision and last-opened time; owned worktrees also have a matching marker in their Git administrative directory. A running PR viewer holds a session lease. A crashed or stopped viewer releases it automatically. Refreshing the PR is explicit: recapture and regenerate the guide, then restart the viewer; never silently move an existing review checkout. Older revisions remain registered for later cleanup.

The viewer provides **Finish review & clean up**, **Reopen code checkout**, and **Clean up unused reviews**. Finishing releases this viewer's lease, then removes only eligible plugin-owned checkouts. Other active viewers still block removal. Guide, notes, progress and captured source survive cleanup; reopening recreates a checkout. The unused-reviews action previews checkouts not opened for 14 days and needs explicit confirmation. It never runs automatically and never relies on a PR being merged or closed as permission to delete.

CLI equivalents:

```sh
python3 <plugin>/scripts/review.py checkout <review-directory>
python3 <plugin>/scripts/review.py cleanup --review <review-directory>
python3 <plugin>/scripts/review.py cleanup --review <review-directory> --apply
python3 <plugin>/scripts/review.py cleanup
python3 <plugin>/scripts/review.py cleanup --apply
```

Cleanup is a preview unless `--apply` is supplied. Run apply only when the user requests that cleanup; save and close any VS Code windows for the checkout first. Never remove reused user worktrees. Refuse removal when ownership is uncertain, HEAD or its history changed, a branch was attached, files are edited/untracked/ignored, registration is locked, or another review session is active. Do not bypass these checks with `--force`, manual directory deletion, or a global Git prune. For Bazel repositories, run `bazel clean --expunge --async` from that checkout before `git worktree remove`; a failed Bazel cleanup must keep the checkout. Read and honor any additional repository teardown instructions before removing a real checkout; the script's Bazel handling does not substitute for those instructions.
