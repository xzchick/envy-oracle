"""Encrypted forecasts and calibration scoring: hold yourself to your predictions.

A self-contained engine for the ENVY BLACK VEIL Oracle app, packaged as a
standalone app. An *oracle* holds *predictions* — a statement with a stated
confidence from 1 to 99 percent and an optional due date — sealed in the same
AES-256-GCM envelope as ENVY Vault. You *resolve* each one yes or no when you
know. A *score* read measures how well your stated confidence matched reality:
your Brier score, accuracy, and a calibration table by confidence band.

Every save is a new, hash-chained revision written with ``O_EXCL`` — an earlier
revision is never overwritten — and every creation and resolution is recorded in
a SHA-256 hash-chained journal.

Honest by design: the Oracle does not foretell anything. It scores the
confidence you stated against the outcomes you resolved, nothing more. You
resolve your own predictions, calibration needs volume to mean much, and the
Brier score is only arithmetic on numbers you chose.

Standard library only, apart from ``cryptography`` for the encrypted envelope.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import json
import os
import stat
import uuid
from pathlib import Path

# --- Shared errors and bounds ------------------------------------------------

MAGIC = b"ENVYVAULT2\x00"
MAX_ENVELOPE = 16 * 1024 * 1024        # the encrypted oracle
MAX_PREDICTIONS = 50000
MAX_JOURNAL = 200000
MAX_REVISIONS = 1000000
MIN_PASSPHRASE = 12
MIN_CONFIDENCE, MAX_CONFIDENCE = 1, 99     # never 0 or 100: no certainty
MAX_STATEMENT = 500
MAX_NOTE = 500


class OracleError(RuntimeError):
    """A clean, expected error: a bad passphrase, an invalid oracle, a chain mismatch."""


# --- Safe file primitives (never follow symlinks, never overwrite) -----------

def read_regular(path, limit: int = MAX_ENVELOPE) -> bytes:
    path = Path(path).expanduser()
    fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_size > limit:
            raise OracleError("Expected a regular file within the size limit")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OracleError("File exceeded the size limit")
    return data


def new_file(path, data: bytes, mode: int = 0o600) -> Path:
    """Write a brand-new file. O_EXCL means an existing file is never overwritten."""
    path = Path(path).expanduser()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    except FileExistsError:
        raise OracleError(f"Refusing to overwrite an existing file: {path}") from None
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    return path


# --- Encrypted envelope (byte-compatible with ENVY Vault) --------------------

def _key(secret: bytes, salt: bytes) -> bytes:
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(secret)


def encrypt_bytes(data: bytes, secret: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt, nonce = os.urandom(16), os.urandom(12)
    return MAGIC + salt + nonce + AESGCM(_key(secret, salt)).encrypt(nonce, data, MAGIC)


def decrypt_bytes(data: bytes, secret: bytes) -> bytes:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not data.startswith(MAGIC) or len(data) < len(MAGIC) + 44:
        raise OracleError("Not an ENVY encrypted envelope")
    blob = data[len(MAGIC):]
    try:
        return AESGCM(_key(secret, blob[:16])).decrypt(blob[16:28], blob[28:], MAGIC)
    except InvalidTag:
        raise OracleError("Incorrect passphrase or modified encrypted data") from None


def check_passphrase(secret: bytes) -> bytes:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < MIN_PASSPHRASE:
        raise OracleError(f"Use a passphrase of at least {MIN_PASSPHRASE} bytes")
    return bytes(secret)


# --- Canonical form and field validation -------------------------------------

def _canonical(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_utc(value, field="recorded_utc"):
    if not isinstance(value, str) or len(value) > 64:
        raise OracleError("Invalid " + field)
    try:
        parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise OracleError(field + " must be ISO 8601 with a UTC offset") from None
    if parsed.tzinfo is None or parsed.utcoffset() != datetime.timedelta(0):
        raise OracleError(field + " must be expressed in UTC")
    return parsed


def _timestamp(value, field="recorded_utc"):
    return _parse_utc(value, field).isoformat()


def _text(value, field="text", limit=200, allow_empty=False):
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise OracleError("Invalid " + field)
    if not allow_empty and not value.strip():
        raise OracleError("Invalid " + field)
    return value


def _uuid(value, field="identifier"):
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise OracleError("Invalid " + field) from None
    return value


def _sha256(value, field="sha256"):
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise OracleError(field + " must be 64 lowercase hexadecimal characters")
    return value


def _confidence(value):
    if type(value) is not int or not MIN_CONFIDENCE <= value <= MAX_CONFIDENCE:
        raise OracleError(f"Confidence must be a whole percent from {MIN_CONFIDENCE} to {MAX_CONFIDENCE}")
    return value


# --- Prediction and journal validation ---------------------------------------

MAX_TAGS = 20
MAX_TAG_LEN = 40


def _normalize_tags(tags):
    """Lower-cased, trimmed, de-duplicated, sorted tags. Non-letters are allowed,
    but a tag is a short label, not free text."""
    if tags is None:
        return []
    if not isinstance(tags, list) or len(tags) > MAX_TAGS:
        raise OracleError(f"At most {MAX_TAGS} tags, as a list")
    out = []
    for tag in tags:
        if not isinstance(tag, str):
            raise OracleError("Each tag must be text")
        label = tag.strip().lower()
        if not label or "\x00" in label or len(label) > MAX_TAG_LEN:
            raise OracleError(f"A tag must be 1 to {MAX_TAG_LEN} characters")
        out.append(label)
    return sorted(set(out))


def _validate_prediction(value):
    fields = {"id", "statement", "confidence", "created_utc", "due_utc",
              "resolved", "outcome", "resolved_utc", "note", "tags"}
    if not isinstance(value, dict) or set(value) != fields:
        raise OracleError("Invalid prediction")
    resolved = value["resolved"]
    if resolved not in (True, False):
        raise OracleError("Invalid resolved flag")
    outcome, resolved_utc = value["outcome"], value["resolved_utc"]
    if resolved:
        if outcome not in (True, False):
            raise OracleError("A resolved prediction needs a boolean outcome")
        _timestamp(resolved_utc, "resolved_utc")
    else:
        if outcome is not None or resolved_utc is not None:
            raise OracleError("An open prediction has no outcome")
    due = value["due_utc"]
    if due is not None:
        due = _timestamp(due, "due_utc")
    return {"id": _uuid(value["id"], "prediction id"),
            "statement": _text(value["statement"], "statement", MAX_STATEMENT),
            "confidence": _confidence(value["confidence"]),
            "created_utc": _timestamp(value["created_utc"], "created_utc"),
            "due_utc": due, "resolved": resolved,
            "outcome": outcome if resolved else None,
            "resolved_utc": resolved_utc if resolved else None,
            "note": _text(value["note"], "note", MAX_NOTE, allow_empty=True),
            "tags": _normalize_tags(value["tags"])}


def _record_digest(record):
    core = {k: v for k, v in record.items() if k != "record_sha256"}
    return hashlib.sha256(_canonical(core)).hexdigest()


def _validate_record(value):
    fields = {"recorded_utc", "kind", "prediction_id", "detail", "previous_sha256", "record_sha256"}
    if not isinstance(value, dict) or set(value) != fields:
        raise OracleError("Invalid journal record")
    if value["kind"] not in ("created", "resolved", "removed"):
        raise OracleError("Invalid journal event kind")
    return {"recorded_utc": _timestamp(value["recorded_utc"]),
            "kind": value["kind"],
            "prediction_id": _uuid(value["prediction_id"], "prediction id"),
            "detail": _text(value["detail"], "detail", 200, allow_empty=True),
            "previous_sha256": _sha256(value["previous_sha256"], "previous_sha256"),
            "record_sha256": _sha256(value["record_sha256"], "record_sha256")}


def _validate_oracle(data):
    fields = {"schema", "oracle_id", "title", "created_utc", "revision",
              "previous_envelope_sha256", "predictions", "journal"}
    if not isinstance(data, dict) or set(data) != fields or type(data["schema"]) is not int or data["schema"] != 1:
        raise OracleError("Unsupported encrypted oracle schema")
    _text(data["title"], "oracle title", 200)
    _uuid(data["oracle_id"], "oracle identifier")
    _timestamp(data["created_utc"], "created_utc")

    predictions = data["predictions"]
    if not isinstance(predictions, list) or len(predictions) > MAX_PREDICTIONS:
        raise OracleError("Invalid prediction list")
    seen, previous_id = set(), None
    for pred in predictions:
        if _validate_prediction(pred) != pred:
            raise OracleError("Prediction is not normalized")
        if pred["id"] in seen:
            raise OracleError("Duplicate prediction id")
        if previous_id is not None and pred["id"] < previous_id:
            raise OracleError("Predictions must be sorted by id")
        seen.add(pred["id"]); previous_id = pred["id"]

    journal = data["journal"]
    if not isinstance(journal, list) or len(journal) > MAX_JOURNAL:
        raise OracleError("Invalid journal")
    previous = "0" * 64
    for record in journal:
        if _validate_record(record) != record:
            raise OracleError("Journal record is not normalized")
        if record["previous_sha256"] != previous or _record_digest(record) != record["record_sha256"]:
            raise OracleError("Oracle journal hash chain mismatch")
        previous = record["record_sha256"]

    revision = data["revision"]
    if type(revision) is not int or not 0 <= revision <= MAX_REVISIONS:
        raise OracleError("Invalid revision")
    _sha256(data["previous_envelope_sha256"], "previous_envelope_sha256")
    return data


# --- Load / write ------------------------------------------------------------

def _migrate(data):
    """Bring an older oracle forward in memory. Predictions gained `tags` in v0.2;
    give any that predate it an empty list so they still validate. The journal is
    unaffected (predictions are not part of the hash chain)."""
    if isinstance(data, dict) and isinstance(data.get("predictions"), list):
        for pred in data["predictions"]:
            if isinstance(pred, dict) and "tags" not in pred:
                pred["tags"] = []
    return data


def _load(source, secret):
    encrypted = read_regular(source, MAX_ENVELOPE)
    try:
        data = json.loads(decrypt_bytes(encrypted, secret))
    except (json.JSONDecodeError, UnicodeError, RecursionError):
        raise OracleError("Encrypted oracle does not contain valid JSON") from None
    return _validate_oracle(_migrate(data)), hashlib.sha256(encrypted).hexdigest()


def _write(destination, data, secret):
    check_passphrase(secret)
    encoded = _canonical(_validate_oracle(data))
    if len(encoded) + 128 > MAX_ENVELOPE:
        raise OracleError("Oracle exceeds the encrypted size limit")
    new_file(destination, encrypt_bytes(encoded, secret))
    return {"output": str(destination), "oracle_id": data["oracle_id"], "revision": data["revision"],
            "predictions": len(data["predictions"]), "encrypted": True, "previous_revision_preserved": True}


def _append_event(data, kind, prediction_id, detail):
    record = {"recorded_utc": _utc(), "kind": kind, "prediction_id": prediction_id,
              "detail": _text(detail, "detail", 200, allow_empty=True),
              "previous_sha256": data["journal"][-1]["record_sha256"] if data["journal"] else "0" * 64}
    record["record_sha256"] = _record_digest(record)
    data["journal"].append(_validate_record(record))


def _save_revision(data, previous_envelope, destination, secret):
    data["revision"] += 1
    data["previous_envelope_sha256"] = previous_envelope
    return _write(destination, data, secret)


def create(destination, title, secret):
    data = {"schema": 1, "oracle_id": str(uuid.uuid4()), "title": _text(title, "oracle title", 200),
            "created_utc": _utc(), "revision": 0, "previous_envelope_sha256": "0" * 64,
            "predictions": [], "journal": []}
    return _write(destination, data, secret)


# --- Mutations (each writes a new, preserved revision) -----------------------

def add_prediction(source, destination, secret, statement, confidence, due_utc=None, tags=None):
    data, envelope = _load(source, secret)
    if len(data["predictions"]) >= MAX_PREDICTIONS:
        raise OracleError(f"An oracle holds at most {MAX_PREDICTIONS} predictions")
    pred = _validate_prediction({"id": str(uuid.uuid4()), "statement": statement,
                                 "confidence": confidence, "created_utc": _utc(),
                                 "due_utc": _timestamp(due_utc) if due_utc else None,
                                 "resolved": False, "outcome": None, "resolved_utc": None,
                                 "note": "", "tags": tags or []})
    data["predictions"] = sorted(data["predictions"] + [pred], key=lambda p: p["id"])
    _append_event(data, "created", pred["id"], f"{confidence}% — {statement[:120]}")
    result = _save_revision(data, envelope, destination, secret)
    return dict(result, prediction_id=pred["id"])


def resolve(source, destination, secret, prediction_id, outcome, note=""):
    data, envelope = _load(source, secret)
    prediction_id = _uuid(prediction_id, "prediction id")
    if outcome not in (True, False):
        raise OracleError("Outcome must be yes (True) or no (False)")
    pred = next((p for p in data["predictions"] if p["id"] == prediction_id), None)
    if pred is None:
        raise OracleError("No such prediction in this oracle")
    if pred["resolved"]:
        raise OracleError("That prediction is already resolved")
    pred["resolved"] = True
    pred["outcome"] = outcome
    pred["resolved_utc"] = _utc()
    pred["note"] = _text(note, "note", MAX_NOTE, allow_empty=True)
    _validate_prediction(pred)
    _append_event(data, "resolved", prediction_id, "yes" if outcome else "no")
    result = _save_revision(data, envelope, destination, secret)
    return dict(result, resolved=prediction_id, outcome=outcome)


def remove_prediction(source, destination, secret, prediction_id):
    data, envelope = _load(source, secret)
    prediction_id = _uuid(prediction_id, "prediction id")
    kept = [p for p in data["predictions"] if p["id"] != prediction_id]
    if len(kept) == len(data["predictions"]):
        raise OracleError("No such prediction in this oracle")
    data["predictions"] = kept   # the journal keeps the created/resolved events
    _append_event(data, "removed", prediction_id, "")
    result = _save_revision(data, envelope, destination, secret)
    return dict(result, removed=prediction_id)


# --- Scoring / calibration ---------------------------------------------------

def _is_correct(confidence, outcome):
    """A prediction leans yes at >=50%. Correct if the lean matched the outcome."""
    return (confidence >= 50) == bool(outcome)


def _brier_decomposition(resolved):
    """Murphy's three-term decomposition of the Brier score over resolved predictions.

    Forecasts are grouped by their exact stated confidence (not the display deciles),
    so every forecast within a group is identical and the identity

        Brier = reliability - resolution + uncertainty

    holds exactly. Returns None when nothing is resolved.

    - **reliability** (lower is better): how far your stated confidence sits from the
      outcome frequency at that confidence — pure calibration error.
    - **resolution** (higher is better): how much your confidence levels separate
      outcomes from the overall base rate — your power to discriminate.
    - **uncertainty**: the base-rate variance of the outcomes themselves, which no
      forecaster can reduce; it is a property of what you chose to predict, not of you.
    """
    if not resolved:
        return None
    n = len(resolved)
    base_rate = sum(1 for p in resolved if p["outcome"]) / n
    groups = {}
    for p in resolved:
        g = groups.setdefault(p["confidence"], {"n": 0, "yes": 0})
        g["n"] += 1
        g["yes"] += 1 if p["outcome"] else 0
    reliability = resolution = 0.0
    for conf, g in groups.items():
        forecast = conf / 100.0
        observed = g["yes"] / g["n"]
        reliability += g["n"] * (forecast - observed) ** 2
        resolution += g["n"] * (observed - base_rate) ** 2
    reliability /= n
    resolution /= n
    uncertainty = base_rate * (1.0 - base_rate)
    return {"reliability": round(reliability, 4), "resolution": round(resolution, 4),
            "uncertainty": round(uncertainty, 4), "base_rate_pct": round(100.0 * base_rate, 1),
            "recomposed_brier": round(reliability - resolution + uncertainty, 4),
            "note": "Brier = reliability − resolution + uncertainty. Reliability is your "
                    "calibration error (lower is better); resolution is how well your "
                    "confidence separates outcomes (higher is better); uncertainty is the "
                    "base-rate variance of what you chose to predict, which no forecaster "
                    "can lower."}


def score(source, secret, now=None, tag=None):
    """Measure calibration over resolved predictions. Read-only.

    With `tag`, the analysis is restricted to predictions carrying that tag, so
    you can ask "how calibrated am I on *security* calls?" on its own.
    """
    data, _ = _load(source, secret)
    moment = _parse_utc(now) if now else datetime.datetime.now(datetime.timezone.utc)
    preds = data["predictions"]
    if tag is not None:
        tag = tag.strip().lower()
        preds = [p for p in preds if tag in p["tags"]]
    resolved = [p for p in preds if p["resolved"]]
    open_preds = [p for p in preds if not p["resolved"]]
    overdue = [p for p in open_preds if p["due_utc"] and _parse_utc(p["due_utc"]) < moment]

    brier = correct = None
    bins = []
    if resolved:
        total, hits = 0.0, 0
        buckets = {}
        for p in resolved:
            prob = p["confidence"] / 100.0
            obs = 1.0 if p["outcome"] else 0.0
            total += (prob - obs) ** 2
            if _is_correct(p["confidence"], p["outcome"]):
                hits += 1
            lo = (p["confidence"] // 10) * 10
            b = buckets.setdefault(lo, {"n": 0, "yes": 0, "conf_sum": 0})
            b["n"] += 1
            b["yes"] += 1 if p["outcome"] else 0
            b["conf_sum"] += p["confidence"]
        brier = round(total / len(resolved), 4)
        correct = hits
        for lo in sorted(buckets):
            b = buckets[lo]
            you_said = round(b["conf_sum"] / b["n"], 1)
            actual = round(100.0 * b["yes"] / b["n"], 1)
            bins.append({"band": f"{lo}–{lo + 9}%", "count": b["n"],
                         "you_said": you_said, "actual": actual,
                         "gap": round(actual - you_said, 1)})
    accuracy = round(100.0 * correct / len(resolved), 1) if resolved else None
    return {"oracle_id": data["oracle_id"], "title": data["title"], "revision": data["revision"],
            "evaluated_utc": moment.isoformat(), "tag": tag,
            "total": len(preds), "resolved": len(resolved), "open": len(open_preds),
            "overdue": len(overdue), "correct": correct, "accuracy_pct": accuracy, "brier": brier,
            "brier_decomposition": _brier_decomposition(resolved),
            "bins": bins, "predictions": _rows(preds, moment),
            "basis": "The Oracle scores the confidence you stated against the outcomes you resolved — it does "
                     "not foretell anything. Calibration needs volume to mean much, and you resolve your own "
                     "predictions. A Brier score of 0.25 is what always saying 50% earns; lower is better.",
            "brier_reference": 0.25}


def due(source, secret, now=None, within_days=None):
    """Open predictions that have a resolve-by date, soonest first, flagging overdue
    ones. With `within_days`, only those due within that many days (overdue always
    included). Read-only."""
    data, _ = _load(source, secret)
    moment = _parse_utc(now) if now else datetime.datetime.now(datetime.timezone.utc)
    rows = []
    for p in data["predictions"]:
        if p["resolved"] or not p["due_utc"]:
            continue
        due_dt = _parse_utc(p["due_utc"])
        days = int((due_dt - moment).total_seconds() // 86400) if due_dt >= moment \
            else -int((moment - due_dt).total_seconds() // 86400)
        if within_days is not None and days > within_days:
            continue
        rows.append({"id": p["id"], "statement": p["statement"], "confidence": p["confidence"],
                     "due_utc": p["due_utc"], "days": days, "overdue": due_dt < moment, "tags": p["tags"]})
    rows.sort(key=lambda r: (r["due_utc"], r["id"]))
    return {"oracle_id": data["oracle_id"], "title": data["title"], "evaluated_utc": moment.isoformat(),
            "due": rows, "count": len(rows), "overdue": sum(1 for r in rows if r["overdue"]),
            "basis": "Open predictions with a resolve-by date you set. A due date is a reminder, not an "
                     "enforcement, and times are assertions from your clock."}


def tags(source, secret):
    """Every tag in use, with how many predictions carry each. Read-only."""
    data, _ = _load(source, secret)
    counts = {}
    for p in data["predictions"]:
        for tag in p["tags"]:
            counts[tag] = counts.get(tag, 0) + 1
    return {"oracle_id": data["oracle_id"], "title": data["title"],
            "tags": [{"tag": tag, "count": counts[tag]} for tag in sorted(counts)]}


def _rows(predictions, moment):
    rows = []
    for p in predictions:
        if p["resolved"]:
            state = "correct" if _is_correct(p["confidence"], p["outcome"]) else "wrong"
        elif p["due_utc"] and _parse_utc(p["due_utc"]) < moment:
            state = "overdue"
        else:
            state = "open"
        rows.append({**p, "state": state})
    order = {"overdue": 0, "open": 1, "wrong": 2, "correct": 3}
    rows.sort(key=lambda r: (order[r["state"]], r["created_utc"]))
    return rows


def inspect(source, secret):
    data, envelope = _load(source, secret)
    return {"oracle_id": data["oracle_id"], "title": data["title"], "revision": data["revision"],
            "created_utc": data["created_utc"], "envelope_sha256": envelope,
            "predictions": data["predictions"],
            "journal": [{"recorded_utc": r["recorded_utc"], "kind": r["kind"],
                         "prediction_id": r["prediction_id"], "detail": r["detail"]} for r in data["journal"]],
            "warning": "The journal is append-only and hash-chained, so creations and resolutions cannot be "
                       "reordered or edited without breaking the chain — but you resolve your own predictions, "
                       "and the chain orders events, it does not prove when they happened."}


# --- Plaintext export --------------------------------------------------------

def _calibration_svg(bins):
    """An inline SVG reliability diagram: stated confidence (x) against the actual
    rate things came true (y). The dashed diagonal is perfect calibration; a point
    above it means you were under-confident, below it over-confident. Point area
    grows with how many predictions fall in that band. No script — it renders under
    the export's strict Content-Security-Policy."""
    w = h = 280
    pad = 38
    span = w - 2 * pad

    def sx(v):
        return pad + span * (v / 100.0)

    def sy(v):
        return h - pad - span * (v / 100.0)

    maxn = max((b["count"] for b in bins), default=1) or 1
    points = "".join(
        f'<circle cx="{sx(b["you_said"]):.1f}" cy="{sy(b["actual"]):.1f}" '
        f'r="{3 + 7 * (b["count"] / maxn):.1f}" fill="#ff3db4" fill-opacity="0.72" stroke="#8547e8"/>'
        for b in bins)
    ticks = "".join(
        f'<text x="{sx(v):.1f}" y="{h - pad + 15}" font-size="10" text-anchor="middle" fill="#666">{v}</text>'
        f'<text x="{pad - 8}" y="{sy(v) + 4:.1f}" font-size="10" text-anchor="end" fill="#666">{v}</text>'
        for v in (0, 50, 100))
    return (
        f'<svg viewBox="0 0 {w} {h}" width="340" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Calibration reliability diagram: stated confidence versus actual rate">'
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#ffffff" stroke="#ccc"/>'
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{pad}" stroke="#8547e8" stroke-dasharray="5 4"/>'
        f'<line x1="{pad}" y1="{h - pad}" x2="{w - pad}" y2="{h - pad}" stroke="#333"/>'
        f'<line x1="{pad}" y1="{h - pad}" x2="{pad}" y2="{pad}" stroke="#333"/>'
        f'{ticks}{points}'
        f'<text x="{w / 2:.0f}" y="{h - 6}" font-size="11" text-anchor="middle" fill="#444">stated confidence %</text>'
        f'<text x="12" y="{h / 2:.0f}" font-size="11" text-anchor="middle" fill="#444" '
        f'transform="rotate(-90 12 {h / 2:.0f})">actual %</text></svg>')


