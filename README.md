# ghostty-logger

Recreates the **Logger** plugin from
[Terminator](https://github.com/gnome-terminator/terminator) for the
[Ghostty](https://ghostty.org) terminal emulator.

Terminator's Logger adds per-terminal *Start Logger / Stop Logger* menu items
that append the terminal's **rendered plain-text content** (no ANSI escape
sequences) to a log file. Ghostty has no plugin API, so `ghostty-logger`
provides the same behavior as a zero-dependency pty wrapper that works with
stock Ghostty:

- **Start logging:** run `ghostty-logger` (optionally bound to a Ghostty keybind).
- **Pause/resume logging:** press `Ctrl-\` to stop logging and keep working in
  the same shell; press it again to resume into a new log segment. Press
  `Ctrl-\` twice to send a literal `Ctrl-\` to the shell. Change or disable
  the key with `--escape-key` (e.g. `--escape-key ^]` or `--escape-key ''`).
- **Stop entirely:** `exit` or Ctrl-D ends the session. The log is flushed
  and closed, and the shell's exit code is propagated.
- **Log content:** exactly what the terminal displays, converted to plain
  text: colors, titles, and control sequences are stripped; carriage returns,
  backspaces, tabs, and basic cursor movements are resolved; alternate-screen
  (full-screen TUI) output is skipped by default.

Only terminal *output* is logged, never raw keystrokes. Secrets typed at a
prompt that disables echo (`sudo`, `ssh` passphrases) are therefore not
logged. Note this is a consequence of echo being off, not a filter: anything
a program *prints* is logged, including a secret it echoes back itself.

## Install

Requires Python 3.10+ (standard library only; Linux and macOS).

```sh
install -m755 ghostty_logger.py ~/.local/bin/ghostty-logger
```

## Usage

```sh
ghostty-logger                 # log to a timestamped file (see below)
ghostty-logger -o build.log    # log to a specific file
ghostty-logger --alt-screen    # also log full-screen TUI output
ghostty-logger -- make test    # log a single command instead of a shell
```

Default log location (first match wins):

1. `-o PATH`
2. `--dir DIR` (timestamped filename inside DIR)
3. `$GHOSTTY_LOGGER_DIR` (timestamped filename inside it)
4. `~/.local/state/ghostty/logs/ghostty-YYYYMMDD-HHMMSS-<pid>.log`

Each log starts with `--- log started <timestamp> ---` and ends with
`--- log ended <timestamp> ---`.

A log holds everything the terminal displayed, so logs are created `0600` and
the timestamped log directory `0700`, regardless of your umask. Because the
timestamped filename is predictable, it is created with `O_EXCL | O_NOFOLLOW`:
if a file or symlink is already sitting at that name the logger picks the next
free suffix rather than writing through it. An explicit `-o PATH` is treated
as your choice, symlink and all; only files the logger creates get `0600`, so
an existing file at that path keeps its current permissions.

With the default timestamped naming, each resume starts a fresh log file.
With `-o PATH`, resumed segments append to the same file, delimited by new
`--- log started/ended <timestamp> ---` markers.

## Ghostty keybind

Add to `~/.config/ghostty/config` (see `examples/ghostty-config`):

```
keybind = ctrl+shift+l=text:ghostty-logger\n
```

Ghostty's `text` binding types the command into the focused terminal, giving
a one-keystroke "Start Logger". Stop with Ctrl-\ (session continues) or
Ctrl-D (session ends).

## Recording indicator in your prompt

The logged shell gets `GHOSTTY_LOGGER=<log path>` in its environment, so your
prompt can show when recording is active. Example for bash/zsh:

```sh
[ -n "$GHOSTTY_LOGGER" ] && PS1="[LOG] $PS1"
```

Note: the variable reflects the first log path of the session; if you resume
logging after a Ctrl-\ pause, subsequent segments use new paths.

## Limitations vs. Terminator's Logger

- No GUI save dialog; the path comes from CLI flags, the environment, or the
  timestamped default.
- Output produced before logging starts is not captured (Terminator behaves
  the same: it starts at the current cursor row).
- Plain-text fidelity is line-based, not a full screen model, so output from
  full-screen TUIs on the primary screen may differ slightly from
  Terminator's `get_text_range`.
- Cursor columns are clamped to 10000 cells (`MAX_COLS`). Terminator is bounded
  by the real screen width; this wrapper has no screen model, so it needs an
  explicit bound to keep hostile output from requesting an unbounded line.
  Anything written past the clamp overwrites the last cell.
- Nesting `ghostty-logger` inside itself works but prints a warning.
- An OSC/DCS sequence that is never terminated suppresses the rest of the log,
  because it suppresses the rest of the display too: Ghostty's parser stays in
  `osc_string` until BEL, ST, CAN or SUB. Rather than diverge, the log notes the
  anomaly with a `--- ghostty-logger: ... ---` line and says so again if the
  session ends mid-sequence, so the gap is never silent.

The parser follows Ghostty's own state machine (`src/terminal/parse_table.zig`,
`Parser.zig`, `osc.zig`) where the two can diverge: CAN/SUB abort a sequence
from any state, ESC leaves a string sequence for the escape state, CSI
parameters past `MAX_PARAMS` (24) drop the command, and alternate-screen modes
are matched numerically. Divergences matter because anything the log renders
differently from the terminal is a way to make the record disagree with what
the operator saw.

## Log integrity

The log records what the terminal displayed, but it is not tamper-proof
against the session being logged. The logged shell runs as you, the log is
owned by you, and its path is in the shell's environment as `$GHOSTTY_LOGGER`,
so anything running in the session can read, rewrite, or truncate it. Treat
the log as a convenience record, not as a forensic audit trail. If you need
one, ship the lines somewhere the session cannot reach.

## Development

```sh
python3 -m unittest test_ghostty_logger -v
```
