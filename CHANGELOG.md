# Changelog

## 1.1.1

- Keep retrying immediately when a continuation is temporarily rejected with `EmptyInput`.
- Adopt an already-started turn when multiple clients race to retry the same failure.
- Preserve Stop and manual-message handling while a retry request is in flight.
- Log accepted retries and fixed rejection categories without recording RPC error text.
- Add an optional offline integration test against an installed real CLI.

## 1.1.0

- Publish as **Codex-AtCapacity-Retry** with English and Chinese documentation.
- Configure SSH targets explicitly instead of embedding machine-specific aliases.
- Support explicit App and original CLI paths.
- Provide reproducible release archives, source patches, checksums and upgrade maintenance.
- Test macOS and Linux across supported Python versions in GitHub Actions.

## 1.0.0

- Immediate, unlimited continuation after terminal model-capacity failures.
- stdio and WebSocket transports, Stop handling, versioned installation and rollback.
