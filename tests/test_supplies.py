#!/usr/bin/env python3
"""Test suite for bundlelib's supplies-entry writer, and for supplies.py.

Run it with:  python3 tests/test_supplies.py

`supplies:` is what tells a runner which files a course hands the learner's
workspace, so no lesson ever assigns "copy these files" as a task. This
suite has two halves.

The unit half covers the three functions bundlelib gives every caller that
needs to write a supplies entry:

  emit_scalar             a single YAML scalar, quoted only when it has to be
  render_supplies_item    the lines for one supplies entry
  add_supplies            append an entry to a manifest or a frontmatter block

The central risk there is silent corruption: an emitter that writes bytes
which parse into something the author did not type. So every case in that
half is a ROUND TRIP - the emitted text is parsed back through this
project's own restricted loader, `yamlite.load_yaml`, and the parsed value
is compared against what went in. A test that only checked "it looks quoted"
would miss exactly the bug this file exists to catch.

The CLI half exercises `scripts/supplies.py list` and `scripts/supplies.py
add` end to end, against the runner's real validator (found the same way the
tool itself finds it). It mirrors tests/test_lesson_add.py's habits: every
refusal is shown firing, with a POSITIVE CONTROL beside it that differs only
in the thing under test, and every refusal is followed by a `tree_digest`
comparison rather than "the new file is absent".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import harness as h
from harness import Workspace, case, check, check_in, check_not_in, report

sys.path.insert(0, str(h.SCRIPTS))
import bundlelib as bl  # noqa: E402  (harness put the scripts dir on sys.path)
from bundlelib import load_yaml  # noqa: E402


# --------------------------------------------------------------------------
# emit_scalar
# --------------------------------------------------------------------------


def test_emit_scalar_round_trips_the_hard_cases() -> None:
    for value in [
        "npm workspace",
        "Duck.glb: the sample model",              # a colon
        "the #1 thing this lesson needs",          # a hash
        "  leading and trailing  ",                # whitespace that YAML would eat
        'she said "copy it"',                      # quotes
        "a - b",                                   # a dash
        "{not a flow mapping}",                    # a brace
    ]:
        emitted = bl.emit_scalar(value)
        doc = f"describe: {emitted}\n"
        back = load_yaml(doc, "probe")
        check(back["describe"] == value, f"emit_scalar round-trips {value!r} (emitted {emitted!r})")


def test_emit_scalar_does_not_confuse_a_string_for_the_thing_it_looks_like() -> None:
    """The defect found in this task's proposed implementation.

    A character-class regex ("anything in [A-Za-z0-9 ._/-] is safe unquoted")
    was proposed for emit_scalar and never run. Measured against yamlite, it
    is wrong: "123", "true", "null" and similar all satisfy that regex and
    all come back as the WRONG PYTHON TYPE - an int, a bool, or None -
    instead of the string that was given. That is silent corruption of
    exactly the kind emit_scalar exists to prevent, and it would not have
    been caught by the round-trip cases above, none of which look like a
    YAML literal.

    emit_scalar as implemented asks the loader directly instead of guessing,
    so every one of these must come back as the identical string.
    """
    for value in [
        "123",
        "0",
        "3.14",
        "true",
        "True",
        "TRUE",
        "false",
        "False",
        "FALSE",
        "null",
        "Null",
        "NULL",
        "~",
        "",
    ]:
        emitted = bl.emit_scalar(value)
        doc = f"describe: {emitted}\n"
        back = load_yaml(doc, "probe")
        check(
            back["describe"] == value and isinstance(back["describe"], str),
            f"emit_scalar round-trips look-alike {value!r} as a string "
            f"(emitted {emitted!r}, got back {back['describe']!r})",
        )

    # POSITIVE CONTROL: a value that is NOT a look-alike is still left
    # unquoted. Without this, the loop above would pass identically if
    # emit_scalar quoted every value unconditionally, which would prove
    # nothing about the actual defect.
    plain = bl.emit_scalar("npm workspace")
    check(
        plain == "npm workspace",
        f"positive control: an ordinary value is still left unquoted (got {plain!r})",
    )


def test_emit_scalar_quotes_what_a_conforming_parser_would_reject() -> None:
    """The false oracle found by the final review.

    emit_scalar kept the unquoted form whenever `load_yaml` round-tripped
    it - and `load_yaml` is yamlite, this project's own LENIENT reader. It
    reads `value: a: b` back as the scalar 'a: b', so it is the one parser
    that cannot fail this check, and the emitter wrote manifests and lesson
    frontmatter that js-yaml@4 REJECTS with "bad indentation of a mapping
    entry". The contract's own example at bundle-format.md is written
    QUOTED, and this emitter could not produce that form.

    Every value below is one yamlite accepts unquoted and a conforming
    parser does not. Each must now come back QUOTED, and must still
    round-trip through yamlite unchanged.
    """
    for value, why in [
        ("a: b", "a colon-space opens a mapping entry"),
        ("Duck.glb: the sample model", "the contract's own example"),
        ("x: y: z", "two colon-spaces"),
        ("trailing:", "a trailing colon reads as a key"),
        ("- item", "a leading '- ' opens a block sequence"),
        ("-", "a lone dash is the block-sequence indicator"),
        ("? x", "a leading '? ' opens a complex mapping key"),
        (", a", "an opening flow indicator"),
        ("{x", "an opening flow indicator"),
        ("a\tb", "a tab"),
    ]:
        emitted = bl.emit_scalar(value)
        check(
            emitted.startswith('"') and emitted.endswith('"'),
            f"emit_scalar quotes {value!r} ({why}); got {emitted!r}",
        )
        back = load_yaml(f"describe: {emitted}\n", "probe")
        check(
            back["describe"] == value,
            f"the quoted form of {value!r} still round-trips (got "
            f"{back['describe']!r})",
        )

    # POSITIVE CONTROL: without it, every assertion above would pass
    # identically if emit_scalar quoted everything, which would prove
    # nothing about the shapes under test. These three are the values the
    # toolkit writes most often, and all three must stay unquoted.
    for value in ["npm workspace", "supplies/workspace/", "."]:
        emitted = bl.emit_scalar(value)
        check(
            emitted == value,
            f"positive control: {value!r} is still emitted unquoted (got "
            f"{emitted!r})",
        )

    # The guard on its own, so a failure says which half broke.
    check(
        not bl.plain_scalar_is_portable("a: b"),
        "plain_scalar_is_portable refuses 'a: b'",
    )
    check(
        bl.plain_scalar_is_portable("the model, which is a duck"),
        "plain_scalar_is_portable accepts a comma INSIDE the value - a flow "
        "indicator is syntax only when the scalar opens with it",
    )


# --------------------------------------------------------------------------
# render_supplies_item
# --------------------------------------------------------------------------


def test_render_supplies_item_orders_keys_and_indents_two_spaces() -> None:
    entry = {"from": "supplies/workspace/", "to": ".", "describe": "the workspace"}
    lines = bl.render_supplies_item(entry)
    check(lines[0] == "  - from: supplies/workspace/", f"first line carries 'from' at the item marker (got {lines[0]!r})")
    check(lines[1] == "    to: .", f"second line carries 'to', four-space indented (got {lines[1]!r})")
    check(lines[2] == "    describe: the workspace", f"third line carries 'describe' (got {lines[2]!r})")
    check(len(lines) == 3, "exactly one line per key")


def test_render_supplies_item_refuses_a_missing_key() -> None:
    try:
        bl.render_supplies_item({"from": "x/", "to": "y/"})
        check(False, "a supplies entry missing 'describe' raises ToolError")
    except bl.ToolError as exc:
        check(True, "a supplies entry missing 'describe' raises ToolError")
        check("describe" in str(exc), f"the message names the missing key (got {exc!r})")

    # POSITIVE CONTROL beside the refusal: the same shape, complete, does not raise.
    complete = bl.render_supplies_item({"from": "x/", "to": "y/", "describe": "z"})
    check(len(complete) == 3, "positive control: a complete entry renders fine")


# --------------------------------------------------------------------------
# add_supplies
# --------------------------------------------------------------------------


def test_add_supplies_creates_the_block_then_appends_to_it() -> None:
    text = "bundle_format: 1\nid: x\n"
    entry = {"from": "supplies/workspace/", "to": ".", "describe": "the workspace"}
    once = bl.add_supplies(text, entry, frontmatter=False)
    twice = bl.add_supplies(once, {"from": "m/", "to": "models/",
                                   "describe": "the model"}, frontmatter=False)
    parsed = load_yaml(twice, "probe")
    check(len(parsed["supplies"]) == 2, "two entries after two adds")
    check(parsed["supplies"][0] == entry, "the first entry survived the second add")
    check(parsed["id"] == "x", "the rest of the manifest is untouched")


def test_add_supplies_in_frontmatter_leaves_the_body_alone() -> None:
    text = "---\nid: 00-x\ntitle: X\n---\n\n## Purpose\n\nTeach something.\n"
    out = bl.add_supplies(text, {"from": "lessons/00-x/model/", "to": "models/",
                                 "describe": "the model"}, frontmatter=True)
    fm, body = bl.split_frontmatter(out)
    check(load_yaml(fm, "probe")["supplies"][0]["to"] == "models/", "entry is in the frontmatter")
    check(body.strip().startswith("## Purpose"), "the body is unchanged")


def test_add_supplies_rewrites_an_empty_inline_supplies_value() -> None:
    """`supplies: []` is the one inline shape add_supplies does NOT refuse.

    Mirrors replace_lessons_list's precedent for an empty `lessons: []`: a
    freshly scaffolded bundle can carry an empty inline `supplies: []`, and
    refusing it would leave such a bundle unable to ever declare its first
    supplies entry. It is rewritten in block form - which loses no
    formatting, because an empty list has none - and the entry is appended.
    """
    text = "bundle_format: 1\nsupplies: []\nid: x\n"
    entry = {"from": "x/", "to": "y/", "describe": "z"}
    out = bl.add_supplies(text, entry, frontmatter=False)
    parsed = load_yaml(out, "probe")
    check(
        parsed["supplies"] == [entry],
        f"an empty 'supplies: []' is rewritten in block form with the entry "
        f"appended (got {parsed.get('supplies')!r})",
    )
    check(parsed["id"] == "x", "the rest of the manifest is untouched")
    check(
        "supplies: []" not in out,
        "the inline flow form is gone from the written text",
    )

    # POSITIVE CONTROL: a trailing comment on the same line survives the
    # rewrite, the same way replace_lessons_list preserves one on an empty
    # `lessons: []`.
    commented = "bundle_format: 1\nsupplies: []  # nothing yet\nid: x\n"
    out2 = bl.add_supplies(commented, entry, frontmatter=False)
    check(
        "supplies:  # nothing yet" in out2,
        f"a trailing comment on the empty flow line survives the rewrite "
        f"(got {out2!r})",
    )


def test_add_supplies_refuses_a_nonempty_inline_supplies_value() -> None:
    """A NON-empty inline `supplies:` value is still refused.

    Mirrors replace_lessons_list's refusal of a non-empty inline `lessons:`:
    rewriting an author's inline flow value into block form is a bigger
    change than "append one entry", so this raises ManifestEditError
    instead. `supplies: []` is the one inline shape that is NOT refused - see
    the case above - so this uses a value that is inline but not empty.
    """
    text = "bundle_format: 1\nsupplies: [x]\nid: x\n"
    entry = {"from": "x/", "to": "y/", "describe": "z"}
    try:
        bl.add_supplies(text, entry, frontmatter=False)
        check(False, "a non-empty inline 'supplies: [x]' raises ManifestEditError")
    except bl.ManifestEditError:
        check(True, "a non-empty inline 'supplies: [x]' raises ManifestEditError")

    # POSITIVE CONTROL: the block form of the same manifest is not refused.
    block_text = "bundle_format: 1\nsupplies:\n  - from: a/\n    to: b/\n    describe: c\nid: x\n"
    out = bl.add_supplies(block_text, entry, frontmatter=False)
    check(
        load_yaml(out, "probe")["supplies"][-1] == entry,
        "positive control: the block form is appended to normally",
    )


def test_add_supplies_catches_a_broken_emitter() -> None:
    """The round-trip check inside add_supplies is a real check, not a comment.

    render_supplies_item is monkeypatched to write bytes that do not match
    the given entry, and add_supplies must refuse to return them. A check
    that has never been shown firing has not been tested - this is that
    demonstration for add_supplies's own honesty check.
    """
    original = bl.render_supplies_item
    text = "bundle_format: 1\nid: x\n"
    entry = {"from": "real/", "to": "real-to/", "describe": "real describe"}
    try:
        bl.render_supplies_item = lambda _entry: [
            "  - from: wrong",
            "    to: also-wrong",
            "    describe: nope",
        ]
        try:
            bl.add_supplies(text, entry, frontmatter=False)
            check(False, "add_supplies raises when the written entry does not match what was given")
        except bl.ToolError:
            check(True, "add_supplies raises when the written entry does not match what was given")
    finally:
        bl.render_supplies_item = original

    # POSITIVE CONTROL: with the real emitter restored, the identical call succeeds.
    out = bl.add_supplies(text, entry, frontmatter=False)
    check(
        load_yaml(out, "probe")["supplies"][-1] == entry,
        "positive control: the same call succeeds once the emitter is restored",
    )


# --------------------------------------------------------------------------
# supplies.py - the CLI half
#
# `add` obeys the same three safety properties every mutating script in this
# toolkit obeys (see tests/test_lesson_add.py, which this half mirrors): the
# validator runs against the edit and a dirty run is discarded, --check does
# the whole thing on a copy and throws it away, and a dirty git working tree
# refuses the run unless --force. Every refusal here gets the same treatment
# as there - a POSITIVE CONTROL that differs only in the thing under test,
# and a `tree_digest` comparison rather than "the file is absent".
# --------------------------------------------------------------------------


def add_cmd(bundle: Path, *args: str, env: dict | None = None) -> h.Run:
    return h.run(h.SUPPLIES, "add", bundle, *args, env=env)


def list_cmd(bundle: Path, *args: str, env: dict | None = None) -> h.Run:
    return h.run(h.SUPPLIES, "list", bundle, *args, env=env)


def commit_all(bundle: Path) -> None:
    """Commit a successful add's edit, so the tree is clean for the next call.

    `add` writes tutorial.yaml or a lesson's frontmatter directly (it is not
    given --force), so a case that chains two successful add calls would
    otherwise have its second call refused by the dirty-tree check for a
    reason that has nothing to do with what that call is testing.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "fixture",
            "GIT_AUTHOR_EMAIL": "fixture@invalid",
            "GIT_COMMITTER_NAME": "fixture",
            "GIT_COMMITTER_EMAIL": "fixture@invalid",
            "GIT_CONFIG_GLOBAL": str(bundle / ".gitconfig-absent"),
            "GIT_CONFIG_SYSTEM": str(bundle / ".gitconfig-absent"),
        }
    )
    for argv in (["git", "add", "-A"], ["git", "commit", "-q", "-m", "test commit"]):
        done = subprocess.run(argv, cwd=str(bundle), env=env, capture_output=True, text=True)
        if done.returncode != 0:  # pragma: no cover - a broken git install
            raise RuntimeError(f"{' '.join(argv)}: {done.stderr}")


