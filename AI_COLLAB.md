# AI collaboration

Full, unedited chat history: [`ai-chat-history/`](ai-chat-history/):
[`conversation.md`](ai-chat-history/conversation.md) to read,
[`transcript.jsonl`](ai-chat-history/transcript.jsonl) for the raw export.
It starts at the very first prompt.

## How this was built, honestly

This document covers seventeen decisions. I set the objective, the constraints
and the quality bar, and handed the
implementation to Claude (Claude Code, Opus) with an explicit mandate to make
the technical and design choices itself: my words were *"you to choose
everything professional from colour to everything"* (excerpt 1).

So the interesting question is not "who typed which line". It is **what stopped
a confident AI from shipping something that merely looked right**. The answer,
in every case below, is the same: *the engine had to prove itself against
measurable ground truth before any of it was believed.* The AI proposed
building that benchmark before the UI; I kept it, and it is the single decision
the rest of this document hangs off. It overturned the AI's own first
implementation four separate times, documented below with the numbers.

Where the AI's first answer survived, I say so. Where it was wrong, I say that
too, including the metric that reported two *identical* images as completely
different.

---

## Decision 1: Shape of the solution

**AI's opening analysis:** framed the task as PCB defect detection and surveyed
learned detectors (YOLO-family) and one-class anomaly detection
(PatchCore/PaDiM via anomalib).

**Decided:** neither. Registration + per-ROI classical comparison against the
golden board.

**Why:** the brief hands you **one** reference photo, at the demo, for a board
nobody has seen. A detector cannot be trained in the room. PatchCore needs a
corpus of normal images, and with one reference it degenerates. Both also
answer the wrong question, the operator names **C1** and **U7**, not
"capacitor". And neither can explain itself: an operator told to scrap a board
asks *why*, and "the patch distance exceeded a threshold" ends badly.

**Evidence:** [`RESEARCH.md §3`](RESEARCH.md). The decisive constraint is in
the brief itself: *"We upload one reference photo and name the parts in your
app."*

---

## Decision 2: Build the benchmark before the UI

**AI suggested:** build the engine, then the UI, then evaluate.

**Decided:** generate ground-truth data and an evaluation harness *first*, and
treat any engine component as unproven until it scores.

**Why:** "most accurate, reliable" is not a thing you can eyeball on three
pretty screenshots. Without numbers, every later decision becomes an argument
about taste.

**Evidence:** it paid off on the very first run: **50% precision**, 14 false
alarms. Had the UI been built first, that would have been found at the demo.
Every subsequent decision here was forced by that harness.

---

## Decision 3: Colour metric: HSV → CIELAB chroma

**AI's first implementation:** hue/saturation histograms with a Bhattacharyya
distance. Textbook choice, looks reasonable, passed review by eye.

**Decided:** throw it out for CIELAB chroma histograms, smoothed, with bin
count scaled to patch size.

