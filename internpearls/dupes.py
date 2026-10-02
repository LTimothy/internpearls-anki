"""Lexical duplicate detection: pure Python, no aqt/anki imports.

Finds near-duplicate notes across two pools (e.g. the add-on's own cards against
everything else in the collection) by IDF-weighted token cosine similarity, using an
inverted index so a search over tens of thousands of notes stays fast. Deliberately not
a string/sequence match (see the duplicate-scan spec): two notes stating the same fact
in different words share almost no substring but share the words that carry the fact,
which is what term weighting rewards.
"""
import html
import math
import re

# About thirty function words: common enough to appear in nearly every note, so they
# carry no signal about which two notes share a fact. Kept short and unambiguous rather
# than exhaustive.
STOP_WORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "on", "is", "are", "was", "were", "be",
    "been", "being", "and", "or", "but", "if", "then", "else", "for", "as", "at",
    "by", "with", "from", "that", "this", "these", "those", "it", "its",
    "which", "who", "what", "does", "did", "has", "have", "had", "not",
})

_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::.*?)?\}\}", re.S)
_SOUND_RE = re.compile(r"\[sound:[^\]]*\]", re.I)
_IMAGE_REF_RE = re.compile(r"\[image:[^\]]*\]", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[^\W_]+")
_CJK_RE = re.compile("[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
_NEGATION_WORDS = frozenset({"not", "no", "never", "without", "cannot"})
_NEGATION_RE = re.compile(r"\b(?:%s)\b|n['\u2019]t\b" % "|".join(_NEGATION_WORDS))

# Short tokens that still say which fact a card is about: roman numerals (a type or a
# factor number) and anything with a digit in it (T3, D1, S2, 5).
_ROMAN = frozenset({"i", "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"})
# Roman numerals that can name a class when two cards differ on them. "iv" is left out:
# it is usually intravenous, so IV against IM must not read as a class contrast.
_ROMAN_CLASS = _ROMAN - {"iv"}
_CLASS_CODE_RE = re.compile(r"[a-z]{1,3}\d+")
# A number or roman numeral (IV included) right after one of these words names a class
# ("type 2", "factor viii", "lead ii"); anywhere else a bare number is not one.
_CLASSIFIERS = {
    "type": "type", "types": "type", "class": "class", "classes": "class",
    "grade": "grade", "grades": "grade", "stage": "stage", "stages": "stage",
    "phase": "phase", "phases": "phase", "factor": "factor", "factors": "factor",
    "group": "group", "groups": "group", "generation": "generation",
    "generations": "generation", "degree": "degree", "degrees": "degree",
    "nerve": "nerve", "cn": "cn", "lead": "lead", "leads": "lead",
}
_CLASS_PHRASE_RE = re.compile(
    r"\b(%s)[\s-]+(\d+[a-z]?|[ivx]{1,4})\b" % "|".join(_CLASSIFIERS))

_GREEK = {
    "\u03b1": "alpha", "\u03b2": "beta", "\u03b3": "gamma", "\u03b4": "delta",
    "\u03b5": "epsilon", "\u03b6": "zeta", "\u03b7": "eta", "\u03b8": "theta",
    "\u03b9": "iota", "\u03ba": "kappa", "\u03bb": "lambda", "\u03bc": "mu",
    "\u03bd": "nu", "\u03be": "xi", "\u03bf": "omicron", "\u03c0": "pi",
    "\u03c1": "rho", "\u03c3": "sigma", "\u03c2": "sigma", "\u03c4": "tau",
    "\u03c5": "upsilon", "\u03c6": "phi", "\u03c7": "chi", "\u03c8": "psi",
    "\u03c9": "omega", "\u00b5": "mu",   # U+00B5 is the micro sign
}
_GREEK_NAMES = frozenset(_GREEK.values())
_GREEK_TABLE = {ord(ch): f" {name} " for ch, name in _GREEK.items()}


def normalise(text):
    """Plain, lower-case, whitespace-collapsed text for term matching.

    Cloze deletions are replaced by their answer text (the hint, if any, is dropped:
    it's a prompt for the learner, not part of the fact). Sound tags, image-name
    markers ("[image: name.png]", the same bracket a picture-only front is shown
    with) and HTML are stripped, entities decoded, everything lower-cased, and runs
    of whitespace collapsed to one space. A filename carries no fact of its own, so
    two picture-only cards must not match each other on a shared image name.
    """
    text = text or ""
    text = _CLOZE_RE.sub(lambda m: m.group(1), text)
    text = _SOUND_RE.sub(" ", text)
    text = _IMAGE_REF_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def _keep(tok):
    if tok in STOP_WORDS:
        return False
    return (len(tok) >= 3 or tok in _ROMAN or tok in _GREEK_NAMES or tok.isdigit()
            or any(ch.isdigit() for ch in tok))


def tokenize(text):
    """Word/number tokens from already-normalised text, dropping function words and
    anything under three characters unless it carries meaning: an all-digit or
    digit-bearing token (a dose, a percentage, T3), a roman numeral, or a Greek letter,
    which is spelled out so an alpha and a beta never collapse into the same token.
    Words are Unicode, so accented and non-Latin text tokenises; a run of CJK
    characters, which has no spaces to split on, becomes overlapping pairs, and any
    Latin text or digits beside it tokenise as usual."""
    out = []
    for tok in _TOKEN_RE.findall(text.translate(_GREEK_TABLE)):
        if _CJK_RE.search(tok):
            for run in _CJK_RE.findall(tok):
                out.extend([run] if len(run) < 2 else
                           [run[i:i + 2] for i in range(len(run) - 1)])
            out.extend(t for t in _TOKEN_RE.findall(_CJK_RE.sub(" ", tok))
                       if _keep(t))
        elif _keep(tok):
            out.append(tok)
    return out


def contrast_marks(tokens, text):
    """What two otherwise similar cards can differ on while meaning different things:
    the class-like tokens (Greek letters, roman numerals other than IV, and a letter
    code with a number such as T3, S2, D1; a bare number is not one), and the word that
    negates something, or "" when nothing is negated."""
    ids = frozenset(
        [t for t in tokens if t in _ROMAN_CLASS or t in _GREEK_NAMES
         or _CLASS_CODE_RE.fullmatch(t)]
        + [f"{_CLASSIFIERS[m.group(1)]} {m.group(2)}"
           for m in _CLASS_PHRASE_RE.finditer(text)])
    found = _NEGATION_RE.search(text)
    word = found.group() if found else ""
    negation = word if word in _NEGATION_WORDS or not word else "not"
    return ids, negation


def _class_conflict(a, b):
    ids_a, ids_b = a[0], b[0]
    return bool(ids_a and ids_b) and not (ids_a <= ids_b or ids_b <= ids_a)


def contrasts(a, b):
    """True when two `contrast_marks` disagree: one side negates and the other does not,
    or both name classes and neither's set contains the other's. A class named on one
    side only is a paraphrase, not a contrast."""
    return bool(a[1]) != bool(b[1]) or _class_conflict(a, b)


def _shown(token):
    if " " in token:
        word, value = token.split(" ")
        return f"{word} {value.upper()}"
    return token if token in _GREEK_NAMES else token.upper()


def _side(own, other):
    """The tokens only `own` has, as a label shows them: a bare "ii" is left out when
    "type ii" already says it."""
    only = own - other
    named = {t.split(" ")[1] for t in only if " " in t}
    return " ".join(_shown(t) for t in sorted(only) if " " in t or t not in named)


def contrast_label(text_a, text_b):
    """The reason two texts contrast, for a row to show ("Differs: T3 vs T4",
    "Differs: not"), or "" when they do not."""
    norm_a, norm_b = normalise(text_a), normalise(text_b)
    a = contrast_marks(tokenize(norm_a), norm_a)
    b = contrast_marks(tokenize(norm_b), norm_b)
    parts = []
    if bool(a[1]) != bool(b[1]):
        parts.append(a[1] or b[1])
    if _class_conflict(a, b):
        parts.append(f"{_side(a[0], b[0])} vs {_side(b[0], a[0])}")
    return f"Differs: {'; '.join(parts)}" if parts else ""


# A contrasting pair keeps only this share of its cosine score. It sits strictly below the
# lowest threshold that applies the evidence gate (Normal, 0.5), so such a pair can appear
# only at Loose sensitivity and never reads as an exact duplicate.
CONTRAST_FACTOR = 0.45


class Index:
    """An inverted index over a pool of rows, with IDF weights and per-document
    weight vectors, built once and reused for every query against that pool."""

    __slots__ = ("rows", "doc_tokens", "doc_marks", "idf", "postings", "doc_norm",
                 "doc_weight_sum")

    def __init__(self, rows, doc_tokens, idf, postings, doc_norm, doc_weight_sum,
                 doc_marks):
        self.rows = rows
        self.doc_tokens = doc_tokens
        self.doc_marks = doc_marks
        self.idf = idf
        self.postings = postings
        self.doc_norm = doc_norm
        self.doc_weight_sum = doc_weight_sum


def build_index(rows, checkpoint=None):
    """Build an `Index` over `rows`, each `(note_id, text, deck_name, note_type)`.

    IDF is computed over this pool alone: a query against it re-uses these weights,
    since a token's rarity in the pool being searched is what should decide how much
    it counts.
    """
    doc_tokens = []
    for index, (_, text, _, _) in enumerate(rows):
        if checkpoint is not None and index % 25 == 0:
            checkpoint(f"duplicate-index:right:batch:{index // 25 + 1}")
        doc_tokens.append(tokenize(normalise(text)))
    n = len(rows)
    df = {}
    for index, tokens in enumerate(doc_tokens):
        if checkpoint is not None and index % 25 == 0:
            checkpoint(f"duplicate-index:frequency:batch:{index // 25 + 1}")
        for tok in set(tokens):
            df[tok] = df.get(tok, 0) + 1
    idf = {}
    for index, (tok, count) in enumerate(df.items()):
        if checkpoint is not None and index % 25 == 0:
            checkpoint(f"duplicate-index:weights:batch:{index // 25 + 1}")
        idf[tok] = math.log((n + 1) / (count + 0.5)) + 1.0

    postings = {}
    doc_norm = [0.0] * n
    doc_weight_sum = [0.0] * n
    for i, tokens in enumerate(doc_tokens):
        if checkpoint is not None and i % 25 == 0:
            checkpoint(f"duplicate-index:postings:batch:{i // 25 + 1}")
        tf = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        weights = {tok: count * idf[tok] for tok, count in tf.items()}
        doc_norm[i] = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        doc_weight_sum[i] = sum(weights.values())
        for tok, w in weights.items():
            postings.setdefault(tok, []).append((i, w))

    return Index(rows=rows, doc_tokens=doc_tokens, idf=idf, postings=postings,
                doc_norm=doc_norm, doc_weight_sum=doc_weight_sum,
                doc_marks=[None] * n)


def find_candidates(left_rows, right_rows, threshold=0.5, top=3, min_shared=2,
                    ignored=(), checkpoint=None):
    """For each row on the left, the best `top` rows on the right at cosine
    similarity >= `threshold`, as `[(score, left_row, right_row, shares)]` sorted by
    score descending (ties broken by left row order, then right row order). `shares`
    is that pair's top five shared tokens (query weight times document weight,
    descending), the terms that actually carried the score, for a screen to show
    the reader what the number means rather than just the number itself.

    A cosine score alone rewards a tiny corpus where the IDF weights collapse and one
    rare shared word can carry a pair over `threshold` with nothing else in common.
    `min_shared` (0 to disable) sets an evidence floor a pair must also clear: at
    least `min_shared` distinct informative tokens shared (one more when either side
    has more than 12 informative tokens), and those shared tokens must carry at least
    40% of the shorter side's own token weight. A pair that fails either check is
    dropped outright, whatever its cosine score says.

    A pair whose texts disagree on a type, class or number token, or on whether
    anything is negated (see `contrasts`), keeps only `CONTRAST_FACTOR` of its cosine
    score before the threshold applies, so it never scores as an exact duplicate.

    Builds one `Index` over `right_rows` and queries it once per left row, walking
    only the postings lists for tokens the query actually has (an inverted index),
    so the cost tracks how many terms actually overlap rather than the size of the
    right pool. Pair keys in `ignored` are removed before the per-left `top` limit,
    so later candidates replenish ignored results instead of being hidden by them.
    """
    index = build_index(right_rows, checkpoint=checkpoint)
    out = []
    for li, left in enumerate(left_rows):
        if checkpoint is not None and li % 25 == 0:
            checkpoint(f"duplicate-index:left:batch:{li // 25 + 1}")
        _, text, _, _ = left
        tokens = tokenize(normalise(text))
        marks = None
        tf = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        # A token absent from the searched pool has document frequency zero, not no
        # weight. Keeping that defined IDF in the query vector makes unmatched query
        # vocabulary count against both cosine similarity and the shorter-side
        # evidence share instead of disappearing from both denominators.
        unseen_idf = math.log((len(right_rows) + 1) / 0.5) + 1.0
        query = {tok: count * index.idf.get(tok, unseen_idf)
                 for tok, count in tf.items()}
        if not query:
            continue
        q_norm = math.sqrt(sum(w * w for w in query.values())) or 1.0
        q_weight_sum = sum(query.values())
        scores = {}
        contrib = {}
        doc_contrib = {}
        for tok, qw in query.items():
            for ri, dw in index.postings.get(tok, ()):
                value = qw * dw
                scores[ri] = scores.get(ri, 0.0) + value
                contrib.setdefault(ri, {})[tok] = value
                doc_contrib.setdefault(ri, {})[tok] = dw
        ranked = []
        for ri, dot in scores.items():
            if pair_key(left[0], right_rows[ri][0]) in ignored:
                continue
            cosine = dot / (q_norm * index.doc_norm[ri])
            if cosine < threshold:
                continue
            # Marks are read only for pairs that already clear the threshold, which
            # is a small share of the pairs sharing a token.
            if marks is None:
                marks = contrast_marks(tokens, normalise(text))
            if index.doc_marks[ri] is None:
                index.doc_marks[ri] = contrast_marks(
                    index.doc_tokens[ri], normalise(index.rows[ri][1]))
            if contrasts(marks, index.doc_marks[ri]):
                cosine *= CONTRAST_FACTOR
                if cosine < threshold:
                    continue
            if min_shared:
                shared = contrib.get(ri, {})
                required = min_shared
                if len(tokens) > 12 or len(index.doc_tokens[ri]) > 12:
                    required += 1
                if len(shared) < required:
                    continue
                q_shorter = len(tokens) <= len(index.doc_tokens[ri])
                shorter_total = q_weight_sum if q_shorter else index.doc_weight_sum[ri]
                shorter_shared = (sum(query[t] for t in shared) if q_shorter
                                  else sum(doc_contrib[ri][t] for t in shared))
                if not shorter_total or shorter_shared / shorter_total < 0.4:
                    continue
            ranked.append((cosine, ri))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        for cosine, ri in ranked[:top]:
            top_tokens = sorted(contrib.get(ri, {}).items(), key=lambda kv: -kv[1])[:5]
            shares = [tok for tok, _ in top_tokens]
            out.append((cosine, left, right_rows[ri], shares))
    out.sort(key=lambda t: -t[0])
    return out


def pair_key(a, b):
    """A stable key for a pair of note ids, for the ignore list: order-independent,
    so ignoring (a, b) also matches a rescan that offers (b, a)."""
    x, y = sorted((a, b))
    return f"{x}:{y}"
