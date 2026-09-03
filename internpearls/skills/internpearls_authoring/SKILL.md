---
name: internpearls-authoring
description: Draft Anki flashcards for medical learners in the Intern Pearls house style.
---

# Intern Pearls card authoring

You are drafting flashcards a learner will meet under time pressure, months from now, in
random order. Follow these rules exactly; the add-on validates your output and a human
reviews every card before import.

## Card craft

### Scope
- Every card is a recurring obligation. Make the fewest cards that teach the material;
  never card a fact just because it appeared in the source.
- Budget in cards, not notes: each cloze deletion group is its own recurring card, so one
  note can quietly be five reviews.
- Don't card what another card already gives away in its answer or its "why", and keep
  every front unique; the add-on blocks a draft that duplicates one.

### Atomicity
- One fact per card. If a card tests two things, make two cards.
- Get stricter as stakes rise: a time-critical sequence ("do A, B, then C") is one card
  per action, or an ordered cloze that tests the order, never a prose chain.
- Never reference another card ("see the other card", "as above"); there is no above in a
  review queue. Never state a sibling's answer on this card's front either: after a split,
  read each front against every other back.
- Never let a front answer itself: if rephrasing it answers it, strip the giveaway. One
  correct answer per front, and if the answer is a set, say how many — but only when the
  set is genuinely closed; otherwise reframe around the subset that is.
- Producing the answer should take a few seconds. Longer means fewer facts on the card.
- Answers stay short: the tested clause, not a paragraph, since reading a paragraph to
  find the one wrong clause cannot be graded honestly. The add-on warns when Back plus Why
  runs long.

### The "why"
- Every card carries a plain-language "why" in its Why field (or Back for Basic): the
  reason the fact is true, two or three sentences.
- Write a causal chain as links in order, one short sentence per step. Compressing three
  steps into one clause reads as complete to the author and loses the learner.
- Nothing more important than the blank may live in the "why": it is read second and
  skimmed, so the point of the card must live on the card. If anything in the "why" beats
  what the blank hides, move it into the visible sentence and re-blank around it.
- The "why" must never contradict the blank: no range there that disagrees with the value
  in the deletion, and if it names an exception, the blank is the rule. An example there
  must be an instance of what the card asserts, not a vivid counterexample to it.
- The "why" is the reason, not storage. Material that will not fit is a decision to card
  it or drop it.

### Cloze or Basic
- Basic is for a genuine single-answer question, or a vignette answered by its cause or
  diagnosis ("what has most likely happened?").
- Cloze is for criteria, thresholds, normal ranges, gradings, step sequences and grouped
  causes; a set that is nearly all Q&A prose is the failure mode. Choose deliberately,
  because converting a note's type later costs its review history.

### Shaping a blank
- Blank whole terms, never split one. A word may sit outside the braces only if saying it
  is not part of a correct spoken answer.
- Blank the name, not what the named thing does: producing a name from its description is
  the retrieval worth drilling, reciting its attributes is not.
- Blank the word the fact turns on. A blank on a detail that merely rode along in the
  sentence is the commonest way a card is true, well made, and worthless.
- Never blank a whole sentence, and never blank a multiple-choice answer option read back
  verbatim: an option is written to be recognized among four, not produced cold. Take its
  point and blank the word it turns on.
- Frame test: read only the words that will be showing and ask whether they point at one
  answer. Everything after the blank shows too, so a trailing clause restating the answer
  belongs in the "why". Word count is a symptom; this is the test.
- Every blank needs essentially one correct wording. A blank hiding a soft principle
  ("treat the underlying cause") cannot be graded; make it Basic or cut it.
- Blank the noun, not the direction word, unless the direction is exactly what a learner
  reasoning from the topic would get backwards.
- Blank every member of a set the front announces, or stop announcing a set: a member left
  showing hands over part of the answer, and the card cannot say whether it is a given or
  the answer.
- A second deletion group has to earn itself: it is another recurring card, so it must be
  independently tested, not the framing that makes the first one meaningful.
- A hint narrows a frame you cannot rewrite: `{{c1::answer::category}}` shows `[category]`
  on the question side. Reach for it after rewriting the sentence, never instead, and never
  let it hand over the answer. A parenthetical outside the braces does; move it inside.
- Group a long list into two or three deletions rather than one per item. A two-sided
  contrast whose cells name each other shares one group, or each card is free.
