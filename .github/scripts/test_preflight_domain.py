#!/usr/bin/env python3
"""Offline tests for domain-preflight decision logic.

Exercises the pure helpers only — no DNS, TLS, HTTP, or git. Exits non-zero on
the first assertion failure, matching the other self-tests in this directory.
"""
import sys

from preflight_domain import (
    GH_PAGES_A,
    GH_PAGES_AAAA,
    apex_of,
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

    # evaluate_tls
    for name, san, err, expect in [
        ("exact SAN", ["console.odisena.com"], None, True),
        ("wildcard parent", ["*.odisena.com"], None, True),
        ("unrelated SAN", ["example.com"], None, False),
        ("handshake error", [], "reset by peer", False),
    ]:
        ok, detail = evaluate_tls(HOST, san, err)
        check(f"tls: {name}", ok, expect, detail)

    # evaluate_http
    for name, status, err, expect in [
        ("200", 200, None, True),
        ("301 redirect", 301, None, True),
        ("404", 404, None, False),
        ("probe error", None, "timed out", False),
    ]:
        ok, detail = evaluate_http(status, err)
        check(f"http: {name}", ok, expect, detail)

    if failures:
        print(f"test_preflight_domain: {failures} FAILURE(S)")
        return 1
    print("test_preflight_domain: all cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(run())
