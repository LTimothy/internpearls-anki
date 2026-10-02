import random
import time

from internpearls.dupes import (CONTRAST_FACTOR, build_index, contrast_label,
                                contrast_marks, find_candidates, normalise, pair_key, tokenize)


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


def _reference_seconds():
    """Time of a small scan on this machine right now. A bound that passes on a quiet
    machine in absolute seconds may also pass by being a multiple of this, which moves
    with load (a busy machine slowed the big scan about 10x but the Python loop 2x, so
    only the same code is a fair yardstick)."""
    vocab = [f"word{i}" for i in range(5000)]
    left = _synthetic_rows(400, vocab, seed=5)
    right = _synthetic_rows(4000, vocab, seed=6)
    start = time.monotonic()
    find_candidates(left, right, threshold=0.5, top=3)
    return time.monotonic() - start


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
    reference = _reference_seconds()
    start = time.monotonic()
    find_candidates(left, right, threshold=0.5, top=3)
    elapsed = time.monotonic() - start
    # The scan is about 50 times the reference one; a brute-force pass would be far past
    # 150 times it.
    assert elapsed < 10.0 or elapsed < 150 * reference, (
        f"{elapsed:.1f}s against a {reference:.2f}s reference scan")


def test_find_candidates_timing_small_pool_is_fast():
    vocab = [f"word{i}" for i in range(5000)]
    left = _synthetic_rows(4000, vocab, seed=3)
    right = _synthetic_rows(800, vocab, seed=4)
    reference = _reference_seconds()
    start = time.monotonic()
    find_candidates(left, right, threshold=0.5, top=3)
    elapsed = time.monotonic() - start
    assert elapsed < 1.0 or elapsed < 6 * reference, (
        f"{elapsed:.2f}s against a {reference:.2f}s reference scan")


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
        ("\u03bc-opioid receptors mediate analgesia and respiratory depression",
         "\u03ba-opioid receptors mediate analgesia and respiratory depression"),
        ("mu opioid receptors mediate analgesia and respiratory depression",
         "kappa opioid receptors mediate analgesia and respiratory depression"),
        ("\u00b5 opioid receptors mediate analgesia and respiratory depression",
         "\u03ba opioid receptors mediate analgesia and respiratory depression"),
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


def test_short_greek_names_survive_tokenizing():
    assert tokenize(normalise("\u03bc-opioid agonist")) == ["mu", "opioid", "agonist"]
    assert tokenize(normalise("\u00b5g per kg")) == ["mu", "per"]
    assert tokenize(normalise("nu xi pi receptors")) == ["nu", "xi", "pi", "receptors"]


def test_a_differing_bare_number_is_not_a_contrast():
    a = "Ketamine onset after intravenous dosing is about 30 s with a short duration"
    b = "Ketamine onset after intravenous dosing is about 60 s with a short duration"
    assert contrast_label(a, b) == ""
    assert _score(a, b) > 0.7
    # a differing dose still scores as an ordinary pair, found at Normal sensitivity
    dose = _score("Ketamine dose 2 mg per kg", "Ketamine dose 5 mg per kg",
                  threshold=0.5, min_shared=2)
    assert dose is not None and dose > 0.5


def test_intravenous_is_not_a_roman_class():
    a = "Ketamine IV bolus produces dissociation within one minute"
    b = "Ketamine IM bolus produces dissociation within one minute"
    assert contrast_label(a, b) == ""
    assert _score(a, b) > 0.7
    assert contrast_marks(["iv", "ii", "t3"], "")[0] == frozenset({"ii", "t3"})


_LONG_T3 = ("T3 raises basal metabolic rate and heart rate through nuclear receptor "
            "transcription in most tissues including cardiac muscle liver kidney and "
            "skeletal muscle during prolonged fasting states")
_LONG_T4 = _LONG_T3.replace("T3", "T4")


def test_contrast_factor_sits_below_the_normal_threshold():
    assert CONTRAST_FACTOR < 0.5


def test_contrasting_pairs_appear_only_at_loose_sensitivity():
    levels = {"strict": (0.6, 2), "normal": (0.5, 2), "loose": (0.4, 0)}
    for a, b in ((_LONG_T3, _LONG_T4),
                 ("Succinylcholine is contraindicated in hyperkalemia with burns "
                  "after major trauma today including crush injuries and denervation "
                  "syndromes with prolonged immobilization",
                  "Succinylcholine is not contraindicated in hyperkalemia with burns "
                  "after major trauma today including crush injuries and denervation "
                  "syndromes with prolonged immobilization")):
        raw = _score(a, a, threshold=0.0)
        assert raw > 0.99
        found = {name: _score(a, b, threshold=t, min_shared=m)
                 for name, (t, m) in levels.items()}
        assert found["strict"] is None, (a, found)
        assert found["normal"] is None, (a, found)
        assert found["loose"] is not None and found["loose"] < 0.5, (a, found)
    # the same pair without a contrast is found at every level
    for name, (t, m) in levels.items():
        assert _score(_LONG_T3, _LONG_T3 + " today", threshold=t, min_shared=m)