- Bold and underline the one visible word that makes the blank specific
  (`<b><u>word</u></b>`), never as general emphasis.
- Keep cloze text plain: no bold or underline inside the sentence; the blank itself is
  the emphasis.

### Shaping an answer
- A comparison of two things across several dimensions is an HTML <table>, not two prose
  clauses joined by "whereas". A prose comparison is a defect, not a style choice.
- A table needs two axes, and its headers have to name the relationship it shows. A list
  of items each carrying one value is a sentence, not an `Item | Value` grid.
- In a directional grid use arrows (`&#8593;` / `&#8595;`) rather than "high" and "low",
  so the learner reads a pattern instead of parsing words.
- A table can be the cloze itself: put it in the cloze Text and blank whole cells, one row
  by default with the rest visible as context. When the value column repeats a small closed
  vocabulary, group by class instead, or the visible cells answer the hidden ones.
- Four or more grouped causes are a <ul>, styled inline (`style="text-align:left;
  display:inline-block; margin:6px auto; padding-left:1.1em;"`). An ordered sequence is a
  cloze, one blank per step.
- Never ask the learner to pair two parallel lists by position; that is a table with the
  grid taken away. Make the rows whichever axis the learner thinks in.
- If none of these shapes fit and the answer is still a paragraph, it is two cards.

### Language and fields
- Spell out uncommon acronyms on first use: "Local Anesthetic Systemic Toxicity (LAST)".
  Standard shorthand (MAP, ETT, ABG, TOF, RSI) stands alone.
- No unexplained term or borrowed metaphor anywhere on the card. Gloss it or say the plain
  version. Teach the mechanism, not its nickname: an eponym is a fair thing for a blank to
  ask for, but the "why" still walks the causal chain.
- An instruction has to name its object: what is adjusted, by how much, in which
  direction, against which reference.
- Write "the patient", never "her" or "him", in a hypothetical.
- Keep the source material's own axis and terms: a card on a different cut of the topic
  reads as confusing even when correct, and plainer paraphrase makes the fact harder to
  recognize where it has to be.
- Keep the source's numbers and never narrow its ranges; a tightened range is a different
  claim. Where a reliable source disagrees with the material, teach the accurate version
  and name the discrepancy in one clause of the "why".
- Never copy a vignette stem or an explanation verbatim onto a card. Write the concept.
- Escape < and > in values: "SpO2 &lt;94%".
- Doses go in the Dosing field with the source named. Leave Notes empty; it belongs to the
  learner. Cloze syntax is exactly `{{c1::...}}`; the add-on blocks a note with no valid
  deletion or with unbalanced braces.

## Thorough mode workflow
When you have web tools: first draft, then verify every dose, threshold, and claim against
a reputable source (society guideline, major textbook, peer-reviewed reference),
correcting anything unsupported. Then self-review the whole set: read each note with each
deletion group blanked in turn and apply the frame test, then check atomicity, blank
choice, the "why" against the blank, answer length, and each front against every other
card's back. Fix what fails instead of reporting it. State the verification source in the
rationale when a fact was checked. Do not trust your own first draft on clinical facts: a
card with two contrasted conditions backwards reads just as fluently as a correct one.

## Images
Never generate a raster image. Allowed sources only: a file attached to this prompt
(reference it as attached:<filename>), a real image found on the web during verification
(url:https://..., with attribution), or a simple SVG you draw yourself for structural
diagrams (svg:<svg...>). Every image needs alt text.
- Ask which job the figure does. This tool cannot make image-front cards, so a figure the
  learner would have to read in order to answer stays on the answer side and the question
  is asked in words; Basic and Cloze render Image on the answer side only.
- A figure that prints its own labels cannot be the question. Crop the labels off, or keep
  it on the back and ask the question in words. Same for lettered panels.
- A multi-panel figure is several cards: one per panel, all cropped to the same frame and
  scale, with the whole figure on each answer.
- A card about an abnormal appearance shows the normal one beside it, at the same scale.
- Check the card's claim against the figure; anything legible in it is content the card
  teaches, so the answer has to survive it.
- For anatomy prefer real figures; a plausible-but-wrong drawing is worse than no image.
  Give any SVG you draw an explicit white background so it survives Night Mode.
- Crop to the useful region, and never ship two "name this" images a learner could not
  tell apart.

## Output
Follow the output contract in the prompt exactly: JSON only, no prose.
