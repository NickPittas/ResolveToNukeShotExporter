# Repository Guidelines

## Project Structure & Module Organization

- `ResolveToNukeShotExporter.py` is the single distributable Resolve Workflow
  Integration script. Keep Resolve-independent planning, validation, and Nuke
  writing logic separate from Resolve/Fusion API calls so it remains testable
  outside Resolve.
- `tests/test_core.py` contains the unit tests for that pure core.
- `install_macos.command` and `install_windows.ps1` install the script into
  Resolve's system-wide Workflow Integration directory.
- `README.md` is the user-facing installation and workflow reference; update it
  when behavior, options, or supported platforms change.

## Build, Test, and Development Commands

No package build or third-party dependencies are required. Run the full test
suite from the repository root:

```sh
python3 -m unittest discover -s tests -v
```

This imports the script directly and tests it without DaVinci Resolve or Nuke.
For a manual integration check, use the appropriate installer, restart Resolve,
and open **Workspace → Workflow Integrations**.

## Coding Style & Naming Conventions

Target the Python version bundled with supported DaVinci Resolve releases and
use only the standard library unless a dependency is explicitly approved. Use
four-space indentation, type hints for new public/core functions, and concise
docstrings for non-obvious behavior. Follow existing names: `PascalCase` for
classes and dataclasses (`ExportSettings`), `snake_case` for functions and
variables (`build_plan`), and `UPPER_SNAKE_CASE` for module constants.

Preserve cross-platform behavior: use `pathlib.Path` for filesystem work and
forward slashes only in generated `.nk` paths. Treat `ExportError` as the
user-facing error type for invalid settings or preflight failures.

## Testing Guidelines

Add or update `unittest` cases in `tests/test_core.py` with names beginning
`test_`. Cover normal behavior and failure cases, especially token expansion,
path collisions, frame ranges, timecode, and generated Nuke output. Use
`tempfile.TemporaryDirectory()` for filesystem tests; do not require Resolve,
Nuke, media files, or user-specific paths.

## Commit & Pull Request Guidelines

This checkout has no available Git history to establish a local convention. Use
short, imperative Conventional Commit-style subjects, for example
`fix: clamp plate handles at source bounds`. Keep commits focused. Pull requests
should explain the Resolve/Nuke-facing effect, list the test command and result,
link relevant issues, and include screenshots for UI changes. Update `README.md`
when installation or user-visible workflow changes.
