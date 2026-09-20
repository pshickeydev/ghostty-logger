import contextlib
import datetime
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ghostty_logger as gl

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghostty_logger.py")


def strip(*chunks, alt_screen=False):
    out = []
    s = gl.VTStripper(out.append, include_alt_screen=alt_screen)
    for chunk in chunks:
        s.feed(chunk)
    s.flush()
    return "".join(out)


class VTStripperTest(unittest.TestCase):
    def test_plain_text_passthrough(self):
        self.assertEqual(strip(b"hello world\n"), "hello world\n")

    def test_sgr_color_sequences_removed(self):
        self.assertEqual(strip(b"\x1b[1;31mred\x1b[0m\n"), "red\n")

    def test_cursor_movement_sequences_removed(self):
        self.assertEqual(strip(b"ab\x1b[2Ccd\x1b[1Db\n"), "ab  cb\n")

    def test_carriage_return_overwrites(self):
        self.assertEqual(strip(b"abc\rX\n"), "Xbc\n")

    def test_backspace(self):
        self.assertEqual(strip(b"ab\bc\n"), "ac\n")

    def test_tab_expansion(self):
        self.assertEqual(strip(b"a\tb\n"), "a" + " " * 7 + "b\n")

    def test_osc_terminated_by_bel(self):
        self.assertEqual(strip(b"\x1b]0;window title\x07hi\n"), "hi\n")

    def test_osc_terminated_by_st(self):
        self.assertEqual(strip(b"\x1b]0;window title\x1b\\hi\n"), "hi\n")

    def test_dcs_removed(self):
        self.assertEqual(strip(b"\x1bPq1;2;3\x1b\\x\n"), "x\n")

    def test_sos_pm_apc_removed(self):
        self.assertEqual(strip(b"\x1bXjunk\x1b\\\x1b^junk\x1b\\\x1b_junk\x1b\\ok\n"), "ok\n")

    def test_charset_designation_skipped(self):
        self.assertEqual(strip(b"\x1b(B\x1b%Gabc\n"), "abc\n")

    def test_split_escape_sequence_across_feeds(self):
        self.assertEqual(strip(b"\x1b[31", b"mred\x1b[0m\n"), "red\n")

    def test_split_utf8_across_feeds(self):
        self.assertEqual(strip(b"\xe2\x82", b"\xac\n"), "€\n")

    def test_alt_screen_suppressed_by_default(self):
        data = b"a\n\x1b[?1049hsecret\n\x1b[?1049lb\n"
        self.assertEqual(strip(data), "a\nb\n")

    def test_alt_screen_included_with_flag(self):
        data = b"a\n\x1b[?1049hsecret\n\x1b[?1049lb\n"
        self.assertEqual(strip(data, alt_screen=True), "a\nsecret\nb\n")

    def test_partial_line_flushed_on_close(self):
        self.assertEqual(strip(b"partial"), "partial\n")

    def test_vt_ff_act_as_line_feed(self):
        self.assertEqual(strip(b"a\x0bb\x0cc\n"), "a\nb\nc\n")

    def test_progress_bar_redraw_collapses(self):
        self.assertEqual(strip(b"10%\r50%\r100%\n"), "100%\n")

    def test_8bit_c1_not_misparsed(self):
        self.assertEqual(strip(b"caf\xc3\xa9\n"), "café\n")


class EscapeKeyTest(unittest.TestCase):
    def test_caret_notation(self):
        self.assertEqual(gl._parse_escape_key("^\\"), 0x1C)
        self.assertEqual(gl._parse_escape_key("^A"), 0x01)

    def test_literal_char(self):
        self.assertEqual(gl._parse_escape_key("x"), ord("x"))

    def test_empty_disables(self):
        self.assertIsNone(gl._parse_escape_key(""))

    def test_invalid_spec(self):
        with self.assertRaises(ValueError):
            gl._parse_escape_key("ab")


