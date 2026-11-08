"""A native ENVY Oracle window: a real desktop app, not a web view.

Built with PyQt6 and styled in the ENVY palette with a live animated header.
Record predictions with a stated confidence, resolve them yes or no, and read
your calibration — Brier score, accuracy, and a table by confidence band. Every
save is a new revision; earlier revisions are never overwritten.

Nothing is sent: no network, no accounts.
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path

from . import __version__
from . import core
from .core import OracleError

BG = "#050713"
PANEL = "#0a0e22"
PANEL_HI = "#121735"
INK = "#f2f4ff"
MUTED = "#b9c3df"
DIM = "#6f7898"
PINK = "#ff3db4"
CYAN = "#35e8ff"
LAVENDER = "#ac8cff"
VIOLET = "#8547e8"
AMBER = "#ffb347"
GREEN = "#49e3a6"
LINE = "#1f2547"

STATE_COLOR = {"correct": GREEN, "wrong": PINK, "open": CYAN, "overdue": AMBER}
STATE_LABEL = {"correct": "correct", "wrong": "wrong", "open": "open", "overdue": "overdue"}


def lerp(a, b, t):
    from PyQt6.QtGui import QColor
    ca, cb = QColor(a), QColor(b)
    return QColor(int(ca.red() + (cb.red() - ca.red()) * t),
                  int(ca.green() + (cb.green() - ca.green()) * t),
                  int(ca.blue() + (cb.blue() - ca.blue()) * t))


def _build():
    from PyQt6.QtCore import Qt, QTimer, QRectF, QPointF
    from PyQt6.QtGui import (QColor, QPainter, QLinearGradient, QRadialGradient, QFont,
                             QPen, QBrush, QPolygonF)
    from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QPushButton, QPlainTextEdit,
                                 QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView,
                                 QAbstractItemView, QVBoxLayout, QHBoxLayout, QInputDialog,
                                 QFileDialog, QMessageBox, QLineEdit as QLE)

    def star_polygon(cx, cy, outer, inner, points=4, rot=0.0):
        poly = QPolygonF()
        for i in range(points * 2):
            r = outer if i % 2 == 0 else inner
            a = rot + i * math.pi / points
            poly.append(QPointF(cx + r * math.cos(a), cy + r * math.sin(a)))
        return poly

    class Header(QWidget):
        def __init__(self):
            super().__init__()
            self.setFixedHeight(172)
            self.phase = 0.0
            self.stars = [(((i * 139 + 37) % 1000) / 1000.0,
                           ((i * 83 + 19) % 172) / 172.0,
                           0.6 + (i % 3) * 0.5, (i % 7) / 7.0) for i in range(60)]

        def tick(self, phase):
            self.phase = phase
            self.update()

        def paintEvent(self, _event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            p.fillRect(self.rect(), QColor(BG))
            for cx, cy, rad, col, a in ((w - 120, 60, 250, LAVENDER, 46),
                                        (60, h, 280, VIOLET, 52), (w * 0.5, -40, 240, CYAN, 40)):
                g = QRadialGradient(cx, cy, rad)
                c = QColor(col); c.setAlpha(a); g.setColorAt(0, c)
                c2 = QColor(col); c2.setAlpha(0); g.setColorAt(1, c2)
                p.setBrush(QBrush(g)); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(cx, cy), rad, rad)
            for sx, sy, sr, ph in self.stars:
                tw = 0.5 + 0.5 * math.sin(self.phase / 0.5 + ph * 6.28)
                c = QColor(205, 217, 255); c.setAlphaF(0.15 + 0.6 * tw)
                p.setBrush(c); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(sx * w, sy * h), sr, sr)
            self._oracle_mark(p, 46, 52, 20)
            pulse = 0.5 + 0.5 * math.sin(self.phase)
            p.setFont(QFont("Helvetica", 30, QFont.Weight.Bold))
            grad = QLinearGradient(84, 0, 470, 0)
            grad.setColorAt(0, QColor(CYAN))
            grad.setColorAt(0.5, QColor(lerp(CYAN, "#ffffff", 0.2 * pulse)))
            grad.setColorAt(1, QColor(PINK))
            glow = QColor(VIOLET); glow.setAlpha(int(55 + 55 * pulse))
            p.setPen(QPen(glow, 1))
            for dx, dy in ((1.3, 1.3), (-1.3, 1.3), (1.3, -1.3)):
                p.drawText(QRectF(84 + dx, 38 + dy, 560, 48), Qt.AlignmentFlag.AlignVCenter, "ENVY ORACLE")
            p.setPen(QPen(QBrush(grad), 1))
            p.drawText(QRectF(84, 38, 560, 48), Qt.AlignmentFlag.AlignVCenter, "ENVY ORACLE")
            p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
            p.setPen(QColor(LAVENDER))
            p.drawText(QRectF(86, 92, 700, 20), Qt.AlignmentFlag.AlignVCenter,
                       "ENCRYPTED  ·  FORECASTS  ·  CALIBRATED")
            y = 126
            p.setPen(QPen(QColor(LINE), 2)); p.drawLine(86, y, w - 54, y)
            p.setPen(QPen(QColor(VIOLET), 3)); p.drawLine(86, y, 158, y)
            span = (w - 54) - 86
            x = 86 + span * ((self.phase * 0.12) % 1.0)
            halo = QRadialGradient(x, y, 15)
            hc = QColor(CYAN); hc.setAlpha(170); halo.setColorAt(0, hc)
            hc2 = QColor(CYAN); hc2.setAlpha(0); halo.setColorAt(1, hc2)
            p.setBrush(QBrush(halo)); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(x, y), 15, 15)
            p.setBrush(QColor("#ffffff")); p.drawEllipse(QPointF(x, y), 2.5, 2.5)
            p.end()

        def _oracle_mark(self, p, cx, cy, r):
            pulse = 0.5 + 0.5 * math.sin(self.phase)
            # A soft aura.
            aura = QRadialGradient(cx, cy, r + 8)
            c = QColor(LAVENDER); c.setAlpha(int(70 + 60 * pulse)); aura.setColorAt(0, c)
            c2 = QColor(LAVENDER); c2.setAlpha(0); aura.setColorAt(1, c2)
            p.setBrush(QBrush(aura)); p.setPen(Qt.PenStyle.NoPen)
            p.drawEllipse(QPointF(cx, cy), r + 8, r + 8)
            # A slowly turning four-point star, with a smaller bright one inside.
            grad = QLinearGradient(cx - r, cy, cx + r, cy)
            grad.setColorAt(0, QColor(CYAN)); grad.setColorAt(1, QColor(PINK))
            p.setBrush(QBrush(grad)); p.setPen(Qt.PenStyle.NoPen)
            p.drawPolygon(star_polygon(cx, cy, r, r * 0.34, 4, self.phase * 0.2))
            p.setBrush(QColor("#ffffff"))
            p.drawPolygon(star_polygon(cx, cy, r * (0.4 + 0.08 * pulse), r * 0.16, 4, -self.phase * 0.3))
            # Orbiting sparks.
            for k in range(3):
                a = self.phase * 0.9 + k * 2.094
                sx, sy = cx + (r + 6) * math.cos(a), cy + (r + 6) * math.sin(a)
                c = QColor(CYAN if k % 2 else PINK); c.setAlphaF(0.4 + 0.5 * ((math.sin(a) + 1) / 2))
                p.setBrush(c); p.drawEllipse(QPointF(sx, sy), 1.6, 1.6)

    def make_table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setDefaultSectionSize(28)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        table.setAlternatingRowColors(True)
        table.horizontalHeader().setStretchLastSection(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        return table

    ID_ROLE = int(Qt.ItemDataRole.UserRole)

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.oracle = None
            self.secret = None
            self.data = None
            self.report = None
            self.setWindowTitle("ENVY Oracle")
            self.resize(1180, 880)
            self.setMinimumSize(1000, 740)
            self.setStyleSheet(STYLE)
            root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
            self.header = Header(); root.addWidget(self.header)

            body = QWidget(); body.setObjectName("body")
            bl = QVBoxLayout(body); bl.setContentsMargins(22, 14, 22, 16); bl.setSpacing(10)

            intro = QLabel("Record a prediction with a confidence, resolve it yes or no when you know, and see "
                           "how well your confidence matched reality. Every save is a new revision. Nothing is sent.")
            intro.setObjectName("status"); intro.setWordWrap(True); bl.addWidget(intro)

            controls = QHBoxLayout()
            for caption, handler, name in (("New", self.new_oracle, "go"),
                                           ("Open", self.open_oracle, "pick"),
                                           ("Lock", self.lock, "pick"),
                                           ("Add prediction", self.add_prediction, "go"),
                                           ("Resolve yes", self.resolve_yes, "go"),
                                           ("Resolve no", self.resolve_no, "pick"),
                                           ("Remove", self.remove_prediction, "pick"),
                                           ("Export", self.export_report, "pick")):
                b = QPushButton(caption); b.setObjectName(name)
                b.setCursor(Qt.CursorShape.PointingHandCursor); b.clicked.connect(handler)
                controls.addWidget(b)
            controls.addStretch(1)
            bl.addLayout(controls)

            self.mon_header = QPlainTextEdit(); self.mon_header.setObjectName("mono")
            self.mon_header.setReadOnly(True); self.mon_header.setFixedHeight(84)
            self.mon_header.setPlainText("No oracle open. Create a new oracle or open an existing one.")
            bl.addWidget(self.mon_header)

            self.views = QTabWidget(); self.views.setObjectName("tabs")
            self.t_preds = make_table(["State", "Statement", "Confidence", "Due", "Outcome"])
            self.t_cal = make_table(["Confidence band", "Predictions", "You said", "Actual", "Gap"])
            self.t_history = make_table(["Logged UTC", "Event", "Detail"])
            self.views.addTab(self.t_preds, "Predictions")
            self.views.addTab(self.t_cal, "Calibration")
            self.views.addTab(self.t_history, "History")
            bl.addWidget(self.views, 1)

            self.path_label = QLabel(""); self.path_label.setObjectName("foot")
            self.path_label.setWordWrap(True); bl.addWidget(self.path_label)
            foot = QLabel("The Oracle does not foretell anything — it scores the confidence you stated against "
                          "the outcomes you resolved. Calibration needs volume to mean much, you resolve your own "
                          "predictions, and times are assertions from your clock.")
            foot.setObjectName("foot"); foot.setWordWrap(True); bl.addWidget(foot)
            root.addWidget(body, 1)

            self.phase = 0.0
            self.timer = QTimer(self); self.timer.timeout.connect(self._animate); self.timer.start(33)
            self._set_enabled(False)

        # ----- lifecycle ----------------------------------------------------
        def _ask_passphrase(self, confirm):
            text, ok = QInputDialog.getText(self, "Passphrase",
                                            "Oracle passphrase (at least 12 characters):", QLE.EchoMode.Password)
            if not ok:
                return None
            secret = text.encode("utf-8")
            if confirm:
                again, ok2 = QInputDialog.getText(self, "Confirm passphrase",
                                                  "Repeat the passphrase:", QLE.EchoMode.Password)
                if not ok2:
                    return None
                if again.encode("utf-8") != secret:
                    self._warn("Passphrases do not match."); return None
            try:
                return core.check_passphrase(secret)
            except OracleError as exc:
                self._warn(str(exc)); return None

        def new_oracle(self):
            title, ok = QInputDialog.getText(self, "New oracle", "Oracle title:")
            if not ok or not title.strip():
                return
            dest, _ = QFileDialog.getSaveFileName(self, "Save the encrypted oracle", "forecasts.envyoracle",
                                                  "Oracle (*.envyoracle);;All files (*)")
            if not dest:
                return
            secret = self._ask_passphrase(confirm=True)
            if secret is None:
                return
            try:
                if Path(dest).exists():
                    Path(dest).unlink()
                core.create(dest, title.strip(), secret)
                self._open_path(Path(dest), secret)
                self._warn("Oracle created. Add a prediction with a confidence to begin.",
                           icon=QMessageBox.Icon.Information, title="Oracle created")
            except (OracleError, OSError) as exc:
                self._warn(str(exc))

        def open_oracle(self):
            path, _ = QFileDialog.getOpenFileName(self, "Open an encrypted oracle", "",
                                                  "Oracle (*.envyoracle);;All files (*)")
            if not path:
                return
            secret = self._ask_passphrase(confirm=False)
            if secret is None:
                return
            self._open_path(Path(path), secret)

        def _open_path(self, path, secret):
            try:
                data, _ = core._load(path, secret)
                report = core.score(path, secret)
            except OracleError as exc:
                self._warn(str(exc)); return
            self.oracle, self.secret, self.data, self.report = path, secret, data, report
            self._set_enabled(True)
            self._refresh_all()

        def lock(self):
            self.oracle = self.secret = self.data = self.report = None
            for t in (self.t_preds, self.t_cal, self.t_history):
                t.setRowCount(0)
            self.mon_header.setPlainText("Locked. The passphrase was cleared from memory.")
            self.path_label.setText("")
            self._set_enabled(False)

        def _reopen(self, path):
            self._open_path(Path(path), self.secret)

        # ----- mutations ----------------------------------------------------
        def add_prediction(self):
            if not self.data:
                return
            statement, ok = QInputDialog.getText(self, "Add prediction", "What do you predict will happen?")
            if not ok or not statement.strip():
                return
            conf, ok = QInputDialog.getInt(self, "Confidence", "How confident? (1–99 percent)",
                                           60, core.MIN_CONFIDENCE, core.MAX_CONFIDENCE)
            if not ok:
                return
            days, ok = QInputDialog.getInt(self, "Resolve by", "Resolve within how many days? (0 = no date)",
                                           0, 0, 36500)
            if not ok:
                return
            due = None
            if days > 0:
                import datetime
                due = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)).isoformat()
            dest = self._next_revision_path()
            try:
                core.add_prediction(self.oracle, dest, self.secret, statement.strip(), conf, due)
            except (OracleError, OSError) as exc:
                self._warn(str(exc)); return
            self._reopen(dest)
            self.views.setCurrentIndex(0)

        def resolve_yes(self):
            self._resolve(True)

        def resolve_no(self):
            self._resolve(False)

        def _resolve(self, outcome):
            if not self.data:
                return
            pid, label, resolved = self._selected_prediction()
            if not pid:
                self._warn("Select an open prediction in the Predictions tab first."); return
            if resolved:
                self._warn("That prediction is already resolved."); return
            note, ok = QInputDialog.getText(self, "Resolve prediction",
                                            f"Resolving “{label}” as {'YES' if outcome else 'NO'}.\nNote (optional):")
            if not ok:
                return
            dest = self._next_revision_path()
            try:
                core.resolve(self.oracle, dest, self.secret, pid, outcome, note.strip())
            except (OracleError, OSError) as exc:
                self._warn(str(exc)); return
            self._reopen(dest)
            self.views.setCurrentIndex(1)

        def remove_prediction(self):
            if not self.data:
                return
            pid, label, _ = self._selected_prediction()
            if not pid:
                self._warn("Select a prediction in the Predictions tab first."); return
            if QMessageBox.question(self, "Remove prediction",
                                    f"Remove “{label}”?\n\nIts journal events are kept; only the prediction is "
                                    "removed. Removing resolved predictions changes your calibration.") \
                    != QMessageBox.StandardButton.Yes:
                return
            dest = self._next_revision_path()
            try:
                core.remove_prediction(self.oracle, dest, self.secret, pid)
            except (OracleError, OSError) as exc:
                self._warn(str(exc)); return
            self._reopen(dest)

        def export_report(self):
            if not self.data:
                return
            choice, ok = QInputDialog.getItem(self, "Export report", "Format:",
                                              ["HTML report", "JSON report"], 0, False)
            if not ok:
                return
            fmt = "html" if choice.startswith("HTML") else "json"
            dest, _ = QFileDialog.getSaveFileName(self, "Export calibration report",
                                                  f"oracle-report.{fmt}", f"{fmt.upper()} (*.{fmt});;All files (*)")
            if not dest:
                return
            try:
                if Path(dest).exists():
                    Path(dest).unlink()
                result = core.export(self.oracle, dest, self.secret, fmt)
            except (OracleError, OSError) as exc:
                self._warn(str(exc)); return
            self._warn(f"Exported a plaintext {fmt.upper()} report to {result['output']}.\n\n"
                       f"SHA-256: {result['sha256']}", icon=QMessageBox.Icon.Information, title="Report exported")

        # ----- selection ----------------------------------------------------
        def _selected_prediction(self):
            row = self.t_preds.currentRow()
            if row < 0:
                return None, None, None
            item = self.t_preds.item(row, 0)
            if item is None:
                return None, None, None
            return item.data(ID_ROLE), item.data(ID_ROLE + 1), item.data(ID_ROLE + 2)

        # ----- display ------------------------------------------------------
        def _refresh_all(self):
            self._refresh_header()
            self._fill_predictions(self.report)
            self._fill_calibration(self.report)
            self._fill_history(self.data["journal"])

        def _refresh_header(self):
            d, r = self.data, self.report
            if not d:
                return
            if r is None or r["brier"] is None:
                verdict = f"{r['open']} open · nothing resolved yet" if r else "—"
            else:
                verdict = (f"{r['resolved']} resolved · {r['accuracy_pct']}% accurate · Brier {r['brier']} · "
                           f"{r['open']} open ({r['overdue']} overdue)")
            self.mon_header.setPlainText(
                f"{d['title']}  ·  revision {d['revision']}  ·  {len(d['predictions'])} predictions  ·  {verdict}\n"
                f"oracle: {d['oracle_id']}")
            if self.oracle:
                self.path_label.setText(str(self.oracle))

        def _cell(self, text, color=None):
            item = QTableWidgetItem("" if text is None else str(text))
            if color:
                item.setForeground(QColor(color))
            return item

        def _fill_predictions(self, report):
            rows = report["predictions"] if report else []
            t = self.t_preds; t.setRowCount(len(rows))
            for i, p in enumerate(rows):
                state = p["state"]
                state_item = self._cell(STATE_LABEL[state], STATE_COLOR[state])
                state_item.setData(ID_ROLE, p["id"]); state_item.setData(ID_ROLE + 1, p["statement"])
                state_item.setData(ID_ROLE + 2, p["resolved"])
                t.setItem(i, 0, state_item)
                t.setItem(i, 1, self._cell(p["statement"], INK))
                t.setItem(i, 2, self._cell(f"{p['confidence']}%"))
                t.setItem(i, 3, self._cell(p["due_utc"] or "—"))
                if p["resolved"]:
                    out = ("yes" if p["outcome"] else "no") + (" ✓" if state == "correct" else " ✗")
                else:
                    out = "—"
                t.setItem(i, 4, self._cell(out, STATE_COLOR[state] if p["resolved"] else DIM))
            t.resizeColumnsToContents(); t.horizontalHeader().setStretchLastSection(True)
            if not rows:
                t.setRowCount(1)
                t.setItem(0, 0, self._cell("empty", DIM))
                t.setItem(0, 1, self._cell("No predictions yet. Use “Add prediction”."))

        def _fill_calibration(self, report):
            t = self.t_cal
            bins = report["bins"] if report else []
            t.setRowCount(len(bins))
            for i, b in enumerate(bins):
                gap = b["gap"]
                gap_color = GREEN if abs(gap) <= 10 else (AMBER if abs(gap) <= 25 else PINK)
                t.setItem(i, 0, self._cell(b["band"], CYAN))
                t.setItem(i, 1, self._cell(b["count"]))
                t.setItem(i, 2, self._cell(f"{b['you_said']}%"))
                t.setItem(i, 3, self._cell(f"{b['actual']}%"))
                t.setItem(i, 4, self._cell(f"{gap:+}", gap_color))
            t.resizeColumnsToContents(); t.horizontalHeader().setStretchLastSection(True)
            if not bins:
                t.setRowCount(1)
                t.setItem(0, 0, self._cell("—", DIM))
                t.setItem(0, 1, self._cell("Resolve some predictions to see your calibration."))

        def _fill_history(self, journal):
            t = self.t_history; t.setRowCount(len(journal))
            for i, r in enumerate(reversed(journal)):
                color = {"created": CYAN, "resolved": LAVENDER, "removed": DIM}.get(r["kind"], INK)
                t.setItem(i, 0, self._cell(r["recorded_utc"]))
                t.setItem(i, 1, self._cell(r["kind"], color))
                t.setItem(i, 2, self._cell(r["detail"]))
            t.resizeColumnsToContents(); t.horizontalHeader().setStretchLastSection(True)

        def _next_revision_path(self):
            base = self.oracle
            stem = base.stem
            if ".r" in stem and stem.rsplit(".r", 1)[-1].isdigit():
                stem = stem.rsplit(".r", 1)[0]
            rev = (self.data["revision"] + 1) if self.data else 1
            candidate = base.with_name(f"{stem}.r{rev}{base.suffix}")
            n = rev
            while candidate.exists():
                n += 1
                candidate = base.with_name(f"{stem}.r{n}{base.suffix}")
            return candidate

        def _set_enabled(self, on):
            self.views.setEnabled(on)

        def _warn(self, message, icon=None, title="Oracle"):
            box = QMessageBox(self); box.setWindowTitle(title)
            box.setIcon(icon or QMessageBox.Icon.Warning); box.setText(message); box.exec()

        def _animate(self):
            self.phase += 0.05
            self.header.tick(self.phase)

    return QApplication, Window


STYLE = f"""
#body {{ background: {BG}; }}
QWidget {{ color: {INK}; font-family: Helvetica, Arial, sans-serif; font-size: 13px; }}
#status {{ color: {MUTED}; font-size: 12px; }}
#foot {{ color: {DIM}; font-size: 11px; }}
#mono {{ background: {PANEL_HI}; border: 1px solid {LINE}; border-radius: 9px; color: {INK};
         font-family: "Courier New", monospace; font-size: 13px; padding: 8px; }}