**Why:** the harness showed the metric returning **~1.0 ("completely
different") for identical patches**. Hue is undefined for near-neutral colours,
and a PCB is overwhelmingly neutral: black IC bodies, white connectors, grey
pads. Those hues were sampling noise, so two photographs of the *same* part
shared almost no histogram bins. Lab chroma degrades gracefully: neutrals
cluster near the origin, where they belong.

**Evidence:** excerpt 2. Fixing this and Decision 4 moved precision from
**50% → 100%** on the 10-board set.

---

## Decision 4: Outline metric: Canny edge IoU → gradient correlation

**AI's first implementation:** intersection-over-union of dilated Canny edge
maps. Intuitive, a component is a silhouette, remove it and the silhouette
goes.

**Decided:** correlate gradient-magnitude maps instead.

**Why:** blur pushes gradients below the Canny threshold, so a slightly
defocused photo empties the test edge map and a component that is plainly
still there reads as absent. Correlation of gradient magnitudes is invariant to
the uniform amplitude loss blur causes, while still collapsing when structure
appears in *different places*, which is what a removal actually looks like.

**Evidence:** present parts were scoring `edge 0.00: 0.12` against a reference
where they were obviously present (excerpt 2).

---

## Decision 5: Fix the root cause, not the threshold

**AI suggested**, when 36 false alarms survived on the larger set: widen the
ramps and lower the weights of the offending channels.

**Decided:** no. Find out *why* first.

**Why:** moving a threshold until the test passes is how you get a system that
works on your data and fails on theirs. The failures were not scattered,
**every single one was the same 22×40 px chip resistor**, flagged even on
boards where nothing had been removed, with signals that barely moved as photo
quality varied. That is a systematic cause, not noise.

The cause: the reference is a careful, sharp photo and line photos are not.
Comparing a sharp patch to a soft one inflates every difference, worst on small
dark parts ringed by bright pads, where blur bleeds the pads into the body. The
fix is a principle, not a constant, **a comparison may only use information
that both images actually contain**, so the engine now equalises sharpness,
blurring whichever image is sharper, exactly as it already equalised exposure.

**Evidence:** excerpt 3. False alarms **36 → 0**, with recall still 100%.

---

## Decision 6: Weight the signals by measured discriminating power

**AI's first weights:** assigned by intuition: pixel residual and texture
energy looked like strong evidence of a missing part.

**Decided:** reweight by how well each channel separates *a removal* from *a
bad photograph of a complete board*, and demote the two that failed that test.

**Why:** the diagnostic output showed raw residual and texture amplitude
scoring **higher for present parts in harsh photos than for genuine removals**.
They were largely measuring the nuisance, not the defect. They stay as
low-weight tie-breakers because they are what would catch a part swapped for a
different one of the same colour and outline.

**Evidence:** separation between the worst false alarm and the weakest true
detection widened from **0.05 to 0.18**, the difference between a knife-edge
threshold and a safe one.

---

## Decision 7: 10 boards was not a benchmark

**AI reported:** 100% precision and 100% recall on the 10-board set, and was
ready to move on.

**Decided:** don't believe it. Generate 80 randomised boards spanning the full
nuisance range, with a third deliberately complete so the false-alarm rate is
measured honestly.

**Why:** 10 boards is 240 decisions dominated by easy cases. It is exactly the
sample size that lets you fool yourself.

**Evidence:** precision on the larger set was **82%**, not 100%. Everything in
Decisions 5 and 6 was found only because the sample got bigger.

---

## Decision 8: Thresholds read off the curve, not chosen by eye

**AI suggested:** sensible-looking round numbers (0.62 / 0.52 / 0.42).

**Decided:** derive them from the operating curve and place them inside the
measured gap.

**Why:** a threshold is a claim about where the two populations separate.
Guessing it is guessing the answer.

**Evidence:** over 1,920 decisions, the highest-scoring part that is actually
present scores **0.247** and the weakest genuine removal **0.424**. The presets
sit inside that gap with margin on both sides, and `tools/evaluate.py --sweep`
reproduces the curve.

---

## Decision 9, A local web app, not a CLI

**AI suggested:** either a command-line tool or a PyQt desktop app.

**Decided:** FastAPI serving a browser UI on `127.0.0.1`.

**Why:** the user is a shop-floor operator, the UI is explicitly scored, and
the brief forbids network access, not localhost. The browser gives a zoomable
annotation canvas, drag-and-drop, and a printable report for free, with no
build step and nothing to install past `pip`. The offline requirement is met
concretely: no CDN script, no web font, no external stylesheet.

**Evidence:** [`ARCHITECTURE.md §3`](ARCHITECTURE.md), which also records what
that choice gives up.

---

## Decision 10: Auto-detect had to be measured too

**AI shipped:** a component-suggestion heuristic fusing colour distance and a
local-variance texture channel. It produced boxes; it looked fine.

**Decided:** score it against ground truth before trusting it.

**Why:** "it looks fine" is how Decisions 3 and 4 nearly shipped. Measured, it
covered **3 of 24 parts**. The texture channel was firing on the white
silkscreen outlines printed *around* every component, welding neighbours into
blobs.

**Decided instead:** drop texture entirely and separate parts morphologically:
silkscreen and traces are thin, component bodies are not, so an opening sized
to the board deletes the connective tissue.

**Evidence:** same board, **24 of 24**.

---

## Decision 11, A harder board, and the bug it exposed

**AI suggested**, when asked to make the sample board more complete: add more
components and move on.

**Decided:** add them, then *re-run the benchmark before believing anything*,
and treat any drop as a finding, not as noise.

**Why:** the first board had 24 parts of fairly similar character. The new one
has 41 and is deliberately harder in three specific ways: a 7x range of part
sizes, a colour range that now includes a bright metal can and a white
connector rather than only dark parts on green, and rows of near-identical
passives so template matching cannot rely on a part being locally unique.

The score dropped immediately, and the diagnostic said something precise: a
small IC that had been **removed outright** still scored 0.74 correlation and
0.80 edge agreement. The two populations had started to overlap, so no
threshold could separate them.

**The cause:** an operator's box encloses the component *and* a ring of its
footprint: solder pads, silkscreen outline, via fanout. That ring is
identical whether the part is fitted or not. On a large QFP it is a thin
border; on a small IC it is most of the box, and it was drowning the signal in
pixels that cannot carry the answer.

**The fix** is one idea, not a tuned constant: measure the *inner* region and
leave the ring to the alignment search, where its contrast is an asset rather
than a liability. Same philosophy as Decision 5: find what the measurement is
actually looking at.

**Evidence:** the weakest genuine removal went from **0.271 to 0.652** while
the worst false alarm *fell* from 0.500 to 0.373. On 3,280 part decisions the
engine now scores 100% precision and 100% recall at **all three** sensitivity
settings, on a board that is substantially harder than the one it used to
score 100% on.

---

## Decision 12, The canvas had to feel like a tool people know

**AI built:** a functional annotation canvas: wheel to zoom, space-drag to
pan, one box selected at a time.

**Decided:** that is "it works", not "it is good". Rebuild it against the
conventions of Figma and every map on the web.

**Why:** at the live demo an unfamiliar person annotates an unfamiliar board
while being watched. Every interaction they have to *discover* is a moment
where the tool, not the board, is the problem. Scroll-wheel-zoom was the worst
offender: on a trackpad, two-finger scrolling is pan everywhere else, so the
original behaviour made the board leap at anyone who touched it.

**What changed:** scroll pans and `Ctrl`+scroll pinch-zooms at the pointer;
draw and select tools on `R`/`V`; marquee selection; multi-select with
`Shift`-click; `Alt`-drag to duplicate; alignment guides that snap a box to
its neighbours' edges and centres; eased transitions for fit and
zoom-to-selection, so the board never teleports.

**Evidence:** verified by driving the canvas with synthetic pointer events:
plain scroll leaves the zoom level untouched, `Ctrl`+scroll changes it, a
marquee across two parts selects exactly two.

---

## Decision 13, A photo of half the board must not invent the other half

**Asked for:** should a close-up of part of the board still work?

**AI's first behaviour:** it "worked" in the worst way. The homography was
fine, but everything outside the photo was filled by edge-pixel replication,
so parts that were never photographed were measured against smeared pixels and
could be called missing. A confident verdict on evidence that does not exist.

**Decided:** separate *aligned* from *observed*. Warping a solid white
rectangle through the same homography says exactly which reference pixels the
photo covers. A part covered less than 70% is reported **Not in view** -- grey,
excluded from the verdict, counted in the summary -- and the headline becomes
"OK for 10 of 41 parts" rather than a bare "OK".

**A second bug fell out of it.** Partial photos were being letterboxed onto a
reference-sized black canvas. Registration runs CLAHE, CLAHE turns the sensor
noise in flat black into texture, and the detector then tried to match it.
Alignment quality on a 60% crop sat at 0.65 and the reported coverage was off
by a third. Handing the photo over untouched -- SIFT is scale-invariant and
RANSAC does not care about image bounds -- took quality to **0.89** and made
coverage exact.

**Evidence:** on a 60% crop the three genuinely missing parts were still found,
33 of 41 checked. On a 50% crop containing none of them: zero false alarms, 31
parts correctly reported as not in view.

---

## Decision 14: Delete the tool palette

**AI built:** draw and select tools with a toggle, copying Figma.

**Decided:** remove it. Dragging on empty board always draws a part; `Shift`
-drag marquee-selects.

**Why:** Figma needs a palette because it draws a dozen kinds of object. Here
there is exactly one, so the toggle added a mode the user can leave in the
wrong state -- and the person using it at the demo has never seen the app. A
mode that silently changes what dragging does is a trap, not a feature.
Double-clicking empty board now fits the whole board, because getting lost
while zoomed in is the easiest way to feel stuck.

**Two real bugs surfaced while testing the simplification:** a plain click on
the board created a 6x6 part (the minimum-size clamp ran *before* the
is-this-a-drag check, so every stray click added an invisible part), and the
name list recursed into itself until the stack blew -- focusing a name field
selected its part, selecting re-rendered the list, and re-rendering restored
focus. Both were pre-existing and only became reachable once naming got
faster.

---

## Decision 15: an offline LLM for naming, and where to fence it

**Asked for:** could a local Ollama read the silkscreen and name the parts
automatically, with manual naming as a fallback?

**My reading of the rules, which is the actual decision:** the brief says AI
tools "may be used only to create practice data, never inside the solution".
A local Ollama does not break the no-network rule, since it is loopback only.
It does sit inside the running solution, and that sentence is hard to read any
other way. So the feature exists, because it genuinely saves the slowest step,
but it is built so that no grader has to take my word for anything:

- off unless a local Ollama answers, and invisible when it does not;
- it suggests a *name* and nothing else, with no path at all into the
  present/missing decision;
- its answers are parsed against the shape a real designator has, so the
  chatty and the uncertain are discarded rather than written into the board;
- deleting `app/naming.py` and one button removes it entirely;
- every benchmark number in the README was measured with it off.

**Why not trust it further:** a 7B vision model reading 8 px of silkscreen will
produce confident, wrong designators, and a wrong name that *looks* right is
worse than `Part 7`, which anyone can see needs fixing. Letting it near the
verdict would also destroy the thing that makes this project defensible, that
every call decomposes into six measured numbers.

**The related question, "can it tell me whether the golden board itself is
complete", has no answer and should not be given one.** The golden board is
the definition of complete; there is nothing to compare it against. A model
asked that will guess, confidently. The honest version of that feature is
classical, detecting footprints with no component on them, and it is not built
yet.

**Evidence, and it is not flattering.** Installed Ollama and ran
`qwen2.5vl:3b` over all 41 parts: **30 correct, 8 confidently wrong, 3
declined, at 48 s per part**. Seven of the eight wrong answers were the
designator of a *different real component* on the same board. It reads the
neighbour's label.

An earlier 8-part sample had read 7 of 8 and I nearly reported that. It was
misleading: those eight were isolated parts along the top edge. The full run
is the honest number.

That failure pattern is at least exploitable. Two parts cannot share a
designator, so when several claim the same one, all the claimants are refused.
My first attempt rejected duplicates *as they arrived*, which was wrong in an
instructive way: the mistaken answer usually lands before the real part's
turn, so it rejected the correct name and left the wrong one. Measured, it
dropped correct answers from 30 to 26 and removed **zero** wrong ones.
Resolving conflicts after collecting everything halves the wrong answers,
8 to 4, at the cost of 4 correct ones. That is the right trade: a declined
part visibly still needs naming, a wrong one looks finished.

**And the conclusion the numbers force:** one in ten names still wrong, 33
minutes per board, and you must check all 41 anyway. Typing them with Enter
takes a couple of minutes. The feature ships off by default with this measured
table in the README rather than a claim that it works.

---

## Decision 16: em dashes out, by hand where the script was wrong

**Asked for:** remove em dashes everywhere, they look ugly.

**AI's first attempt:** one regex over each whole file. It replaced 122 dashes
and quietly broke things: the pattern captured a single character after the
dash rather than the following word, so almost every dash became a colon
including the ones that wanted a comma; and because it matched across
newlines, dashes at the end of a wrapped line swallowed the break and welded
two lines together, markdown `>` quote prefixes and all.

**Decided:** reverted it, then rewrote it to work line by line, choosing a
comma or a colon from the actual next word, and asserting that the line count
is unchanged before writing each file.

**Then read the diff**, which is the part a script cannot do. Four needed a
human: a troubleshooting list that reads as definitions wanted colons rather
than commas, and one dash was not prose at all but the "no value" placeholder
in `formatBytes`, which the script turned into a literal `": "` on screen.

**Also left alone deliberately:** `ai-chat-history/`. It is a record of what
was said; editing it to look tidier would make it a worse record.

---

## Decision 17: a button that promised what it never did

**Reported:** "auto detect hone per parts add ho rahe jabki already hai" and
"auto name to name detect hi nhi kr raha". Two bugs, and the second is the
more interesting one.

**Auto-detect was stateless and the screen did not compensate.** It looks at
the board, not at what is already on it, so it returns the same forty
components every single run. The screen added them blindly, so a second press
silently doubled the list and a few presses buried the board under hundreds of
stacked boxes, numbered into the 280s. Fixed by skipping any suggestion that
overlaps an existing box, which also makes it safe to run again after drawing
a few by hand: delete two boxes, press it, and exactly those two come back.

**"Auto-name" never named anything.** It renumbered boxes in reading order:
Part 1, Part 2, Part 3. That is a useful thing to have, but the label claimed
something else entirely, and a user who presses a button called Auto-name is
entitled to expect it read the designators printed on the board.

Nobody wrote a wrong line of code there. The function does exactly what its
name inside the codebase says, `autoName`, and I carried that name onto a
button without asking what a person would read it as. It is the cheapest kind
of defect to ship and one of the more expensive to be caught on in a demo,
because it does not look like a bug, it looks like a lie.

**Decided:** the button now says **Renumber**, its tooltip says it does not
read the printed designators, and pressing it says so again in plain words.
The only thing in this project that reads real printed names is the optional
label reader, and that one publishes its error rate (Decision 18).

---

## Decision 18: the reader was never the problem

**Reported:** "33 minutes too much i thought bs ek click mai kaam ho jayega",
then "aur raste try kro", with the constraint restated: it has to run offline,
with no internet and no API keys.

Fair. Thirty-three minutes for thirty right answers and eight wrong ones is
not a feature, it is a thing you switch on once and never again.

**What I had already concluded, and what I had missed.** I had measured OCR
against the vision model, found it worse, removed it, and written up "typing
is better" as the honest conclusion. That write-up contained the answer and I
walked past it: *seven of the model's eight wrong answers were a neighbouring
component's designator*. Every approach I had tried, the model included, read
the text more or less correctly and then attached it to the wrong part. I had
been treating a data-association problem as a character-recognition problem,
so every fix I reached for, bigger crops, more upscaling, a stronger model,
was aimed at the half that already worked.

**Three ideas tested and rejected, before the one that worked.** Isolating
the silkscreen by colour so the reader sees clean text (17/41: the glyphs are
anti-aliased into the board, so the threshold eats them). Accepting a reading
only when its letter prefix matches the component's visual type (no new
correct answers; it converts wrong answers into blanks, which is worth having
but is not an improvement in reading). Stripping long bright structures so an
IC's pins stop merging with the designator printed across them (deletes the
text with the pins, because at 8 px they are one connected blob). Each one is
in the README, because the negative results are what justify the shape of
what replaced them.

