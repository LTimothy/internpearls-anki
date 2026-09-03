# Changelog

All notable changes to Intern Pearls Deck Tools. Versions follow the semver rules in
this repo's `README.md` ("Versioning").

## v0.58.2

A failed generation used to leave nothing behind: the wizard showed one line and the
whole subprocess stream was gone, which made a run that failed only sometimes, on
material that worked fine when replayed by hand outside Anki, impossible to chase
down after the fact. Every run now writes its own evidence to `ai_last_run.log` in
the add-on's `user_files` folder: the argv it ran (the prompt itself elided, never
written), every raw stdout line, stderr, the exit code, and how long it took,
overwritten by the next run and capped at 2 MB kept from the tail. The "no usable
reply" error itself is more specific too, naming how many stream lines it saw and
echoing the last non-empty one, rather than a single unhelpful sentence.

Antigravity CLI's own stream narrates the whole reply as it goes, one chunk per
step, well before its terminal result line. A real run reproduced the failure this
release fixes: that terminal result reported success but carried an empty reply,
even though the narrated chunks amounted to a complete, valid one. The add-on now
falls back to those accumulated chunks whenever this happens, rather than treating
a working reply as a failure because its last line forgot to carry it.

The AI Backends window's settings panel (executable path, Model, Effort, Test
connection) got a regression test pinning down that all four already share one left
edge, after a report that they didn't.

## v0.58.1

The AI Backends window's rows could still clip their own muted third line ("Works
with a ... . Tools fully restricted (strongest)."), cut off mid-glyph, on first open.
An earlier attempt at this fix tried to grow the window from right inside a wrapped
label's own resize, by reading the window's minimumSizeHint() synchronously and
resizing from it there and then, but that read is not reliable at that exact moment:
the box layouts between a row and the window do not all catch up to a layout change
within the same pass it happened in, so the value read there could still be short,
and the window's first paint went out at the too-small geometry it computed. Any
later repaint (a Re-check, or just moving the window) showed it correctly, which is
why it never seemed to persist. Fixed by settling the window's size after its own
showEvent instead, once its layout has actually finished activating, with one more
pass on the next event-loop turn as a backstop; the window's top-level layout also
now enforces its own real minimum on every layout pass, not only at first paint.

The wizard's input page is a little simpler. The Deck row's own Change link opened
the same Advanced panel the Advanced link already does, so it's gone, with the row's
detail pointing at Advanced instead. The horizontal rule introducing that panel now
collapses and reappears with it, instead of always sitting there whether the panel is
open or not.

The Advanced panel's own grid is fixed: "Exact number of cards" no longer overflows
into the spinbox beside it, which is now wide enough for its own "auto" special value
too. Note types lines up with the first of its four checkboxes now, rather than
centred against all of them.

My rules is easier to find and use. Its wizard link now reads "Add my rules" until
you've saved any and "Edit my rules" after, and View skills no longer prints "My
rules: none" when there's nothing there, just one line pointing at where to add some.
The editor's own hint states plainly how your rules rank against the bundled skill:
they win on style, wording, and emphasis where the two disagree, but the output
format and the rule against raster images always win regardless, and shows three
short examples. That ranking now actually reaches the assistant, not just the
editor's own hint text: your rules are sent with a heading stating the same thing.

A few smaller fixes round out this release: a usage-count fallback scoped to the one
backend it was ever needed for, a cloze-rule wording fix in the bundled skill, and a
depth-row hedge that no longer claims Quick draft verifies anything online.

## v0.58.0

The "Generate cards with AI" wizard picks its own card count now. Left alone, the
assistant drafts one card per point the source actually teaches, up to a ceiling of
40, and is told plainly not to pad to a number or merge points just to save cards. An
exact count is still available in the input page's new Advanced disclosure for anyone
who wants to pin one. On Claude Code, a per-backend turn cap (one turn for Quick, up
to 15 for Thorough) still bounds a single generation call regardless of how many cards
a reply carries, so a larger automatic count doesn't also mean a slower or costlier
run there; Codex CLI and Antigravity CLI have no turn cap and are bounded only by the
run timeout.

Depth now defaults from what you're actually pasting in, rather than a manual choice
every time. Thorough kicks in once the source reaches 1,500 characters or carries any
attachment; anything shorter starts on Quick. Two new config keys, `ai_default_count`
and `ai_default_depth`, let you seed the wizard's starting point ahead of time; both
only seed the control, and neither is ever written back from a single session's pick.

The input page itself is rebuilt around four status rows (Backend, Cards and depth,
Deck, Skills), each with a chip and a link into more detail, with the count spin box,
depth radios, note-type checkboxes, and destination deck combo moved into the new
Advanced disclosure underneath them. Each row's own honesty holds per backend: the
Cards and depth row's detail line is built from that backend's own mode text rather
than one shared sentence, since what Thorough actually reaches (a real web check on
Claude Code, nothing on Codex CLI, whatever Antigravity's own approval defaults allow)
is not the same claim on all three.

The progress page is now one status row too: a chip and phase, elapsed time, and a
trailing Cancel link, instead of three separate labels and a dialog button box. Escape
now does exactly what Cancel does here, ending the run in place, rather than closing
the whole dialog out from under it. The review page's header now names what a run
actually produced, "3 cards drafted" and, when you attached files, "from 2 sources", so
you're not left counting rows yourself.

A few smaller fixes round out this batch. Codex CLI runs that finish quickly used to
report zero tokens even when usage was there the whole time, sitting on the stream's
`token_count` event instead of the terminal result the add-on was already reading; the
add-on now tracks the last usage figure it saw across the whole run and falls back to
it. Antigravity CLI's model hint in AI Backends now names `gemini-3.8-flash-low` as its
fastest option. And the bundled authoring skill now says plainly to keep cloze text
plain, no bold or underline inside the sentence, since the blank itself is already the
emphasis.

## v0.57.1

"Check for add-on updates" now tells the truth when GitHub's request limit runs out.
The add-on's own update check calls the public GitHub API with no token, since the
add-on repo is public, and that API allows only 60 unauthenticated requests per hour
per IP address, shared by everyone on the same connection and by every unattended
check this add-on runs on its own. Once that's used up, GitHub answers with the same
403 status a bad token gets, and the add-on said "access denied (check that your token
is valid and can read this repo)" to a person who had never entered a token at all.
The add-on now recognizes the rate limit specifically and says so, with the time it
resets, and no longer implies a token exists to check when none was sent.

A configured GitHub token now also speeds up and backs up this same check. Signing in
with a token in Manage decks (read access to any repo is enough) raises the request
limit from 60 to 5,000 per hour, so the check now sends it when one is set. If the API
is still rate limited even so, the check falls back to fetching the file straight from
GitHub's raw CDN instead, which carries no request quota at all.

## v0.57.0

Antigravity CLI runs now actually reach the model. Its print mode takes the prompt as
the value of its own `-p` flag and never reads standard input, so the argv the add-on
built (`agy -p --output-format stream-json`, prompt on stdin) made `agy` treat
`--output-format` as the whole prompt and fail. The prompt is now passed as that flag's
value and goes last, where nothing after it can be mistaken for it, with the scratch
folder handed over as `--add-dir` and slash-command expansion turned off so a `/word`
in your own source material stays text. A prompt over 200,000 characters is refused
with a readable sentence rather than an operating-system argument-size error.

Antigravity's streamed progress is read correctly too. Its lines are keyed by `event`
rather than `type`, and its final reply, token count, and error message all sit inside
a nested `result` object, so the phase line during a run and the token count after one
were both blank, and a failed run could report nothing useful. A refused file write and
an empty prompt each get one short sentence now instead of the CLI's own long line.

Antigravity CLI gains real Model and Effort controls. `agy` documents `--model` and
`--effort` in its own help and lists the ids it accepts under `agy models`, so both are
yours to set, and each is sent only when you set one and only when the installed binary
documents that flag. Left blank, Antigravity runs its own default, already a cheap
Flash tier. The wizard's one-line backend summary reads the configured model, or
"default model", for every backend.

Attached images work with all three assistants. Antigravity reads the scratch copy of
exactly the files you attach, the same as Claude Code and Codex CLI, so its row now
carries the "image attach: supported" badge. No backend is ever given a file-writing
tool.

Leftover scratch folders get cleaned up. Each run copies your attachments into a folder
in the system temp directory and removes it when the wizard closes; a crash used to
leave it behind for good. Shortly after Anki starts, the add-on now removes its own
leftovers older than a day, and trims the oldest if what remains still exceeds 200 MB.
Nothing outside its own named folders is touched.

The account each assistant needs is stated more precisely. Codex CLI works with a free
ChatGPT account, at roughly 50 agentic coding messages a day, with more on Go, Plus, or
Pro; Claude Code has no free tier; Antigravity CLI stays a free, throttled Google
account tier, and its row notes that it replaces Gemini CLI, which stopped serving
personal Google AI Pro and Ultra accounts on 2026-06-18.

When a run fails, the wizard now shows the same one-line summary of the CLI's error
that Test connection already used, for every backend: the first line of the message,
trimmed, with a sign-in hint when it looks like an authentication failure.

## v0.56.1

The AI Backends rows no longer cut off their own last line. Each row's muted line
("Works with a Claude Pro or Max. Tools fully restricted") wraps to two lines at the
width the window opens at, and a wrapped label reports a minimum height of one line
whatever it wraps to, so the window could open shorter than its own text and every row
was squeezed until that line was clipped mid-glyph. Each wrapped line now holds the
height it really needs.

AI Backends has left the Experimental menu and opens from the "Generate cards with AI"
wizard alone, which is the only thing it is ever about: a "Set up an assistant" button
on the first-run page, and a Setup link beside the backend summary on the input page.
The window itself is unchanged.

The Night Mode Dimming preview holds still. The hint under the scope radios is one
line for bright images only and three for everything on cards and deck screens, so the
Dim by row and the preview below it slid down as soon as the second scope was picked;
the hint now stands as tall as the longer of the two either way.

Both Test connection buttons, the one in AI Backends and the one on the wizard's input
page, now run through the same off-thread runner rather than two copies of it.

## v0.56.0

