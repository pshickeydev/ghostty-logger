#!/usr/bin/env python3
"""ghostty-logger: Terminator-style plain-text session logging for Ghostty.

Runs a shell (or command) inside a pty and appends the rendered plain
text of everything the terminal displays to a log file, recreating the
behavior of Terminator's Logger plugin for stock Ghostty.
"""

import argparse
import codecs
import datetime
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import tty
from collections.abc import Callable
from typing import TextIO

GROUND, ESC, CSI, CSI_IGNORE, OSC, DCS, IGNORE_STR, ESC_SKIP = range(8)

_ALT_SCREEN_MODES = frozenset((1049, 1047, 47))

# Cursor columns come straight out of the (untrusted) terminal stream, so every
# column and repeat count is clamped to this many cells. Real terminals clamp to
# the screen width; without a bound, "\x1b[999999999G" asks for a 1e9-cell line.
MAX_COLS = 10000

# Parameter bytes buffered before a CSI sequence is treated as malformed.
# Ghostty (src/terminal/Parser.zig) caps at MAX_PARAMS = 24 semicolon-separated
# parameters and drops the whole command past that, so we bound both ways.
_MAX_CSI_PARAMS = 64
_MAX_CSI_PARAM_COUNT = 24

# Ghostty (src/terminal/osc.zig) stops buffering an OSC past MAX_BUF and marks
# it invalid, but its state machine stays in osc_string until a real terminator.
# We match that rather than resuming on length: resuming would log bytes the
# terminal never displayed. We only note the anomaly so it is not invisible.
_STRING_WARN_LEN = 2048


