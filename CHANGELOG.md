# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/), versions follow
[Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-10-05

### Added
- `list` subcommand: table of extension / IP / model / firmware parsed from
  `User-Agent`; `--json` for scripts.
- Filters `--ext 4001,4010-4020`, `--subnet 10.1.0.0/16`, `--model T33G` for
  `collect`, `list`, `reboot`, `autop`, `provision`.
- `--dry-run` for bulk operations: show targets, send nothing.
- Confirmation prompt before `reboot`, `autop`, `provision`; `--yes` to skip.
- Waves: `--batch N --delay SEC` to avoid a re-registration storm.
- `--failed-file` to save failed IPs/extensions for a re-run.
- `--allow-http-fallback` (opt-in HTTPS → HTTP retry on connection error only).
- `--version`, `pyproject.toml` (`pipx install git+https://…` installs the
  `yealink-manager` command), pytest suite, GitHub Actions CI with ruff.
- README language switcher (English / Русский).

### Changed
- **Breaking:** bulk operations without a terminal (cron) now require `--yes`.
- **Breaking:** no default password; use `-p`, `YEALINK_PASSWORD` or the
  interactive prompt.
- **Breaking:** no silent HTTPS → HTTP fallback.
- `reboot`, `autop`, `provision` exit with code `1` if any device failed.
- AstDB status messages go to stderr (keeps `list --json` output clean).

### Fixed
- `provision` reported every NOTIFY as failed: success is now detected by
  the actual Asterisk output `Sending NOTIFY ...`.
- Several contacts on one endpoint (`max_contacts > 1`): only the last one
  was kept.
- Endpoint name is taken from the contact's `endpoint` field (AOR as fallback).
- `test` ignored `-t`; `-w -1` crashed with a traceback.
- README: real clone URL, cron example, Asterisk notify error text and
  reload command, `check-sync` reboot warning.

## [0.1.0] — 2026-10-05

Initial release: `collect`, `test`, `reboot`, `autop`, `provision`.

[0.2.0]: https://github.com/detroitwtf/yealink-park-manager/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/detroitwtf/yealink-park-manager/releases/tag/v0.1.0