def refusal(
    label: str, bundle: Path, args: tuple[str, ...], needle: str, env: dict | None = None
) -> h.Run:
    """Run `add` expecting a refusal, and prove the bundle did not move."""
    before = h.tree_digest(bundle)
    result = add_cmd(bundle, *args, env=env)
    check(result.returncode != 0, f"{label}: exits non-zero")
    check_in(needle, result.output, f"{label}: says what the author can act on")
    check(
        h.tree_digest(bundle) == before,
        f"{label}: the bundle is byte-identical afterwards",
    )
    return result


def cli_case_from_missing() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        h.git_init(bundle)
        before = h.tree_digest(bundle)
        out = add_cmd(bundle, "--from", "supplies/nope/", "--to", ".", "--describe", "the workspace")
        check(out.returncode != 0, "it refused")
        check_in("does not exist in the bundle", out.output, "and said why")
        check(h.tree_digest(bundle) == before, "the bundle is byte-identical")

    with Workspace() as ws:
        # POSITIVE CONTROL: the same add succeeds once the file exists.
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "Cargo.toml").write_text("[package]\n", encoding="utf-8")
        h.git_init(bundle)
        before = h.tree_digest(bundle)
        out = add_cmd(
            bundle, "--from", "supplies/Cargo.toml", "--to", "Cargo.toml",
            "--describe", "the manifest this course assumes",
        )
        check(out.returncode == 0, "it succeeded")
        check(h.tree_digest(bundle) != before, "and the digest oracle can tell")