class VTStripper:
    """Incremental VT stream parser that emits rendered plain text."""

    def __init__(self, emit: Callable[[str], object], include_alt_screen: bool = False,
                 max_cols: int = MAX_COLS) -> None:
        self.emit = emit
        self.include_alt_screen = include_alt_screen
        self.max_cols = max_cols
        self.state = GROUND
        self.text = bytearray()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.line: list[str] = []
        self.col = 0
        self.alt_screen = False
        self.csi = bytearray()
        self.str_len = 0
        self.str_warned = False

    @property
    def suppressed(self) -> bool:
        return self.alt_screen and not self.include_alt_screen

    def feed(self, data: bytes) -> None:
        for b in data:
            if self.state == GROUND:
                if b == 0x1B:
                    self._flush_text()
                    self.state = ESC
                elif b < 0x20 or b == 0x7F:
                    self._flush_text()
                    self._control(b)
                else:
                    self.text.append(b)
            elif self.state == ESC:
                self.state = self._esc_next(b)
            elif self.state == ESC_SKIP:
                self.state = GROUND
            elif self.state in (CSI, CSI_IGNORE):
                self._csi_byte(b)
            else:
                self._string_byte(b)
        self._flush_text()

    def flush(self) -> None:
        chunk = self.decoder.decode(b"", final=True)
        if not self.suppressed:
            for ch in chunk:
                self._put(ch)
        if self.state in (OSC, DCS, IGNORE_STR):
            # Everything since the sequence opened was swallowed as string
            # content. Say so rather than closing the log as if it were complete.
            self._note("log ended inside an unterminated string sequence")
        if self.line:
            self._newline()

    def _esc_next(self, b: int) -> int:
        if b == 0x5B:
            self.csi.clear()
            return CSI
        if b == 0x5D:
            self._begin_string()
            return OSC
        if b == 0x50:
            self._begin_string()
            return DCS
        if b in (0x58, 0x5E, 0x5F):
            self._begin_string()
            return IGNORE_STR
        if b in (0x28, 0x29, 0x2A, 0x2B, 0x23, 0x25):
            return ESC_SKIP
        return GROUND

    def _begin_string(self) -> None:
        self.str_len = 0
        self.str_warned = False

    def _end_string(self) -> None:
        self.state = GROUND
        self.str_len = 0
        self.str_warned = False

    def _string_byte(self, b: int) -> None:
        """Handle one byte inside an OSC/DCS/SOS/PM/APC string sequence."""
        if self.state == OSC and b == 0x07:
            self._end_string()  # BEL terminates an OSC
        elif b in (0x18, 0x1A):
            self._end_string()  # CAN/SUB abort any sequence from any state
        elif b == 0x1B:
            # ESC leaves the string for the escape state, so "ESC \\" ends it as
            # ST and "ESC [" starts a CSI. Staying put would let a program park
            # the parser here and silently stop the log while the terminal -
            # which does take this transition - carried on displaying.
            self.state = ESC
            self.str_len = 0
            self.str_warned = False
        else:
            self.str_len += 1
            if self.str_len > _STRING_WARN_LEN and not self.str_warned:
                self.str_warned = True
                self._note("string sequence exceeds "
                           f"{_STRING_WARN_LEN} bytes without terminating")

    def _note(self, message: str) -> None:
        """Record an anomaly in the log itself so a gap is never silent."""
        if self.suppressed:
            return
        if self.line:
            self._newline()
        self.emit(f"--- ghostty-logger: {message} ---\n")

    def _csi_byte(self, b: int) -> None:
        """Handle one byte inside a CSI sequence, per the ECMA-48 state machine."""
        if b == 0x1B:
            self.state = ESC
        elif b in (0x18, 0x1A):
            self.state = GROUND
        elif b < 0x20:
            self._control(b)  # C0 controls execute mid-sequence, as on a real terminal
        elif b == 0x7F:
            pass  # DEL is ignored inside a sequence
        elif 0x40 <= b <= 0x7E:
            if self.state == CSI:
                self._csi_final(b)
            self.state = GROUND
        elif self.state == CSI:
            if len(self.csi) < _MAX_CSI_PARAMS:
                self.csi.append(b)
            else:
                # Overlong parameter list. Keep discarding until the final byte
                # rather than dropping to GROUND: a real terminal displays none
                # of this, so logging it would record text that was never shown.
                self.csi.clear()
                self.state = CSI_IGNORE

    def _set_col(self, col: int) -> None:
        self.col = min(max(0, col), self.max_cols - 1)

    def _csi_final(self, b: int) -> None:
        params = bytes(self.csi)
        self.csi.clear()
        if params.startswith(b"?"):
            if b in (ord("h"), ord("l")):
                # Compare numerically: terminals read "?01049h" as mode 1049, so
                # a string compare would miss it and log the TUI we meant to skip.
                modes = set()
                for p in params[1:].split(b";"):
                    try:
                        modes.add(int(p))
                    except ValueError:
                        continue
                if modes & _ALT_SCREEN_MODES:
                    self._set_alt_screen(b == ord("h"))
            return
        if self.suppressed:
            return
        if params.count(b";") >= _MAX_CSI_PARAM_COUNT:
            return  # Ghostty drops a command with more parameters than it stores
        try:
            # Clamping here bounds every column move and repeat count at once;
            # no sequence we honor is meaningful beyond one line's width.
            nums = [min(int(p) if p else 0, self.max_cols) for p in params.split(b";")]
        except ValueError:
            return
        n = nums[0] if nums else 0
        if b == ord("C"):
            self._set_col(self.col + max(n, 1))
        elif b == ord("D"):
            self._set_col(self.col - max(n, 1))
        elif b in (ord("G"), ord("`")):
            self._set_col(n - 1)
        elif b in (ord("H"), ord("f")):
            col_param = nums[1] if len(nums) > 1 else 0
            self._set_col(col_param - 1)
        elif b == ord("K"):
            self._erase_in_line(n)
        elif b == ord("J") and n in (2, 3):
            self.line = []
            self.col = 0
        elif b == ord("P"):
            del self.line[self.col : self.col + max(n, 1)]
        elif b == ord("@"):
            self.line[self.col : self.col] = [" "] * max(n, 1)
            del self.line[self.max_cols :]

    def _erase_in_line(self, n: int) -> None:
        if n == 0:
            del self.line[self.col :]
        elif n == 1:
            for i in range(min(self.col + 1, len(self.line))):
                self.line[i] = " "
        else:
            self.line = []
            self.col = 0

    def _set_alt_screen(self, on: bool) -> None:
        if on == self.alt_screen:
            return
        if self.line:
            self._newline()
        self.alt_screen = on
        self.col = 0

    def _control(self, b: int) -> None:
        if self.suppressed:
            return
        if b in (0x0A, 0x0B, 0x0C):
            self._newline()
        elif b == 0x0D:
            self.col = 0
        elif b == 0x08:
            self._set_col(self.col - 1)
        elif b == 0x09:
            for _ in range(8 - self.col % 8):
                self._put(" ")

    def _newline(self) -> None:
        self.emit("".join(self.line) + "\n")
        self.line = []
        self.col = 0

    def _flush_text(self) -> None:
        if not self.text:
            return
        chunk = self.decoder.decode(bytes(self.text))
        self.text.clear()
        if self.suppressed:
            return
        for ch in chunk:
            self._put(ch)

    def _put(self, ch: str) -> None:
        if self.col >= self.max_cols:
            self.col = self.max_cols - 1  # pinned at the right margin
        while len(self.line) <= self.col:
            self.line.append(" ")
        self.line[self.col] = ch
        self.col += 1


