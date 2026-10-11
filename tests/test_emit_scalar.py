#!/usr/bin/env python3
"""emit_scalar: every value is written so it reads back, or it is refused.

Run it:  python3 tests/test_emit_scalar.py

belay-authoring#26. The contract is by VALUE: for every string, emit_scalar
either raises ToolError, or `value: <form>` read by yamlite AND by PyYAML
gives back exactly that string. PyYAML is a test-time oracle only (the
toolkit stays stdlib); when it is not importable the PyYAML half is reported
as SKIPPED in the notes, never passed silently.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import case, check, note, report  # noqa: E402

import bundlelib as bl  # noqa: E402

try:
    import yaml as pyyaml
except ImportError:  # pragma: no cover - depends on the machine
    pyyaml = None

ALPHABET = list(": # - [ ] { } ' \" \\ , & * ! | > % @ ` ~ = < . ?".split(" ")) + [
    " ", " ", " ", "\t", "a", "b", "é", "ß", "0", "1", "9", "\x07", chr(0x2028), " #",
]
TOKENS = ["true", "Yes", "null", "1:30", "0x1f", "2026-09-12", ".inf", "No", "~"]


def read_both(form: str):
    """(yamlite value, pyyaml value or the exception) for `value: <form>`."""
    doc = f"value: {form}\n"
    try:
        y = bl.load_yaml(doc, "t")["value"]
    except Exception as exc:  # noqa: BLE001 - the exception is the finding
        y = exc
    p = None
    if pyyaml is not None:
        try:
            p = pyyaml.safe_load(doc)["value"]
        except Exception as exc:  # noqa: BLE001
            p = exc
    return y, p


def round_trips(value: str, form: str) -> tuple[bool, str]:
    y, p = read_both(form)
    if not (isinstance(y, str) and y == value):
        return False, f"yamlite read {y!r}"
    if pyyaml is not None and not (isinstance(p, str) and p == value):
        return False, f"PyYAML read {p!r}"
    return True, ""


def classify(value: str):
    try:
        return bl.emit_scalar(value)
    except bl.ToolError:
        return None


def case_property() -> None:
    if pyyaml is None:
        note("PyYAML is not importable: the PyYAML half of the emit_scalar property test was SKIPPED")
    rng = random.Random(26)
    values = []
    for _ in range(4000):
        parts = []
        for _ in range(rng.randint(1, 7)):
            parts.append(rng.choice(TOKENS) if rng.random() < 0.15 else rng.choice(ALPHABET))
        values.append("".join(parts))
    plain = quoted = refused = 0
    bad = []
    for value in values:
        form = classify(value)
        if form is None:
            refused += 1
            continue
        if form == value:
            plain += 1
        else:
            quoted += 1
        ok, why = round_trips(value, form)
        if not ok:
            bad.append((value, form, why))
    note(f"emit_scalar property test: {len(values)} values, {plain} plain, {quoted} quoted, {refused} refused")
    for value, form, why in bad[:10]:
        print(f"    ---- {value!r} -> {form}: {why}")
    check(not bad, f"every emitted form reads back as the exact string ({len(bad)} did not)")
    check(plain > 0, "the property test saw plain values")
    check(quoted > 0, "the property test saw quoted values")
    check(refused > 0, "the property test saw refused values")


def case_named() -> None:
    # F1: yamlite's comment stripping treats the apostrophe as an opening quote.
    form = bl.emit_scalar("N'? # 1")
    check(form.startswith('"'), f"N'? # 1 is quoted, not plain ({form!r})")
    ok, why = round_trips("N'? # 1", form)
    check(ok, f"N'? # 1 reads back in both readers ({why})")
    # An even number of double quotes before ' #' reads back fine.
    form = bl.emit_scalar('she said "x" # y')
    ok, why = round_trips('she said "x" # y', form)
    check(ok, f'she said "x" # y is written and reads back in both readers ({why})')
    # An odd number of double quotes before ' #' cannot be read by yamlite:
    # it toggles quote state on every '"' and ignores the backslash, so the
    # escaped quote ends the string early. Reader defect: belay#57 (skomp/belay).
    # Until it is fixed the value is REFUSED; PyYAML would have read it.
    try:
        got = bl.emit_scalar('say "hi # there')
        check(False, f'say "hi # there is refused, not written as {got!r}')
    except bl.ToolError as exc:
        check(True, 'say "hi # there is refused')
        check("cannot be written" in str(exc) and "hi # there" in str(exc),
              f"the refusal quotes the value ({exc})")
    for value in ("bell\x07 here", "line" + chr(0x2028) + "x", "n" + chr(0x85) + "l"):
        try:
            bl.emit_scalar(value)
            check(False, f"{value!r} is refused")
        except bl.ToolError:
            check(True, f"{value!r} is refused")
    # Positive controls: ordinary values stay plain, so nothing passes by
    # quoting everything, and no existing manifest churns.
    for value in ("Parse the header", "03-events-to-state", "lessons/03-x.md",
                  "models/Duck.glb", "a-b", "it's fine"):
        check(bl.emit_scalar(value) == value, f"{value!r} stays plain")
    # The retyped words are quoted.
    for value in ("Yes", "off", "0x1f", "0b11", "017", "1_000", "2026-09-12",
                  "1:30", ".inf", ".nan", "1e3", "1.5e+3", "<<", "=", "~", "null", "True"):
        check(bl.emit_scalar(value).startswith('"'), f"{value!r} is quoted")


def case_resolver_derivation() -> None:
    """The retyped-scalar patterns in bundlelib are copies of PyYAML's own.
    Fail if the installed resolver has drifted from them."""
    if pyyaml is None:
        note("PyYAML is not importable: the resolver-derivation check was SKIPPED")
        return
    from yaml.resolver import Resolver

    theirs = set()
    for entries in Resolver.yaml_implicit_resolvers.values():
        for tag, rx in entries:
            if not tag.endswith(("yaml",)):
                theirs.add((tag, rx.pattern))
    def squash(pattern: str) -> str:  # the verbose-mode layout is not significant
        return "".join(pattern.split())

    mine = {squash(rx.pattern) for rx in bl._YAML11_RETYPED}
    for tag, pattern in sorted(theirs):
        check(squash(pattern) in mine, f"PyYAML's {tag} pattern is carried by bundlelib")


def main() -> int:
    print("emit_scalar")
    with case("property: every value round-trips by value or is refused"):
        case_property()
    with case("named cases, refusals and positive controls"):
        case_named()
    with case("the retyped-scalar patterns match PyYAML's resolver"):
        case_resolver_derivation()
    return report("test_emit_scalar.py")


if __name__ == "__main__":
    sys.exit(main())