QTableWidget {{ background: {PANEL_HI}; alternate-background-color: {PANEL}; border: 1px solid {LINE};
                border-radius: 10px; gridline-color: {LINE}; }}
QTableWidget::item {{ padding: 4px 6px; }}
QTableWidget::item:selected {{ background: {VIOLET}; color: {INK}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; padding: 6px 8px; border: none;
                        border-bottom: 1px solid {LINE}; font-weight: bold; }}
QTableCornerButton::section {{ background: {PANEL}; border: none; }}
QTabWidget#tabs::pane {{ border: 1px solid {LINE}; border-radius: 12px; background: {PANEL}; }}
QTabBar::tab {{ background: {PANEL_HI}; color: {MUTED}; padding: 8px 18px; margin-right: 6px;
                border-top-left-radius: 9px; border-top-right-radius: 9px; font-weight: bold; }}
QTabBar::tab:selected {{ color: {INK}; border-bottom: 2px solid {PINK}; }}
QPushButton#go {{ background: {PINK}; color: {BG}; border: none; border-radius: 10px; font-weight: bold; padding: 9px 14px; }}
QPushButton#go:hover {{ background: {CYAN}; }}
QPushButton#pick {{ background: {PANEL_HI}; border: 1px solid {LAVENDER}; border-radius: 9px; color: {INK};
                    font-weight: bold; padding: 9px 12px; }}
QPushButton#pick:hover {{ background: #1b2150; }}
"""


def run_app() -> int:
    try:
        from PyQt6.QtWidgets import QApplication  # noqa: F401
    except Exception:
        print("PyQt6 is required for the Oracle window. Use 'envy-oracle' subcommands instead.")
        return 2
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("No display found. Use 'envy-oracle init/add/resolve/score/export' in a terminal instead.")
        return 2
    try:
        QApplication, Window = _build()
        app = QApplication.instance() or QApplication(sys.argv)
        window = Window(); window.show()
        return app.exec()
    except Exception as exc:  # pragma: no cover
        print(f"Could not open the window: {exc}")
        return 2
