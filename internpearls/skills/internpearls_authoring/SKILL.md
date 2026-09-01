---
name: internpearls-authoring
description: Draft Anki flashcards for medical learners in the Intern Pearls house style.
---

# Intern Pearls card authoring

You are drafting flashcards a resident will review under time pressure. Follow
these rules exactly; the add-on validates your output and a human reviews
every card before import.

## Card craft
- One fact per card. If a card tests two things, make two cards.
- Every card carries a plain-language "why" in its Why field (or Back for
  Basic): the mechanism or reasoning, one to three sentences.
- Prefer cloze for lists, gradings, thresholds, normal ranges, and ordered
  sequences. Blank whole terms, never split a term across deletions. Use a
  Basic card for a genuine single-answer question or a vignette.
- Comparisons of two things are an HTML <table>. Four or more grouped causes
  are a <ul>. Ordered sequences are cloze.
- Answers stay short: the tested clause, not a paragraph. Reading a paragraph
  to find one wrong clause cannot be graded honestly.
- Never reference another card ("see the other card", "as above"). Reviews
  arrive in random order.
- Spell out uncommon acronyms on first use: "Local Anesthetic Systemic
  Toxicity (LAST)". Standard shorthand (MAP, ETT, ABG, TOF, RSI) stands
  alone.
- Escape < and > in values: "SpO2 &lt;94%".
- Doses go in the Dosing field with the source named. Leave Notes empty; it
  belongs to the learner.

## Thorough mode workflow
When you have web tools: first draft, then verify every dose, threshold, and
claim against a reputable source (society guideline, major textbook,
peer-reviewed reference), then run a self-review pass: check each card for
accuracy, atomicity, cloze shape, and answer length, and fix what fails.
State the verification source in the rationale when a fact was checked.

## Images
Never generate a raster image. Allowed sources only: a file attached to this
prompt (reference it as attached:<filename>), a real image found on the web
during verification (url:https://..., with attribution), or a simple SVG you
draw yourself for structural diagrams (svg:<svg...>). For anatomy prefer real
figures over drawings; a plausible-but-wrong drawing is worse than no image.
Every image needs alt text.

## Output
Follow the output contract in the prompt exactly: a JSON list only, no prose.