def test_contrast_label_names_what_differs():
    assert contrast_label(_LONG_T3, _LONG_T4) == "Differs: T3 vs T4"
    assert contrast_label("Type I error", "Type II error") == "Differs: type I vs type II"
    assert contrast_label("alpha-2 agonist", "beta-2 agonist") == "Differs: alpha vs beta"
    assert contrast_label("Drug is contraindicated here",
                          "Drug is not contraindicated here") == "Differs: not"
    assert contrast_label("Drug is contraindicated here",
                          "Drug isn't contraindicated here") == "Differs: not"
    assert contrast_label("Never give it", "Give it") == "Differs: never"
    assert contrast_label("T3 is not active", "T4 is active").count("; ") == 1
    assert contrast_label("T3 raises rate", "T3 raises rate") == ""
    assert contrast_label("T3 raises rate", "raises rate") == ""


def test_cjk_with_latin_and_digits_keeps_the_latin_tokens():
    toks = tokenize(normalise("\u5168\u8eab\u9ebb\u9189 propofol 2 mg \u5265\u8131x5"))
    assert "propofol" in toks
    assert "2" in toks
    assert "x5" in toks
    assert "\u5168\u8eab" in toks
    assert tokenize(normalise("\u9ebb\u9189propofol")) == ["\u9ebb\u9189", "propofol"]


def test_contrast_pairs_do_not_flood_a_strict_scan():
    left = [(1, _LONG_T3, "Ours", "Basic")]
    right = [(i + 2, _LONG_T3.replace("T3", f"T{i % 5 + 4}"), "Theirs", "Basic")
             for i in range(5)]
    assert find_candidates(left, right, threshold=0.6, top=3, min_shared=2) == []
    assert find_candidates(left, right, threshold=0.5, top=3, min_shared=2) == []


def test_a_number_after_a_classifier_word_is_a_class():
    pairs = [
        ("Type 1 diabetes results from autoimmune destruction of pancreatic beta cells",
         "Type 2 diabetes results from autoimmune destruction of pancreatic beta cells",
         "Differs: type 1 vs type 2"),
        ("Type IV renal tubular acidosis causes hyperkalemia with low aldosterone",
         "Type II renal tubular acidosis causes hyperkalemia with low aldosterone",
         "Differs: type IV vs type II"),
        ("Factor VIII deficiency prolongs the aPTT in patients with bleeding history",
         "Factor IX deficiency prolongs the aPTT in patients with bleeding history",
         "Differs: factor VIII vs factor IX"),
        ("Phase 2 block shows fade on train of four stimulation at the adductor",
         "Phase 3 block shows fade on train of four stimulation at the adductor",
         "Differs: phase 2 vs phase 3"),
        ("ST elevation in lead II suggests an inferior wall infarct on the tracing",
         "ST elevation in lead III suggests an inferior wall infarct on the tracing",
         "Differs: lead II vs lead III"),
        ("Cranial nerve VII supplies the muscles of facial expression and taste",
         "Cranial nerve IX supplies the muscles of facial expression and taste",
         "Differs: nerve VII vs nerve IX"),
    ]
    for a, b, label in pairs:
        assert contrast_label(a, b) == label, (a, contrast_label(a, b))
        score = _score(a, b)
        assert score is None or score < 0.5, (a, score)
        assert _score(a, a) > 0.99


def test_classifier_contrast_needs_the_classifier_word():
    assert contrast_label("Type 2 block", "type 2 block") == ""
    assert contrast_label("Type 1 and type 2 block", "Type 1 block") == ""
    assert contrast_label("Onset 30 s after 2 mg", "Onset 60 s after 5 mg") == ""
    assert contrast_label("Ketamine IV bolus works fast", "Ketamine IM bolus works fast") == ""
    assert contrast_label("Give 4 mg IV now", "Give 8 mg IV now") == ""
    assert contrast_marks([], "type iv reaction")[0] == frozenset({"type 4"})