class ScanInputTest(unittest.TestCase):
    def test_passthrough_without_escape(self):
        self.assertEqual(gl._scan_input(b"abc", 0x1C, False), (b"abc", 0, False))

    def test_escape_toggles_and_forwards_rest(self):
        self.assertEqual(gl._scan_input(b"\x1ca", 0x1C, False), (b"a", 1, False))

    def test_doubled_escape_sends_literal(self):
        self.assertEqual(gl._scan_input(b"\x1c\x1c", 0x1C, False), (b"\x1c", 0, False))

    def test_escape_split_across_chunks(self):
        out1, toggles1, pending = gl._scan_input(b"ab\x1c", 0x1C, False)
        self.assertEqual((out1, toggles1, pending), (b"ab", 0, True))
        out2, toggles2, pending = gl._scan_input(b"cd", 0x1C, pending)
        self.assertEqual((out2, toggles2, pending), (b"cd", 1, False))

    def test_disabled_escape_passes_through(self):
        self.assertEqual(gl._scan_input(b"\x1ca", None, False), (b"\x1ca", 0, False))


class UntrustedSequenceLimitsTest(unittest.TestCase):
    """Cursor columns come from untrusted output and must stay bounded.

    Without a clamp each of these payloads asked for a ~1e9-cell line, raising
    MemoryError (or OverflowError) out of the parser and killing the session.
    """

    PAYLOADS = {
        "cursor_abs_col": b"\x1b[999999999GX",
        "cursor_forward": b"\x1b[999999999CX",
        "cursor_position": b"\x1b[1;999999999HX",
        "insert_blanks": b"\x1b[999999999@",
        "insert_blanks_64_digit": b"\x1b[" + b"9" * 60 + b"@",
        "tab_at_huge_column": b"\x1b[999999999G\t",
        "repeated_cursor_forward": b"\x1b[9999CX" * 200,
        "delete_chars": b"\x1b[999999999P",
    }

    def test_payloads_stay_bounded(self):
        for name, payload in self.PAYLOADS.items():
            with self.subTest(payload=name):
                s = gl.VTStripper(lambda _: None)
                s.feed(payload)
                self.assertLessEqual(len(s.line), gl.MAX_COLS)
                self.assertLess(s.col, gl.MAX_COLS + 1)

    def test_column_clamped_to_max_cols(self):
        out = []
        s = gl.VTStripper(out.append, max_cols=10)
        s.feed(b"\x1b[9999GX\n")
        self.assertEqual(out, [" " * 9 + "X\n"])

    def test_writes_past_margin_overwrite_last_cell(self):
        out = []
        s = gl.VTStripper(out.append, max_cols=4)
        s.feed(b"\x1b[99Gabc\n")
        self.assertEqual(out, ["   c\n"])

    def test_normal_output_unaffected_by_clamp(self):
        long_line = b"x" * 500
        self.assertEqual(strip(long_line + b"\n"), "x" * 500 + "\n")


class CsiIgnoreTest(unittest.TestCase):
    """An overlong CSI sequence must be discarded, not logged as text.

    A real terminal stays in csi_ignore until a final byte (0x40-0x7E) and
    displays nothing; dropping straight to GROUND let attacker-chosen bytes in
    0x20-0x3F land in the log without ever appearing on screen.
    """

    def test_overlong_csi_params_not_logged(self):
        forged = b"\x1b[" + b"0" * 64 + b"192.168.1.1 - - 2026-01-01 500.00" + b"@"
        self.assertEqual(strip(forged), "")

    def test_overlong_csi_does_not_leak_tail(self):
        self.assertEqual(strip(b"\x1b[" + b"0" * 70 + b"m" + b"visible\n"), "visible\n")

    def test_overlong_csi_ends_at_final_byte(self):
        self.assertEqual(strip(b"\x1b[" + b"0" * 70 + b"mafter\n"), "after\n")

    def test_c0_executes_inside_csi(self):
        # A real terminal executes C0 controls mid-sequence; the newline shows.
        self.assertEqual(strip(b"a\x1b[" + b"0" * 70 + b"\nb\n"), "a\n\n")

    def test_esc_restarts_sequence_inside_csi(self):
        self.assertEqual(strip(b"\x1b[1;2\x1b[31mok\n"), "ok\n")

    def test_can_cancel_sequence(self):
        self.assertEqual(strip(b"\x1b[1;2\x18ok\n"), "ok\n")


