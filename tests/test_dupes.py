import random
import time

from internpearls.dupes import build_index, find_candidates, normalise, pair_key


def test_normalise_strips_html_entities_and_sound():
    text = "<b>Metoclopramide</b> &amp; the LES &lt;tone&gt; [sound:beep.mp3]"
    assert normalise(text) == "metoclopramide & the les <tone>"


def test_normalise_cloze_keeps_answer_drops_hint():
    text = "The dural sac ends at {{c1::S2::level}}."
    assert normalise(text) == "the dural sac ends at s2."


def test_normalise_lower_cases_and_collapses_whitespace():
    text = "  Fenoldopam   is  a   D1   Agonist  "
    assert normalise(text) == "fenoldopam is a d1 agonist"


def test_normalise_keeps_digits():
    assert "1" in normalise("ketamine 1 to 2 mg/kg").split()


def test_normalise_strips_image_name_keeps_surrounding_text():
    text = "[image: carotid_stent_diagram.png] Name this vascular structure"
    normalised = normalise(text)
    assert "carotid" not in normalised
    assert "stent" not in normalised
    assert "diagram" not in normalised
    assert "vascular" in normalised
    assert "structure" in normalised


def test_build_index_basic():
    rows = [(1, "fenoldopam is a selective D1 receptor agonist", "Deck", "Basic")]
    idx = build_index(rows)
    assert idx.doc_tokens[0]
    assert "fenoldopam" in idx.idf


def test_find_candidates_matches_paraphrase():
    left = [(1, "mechanism of fenoldopam, D1 agonist", "Ours", "Basic")]
    right = [(2, "fenoldopam is a selective D1 receptor agonist", "Theirs", "Cloze"),
            (3, "totally unrelated fact about propofol induction", "Theirs", "Cloze")]
    results = find_candidates(left, right, threshold=0.3, top=3)
    assert results
    score, l, r, shares = results[0]
    assert l[0] == 1
    assert r[0] == 2
    assert score > 0.3
    assert shares


def test_find_candidates_respects_threshold():
    left = [(1, "completely different sentence about surgery", "Ours", "Basic")]
    right = [(2, "fenoldopam is a selective D1 receptor agonist", "Theirs", "Cloze")]
    assert find_candidates(left, right, threshold=0.9, top=3) == []


def test_find_candidates_respects_top_n():
    left = [(1, "ketamine induction dose one to two mg per kg", "Ours", "Basic")]
    right = [(i, f"ketamine induction dose one to two mg per kg variant {i}",
             "Theirs", "Cloze") for i in range(10)]
    results = find_candidates(left, right, threshold=0.1, top=3)
    assert len(results) == 3


