#!/usr/bin/env python3
"""pages-preflight: guard the GitHub Pages custom-domain publish of
console.odisena.com against silent breakage.

The live console at https://console.odisena.com/ is served by GitHub Pages
directly from the `main` branch root. That binding depends on a handful of
committed, load-bearing files. If any of them is deleted or edited, Pages
silently drops the custom domain back to the `*.github.io` default. This check
fails the build before such a change can merge.

Two independent layers:

  Offline (default, deterministic, no network) — validates the committed repo
  invariants:
    - CNAME exists, is a single line, and equals the expected custom domain
      exactly (no scheme, no trailing slash, no stray whitespace).
    - .nojekyll exists at the repo root (Jekyll processing disabled).
    - index.html exists (Pages entry document).
    - 404.html exists (custom not-found page).

  Live (--live, opt-in, read-only) — queries the GitHub Pages API via `gh api`
  and cross-checks it against the committed state:
    - source branch == main, source path == /
    - live cname == the committed CNAME value
    - https_enforced is true
  The live layer never mutates anything and degrades gracefully (SKIP, not
  FAIL) when `gh` is unavailable, unauthenticated, or offline, so it is safe to
  run in CI or locally.

The offline decision logic (`evaluate`) is a pure function so it can be
unit-tested without a filesystem or network; see test_pages_preflight.py.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

EXPECTED_DOMAIN = "console.odisena.com"
EXPECTED_BRANCH = "main"
EXPECTED_PATH = "/"

# Committed files that the Pages custom-domain publish depends on.
REQUIRED_FILES = ["CNAME", ".nojekyll", "index.html", "404.html"]


def evaluate(cname_raw, present_files, expected_domain=EXPECTED_DOMAIN):
    """Pure offline decision.

    Args:
      cname_raw: full text of the CNAME file, or None if the file is absent.
      present_files: iterable of repo-relative paths that exist.
      expected_domain: the custom domain the CNAME must bind.

    Returns (ok: bool, problems: list[str]).
    """
    problems: list[str] = []
    present = set(present_files)

    for rel in REQUIRED_FILES:
        if rel not in present:
            problems.append(f"required file missing: {rel}")

    if cname_raw is None:
        # Missing-file problem already recorded above; nothing more to say.
        return (not problems), problems

    # A CNAME that GitHub Pages accepts is a single bare hostname line.
    lines = [ln for ln in cname_raw.splitlines() if ln.strip()]
    if len(lines) != 1:
        problems.append(
            f"CNAME must contain exactly one non-empty line; found {len(lines)}"
        )
    raw_line = lines[0] if lines else ""
    value = raw_line.strip()

    # Well-formed content is the bare hostname plus at most a trailing newline;
    # anything else (surrounding spaces, extra lines) is a problem.
    if cname_raw.rstrip("\n") != value or raw_line != value:
        problems.append("CNAME has leading/trailing whitespace or extra lines")
    if value.startswith(("http://", "https://")):
        problems.append(f"CNAME must be a bare hostname, not a URL: {value!r}")
    if value.endswith("/"):
        problems.append(f"CNAME must not have a trailing slash: {value!r}")
    if value != expected_domain:
        problems.append(f"CNAME is {value!r}; expected {expected_domain!r}")

    return (not problems), problems


def check_live(expected_domain=EXPECTED_DOMAIN):
    """Read-only live cross-check via the GitHub Pages API.

    Returns (status, messages) where status is 'ok', 'fail', or 'skip'. Never
    mutates remote state. Any tooling/auth/network gap yields 'skip'.
    """
    repo = _detect_repo()
    if not repo:
        return "skip", ["could not determine owner/repo; skipping live check"]

    proc = subprocess.run(
        ["gh", "api", f"repos/{repo}/pages"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        return "skip", [
            "gh Pages API unavailable (unauthenticated/offline/no-gh); "
            f"skipping live check: {proc.stderr.strip()[:200]}"
        ]

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return "skip", [f"could not parse Pages API response: {exc}"]

    problems: list[str] = []
    source = data.get("source") or {}
    branch, path = source.get("branch"), source.get("path")
    if branch != EXPECTED_BRANCH:
        problems.append(f"Pages source branch is {branch!r}; expected {EXPECTED_BRANCH!r}")
    if path != EXPECTED_PATH:
        problems.append(f"Pages source path is {path!r}; expected {EXPECTED_PATH!r}")

    cname = data.get("cname")
    if cname != expected_domain:
        problems.append(f"Pages custom domain is {cname!r}; expected {expected_domain!r}")

    if data.get("https_enforced") is not True:
        problems.append("Pages https_enforced is not true")

    if problems:
        return "fail", problems
    return "ok", [
        f"live Pages config healthy: source={branch}{path}, "
        f"cname={cname}, https_enforced=true"
    ]


def _detect_repo() -> str | None:
    """Best-effort owner/repo detection from the git remote, then gh."""
    r = subprocess.run(
        ["git", "-C", str(ROOT), "remote", "get-url", "origin"],
        capture_output=True, text=True,
    )
    url = r.stdout.strip()
    if r.returncode == 0 and url:
        slug = url.rsplit("github.com", 1)[-1].lstrip(":/")
        if slug.endswith(".git"):
            slug = slug[:-4]
        if slug.count("/") == 1:
            return slug
    r = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() or None if r.returncode == 0 else None


def _read_cname() -> str | None:
    p = ROOT / "CNAME"
    return p.read_text(encoding="utf-8") if p.is_file() else None


def _present_files() -> list[str]:
    return [rel for rel in REQUIRED_FILES if (ROOT / rel).is_file()]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="also run the read-only GitHub Pages API cross-check")
    ap.add_argument("--require-live", action="store_true",
                    help="treat a skipped live check as a failure")
    args = ap.parse_args(argv[1:])

    ok, problems = evaluate(_read_cname(), _present_files())
    if ok:
        print(f"pages-preflight: OK (offline) — CNAME={EXPECTED_DOMAIN}, "
              "required files present, .nojekyll set")
    else:
        print("pages-preflight: FAIL (offline)")
        for p in problems:
            print(f"  - {p}")

    exit_code = 0 if ok else 1

    if args.live or args.require_live:
        status, messages = check_live()
        label = {"ok": "OK", "fail": "FAIL", "skip": "SKIP"}[status]
        print(f"pages-preflight: {label} (live)")
        for m in messages:
            print(f"  - {m}")
        if status == "fail":
            exit_code = 1
        elif status == "skip" and args.require_live:
            print("  - live check was required but skipped")
            exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