**What worked was geometry, not recognition.** A CAD tool prints every
designator at the same offset from its component. That offset is recoverable
from the board itself: survey the board once, pair every text found with every
part near it, and let the pairings vote. The wrong pairs disagree with each
other; the right ones agree. Twenty-three of them agreed on a single offset,
and from there each label's position is known, so the reader gets the narrow
strip the text actually occupies instead of a crop containing a whole
component.

| | Correct | Wrong | Time |
|---|---|---|---|
| Vision model | 30 / 41 | 8 | 33 min |
| OCR, per-part crops | 22 / 41 | 14 | 40 s |
| OCR + learned convention | **32 / 41** | **0** | **36 s** |

Fifty-five times faster than the model, two more right, and the eight wrong
answers became zero, verified on a second independently rendered board and
then end to end through the running app.

**The number I care about is the zero.** A blank costs the operator a few
seconds of typing. A confidently wrong designator looks completely plausible,
mislabels the report, and is caught only by checking every name against the
board, which is the entire job you were trying to avoid. An assistant that
declines is useful; one that is quietly wrong one time in five is worse than
no assistant at all.

**Then I went after the last nine, and did not get them.** Asked to try, I
spent another round on the labels the reader leaves blank: a taller strip
choosing by distance in both axes, a tighter one to exclude the pins, eroding
the bright mask to find solid blocks and inpainting the pins away, and reading
each strip at three sizes to keep only readings that agree. Every one was
measured on both boards and every one was neutral or worse, the tight crop
notably so: it starts reading `R7` as `F7`, which is the failure mode worth
least.