def test_one_class_in_two_numeral_systems_is_not_a_contrast():
    pairs = [
        ("Type 2 diabetes results from insulin resistance with relative deficiency",
         "Type II diabetes results from insulin resistance with relative deficiency"),
        ("Grade 3 hypertensive retinopathy shows papilledema and cotton wool spots",
         "Grade III hypertensive retinopathy shows papilledema and cotton wool spots"),
        ("Class 2 obesity is defined by a body mass index between 35 and 39.9",
         "Class II obesity is defined by a body mass index between 35 and 39.9"),
        ("Factor V Leiden causes resistance to activated protein C and thrombosis",
         "Factor 5 Leiden causes resistance to activated protein C and thrombosis"),
        ("Cranial nerve 5 supplies sensation to the face and the muscles of mastication",
         "Cranial nerve V supplies sensation to the face and the muscles of mastication"),
    ]
    for a, b in pairs:
        assert contrast_label(a, b) == "", (a, contrast_label(a, b))
        assert _score(a, b) > 0.7, (a, _score(a, b))


def test_different_classes_in_mixed_numeral_systems_still_contrast():
    assert contrast_label("Type 1 diabetes", "Type II diabetes") == (
        "Differs: type 1 vs type II")
    assert contrast_label("Factor VIII deficiency", "Factor 9 deficiency") == (
        "Differs: factor VIII vs factor 9")
    a = "Type 1 diabetes mellitus results from autoimmune destruction of beta cells"
    b = "Type II diabetes mellitus results from autoimmune destruction of beta cells"
    score = _score(a, b)
    assert score is None or score < 0.5


def test_a_class_label_has_one_entry_per_class_value():
    assert contrast_label("Type 2 (type II) block", "Type 3 block") == (
        "Differs: type 2 vs type 3")
    assert contrast_label("Type 1 and type II block", "Type 3 block") == (
        "Differs: type 1 type II vs type 3")
    assert contrast_marks([], "type 2 or type ii")[0] == frozenset({"type 2"})
    # a bare numeral nests inside the same class written as a phrase
    assert contrast_label("Type II", "Type 2") == ""


def test_roman_class_values_run_past_ten():
    for a, b in (("Factor XII deficiency prolongs the aPTT", "Factor 12 deficiency prolongs the aPTT"),
                 ("Factor XIII stabilises fibrin clot", "Factor 13 stabilises fibrin clot"),
                 ("Cranial nerve XII moves the tongue", "CN 12 moves the tongue")):
        assert contrast_label(a, b) == "", (a, contrast_label(a, b))
    assert contrast_label("Factor XII deficiency", "Factor XIII deficiency") == (
        "Differs: factor XII vs factor XIII")
    assert contrast_label("Factor XIII deficiency", "Factor 12 deficiency") == (
        "Differs: factor XIII vs factor 12")


def test_invalid_roman_forms_are_not_numerals():
    from internpearls.dupes import _roman_value
    assert [_roman_value(r) for r in ("iv", "xiv", "xxxix")] == [4, 14, 39]
    for bad in ("iiii", "ic", "vv", "xxxx", "mix", "in", ""):
        assert _roman_value(bad) is None, bad
    for text in ("type iiii reaction", "type ic reaction", "lead in the circuit",
                 "type mix of cells"):
        assert contrast_marks([], text)[0] == frozenset(), text
    assert contrast_label("Type iiii", "Type 4") == ""


def test_search_reuses_one_index_for_many_queries():
    from internpearls.dupes import DEFAULT_MIN_SHARED, DEFAULT_THRESHOLD, search
    right = [(2, "fenoldopam is a selective D1 receptor agonist", "Theirs", "Basic"),
             (3, "propofol induction causes dose dependent hypotension", "Theirs", "Basic")]
    left_a = [(1, "fenoldopam selective D1 receptor agonist mechanism", "Ours", "Basic")]
    left_b = [(1, "propofol induction dose dependent hypotension", "Ours", "Basic")]
    index = build_index(right)
    assert search(index, left_a) == find_candidates(left_a, right)
    assert search(index, left_b) == find_candidates(left_b, right)
    assert [r[2][0] for r in search(index, left_a + left_b)] in ([2, 3], [3, 2])
    # The scan's own Normal sensitivity is the default here too.
    assert (DEFAULT_THRESHOLD, DEFAULT_MIN_SHARED) == (0.5, 2)


def test_normal_sensitivity_and_the_stored_default_are_the_scan_defaults(anki):
    from internpearls import config, dupes, dupes_dialog
    normal = dict((label, (t, m)) for label, t, m in dupes_dialog._SENSITIVITY_LEVELS)
    assert normal["Normal"] == (dupes.DEFAULT_THRESHOLD, dupes.DEFAULT_MIN_SHARED)
    assert config._cfg()["dupes_threshold"] == dupes.DEFAULT_THRESHOLD