class StringSequenceEvasionTest(unittest.TestCase):
    """Logging must not be silenceable by parking the parser in a string state.

    Ghostty's parse_table.zig leaves osc_string on BEL, on ESC (into the escape
    state, so "ESC \\" is ST and "ESC [" starts a CSI), and on CAN/SUB via its
    anywhere-transitions. Missing any of those let a program keep the parser in
    a string state and stop the log while the terminal displayed normally.
    """

    def test_bel_terminates_osc(self):
        self.assertEqual(strip(b"\x1b]0;t\x07after\n"), "after\n")

    def test_st_terminates_osc(self):
        self.assertEqual(strip(b"\x1b]0;t\x1b\\after\n"), "after\n")

    def test_esc_leaves_string_for_escape_state(self):
        # ESC [ starts a CSI on a real terminal; the log must follow it out.
        self.assertEqual(strip(b"\x1b]0;t\x1b[31mafter\n"), "after\n")
        self.assertEqual(strip(b"\x1bPq\x1b[0mafter\n"), "after\n")
        self.assertEqual(strip(b"\x1b_x\x1b[0mafter\n"), "after\n")

    def test_can_aborts_string(self):
        self.assertEqual(strip(b"\x1b]0;t\x18after\n"), "after\n")

    def test_sub_aborts_string(self):
        self.assertEqual(strip(b"\x1b]0;t\x1aafter\n"), "after\n")

    def test_can_aborts_dcs_and_apc(self):
        self.assertEqual(strip(b"\x1bPq\x18after\n"), "after\n")
        self.assertEqual(strip(b"\x1b_x\x18after\n"), "after\n")

    def test_unterminated_string_matches_terminal_but_warns(self):
        # Ghostty stays in osc_string until a real terminator, so we do too:
        # resuming on length would log bytes the terminal never displayed.
        result = strip(b"\x1b]0;t" + b"x" * (gl._STRING_WARN_LEN + 10) + b"\nhidden\n")
        self.assertNotIn("hidden", result)
        self.assertIn("without terminating", result)

    def test_session_ending_mid_string_is_marked(self):
        out = []
        s = gl.VTStripper(out.append)
        s.feed(b"seen\n\x1b]0;never-terminated")
        s.flush()
        self.assertEqual(out[0], "seen\n")
        self.assertIn("unterminated string sequence", "".join(out))

    def test_complete_session_has_no_spurious_marker(self):
        self.assertNotIn("ghostty-logger:", strip(b"\x1b]0;title\x07ok\n"))


class CsiParamCountTest(unittest.TestCase):
    """Ghostty's MAX_PARAMS is 24; a longer parameter list drops the command."""

    def test_command_within_param_limit_is_honored(self):
        # CSI 1 ;;;... G -> column 1, so X lands on the "a".
        self.assertEqual(strip(b"abc" + b"\x1b[1" + b";" * 20 + b"GX\n"), "Xbc\n")

    def test_command_beyond_param_limit_is_dropped(self):
        # Past MAX_PARAMS the move is dropped, so X appends instead. The
        # sequence still ends at its final byte: no parameter byte is logged.
        self.assertEqual(strip(b"abc" + b"\x1b[1" + b";" * 30 + b"GX\n"), "abcX\n")