def export(source, destination, secret, format="html", now=None):
    if format not in ("html", "json", "md"):
        raise OracleError("Export format must be html, json or md")
    data, envelope = _load(source, secret)
    report = score(source, secret, now)
    out = {"oracle": {"oracle_id": data["oracle_id"], "title": data["title"], "revision": data["revision"],
                      "created_utc": data["created_utc"]},
           "evaluated_utc": report["evaluated_utc"], "total": report["total"], "resolved": report["resolved"],
           "open": report["open"], "overdue": report["overdue"], "correct": report["correct"],
           "accuracy_pct": report["accuracy_pct"], "brier": report["brier"],
           "brier_decomposition": report["brier_decomposition"], "bins": report["bins"],
           "predictions": report["predictions"], "envelope_sha256": envelope,
           "limitations": "Plaintext export. The Oracle scores stated confidence against resolved outcomes "
                          "only; it does not foretell anything, calibration needs volume, you resolve your own "
                          "predictions, and times are assertions from the operator's clock."}
    if format == "json":
        output = (json.dumps(out, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    elif format == "md":
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

        def md_outcome(p):
            if not p["resolved"]:
                return "overdue" if p["state"] == "overdue" else "open"
            return ("yes" if p["outcome"] else "no") + (" ✓" if p["state"] == "correct" else " ✗")

        headline = ("No resolved predictions yet — nothing to score." if report["brier"] is None else
                    f"{report['resolved']} resolved · {report['accuracy_pct']}% accurate · "
                    f"Brier {report['brier']} (0.25 = always 50%)")
        lines = [f"# {data['title']}", "", out["limitations"], "",
                 f"- **Oracle:** `{data['oracle_id']}`",
                 f"- **Revision:** {data['revision']}",
                 f"- **Evaluated:** {report['evaluated_utc']}",
                 f"- {headline}", "",
                 "## Calibration by confidence band", ""]
        if report["bins"]:
            lines += ["| Band | Predictions | You said | Actual | Gap |", "| --- | --- | --- | --- | --- |"]
            for b in report["bins"]:
                lines.append(f"| {b['band']} | {b['count']} | {b['you_said']}% | {b['actual']}% | {b['gap']:+} |")
        else:
            lines.append("_No resolved predictions yet._")
        lines += ["", f"## Predictions ({report['total']})", ""]
        if report["predictions"]:
            lines += ["| State | Statement | Confidence | Due | Outcome |", "| --- | --- | --- | --- | --- |"]
            for p in report["predictions"]:
                lines.append(f"| {p['state']} | {cell(p['statement'])} | {p['confidence']}% | "
                             f"{p['due_utc'] or '—'} | {md_outcome(p)} |")
        else:
            lines.append("_No predictions yet._")
        output = ("\n".join(lines) + "\n").encode("utf-8")
    else:
        def esc(v):
            return html.escape(str(v), quote=True)

        def outcome_text(p):
            if not p["resolved"]:
                return "overdue" if p["state"] == "overdue" else "open"
            return ("yes" if p["outcome"] else "no") + (" ✓" if p["state"] == "correct" else " ✗")

        rows = "".join(
            "<tr><td>" + esc(p["state"]) + "</td><td>" + esc(p["statement"]) + "</td><td>" +
            esc(p["confidence"]) + "%</td><td>" + esc(p["due_utc"] or "—") + "</td><td>" +
            outcome_text(p) + "</td></tr>" for p in report["predictions"])
        cal = "".join(
            "<tr><td>" + esc(b["band"]) + "</td><td>" + esc(b["count"]) + "</td><td>" + esc(b["you_said"]) +
            "%</td><td>" + esc(b["actual"]) + "%</td><td>" + esc(b["gap"]) + "</td></tr>" for b in report["bins"])
        chart = ("<h3>Reliability diagram</h3><p>" + _calibration_svg(report["bins"]) +
                 "</p>") if report["bins"] else ""
        headline = ("No resolved predictions yet — nothing to score."
                    if report["brier"] is None else
                    f"{report['resolved']} resolved · {report['accuracy_pct']}% accurate · "
                    f"Brier {report['brier']} (0.25 = always 50%)")
        # No external content, executable script, clickable operator-supplied URLs, or active HTML.
        output = ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
                  "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'\">"
                  "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                  "<title>" + esc(data["title"]) + "</title><style>body{max-width:74rem;margin:2rem auto;padding:0 1rem;font:16px system-ui;line-height:1.5}"
                  "table{border-collapse:collapse;width:100%;margin:0 0 2rem}th,td{border:1px solid #777;padding:.4rem .6rem;text-align:left;vertical-align:top}"
                  "th{background:#eee}</style></head><body><h1>" + esc(data["title"]) +
                  "</h1><p>" + esc(out["limitations"]) + "</p><p>Oracle: " + esc(data["oracle_id"]) +
                  " · Revision: " + str(data["revision"]) + " · Evaluated UTC: " + esc(report["evaluated_utc"]) +
                  " · Encrypted oracle SHA-256: " + envelope + "</p><h2>" + esc(headline) + "</h2>"
                  "<h3>Calibration by confidence band</h3>"
                  "<table><thead><tr><th>Band</th><th>Predictions</th><th>You said</th><th>Actual</th><th>Gap</th></tr></thead><tbody>"
                  + (cal or "<tr><td colspan=\"5\">No resolved predictions yet.</td></tr>") + "</tbody></table>"
                  + chart +
                  "<h3>Predictions (" + str(report["total"]) + ")</h3>"
                  "<table><thead><tr><th>State</th><th>Statement</th><th>Confidence</th><th>Due</th><th>Outcome</th></tr></thead><tbody>"
                  + rows + "</tbody></table></body></html>").encode("utf-8")
    new_file(destination, output)
    return {"output": str(destination), "format": format, "plaintext": True,
            "sha256": hashlib.sha256(output).hexdigest(),
            "resolved": report["resolved"], "brier": report["brier"]}