The diagnosis is the useful part. I had assumed the chips failed because
their designators are printed over a pin row, and that was wrong twice over.
First, the labels are where the convention predicts, within a pixel, for the
parts that fail exactly as for the parts that work. Second, asking the reader
what it sees gives `J2` as `2`, `R7` as `7`, `Q1` as `01`, `U5` as `UG`, and
a chip's pins as a Chinese character. These are character-level failures of
an 8 px single-stroke font read by a model trained mainly on Chinese, not a
placement or clutter problem, so no amount of cropping was ever going to
help. I had been fixing the wrong thing again, which is the same mistake as
earlier in this entry, caught two rounds faster.

What is left would be guessing: inferring the letter from the component's
appearance, or completing the numbering from its neighbours. Both work on
this board and both are inventions, and an invented designator is
indistinguishable from a read one by the time it reaches the report. So the
nine stay blank, the attempts are in the README, and the shipped number is
still 32 of 41 with none wrong.

**Asked for accuracy over coverage, I went looking for the hole.** The
instruction was to keep what the reader gets right and never hand back
anything wrong, so I stopped trying to read more and audited what could let a
wrong name through. One real hole: the reading nearest the predicted label
position was accepted no matter how far away it actually was. On this board
the strips are narrow enough that it never bit, but a board with tighter
silkscreen would have handed a part its neighbour's designator. Measured the
spread over 122 correct readings, found the worst at 0.23 text heights, and
put the limit at 1.5.