def _timestamp() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class LogSession:
    """Owns the active log file and stripper; supports stop/restart."""

    def __init__(self, path: str, log_file: TextIO,
                 open_next: Callable[[], tuple[str, TextIO]],
                 include_alt_screen: bool, out_fd: int) -> None:
        self._open_next = open_next
        self._include_alt_screen = include_alt_screen
        self._out_fd = out_fd
        self.path = path
        self._file: TextIO | None = None
        self._stripper: VTStripper | None = None
        self._start(log_file, path)

    @property
    def active(self) -> bool:
        return self._file is not None

    def feed(self, data: bytes) -> None:
        if self._stripper is not None:
            self._guard(self._stripper.feed, data)

    def toggle(self) -> None:
        if self.active:
            self.stop()
        else:
            self.restart()

    def restart(self) -> None:
        if self.active:
            return
        try:
            path, log_file = self._open_next()
        except OSError as exc:
            self._notice(f"cannot open log file: {exc}")
            return
        self._start(log_file, path)
        self._notice(f"logging to {path}")

    def stop(self) -> None:
        if not self.active:
            return
        if self._stripper is not None:
            self._guard(self._stripper.flush)
        if self._file is not None:
            try:
                self._file.write(f"--- log ended {_timestamp()} ---\n")
                self._file.close()
            except OSError as exc:
                self._notice(f"cannot close log file: {exc}")
        self._file = None
        self._stripper = None
        self._notice("logging stopped")

    def _guard(self, fn: Callable[..., object], *args: object) -> None:
        """Run a parser call so that a fault degrades logging, not the session.

        The parser consumes untrusted terminal output. An exception escaping
        here used to propagate out of run_session and kill the user's shell.
        """
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            self._fault(exc)

    def _fault(self, exc: BaseException) -> None:
        if self._stripper is not None:
            # Drop the partial line; on MemoryError this is what frees the memory.
            self._stripper.line = []
            self._stripper.col = 0
        if self._file is not None:
            try:
                self._file.write(
                    f"--- log parser error {_timestamp()}: {exc!r} "
                    f"(output may be missing) ---\n"
                )
            except OSError:
                pass
        self._notice(f"parser error, log may be incomplete: {exc!r}")

    def _start(self, log_file: TextIO, path: str) -> None:
        self._file = log_file
        self._stripper = VTStripper(log_file.write, self._include_alt_screen)
        self.path = path
        log_file.write(f"--- log started {_timestamp()} ---\n")

    def _notice(self, message: str) -> None:
        _write_stdout(self._out_fd, f"\r\n[ghostty-logger: {message}]\r\n".encode())


def _parse_escape_key(spec: str) -> int | None:
    if not spec:
        return None
    if len(spec) == 2 and spec.startswith("^"):
        return ord(spec[1].upper()) & 0x1F
    if len(spec) == 1:
        return ord(spec)
    raise ValueError(f"invalid escape key: {spec!r}")


def _key_label(escape: int | None) -> str:
    if escape is None:
        return "disabled"
    if escape < 0x20:
        return f"Ctrl-{chr(escape + 0x40)}"
    return chr(escape)


def _scan_input(data: bytes, escape: int | None, pending: bool) -> tuple[bytes, int, bool]:
    out = bytearray()
    toggles = 0
    for b in data:
        if pending:
            pending = False
            if b == escape:
                out.append(b)
            else:
                toggles += 1
                out.append(b)
        elif escape is not None and b == escape:
            pending = True
        else:
            out.append(b)
    return bytes(out), toggles, pending


def _winsize(master_fd: int) -> None:
    try:
        packed = fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, b"\0" * 8)
    except OSError:
        packed = struct.pack("HHHH", 24, 80, 0, 0)
    try:
        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


def _write_stdout(fd: int, data: bytes) -> None:
    try:
        os.write(fd, data)
    except OSError:
        pass


def run_session(log_path: str, log_file: TextIO, open_next: Callable[[], tuple[str, TextIO]],
                command: list[str], include_alt_screen: bool = False,
                escape: int | None = None) -> int:
    pid, master_fd = pty.fork()
    if pid == 0:
        env = dict(os.environ)
        env["GHOSTTY_LOGGER"] = log_path
        try:
            os.execvpe(command[0], command, env)
        except BaseException as exc:  # noqa: BLE001 - must never return to caller
            # Anything escaping here would run the parent's session loop a
            # second time in this process, so exit rather than propagate.
            os.write(2, f"ghostty-logger: {exc}\n".encode())
        os._exit(127)

    _winsize(master_fd)
    if hasattr(signal, "SIGWINCH"):
        signal.signal(signal.SIGWINCH, lambda *_: _winsize(master_fd))

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    saved_termios = None
    if os.isatty(stdin_fd):
        saved_termios = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    session = LogSession(log_path, log_file, open_next, include_alt_screen, stdout_fd)
    status = None
    escape_pending = False
    fds = {stdin_fd, master_fd}
    try:
        while master_fd in fds and status is None:
            try:
                ready, _, _ = select.select(sorted(fds), [], [], 0.5)
            except InterruptedError:
                continue
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except OSError:
                    data = b""
                if not data:
                    fds.discard(fd)
                    continue
                if fd == stdin_fd:
                    data, toggles, escape_pending = _scan_input(data, escape, escape_pending)
                    for _ in range(toggles):
                        session.toggle()
                    if data:
                        try:
                            os.write(master_fd, data)
                        except OSError:
                            fds.discard(stdin_fd)
                else:
                    _write_stdout(stdout_fd, data)
                    session.feed(data)
            done, st = os.waitpid(pid, os.WNOHANG)
            if done == pid:
                status = st
    finally:
        if saved_termios is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, saved_termios)
        if status is None:
            _, status = os.waitpid(pid, 0)
        try:
            os.set_blocking(master_fd, False)
            while True:
                try:
                    data = os.read(master_fd, 65536)
                except BlockingIOError:
                    break
                if not data:
                    break
                _write_stdout(stdout_fd, data)
                session.feed(data)
        except OSError:
            pass
        os.close(master_fd)
        session.stop()

    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    if os.WIFSIGNALED(status):
        return 128 + os.WTERMSIG(status)
    return 1


