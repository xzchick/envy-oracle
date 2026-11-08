"""CLI tests that need no display. The passphrase prompt is patched."""

import contextlib
import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from envy_oracle import cli

UTC = datetime.timezone.utc


class CLITest(unittest.TestCase):
    SECRET = b"a long private oracle passphrase"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="envy-oracle-cli-"))
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self._n = 0

    def seq(self):
        self._n += 1
        return self.dir / f"o.r{self._n}.envyoracle"

    def invoke(self, *args, secret=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_passphrase", return_value=secret or self.SECRET):
            code = cli.main([str(a) for a in args])
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("envy-oracle", out.getvalue())

    def test_add_resolve_score_flow(self):
        base = self.dir / "o.envyoracle"
        code, out, _ = self.invoke("init", base, "--title", "Bets")
        self.assertEqual(code, 0)
        p1 = self.seq()
        code, out, _ = self.invoke("add", base, p1, "--statement", "ships Friday", "--confidence", "80")
        self.assertEqual(code, 0)
        pid = out.split("prediction added ")[1].split(" ")[0]
        # No resolved predictions yet.
        code, out, _ = self.invoke("score", p1)
        self.assertEqual(code, 0)
        self.assertIn("nothing to score", out)
        # Resolve yes, then score reflects it.
        p2 = self.seq()
        code, out, _ = self.invoke("resolve", p1, p2, "--prediction", pid, "--yes")
        self.assertEqual(code, 0)
        code, out, _ = self.invoke("score", p2)
        self.assertEqual(code, 0)
        self.assertIn("1 resolved", out)
        self.assertIn("Brier", out)

    def test_overdue_scores_exit_1(self):
        base = self.dir / "o.envyoracle"
        self.invoke("init", base, "--title", "Bets")
        p1 = self.seq()
        past = (datetime.datetime.now(UTC) - datetime.timedelta(days=2)).isoformat()
        self.invoke("add", base, p1, "--statement", "late one", "--confidence", "60", "--due", past)
        code, out, _ = self.invoke("score", p1)
        self.assertEqual(code, 1)
        self.assertIn("overdue", out)

    def test_resolve_no_and_inspect(self):
        base = self.dir / "o.envyoracle"
        self.invoke("init", base, "--title", "Bets")
        p1 = self.seq()
        _, out, _ = self.invoke("add", base, p1, "--statement", "wont happen", "--confidence", "30")
        pid = out.split("prediction added ")[1].split(" ")[0]
        p2 = self.seq()
        code, out, _ = self.invoke("resolve", p1, p2, "--prediction", pid, "--no")
        self.assertEqual(code, 0)
        code, out, _ = self.invoke("inspect", p2)
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertFalse(data["predictions"][0]["outcome"])
        self.assertEqual(data["journal"][-1]["kind"], "resolved")

    def test_export(self):
        base = self.dir / "o.envyoracle"
        self.invoke("init", base, "--title", "Bets")
        p1 = self.seq()
        self.invoke("add", base, p1, "--statement", "x", "--confidence", "50")
        out_html = self.dir / "r.html"
        code, out, _ = self.invoke("export", p1, out_html, "--format", "html")
        self.assertEqual(code, 0)
        self.assertIn("sha256", out)

    def test_bad_passphrase_is_error(self):
        base = self.dir / "o.envyoracle"
        self.invoke("init", base, "--title", "Bets")
        code, _, err = self.invoke("score", base, secret=b"the wrong passphrase!")
        self.assertEqual(code, 2)
        self.assertIn("passphrase", err.lower())

    def test_unknown_command(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["seance"])


if __name__ == "__main__":
    unittest.main()