The two guards that look obvious are both measured and both absent. A
confidence floor does not separate anything here: the wrong reading available
to test against scored 0.83 while correct ones go down to 0.69, so any floor
that catches it discards a pile of right answers in exchange for the
appearance of safety. Reading each strip at three sizes and keeping only
readings that agree is worse than useless: tested against the configuration
that does produce wrong answers, it removed none of them and added one. The
errors are systematic, not random. An 8 px `R` and an 8 px `F` differ by two
pixels and the reader is wrong about them the same way every time, so a check
that fails identically to the thing it is checking is not a check. That is
the part I would have got wrong by reasoning alone, because re-reading
*feels* like verification.

**Where I made myself less comfortable on purpose.** The obvious move was to
turn this on by default, since it needs no install, no server and no key.
I did not, because this project had already read the brief's "AI tools ...
never inside the solution" conservatively and fenced the vision model behind a
flag. An ONNX text recogniser is arguably not "an AI tool like Gemini" in the
way a barcode scanner is not. It is still a convolutional network in a file,
and reading the same sentence strictly for a model I was unimpressed by and
loosely for one I had just got working would be convenience dressed up as a
principle. Same switch, now with a readable name: `python run.py
--read-labels`. The cost is one flag; what it buys is that "out of the box
this app runs no model of any kind" is still a true sentence.