def cli_case_describe_round_trips() -> None:
    """--describe is free text and goes through emit_scalar. Measured for
    belay-authoring#26: it already round-trips by value, in yamlite and in
    PyYAML, so this case pins that and no code changed. Without PyYAML the
    second half is reported as skipped, not passed."""
    try:
        import yaml as pyyaml
    except ImportError:
        pyyaml = None
        h.note("PyYAML is not importable: the PyYAML half of the --describe check was SKIPPED")
    for value in ("Needs: a manifest", "Trailing colon:", "- dash", "has a # hash",
                  'say "hi"', "true", "0.74", "[x] y", "it's"):
        with Workspace() as ws:
            bundle = ws.copy("rust-automaton-db")
            (bundle / "supplies").mkdir()
            (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
            h.git_init(bundle)
            out = add_cmd(bundle, "--from", "supplies/a.txt", "--to", "safe/a.txt",
                          "--describe", value)
            check(out.returncode == 0, f"describe {value!r}: exit 0 ({out.output[-200:]})")
            text = (bundle / "tutorial.yaml").read_text(encoding="utf-8")
            got = load_yaml(text, "m")["supplies"][-1]["describe"]
            check(got == value, f"describe {value!r}: yamlite reads {got!r}")
            if pyyaml is not None:
                got = pyyaml.safe_load(text)["supplies"][-1]["describe"]
                check(got == value, f"describe {value!r}: PyYAML reads {got!r}")


def cli_case_describe_unwritable() -> None:
    """A --describe emit_scalar cannot write is refused cleanly: non-zero,
    names the value, tree byte-identical. (belay#57: yamlite cannot read it.)"""
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        h.git_init(bundle)
        before = h.tree_digest(bundle)
        out = add_cmd(bundle, "--from", "supplies/a.txt", "--to", "safe/a.txt",
                      "--describe", 'say "hi # there')
        check(out.returncode != 0, "an unwritable --describe exits non-zero")
        check_in("cannot be written", out.output, "and says why")
        check(h.tree_digest(bundle) == before, "and the bundle is byte-identical")


def cli_case_to_escapes() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        h.git_init(bundle)

        refusal(
            "--to escaping the workspace",
            bundle,
            ("--from", "supplies/a.txt", "--to", "../x", "--describe", "escape attempt"),
            "is not allowed",
        )
        # POSITIVE CONTROL: a safe relative destination is accepted.
        ok = add_cmd(bundle, "--from", "supplies/a.txt", "--to", "safe/x.txt", "--describe", "a safe destination")
        check(ok.returncode == 0, "--to control: a safe relative destination is accepted")


def cli_case_to_tutorial() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        h.git_init(bundle)

        refusal(
            "--to starting tutorial/",
            bundle,
            ("--from", "supplies/a.txt", "--to", "tutorial/hack.txt", "--describe", "instance escape attempt"),
            "tutorial/",
        )
        # POSITIVE CONTROL: a destination not under tutorial/ is accepted.
        ok = add_cmd(bundle, "--from", "supplies/a.txt", "--to", "not-tutorial/x.txt", "--describe", "a safe destination")
        check(ok.returncode == 0, "--to control: a destination outside tutorial/ is accepted")


def cli_case_to_trailing_slash() -> None:
    """A `--to` ending in '/' is normalised, not refused (fix round 1).

    `--from` needs a trailing slash to mean "this directory's contents,
    recursively" (section 4.1). `--to` has no such meaning for the slash, so
    typing `--to models/` - a natural thing to do right after typing `--from
    supplies/models/` - must not produce a `to: models/` that trips check
    22's "path component '' is not allowed" on the empty component after the
    slash. It is stripped instead, and the PLAN must print the stripped
    value, because it differs from what the author typed.
    """
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies" / "models").mkdir(parents=True)
        (bundle / "supplies" / "models" / "duck.glb").write_text("glb\n", encoding="utf-8")
        h.git_init(bundle)

        result = add_cmd(
            bundle, "--from", "supplies/models/", "--to", "models/",
            "--describe", "the sample models this course assumes",
        )
        check(result.returncode == 0, "--to trailing slash: the add succeeds")
        check_in(
            "PASS", result.output,
            "--to trailing slash: the validator PASSES (check 22 clears it)",
        )
        check_in(
            "to       models\n",
            result.output,
            "--to trailing slash: the PLAN prints the normalised value, not the raw one",
        )

        manifest_text = (bundle / "tutorial.yaml").read_text(encoding="utf-8")
        check(
            "to: models\n" in manifest_text,
            f"--to trailing slash: the WRITTEN entry reads 'to: models', not "
            f"'to: models/' (manifest text: {manifest_text!r})",
        )
        check(
            "to: models/" not in manifest_text,
            "--to trailing slash: the raw trailing-slash form is not what got written",
        )

    with Workspace() as ws:
        # Normalising a trailing slash is not the same as relaxing the
        # escape check: a genuinely malformed --to, spelled WITH a trailing
        # slash, must still be refused.
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        h.git_init(bundle)

        refusal(
            "--to escaping the workspace, spelled with a trailing slash",
            bundle,
            ("--from", "supplies/a.txt", "--to", "../", "--describe", "escape attempt"),
            "is not allowed",
        )
        # POSITIVE CONTROL: the identical escape, without the trailing
        # slash, is refused the same way - proving the refusal is about the
        # escape, not incidentally about the slash this case adds.
        before = h.tree_digest(bundle)
        control = add_cmd(bundle, "--from", "supplies/a.txt", "--to", "..", "--describe", "escape attempt")
        check(control.returncode != 0, "--to escaping control: '..' without a trailing slash is refused too")
        check(h.tree_digest(bundle) == before, "--to escaping control: the bundle is unchanged")


def cli_case_empty_describe() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        h.git_init(bundle)

        refusal(
            "an empty --describe",
            bundle,
            ("--from", "supplies/a.txt", "--to", ".", "--describe", ""),
            "must not be empty",
        )
        # POSITIVE CONTROL: a non-empty description is accepted.
        ok = add_cmd(bundle, "--from", "supplies/a.txt", "--to", ".", "--describe", "the workspace")
        check(ok.returncode == 0, "--describe control: a non-empty description is accepted")


def cli_case_duplicate_entry() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        h.git_init(bundle)

        first = add_cmd(bundle, "--from", "supplies/a.txt", "--to", ".", "--describe", "the workspace")
        check(first.returncode == 0, "duplicate entry fixture: the first add succeeds")
        commit_all(bundle)

        refusal(
            "an identical entry already declared",
            bundle,
            ("--from", "supplies/a.txt", "--to", ".", "--describe", "the workspace"),
            "already declared",
        )
        # POSITIVE CONTROL: the same from/to with a DIFFERENT describe is a
        # different entry, and is accepted - the refusal is about identity,
        # not about repeating --from/--to.
        ok = add_cmd(bundle, "--from", "supplies/a.txt", "--to", ".", "--describe", "a different description")
        check(ok.returncode == 0, "duplicate entry control: a differing entry is accepted")


def cli_case_lesson_unknown() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        # A LESSON-scope 'from' must resolve inside lessons/, so the sample
        # cannot live in supplies/ at the bundle root: supplies/ is for
        # MANIFEST-scope entries, which are placed while the bundle source is
        # still in reach. This fixture uses the lesson's own folder; a loose
        # file directly under lessons/ would resolve just as well.
        lesson_from = folder_a_lesson(bundle, "00-foundations", "model.txt")
        h.git_init(bundle)

        refusal(
            "--lesson naming a lesson that does not exist",
            bundle,
            (
                "--from", lesson_from, "--to", "model.txt",
                "--describe", "the sample model", "--lesson", "nope-not-a-lesson",
            ),
            "does not name a lesson",
        )
        # POSITIVE CONTROL: the same command with a real lesson id succeeds,
        # and lands in that lesson's frontmatter, not the manifest.
        ok = add_cmd(
            bundle, "--from", lesson_from, "--to", "model.txt",
            "--describe", "the sample model", "--lesson", "00-foundations",
        )
        check(ok.returncode == 0, "--lesson control: a real lesson id is accepted")
        fm_text = (bundle / "lessons" / "00-foundations" / "LESSON.md").read_text(
            encoding="utf-8"
        )
        check("supplies:" in fm_text, "--lesson control: the entry lands in the lesson's frontmatter")
        manifest_text = (bundle / "tutorial.yaml").read_text(encoding="utf-8")
        check("supplies:" not in manifest_text, "--lesson control: and NOT in the manifest")


def cli_case_lesson_optional() -> None:
    """`--lesson` must accept a lesson that is listed ONLY in optional_lessons.

    Fix round 1, finding 2: `rust-automaton-db` has no optional lesson, so
    every earlier case here only ever exercised "not listed anywhere ->
    refused". `_resolve_lesson` consults `set(bundle.listed) | bundle.optional`
    (bundlelib's own defensive-but-shallow `Bundle.optional` property), and
    that union is exactly what this fixture is built to exercise: a lesson a
    reviewer could only prove correct earlier by constructing a `Bundle` by
    hand is now proven by a real bundle and a real subprocess run.

    `tests/fixtures/durable-event-broker-mini` is a trimmed copy of
    `durable-event-broker` (belay-bundles), the bundle named in the
    finding: two lessons on the main path, and `lessons/tcp-transport.md`
    declared only in `optional_lessons`, with `optional: true` in its own
    frontmatter, exactly like the original.
    """
    with Workspace() as ws:
        bundle = ws.copy("durable-event-broker-mini")
        # The certificate lives in the optional lesson's OWN folder: a
        # lesson-scope 'from' is placed from the instance, which carries only
        # lessons/.
        lesson_from = folder_a_lesson(bundle, "tcp-transport", "cert.pem")
        h.git_init(bundle)

        # ACCEPTED: the id names a lesson that is listed ONLY in
        # optional_lessons, not in lessons:.
        ok = add_cmd(
            bundle, "--from", lesson_from, "--to", "tls/cert.pem",
            "--describe", "the TLS certificate the TCP transport lesson uses",
            "--lesson", "tcp-transport",
        )
        check(
            ok.returncode == 0,
            "--lesson (optional_lessons only): a lesson listed only in "
            "optional_lessons is accepted",
        )
        check_in(
            "lesson scope: tcp-transport",
            ok.output,
            "--lesson (optional_lessons only): the plan names the lesson scope",
        )
        fm_text = (bundle / "lessons" / "tcp-transport" / "LESSON.md").read_text(
            encoding="utf-8"
        )
        check(
            "supplies:" in fm_text,
            "--lesson (optional_lessons only): the entry lands in that "
            "lesson's frontmatter",
        )
        manifest_text = (bundle / "tutorial.yaml").read_text(encoding="utf-8")
        check(
            "\nsupplies:" not in manifest_text,
            "--lesson (optional_lessons only): and NOT in the manifest",
        )

    with Workspace() as ws:
        # REFUSED: a lesson FILE that genuinely exists on disk, but that
        # tutorial.yaml names in NEITHER lessons: nor optional_lessons:, is
        # still refused. Consulting the union must not become "anything
        # under lessons/ is fair game" - this is the other half of the same
        # branch, and it is what stops _resolve_lesson from silently
        # widening into "any file on disk".
        bundle = ws.copy("durable-event-broker-mini")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "notes.txt").write_text("n\n", encoding="utf-8")
        (bundle / "lessons" / "orphan-lesson.md").write_text(
            "---\n"
            "id: orphan-lesson\n"
            "title: Not part of this bundle\n"
            "---\n\n"
            "## Purpose\n\n"
            "This file is on disk but named in neither lessons: nor "
            "optional_lessons:.\n",
            encoding="utf-8",
        )
        h.git_init(bundle)

        refusal(
            "--lesson naming a lesson that is on disk but in neither list",
            bundle,
            (
                "--from", "supplies/notes.txt", "--to", "notes.txt",
                "--describe", "orphan notes", "--lesson", "orphan-lesson",
            ),
            "not listed in tutorial.yaml's lessons: or optional_lessons:",
        )


