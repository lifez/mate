# Configuring Mate

Mate reads **`mate.config.json` from the Mate installation directory**, not from a
worker repository and not from `$MATE_HOME`. Each installation has its own config.
The real file is local-only and Git-ignored. Share `mate.config.example.json` instead.

## First-time setup

From the Mate repository root:

```sh
# Create only if missing; never overwrite your existing config.
test -e mate.config.json || cp mate.config.example.json mate.config.json
chmod 600 mate.config.json
```

Edit the copied file before starting the supervisor. The example supplies worker
model/effort defaults and an empty `projects` object, so no startup commands run.
Choose a model available in your Pi `/model` picker and an effort it supports.

JSON must not contain comments or trailing commas. You can check JSON syntax with:

```sh
python3 -m json.tool mate.config.json >/dev/null
```

This checks syntax only; Mate validates settings when they are used. It does not
fetch branches or execute startup merely because you copied/edited the config.
A missing, unreadable or invalid config fails new proposal/dispatch operations;
Mate does **not** silently copy the example or fall back to it at runtime.
Existing task continuation retains its saved settings and does not rerun setup.

### Updating an older installation

Older versions tracked `mate.config.json`. **Back up your copy outside the checkout
before pulling the change that removes it from Git**, then restore it afterward.
Git may remove an unchanged tracked copy or refuse an update when it has local edits.
Do not force the update over your local settings. For new installations, copy the
example as above. Git history is unchanged; untracking does not erase older commits.

## Worker defaults

```json
{
  "worker": {
    "model": "openai-codex/gpt-5.6-luna",
    "effort": "xhigh",
    "max_active": 2
  },
  "projects": {}
}
```

- `worker` is optional. Omit it or use `{}` to inherit supervisor settings.
- `model`: exact model ID, preferably `provider/model-id`. A bare ID uses the
  supervisor's provider on initial dispatch. Model/provider must be available to Pi;
  changing providers may require separate authentication or billing.
- `effort`: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`.
  The selected model must support an explicitly configured level.
- `max_active`: positive integer limiting concurrent acquiring, launching and running
  workers. Defaults to `2`. It applies to new dispatches and continuations as soon as
  the file is saved; it does not resize the Treehouse pool or provider quota.
- Initial dispatch precedence per model/effort field: **task override → worker config → supervisor**.
- Continuation keeps the task's saved profile unless explicitly overridden. Config
  edits do not change the supervisor model or existing task profiles.

Use Pi authentication (`/login`); never put API keys in this file.

## Project settings

Merge project entries into `projects` alongside your existing `worker` settings.
This example uses placeholders; replace all paths and the branch before use:

```json
{
  "projects": {
    "my-project": {
      "repo": "/absolute/path/to/my-project",
      "base_branch": "origin/migration",
      "startup": {
        "command": [
          "python3",
          "/absolute/path/to/mate/examples/copy-env.py",
          "/absolute/path/to/source.env"
        ],
        "timeout_seconds": 30
      }
    }
  }
}
```

### `repo` (required for every project entry)

The project key (`my-project`) is a label. Mate selects the entry by exact resolved
repository path, not by the label or directory basename.

Use an absolute local Git repository root, including `~/...` if desired. Symlinks
are resolved. Relative paths, URLs, globs, subdirectories and duplicate resolved
repo mappings are not supported. Point at the source checkout, not the task worktree.

### `base_branch` (optional)

A **required base policy**, not a suggestion the model can override. With this set,
`mate_propose` may omit `base`; a conflicting explicit base is rejected. Without it,
the task must supply an explicit base ref.

Use a short local branch (`migration`) or remote-tracking branch (`origin/migration`).
Do not use `refs/...`, a tag, SHA, revision expression, or symbolic ref like
`origin/HEAD`. The branch must exist locally and be unambiguous. Mate never fetches.

Proposal pins the branch's current local SHA; **human approval is still required**.
If the branch moves afterward, the approved SHA remains unchanged. Changing the
configured base policy before dispatch requires a new proposal/approval. The worker
uses its own `mate/...` branch; Mate does not write to, merge into or rebase onto the
configured branch automatically.

### `startup` (optional)

- `command` (required): nonempty argv array, up to 128 nonempty string arguments
  of at most 4096 characters each. No implicit shell, variable or glob expansion.
  Executable names may resolve via `PATH`; use absolute script paths for clarity.
  For shell scripts use `["/bin/sh", "/absolute/path/to/setup.sh"]`.
- `timeout_seconds` (optional): integer **1–120**, default **30**.
- Working directory: the task's leased worktree, already on its approved task branch.
- Environment: inherited plus `MATE_REPO`, `MATE_WORKTREE`, and `MATE_TASK_ID`.
  stdin is closed; scripts cannot prompt for interactive input.

Setup runs once during initial dispatch, including when Treehouse supplies a reused
pooled worktree. It must finish successfully before a Herdr tab/worker is opened.
It must preserve repository identity, task branch and approved HEAD. It can create
setup files, but must not launch background services.

Duplicate dispatch and `mate_continue` never rerun setup. Startup config is read and
saved on initial dispatch, not on proposal. External scripts are not content-pinned;
editing their contents affects future initial dispatches.

Failure, timeout or uncertain crash blocks launch and leaves the task in `attention`
(after recovery reconciliation for a hard crash). Inspect before intervening; no
automatic retry, rollback, worktree reset or lease return occurs. Timeout/error kills
only the owned process group; detached processes or a hard-killed supervisor can
leave descendants running. This is trusted local execution, **not a sandbox**.

stdout/stderr are retained in private `$MATE_HOME/<task-id>/startup.log`, not sent to
the model automatically. Task status includes saved startup settings and execution
metadata. Never put secrets in command arguments or print them in logs.

## Copying environment files

[`examples/copy-env.py`](examples/copy-env.py) copies its source argument into
`.env.local` in the worktree with mode `0600`. Add `.env.local` to the project's
`.gitignore` **before approving the base**. The example refuses unignored or existing
destinations, including symlinks; it does not silently overwrite pooled env files.
Adapt your own trusted script for another destination or an explicit overwrite policy.
Never commit the env source/destination, credentials or startup logs.

## When edits take effect

- Required branch policy: new proposals, checked again before initial dispatch.
- Worker defaults and startup: initial dispatch reads current config.
- Active-worker limit: every dispatch/continuation reads current config.
- Continuation: saved worktree/base/profile; no startup replay.
- Config-only edits: no supervisor restart needed.
- Runtime updates: reload/restart the supervisor with workers stopped.

See [README.md](README.md) for human approval, dispatch, continuation, recovery and
verification commands. Automated tests use disposable config, not your personal file.