---

## Decision 19: the benchmark was only testing the easy case

**Asked for:** renaming the board did nothing when clicked, and "photos dark
hona, blur hona, different angle se lena ... we want reliable results".

The rename was a five-minute bug with a wider lesson: it called
`window.prompt`, which several embedded webviews disable and return null from
without showing anything. Clicking did nothing, silently, with no error. It
now uses the app's own dialog, like everything else does.

The second half was the real work, and the first thing to admit is that I
could not answer the question. I had said a few days of work earlier that the
engine handles dim and harsh lighting, because the benchmark contained boards
labelled dim and harsh. What the benchmark actually contained was a camera
bolted to a bench: a nudge of a degree, exposure drift, mild defocus, JPEG.
There was no perspective in it at all. Somebody holding a phone over a board
was simply not represented, so "does it handle a different angle" had no
measured answer and I should not have implied otherwise.

**So the first change was to the benchmark, not the engine.** Added: held at
an angle, underexposed, somebody leaning over the board, a flash off the
solder mask, focus missed. The existing boards regenerate byte for byte, so
every earlier number still stands.

The engine scored **49.8% precision** on them. Recall stayed at 100%.

**Then I guessed wrong three times, and measuring stopped it each time.**

I assumed the angle was the problem, since it was the new thing. Probed the
alignment: SIFT recovered the tilt on every board, nothing rejected,
perspective terms a factor of ten inside the envelope. Not the angle.