def cli_case_dirty_tree() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        h.git_init(bundle)

        lesson_file = bundle / "lessons" / "00-foundations.md"
        lesson_file.write_text(
            lesson_file.read_text(encoding="utf-8") + "\nAn uncommitted edit.\n",
            encoding="utf-8",
        )

        tracked = refusal(
            "dirty tree (tracked file modified)",
            bundle,
            ("--from", "supplies/a.txt", "--to", ".", "--describe", "the workspace"),
            "uncommitted change(s)",
        )
        check_in("--force", tracked.output, "dirty tree: the message names --force as the override")

        # POSITIVE CONTROL: --force overrides, on the same still-dirty bundle.
        forced = add_cmd(
            bundle, "--from", "supplies/a.txt", "--to", ".", "--describe", "the workspace", "--force",
        )
        check(forced.returncode == 0, "dirty tree: --force runs anyway (exit 0)")
        check_in("--force was given", forced.output, "dirty tree: --force says out loud that it is overriding")
        check(
            "An uncommitted edit." in lesson_file.read_text(encoding="utf-8"),
            "dirty tree: --force left the author's uncommitted edit alone",
        )


def cli_case_check() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "workspace.txt").write_text("hello\n", encoding="utf-8")
        h.git_init(bundle)

        before = h.tree_digest(bundle)
        result = add_cmd(
            bundle, "--from", "supplies/workspace.txt", "--to", "workspace.txt",
            "--describe", "the starter file", "--check",
        )
        after = h.tree_digest(bundle)

        check(result.returncode == 0, "--check: exit 0")
        check(after == before, "--check: the bundle is byte-identical afterwards")
        check_in(
            "add: supplies entry (manifest scope",
            result.output,
            "--check: the plan names the manifest scope",
        )
        check_in("PASS", result.output, "--check: the validator reports PASS")
        check_in("nothing was written", result.output, "--check: the output says nothing was written")

        # POSITIVE CONTROL for the digest oracle itself: the identical
        # command WITHOUT --check must move it.
        real = add_cmd(
            bundle, "--from", "supplies/workspace.txt", "--to", "workspace.txt",
            "--describe", "the starter file",
        )
        check(real.returncode == 0, "--check control: the same add without --check succeeds")
        check(h.tree_digest(bundle) != before, "--check control: the digest DOES change for a real add")