A new Experimental item, AI Backends, is now where the three assistants are set up.
Each one is a single row: a chip for what the check found, its name and command, small
badges for image support and a free account tier, the subscription it needs and how
restricted it is, and a link to its own install guide. The preferred backend is marked
as such and the others offer a Use link; an ignore link sets one aside and reads use
again afterwards. One settings panel below the rows covers the preferred backend only,
since that is the one a run will use: its executable path override, its default model
and effort, and a Test connection button that runs one real, trivial prompt. So to set a
path for an assistant that is not found yet, click its Use link first, then fill in the
path in the panel that follows it. The model
and effort controls moved here from the "Generate cards with AI" wizard, whose input
page now shows a one-line summary of the backend in use with an AI Backends link beside
it, and whose first-run page is a single Open AI Backends button. Defaults for Claude
Code stay at sonnet with medium effort, chosen so a Thorough run does not spend a
subscription's credits on the top model by default. The honesty rules are unchanged:
Codex is only handed a model flag when its own help documents one, and Antigravity's
model cannot be set from here at all. `ai_cli_path` in config.json is now per backend
rather than one flat string; a path already saved under the old shape carries over
automatically to the preferred backend.

Night Mode Dimming can now dim more than images. A scope choice under the checkbox
picks between bright images only, the previous behaviour, and everything Anki draws as
a web page: cards, the deck list, the overview, and the editor. The menu bar and
dialogs are native windows and stay as they are. A changed scope or percentage shows
on the next screen that loads, and the confirmation after Save now says which scope
was saved.

My rules is a third skill, written by you. Plain text, edited from an "Edit my rules"
link beside "View skills" on the wizard, stored in the add-on's user files so it
survives updates, and sent to the assistant on every run after the bundled skill and
any deck skill. It is capped at 20,000 characters and View skills shows it in full.

## v0.55.1

Two small fixes to the "Generate cards with AI" wizard. The Model field, for a
backend with known aliases (currently Claude Code), is now a closed list of
those aliases plus a Custom entry, rather than a free-text box; picking Custom
reveals a line edit for typing any other model name. This also fixes a visible
misalignment on macOS, where the old free-text Model field rendered at a
different height and left inset than the Effort field beside it, so the two
rows never lined up. Also, every stray double-hyphen dash in the add-on's own
text (dialog copy, the bundled skill, this changelog, and more) has been
rewritten as ordinary punctuation.

## v0.55.0

The "Generate cards with AI" wizard's backend row can now pick a model and, for
Claude Code, a reasoning-effort level, instead of every backend running whatever
it defaults to on its own. Claude Code now runs with `--model sonnet --effort
medium` unless overridden, rather than silently inheriting the signed-in
account's own default model, the top model for a Max subscriber, which used to
burn through a subscription's credits fast across Thorough mode's up-to-15-turn
loop. A hand-edited effort value the CLI wouldn't recognize now falls back to
`medium` instead of reaching `claude --effort <typo>` and dying with an opaque
CLI error, and the Effort combo always shows that same effective value. Codex
CLI's Model field passes `--model` only when a model is set and the installed
CLI's own help documents that flag (probed against both `codex --help` and
`codex exec --help`, since a subcommand's own options often live only under its
own help), so an older Codex isn't hard-broken. Antigravity CLI's Model field is
read-only text, since headless mode has no verified way to honor a model choice
at all (its default is already the cheap tier). Two new config keys, `ai_model`
and `ai_effort`, hold whatever's picked, stored per backend so a value set while
one backend is active is never pre-filled or sent for another.

The Experimental menu's dimming item is renamed to "Night Mode Dimming", title
case, and its dialog now shows a live side-by-side Normal/Dimmed preview so a
chosen dim percent can be judged before it's applied, rather than only after
closing the dialog and opening a real card. "Generate cards (AI)" is now
"Generate Cards (AI)", title case to match.

