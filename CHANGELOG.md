# Changelog

All notable changes to this project. This project adheres to semantic versioning.

## v0.2.2 — 2026-11-16

- Attach a CycloneDX SBOM to each release (covered by the signed checksums)

## v0.2.1 — 2026-11-15

- Sign release checksums with Sigstore (keyless) and attach the `.sigstore.json` bundle
- Add property/fuzz hardening tests for the AES-256-GCM + scrypt envelope

## v0.2.0 — 2026-11-14

- Add Murphy's Brier decomposition to the calibration score
- Add Markdown export of a calibration report
- Add a due command: open predictions by resolve-by date
- Add a calibration reliability diagram to the HTML export
- Add tags to predictions, with tag-filtered scoring
- Add a Copy id action to the Predictions tab
- Docs: point install at the live v0.1.0 release

## v0.1.0

- Initial public release.