def folder_a_lesson(bundle: Path, slug: str, material: str) -> str:
    """Turn a single-file lesson into a FOLDERED one carrying `material`.

    A lesson-scope supplies entry's `from` must resolve inside `lessons/`,
    because the entry is placed when that lesson opens - from the instance,
    which carries only `lessons/`. A FOLDER IS NOT REQUIRED TO SATISFY THAT.
    Measured against the pinned validator: a loose file directly under
    `lessons/` passes check 4, and a single-file lesson declaring
    `from: lessons/seed.txt` passes in bundle mode and in instance mode.
    Only a bare DIRECTORY under `lessons/` with no LESSON.md is refused by
    check 4, because nothing can reach what is inside it. This helper builds
    the FOLDERED case on purpose - it is the case check 6 also reaches - and
    that is a fixture choice, not a rule the format imposes.

    Returns the bundle-relative `from` for the material file. The LESSON.md
    body names the file as well, so the fixture is valid BEFORE the supplies
    entry is added and not only after it - otherwise this helper would be
    leaning on the very exemption the case under test is about to create.
    """
    lessons = bundle / "lessons"
    single = lessons / f"{slug}.md"
    assert single.is_file(), f"{single} is not a single-file lesson"
    folder = lessons / slug
    folder.mkdir()
    body = single.read_text(encoding="utf-8")
    single.unlink()
    (folder / "LESSON.md").write_text(
        body
        + f"\n## Material\n\nThe file `{material}` in this folder is the sample "
        f"this lesson reads.\n",
        encoding="utf-8",
    )
    (folder / material).write_text("sample\n", encoding="utf-8")
    repointed = 0
    for name in ("tutorial.yaml", "STATE.template.md"):
        path = bundle / name
        text = path.read_text(encoding="utf-8")
        if f"lessons/{slug}.md" not in text:
            # STATE.template.md names only lessons[0], so an optional lesson
            # is legitimately absent from it. tutorial.yaml is not optional.
            continue
        repointed += 1
        path.write_text(
            text.replace(f"lessons/{slug}.md", f"lessons/{slug}/LESSON.md"),
            encoding="utf-8",
        )
    assert repointed, f"{slug}: tutorial.yaml never named lessons/{slug}.md"
    return f"lessons/{slug}/{material}"


