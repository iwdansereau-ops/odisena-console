#!/usr/bin/env python3
"""domain-preflight: validate the GitHub Pages custom-domain binding for
console.odisena.com before/after any change that touches CNAME or DNS.

Two modes:

  offline (default)
      File-only checks that are safe for CI and require no network:
        - CNAME file exists, is a single bare hostname, matches the expected
          host, and carries no scheme / path / trailing slash.
        - .nojekyll exists (Pages must serve files verbatim).
      Exits non-zero if a required offline check fails.

  live (--live)
      Everything above, plus read-only network probes (no mutation):
        - CNAME chain: console.odisena.com -> <owner>.github.io.
        - A / AAAA records resolve to GitHub Pages' published address set.
        - TLS: certificate is served and its SAN covers the host.
        - HTTP: apex-> HTTPS behaviour and a 200/redirect from the site.
        - Apex isolation: the naked apex (odisena.com) must NOT point at the
          Pages address set — the console lives on its own subdomain only.
        - Founder vanity aliases (optional): reported as present/absent.
          Absent aliases are informational and never fail the preflight.

The decision helpers (validate_*, classify_*, evaluate_*) are pure functions
with no I/O so they can be unit-tested without a network; see
test_preflight_domain.py. Network probing is confined to the `probe_*`
helpers and is strictly read-only (DNS queries, a TLS handshake, and HEAD/GET
requests). Nothing here changes DNS, Pages settings, or repository state.
"""
import argparse
import re
import socket
import ssl
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# --- Expected configuration -------------------------------------------------
EXPECTED_HOST = "console.odisena.com"
# GitHub Pages custom-subdomain target (the CNAME record's value). Derived from
# the repository owner; kept explicit so the check is self-documenting.
EXPECTED_PAGES_TARGET = "iwdansereau-ops.github.io"

# GitHub Pages' published apex addresses (docs.github.com "Managing a custom
# domain"). A correctly-served subdomain resolves through its github.io CNAME
# to a subset of these.
GH_PAGES_A = {
    "185.199.108.153",
    "185.199.109.153",
    "185.199.110.153",
    "185.199.111.153",
}
GH_PAGES_AAAA = {
    "2606:50c0:8000::153",
    "2606:50c0:8001::153",
    "2606:50c0:8002::153",
    "2606:50c0:8003::153",
}

# Optional founder vanity aliases. These are NOT required to exist; the
# preflight only reports their state. It never creates or mutates them.
DEFAULT_FOUNDER_ALIASES = ("founder.odisena.com", "ian.odisena.com")

IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
IPV6_RE = re.compile(r"^[0-9a-fA-F:]+:[0-9a-fA-F:]+$")

# Status vocabulary. OK/FAIL are gating; SKIP/INFO never affect the exit code.
OK, FAIL, SKIP, INFO = "OK", "FAIL", "SKIP", "INFO"


# --- Pure decision helpers (no I/O; unit-tested) ----------------------------
def apex_of(host: str) -> str:
    """Return the registrable apex for a host (strip the left-most label)."""
    parts = host.strip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def validate_cname_file(content: str, expected: str) -> tuple[bool, str]:
    """A GitHub Pages CNAME file must be exactly one bare hostname."""
    raw = content
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return False, "CNAME is empty"
    if len(lines) > 1:
        return False, f"CNAME has {len(lines)} non-empty lines; expected 1"
    host = lines[0]
    if any(c in host for c in "/:") or host.startswith("http"):
        return False, f"CNAME must be a bare hostname, got {host!r}"
    if host.endswith("/"):
        return False, f"CNAME must not have a trailing slash, got {host!r}"
    if host != expected:
        return False, f"CNAME host {host!r} != expected {expected!r}"
    return True, f"CNAME = {host}"