class AltScreenModeMatchingTest(unittest.TestCase):
    """Alt-screen modes are numeric; "?01049h" is mode 1049 to a real terminal."""

    def suppressed(self, data):
        return "SECRET" not in strip(data)

    def test_plain_mode_suppressed(self):
        self.assertTrue(self.suppressed(b"a\n\x1b[?1049hSECRET\n\x1b[?1049lb\n"))

    def test_leading_zero_mode_suppressed(self):
        self.assertTrue(self.suppressed(b"a\n\x1b[?01049hSECRET\n\x1b[?01049lb\n"))

    def test_combined_params_suppressed(self):
        self.assertTrue(self.suppressed(b"a\n\x1b[?1049;1hSECRET\n\x1b[?1049;1lb\n"))

    def test_legacy_modes_suppressed(self):
        for mode in (b"47", b"1047", b"047"):
            with self.subTest(mode=mode):
                data = b"a\n\x1b[?" + mode + b"hSECRET\n\x1b[?" + mode + b"lb\n"
                self.assertTrue(self.suppressed(data))

    def test_unrelated_private_mode_does_not_suppress(self):
        self.assertFalse(self.suppressed(b"\x1b[?25lSECRET\n"))


class LogFilePermissionsTest(unittest.TestCase):
    """Session logs hold whatever the terminal displayed, so they are owner-only."""

    @contextlib.contextmanager
    def permissive_umask(self):
        old = os.umask(0o000)
        try:
            yield
        finally:
            os.umask(old)

    def test_generated_log_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp, self.permissive_umask():
            base = os.path.join(tmp, "logs")
            path, f = gl._open_log(None, base)
            with f:
                f.write("secret\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(base).st_mode), 0o700)

    def test_explicit_output_path_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp, self.permissive_umask():
            path = os.path.join(tmp, "explicit.log")
            _, f = gl._open_log(path, None)
            with f:
                f.write("secret\n")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)

    def test_end_to_end_log_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp, self.permissive_umask():
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--dir", tmp, "printf", "hi\n"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            logs = [f for f in os.listdir(tmp) if f.startswith("ghostty-")]
            self.assertEqual(len(logs), 1)
            mode = stat.S_IMODE(os.stat(os.path.join(tmp, logs[0])).st_mode)
            self.assertEqual(mode, 0o600)


class LogPathPlantingTest(unittest.TestCase):
    """The generated name is predictable, so never write through what is there."""

    def candidate_names(self):
        now = datetime.datetime.now()
        later = now + datetime.timedelta(seconds=1)
        return [f"ghostty-{t.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.log"
                for t in (now, later)]

    def test_planted_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            victim = os.path.join(tmp, "victim.txt")
            with open(victim, "w", encoding="utf-8") as f:
                f.write("IMPORTANT")
            for name in self.candidate_names():
                os.symlink(victim, os.path.join(tmp, name))
            path, f = gl._open_log(None, tmp)
            with f:
                f.write("session data\n")
            self.assertFalse(os.path.islink(path))
            with open(victim, encoding="utf-8") as f:
                self.assertEqual(f.read(), "IMPORTANT")

    def test_planted_regular_file_is_not_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            planted = os.path.join(tmp, self.candidate_names()[0])
            with open(planted, "w", encoding="utf-8") as f:
                f.write("PRE-EXISTING")
            path, f = gl._open_log(None, tmp)
            with f:
                f.write("session data\n")
            self.assertNotEqual(path, planted)
            with open(planted, encoding="utf-8") as f:
                self.assertEqual(f.read(), "PRE-EXISTING")


class ParserFaultIsContainedTest(unittest.TestCase):
    """A parser fault must degrade logging, not tear down the user's shell."""

    class ExplodingStripper:
        def __init__(self, emit, include_alt_screen=False, max_cols=0):
            self.line = []
            self.col = 0

        def feed(self, data):
            raise MemoryError()

        def flush(self):
            pass

    def test_feed_error_is_contained_and_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fault.log")
            devnull = os.open(os.devnull, os.O_WRONLY)
            original = gl.VTStripper
            gl.VTStripper = self.ExplodingStripper
            try:
                with open(path, "w", encoding="utf-8", buffering=1) as f:
                    session = gl.LogSession(path, f, lambda: (path, f), False, devnull)
                    session.feed(b"anything")  # must not raise
                    session.stop()
            finally:
                gl.VTStripper = original
                os.close(devnull)
            with open(path, encoding="utf-8") as f:
                content = f.read()
        self.assertIn("log parser error", content)
        self.assertIn("MemoryError", content)
        self.assertIn("--- log ended", content)