def cli_case_list() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        # One entry per scope, each taking its 'from' from a place that
        # scope may use: supplies/ at the bundle root for the manifest, the
        # lesson's own folder for the lesson. The lesson scope is only
        # required to stay under lessons/; the folder is this fixture's
        # choice, not the rule.
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "a.txt").write_text("a\n", encoding="utf-8")
        lesson_from = folder_a_lesson(bundle, "00-foundations", "b.txt")
        h.git_init(bundle)

        manifest_add = add_cmd(
            bundle, "--from", "supplies/a.txt", "--to", ".",
            "--describe", "the workspace this course assumes",
        )
        check(manifest_add.returncode == 0, "list fixture: the manifest-scope add succeeds")
        commit_all(bundle)

        lesson_add = add_cmd(
            bundle, "--from", lesson_from, "--to", "model.txt",
            "--describe", "the sample model this lesson uses", "--lesson", "00-foundations",
        )
        check(lesson_add.returncode == 0, "list fixture: the lesson-scope add succeeds")

        result = list_cmd(bundle)
        check(result.returncode == 0, "list: exit 0")
        check_in("[manifest]", result.output, "list: names the manifest scope")
        check_in("after materialization", result.output, "list: says when the manifest entry is placed")
        check_in("[lesson 00-foundations]", result.output, "list: names the lesson scope with its id")
        check_in("when that lesson opens", result.output, "list: says when the lesson entry is placed")
        check_in("supplies/a.txt", result.output, "list: names the manifest entry's from")
        check_in(lesson_from, result.output, "list: names the lesson entry's from")


# --------------------------------------------------------------------------
# Repair: an edit that removes the finding the bundle is invalid because of
# (belay-authoring#28)
#
# Every case asserts the outcome on disk AND the validator's own verdict on
# the result, not only the message text.
# --------------------------------------------------------------------------


def undeclared_bundle(ws: Workspace, names: tuple[str, ...], body_extra: str = "") -> Path:
    """rust-automaton-db with `names` as material in 00-foundations, none declared.

    Each name is a check 6 finding: the file is in a lesson folder and no
    LESSON.md line or supplies entry names it. `body_extra` is appended to the
    LESSON.md body (used to plant a finding that carries a line number).
    """
    bundle = ws.copy("rust-automaton-db")
    folder_from = None
    for index, name in enumerate(names):
        made = folder_a_lesson(bundle, "00-foundations", name) if index == 0 else None
        folder_from = folder_from or made
        (bundle / "lessons" / "00-foundations" / name).write_text("sample\n", encoding="utf-8")
    lesson = bundle / "lessons" / "00-foundations" / "LESSON.md"
    text = lesson.read_text(encoding="utf-8")
    # folder_a_lesson names its first material file in the body so the
    # fixture is valid before the entry exists. Here the opposite is wanted:
    # the file must be UNNAMED, so the finding exists.
    text = text.replace(f"`{names[0]}`", "the sample")
    lesson.write_text(text + body_extra, encoding="utf-8")
    h.git_init(bundle)
    return bundle


def verdict(bundle: Path) -> tuple[int, list[str]]:
    """The real validator's exit code and its findings (not warnings)."""
    run = bl.run_validator(bundle)
    entries, _ = run.failures()
    return run.returncode, entries


def lesson_add(bundle: Path, name: str, *extra: str, env: dict | None = None) -> h.Run:
    return add_cmd(
        bundle, "--from", f"lessons/00-foundations/{name}", "--to", name,
        "--describe", f"the {name} file", "--lesson", "00-foundations", *extra,
        env=env,
    )


def cli_case_repair_one_undeclared() -> None:
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("a.txt",))
        code, found = verdict(bundle)
        check(code == 1 and len(found) == 1 and "[check  6]" in found[0],
              "repair/one: the fixture really fails on one check 6 finding")
        result = lesson_add(bundle, "a.txt")
        check(result.returncode == 0, "repair/one: add exits 0")
        check_in("PASS", result.output, "repair/one: a fully repaired bundle says PASS")
        code, found = verdict(bundle)
        check(code == 0 and not found, "repair/one: the bundle validates clean afterwards")
        text = (bundle / "lessons" / "00-foundations" / "LESSON.md").read_text(encoding="utf-8")
        check("from: lessons/00-foundations/a.txt" in text, "repair/one: the entry is on disk")


def cli_case_repair_two_undeclared() -> None:
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("a.txt", "b.txt"))
        first = lesson_add(bundle, "a.txt")
        check(first.returncode == 0, "repair/two: the first add exits 0")
        check_in("still invalid", first.output, "repair/two: the first add says the bundle is still invalid")
        check_in("removed 1 finding(s); 1 remain", first.output, "repair/two: says what it removed and what remains")
        check_in("b.txt", first.output, "repair/two: lists the remaining finding")
        check_not_pass(first.output, "repair/two: the first add never prints PASS")
        text = (bundle / "lessons" / "00-foundations" / "LESSON.md").read_text(encoding="utf-8")
        check("from: lessons/00-foundations/a.txt" in text, "repair/two: the first entry is on disk")
        code, found = verdict(bundle)
        check(code == 1 and len(found) == 1 and "b.txt" in found[0],
              "repair/two: the validator agrees: one finding, for b.txt")

        commit_all(bundle)
        second = lesson_add(bundle, "b.txt")
        check(second.returncode == 0, "repair/two: the second add exits 0")
        code, found = verdict(bundle)
        check(code == 0 and not found, "repair/two: the bundle is clean after the second add")


def check_not_pass(output: str, description: str) -> None:
    lines = [ln for ln in output.splitlines() if "PASS" in ln]
    check(not lines, description)


def cli_case_repair_survives_a_line_shift() -> None:
    """A check 5 finding is located file:line; the frontmatter insert moves it."""
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("a.txt",), body_extra="\nStatus: done\n")
        code, found = verdict(bundle)
        five = [f for f in found if f.startswith("[check  5]")]
        check(code == 1 and len(five) == 1, "repair/shift: the fixture has a check 5 finding")
        before_line = five[0].split(":")[1].split()[0] if five else None
        result = lesson_add(bundle, "a.txt")
        check(result.returncode == 0, "repair/shift: a repair is accepted although the check 5 line moved")
        code, found = verdict(bundle)
        five_after = [f for f in found if f.startswith("[check  5]")]
        check(len(found) == 1 and len(five_after) == 1,
              "repair/shift: exactly the unrelated check 5 finding remains")
        after_line = five_after[0].split(":")[1].split()[0] if five_after else None
        check(before_line != after_line,
              f"repair/shift: the line number really did move ({before_line} -> {after_line})")


def cli_case_repair_unrelated_edit_refused() -> None:
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("b.txt",))
        (bundle / "supplies").mkdir()
        (bundle / "supplies" / "w.txt").write_text("w\n", encoding="utf-8")
        commit_all(bundle)
        before = h.tree_digest(bundle)
        result = add_cmd(
            bundle, "--from", "supplies/w.txt", "--to", "w.txt", "--describe", "unrelated"
        )
        check(result.returncode != 0, "repair/unrelated: exits non-zero")
        check_in("removed no finding", result.output, "repair/unrelated: says the edit removed no finding")
        check(h.tree_digest(bundle) == before, "repair/unrelated: the bundle is byte-identical")
        # POSITIVE CONTROL: the same unrelated edit is accepted on a valid bundle.
        valid = ws.copy("rust-automaton-db", "valid")
        (valid / "supplies").mkdir()
        (valid / "supplies" / "w.txt").write_text("w\n", encoding="utf-8")
        h.git_init(valid)
        ok = add_cmd(valid, "--from", "supplies/w.txt", "--to", "w.txt", "--describe", "unrelated")
        check(ok.returncode == 0, "repair/unrelated control: accepted on a valid bundle")