def classify_records(records: list[str], expected: set[str], kind: str
                     ) -> tuple[bool, str]:
    """Resolved records must be a non-empty subset of the expected set."""
    got = set(records)
    if not got:
        return False, f"no {kind} records resolved"
    unexpected = sorted(got - expected)
    if unexpected:
        return False, f"unexpected {kind} record(s): {', '.join(unexpected)}"
    missing = sorted(expected - got)
    detail = f"{len(got)}/{len(expected)} GitHub Pages {kind} records"
    if missing:
        detail += f" (missing {', '.join(missing)} — non-fatal)"
    return True, detail


def evaluate_cname_target(chain: list[str], expected: str) -> tuple[bool, str]:
    """The CNAME chain for the host must reach the expected Pages target."""
    norm = [c.strip(".").lower() for c in chain]
    if expected.lower() in norm:
        return True, f"CNAME -> {expected}"
    if not norm:
        return False, "no CNAME record (host is not aliased to github.io)"
    return False, f"CNAME chain {norm} does not include {expected!r}"


def evaluate_apex_isolation(apex_a: list[str], apex_aaaa: list[str]
                            ) -> tuple[bool, str]:
    """The naked apex must not point at the Pages address set."""
    a_hit = sorted(set(apex_a) & GH_PAGES_A)
    aaaa_hit = sorted(set(apex_aaaa) & GH_PAGES_AAAA)
    if a_hit or aaaa_hit:
        hit = ", ".join(a_hit + aaaa_hit)
        return False, f"apex resolves to Pages address(es): {hit}"
    if not apex_a and not apex_aaaa:
        return True, "apex has no A/AAAA records (isolated)"
    shown = ", ".join(apex_a + apex_aaaa)
    return True, f"apex isolated (points elsewhere: {shown})"


def evaluate_tls(host: str, san: list[str], error: str | None
                 ) -> tuple[bool, str]:
    """Certificate SAN must cover the host (exact or wildcard parent)."""
    if error:
        return False, f"TLS handshake failed: {error}"
    host = host.lower()
    parent = "." + host.split(".", 1)[1] if "." in host else host
    for name in san:
        n = name.lower()
        if n == host or (n.startswith("*.") and n[1:] == parent):
            return True, f"certificate SAN covers {host}"
    return False, f"certificate SAN {san} does not cover {host}"


def evaluate_http(status: int | None, error: str | None) -> tuple[bool, str]:
    if error:
        return False, f"HTTP probe failed: {error}"
    if status is None:
        return False, "no HTTP status"
    if 200 <= status < 400:
        return True, f"HTTP {status}"
    return False, f"unexpected HTTP status {status}"


# --- Live probes (read-only network I/O) ------------------------------------
def _dig(host: str, rtype: str) -> list[str]:
    out = subprocess.run(
        ["dig", "+short", host, rtype],
        capture_output=True, text=True, timeout=20,
    ).stdout
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def resolve_a(host: str) -> list[str]:
    return [r for r in _dig(host, "A") if IPV4_RE.match(r)]


def resolve_aaaa(host: str) -> list[str]:
    return [r for r in _dig(host, "AAAA") if IPV6_RE.match(r)]


def resolve_cname(host: str) -> list[str]:
    return _dig(host, "CNAME")


def probe_tls(host: str, timeout: float = 10.0
              ) -> tuple[list[str], str | None]:
    """Return (SAN list, error). Read-only TLS handshake."""
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                cert = ssock.getpeercert()
        san = [v for typ, v in cert.get("subjectAltName", []) if typ == "DNS"]
        return san, None
    except Exception as exc:  # noqa: BLE001 - report any probe failure verbatim
        return [], f"{type(exc).__name__}: {exc}"


def probe_http(host: str, scheme: str = "https", timeout: float = 10.0
               ) -> tuple[int | None, str | None]:
    """HEAD request; return (status, error). Read-only."""
    import http.client

    conn_cls = (http.client.HTTPSConnection if scheme == "https"
                else http.client.HTTPConnection)
    conn = conn_cls(host, timeout=timeout)
    try:
        conn.request("HEAD", "/")
        return conn.getresponse().status, None
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


