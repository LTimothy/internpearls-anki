import random
import time

from internpearls.dupes import (build_index, find_candidates, normalise, pair_key,
                                tokenize)


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
    left = [(1, "phenylephrine bolus", "Ours", "Basic")]
    right = [(2, "phenylephrine allergy", "Theirs", "Basic")]
    assert find_candidates(left, right, threshold=0.1, top=3, min_shared=2) == []


def test_find_candidates_min_shared_zero_disables_the_evidence_floor():
    """Loose sensitivity (min_shared=0) is the raw cosine threshold, same as before
    the evidence floor existed: a single shared rare word is enough."""
    left = [(1, "phenylephrine bolus", "Ours", "Basic")]
    right = [(2, "phenylephrine allergy", "Theirs", "Basic")]
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


def test_find_candidates_query_only_vocabulary_counts_in_small_and_large_pools():
    """Words found only on the query side must still dilute a mostly-disjoint pair.
    The result must not depend on whether the searched pool is tiny or substantial."""
    left = [(1, "orchid amber banana cherry date fig grape", "Ours", "Basic")]
    target = (2, "orchid amber orchid amber orchid amber orchid", "Theirs", "Basic")
    filler = [(i + 3, f"unrelatedtoken{i} separateword{i}", "Theirs", "Basic")
              for i in range(99)]

    assert find_candidates(left, [target], threshold=0.6, min_shared=2) == []
    assert find_candidates(left, [target, *filler], threshold=0.6,
                           min_shared=2) == []


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


def _score(left_text, right_text, **kw):
    left = [(1, left_text, "Ours", "Basic")]
    right = [(2, right_text, "Theirs", "Basic")]
    kw.setdefault("threshold", 0.1)
    kw.setdefault("min_shared", 2)
    found = find_candidates(left, right, **kw)
    return found[0][0] if found else None


def test_tokenize_keeps_short_tokens_that_carry_meaning():
    toks = tokenize(normalise("T3 and T4, Type II, Factor V, D1 and S2, 5 mg"))
    for kept in ("t3", "t4", "ii", "d1", "s2", "5", "type"):
        assert kept in toks
    assert "v" in toks
    assert "mg" not in toks


def test_tokenize_spells_greek_letters_and_keeps_unicode_words():
    assert tokenize(normalise("\u03b12 agonist")) == ["alpha", "2", "agonist"]
    assert tokenize(normalise("\u03b2-blocker")) == ["beta", "blocker"]
    assert tokenize(normalise("Der Blutdruck f\u00e4llt unter an\u00e4sthesie")) == [
        "der", "blutdruck", "f\u00e4llt", "unter", "an\u00e4sthesie"]
    assert "\u9ebb\u9189" in tokenize(normalise("\u5168\u8eab\u9ebb\u9189\u8584"))


def test_non_english_cards_match_each_other():
    left = "Der Blutdruck f\u00e4llt unter Propofol Narkose stark"
    right = "Unter Propofol Narkose f\u00e4llt der Blutdruck stark ab"
    assert _score(left, right, threshold=0.5) is not None


def test_cjk_cards_match_each_other():
    left = "\u5168\u8eab\u9ebb\u9189\u7684\u8bf1\u5bfc\u836f\u7269"
    right = "\u5168\u8eab\u9ebb\u9189\u8bf1\u5bfc\u836f\u7269\u5265\u8131"
    assert _score(left, right, threshold=0.3, min_shared=0) is not None


def test_differing_class_or_number_token_is_not_an_exact_duplicate():
    pairs = [
        ("T3 raises the basal metabolic rate", "T4 raises the basal metabolic rate"),
        ("alpha-2 agonist lowers the MAC of volatile agents",
         "beta-2 agonist lowers the MAC of volatile agents"),
        ("\u03b12 agonist lowers the MAC of volatile agents",
         "\u03b22 agonist lowers the MAC of volatile agents"),
        ("Type I hypersensitivity is mediated by IgE antibodies",
         "Type II hypersensitivity is mediated by IgE antibodies"),
        ("Phase 1 block shows fade on train of four",
         "Phase 2 block shows fade on train of four"),
    ]
    for a, b in pairs:
        identical = _score(a, a)
        assert identical is not None and identical > 0.99
        score = _score(a, b)
        assert score is None or score < 0.7, (a, b, score)


def test_differing_negation_is_not_an_exact_duplicate():
    a = "Succinylcholine is contraindicated in malignant hyperthermia history"
    b = "Succinylcholine is not contraindicated in malignant hyperthermia history"
    score = _score(a, b)
    assert score is None or score < 0.7
    c = "Nitrous oxide never causes diffusion hypoxia during induction"
    d = "Nitrous oxide causes diffusion hypoxia during induction"
    score = _score(c, d)
    assert score is None or score < 0.7


def test_same_negation_and_same_numbers_still_score_high():
    a = "Succinylcholine is not safe in hyperkalemia with 5 mEq elevation"
    b = "In hyperkalemia with 5 mEq elevation succinylcholine is not safe"
    assert _score(a, b) > 0.95
    c = "Type II error is not the same as a type I error in statistics"
    d = "A type I error is not the same as a type II error in statistics"
    assert _score(c, d) > 0.95


def test_a_number_missing_from_one_side_is_not_a_contrast():
    a = "Ketamine induction dose is 1 to 2 mg per kg intravenously"
    b = "Ketamine induction dose is 1 to 2 mg per kg intravenously, max 5 total"
    assert _score(a, b) > 0.8
    c = "Ketamine induction dose per kg intravenously"
    assert _score(a, c, threshold=0.3) is not None
    assert _score(a, c, threshold=0.3) > 0.5


def test_contrast_pairs_do_not_flood_a_strict_scan():
    left = [(1, "T3 raises the basal metabolic rate", "Ours", "Basic")]
    right = [(i + 2, f"T{i % 5 + 4} raises the basal metabolic rate", "Theirs", "Basic")
             for i in range(5)]
    found = find_candidates(left, right, threshold=0.6, top=3, min_shared=2)
    assert all(r[2][0] != 2 for r in found)