class ProxyTest(unittest.TestCase):
    def run_logger(self, *command, timeout=30):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "test.log")
            proc = subprocess.run(
                [sys.executable, SCRIPT, "-o", log_path, *command],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
            )
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
        return proc, content

    def test_output_is_logged_as_plain_text(self):
        proc, content = self.run_logger("printf", "hello\n")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("hello\n", content)
        self.assertIn("--- log started", content)
        self.assertIn("--- log ended", content)

    def test_ansi_sequences_stripped_in_log(self):
        proc, content = self.run_logger("printf", "\033[31mhello\033[0m\n")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("hello", content)
        self.assertNotIn("\x1b", content)

    def test_child_exit_code_propagates(self):
        proc, _ = self.run_logger("sh", "-c", "exit 3")
        self.assertEqual(proc.returncode, 3)

    def test_logger_env_var_visible_to_child(self):
        proc, content = self.run_logger("sh", "-c", 'echo "LOG=$GHOSTTY_LOGGER"')
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("LOG=", content)
        self.assertIn("test.log", content)

    def test_default_log_path_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, SCRIPT, "--dir", tmp, "printf", "hi\n"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr.decode())
            logs = [f for f in os.listdir(tmp) if f.startswith("ghostty-")]
            self.assertEqual(len(logs), 1)

    def test_unwritable_log_path_fails_cleanly(self):
        proc = subprocess.run(
            [sys.executable, SCRIPT, "-o", "/nonexistent-dir/x/y.log", "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 1)
        self.assertIn(b"cannot open log file", proc.stderr)

    def test_hostile_escape_sequences_do_not_kill_session(self):
        payload = "before\\n\\033[999999999GX\\033[" + "0" * 70 + "mafter\\n"
        proc, content = self.run_logger("printf", payload)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("before", content)
        self.assertIn("--- log ended", content)
        self.assertNotIn("Traceback", proc.stderr.decode())

    def test_string_sequences_cannot_silence_the_log(self):
        payload = (
            "visible-one\\n"
            "\\033]0;t\\030"            # OSC aborted with CAN
            "visible-two\\n"
            "\\033]0;unterminated"       # unterminated OSC
            "visible-three\\n"
        )
        proc, content = self.run_logger("printf", payload)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertIn("visible-one", content)
        # CAN aborts the sequence, so logging resumes immediately.
        self.assertIn("visible-two", content)
        # The trailing sequence is never terminated, which suppresses the rest
        # on a real terminal too - but the log says so rather than ending as
        # though nothing were missing.
        self.assertIn("unterminated string sequence", content)

    def test_toggle_logging_without_exiting(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = os.path.join(tmp, "toggle.log")
            proc = subprocess.Popen(
                [sys.executable, SCRIPT, "-o", log_path, "bash", "--norc", "-i"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            assert proc.stdin is not None
            proc.stdin.write(b"echo alpha\n")
            proc.stdin.flush()
            time.sleep(0.7)
            proc.stdin.write(b"\x1c")
            proc.stdin.flush()
            time.sleep(0.3)
            proc.stdin.write(b"echo bravo\n")
            proc.stdin.flush()
            time.sleep(0.7)
            proc.stdin.write(b"\x1c")
            proc.stdin.flush()
            time.sleep(0.3)
            proc.stdin.write(b"echo charlie\nexit\n")
            proc.stdin.flush()
            rc = proc.wait(timeout=20)
            with open(log_path, encoding="utf-8") as f:
                content = f.read()
        self.assertEqual(rc, 0)
        self.assertIn("alpha", content)
        self.assertNotIn("bravo", content)
        self.assertIn("charlie", content)
        self.assertEqual(content.count("--- log started"), 2)
        self.assertEqual(content.count("--- log ended"), 2)


if __name__ == "__main__":
    unittest.main()
