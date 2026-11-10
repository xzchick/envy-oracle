"""Core Oracle tests: encrypted oracles, predictions, resolution, the calibration
score (Brier, accuracy, confidence bands), the event journal and its hash chain,
revision-preserving saves, export and the no-overwrite guarantee.
"""

import datetime
import json
from pathlib import Path
import tempfile
import unittest

from envy_oracle import core
from envy_oracle.core import OracleError

UTC = datetime.timezone.utc


class OracleTest(unittest.TestCase):
    SECRET = b"a long private oracle passphrase"

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="envy-oracle-"))
        self.addCleanup(__import__("shutil").rmtree, self.dir)
        self.base = datetime.datetime(2026, 1, 1, tzinfo=UTC)
        self._n = 0

    def seq(self):
        self._n += 1
        return self.dir / f"o.r{self._n}.envyoracle"

    def oracle(self, name="o.envyoracle", title="Forecasts"):
        path = self.dir / name
        core.create(path, title, self.SECRET)
        return path

    # ----- envelope / create -------------------------------------------
    def test_encrypted_round_trip_and_magic(self):
        blob = core.encrypt_bytes(b"x", self.SECRET)
        self.assertEqual(blob[:11], b"ENVYVAULT2\x00")
        self.assertEqual(core.decrypt_bytes(blob, self.SECRET), b"x")

    def test_create_empty_oracle(self):
        path = self.oracle(title="2026 bets")
        data, _ = core._load(path, self.SECRET)
        self.assertEqual(data["revision"], 0)
        self.assertEqual(data["predictions"], [])

    def test_short_passphrase_rejected(self):
        with self.assertRaises(OracleError):
            core.create(self.dir / "s", "t", b"short")

    # ----- predictions -------------------------------------------------
    def test_add_prediction_preserves_revision_and_logs(self):
        p0 = self.oracle()
        p1 = self.seq()
        res = core.add_prediction(p0, p1, self.SECRET, "Ship by Friday", 70)
        self.assertEqual(res["revision"], 1)
        self.assertTrue(p0.exists(), "previous revision preserved")
        data, _ = core._load(p1, self.SECRET)
        self.assertEqual(len(data["predictions"]), 1)
        self.assertEqual(data["predictions"][0]["confidence"], 70)
        self.assertFalse(data["predictions"][0]["resolved"])
        self.assertEqual(data["journal"][0]["kind"], "created")

    def test_confidence_bounds(self):
        p0 = self.oracle()
        for bad in (0, 100, -5):
            with self.assertRaises(OracleError):
                core.add_prediction(p0, self.seq(), self.SECRET, "x", bad)

    def test_resolve_sets_outcome_and_chains(self):
        p0 = self.oracle()
        p1 = self.seq()
        pid = core.add_prediction(p0, p1, self.SECRET, "It rains", 60)["prediction_id"]
        p2 = self.seq()
        core.resolve(p1, p2, self.SECRET, pid, True, note="it did")
        data, _ = core._load(p2, self.SECRET)
        pred = data["predictions"][0]
        self.assertTrue(pred["resolved"])
        self.assertIs(pred["outcome"], True)
        self.assertEqual(data["journal"][1]["kind"], "resolved")
        self.assertEqual(data["journal"][1]["previous_sha256"], data["journal"][0]["record_sha256"])

    def test_cannot_resolve_twice(self):
        p0 = self.oracle()
        p1 = self.seq()
        pid = core.add_prediction(p0, p1, self.SECRET, "x", 50)["prediction_id"]
        p2 = self.seq()
        core.resolve(p1, p2, self.SECRET, pid, True)
        with self.assertRaises(OracleError):
            core.resolve(p2, self.seq(), self.SECRET, pid, False)

    def test_remove_keeps_journal(self):
        p0 = self.oracle()
        p1 = self.seq()
        pid = core.add_prediction(p0, p1, self.SECRET, "x", 50)["prediction_id"]
        p2 = self.seq()
        core.remove_prediction(p1, p2, self.SECRET, pid)
        data, _ = core._load(p2, self.SECRET)
        self.assertEqual(data["predictions"], [])
        self.assertEqual([e["kind"] for e in data["journal"]], ["created", "removed"])

    # ----- scoring -----------------------------------------------------
    def _resolved_oracle(self, outcomes):
        """outcomes: list of (confidence, outcome_bool). Returns the final oracle path."""
        p = self.oracle()
        for i, (conf, out) in enumerate(outcomes):
            d = self.seq()
            pid = core.add_prediction(p, d, self.SECRET, f"pred {i}", conf)["prediction_id"]
            p = d
            d = self.seq()
            core.resolve(p, d, self.SECRET, pid, out)
            p = d
        return p

    def test_brier_and_accuracy(self):
        # Two predictions: 100%-ish is not allowed, use 90% yes (true) and 20% (false).
        p = self._resolved_oracle([(90, True), (20, False)])
        rep = core.score(p, self.SECRET)
        self.assertEqual(rep["resolved"], 2)
        self.assertEqual(rep["correct"], 2)          # 90%->yes correct, 20%->no correct
        self.assertEqual(rep["accuracy_pct"], 100.0)
        # Brier = mean((.9-1)^2, (.2-0)^2) = mean(.01, .04) = .025
        self.assertAlmostEqual(rep["brier"], 0.025, places=4)

    def test_calibration_bands(self):
        # Three 70% predictions, two true -> band 70-79 actual ~66.7%.
        p = self._resolved_oracle([(70, True), (70, True), (70, False)])
        rep = core.score(p, self.SECRET)
        band = [b for b in rep["bins"] if b["band"] == "70–79%"][0]
        self.assertEqual(band["count"], 3)
        self.assertEqual(band["you_said"], 70.0)
        self.assertAlmostEqual(band["actual"], 66.7, places=1)

    def test_brier_decomposition_identity_and_none(self):
        # No resolved predictions -> no decomposition.
        empty = self.oracle(name="empty.envyoracle")
        self.assertIsNone(core.score(empty, self.SECRET)["brier_decomposition"])
        # A spread of confidences and outcomes; the three terms must recompose the Brier.
        p = self._resolved_oracle([(90, True), (90, False), (70, True), (70, True),
                                    (30, False), (30, False), (20, True)])
        rep = core.score(p, self.SECRET)
        d = rep["brier_decomposition"]
        # Murphy identity: Brier == reliability - resolution + uncertainty (exact here).
        self.assertAlmostEqual(d["recomposed_brier"], rep["brier"], places=4)
        self.assertAlmostEqual(d["reliability"] - d["resolution"] + d["uncertainty"],
                               rep["brier"], places=4)
        # Perfect reliability: at 70% confidence, exactly 70%... use a clean case.
        # 2 of 2 true at 50% is miscalibrated (said 50, got 100) -> reliability > 0.
        for term in ("reliability", "resolution", "uncertainty"):
            self.assertGreaterEqual(d[term], 0.0)

    def test_brier_decomposition_perfect_calibration(self):
        # Ten 50% predictions, exactly five true: observed freq == stated 50% -> reliability 0.
        p = self._resolved_oracle([(50, True)] * 5 + [(50, False)] * 5)
        d = core.score(p, self.SECRET)["brier_decomposition"]
        self.assertAlmostEqual(d["reliability"], 0.0, places=4)
        # No discrimination (one confidence level) -> resolution 0, Brier == uncertainty.
        self.assertAlmostEqual(d["resolution"], 0.0, places=4)

    def test_open_and_overdue_states(self):
        p0 = self.oracle()
        p1 = self.seq()
        past = (self.base - datetime.timedelta(days=1)).isoformat()
        core.add_prediction(p0, p1, self.SECRET, "overdue one", 60, due_utc=past)
        p2 = self.seq()
        future = (self.base + datetime.timedelta(days=365)).isoformat()
        core.add_prediction(p1, p2, self.SECRET, "open one", 60, due_utc=future)
        rep = core.score(p2, self.SECRET, now=self.base.isoformat())
        self.assertEqual(rep["open"], 2)
        self.assertEqual(rep["overdue"], 1)
        self.assertEqual(rep["predictions"][0]["state"], "overdue")  # overdue sorts first

    def test_no_resolved_predictions_scores_none(self):
        p0 = self.oracle()
        p1 = self.seq()
        core.add_prediction(p0, p1, self.SECRET, "someday", 55)
        rep = core.score(p1, self.SECRET)
        self.assertIsNone(rep["brier"])
        self.assertIsNone(rep["accuracy_pct"])
        self.assertEqual(rep["bins"], [])

    def test_tampered_journal_breaks_chain(self):
        p0 = self.oracle()
        p1 = self.seq()
        core.add_prediction(p0, p1, self.SECRET, "x", 50)
        data, _ = core._load(p1, self.SECRET)
        data["journal"][0]["detail"] = "forged"      # tamper without re-hashing
        forged = self.dir / "forged.envyoracle"
        forged.write_bytes(core.encrypt_bytes(core._canonical(data), self.SECRET))
        with self.assertRaises(OracleError):
            core.inspect(forged, self.SECRET)

    # ----- export -------------------------------------------------------
    def test_export_html_and_json(self):
        p = self._resolved_oracle([(80, True), (30, False)])
        html_out = self.dir / "r.html"
        res = core.export(p, html_out, self.SECRET, "html")
        text = html_out.read_text()
        self.assertIn("Content-Security-Policy", text)
        self.assertIn("Brier", text)
        self.assertEqual(res["resolved"], 2)
        json_out = self.dir / "r.json"
        core.export(p, json_out, self.SECRET, "json")
        report = json.loads(json_out.read_text())
        self.assertIn("limitations", report)
        self.assertEqual(report["resolved"], 2)

    def test_export_escapes_markup_in_statement(self):
        p0 = self.oracle()
        p1 = self.seq()
        core.add_prediction(p0, p1, self.SECRET, "<script>alert(1)</script> happens", 60)
        out = self.dir / "r.html"
        core.export(p1, out, self.SECRET, "html")
        body = out.read_text().split("<style>")[1]
        self.assertNotIn("<script>alert", body)
        self.assertIn("&lt;script&gt;", body)

    # ----- no overwrite -------------------------------------------------
    def test_new_file_never_overwrites(self):
        keep = self.dir / "keep"
        keep.write_bytes(b"precious")
        with self.assertRaises(OracleError):
            core.new_file(keep, b"clobber")
        self.assertEqual(keep.read_bytes(), b"precious")

    def test_add_refuses_to_overwrite(self):
        p0 = self.oracle()
        dest = self.dir / "exists.envyoracle"; dest.write_bytes(b"keep me")
        with self.assertRaises(OracleError):
            core.add_prediction(p0, dest, self.SECRET, "x", 50)
        self.assertEqual(dest.read_bytes(), b"keep me")

    # ----- tags (v0.2) --------------------------------------------------
    def test_tags_are_normalized_on_add(self):
        p0 = self.oracle()
        p1 = self.seq()
        core.add_prediction(p0, p1, self.SECRET, "ships Q1", 70, tags=["Security", "security", " Infra "])
        data, _ = core._load(p1, self.SECRET)
        self.assertEqual(data["predictions"][0]["tags"], ["infra", "security"])  # lower, dedup, sorted

    def test_tag_rejects_bad_values(self):
        p0 = self.oracle()
        for bad in [["  "], ["x" * 41], [5], ["ok"] * 21]:
            with self.assertRaises(OracleError):
                core.add_prediction(p0, self.seq(), self.SECRET, "s", 60, tags=bad)

    def test_score_filters_by_tag(self):
        p = self.oracle()
        p = self._add(p, "sec call", 80, tags=["security"])
        pid_sec = self._last_id(p)
        p = self._add(p, "infra call", 60, tags=["infra"])
        # Resolve only the security one, correctly.
        p2 = self.seq(); core.resolve(p, p2, self.SECRET, pid_sec, True)
        full = core.score(p2, self.SECRET)
        sec = core.score(p2, self.SECRET, tag="security")
        self.assertEqual(full["total"], 2)
        self.assertEqual(sec["total"], 1)
        self.assertEqual(sec["resolved"], 1)
        self.assertEqual(sec["tag"], "security")
        self.assertEqual(core.score(p2, self.SECRET, tag="nope")["total"], 0)

    def test_tags_listing_counts(self):
        p = self.oracle()
        p = self._add(p, "a", 60, tags=["security"])
        p = self._add(p, "b", 60, tags=["security", "infra"])
        result = core.tags(p, self.SECRET)
        self.assertEqual(result["tags"], [{"tag": "infra", "count": 1}, {"tag": "security", "count": 2}])

    def test_pre_tags_oracle_migrates_on_load(self):
        # An oracle whose prediction predates `tags` must still open (migrated to []).
        p = self.oracle()
        p = self._add(p, "legacy", 55, tags=["keep"])
        data, _ = core._load(p, self.SECRET)
        del data["predictions"][0]["tags"]                      # simulate an old file
        legacy = self.dir / "legacy.envyoracle"
        core.new_file(legacy, core.encrypt_bytes(core._canonical(data), self.SECRET))
        reloaded, _ = core._load(legacy, self.SECRET)           # must not raise
        self.assertEqual(reloaded["predictions"][0]["tags"], [])

    def _add(self, src, statement, conf, tags=None):
        dst = self.seq()
        core.add_prediction(src, dst, self.SECRET, statement, conf, tags=tags)
        return dst

    def _last_id(self, src):
        data, _ = core._load(src, self.SECRET)
        return data["predictions"][-1]["id"]

    # ----- due listing (v0.2) ------------------------------------------
    def _at(self, days):
        return (self.base + datetime.timedelta(days=days)).isoformat()

    def test_due_lists_and_flags_overdue_sorted(self):
        p = self.oracle()
        p1 = self.seq(); core.add_prediction(p, p1, self.SECRET, "past one", 60, due_utc=self._at(5))
        p2 = self.seq(); core.add_prediction(p1, p2, self.SECRET, "future one", 70, due_utc=self._at(40))
        p3 = self.seq(); core.add_prediction(p2, p3, self.SECRET, "no due", 50)   # no due date
        rep = core.due(p3, self.SECRET, now=self._at(10))
        self.assertEqual(rep["count"], 2)
        self.assertEqual(rep["overdue"], 1)
        self.assertEqual(rep["due"][0]["statement"], "past one")   # soonest due first
        self.assertTrue(rep["due"][0]["overdue"])
        self.assertFalse(rep["due"][1]["overdue"])

    def test_due_within_filter(self):
        p = self.oracle()
        p1 = self.seq(); core.add_prediction(p, p1, self.SECRET, "soon", 60, due_utc=self._at(12))
        p2 = self.seq(); core.add_prediction(p1, p2, self.SECRET, "far", 60, due_utc=self._at(90))
        rep = core.due(p2, self.SECRET, now=self._at(0), within_days=30)
        self.assertEqual([r["statement"] for r in rep["due"]], ["soon"])

    # ----- calibration reliability diagram (v0.2) ----------------------
    def test_calibration_svg_is_well_formed_with_a_point_per_band(self):
        import xml.etree.ElementTree as ET
        bins = [{"band": "80–89%", "count": 2, "you_said": 80.0, "actual": 100.0, "gap": 20.0},
                {"band": "30–39%", "count": 1, "you_said": 30.0, "actual": 0.0, "gap": -30.0}]
        svg = core._calibration_svg(bins)
        self.assertTrue(svg.startswith("<svg"))
        root = ET.fromstring(svg)                                  # must be valid XML
        circles = root.findall(".//{http://www.w3.org/2000/svg}circle")
        self.assertEqual(len(circles), 2)

    def test_export_html_includes_reliability_diagram_when_resolved(self):
        p = self.oracle()
        p = self._add(p, "ships Q1", 80)
        pid = self._last_id(p)
        p2 = self.seq(); core.resolve(p, p2, self.SECRET, pid, True)
        out = self.dir / "r.html"
        core.export(p2, out, self.SECRET, "html")
        text = out.read_text()
        self.assertIn("Reliability diagram", text)
        self.assertIn("<svg", text)
        self.assertIn("stated confidence", text)

    def test_export_html_omits_diagram_when_nothing_resolved(self):
        p = self.oracle()
        p = self._add(p, "ships later", 60)            # open, no bins
        out = self.dir / "r2.html"
        core.export(p, out, self.SECRET, "html")
        self.assertNotIn("Reliability diagram", out.read_text())

    def test_export_markdown(self):
        p = self.oracle(title="2026 bets")
        p = self._add(p, "ships Q1", 80)
        pid = self._last_id(p)
        p2 = self.seq(); core.resolve(p, p2, self.SECRET, pid, True)
        out = self.dir / "r.md"
        res = core.export(p2, out, self.SECRET, "md")
        self.assertEqual(res["format"], "md")
        text = out.read_text()
        self.assertTrue(text.startswith("# 2026 bets"))
        self.assertIn("## Calibration by confidence band", text)
        self.assertIn("## Predictions (1)", text)
        self.assertIn("ships Q1", text)
        self.assertIn("| Band | Predictions |", text)


if __name__ == "__main__":
    unittest.main()
