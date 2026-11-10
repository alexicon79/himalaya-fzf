"""Offline reply tests: real Neovim, but a fake Himalaya that never sends mail."""
import json
import os
from pathlib import Path
import pty
import select
import signal
import tempfile
import time
import unittest


HIF = Path(__file__).resolve().parents[1] / "hif"


class ComposeTests(unittest.TestCase):
    def compose(self, fileformat, cc=(), bcc=(), fail_normalize=False):
        with tempfile.TemporaryDirectory(prefix="hif-test-") as directory:
            root = Path(directory)
            capture = root / "capture.json"
            fields = {"to": ['"Test Sender" <sender@example.org>'],
                      "cc": list(cc), "bcc": list(bcc), "subject": "Re: Test reply",
                      "body": "Reply text.\n\nTo: body text, not a header\n"}
            editor = root / "editor"
            editor.write_text('#!/bin/sh\nexec nvim --clean --headless "$@" '
                              '-c "setlocal fileformat=$TEST_FILEFORMAT" -c wq\n')
            editor.chmod(0o700)
            fake = root / "fake_himalaya.py"
            fake.write_text('''import json, os, pathlib, sys
args = sys.argv[1:]
if "--json" in args:
    print(os.environ["TEST_FIELDS"])
else:
    body = pathlib.Path(args[args.index("--body-file") + 1]).read_bytes().decode()
    pathlib.Path(os.environ["TEST_CAPTURE"]).write_text(json.dumps({"args": args, "body": body}))
    if any("\\r" in arg for arg in args):
        sys.exit("Invalid carriage return in address/header")
''')
            env = dict(os.environ, HIF_SCRIPT=str(HIF), TEST_FAKE=str(fake),
                       TEST_CAPTURE=str(capture), TEST_FIELDS=json.dumps(fields),
                       TEST_FILEFORMAT=fileformat, TMPDIR=str(root), VISUAL=str(editor),
                       EDITOR=str(editor))
            if fail_normalize:
                fakebin = root / "bin"
                fakebin.mkdir()
                sed = fakebin / "sed"
                sed.write_text("#!/bin/sh\nprintf partial\nexit 1\n")
                sed.chmod(0o700)
                env["PATH"] = str(fakebin) + ":" + env["PATH"]
            # pty.fork gives the editor and zsh read -k a real controlling tty.
            pid, terminal = pty.fork()
            if pid == 0:
                os.execvpe("zsh", ["zsh", "-f", "-c", '''
source "$HIF_SCRIPT"
_hi() { /usr/bin/python3 "$TEST_FAKE" "$@"; }
_pause() { :; }
_compose reply inbox source-id
'''], env)
            output = b""
            answered = False
            finished = False
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    if select.select([terminal], [], [], 0.1)[0]:
                        try:
                            chunk = os.read(terminal, 65536)
                        except OSError:
                            break
                        if not chunk:
                            break
                        output += chunk
                        if not answered and b"[N]o: " in output:
                            os.write(terminal, b"y")
                            answered = True
                    exited, _ = os.waitpid(pid, os.WNOHANG)
                    if exited:
                        finished = True
                        break
                else:
                    self.fail("Reply test timed out")
            finally:
                if not finished:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(pid, 0)
                os.close(terminal)
            if fail_normalize:
                self.assertFalse(capture.exists(), "Normalization failure must not send")
                drafts = list(root.glob("*/draft.eml"))
                self.assertEqual(len(drafts), 1)
                self.assertIn(b"Reply text.\r\n", drafts[0].read_bytes())
                return
            self.assertTrue(capture.exists(), output.decode(errors="replace"))
            result = json.loads(capture.read_text())
            args = result["args"]
            self.assertEqual(args[args.index("-t") + 1], fields["to"][0])
            self.assertEqual(args[args.index("-s") + 1], fields["subject"])
            self.assertFalse(any("\r" in arg for arg in args))
            for option, addresses in (("--cc", cc), ("--bcc", bcc)):
                if addresses:
                    self.assertEqual(args[args.index(option) + 1], ", ".join(addresses))
                else:
                    self.assertNotIn(option, args)
            self.assertEqual(result["body"], "\n\n" + fields["body"] + "\n")
            self.assertNotIn("\r", result["body"])

    def test_failed_normalization_keeps_draft_without_sending(self):
        self.compose("dos", fail_normalize=True)

    def test_lf_reply_preserves_headers_and_body(self):
        self.compose("unix")

    def test_crlf_reply_omits_empty_cc_and_bcc_and_preserves_body(self):
        self.compose("dos")

    def test_crlf_reply_preserves_nonempty_cc_and_bcc(self):
        self.compose("dos", cc=("cc@example.org",), bcc=("bcc@example.org",))


if __name__ == "__main__":
    unittest.main()
