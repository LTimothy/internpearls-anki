"""The public repo must not name the learner or assume her gender.

Both have happened: a first name was scrubbed from history in August and another
appeared in September. A sweep is a moment; this is what makes it hold.

Line-based text scanning, not git grep: git grep silently drops \\b word-boundary
anchors from an -E pattern rather than erroring, so a check built on it can report
zero hits while missing everything. Reading files directly with re gives real
word boundaries.
"""
import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Paths the guard does not police, each for a stated reason.
SKIP_PREFIXES = (
    "internpearls/vendor/",         # third party, not ours to rewrite
    "docs/addon/",                  # generated from internpearls/ by build.sh; the source is policed instead
    "CHANGELOG.md",                 # shipped history, an editorial decision
    "tests/test_repo_hygiene.py",   # this file names what it forbids
)

PRONOUNS = re.compile(r"\b(she|her|hers|herself)\b", re.I)

# A string literal may legitimately hold "her" as fixture DATA ("her-guid"), so only
# prose outside quotes is policed. The single-quote branch excludes a quote character
# immediately after a letter, because that is an apostrophe in a contraction or
# possessive ("learner's", "people's"), not a string delimiter; a real string literal's
# opening quote is never preceded by a letter. Without that guard, two apostrophes on
# one prose line (a docstring sentence with two possessives, say) would be misread as
# a quoted span and everything between them, possibly including a real violation,
# would be silently dropped from the scan.
QUOTED = re.compile(r'"[^"\n]*"' + r"|(?<![A-Za-z])'[^'\n]*'")

# Deliberately requires an underscore (or a plural-then-underscore) right after "her",
# not just any lowercase run: a naive `her[_a-z]*` also matches ordinary words like
# "here", "hero", and "herald" in comments and prose, since they satisfy the same
# pattern letter for letter. Requiring the underscore keeps this test scoped to
# identifiers (her_guid, _her_front_to_guid, hers_map) rather than English.
IDENTIFIER = re.compile(r"\b_?hers?_[a-z]*|\bby_her\b|Her[A-Z]")


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout.split()
    return [p for p in out if not p.startswith(SKIP_PREFIXES)]


def _read(path):
    with open(os.path.join(ROOT, path), encoding="utf8", errors="ignore") as fh:
        return fh.read()


def test_no_gendered_pronoun_in_tracked_prose():
    # Prose scan covers all file types that carry user-visible text: Python,
    # Markdown, JavaScript, HTML, YAML, JSON, shell scripts, and extensionless
    # tracked files (like workflows and config files in .github/). Fixture data
    # in quoted strings is excluded. The identifier test below stays Python-only
    # since gendered identifiers are a Python construct.
    offenders = []
    for path in _tracked():
        # Check if path matches one of the prose file types, or is extensionless
        has_prose_ext = path.endswith(
            (".py", ".md", ".js", ".mjs", ".html", ".yml", ".yaml", ".json", ".sh")
        )
        is_extensionless = not any(c == "." for c in path.split("/")[-1])

        if not (has_prose_ext or is_extensionless):
            continue
        for n, line in enumerate(_read(path).splitlines(), 1):
            if PRONOUNS.search(QUOTED.sub("", line)):
                offenders.append(f"{path}:{n}: {line.strip()}")
    assert not offenders, (
        "The public repo assumes the learner's gender. Write 'the learner':\n  "
        + "\n  ".join(offenders))


def test_no_gendered_identifier():
    offenders = []
    for path in _tracked():
        if not path.endswith(".py"):
            continue
        for n, line in enumerate(_read(path).splitlines(), 1):
            if IDENTIFIER.search(QUOTED.sub("", line)):
                offenders.append(f"{path}:{n}: {line.strip()}")
    assert not offenders, "Gendered identifier:\n  " + "\n  ".join(offenders)