I assumed glare was destroying the parts underneath it, and built a rule to
send those parts for review instead of calling them missing. Measured the
saturation: zero, everywhere. Then measured which parts the false alarms
actually were: *not the ones under the highlight*. The rule I had just
written was aimed at the wrong parts, and worse, it suppressed eleven real
removals, because lifting a dark chip off a board leaves something brighter
behind, which is exactly what my highlight detector was looking for. A
detector for reflections that fires on missing components is not a safety
feature, it is a way to miss faults. Deleted.

I assumed it was one dominant nuisance. Ablated them one at a time: angle
100%, blur 100%, dark 100%, shadow 100%, noise 100%, glare 77%. Then ran
everything *except* glare: also 77%. No single cause. The nuisances stack.

**What the measurements eventually said.** The separation was still good: a
genuinely missing part outscored every present one on 38 boards in 42. The
ordering was right and the calibration was wrong, so the answer was not a
better filter but a better line. Each photograph is now judged against its
own noise floor as well as the fixed threshold.

The part I want to keep is where that line is allowed to act. Raising it to
decide present against missing buys precision by missing real removals, and
for an inspection tool that is backwards: a false alarm costs a second look,
a missed removal ships a faulty board. So it decides *stated missing* against
*sent for review* instead. Nothing is hidden, everything the old threshold
flagged is still flagged, and what changes is whether the tool asserts it or
asks about it.

| | before | after |
|---|---|---|
| Missed removals | 0 | 0 |
| Precision of stated missing | ~50% | 88% |
| Boards exactly right | 33 / 60 | 44 / 60 |
| Bench benchmark | 80/80 | 80/80, unchanged |