The bundled internpearls-authoring skill (used by the AI wizard's prompts) is
replaced with a distilled version of the house card-authoring rules covering
card scope, atomicity, the "why" field, table/list/cloze shape, and image
sourcing.

## v0.54.2

The "Generate cards with AI" wizard's review rows are rebuilt on the same shape the
Update my decks screen already uses, visual only, no behavior change. Each row is now
a caret, a fixed chip column, and a bold front line, with the back, why, dosing and any
image tucked into a body the caret reveals, and Edit/Note as quiet links at the end of
that body instead of two native push buttons on every row. A card a check flagged now
shows its own reason underneath, and a duplicate names the existing card it matched:
both used to be invisible, with only a bracketed word saying something was wrong. A
queued revision note shows on its row instead of only living inside a prompt's memory.
The two quality radios on the input page now carry just their short name (Thorough,
Quick draft); the full per-backend disclosure sentence moved to a wrapped line
underneath, since it used to force the whole dialog to roughly 1000px wide trying to
fit unwrapped on one radio button.

## v0.54.1

Layout and control fixes for the "Generate cards with AI" wizard, no behavior change.
Every page now hands its own leftover height to something that owns it instead of
leaving it to spread across every widget, so a page never floats a dead band above its
buttons and the Import button on the review page stays reachable no matter how many
cards were drafted. Every loose row of push buttons is a proper button box now (a
visible default action, Cancel on the platform's usual side), and Re-check on the setup
page reads as a link next to a plain status line rather than a full-width button. View
skills routes through the same scrollable, button-outside-the-body dialog on both of
its branches, so the bundled skill's own length can never push its Close button off the
screen. The review page's status line is split: what you're deciding (how many cards,
how many are included) stays under the title, and run facts (token spend, rate limits,
what a revision changed) move to a line under the list. A successful import shows a
brief tooltip instead of a dialog to click through, the same way Anki's own Add does.

## v0.54.0

New "Experimental" menu, a sibling of Advanced for features that are new or still
settling: "Generate cards (AI)" (moved off the top level, same feature, renamed to fit
the menu's sentence-case/no-ellipses style) and a new "Night mode dimming" dialog.

Night mode image dimming used to be a single on/off toggle in Settings. It's now its
own dialog under Experimental, with a percentage control for how much dimmer bright
images get while Anki itself is in Night Mode (it never applies in Day mode, and never
applies to anything but images). The existing on/off setting keeps working exactly as
before: an install that already had it on keeps today's exact look, since the new
percentage defaults to 30, the fixed amount every prior build applied.

## v0.53.5

Fixed a crash risk in the "Generate cards with AI" wizard's undo-shortcut helper.
Asking Qt to render the standard Undo key sequence with no live application running
dereferences a null pointer inside Qt itself, which is not a catchable Python
exception: it hard-crashes the whole process. That path is unreachable in the
running add-on, where Anki's own app is always live, but is now guarded either way:
the helper checks for a live application first and falls back to a plain literal
when there isn't one.

## v0.53.4

Four more defects in "Generate cards with AI", again found only by running the add-on
for real.

The most important: a successful import genuinely wrote one clean, undoable batch of
notes (a headless test calling `col.undo()` really did remove the whole thing in one
step), but nothing ever told Anki's own main window that a new undo entry existed, so
Edit > Undo stayed greyed out in the running app no matter how quickly you tried it.
The deck list had the same problem: a new deck's cards and counts only showed up after
manually clicking "Decks". Both are fixed by the same call this add-on already makes
after every other collection-writing action, right after the import succeeds.

The completion message also hardcoded "Ctrl+Z" on every platform, which is simply
wrong on macOS. It now asks Qt for the platform's own rendering of the standard Undo
key sequence, so it reads "Cmd+Z" (or whatever glyph the OS actually uses) there
instead.

And the review page's import button, its header counts, and the completion message
could all read a grammatically bare "1 cards" when exactly one card was involved; all
three now use the same singular/plural helper the rest of the add-on already relies on.

## v0.53.3

Two more defects in "Generate cards with AI", again found only by running the add-on
for real, not by the automated test suites.

The crash guard added in v0.53.2 covered every wizard button, but missed the one path
that matters most: generation completes off a timer, not a click, so an exception
there still showed Anki's own raw crash box. Worse, it left the wizard stuck: the
progress page kept showing "Generating cards" with a Cancel button wired to a run that
had already finished, so clicking it did nothing, repeatedly, and closing the window
was the only way out. Completion now goes through the same guard as every button, and
a failure there tears the run down properly and returns you to the input page (or
your existing draft, if you were mid-revision) instead of leaving you stranded.

Separately, a single missing folder used to crash every write of the add-on's own
saved state (usage stats, deck skill consent, and more): the shared atomic-write
helper behind all of them now creates its target folder if it isn't there.

Also re-verified: the note-type availability check from v0.53.2 already behaves
correctly against a real collection with neither managed note type present. It now
has a real-Qt-widget test alongside the existing one, so a genuine rendering
regression there can't go unnoticed the way this round's re-check first seemed to
suggest.

## v0.53.2

Fixed two defects in "Generate cards with AI" found by hand, neither reachable by the
automated test suites.

The input page used to offer "Study Deck - Basic" and "Study Deck - Cloze" checked by
default even before you had ever synced a deck, which is the only thing that creates
them. Picking one before that first sync meant writing source material, waiting
through a whole generation, and only discovering at the very last click that Import
rejected the entire batch. Only note types actually present in your collection are
offered now; a managed type you don't have yet shows disabled with a short reason
instead of disappearing outright.

Also, a bug in any of the wizard's buttons (Import, Revise all, Edit, Note, Test
connection, Attach, Generate) used to show Anki's own raw "encountered a problem" box
instead of this add-on's dialog, since a Qt button's click doesn't run through the
same protection a menu action gets. Every one of those now shows a plain, titled
Intern Pearls dialog with the actual error instead.

Separately, the review page's include/exclude checkboxes did not visibly respond to a
click: the underlying state was in fact changing, but nothing ever refreshed the "N
included" count or the "Import N cards" button label, so a toggle that worked read as
one that didn't. Both now update immediately.

## v0.53.1

Fixed a shipped defect: extracting a PDF's embedded images relies on Pillow, which
Anki's own bundled Python doesn't carry, so those images silently extracted nothing
for every real user, indistinguishable from a PDF that never had any. PDF text
extraction was never affected. Attaching a PDF now says plainly, once per session,
when its images couldn't be decoded here, so figures you want on a card can be
attached separately as image files instead.

## v0.53.0

A new "Generate cards with AI" menu item drafts cards from source material you paste
in or attach, through a coding-assistant CLI you install and sign into yourself:
Claude Code, Codex CLI, or Antigravity CLI, whichever you have running. There is no
API key field anywhere in the add-on; it only shells out to a CLI already signed in
on your own machine, and never reads, sends, or stores a credential of any kind.

The three backends are not equally sandboxed, and the setup screen says so plainly
for each rather than folding them into one reassurance: including what Quick draft
and Thorough actually restrict, which differs by backend: Claude Code is the only one
the add-on itself caps by mode (Quick gets one turn and no tools beyond reading files
you attach; Thorough gets more turns and web-search tools), Codex CLI is sandboxed
read-only in both modes so neither can reach the network, and Antigravity CLI isn't
restricted by mode at all, so it may still reach the web even under Quick. Separately,
and in every mode: if a drafted card proposes a picture it found online, the add-on
itself (not the assistant) fetches that image during review so you can see it, and
shows the host it came from. No card here ever carries an AI-generated picture: an
image can only come from a real web source, an image file you attached, or SVG the
model draws itself. An attached PDF's text is always pulled in; its embedded images
generally are not, since Anki's own bundled Python lacks the library needed to
decode them.

Cards you generate land in their own `Generated` subdeck and tag with a fresh local
GUID, so a shared deck source's own sync and reconcile machinery can never match,
retire, or overwrite one. Nothing about a session (the source text, drafts, your
feedback, the exchange with the CLI) is saved once the dialog closes; only your
backend choice, a deck skill you've explicitly consented to, a rolling usage log, and
a rolling log of recent run durations (for the progress screen's learned time
estimate) persist between sessions.

## v0.52.1

The receipt lines sit tight now. The Show yours link, though flat, still
carried the platform's native button height, which stretched every receipt row
to double its text and read as a band of dead space between the lines; it sits
at text height now, like the row's expand caret. The summary phrase also no
longer folds itself onto three short lines beside the link: it is a one-liner
by design and renders as one.

## v0.52.0

A rewritten field now reads as a receipt, not a wall. Two treatments had been
tried and both fell short: marking a rewrite up word by word buried the change
in strikethrough and highlights, and showing the old text plainly parked a
second full paragraph under every rewritten card. Two independent design
reviews converged on the same answer, and this is it:

- The default is one line: "Why  rewritten, shortened (104 → 66 words)", with
  the word counts appearing only when the length really moved. The new version
  is already the card shown above, so at a glance that line is everything an
  Apply decision needs.
- A "Show yours" link (the same quiet link style as Add note, named in the
  Keep yours button's own vocabulary) reveals the old version in place: clean,
  unmarked, behind a grey rule that mirrors the green rule on the card's own
  explanation, so the two read as the same kind of block from two moments. It
  is built only when clicked, so long lists stay as fast as before.
- The word-diff line for small edits keeps its place, with its bar raised: a
  change now takes the diff treatment only while the marked words stay well
  under half the line, so a mostly-marked line can no longer ship.
- In the blanks-moved line, the named blank is bold now rather than blue,
  keeping blue for things that actually respond to a click.

## v0.51.0

Two refinements to the What changed group, from its first day in real use:

- A rewritten explanation no longer renders as a word diff. On a paragraph
  where most of the words moved, striking the old ones and highlighting the new
  ones made a wall of markup harder to read than either version alone, so a
  change below difflib's own similarity floor now shows the old value plainly
  instead. Small edits keep the diff, which is where it earns its keep.
- A card's question-ID label ([T11Q4]) moved out of the card's own text, where
  it read as a weirdly placed line, into the chip column: it stacks as a small
  tag under the row's NEW or UPDATED chip, one tag per reference. It is
  metadata about the card, so it lives in the row's metadata gutter and the
  text column stays purely the card.

## v0.50.0

An opened changed card now says what changed instead of making you find it.

Each field's old value used to render in full directly under its new one, so an
expanded card read as an alternation of card and ghost: the note, then the old
text, then the explanation, then the old explanation, with the actual difference
buried in two near-identical paragraphs. The card now reads clean top to bottom,
followed by one quiet "What changed" group that states each field's delta once:

- A reworded field shows as a single line with the dropped words struck through
  and the added words highlighted, so a corrected dose reads as "Give ~~1~~ 1.5
  mg/kg over ~~10~~ 2 to 3 minutes" rather than as two sentences to compare by
  eye.
- A fill-in-the-blank card whose words survived but whose blanks moved names the
  moved blanks ("no longer blanked: pencil-point") instead of reprinting the
  whole sentence. Old cloze text also never renders as raw {{c1::…}} markup any
  more; where the full old version is still the honest thing to show (the
  sentence was reworded too), its blanks render filled and blue, the same way
  the card's own line does.
- A field carrying a table, list, or picture keeps the verbatim old value, since
  a word diff would tear its structure apart.

## v0.49.1

Room to read the update screen, which had grown dense enough that the cards it
lists were the part with the least space:

- It opens noticeably larger now, sized to your screen rather than to the small
  floor it used to start at, and it is still resizable either way. The extra
  height goes to the list of cards.
- The standing reassurance below the list is shorter and set as small print. It
  says the same things, in four lines rather than nine, so it stops being the
  tallest block on a screen that exists to show cards.
- The Apply / Keep yours / Never buttons no longer look cut off. They were being
  drawn flush against the list's own edge, so the last button's border sat under
  the frame line and the scrollbar could pass over it.

Separately, the message you get when a deck brings a card format your collection
has never held now says what it is: the import adds that format, so one more run
of Update my decks finishes moving your existing cards onto it. It reported this
as a bare absence before, which read as an error rather than as the second half
of a normal two-step update.

## v0.49.0

A deck source can now also ship a short label saying where a card came from, such
as a question number in the bank it was written from. It shows above any note on
the card's row, on newly added and changed cards alike, since it says where the
card came from rather than why it changed.

Turning a card down with Never now opens the same note box that Skip and Keep yours
do. It was the one decision the dialog would not take a reason for, which left the
strongest thing you can say about a card as the only silent one.

A changed card now offers Never alongside Apply and Keep yours, the same three-way
choice a new card has always had. Keep yours sets one change aside and the card comes
back the next time the deck changes; Never keeps your version and stops offering
changes to that card at all. Both leave your card exactly as it is, and Never here is
undone from Manage decks → Declined cards, where those cards get their own group
rather than being filed as never imported.

## v0.48.0

A deck source can now attach a short note to a changed or newly added card
explaining why it changed. Reviewer feedback shows quoted, marked "from
feedback"; a maintainer's own note shows the same way, unquoted. Either kind
only appears when it describes the exact content on offer.

## v0.47.3

What a review round found in the card-declining feature, and a few older things
it turned up along the way:

- A card you declined can no longer have its note type changed underneath it.
  Declining stopped the new content from being imported, but the format change
  that came with it was still applied, so a card you turned away could end up
  converted to a fill-in-the-blank type holding no blanks. The format change now
  follows the decline: not applied, not asked about, and a deck whose only
  format change belongs to a declined card is no longer held back from
  unattended syncing forever.
- A hand-edited or corrupted declined-card file can no longer cost you your
  annotations. It used to fail in the one window between an import and the step
  that puts your own notes back; the file is now checked when it is read, and
  that step can no longer be skipped by anything failing after it.
- Agreeing to a format change that has nowhere to land (the first time a deck
  sends a card type your collection has never held) used to do nothing at all,
  silently, and mark the deck up to date anyway. It now says what happened and
  leaves the deck pending, so the next update completes the change for real.
- Cards you skipped or kept are no longer counted in the update's "new" and
  "changing" totals, since they aren't going to be imported.
- A skipped or kept card wears a single chip again, so its own words get the
  full width of the row instead of sharing it with a stack of badges, and an
  opened card's text lines up with its own row.
- The feedback box no longer claims your note is "sent to the deck author":
  nothing is sent anywhere automatically, and the digest at the end of a run now
  says plainly that it is on your clipboard for you to paste and send.
- Skip and Keep say when the card really comes back (the next time that deck
  changes, or any time from Manage decks) rather than promising a next update
  that may never arrive on a quiet deck.
- Being offline is described as being offline again, instead of advising you to
  check your token or folder for a source that was never reached.
- Cancelling an update at the download step now says the run stopped and nothing
  changed, the way cancelling at any other step already did.
- Import single deck now handles a deck whose card format changed, instead of
  claiming it would keep the history of cards it cannot.
- The decision buttons and the caret on each card now name the card they belong
  to for screen readers.
- Smaller fixes: a deck held back for a look or format change is announced again
  when a newer version of it arrives; one deck's backups can no longer be pruned
  early by another deck whose name starts the same way; an unusable poll interval
  in a hand-edited config no longer breaks startup; a failed automatic add-on
  update now tells you an update is waiting; a card that is both retired and
  reworded is counted once; the Settings summary lists only real settings; the
  cleared-exclusions line says it takes effect on save; "Keep yours" is worded
  the same way everywhere; and the demo shows the struck-through Never state.

## v0.47.2

A layout fix for the decline controls' macOS debut:

- Wider card text in the update preview: the confirmation opens wider now, so a
  card's own words no longer get crushed into a narrow column beside its Import /
  Skip / Never buttons.
- Tidier decision buttons: the segmented control reads as one control again, not a
  row of square, individually bordered buttons with a doubled line between them.
- Two of that control's labels are shorter (Skip, Keep mine), so they fit the
  control instead of stretching it.
- "Add note" now sits at the end of a card's own expanded view instead of its
  collapsed header, so a closed row doesn't carry a column for it.

## v0.47.1

Polish on the new decline controls, from the v0.47.0 review round:

- The per-deck counts atop Update my decks no longer count cards you've said
  Never to: a deck whose only pending card is hidden now reads the same as the
  list below it, instead of promising cards that never appear.
- Declined cards now lists every registry entry, including one a hand-edited
  file left unreadable: it renders under Other, named by its internal id, with
  a working Offer again, so there is always an in-app way back. Previously such
  an entry silently kept its card declined while the dialog showed nothing.
- The Manage decks "Declined cards (N)" count updates when that dialog closes,
  instead of staying stale after you offer cards again.
- Each Offer again button is named for its card ("Offer again: ..."), so screen
  readers no longer announce an undifferentiated list of identical buttons.
- Internal cleanups and new test coverage behind all of the above; no other
  behavior changes.

## v0.47.0

Per-card decline controls on Update my decks, replacing the old all-or-nothing
"flag problems" checkbox with a decision on each card:

- Every new-card row now carries an Import / Skip for now / Never choice
  (Import is the default), and every changed-card row carries an Apply / Keep
  mine for now choice (Apply is the default). Choosing Skip for now or Keep
  mine for now opens a small, optional feedback box for that card; every other
  row still carries a quiet "Add note" link, so writing something down never
  requires declining a card.
- Choosing Never collapses the row to a single struck-through line reading
  "won't be offered again." A card you skip or keep comes back on that deck's
  next update, already pre-set to the same choice and marked SKIPPED or KEPT
  YOURS, with a hint if the upstream content changed since you decided; a
  card you've said Never to isn't shown as a row again at all, and the run
  just reports how many are being held back.
- Declined cards are filtered out of the downloaded package before any
  import, so nothing you skipped, kept back, or said Never to can land in or
  overwrite your collection, whether you ran Update my decks yourself or
  auto-sync applied it in the background. Nothing already in your collection
  is ever deleted.
- Manage decks has a new "Declined cards" button listing everything you've
  declined, grouped by why, each with an Offer again button that undoes the
  decision and re-offers the card on your next update.
- The end-of-run digest now includes a line for every decision you made this
  run, with or without a note attached.
- The "Let me flag problems with cards as they sync" setting is gone;
  feedback is contextual now, tied to a card's own row instead of a global
  toggle. An install that had it on or off just drops the setting silently on
  its next save.

## v0.46.2

Fixes from a third review round, mostly where the v0.46.x fixes meet:

- A deck whose preview download failed no longer skips its consent questions.
  The retry that v0.46.1 added downloaded the deck at apply time but never
  looked inside it, so a note-type format change went unasked (and Anki then
  quietly skipped those cards while the deck was marked up to date). Failed
  previews are now retried right after you confirm, and anything found in
  them joins the same single consent flow as every other deck.
- Backups of two decks whose names reduce to the same filename (names in
  another alphabet, or differing only in punctuation) no longer overwrite
  each other; each name now carries a short unique suffix, and older backup
  files are folded into the same pruning so they don't pile up.
- Remove empty cards and Clean up duplicates now back up the decks the
  affected cards actually sit in, not just the configured deck; content
  updates do the same for cards you've refiled (on Update my decks and
  auto-sync), and the README now says exactly which flows cover what.
- The renamed consent dialogs can be dismissed again: Escape and the close
  button now mean the safe answer, which is also the default, so pressing
  Enter can never agree to a full AnkiWeb sync unread. The remaining
  consequential questions (continuing without a backup, importing a file,
  restoring, installing an update) got named buttons with safe defaults too.
- Moved, superseded, and kept-back cards on the confirmation and summary
  screens now show a readable card name instead of raw markup (an image
  card no longer renders as a broken picture, a cloze card no longer shows
  its braces).
- Cancelling an update no longer discards a ticked "apply the new card look"
  for the decks that finished before the cancel.
- A deck held back for a template change no longer makes auto-sync
  re-download it and take a fresh backup on every poll, and a failing backup
  nags once per session instead of every poll.
- Reconcile now leaves decks you've opted out of alone, matching what Manage
  decks promises, and Manage decks shows opt-outs for decks the current
  source doesn't offer, with a link to clear them.
- Broken deck sources are no longer announced as "Couldn't reach the deck
  source" when the source was reached but its manifest is unusable; the two
  cases now read differently, and a manifest that isn't even a JSON object
  gets the same plain diagnosis.
- Smaller fixes: a card both retired and relocated by the same update stays
  in the Retired deck; the look-change checkbox sits directly under the
  sentence it answers when a format change is also listed; a blank repo on
  the GitHub source form warns instead of silently discarding what you typed
  (including a token); the Settings summary reports the card-feedback
  toggle; deck backups survive Anki builds missing newer export options; and
  the demo's "N more not shown" line counts cards rather than list rows and
  its consent dialogs show the real button labels.

## v0.46.1

Follow-up fixes to v0.46.0, from a second review of that release:

- A deck whose preview download failed really does still import now. The
  confirmation said so, but the apply step used to replay the failed preview
  instead of trying again; it now retries the download (with Cancel still
  live), and a file that downloaded fine but couldn't be previewed is kept
  and used rather than fetched twice.
- Opening Manage decks while your source was unreachable could silently erase
  every deck opt-out on Save, and Change source carried that empty state into
  the reopened dialog. Exclusions now survive a dialog that couldn't show the
  deck list, a source switch, and decks the current source doesn't offer.
- The backup taken before a run now also covers the decks touched by
  retired-card archiving, relocations, and reworded-card merges, wherever
  those cards actually sit; unattended auto-sync scopes its backup the same
  way (and skips the run when it can't); Import single deck backs up the
  decks named inside the chosen file. A run that backed up some decks but not
  others now says exactly which, instead of claiming no backup was taken, and
  each deck's backups are pruned separately so one deck's history can't evict
  another's.
- These backups are also written in the older package format again, so the
  add-on's own tools (and older Anki versions) can read them back.
- Background auto-sync now also waits for the Advanced actions (Clean up
  duplicates, both Restore actions, Remove empty cards) instead of
  possibly applying an update while one of their confirmations was open, and
  Remove empty cards re-checks which cards are still empty after you confirm.
- Two decks whose files share a name in different folders of the deck source
  no longer overwrite each other's downloads, and a download in progress can
  no longer be read half-written.
- A GitHub source with a broken or empty manifest.json now says so plainly
  instead of "Couldn't reach the deck source" or "No deck source configured";
  a manifest entry missing its name no longer breaks Manage decks.
- The template and format-change questions now use buttons naming the action
  (such as "Apply the new look") instead of bare Yes/No, and the look-change
  checkbox sits at the top of the confirmation next to the sentence
  explaining it rather than below the card list.
- Cancelling an update no longer drops the report of fields that were kept
  back from decks that did finish, and a field whose update matches what you
  had written yourself is no longer reported as a conflict.
- Everywhere the add-on mentions a held-back "card-template update" it now
  also names a note-type format change, since both are held for a manual run.
- Smaller fixes: a long source error wraps instead of stretching the dialog;
  Select all/none no longer appear when there are no decks to select; wide
  deck names elide by actual width; the local-folder option says which folder
  to pick (macOS never showed the picker's own caption); a deck synced from a
  local folder no longer writes its temporary copy into that folder. In the
  demo, card rows now start collapsed like the real thing, and a list longer
  than the demo builds shows how many more rows it holds.

## v0.46.0

Safety fixes:

- A `.apkg` in Anki's newer export format no longer imports as if it were nearly
  empty. Import single deck used to read only the compatibility stub inside such a
  file: the preview matched nothing, the import then overwrote every field
  (protected ones included), and the restore had nothing to restore onto. Deck
  names, card matching, and note-type checks now read the file's real data where
  possible, and anything that can't (inside Anki, which ships no zstd decoder)
  stops up front with a clear ask to re-export with "Support older Anki versions"
  ticked, before anything touches your collection.
- If a step after a deck's import failed, that deck's protected fields could be
  left un-restored for the run. The restore bookkeeping now happens immediately
  after the import, so a later failure can't skip it.
- The pre-update backup now covers every top-level deck the run is about to touch,
  not only the configured export deck, and speaks up when your cards exist but
  nothing exportable covers them, instead of proceeding silently.
- Import single deck no longer modifies the note type (which could force a full
  AnkiWeb sync) before you've confirmed the import and the backup has run.
- Background auto-sync and a manual sync can no longer interleave: each waits its
  turn, and a finished run merges its results instead of overwriting what a
  concurrent run just recorded.
- Downloaded files and the self-update package now land in private, unpredictable
  temp locations, and saved state files are written atomically.

Dialog and flow fixes:

- Enlarging the update confirmation before scrolling could leave the card list
  silently ending at the first batch; the list now keeps filling the taller
  window (and still builds lazily as you scroll).
- Cancel in the progress dialog now responds during a deck's download, not only
  between decks, and a cancelled download reads as cancelled rather than failed.
- A note-type format change (for example Q&A to cloze) is disclosed on the update
  confirmation and asked about once, before the run applies anything, instead of
  as a pop-up part-way through.
- A configured source that fails to load now shows what's actually wrong (missing
  folder, missing or invalid manifest.json) in warning colouring, instead of
  claiming nothing is configured, and the button beside it stays "Change source".
- The local-folder source is picked with a real folder picker instead of a typed
  path.
- Manage decks keeps your unsaved checkbox and protected-field edits when you
  open Change source; two decks sharing a leaf name are disambiguated and every
  row carries its full path as a tooltip; the auto-sync interval control follows
  its checkbox; closed dialogs release their memory instead of holding it until
  Anki exits.
- The demo page now renders the card-feedback boxes and the end-of-run digest,
  and follows your browser's dark mode with the add-on's real dark palette.
- A manifest with a malformed schema or deck entry now degrades gracefully (the
  "update the add-on" notice, or skipping the bad entry) instead of erroring out.

## v0.45.2

- Formulas now read as formulas in the review dialogs. A card written with MathJax
  markup used to reach the new-card review (and the "was" rows) as raw backslash
  code like `\(\text{PaCO}_2\)`; the preview now renders the constructs the decks
  use as plain text with real sub/superscripts (PaCO₂ = 1.5 × HCO₃⁻ + 8), and
  fractions as an inline slash. Only the preview changes; the card itself always
  typeset correctly during actual reviews.

## v0.45.1

- Fixed Update my decks ending in "Something went wrong: wrapped C/C++ object of type
  QTimer has been deleted". The run itself was fine, but the step that saves any notes
  you flagged and cleans up afterwards ran a moment too late, once Anki had already
  taken the screen's widgets away. It now runs while they are still there. Present in
  v0.44.0 and v0.45.0.

## v0.45.0

- Every card in a list now starts at the same place. A row's chip sits in a column of
  its own rather than inline with the text, so the fronts line up down the whole list
  instead of starting wherever the chip beside them happened to end, and the chips
  themselves are rounded pills of one width.
- Retired and relocated cards sit under the deck they belong to, alongside that deck's
  new and changed cards, rather than in their own sections at the bottom.
- No screen shows a bulleted list any more. Sync decks, Reconcile my decks, Clean up
  duplicate cards, Remove empty cards, and every end-of-run summary use the same rows
  the update screen does, and the lists that used to stop at fifteen entries with an
  "and N more" now show everything.
- Choosing where decks come from is a proper screen: each source gets its own button
  with a line explaining it, and the one to pick first says so.
- Manage decks shows each deck's state as a chip, the same chips a sync uses, instead of
  coloured text.
- Settings is ruled off into its four groups, and each explanation is cut back to what
  actually changes the decision.
- Counts read as sentences: "1 deck has updates", "2 retired cards", never "1 deck(s)"
  or "1 cards".
- The caret that opens a card is legible now, and rows separate as clearly on the light
  theme as on the dark one.

## v0.44.0

- Pending cards now sit directly on the update screen, as rows in one list under the
  summary and above the Update button, instead of behind a separate Review button.
- Retired and relocated cards are rows in that same list too, each carrying its own
  chip, so every kind of pending change reads as one list rather than a summary with a
  button below it.
- The list streams: it builds its first batch of rows and adds more as you scroll, so
  the screen opens instantly whether a handful of cards are pending or a whole backlog.
- The end of a run now uses the same row and heading look the update screen does, so a
  run reads as one product from open to close.

## v0.43.0

- Every colour now comes in a light and a dark version, picked from Anki's own theme. The
  explanation text on a card, the cloze fills, the links and the muted help text were all
  hard to read in Night Mode; all of them now meet the WCAG AA contrast standard on both
  themes, as does every block that carries its own background.
- The NEW and UPDATED markers in the review read as chips rather than as tinted text.
- The update confirmation starts at the top of its window instead of floating in the middle.
- Advanced is regrouped so its items sit with the ones they belong with: acting on the deck
  source, repairing the collection, then the backup and restore pairs.
- "Import intern pearls deck" is now "Restore intern pearls deck", which is what it does and
  matches "Restore full collection".

## v0.42.0

- The review dialog shows a card's pictures. Opening a row extracts just that card's
  images from the deck file already downloaded and renders them in place; a collapsed
  row still names them, so a long list stays cheap and a review nobody opens extracts
  nothing.
- Review now covers cards an update would change, not only cards it would add. A card
  whose content was rewritten upstream used to import silently because it matched an
  existing card; it is now counted per deck, named on the confirmation, and readable
  before anything applies, with what it says today shown under each field that moved.
- Rows carry a NEW or UPDATED marker, and the review button names whichever kinds it
  covers.
- Fixed Configure source: choosing a local folder while a GitHub repo was already
  configured left the repo in effect, so the folder was silently ignored. Picking a
  local folder now actually switches the source to it, and no longer clears the
  GitHub token, so switching back doesn't cost the saved credential.

## v0.41.1

- Fixed the flagged-card summary being unreadable in Night Mode. The text block set its
  own near-white background but left the text colour to the theme, so the text came out
  light grey on near-white: measured at 1.34:1, against the 4.5:1 needed to read
  comfortably. It now takes both colours from the theme, so it is readable in either one
  and no longer a white box on a dark screen. Copying was never affected, only reading.

## v0.41.0

- Notes you write about a new card are now saved as you type, not only when the review
  closes. They used to live in memory until the summary at the very end of a run, so
  anything that ended the run in between (a crash, a force quit, an error partway
  through the import) threw them away without saying so. Anything still unsent is picked
  up automatically by your next update and included in that summary, with no recovery
  prompt to click through.
- Fewer dialogs in an update. The question about applying a deck's new card look has
  moved onto the one confirmation as a checkbox, so it no longer interrupts the run
  after the import has started; unticked still means your current card look is kept.
  A deck whose new cards can't be read is named inside the card list instead of as its
  own warning. And the completion summary and the flagged-card summary now arrive as
  one dialog rather than two back to back. A busy update goes from six dialogs to three.

## v0.40.0

- Reviewing new cards now shows which fill-in-the-blank belongs to which card. A card
  with more than one group of blanks is really several cards sharing one field, and
  every blank rendering the same colour gave no way to see where one card ended and the
  next began. Each blank now carries its group number as a small superscript (c1, c2),
  so a table whose rows are separate cards reads as separate cards. A field with only
  one group is left unlabelled, since there is no distinction to draw there.

## v0.39.0

- New Advanced menu item, "Remove empty cards." When a deck rewrites a fill-in-the-blank
  card to use fewer blanks, the cards generated for the blanks that are gone stay in your
  collection and come up in review reading "No cloze 3 found on card." This is Anki's own
  Tools > Empty Cards scoped to your deck: same report, filtered to the notes under your
  scope tag, with every card it proposes to remove listed first. It is the one action here
  that deletes rather than archives, since an empty card has no content to keep, and it
  never leaves a note with zero cards, so no note can be deleted by it.

## v0.38.3

- The new-card review now shows a card the way the card is written. Tables and bulleted
  lists were being reduced to plain text first, so a card whose answer is a comparison
  table arrived as one run-on line of cell text and read as unusable when it was fine.
  Tables, lists, bold and line breaks are kept; anything else is still stripped, and
  pictures are still named rather than drawn, since the review runs before the deck's
  media is on disk.

## v0.38.2

- Fixed a false alarm: cards in decks that had no update at all were being reported as
  having a conflict between your own notes and the deck's. Nothing had imported over
  those cards, so nothing of yours was overwritten and there was no update to conflict
  with. Only cards an update actually wrote are considered now.
- Reworded that report, which said "your edits sit on a field the deck source also
  changed" without saying what had happened or what to do about it. It now says plainly
  that your version was kept, the update to that one field was skipped, and nothing you
  wrote was lost.

## v0.38.1

- Fixes an update failing with 'Protocol message ChangeNotetypeRequest has no
  "new_notetype_name" field' on any deck that reformats a card. The add-on was setting
  a field that does not exist on the real message. Decks that reported this were not
  applied and were not marked as installed, so simply running Update my decks again on
  this version picks them up; nothing was left half-imported.

## v0.38.0

- Fixes v0.37.0 for anyone using FSRS. FSRS schedules from a card's memory state rather
  than from its interval, so seeding an extra blank with an interval alone left the
  number saying one thing and the scheduler computing another. The parent's memory
  state, desired retention and decay now travel with the interval, at half the
  parent's stability since stability is what the interval is derived from. Difficulty
  carries over unchanged, since how hard the material is does not depend on which blank
  is asking about it. No effect on a collection not using FSRS.

## v0.37.0

- When a card is reformatted into several fill-in-the-blanks, all of them keep your
  progress, not just the first. Anki carries the original card's scheduling onto one
  blank and creates the rest as brand new, which would have dropped a very large new
  queue on you the first time a whole deck was reformatted, for facts you have been
  reviewing for months. Each extra blank now inherits the original's ease and standing
  at half its interval: producing one blank cold is harder than recognising the
  paragraph the original tested, so it comes back sooner to prove itself rather than
  either starting from scratch or coasting on the old interval.
- A blank whose original card had never been studied still starts new, and a card with
  reviews of its own is never overwritten.

## v0.36.0

- The format conversion added in v0.35.0 now actually finds your cards. Anki keeps both
  note types and adds a "+" when an imported one collides with an existing name, so a
  collection that has synced these decks across several updates holds cards on
  "Study Deck - Basic", "Basic+", "Basic++" and so on. The check was matching the exact
  name, so on a real collection it would have converted 30 cards and skipped 595.
- Understands deck sources using the newer manifest format, which is what lets a deck
  ship a reformatted card as the same card you already have rather than a new one.

## v0.35.0

- A card that changes format keeps its review history. When a question-and-answer card
  becomes a fill-in-the-blank, its note type changes, and Anki's importer will not move
  an existing note to a different type, so until now the deck had to retire your card and
  give you a new one starting from zero. Sync now offers to move your own cards to the
  new format first, so the update lands on the card you already have. You keep one card,
  with its history and your personal notes. Anki treats this as a schema change, so it
  asks first and warns about the one-time full AnkiWeb sync, the same as a card-styling
  change. Declining still imports them, just as separate new cards.
- Background auto-sync never does this on its own, for the same reason it never applies a
  styling change unattended: the deck is held back for a manual sync instead.
- Only the deck's own note types are ever converted, never one of your own.

## v0.34.0

- A preserved field no longer means a frozen field. Preserving a field used to restore
  your copy of it over every sync forever, so protecting anything the decks actually
  ship a value for (Dosing, say) meant never receiving a correction to it again. The
  add-on now records what the deck source last shipped for each preserved field, so it
  can tell your edit apart from the author's: a field you have never touched takes the
  update, and one you have edited keeps your version. That makes preserving every field
  a reasonable thing to do rather than a trade-off.
- Where your edit and an update land on the same field, yours is kept and the sync
  summary now names those cards, so you can send them back to be folded in instead of
  the two versions quietly drifting apart.
- A preserved field name now matches whatever its capitalisation. "notes" used to
  protect nothing at all, with no error, and the first sign was an annotation gone.

## v0.33.0

- Update my decks now repairs a card you ended up holding twice. When a card's wording
  changes, the deck source freezes its identity so the new wording lands on the copy you
  already have. If your copy's identity had drifted before that freeze, the new wording
  arrived as a second card instead, leaving your review progress on the outdated one and
  the current one starting from zero. Update my decks now spots those pairs, moves your
  progress and your personal notes onto the current wording, and archives the outdated
  copy along with everything else it archives. Nothing is deleted, the outdated copy
  keeps its own history, and a card you were already further along on is never rolled
  back to an older schedule. Reconcile my decks does the same thing on its own.

## v0.32.3

- Retired cards are found and archived even when your copy carries an older GUID than
  the one the deck source retired. Reconcile matched on GUID alone, so a card you
  imported before its identity was frozen was invisible to it: the replacement cards
  arrived, the old bulky one was never archived, and it duplicated them in every
  review with nothing to indicate anything was wrong. It now falls back to matching by
  front text, the same signal a content sync already uses, exactly as relocating a
  reorganized card has done since v0.29.1.

## v0.32.2

- A new card's dosing block is readable in Night Mode again. It set its own light
  background but left the text color to the theme, so the text turned white on a
  near-white block. Found by rendering the dialog on a dark background, which is
  something the add-on's tests, running on a mock Qt, cannot see.

## v0.32.1

- The new-card review list is much tighter to read. Each row's caret was an
  unconstrained push button sitting at its platform minimum width, which left a wide
  empty gutter down the whole list, and nested layouts each added their own default
  margins on top. A card's tag now shares one line with its text rather than sitting
  in a separate widget beside it, so every row's text starts at the same place and
  wraps against the row's edge instead of the tag's.
- Fixed the doubled hairline under every card. The rule was a border on the row
  itself, and a stylesheet with no selector propagates into a widget's children, so
  each row drew an inset second copy under its own header. It's now one rule between
  each pair of cards, and none after the last.
- The green rule beside a card's "why" now actually appears. Qt ignores a lone
  border-left on a label unless the border shorthand is set first, so it had never
  painted; the indent it created made it look intentional.

## v0.32.0

- A restore is now re-detected. The add-on's record of which deck versions you've
  already applied lives outside your collection, so restoring a backup used to roll
  your cards back to older content while Update my decks kept reporting you were up
  to date. Both restore paths now clear the relevant part of that record: Restore
  full collection clears it entirely, and Import intern pearls deck clears just the
  decks in the file you're restoring (falling back to clearing all of them if the
  file can't be read). Your next Update my decks re-offers whatever rolled back, and
  the re-import still matches by GUID, so review history carries over.
- New Settings toggle, "Let me flag problems with new cards as they sync," off by
  default. With it off, reviewing the new cards an update would add is a quick,
  read-only preview: no note boxes, nothing to send afterward. Turn it on to get
  both back.
- The new-card review itself now reads as a scannable list instead of a stack of
  full note dumps: one row per card, with a caret, its tag, and its primary line
  collapsed by default, expanding on click to the answer, why, and dosing. A cloze
  card shows its deletions filled in rather than raw markup, and an image is named
  rather than rendered, since the review has no access to the deck's media on disk.
- The flagged-card summary is easier to use once you have it: a monospace,
  read-only view styled like the payload it is, with a Copy again button in case
  something else lands on your clipboard first.

## v0.31.0

- You can now read the cards an update would add, before it adds them. "Update my
  decks" already listed retired and relocated cards by name, but a card being *added*
  only ever showed up as a count ("3 new"), so new cards were the one kind that
  arrived without ever being seen first. The confirmation now names them like
  everything else, and a "Review N new card(s)" button opens each one in full: every
  field, labeled, grouped by deck. Reviewing doesn't cost you the decision, since the
  confirmation stays open behind it, and nothing is applied until you choose Update.
- Cards you review can be flagged with a note, and closing the review hands you a
  plain-text summary of everything you flagged, copied to your clipboard and shown so
  you can see exactly what it says before sending it back to whoever maintains the
  decks. Each entry names the deck, the card, and its id, so a fix doesn't have to
  start by working out which card you meant. The summary is offered whether you go
  ahead with the update or back out of it: notes you took are worth keeping either
  way. Flagging changes nothing about what imports; it's a message, not a veto.

## v0.30.0

- Configuring a deck source now offers that source's recommended settings. A deck
  author's manifest can carry `scope_tag` and `export_deck` (both optional; older
  add-on versions ignore them), and right after Configure source connects, it offers
  to apply whichever of the two differ from your current config. Accepting means
  field protection and the automatic pre-sync backup cover that source's decks
  without hand-editing raw config keys, which used to be the only way when
  subscribing to a source with its own tag and deck names. Nothing applies without a
  yes, and background auto-sync never touches these settings on its own.

## v0.29.2

- Made the "Clean up duplicate cards" confirmation actually readable. Each line now
  leads with a readable label for the card, so an image card identifies itself by its
  prompt or image filename instead of rendering as a broken-image icon with no way to
  tell what it is. When both copies sit in the same deck the line reads as a copy count
  ("2 copies in <deck>: keeping the one with N reviews...") rather than repeating the
  deck name twice, and the summary heading is reworded ("Found N duplicate copies of M
  cards") so it no longer reuses the word "card" to mean two different things. Behavior
  is unchanged: this only affects what the dialog shows, not which copy is archived.

## v0.29.1

- Fixed a bug where a reorganized deck could be offered as "needs update" forever,
  even right after applying it, with nothing actually changing. It happened to a
  card whose deck source changed its internal ID (so a long-time collection holds it
  under an older ID than the current one) and which a later reorg moved to a new deck:
  Reconcile matched the relocation by ID only, so it never moved that card, the new
  deck looked permanently empty, and it kept getting re-offered. Reconcile now also
  matches such a card by its front text (the same signal a normal sync already uses),
  so it relocates on the next "Update my decks" and stops re-offering. Requires the
  deck source's manifest to include each move's front; an older manifest is handled
  gracefully (unchanged behavior).

## v0.29.0

- New Advanced menu item, "Clean up duplicate cards." Finds sync duplicates (the same
  card imported twice under different GUIDs, most often right after a deck
  reorganization) and archives the losing copy: suspended, moved to the Retired deck,
  and tagged, exactly like a retired card. Keeps whichever copy has more reviews;
  ties prefer the copy already under the deck source's current deck path. Personal
  notes on the archived copy carry over to the kept one first. Nothing is ever
  deleted, and a backup is taken automatically before anything changes.

## v0.28.0

- New Settings toggle, "Dim bright images in Night Mode." When on, applies a
  brightness and contrast reduction to every image in every deck (not just
  Intern Pearls ones) whenever Anki's Night Mode is active, so a white
  background image no longer renders at full brightness during a night review
  session. Off by default. Takes effect immediately, no restart needed.

## v0.27.1

- Update my decks now caches each pending deck's download for the session, so opening
  it, looking at the preview, and cancelling no longer re-downloads every deck the next
  time you open it. Since v0.26.1 made the preview a real per-deck download, a "just
  checking" habit was multiplying source requests and running into sporadic "server not
  available" hiccups more often; a deck is only re-fetched when its content actually
  changed (the cache is keyed by the content-hash version).
- Loosened the first-contact network timeout from 6 to 10 seconds. This only affects
  user-initiated fetches (never the unattended background poll, which keeps its own tight
  bound), so a connection that's alive but briefly slow no longer fails a click that
  would have succeeded a couple seconds later.

## v0.27.0

- Update my decks and Sync decks now show a real, cancellable progress dialog
  (an actual "N of M decks" bar, not just a static label) while checking for and
  applying updates, with a working Cancel button. Previously `mw.progress`'s
  simple busy indicator gave no percentage and no cancel support at all, which on
  a slow connection reads as a frozen add-on with no way out. Cancelling always
  happens between whole decks, never mid-import, so whatever already completed
  stays applied and persisted; cancelling during Update my decks skips
  archiving/relocating retired cards for that run, since that step assumes every
  content update already landed.
- Update my decks' confirmation now says outright that it's a preview and nothing
  has been applied yet, since Cancel there was already safe and read-only, just
  not obviously so.

## v0.26.1

- Fixed a real bug in the collection-revert reconciliation added in v0.25.2: it
  required an *exact* match between a manifest deck's name and an Anki deck you
  actually have, but a deck spec's `deck_name` is routinely just the parent path —
  cards land in `deck_name::<subdeck>` for any spec using subdecks, which is the
  normal case (the public example deck included). That meant every subdeck-based
  deck was silently treated as "not installed" on every single check, forever,
  forcing a pointless resync each time — caught via the live demo constantly
  offering an update with nothing actually changed. Now matches the manifest name
  itself or any subdeck beneath it.
- Update my decks' confirmation now downloads and matches each pending deck before
  showing it, the same way the old "Check what will sync" preview did — real
  "N kept · M new" counts per deck, not just how big the deck is. A progress window
  covers the check itself, since it's a live download per deck. Nothing already
  downloaded for this preview is fetched again during the actual update.
- The live demo now shows a busy indicator while a menu action is running, instead
  of appearing to do nothing until the next dialog pops up (add-on progress dialogs
  and wait cursors are mocked out in the browser, so they were never visible there;
  this doesn't change the real add-on, only the demo page).

## v0.26.0

- Added **Update my decks**, a new top-level menu item and the recommended way to
  stay current from now on. It computes everything pending in one pass — deck
  content changes, retired cards still in your collection, and cards a reorg needs
  to relocate — and shows one confirmation covering all of it, instead of the old
  multi-step dance of syncing, then separately digging into Advanced to reconcile.
  Content updates apply first, then archiving/relocating, so a retired card's
  replacement is already there before the old card archives out. Sync decks and
  Reconcile my decks still exist under Advanced for running either half on its own.
- Manage decks no longer has its own "Check what will sync" preview button — that
  same preview is now Update my decks' own confirmation, so there was no reason to
  ask twice. "Save and sync now" is renamed "Save and update now" and routes through
  the new unified flow.
- Auto-sync (Settings) still only ever applies deck content on its own, never
  archives or relocates — but it now keeps the "Reconcile my decks" menu item
  labeled with a live pending count (e.g. "Reconcile my decks (3 pending)") and
  shows a one-time tooltip when a backlog first appears or grows, so retired or
  reorganized cards can no longer pile up silently between manual checks just
  because auto-sync is unattended.

## v0.25.2

- Fixed a gap in v0.25.1's collection-revert fix: it only detected a *total* wipe
  (every synced note gone at once), so a revert that only rolled back part of the
  collection — the common case, e.g. one deck's cards erased while others stayed
  intact — still left that one deck wrongly reporting "up to date". The check is now
  per deck: a deck counts as synced only if the collection currently has a note under
  it actually sitting in an Anki deck of that name, so a partial revert is caught and
  recovered the same way a full one is.

## v0.25.1

- Fixed a real bug: restoring an Anki collection backup ("collection revert") after a
  sync could leave Sync decks, Check what will sync, and the Manage decks status pills
  all reporting "up to date" even though the revert had erased the synced cards. The
  add-on's own sync bookkeeping (`installed.json`) lives outside the collection file,
  so it never rolled back along with it — nothing was actually being compared against
  the collection's real contents. All three now reconcile that bookkeeping against the
  collection first, so a deck the collection has lost is treated as not-yet-synced
  again and a normal sync recovers it. Same fix applies to the unattended auto-sync
  poll.

## v0.25.0

- Fixed a real bug: Reconcile my decks' confirmation could become unusable after a
  large backlog (e.g. dozens of cards relocated by one reorg) — it used a plain
  message box with no scroll area, so a long enough list pushed the Yes/No buttons
  off-screen with no way to reach them. The confirmation now scrolls in a fixed-height
  viewport with the buttons pinned outside it, so they're always reachable regardless
  of content length, and the card list itself is capped to the first 15 plus a "...and
  N more" summary so it also reads as a short list rather than a wall of text. A large
  first run also now says up front that it's a one-time catch-up, since the length
  alone can otherwise read as something having gone wrong.
- Tightened the archive/relocate confirmation's copy: one shared "nothing is deleted,
  here's how to undo it" note instead of repeating the same reassurance once per
  section, and action-specific buttons ("Archive", "Relocate", "Archive and relocate")
  instead of a generic Yes/No.

## v0.24.0

- "Check what will sync" (Manage decks) now also reports what Reconcile my decks has
  pending — retired cards still in your collection and cards a deck reorg needs to
  relocate — not just the per-deck kept/new breakdown. Read-only, same as the rest of
  the preview; nothing is archived or moved until you actually run Reconcile.

## v0.23.0

- Sync now refuses to run against a deck source whose `manifest.json` `schema` is
  newer than this add-on version understands, with a clear message to update first,
  instead of attempting an import against a manifest shape it can't fully interpret.
  Auto-sync applies the same check and pauses quietly (one tooltip per session) rather
  than looping every poll interval. This is a forward-looking safety net — today's
  manifest schema (2) is unchanged and every existing source keeps syncing normally.
- Add-on updates are no longer only visible in the 8-second startup tooltip: the
  "Check for add-on updates" menu item now shows the known-available version right on
  the label (persists across the tooltip fading and across restarts, seeded from the
  last check), and About shows the same "latest known" line next to the installed
  version.

## v0.22.0

- "Reconcile my decks" now also relocates cards a deck reorg has moved to a
  different deck without changing their identity (e.g. a topic getting split
  into its own deck) — a normal sync updates such a card's content in place but
  never its deck, since only a brand-new card gets filed into the source's
  declared deck. Reconcile reads a new `deck_moves` ledger the source ships in
  its manifest and moves any card still sitting exactly where the source last
  filed it; a card you've since filed somewhere of your own choosing is left
  alone. Schema-neutral and trivially reversible, same as the retired-card
  archiving this action already did.
- Reconcile also now carries a personal note (or any other protected field) from
  a retired card onto its replacement(s) before archiving it, as long as the
  replacement's field is still blank — so annotating a card doesn't get stranded
  the moment it's superseded by a split or reword.

## v0.21.0

- New Advanced action, "Reconcile my decks": finds retired cards still in your
  collection — older versions of cards a deck has since split into focused ones or
  reworded — and archives them so they stop showing up as duplicates in your reviews.
  Each is moved to an `…::Retired` subdeck, suspended, and tagged; **nothing is
  deleted**, review history is kept, and anything can be brought back by unsuspending
  it or moving it out of the Retired deck. A backup is taken automatically first.
  Reading a new `retired` ledger the deck source ships in its manifest; older add-on
  versions ignore it.

## v0.20.1

- "Try the example deck" now scopes its automatic backups to the parent
  `Example Decks` deck instead of a single subdeck, so all of the example repo's
  decks (it now ships more than one) are covered by the pre-sync backup.
- Live demo: the default source is the example GitHub repo (exactly what "Try the
  example deck" configures in real Anki) instead of a local folder; the demo serves
  that repo's files from its in-page copy so the maintainer buttons still take
  effect instantly.

## v0.20.0

- Cards now match by GUID first, before front text and `front_aliases`. Deck sources
  that keep GUIDs stable (an explicit per-card `id` in the spec) can reword a card's
  front any number of times without an alias entry, and the learner's review history
  still carries over — the single-hop limit of `front_aliases` no longer applies to
  those cards. Front-text and alias matching remain as fallbacks for collections whose
  GUIDs predate stable ids.
- Sync now detects when an updated deck changes a card template or its CSS (the one
  thing `merge_notetypes=False` imports deliberately never propagate) and offers to
  apply the new look, explaining that doing so makes the next AnkiWeb sync a one-time
  full sync. Declining keeps the current appearance; content and history import
  either way. Import single deck gets the same offer.
- The unattended auto-sync poll never applies a template change (no one is there to
  consent to a full sync): a deck update that includes one is held back, stays
  pending, and a tooltip points at Sync decks to review it — mentioned once per
  session, not on every poll.

## v0.19.0

- Sync decks now shows Anki's progress window while each deck downloads and imports
  ("Syncing <deck> (2 of 5)"), instead of appearing frozen on a slow connection. The
  unattended auto-sync poll is unchanged; it already ran its downloads off the main
  thread and reports through tooltips.
- The GitHub source setup is one dialog with both fields (repo, optional masked
  token) instead of two prompts in a row, so cancelling the token question no longer
  throws away the repo you just typed.
- The blocking waits that remain on the main thread (opening Manage decks, testing a
  just-saved source, "Check what will sync") now show the busy cursor while they run.

## v0.18.2

- Internal restructure, no behavior change: the single 1,600-line `__init__.py` is now
  nine modules split by concern (`config`, `ui`, `net`, `collection`, `sync`,
  `updates`, `background`, `dialogs`, with `__init__.py` reduced to menu and startup
  wiring). `ADDON_VERSION` moved to `internpearls/config.py`. See "Code layout" in the
  README.
- Dialog headings, hints, and link-style buttons now share styling helpers in `ui.py`
  instead of per-dialog stylesheet strings; the three link-style buttons in Manage
  decks now render at one consistent size.
- `build.sh` packages every `internpearls/*.py` file (the previous hardcoded two-file
  list would have shipped a broken add-on after this split) and removes the old
  archive before zipping, so a deleted module can't linger inside the package.

## v0.18.1

- Auto-sync no longer downloads decks on the main thread. Only the manifest check
  moved off-thread in earlier work; the per-deck `.apkg` download (the part that can
  actually take a while on a big deck or a slow link) still ran inside the completion
  callback, so it could still freeze Anki mid-review, which is exactly what background
  sync is supposed to avoid. Downloads now happen alongside the manifest fetch in the
  background step; a per-deck download failure is still reported per-deck (not a fetch
  that takes down the whole sync), same as before.

## v0.18.0

- Fixed public GitHub repos as a deck source: the token is now genuinely optional.
  Previously a GitHub source was only used when a token was set, so following the
  documented "leave the token blank for a public repo" advice silently fell through to
  "no source configured".
- Added "Try the example deck" to the Configure source dialog: one click points the
  add-on at the public `internpearls-example-deck` demo repo, so someone with no deck
  source of their own can watch a sync work end to end. It also points `scope_tag` and
  `export_deck` at the example deck's values (only when they're still at their
  defaults), so field preservation and the pre-sync backup work in the demo too;
  configuring a GitHub repo or local folder later resets exactly those injected values.
- The Sync completion dialog no longer claims a pre-sync backup was saved when none was
  taken (first sync, or the backup failed and you chose to continue); it now says so.
- "Check what will sync" can be run again after it completes (the button re-enables as
  "Check again"), instead of sticking disabled at "Preview updated".
- The background auto-sync poll's manifest fetch now actually uses the tight unattended
  timeout the docs already claimed for it, rather than the interactive 6-second one.
- Docs: token-optional-for-public-repos everywhere the token is mentioned; corrected
  `decks_dir` precedence (GitHub wins when both are somehow set); noted that deck
  backups live in `user_files/` and are removed by an add-on uninstall.

## v0.17.0

- Cleaned up the menu bar. Top level is now just Sync decks and Manage decks; everything
  occasional, including the manual "Check for add-on updates" (most people never need it
  since the background notice already covers that job), moved under Advanced; Settings
  and About now sit together at the bottom, in that order.
- Configure deck source is no longer its own menu item. It lives inside Manage decks now,
  behind a "Configure source" (nothing set up yet) or "Change source" (something is)
  button next to the Source line, since it only ever mattered in the context of what
  decks are available to manage.
- Manage decks no longer dead-ends when no source is configured or the configured one is
  unreachable. It still opens, with an empty deck list, the reason shown right in the
  Source line, and the same button waiting, instead of a warning that sends you off to a
  different menu item that no longer exists.
- Tests: full coverage of the new bootstrap paths (nothing configured, source unreachable,
  source working, and the change-source-then-reopen flow) plus an exact assertion on the
  new menu structure, exercised against a mocked Anki environment.

## v0.16.0

- Fixed the root cause of "Check for add-on updates" sometimes not seeing a version
  that had already shipped: the add-on's own version check fetched `version.json` from
  `raw.githubusercontent.com`, a CDN endpoint that can lag well behind a push. Confirmed
  directly: right after a push, the GitHub contents API reflected the new file
  immediately while the raw CDN link for the same file and branch still served the old
  one more than two minutes later. Both the version check and the package download now
  go through the contents API instead, the same way deck content already did.
- Added a Settings dialog (moved out of Manage decks, since "which decks" and "how
  automatic" are different kinds of choices): sync automation and add-on update
  behavior in one place, with an interval field instead of a fixed hourly check.
- Added "Install add-on updates automatically," off by default, alongside the existing
  notify-only option. When on, a newer version downloads and installs itself as part of
  the once-per-launch check; a restart is still needed to load it either way.
- Lowered the auto-sync poll's default to 15 minutes and its floor to 1 minute (previously
  60 and 15), so decks that need to land within the hour actually can. To make a 1-minute
  floor safe, both background checks (add-on updates and the deck poll) now run their
  network fetch through Anki's QueryOp, off the main thread, so neither one can freeze
  Anki no matter how often it fires. Only backing up and importing, which happens on the
  main thread just like a manual sync, and only once something actually changed, is
  unaffected; the check that runs constantly is what needed to stop blocking. Falls back
  to a short, bounded synchronous call on an Anki build old enough to lack QueryOp.
- Generalized "Notes restored on N card(s)" to "Preserved fields restored on N card(s)"
  everywhere it appears, since the add-on has supported any configured field, or
  several, for a while now, not just Notes.
- Expanded About: it now shows your current auto-sync, add-on update, and preserved-
  field settings, not just a static description.
- Test suite grown to 51: full coverage of the new pure decision logic (interval
  clamping, which action a version check should take given the notify/auto-update
  toggles) plus the auto-sync fetch/apply split and the QueryOp/fallback dispatch,
  exercised with a mocked Anki environment.

## v0.15.0

- Startup update notice: once per launch, a silent check compares your version against
  the public repo's and shows a brief tooltip if a newer one exists — at most once per
  new release, not every launch. Never auto-installs; "Check for add-on updates" is
  still the explicit action that does that. Fixes the confusing case where pushing a
  fix to GitHub doesn't change what's running until you notice and update yourself.
  Toggle with the new `notify_addon_updates` config key (default on).
- Auto-sync decks: a new checkbox in Manage decks ("Automatically sync when updates are
  available", off by default). When on, decks sync in the background — once shortly
  after startup, then on a repeating poll (default every 60 minutes, floored at 15) —
  without asking each time. A backup is still taken first, same guarantee as a manual
  sync; if the backup fails, that round is skipped rather than importing unprotected.
  Results show as a transient tooltip, never a blocking dialog, since this can fire
  mid-review. Toggling the checkbox takes effect immediately, no restart needed.
- The interactive Sync decks and the new background auto-sync now share one
  implementation of the actual import sequence (`_run_sync`) instead of two, so there's
  exactly one place the history-preserving logic lives.
- GitHub load at the default cadence is trivial: one small `manifest.json` fetch per
  poll, well under the unauthenticated 60-req/hour limit even at the 15-minute floor.

## v0.14.1

- When every deck in Manage decks is already up to date, the "Check what will sync"
  button now shows "All decks up to date" and is disabled, instead of looking like
  there's something to check and then reporting nothing on click.

## v0.14.0

- Manage decks can now preview changes in place: a "Check what will sync" button
  downloads the changed decks and fills in each row with how many cards would update in
  place (history kept) vs. be added as new — read-only, nothing is imported. It runs on
  click, not on open, so the panel still opens instantly.
- Retired the separate Preview sync menu item — it's now fully covered by the button
  above, so there's one place to see what a sync will do instead of two. (Sync decks
  still shows its own confirmation and backs up before importing.)

## v0.13.1

- Clearer Manage decks flow. After Save (without syncing), the confirmation now says
  plainly that nothing has synced yet and to run Sync decks when ready, instead of the
  misleading "N decks will sync". Added an inline hint next to the buttons explaining
  that Save keeps the choices for the next sync while Save and sync now also pulls right
  away.
- Decluttered the top menu: Preview sync moved under Advanced (Sync decks already shows
  a per-deck confirmation and takes a backup, so a separate dry run is a power-user
  tool, not a primary action). Top level is now Sync decks, Manage decks, Configure
  deck source, Check for add-on updates.
- Field parsing for the preserved-fields box moved into a tested pure helper
  (`parse_fields`) that also de-dupes.

## v0.13.0

- Added Manage decks: a clean panel listing every deck the source offers, each with a
  checkbox and a status pill (New / Update available / Up to date) and its card count.
  Uncheck a deck to stop syncing it (already-imported cards are left alone); Select
  all / none for quick toggling. Sync and Preview sync now honor the selection.
- The same panel edits Preserved fields (the fields snapshotted and restored around
  every import so your personal annotations are never overwritten) — previously only
  reachable by hand-editing the add-on config.
- Save, or Save and sync now, straight from the panel.
- New config key `excluded_decks` backs the selection; an empty list (the default)
  syncs everything, so existing setups are unchanged.

## v0.12.1

- Fixed the biggest source of post-sync friction: after syncing, AnkiWeb often forced a
  one-way "upload from local" full sync instead of a normal incremental one. Cause: the
  importer ran with `merge_notetypes=True`, which rewrites note types on every import and
  bumps Anki's schema modification time — and any schema change forces AnkiWeb into a
  full sync. Imports now run with `merge_notetypes=False`; note types are still kept
  compatible ahead of time by the existing Fix-note-types step, which only touches the
  schema when it genuinely adds a missing field. Steady-state syncs now leave the schema
  alone, so AnkiWeb stays incremental. (Trade-off: a changed card *template/CSS* no
  longer propagates automatically — run Advanced → Fix note types, or accept one full
  sync, when a template itself changes.)
- Fail fast when offline: network calls used a 30-second timeout on Anki's UI thread, so
  an unreachable host or captive portal froze the app (beachball) for 10+ seconds.
  First-contact calls (manifest, update check) now use a 6-second timeout and show a
  clear "network isn't responding" message; only the actual deck download keeps a longer
  timeout, and it's reached only after connectivity is already confirmed.

## v0.12.0

- Added Preview sync: a dry run that shows exactly what Sync would change — per deck,
  how many cards update in place (history kept) versus get added as new — without
  taking a backup, importing, or writing anything. The "show me first" companion to
  Sync decks.
- Factored the "which decks are pending" decision into `logic.decks_to_update` so Sync
  and Preview sync compute the identical set and can't drift apart.

## v0.11.0

- Sync's confirmation dialog now flags any deck you've never synced before as a new
  deck, separately from its card count.
- Network errors (bad token, wrong repo/branch, unreachable host) now show a specific,
  actionable message instead of a raw urllib exception.
- Every menu action is wrapped so an unexpected bug shows a plain warning dialog
  instead of Anki's traceback box; the full traceback still prints to Anki's debug
  console for troubleshooting.
- Split the add-on's code: `internpearls/logic.py` holds everything that doesn't touch
  `aqt`/`anki` (apkg reading/rewriting, GUID matching, version comparison), so it's
  unit-testable with plain `pytest`, no Anki install needed. Added a test suite
  covering it.
- Deck `.apkg` and spec paths in the manifest can now include subfolders (the private
  decks repo moved its built decks into a `decks/` folder); fixed a bug where the
  GitHub-fetch path assumed a flat filename and would have failed to write the
  downloaded file.

## v0.10.2

- Fixed a factual error in About and the README: the add-on doesn't ship with any deck
  content, it only syncs whatever you point it at.
- "Notes restored on 0 card(s)" no longer shows on a fresh sync, where it's always zero
  and reads like something's missing.

## v0.10.1

- Fixed a crash on every use of Import intern pearls deck (`getFile()` rejects being
  passed both `dir` and `key`).
- Removed Restore my notes. Modernizing Import single deck to do a full one-click
  import, matching how Sync already worked, meant nothing wrote the notes-snapshot
  file anymore, so the button had quietly stopped doing anything.
- Renamed for consistency: Backup intern pearls deck now to Backup intern pearls deck,
  Full collection backup now to Backup full collection, Restore from backup to Restore
  full collection.

## v0.10.0

- Dropped "..." from every menu item, including ones that open a file picker.
- "Intern Pearls" goes lowercase inside Advanced submenu labels (still capitalized as
  the top-level menu name and in dialog titles).
- `export_deck` is a config key now instead of a hardcoded constant, so the
  deck-scoped backup/export/import tools work against any deck hierarchy.
- Added `config.md` so Anki's Config editor documents every key in place.
- Expanded About and added a "Using this for your own decks" section to the README.

## v0.9.0

- The automatic pre-sync backup now defaults to a fast, self-contained export of just
  the configured deck instead of the whole collection, pruned to the 10 most recent.
- Added Backup/Import intern pearls deck (the deck-scoped pair) and Full collection
  backup now (kept for anyone who wants broader protection).
- On a genuinely first sync, before the deck exists, the backup step is skipped rather
  than failing and asking to proceed.

## v0.8.0

- Added Export intern pearls deck: a standalone `.apkg` of just the configured deck,
  with scheduling, deck options, and media included, meant to be kept or shared.

## v0.7.1

- Every dialog now carries the "Intern Pearls" title bar (Anki's helpers default to
  the generic "Anki") and list-style messages render as real HTML bullets.
- Dropped the ellipsis from "About", which doesn't need one.

## v0.7.0

- Fixed sync state getting reset on every add-on update: `installed.json` and the
  notes snapshot used to live next to `__init__.py`, which Anki's add-on manager wipes
  and re-extracts on every update. Both now live under `user_files/`, the one
  subfolder Anki preserves across reinstalls.
- Added Restore from backup, opening Anki's own backup picker.

## v0.6.0

- Sync decks and Import single deck take a real backup automatically before touching
  anything, instead of just asking the user to remember to export one first.
- Confirmation dialogs show per-deck card counts and say plainly what's about to happen.
- Configure deck source became a proper multi-button dialog instead of a Yes/No
  question standing in for a choice; the access token field is masked; saving tests
  the connection immediately.
- Fixed a silent failure: if the front-alias list can't be fetched, the user is now
  warned instead of reworded cards quietly losing history.

## v0.5.1

- Fixed menu items vanishing on macOS: Qt auto-detects labels like "Configure..." and
  "About..." and relocates those actions into the native app menu, which can hide them
  entirely if Anki already owns that role slot.

## v0.5.0

- Adopted three-part semver (`0.5.0`, not `0.5`) and made the update comparator treat
  `0.5` and `0.5.0` as equal.

## Earlier

Menu, one-click history-safe sync (fix note types, snapshot notes, match GUIDs, import,
restore notes), and GitHub-based distribution were built out before this changelog
started; see the deck repo's own notes for that history.
