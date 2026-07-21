#!/usr/bin/env python3
"""Offline tests for domain-preflight decision logic.

Exercises the pure helpers only — no DNS, TLS, HTTP, or git. Exits non-zero on
the first assertion failure, matching the other self-tests in this directory.
"""
import sys

from preflight_domain import (
    FAIL,
    GH_PAGES_A,
    GH_PAGES_AAAA,
    SKIP,
    apex_of,
    classify_probe_error,
    classify_records,
    evaluate_apex_isolation,
    evaluate_cname_target,
    evaluate_http,
    evaluate_tls,
    validate_cname_file,
)

HOST = "console.odisena.com"
TARGET = "iwdansereau-ops.github.io"


def run() -> int:
    failures = 0

    def check(name: str, got_ok: bool, expect_ok: bool, detail: str) -> None:
        nonlocal failures
        status = "PASS" if got_ok == expect_ok else "FAIL"
        if got_ok != expect_ok:
            failures += 1
        print(f"  [{status}] {name}: got ok={got_ok} ({detail})")

    # apex_of
    assert apex_of(HOST) == "odisena.com", apex_of(HOST)
    assert apex_of("a.b.odisena.com") == "odisena.com"
    assert apex_of("odisena.com") == "odisena.com"

    # validate_cname_file
    for name, content, expect in [
        ("bare host matches", "console.odisena.com\n", True),
        ("trailing blank line ok", "console.odisena.com\n\n", True),
        ("wrong host", "other.odisena.com\n", False),
        ("has scheme", "https://console.odisena.com\n", False),
        ("has path", "console.odisena.com/app\n", False),
        ("trailing slash", "console.odisena.com/\n", False),
        ("empty", "\n", False),
        ("two hosts", "console.odisena.com\nextra.com\n", False),
    ]:
        ok, detail = validate_cname_file(content, HOST)
        check(f"cname-file: {name}", ok, expect, detail)

    # classify_records — A
    for name, recs, expect in [
        ("all four A", sorted(GH_PAGES_A), True),
        ("subset of A", ["185.199.108.153"], True),
        ("empty A", [], False),
        ("foreign A present", ["185.199.108.153", "76.76.21.21"], False),
    ]:
        ok, detail = classify_records(recs, GH_PAGES_A, "A")
        check(f"classify-a: {name}", ok, expect, detail)

    ok, detail = classify_records(sorted(GH_PAGES_AAAA), GH_PAGES_AAAA, "AAAA")
    check("classify-aaaa: all four", ok, True, detail)

    # evaluate_cname_target
    for name, chain, expect in [
        ("exact target", [TARGET + "."], True),
        ("case + dot", ["IWDANSEREAU-OPS.GITHUB.IO"], True),
        ("empty chain", [], False),
        ("wrong target", ["someone-else.github.io."], False),
    ]:
        ok, detail = evaluate_cname_target(chain, TARGET)
        check(f"cname-target: {name}", ok, expect, detail)

    # evaluate_apex_isolation
    for name, a, aaaa, expect in [
        ("apex empty", [], [], True),
        ("apex elsewhere", ["76.76.21.21"], [], True),
        ("apex on Pages A", ["185.199.108.153"], [], False),
        ("apex on Pages AAAA", [], ["2606:50c0:8000::153"], False),
    ]:
        ok, detail = evaluate_apex_isolation(a, aaaa)
        check(f"apex-isolation: {name}", ok, expect, detail)

    # evaluate_tls (SAN-coverage decision only; probe errors are classified
    # separately by classify_probe_error)
    for name, san, expect in [
        ("exact SAN", ["console.odisena.com"], True),
        ("wildcard parent", ["*.odisena.com"], True),
        ("unrelated SAN", ["example.com"], False),
        ("empty SAN", [], False),
    ]:
        ok, detail = evaluate_tls(HOST, san)
        check(f"tls: {name}", ok, expect, detail)

    # evaluate_http (status-code decision only)
    for name, status, expect in [
        ("200", 200, True),
        ("301 redirect", 301, True),
        ("404", 404, False),
        ("no status", None, False),
    ]:
        ok, detail = evaluate_http(status)
        check(f"http: {name}", ok, expect, detail)

    # classify_probe_error — connection-level egress failures SKIP; genuine
    # certificate/protocol failures FAIL.
    def check_cls(name: str, got: str, expect: str) -> None:
        nonlocal failures
        status = "PASS" if got == expect else "FAIL"
        if got != expect:
            failures += 1
        print(f"  [{status}] classify: {name}: got {got} (want {expect})")

    for name, exc_name, expect in [
        # egress / environment limits -> SKIP (preserves sandbox behavior)
        ("connection reset (sandbox egress)", "ConnectionResetError", SKIP),
        ("connection refused", "ConnectionRefusedError", SKIP),
        ("timeout", "TimeoutError", SKIP),
        ("socket.timeout alias", "timeout", SKIP),
        ("dns lookup failure", "gaierror", SKIP),
        ("network unreachable (OSError)", "OSError", SKIP),
        # genuine certificate / protocol defects -> FAIL
        ("cert verification failure", "SSLCertVerificationError", FAIL),
        ("tls protocol error", "SSLError", FAIL),
        ("hostname mismatch", "CertificateError", FAIL),
        ("unknown error", "ValueError", FAIL),
    ]:
        check_cls(name, classify_probe_error(exc_name), expect)

    if failures:
        print(f"test_preflight_domain: {failures} FAILURE(S)")
        return 1
    print("test_preflight_domain: all cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
