# AGENTS.md

Instructions for AI agents and contributors working on ghostty-logger.

## Project overview

ghostty-logger recreates Terminator's Logger plugin for the Ghostty terminal:
a zero-dependency pty wrapper that appends the terminal's rendered plain-text
output (no ANSI escape sequences) to a log file. Python 3.10+, standard
library only, Linux and macOS. Deliberately a single self-contained script so
it can be installed with one `install` command.

## Commands

```sh
# Run the full test suite
python3 -m unittest test_ghostty_logger -v

# Run the tool
python3 ghostty_logger.py -- printf 'hello\n'

# Install
install -m755 ghostty_logger.py ~/.local/bin/ghostty-logger
```

No build step, no dependencies, no configured linter. Do not add a packaging
or dependency toolchain without being asked.

## Project structure

```text
ghostty_logger.py        The entire application (see layers below)
test_ghostty_logger.py   unittest suite, stdlib only
examples/ghostty-config  Sample Ghostty keybind
README.md                User-facing docs (keep in sync, see Docs below)
```

### Internal layers (control flow)

The file is organized as a strict top-down hierarchy. Each layer may only
talk to the one directly below it; never punch holes (e.g. `main` must not
poke at `VTStripper` internals):

1. `main()` - CLI parsing, log path resolution, process exit code.
2. `run_session()` - pty lifecycle: `pty.fork()`, raw stdin, the `select`
   loop, escape-key scanning (`_scan_input`), child exit-code propagation.
3. `LogSession` - owns the log file and parser; start/stop/restart on toggle;
   `_guard()` contains parser faults so they degrade logging instead of
   killing the user's shell.
4. `VTStripper` - incremental VT parser that consumes untrusted terminal
   output and emits rendered plain text via a callback.
5. Helpers - `_open_log` (secure log file creation), `_winsize`,
   `_parse_escape_key`, `_timestamp`, etc.

## Code style

General rules (apply to all new and modified code):

- Avoid magic numbers and strings: extract recurring or meaningful values
  into descriptive constants (see `MAX_COLS`, `_MAX_CSI_PARAMS`,
  `_LOG_FILE_MODE`). Keep self-explanatory one-off values inline. Values from
  a spec (e.g. Ghostty's `MAX_PARAMS = 24`) always get a named constant.
- Reduce indentation. Avoid the Arrow Anti-Pattern; use early return and
  `continue`.
- Keep function names short: under 30 characters.
- Use enums instead of booleans for new function parameters.
- Members are private (single leading underscore) by default. Treat
  visibility changes as a breaking design shift: prompt the user for explicit
  approval before exposing anything that is currently private.
- Program to levels of abstraction. Low-level mechanics (pty ioctls, raw
  byte scanning, VT state machine) stay encapsulated behind clean high-level
  APIs; callers work with domain concepts, not raw implementation details.
- Don't touch code unrelated to the feature at hand. Minimize the number of
  changed lines; no drive-by comments or reformatting.

Python-specific patterns:

- Concise: minimize lines, avoid boilerplate.
- OOP preferred: classes with clear, single responsibilities
  (`VTStripper`, `LogSession`). Functional style (comprehensions,
  `map`/`filter`) where it is simpler, e.g. pure transforms.
- Type hints on all function signatures. Use `T | None` for optionals.
- Stdlib only: no pydantic here; use `@dataclass` for internal structured
  data if the need arises, and `os.getenv` for config.
- Raise errors with context: `raise XError("context") from e`.
- Manage resources with context managers (`with`).
- Minimal logging: surface only meaningful events. Parser anomalies are
  recorded in the log itself as `--- ghostty-logger: ... ---` markers, not
  via a logging framework.

Checklist before finishing Python work: signatures typed? classes single
responsibility? comprehensions where clearer? errors carry context? logging
minimal and meaningful?

## Testing

- Framework: stdlib `unittest`. Run with
  `python3 -m unittest test_ghostty_logger -v`.
- Unit tests drive `VTStripper` through the `strip()` helper; end-to-end
  tests spawn the script via `subprocess.run` with a temp log directory.
- Bug fixes: write the failing test first, observe it fail, then write the
  fix and observe it pass.
- Security-sensitive behavior has dedicated test classes
  (`UntrustedSequenceLimitsTest`, `LogFilePermissionsTest`,
  `LogPathPlantingTest`, `ParserFaultIsContainedTest`). Changes to parser
  bounds, file permissions, or fault handling must extend these.

## Gotchas

- The parser must match Ghostty's terminal semantics exactly: CAN/SUB abort
  a sequence from any state, ESC leaves a string sequence for the escape
  state, CSI parameter lists past `MAX_PARAMS` (24) drop the command,
  alternate-screen modes match numerically (`?01049h` == mode 1049). Any
  divergence makes the log disagree with what the user saw.
- `VTStripper` consumes untrusted input. Keep every attacker-controlled
  count bounded (`MAX_COLS`, `_MAX_CSI_PARAMS`, `_STRING_WARN_LEN`); an
  unbounded clamp is a memory-exhaustion DoS against the user's own session.
- Never let a parser exception escape `LogSession._guard`; it must degrade
  logging, not tear down the shell.
- Session logs hold everything the terminal displayed: generated files are
  `0600`, directories `0700`, and timestamped names are opened with
  `O_EXCL | O_NOFOLLOW` (the name is predictable; never write through a
  planted file or symlink).
- The exec'd child in `run_session` must never return to the caller; any
  failure path ends in `os._exit(127)`.
- Only terminal output is logged, never keystrokes. Preserve that property.

## Commits

- Conventional Commits: `feat`, `fix`, `docs`, `style`, `refactor`, `perf`,
  `test`, `build`, `ci`, `chore`. Optional scope in parentheses, e.g.
  `feat(parser):`. Breaking changes use `!` before the colon or a
  `BREAKING CHANGE:` footer.
- Subject line under 50 characters; body wrapped at 72 characters; footers
  (`Reviewed-by:`, `Refs:`) after a blank line.

## Docs

- Any update to AGENTS.md that is relevant to users must also be reflected
  in README.md.
