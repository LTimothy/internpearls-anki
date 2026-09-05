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
_TAG_RE = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def normalise(text):
    """Plain, lower-case, whitespace-collapsed text for term matching.

    Cloze deletions are replaced by their answer text (the hint, if any, is dropped:
    it's a prompt for the learner, not part of the fact). Sound tags and HTML are
    stripped, entities decoded, everything lower-cased, and runs of whitespace
    collapsed to one space.
    """
    text = text or ""
    text = _CLOZE_RE.sub(lambda m: m.group(1), text)
    text = _SOUND_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = text.lower()
    return re.sub(r"\s+", " ", text).strip()


def tokenize(text):
    """Word/number tokens from already-normalised text, dropping function words and
    anything under three characters unless it's all digits (a dose or a percentage is
    exactly the kind of short token worth keeping)."""
    out = []
    for tok in _TOKEN_RE.findall(text):
        if tok in STOP_WORDS:
            continue
        if len(tok) < 3 and not tok.isdigit():
            continue
        out.append(tok)
    return out


class Index:
    """An inverted index over a pool of rows, with IDF weights and per-document
    weight vectors, built once and reused for every query against that pool."""

    __slots__ = ("rows", "doc_tokens", "idf", "postings", "doc_norm")

    def __init__(self, rows, doc_tokens, idf, postings, doc_norm):
        self.rows = rows
        self.doc_tokens = doc_tokens
        self.idf = idf
        self.postings = postings
        self.doc_norm = doc_norm


def build_index(rows):
    """Build an `Index` over `rows`, each `(note_id, text, deck_name, note_type)`.

    IDF is computed over this pool alone: a query against it re-uses these weights,
    since a token's rarity in the pool being searched is what should decide how much
    it counts.
    """
    doc_tokens = [tokenize(normalise(text)) for _, text, _, _ in rows]
    n = len(rows)
    df = {}
    for tokens in doc_tokens:
        for tok in set(tokens):
            df[tok] = df.get(tok, 0) + 1
    idf = {tok: math.log((n + 1) / (count + 0.5)) + 1.0 for tok, count in df.items()}

    postings = {}
    doc_norm = [0.0] * n
    doc_weights = []
    for i, tokens in enumerate(doc_tokens):
        tf = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        weights = {tok: count * idf[tok] for tok, count in tf.items()}
        doc_weights.append(weights)
        doc_norm[i] = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        for tok, w in weights.items():
            postings.setdefault(tok, []).append((i, w))

    return Index(rows=rows, doc_tokens=doc_tokens, idf=idf, postings=postings,
                doc_norm=doc_norm)


def find_candidates(left_rows, right_rows, threshold=0.5, top=3):
    """For each row on the left, the best `top` rows on the right at cosine
    similarity >= `threshold`, as `[(score, left_row, right_row, shares)]` sorted by
    score descending (ties broken by left row order, then right row order). `shares`
    is that pair's top five shared tokens (query weight times document weight,
    descending), the terms that actually carried the score, for a screen to show
    the reader what the number means rather than just the number itself.

    Builds one `Index` over `right_rows` and queries it once per left row, walking
    only the postings lists for tokens the query actually has (an inverted index),
    so the cost tracks how many terms actually overlap rather than the size of the
    right pool.
    """
    index = build_index(right_rows)
    out = []
    for li, left in enumerate(left_rows):
        _, text, _, _ = left
        tokens = tokenize(normalise(text))
        tf = {}
        for tok in tokens:
            tf[tok] = tf.get(tok, 0) + 1
        query = {tok: count * index.idf[tok] for tok, count in tf.items()
                if tok in index.idf}
        if not query:
            continue
        q_norm = math.sqrt(sum(w * w for w in query.values())) or 1.0
        scores = {}
        contrib = {}
        for tok, qw in query.items():
            for ri, dw in index.postings.get(tok, ()):
                value = qw * dw
                scores[ri] = scores.get(ri, 0.0) + value
                contrib.setdefault(ri, {})[tok] = value
        ranked = []
        for ri, dot in scores.items():
            cosine = dot / (q_norm * index.doc_norm[ri])
            if cosine >= threshold:
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
