#!/usr/bin/env python3
"""exposure-scan check: fail if the public console surface leaks personal PII,
private-workspace deep-links, or internal operational/legal detail.

Deterministic, stdlib-only, no network. Scans the Pages-served surface plus the
published runbooks and artifacts (including text members inside .zip/.tar.gz
downloads), and exits non-zero on any forbidden match.

The decision core (`scan_text`) is a pure function so it can be unit-tested
without touching the filesystem; see test_check_exposure.py.

Scope: this intentionally covers only content that ships to the public site.
CI scripts (.github/) and tests/ are excluded — including this file, whose
pattern strings would otherwise self-match.
"""
import re
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Directories/paths under ROOT to scan (public console surface only).
SCAN_DIRS = ["runbooks", "artifacts"]
SCAN_ROOT_FILES = [
    "index.html", "app.js", "catalog.json", "sw.js", "404.html",
    "manifest.webmanifest", "robots.txt", "README.md", "DEPLOYMENT.md",
]

TEXT_SUFFIXES = {
    ".html", ".js", ".json", ".css", ".md", ".txt", ".webmanifest",
    ".py", ".sh", ".yml", ".yaml", ".go", ".csv", ".jsonl", ".j2", ".tf",
    ".mjs", ".toml", ".cfg", ".ini", ".xml", ".svg",
}
ARCHIVE_SUFFIXES = {".zip", ".tgz"}  # .tar.gz handled by name check

# (name, severity, compiled pattern). A single match fails the build.
PATTERNS = [
    # --- Personal PII -----------------------------------------------------
    # Free-mail inboxes are personal; role aliases on example/company domains
    # are fine. example.* is explicitly allowed as a placeholder.
    ("personal-freemail-address", "PII", re.compile(
        r"[A-Za-z0-9._%+-]+@(?:gmail|yahoo|ymail|hotmail|outlook|live|msn|"
        r"icloud|me|mac|proton|protonmail|aol|gmx|zoho)\.[A-Za-z.]{2,}",
        re.IGNORECASE)),

    # --- Private collaboration workspace deep-links -----------------------
    # Flag the logged-in app host and any notion.so page link carrying a
    # 32-hex page id. Allowed: api.notion.com (tooling) and generic product
    # pages like notion.so/my-integrations or notion.so/profile/integrations.
    ("notion-workspace-link", "PRIVATE-LINK",
     re.compile(r"\bapp\.notion\.com/|\bnotion\.so/\S*[0-9a-f]{32}",
                re.IGNORECASE)),
    ("asana-workspace-link", "PRIVATE-LINK",
     re.compile(r"\bapp\.asana\.com/", re.IGNORECASE)),
    ("atlassian-workspace-link", "PRIVATE-LINK",
     re.compile(r"\b[a-z0-9-]+\.atlassian\.net/", re.IGNORECASE)),
    ("slack-archive-link", "PRIVATE-LINK",
     re.compile(r"\b[a-z0-9-]+\.slack\.com/archives/", re.IGNORECASE)),

    # --- Operational authority-gap / DNS-control disclosures --------------
    ("vercel-authority-gap", "OPS-DISCLOSURE",
     re.compile(r"recover\s+vercel\s+authority|owning\s+vercel\s+scope|"
                r"owner-authorized\s+project\s+scope", re.IGNORECASE)),
    ("unregistered-domain-plan", "OPS-DISCLOSURE",
     re.compile(r"winfieldchronicles\.com", re.IGNORECASE)),

    # --- Proprietary legal / IP governance --------------------------------
    ("legal-governance-detail", "LEGAL", re.compile(
        r"schedule\s+a\s+property\s+inventory|counsel[- ]facing\s+rights|"
        r"invention\s+disclosure\s+packet|fictional-composite\s+disclosure|"
        r"recovery-c\s+hold", re.IGNORECASE)),
]


def scan_text(text: str):
    """Pure decision core. Returns a list of (pattern_name, severity, snippet)."""
    findings = []
    for name, severity, rx in PATTERNS:
        m = rx.search(text)
        if m:
            s = m.group(0)
            if len(s) > 80:
                s = s[:77] + "..."
            findings.append((name, severity, s))
    return findings


def _iter_archive_texts(path: Path):
    """Yield (member_name, text) for text members inside an archive."""
    name = path.name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if Path(info.filename).suffix.lower() not in TEXT_SUFFIXES:
                        continue
                    with zf.open(info) as fh:
                        yield info.filename, fh.read().decode("utf-8", "ignore")
        elif name.endswith(".tar.gz") or name.endswith(".tgz"):
            with tarfile.open(path, "r:gz") as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    if Path(member.name).suffix.lower() not in TEXT_SUFFIXES:
                        continue
                    fh = tf.extractfile(member)
                    if fh is None:
                        continue
                    yield member.name, fh.read().decode("utf-8", "ignore")
    except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
        print(f"  ! could not read archive {path.name}: {exc}")


def _candidate_files():
    for rel in SCAN_ROOT_FILES:
        p = ROOT / rel
        if p.is_file():
            yield p
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if p.is_file():
                yield p


def main() -> int:
    findings = []  # (location, pattern_name, severity, snippet)
    scanned = 0

    for path in _candidate_files():
        rel = path.relative_to(ROOT).as_posix()
        suffix = path.suffix.lower()
        is_targz = path.name.lower().endswith(".tar.gz")
        if suffix in ARCHIVE_SUFFIXES or is_targz:
            for member, text in _iter_archive_texts(path):
                scanned += 1
                for name, sev, snip in scan_text(text):
                    findings.append((f"{rel}!{member}", name, sev, snip))
        elif suffix in TEXT_SUFFIXES:
            scanned += 1
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                print(f"  ! could not read {rel}: {exc}")
                continue
            for name, sev, snip in scan_text(text):
                findings.append((rel, name, sev, snip))

    if findings:
        print(f"exposure-scan: FAIL ({len(findings)} issue(s) in {scanned} files)")
        for loc, name, sev, snip in findings:
            print(f"  - [{sev}] {name} in {loc}: {snip!r}")
        return 1

    print(f"exposure-scan: OK (no forbidden patterns in {scanned} scanned files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