def cli_case_repair_adding_a_finding_refused() -> None:
    """Removes the check 6 finding, adds a check 22 one (`Tutorial/` is not refused by the tool)."""
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("a.txt", "b.txt"))
        before = h.tree_digest(bundle)
        result = add_cmd(
            bundle, "--from", "lessons/00-foundations/a.txt", "--to", "Tutorial/a.txt",
            "--describe", "bad target", "--lesson", "00-foundations",
        )
        check(result.returncode != 0, "repair/adds: exits non-zero")
        check_in("ADD 1 finding(s)", result.output, "repair/adds: says it adds a finding")
        check_in("[check 22]", result.output, "repair/adds: names the added finding")
        check_in("begins with 'tutorial/'", result.output, "repair/adds: gives the added finding's text")
        check_in("already had 2 finding(s)", result.output, "repair/adds: says how many were there before")
        check_not_in("b.txt", result.output, "repair/adds: does not list the pre-existing findings")
        check(h.tree_digest(bundle) == before, "repair/adds: the bundle is byte-identical")


def cli_case_valid_bundle_made_invalid_refused_as_before() -> None:
    with Workspace() as ws:
        bundle = ws.copy("rust-automaton-db")
        folder_a_lesson(bundle, "00-foundations", "a.txt")
        h.git_init(bundle)
        before = h.tree_digest(bundle)
        result = add_cmd(
            bundle, "--from", "lessons/00-foundations/a.txt", "--to", "Tutorial/a.txt",
            "--describe", "bad target", "--lesson", "00-foundations",
        )
        check(result.returncode != 0, "repair/valid: exits non-zero")
        check_in("would leave the bundle invalid", result.output, "repair/valid: the refusal text is the original")
        check_in("fix the existing findings first", result.output, "repair/valid: the original hint is kept")
        check_not_in("ADD", result.output, "repair/valid: none of the repair wording")
        check(h.tree_digest(bundle) == before, "repair/valid: the bundle is byte-identical")


def cli_case_repair_check_mode() -> None:
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("a.txt", "b.txt"))
        before = h.tree_digest(bundle)
        result = lesson_add(bundle, "a.txt", "--check")
        check(result.returncode == 0, "repair/--check: exit 0")
        check_in("nothing was written", result.output, "repair/--check: says nothing was written")
        check_in("would remove 1 finding(s); 1 would remain", result.output,
                 "repair/--check: reports the same verdict the real run gives")
        check_in("still be invalid", result.output, "repair/--check: says the bundle would still be invalid")
        check_not_pass(result.output, "repair/--check: never prints PASS")
        check(h.tree_digest(bundle) == before, "repair/--check: the bundle is byte-identical")
        real = lesson_add(bundle, "a.txt")
        check_in("removed 1 finding(s); 1 remain", real.output, "repair/--check control: the real run gives that verdict")


STUB_BY_STATE = """#!/usr/bin/env python3
import sys
from pathlib import Path
root = Path(sys.argv[-1])
text = (root / "lessons" / "00-foundations" / "LESSON.md").read_text()
edited = "supplies:" in text
code = int(Path(__file__).with_name("after" if edited else "before").read_text())
print("Belay validator - mode: bundle")
if code == 1:
    print("FAIL - 2 finding(s):" if not edited else "FAIL - 1 finding(s):")
    print("  [check  6] lessons/00-foundations/LESSON.md -> b.txt: x")
    if not edited:
        print("  [check  6] lessons/00-foundations/LESSON.md -> a.txt: x")
sys.exit(code)
"""


def cli_case_repair_indeterminate_is_refused() -> None:
    """Never accept on a run that did not complete: the stub exits 3 or 2."""
    for before_code, after_code, label in ((1, 3, "after exits 3"), (3, 1, "before exits 3"), (1, 2, "after exits 2")):
        with Workspace() as ws:
            bundle = undeclared_bundle(ws, ("a.txt",))
            stub = ws.root / "stub_validate_bundle.py"
            stub.write_text(STUB_BY_STATE, encoding="utf-8")
            (ws.root / "before").write_text(str(before_code))
            (ws.root / "after").write_text(str(after_code))
            before = h.tree_digest(bundle)
            result = lesson_add(bundle, "a.txt", env={"BELAY_VALIDATOR": str(stub)})
            check(result.returncode != 0, f"repair/indeterminate ({label}): refused")
            check_in("did not complete", result.output, f"repair/indeterminate ({label}): says the run did not complete")
            check(h.tree_digest(bundle) == before, f"repair/indeterminate ({label}): byte-identical")
    # POSITIVE CONTROL: the same stub with both runs exit 1 accepts the repair
    # (a=gone, b=still there), so the refusals above are the exit codes' doing.
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("a.txt",))
        stub = ws.root / "stub_validate_bundle.py"
        stub.write_text(STUB_BY_STATE, encoding="utf-8")
        (ws.root / "before").write_text("1")
        (ws.root / "after").write_text("1")
        result = lesson_add(bundle, "a.txt", env={"BELAY_VALIDATOR": str(stub)})
        check(result.returncode == 0, "repair/indeterminate control: both runs exit 1 -> accepted")


def test_failures_excludes_warnings_and_joins_continuations() -> None:
    out = (
        "Belay validator - mode: bundle\n\n"
        "FAIL - 2 finding(s):\n"
        "  [check  3] a.md: first\n"
        "  [check  6] b.md: second\n"
        "    continued\n"
        "\n"
        "1 warning(s) - these do not make the bundle invalid:\n"
        "  [check 28] tutorial.yaml: unknown field\n"
    )
    run = bl.ValidatorRun(Path("v"), "t", 1, out, "")
    entries, complete = run.failures()
    check(complete and len(entries) == 2, "failures: two findings, header count reproduced")
    check(entries[1].endswith("second\ncontinued"), "failures: a continuation line joins its finding")
    check(len(run.findings) == 3, "failures: control - `findings` DOES count the warning (why failures exists)")
    short = bl.ValidatorRun(Path("v"), "t", 1, out.replace("FAIL - 2", "FAIL - 3"), "")
    check(short.failures()[1] is False, "failures: a header count it cannot reproduce is incomplete")
    check(bl.ValidatorRun(Path("v"), "t", 1, "garbage", "").failures() == ([], False),
          "failures: no header is incomplete")