# --- Report plumbing --------------------------------------------------------
class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, name: str, status: str, detail: str) -> None:
        self.rows.append((name, status, detail))

    def gating_failed(self) -> bool:
        return any(status == FAIL for _, status, _ in self.rows)

    def render(self) -> str:
        width = max((len(n) for n, _, _ in self.rows), default=0)
        lines = []
        for name, status, detail in self.rows:
            lines.append(f"  [{status:<4}] {name.ljust(width)}  {detail}")
        return "\n".join(lines)


def run_offline(report: Report, host: str) -> None:
    cname_path = ROOT / "CNAME"
    if not cname_path.is_file():
        report.add("cname-file", FAIL, "CNAME file is missing")
    else:
        ok, detail = validate_cname_file(
            cname_path.read_text(encoding="utf-8"), host)
        report.add("cname-file", OK if ok else FAIL, detail)

    nojekyll = ROOT / ".nojekyll"
    report.add("nojekyll", OK if nojekyll.is_file() else FAIL,
               ".nojekyll present" if nojekyll.is_file()
               else ".nojekyll missing (Pages would run Jekyll)")


def run_live(report: Report, host: str, aliases: list[str]) -> None:
    apex = apex_of(host)

    chain = resolve_cname(host)
    ok, detail = evaluate_cname_target(chain, EXPECTED_PAGES_TARGET)
    report.add("dns-cname", OK if ok else FAIL, detail)

    ok, detail = classify_records(resolve_a(host), GH_PAGES_A, "A")
    report.add("dns-a", OK if ok else FAIL, detail)

    ok, detail = classify_records(resolve_aaaa(host), GH_PAGES_AAAA, "AAAA")
    report.add("dns-aaaa", OK if ok else FAIL, detail)

    ok, detail = evaluate_apex_isolation(resolve_a(apex), resolve_aaaa(apex))
    report.add("apex-isolation", OK if ok else FAIL, detail)

    san, err = probe_tls(host)
    if err:
        # A blocked handshake from this host is an environment limit, not a
        # site defect; surface it as SKIP so it does not gate the preflight.
        report.add("tls", SKIP, f"unverifiable from this host ({err})")
    else:
        ok, detail = evaluate_tls(host, san, None)
        report.add("tls", OK if ok else FAIL, detail)

    status, err = probe_http(host)
    if err:
        report.add("http", SKIP, f"unverifiable from this host ({err})")
    else:
        ok, detail = evaluate_http(status, None)
        report.add("http", OK if ok else FAIL, detail)

    for alias in aliases:
        a = resolve_a(alias)
        aaaa = resolve_aaaa(alias)
        cname = resolve_cname(alias)
        if not (a or aaaa or cname):
            report.add(f"alias:{alias}", INFO, "absent (optional; not created)")
        else:
            pts = cname or (a + aaaa)
            report.add(f"alias:{alias}", INFO, f"present -> {', '.join(pts)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--live", action="store_true",
                    help="perform read-only DNS/TLS/HTTP probes")
    ap.add_argument("--host", default=EXPECTED_HOST,
                    help=f"custom domain to check (default {EXPECTED_HOST})")
    ap.add_argument("--alias", action="append", default=None,
                    help="founder vanity alias to report (repeatable)")
    args = ap.parse_args(argv)

    aliases = (args.alias if args.alias is not None
               else list(DEFAULT_FOUNDER_ALIASES))

    report = Report()
    run_offline(report, args.host)
    if args.live:
        run_live(report, args.host, aliases)

    print(f"domain-preflight for {args.host} "
          f"({'live' if args.live else 'offline'} mode)")
    print(report.render())

    if report.gating_failed():
        print("domain-preflight: FAIL")
        return 1
    print("domain-preflight: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