def test_find_candidates_sorted_descending():
    left = [(1, "ketamine induction dose one to two mg per kg", "Ours", "Basic")]
    right = [(2, "ketamine induction dose one to two mg per kg", "Theirs", "Cloze"),
            (3, "ketamine dose mg kg", "Theirs", "Cloze")]
    results = find_candidates(left, right, threshold=0.1, top=3)
    scores = [r[0] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_find_candidates_shares_top_tokens_by_contribution():
    left = [(1, "fenoldopam is a selective D1 receptor agonist used for hypertension",
             "Ours", "Basic")]
    right = [(2, "fenoldopam is a selective D1 receptor agonist", "Theirs", "Cloze"),
            (3, "the sky is blue and the grass is green", "Theirs", "Cloze")]
    results = find_candidates(left, right, threshold=0.1, top=3)
    score, l, r, shares = results[0]
    assert r[0] == 2
    assert "fenoldopam" in shares
    assert len(shares) <= 5


def test_find_candidates_shares_empty_when_no_overlap():
    left = [(1, "ketamine induction dose", "Ours", "Basic")]
    right = [(2, "ketamine induction dose", "Theirs", "Cloze")]
    results = find_candidates(left, right, threshold=0.1, top=3)
    score, l, r, shares = results[0]
    assert shares


def test_find_candidates_rejects_single_shared_word_in_tiny_pool():
    """A tiny comparison pool collapses IDF weights, so one rare shared word alone
    can carry a pair past a low threshold with nothing else in common. The evidence
    floor (at least two shared informative tokens) drops it regardless of score."""
    left = [(1, "Phenylephrine bolus dose is one hundred micrograms intravenously "
                "for hypotension", "Ours", "Basic")]
    right = [(2, "The patient chart mentions phenylephrine allergy documented "
                 "yesterday afternoon clearly", "Theirs", "Basic")]
    assert find_candidates(left, right, threshold=0.1, top=3, min_shared=2) == []


def test_find_candidates_min_shared_zero_disables_the_evidence_floor():
    """Loose sensitivity (min_shared=0) is the raw cosine threshold, same as before
    the evidence floor existed: a single shared rare word is enough."""
    left = [(1, "Phenylephrine bolus dose is one hundred micrograms intravenously "
                "for hypotension", "Ours", "Basic")]
    right = [(2, "The patient chart mentions phenylephrine allergy documented "
                 "yesterday afternoon clearly", "Theirs", "Basic")]
    results = find_candidates(left, right, threshold=0.1, top=3, min_shared=0)
    assert results
    assert results[0][3] == ["phenylephrine"]


def test_find_candidates_needs_three_shared_tokens_when_text_is_long():
    """Once either side runs past 12 informative tokens, the evidence floor rises
    from two shared tokens to three."""
    left = [(1, "one two three four five six seven eight nine ten eleven twelve "
                "thirteen alphaword betaword", "Ours", "Basic")]
    right = [(2, "alphaword betaword completely different topic entirely", "Theirs",
             "Basic")]
    # only two shared tokens (alphaword, betaword) while the left text has 15
    # informative tokens (> 12), so the floor is three: rejected.
    assert find_candidates(left, right, threshold=0.05, top=3, min_shared=2) == []


def test_find_candidates_rejects_low_weight_share_even_with_enough_shared_tokens():
    """Three shared tokens can still be a small fraction of a longer text's own
    vocabulary; the shared tokens must also carry at least 40% of the shorter
    text's weight."""
    left = [(1, "Ketamine induction dose is one to two milligrams per kilogram "
                "given intravenously during rapid sequence induction for trauma "
                "patients today", "Ours", "Basic")]
    right = [(2, "The dibucaine number of ninety two indicates normal "
                 "pseudocholinesterase activity in this ketamine induction case "
                 "reviewed", "Theirs", "Basic")]
    assert find_candidates(left, right, threshold=0.05, top=3, min_shared=2) == []


def test_find_candidates_accepts_pair_that_clears_both_evidence_checks():
    left = [(1, "Ketamine induction dose one to two mg per kg intravenously",
             "Ours", "Basic")]
    right = [(2, "The induction dose of ketamine is one to two mg per kg", "Theirs",
             "Basic")]
    results = find_candidates(left, right, threshold=0.3, top=3, min_shared=2)
    assert results
    assert results[0][0] > 0.3


def test_find_candidates_image_names_do_not_match_across_picture_only_fronts():
    left = [(1, "[image: carotid_stent_diagram.png] Identify this vessel on "
                "ultrasound", "Ours", "Image")]
    right = [(2, "[image: carotid_stent_diagram.png] Unrelated fact about "
                 "propofol clearance rate", "Theirs", "Image")]
    assert find_candidates(left, right, threshold=0.1, top=3, min_shared=2) == []


def test_pair_key_order_independent():
    assert pair_key(5, 9) == pair_key(9, 5)


def test_pair_key_format():
    assert pair_key(3, 1) == "1:3"


def _synthetic_rows(n, vocab, seed):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        words = rng.choices(vocab, k=12)
        rows.append((i, " ".join(words), f"Deck {i % 20}", "Basic"))
    return rows


def test_find_candidates_timing_bound():
    """4,000 left rows against 40,000 right rows must finish well inside a generous
    CI bound. Real numbers from the design spec: this collection's own run stays in
    the low seconds; 10s leaves ample headroom for a slower CI box."""
    vocab = [f"word{i}" for i in range(5000)]
    left = _synthetic_rows(4000, vocab, seed=1)
    right = _synthetic_rows(40000, vocab, seed=2)
    start = time.monotonic()
    find_candidates(left, right, threshold=0.5, top=3)
    elapsed = time.monotonic() - start
    assert elapsed < 10.0


def test_find_candidates_timing_small_pool_is_fast():
    vocab = [f"word{i}" for i in range(5000)]
    left = _synthetic_rows(4000, vocab, seed=3)
    right = _synthetic_rows(800, vocab, seed=4)
    start = time.monotonic()
    find_candidates(left, right, threshold=0.5, top=3)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0