# A session log holds everything the terminal displayed - tokens, keys, command
# output - so it is created owner-only rather than inheriting the umask.
_LOG_FILE_MODE = 0o600
_LOG_DIR_MODE = 0o700


def _fdopen(fd: int) -> TextIO:
    return os.fdopen(fd, "w", encoding="utf-8", buffering=1)


def _ensure_log_dir(base: str) -> None:
    try:
        os.makedirs(base, mode=_LOG_DIR_MODE)
    except FileExistsError:
        return
    # makedirs() masks mode with the umask and only applies it to the leaf.
    os.chmod(base, _LOG_DIR_MODE)


def _open_log(output: str | None, directory: str | None, append: bool = False) -> tuple[str, TextIO]:
    if output:
        # An explicit path is the caller's choice, including any symlink at it;
        # we only guarantee that a file *we* create is not world-readable.
        flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC)
        return output, _fdopen(os.open(output, flags, _LOG_FILE_MODE))
    base = directory or os.environ.get("GHOSTTY_LOGGER_DIR") or os.path.join(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")),
        "ghostty",
        "logs",
    )
    _ensure_log_dir(base)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    # This name is predictable, so refuse to write through anything already
    # sitting at it: O_EXCL rejects a planted file, O_NOFOLLOW a planted symlink.
    # The suffix loop also stops a resume within the same second from silently
    # truncating the segment it just wrote.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    for suffix in ("", *(f"-{i}" for i in range(1, 100))):
        path = os.path.join(base, f"ghostty-{stamp}-{os.getpid()}{suffix}.log")
        try:
            return path, _fdopen(os.open(path, flags, _LOG_FILE_MODE))
        except FileExistsError:
            continue
    raise OSError(errno.EEXIST, "no free log filename", base)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ghostty-logger",
        description="Log the plain-text content of a terminal session to a file, "
        "recreating Terminator's Logger plugin for Ghostty.",
    )
    parser.add_argument(
        "-o", "--output", metavar="PATH", help="log file path (default: timestamped "
        "file in --dir, GHOSTTY_LOGGER_DIR, or ~/.local/state/ghostty/logs)"
    )
    parser.add_argument("--dir", dest="directory", metavar="DIR",
                        help="directory for timestamped log files")
    parser.add_argument("--alt-screen", action="store_true",
                        help="also log alternate-screen (full-screen TUI) content")
    parser.add_argument("--escape-key", default="^\\", metavar="KEY",
                        help="key that toggles logging without exiting (default ^\\; "
                        "press twice for a literal; empty string disables)")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="command to run instead of $SHELL")
    args = parser.parse_args(argv)

    try:
        escape = _parse_escape_key(args.escape_key)
    except ValueError as exc:
        parser.error(str(exc))

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = [os.environ.get("SHELL") or "/bin/sh"]

    try:
        log_path, log_file = _open_log(args.output, args.directory)
    except OSError as exc:
        print(f"ghostty-logger: cannot open log file: {exc}", file=sys.stderr)
        return 1

    if os.environ.get("GHOSTTY_LOGGER"):
        print("ghostty-logger: warning: already inside a logged session",
              file=sys.stderr)

    open_next = lambda: _open_log(args.output, args.directory, append=True)  # noqa: E731
    label = _key_label(escape)
    print(f"ghostty-logger: logging to {log_path} "
          f"({label} toggles logging; exit or Ctrl-D ends the session)",
          file=sys.stderr)
    with log_file:
        rc = run_session(log_path, log_file, open_next, command, args.alt_screen, escape)
    print(f"ghostty-logger: log saved to {log_path}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