It costs about a second a board, from the per-part local lighting
correction, and that is in the README too.

**What I would flag to a reviewer.** 58% of everything flagged is still
spurious on hand-held photographs; those now arrive as review items rather
than assertions, which is honest but is not the same as being right. And a
rendered model of a bad photograph is still a model. The useful claim is
narrower than "it handles bad photos": it is that the failure is understood,
that it degrades into questions rather than into wrong answers, and that it
has never yet missed a removal.

---

## What the AI was better at, and where it needed holding

**Better:** breadth and speed. Six evidence channels, a homography
plausibility envelope, EXIF-orientation handling, atomic JSON writes, a full
annotation canvas with undo, all drafted faster than I could have specified
them.

**Needed holding:** it is persuasive about work it has not verified. Every
metric above was delivered with a confident explanation of why it was the right
choice; two of them were broken, and one was *backwards*. It also reached for
threshold tuning when the honest move was a root-cause hunt (Decision 5). The
steering that mattered was never about algorithms, it was insisting that
nothing counted until it was measured, and that unexplained failures get
explained rather than tuned away.

One thing it got right unprompted and I kept: when the sharpness fix made
removals *less* obvious as well as present parts, it re-derived the thresholds
rather than quietly keeping the old ones.

---

## Chat excerpts

### Excerpt 1: setting the bar and delegating the choices

> **me:** *(with the task PDF attached)* build fully functional app, like we r
> building it for top companies & you're a top professional in this field who
> have full domain knowledge of these things. Most accurate, relailble ,
> userfriendly, with good top notch ui i hope you know what i want build it,
> you to choose everything professional from color to everything if you need
> any 3d design to look it better then create it it should very smooth

This is the whole of my technical direction. Everything below is the AI working
inside that mandate, which is exactly why an external check on its output
mattered so much.

*(`conversation.md`, turn 3)*

### Excerpt 2, the benchmark catching two broken metrics

> **Claude:** Recall is perfect but precision is bad: 14 false alarms. Let me
> see exactly which parts misfire and why.

> **Claude:** Two real bugs exposed. The HSV colour metric returns ~1.0 even
> for identical patches (hue is pure noise on desaturated parts), and Canny
> edge IoU collapses under defocus. Let me fix both.

Both metrics had been written with confident, plausible justifications. Neither
survived contact with ground truth.

*(`conversation.md`, lines 745 and 779)*

### Excerpt 3: root cause instead of threshold tuning

> **Claude:** The 10-board set was flattering me. On 80 boards precision falls
> to 82%. Let me find the actual failures.

> **Claude:** Every false alarm is the same part: R6, the small rotated chip
> resistor, and its signals barely change with severity. That points at a
> systematic cause, not noise: the reference is sharp and the test photos are
> defocused, which hurts small high-contrast parts most. Let me fix the root
> cause.

The tell was that the signals *did not vary with photo quality*. A nuisance
effect would have varied. That observation turned a threshold-tuning exercise
into the sharpness-equalisation step, and took false alarms to zero.

*(`conversation.md`, lines 1037 and 1074)*

---

## A note on scope

Two things in this repo were **not** done by the AI acting alone, and both are
flagged where they appear:

- **Archify** ([`ARCHITECTURE.md`](ARCHITECTURE.md)), the installer writes a
  skill into the agent's own skill directory, so it was blocked by this
  machine's permission policy until I approved it explicitly. The diagram is
  now genuine Archify output, with all four of its gates passing; the commands
  and the pinned, source-cited diagram spec are recorded there.
- **The benchmark is synthetic.** Stated plainly in
  [`README.md`](README.md) and [`RESEARCH.md §4`](RESEARCH.md) rather than
  buried, because the headline numbers are perfect and perfect numbers deserve
  a caveat.
