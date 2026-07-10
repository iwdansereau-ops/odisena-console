#!/usr/bin/env python3
"""html-validate check: markup-integrity validation for the app-shell HTML.

Deterministic, stdlib-only, no network. Validates the served PWA shell files
(index.html, 404.html, v7.html) for:
  - presence of a <!doctype html> declaration
  - parseability with the stdlib HTML parser
  - no stray/mismatched explicit end tags (lenient about HTML5 optional closes)
  - no duplicate id attributes within a document

It intentionally does NOT validate downloadable content under artifacts/ or
runbooks/, which are payloads rendered/downloaded by the app rather than the
shell itself. Exits non-zero on any violation.
"""
import sys
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHELL_FILES = ["index.html", "404.html", "v7.html"]

VOID = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
}
# Elements whose end tag is commonly omitted in valid HTML5; when we see a
# close tag we pop through these to find the real match, so optional-close
# authoring does not produce false positives.
OPTIONAL_CLOSE = {
    "li", "dt", "dd", "p", "option", "thead", "tbody", "tfoot",
    "tr", "td", "th", "colgroup", "optgroup", "rp", "rt",
}


class MarkupChecker(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, int]] = []
        self.ids: dict[str, int] = {}
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name == "id" and value:
                if value in self.ids:
                    self.errors.append(
                        f"duplicate id {value!r} (first at line {self.ids[value]}, "
                        f"again at line {self.getpos()[0]})"
                    )
                else:
                    self.ids[value] = self.getpos()[0]
        if tag not in VOID:
            self.stack.append((tag, self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        # self-closing form (<tag/>) — treat as void, but still check id
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack and self.stack[-1][0] == tag:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for depth in range(len(self.stack) - 1, -1, -1):
            if self.stack[depth][0] == tag:
                # Pop everything above the match; anything skipped must be an
                # optional-close element, otherwise it is a real misnest.
                skipped = [t for t, _ in self.stack[depth + 1:]]
                bad = [t for t in skipped if t not in OPTIONAL_CLOSE]
                if bad:
                    self.errors.append(
                        f"</{tag}> at line {self.getpos()[0]} closes across "
                        f"unclosed element(s): {bad}"
                    )
                del self.stack[depth:]
                return
        self.errors.append(f"stray </{tag}> at line {self.getpos()[0]} with no open tag")


def check_file(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="strict")
    errors: list[str] = []
    head = text.lstrip()[:64].lower()
    if not head.startswith("<!doctype html"):
        errors.append("missing <!doctype html> declaration")
    checker = MarkupChecker()
    try:
        checker.feed(text)
        checker.close()
    except Exception as exc:  # parser blew up on malformed markup
        errors.append(f"parse error: {exc}")
        return errors
    errors.extend(checker.errors)
    leftover = [t for t, _ in checker.stack if t not in OPTIONAL_CLOSE and t != "html"]
    # Unclosed non-optional elements at EOF (html/body optional-close tolerated).
    hard_leftover = [t for t in leftover if t not in {"body", "head"}]
    if hard_leftover:
        errors.append(f"unclosed element(s) at end of file: {hard_leftover}")
    return errors


def main(argv: list[str]) -> int:
    targets = argv[1:] or SHELL_FILES
    total = 0
    for rel in targets:
        path = ROOT / rel
        if not path.is_file():
            print(f"html-validate: SKIP {rel} (not found)")
            continue
        errs = check_file(path)
        if errs:
            total += len(errs)
            print(f"html-validate: FAIL {rel}")
            for e in errs:
                print(f"  - {e}")
        else:
            print(f"html-validate: OK {rel}")
    if total:
        print(f"html-validate: {total} issue(s) found")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