def test_finding_key_normalises_only_line_numbers() -> None:
    a = "[check  5] lessons/x.md:112: a Status: label appears here: 'Status: done'. why"
    b = "[check  5] lessons/x.md:116: a Status: label appears here: 'Status: done'. why"
    check(bl.finding_key(a) == bl.finding_key(b), "finding_key: a moved check 5 line compares equal")
    other = b.replace("'Status: done'", "'Status: open'")
    check(bl.finding_key(a) != bl.finding_key(other), "finding_key: a different marker still differs")
    elsewhere = b.replace("lessons/x.md", "lessons/y.md")
    check(bl.finding_key(a) != bl.finding_key(elsewhere), "finding_key: the same marker in another file differs")
    six = "[check  6] lessons/x/LESSON.md -> a.txt: the material file 'a.txt' is never named"
    check(bl.finding_key(six) == six, "finding_key: any other finding is compared exactly")
    p1 = "[check  3] lessons/x.md: the frontmatter does not parse: lessons/x.md frontmatter: line 4: bad"
    p2 = p1.replace("line 4", "line 8")
    check(bl.finding_key(p1) == bl.finding_key(p2), "finding_key: a moved parse-error line compares equal")


def test_staged_default_is_unchanged() -> None:
    """Without allow_repair, an edit that removes a finding is still refused."""
    with Workspace() as ws:
        bundle = undeclared_bundle(ws, ("a.txt", "b.txt"))
        before = h.tree_digest(bundle)
        refused = None
        try:
            with bl.Staged(bundle) as stage:
                lesson = stage.root / "lessons" / "00-foundations" / "LESSON.md"
                lesson.write_text(
                    lesson.read_text(encoding="utf-8") + "\nSee a.txt.\n", encoding="utf-8"
                )
                stage.commit()
        except bl.ToolError as exc:
            refused = str(exc)
        check(refused is not None and "would leave the bundle invalid" in refused,
              "Staged default: a partial repair is refused with the original text")
        check(refused is not None and "fix the existing findings first" in refused,
              "Staged default: the original 'already invalid' hint is kept")
        check(h.tree_digest(bundle) == before, "Staged default: byte-identical")
        # CONTROL: the same edit with the mode on is accepted.
        with bl.Staged(bundle, allow_repair=True) as stage:
            lesson = stage.root / "lessons" / "00-foundations" / "LESSON.md"
            lesson.write_text(lesson.read_text(encoding="utf-8") + "\nSee a.txt.\n", encoding="utf-8")
            stage.commit()
        check(h.tree_digest(bundle) != before, "Staged default control: allow_repair=True writes the same edit")


# --------------------------------------------------------------------------


def main() -> int:
    print("bundlelib supplies-entry writer test suite (unit half)")
    print()
    with case("emit_scalar round-trips the hard cases"):
        test_emit_scalar_round_trips_the_hard_cases()
    with case("emit_scalar does not confuse a string for the thing it looks like"):
        test_emit_scalar_does_not_confuse_a_string_for_the_thing_it_looks_like()
    with case("emit_scalar quotes what a conforming parser would reject"):
        test_emit_scalar_quotes_what_a_conforming_parser_would_reject()
    with case("render_supplies_item orders keys and indents two spaces"):
        test_render_supplies_item_orders_keys_and_indents_two_spaces()
    with case("render_supplies_item refuses a missing key"):
        test_render_supplies_item_refuses_a_missing_key()
    with case("add_supplies creates the block, then appends to it"):
        test_add_supplies_creates_the_block_then_appends_to_it()
    with case("add_supplies in frontmatter leaves the body alone"):
        test_add_supplies_in_frontmatter_leaves_the_body_alone()
    with case("add_supplies rewrites an empty inline supplies value"):
        test_add_supplies_rewrites_an_empty_inline_supplies_value()
    with case("add_supplies refuses a non-empty inline supplies value"):
        test_add_supplies_refuses_a_nonempty_inline_supplies_value()
    with case("add_supplies catches a broken emitter"):
        test_add_supplies_catches_a_broken_emitter()

    print()
    print("supplies.py command-line tool (CLI half)")
    print()
    try:
        validator, how = bl.find_validator()
        h.note(f"validator under test: {validator} ({how})")
    except bl.ToolError as exc:  # pragma: no cover - a machine without the runner
        print(f"  cannot run: {exc}")
        return 2

    with case("add refuses a --from that is not in the bundle"):
        cli_case_from_missing()
    with case("add --describe round-trips free text by value"):
        cli_case_describe_round_trips()
    with case("add refuses a --describe it cannot write, and writes nothing"):
        cli_case_describe_unwritable()
    with case("add refuses a --to that escapes the workspace"):
        cli_case_to_escapes()
    with case("add refuses a --to that begins with tutorial/"):
        cli_case_to_tutorial()
    with case("add normalises a --to ending in '/' instead of refusing it"):
        cli_case_to_trailing_slash()
    with case("add refuses an empty --describe"):
        cli_case_empty_describe()
    with case("add refuses an identical entry already declared"):
        cli_case_duplicate_entry()
    with case("add refuses a --lesson naming a lesson that does not exist"):
        cli_case_lesson_unknown()
    with case("--lesson accepts a lesson listed only in optional_lessons, and still refuses one in neither list"):
        cli_case_lesson_optional()
    with case("add refuses a dirty working tree, and --force overrides"):
        cli_case_dirty_tree()
    with case("add --check prints the plan and changes nothing"):
        cli_case_check()
    with case("list names every declared entry, its scope and its timing"):
        cli_case_list()

    print()
    print("repair: an edit that removes the finding the bundle is invalid because of")
    print()
    with case("failures() reads findings only, not warnings"):
        test_failures_excludes_warnings_and_joins_continuations()
    with case("finding_key normalises line numbers and nothing else"):
        test_finding_key_normalises_only_line_numbers()
    with case("Staged without allow_repair is unchanged"):
        test_staged_default_is_unchanged()
    with case("repair: one undeclared file, add leaves the bundle clean"):
        cli_case_repair_one_undeclared()
    with case("repair: two undeclared files, worked down one at a time"):
        cli_case_repair_two_undeclared()
    with case("repair: an unrelated finding whose line number moves does not block it"):
        cli_case_repair_survives_a_line_shift()
    with case("repair: an unrelated edit to an invalid bundle is refused"):
        cli_case_repair_unrelated_edit_refused()
    with case("repair: an edit that removes one finding and adds another is refused"):
        cli_case_repair_adding_a_finding_refused()
    with case("repair: an edit that makes a valid bundle invalid is refused as before"):
        cli_case_valid_bundle_made_invalid_refused_as_before()
    with case("repair: --check reports the verdict and writes nothing"):
        cli_case_repair_check_mode()
    with case("repair: a validator run that did not complete is never accepted"):
        cli_case_repair_indeterminate_is_refused()

    return report("test_supplies.py")


if __name__ == "__main__":
    sys.exit(main())
