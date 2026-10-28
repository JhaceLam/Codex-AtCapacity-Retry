# Codex-AtCapacity-Retry

[简体中文](README.zh-CN.md)

Immediately retry terminal **“Selected model is at capacity”** failures in Codex App conversations. No added delay. No attempt limit. Keep the same model and conversation; stop when the attempt succeeds or you press Stop.

This is a community-maintained transport wrapper, independent of the App bundle. It supports the local macOS App backend and optional Linux SSH backends, using Python's standard library.

## Install

Requirements: Python **3.9+**, macOS for local App integration, or Linux for an SSH backend. Windows is not supported. Install and sign in to Codex first. The tool uses that existing installation and authentication.

```sh
git clone https://github.com/JhaceLam/Codex-AtCapacity-Retry.git
cd Codex-AtCapacity-Retry
python3 patch_manager.py apply
```

Completely quit the App and reopen it once so its new connections go through the wrapper. Subsequent maintenance does not restart the App, backend, or running tasks.

If the App or CLI is in a different location:

```sh
python3 patch_manager.py apply --app /path/to/Codex.app
# Or explicitly select the original, unwrapped CLI:
python3 patch_manager.py apply --codex /path/to/original/codex
```

The installer discovers common `Codex.app` / `ChatGPT.app` bundle layouts. A nonstandard Linux installation may need `--codex`.

### Optional SSH backends

From your Mac, specify your own configured SSH aliases:

```sh
python3 patch_manager.py apply --host my-devbox --host my-other-server
```

These aliases are saved locally, outside the repository. No remote host is configured by default. SSH must already work noninteractively, and each selected host must have Python 3.9+ and a working Codex CLI. This also installs the patch on the local Mac.

On Linux, running `apply` directly installs the backend wrapper only. macOS launchd performs automatic maintenance for hosts configured from the Mac. A standalone Linux installation can run `ensure` from its existing update workflow.

## Manage

After installation, add `~/.local/bin` to your shell's PATH if necessary.

```sh
codex-capacity-patch status --all
codex-capacity-patch apply --all
codex-capacity-patch rollback --all
```

`--all` includes only the SSH aliases you previously configured. Without it, the command operates on the current machine. You can also use `--host SSH_ALIAS` to target specific remote hosts.

`apply` is repeatable. `ensure` maintains an already-enabled patch; it does not enable a patch after rollback. Reopening the App after rollback ends the existing wrapper connections. Source files, backups, and rollback records are retained.

## What it does

```text
Codex App  →  retry wrapper  →  original Codex backend
                    │
       terminal capacity failure
                    └─ immediately send an empty continuation
                       to the same thread, without switching models
```

- Observes `turn/completed` with `status: failed` and `serverOverloaded`, or the exact selected-model-capacity message.
- Sends `turn/start` with empty input, preserving the thread's sticky model, permissions, directory, and mode.
- Uses **no sleep, timer, backoff, or maximum attempt count in the capacity retry path**.
- Stops retrying after success, a different error, or the App's Stop action. Stop also handles a retry that has just started.
- Leaves ordinary CLI commands and noncapacity errors alone. Credentials, prompts, and model output are not recorded in its operational log.

The wrapper acts after the backend reports a **terminal** failure. It does not alter the backend's own internal recovery before that event, network latency, or server availability.

## App and CLI upgrades

The patch lives outside the App bundle. Releases are installed under:

```text
~/.local/lib/codex-capacity-retry/releases/<version>/
```

A `current` symlink selects the installed version. Existing internal paths and the `codex-capacity-patch` command are retained for upgrade compatibility.

On macOS, a launchd job restores `CODEX_CLI_PATH` at login, watches App/entry changes, and checks the local installation and configured SSH backends every 60 seconds. **That maintenance interval does not delay capacity retries.**

When an installer overwrites the remote CLI entry, maintenance saves the new real CLI and restores the wrapper. Rollback preserves the CLI upgrade. A changed CLI version or file identity is checked against the relevant app-server protocol schemas before reapplying the entry.

An already-open connection that bypassed the wrapper during an upgrade needs its next reconnect to use the restored entry. Unknown future protocol changes or unrecognized custom launchers can require a newer patch; failed checks are reported rather than silently marked healthy.

## Source patches and release archives

[Releases](https://github.com/JhaceLam/Codex-AtCapacity-Retry/releases) include:

- `Codex-AtCapacity-Retry-<version>.tar.gz`: standalone installation bundle.
- `Codex-AtCapacity-Retry-<version>.patch`: complete source patch, expandable with `git apply` in an empty directory.
- `SHA256SUMS`: archive and patch checksums.

Each bundle includes a manifest with the version and SHA-256 hashes of its payload. Installation verifies them before applying files.

## Tests and build

```sh
python3 -m unittest -v
python3 make_bundle.py
```

Tests use simulated backends, not model requests. They cover 10,000 consecutive capacity failures, actual stdio/WebSocket subprocesses, stopping during retry creation, frame fragmentation, duplicate events, other errors, repeated installation, upgrade restoration, checksum failures, protocol incompatibility, and rollback.

## Logs

| Location | Purpose |
| --- | --- |
| macOS: `~/Library/Logs/codex-capacity-retry/events.jsonl` | Connection, retry and stop events |
| Linux: `~/.codex/log/capacity-immediate-retry.jsonl` | Connection, retry and stop events |
| macOS: `~/Library/Logs/codex-capacity-retry/patch-maintenance.jsonl` | Upgrade maintenance results |

Logs are bounded and contain event metadata, IDs and attempt counts, not prompts, output or authentication tokens.

## License

[MIT](LICENSE).
