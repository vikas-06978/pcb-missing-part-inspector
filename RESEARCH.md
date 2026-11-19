# Research

What I looked at before writing code, what I compared, what I chose, and what
I deliberately rejected.

---

## 1. Reading the problem precisely

The brief looks like "PCB defect detection", which is a crowded field. It is
not that problem, and the difference decides the whole design:

| Published PCB inspection work | This brief |
|---|---|
| Finds defects in the **copper artwork** of a bare board: opens, shorts, mousebites, spurs | Finds **whole components missing** from an assembled board |
| Defect classes are fixed, universal and known in advance | The "classes" are whatever the operator names on *their* board, minutes before use |
| Trained on thousands of labelled examples | **One** reference photo, supplied at runtime by someone who has never used the app |
| Camera and board vary | A fixed camera photographs the same board the same way |

Three constraints follow, and they turned out to be the binding ones:

1. **One reference image, supplied live.** At the demo, an unseen board is
   uploaded and parts are named on the spot. Anything that needs training, or
   even a handful of examples per part, is disqualified — not slow, *impossible*.
2. **Fully offline, on a laptop.** No hosted model, no download at runtime.
3. **The fixed camera is a gift.** If the viewpoint is nearly constant, the
   reference and the test photo can be compared pixel-region to pixel-region.
   Almost all the engineering difficulty is in "nearly".

This reframes the task as **registered change detection against a golden
sample**, which is a much older and better-understood problem than
learned defect detection — and, not coincidentally, what the automated optical
inspection industry has done for decades.

---

## 2. Prior art surveyed

### Industrial practice (the strongest signal)

Commercial AOI machines (Koh Young, Omron, Saki, Mirtec) are programmed by
showing them a known-good board, having an operator mark the components, and
then comparing every subsequent board against that golden sample within each
component window. Missing-component detection specifically is usually decided
by a small set of complementary measures inside the window — presence of the
expected body colour, local contrast, and correlation against the stored
template — rather than by one clever metric.

That is almost exactly the workflow the brief describes. It is a strong signal
that golden-board comparison is the right shape of answer, and it is why the
app's three screens mirror an AOI programming flow (teach reference → define
windows → run).

### Open-source building blocks

| Tool | What it gives | Verdict |
|---|---|---|
| OpenCV `matchTemplate`, `findHomography`, `findTransformECC`, `calcHist`, `Canny`, `Sobel` ([docs](https://docs.opencv.org/)) | Everything needed for registration and per-window measurement, CPU-only | **Used.** The whole engine is built on these |
| scikit-image `structural_similarity` | A tested SSIM | **Rejected as a dependency.** SSIM is ~25 lines over `cv2.GaussianBlur`; writing it removes a dependency and let me control the window size for ROIs only ~24 px across |
| ImageMagick `compare` | Quick visual diff | **Rejected.** No registration, no per-region verdicts, nothing to explain |
| [anomalib](https://github.com/open-edge-platform/anomalib) (PatchCore, PaDiM) ([paper](https://arxiv.org/pdf/2202.08341v1.pdf), [docs](https://anomalib.readthedocs.io/en/v1/markdown/get_started/anomalib.html)) | State-of-the-art one-class anomaly detection, needs only "good" images | **Rejected**, see §3 |
| YOLO-family component detectors | Learned detection of ICs, capacitors, connectors | **Rejected**, see §3 |

### Datasets reviewed

I looked for public data with the one property this task needs — the *same
board* photographed from the *same viewpoint*, with and without a component.

| Dataset | Contents | Why it does not fit |
|---|---|---|
| [DeepPCB](https://github.com/tangsanli5201/DeepPCB) ([paper](https://arxiv.org/abs/1902.06197)) | 1,500 aligned template/test **binarised** image pairs, 6 artwork defect classes | Bare-board copper defects, not assembled boards. Closest in *structure* (it is explicitly a registered-pair dataset) and it confirmed that template/test pairing is the standard framing |
| [HRIPCB](https://arxiv.org/abs/1901.08204) (Huang & Wei, [PKU-SZ PDF](https://robotics.pkusz.edu.cn/static/papers/ACAIT2019-huangweibo1.pdf)) | 1,386 synthesised images from 10 template boards, 6 defect types | Also bare-board artwork defects. Notably the authors *synthesised* their defects onto template boards — the same decision I ended up making, for the same reason |
| [MVTec AD](https://www.mvtec.com/company/research/datasets/mvtec-ad) / [AD 2](https://www.mvtec.com/company/research/datasets/mvtec-ad-2) | 15 industrial categories, defect-free train + annotated test | Has a PCB-adjacent category but is built for *unsupervised anomaly detection on many samples*, not single-reference comparison. Licence is also non-commercial on at least one mirror |

A community mirror of PCB defect data exists at
`github.com/Ironbrotherstyle/PCB-DATASET`; I did not verify its contents or
licence and did not use it.

**Conclusion: no public dataset matches the task.** Every candidate is bare-board
artwork defects or multi-sample anomaly detection. So I generated my own —
see §4.

---

## 3. Options compared

### A. Train an object detector to recognise components (YOLO/DETR)

Detect "capacitor", "IC", "connector" on the test board; report any named part
whose class is not found at its location.

*Rejected.* It answers the wrong question. The operator does not name
*categories*, they name **C1** and **U7** — two parts that may be visually
identical. More decisively, the demo supplies an unseen board and a single
reference photo; a detector cannot be trained in the room, and one pretrained
on other boards would carry an unquantifiable domain gap onto hardware I have
never seen. It also pulls in a multi-hundred-megabyte runtime for a problem
that a fixed camera has already made easy.

### B. One-class anomaly detection (PatchCore / PaDiM via anomalib)

Fit a memory bank of "normal" patch features, flag outliers.

*Rejected, with regret* — this is the most intellectually attractive option and
genuinely state of the art. Three blockers:

1. **It needs a corpus of normal images.** PatchCore's memory bank is built
   from many defect-free samples; with exactly one reference it degenerates.
2. **It localises anomalies, it does not name parts.** The brief demands "which
   *named* parts are missing". I would still need the ROI layer on top, so the
   deep model only replaces the measurement step — the hardest part to justify.
3. **It cannot explain itself.** A heat map is not a reason. On a line, an
   operator who is told to scrap a board asks *why*, and "the network's patch
   distance exceeded a threshold" ends badly.

Worth revisiting if the deployment ever accumulates hundreds of good boards per
product. It does not fit a cold start.

### C. Registration + per-ROI multi-signal comparison — **chosen**

Align the test photo to the reference, normalise the nuisances, then measure
several independent, deliberately redundant signals inside each named box and
fuse them.

Chosen because it is the only option that:

- works from **exactly one** reference image, with zero training;
- is **set up in the room** in the time it takes to draw boxes;
- is **explainable** — every verdict decomposes into named measurements the
  operator can see, with the two image crops side by side;
- runs **offline on a CPU** in well under a second per board;
- **degrades honestly** — when registration is poor it says so, rather than
  emitting a confident wall of false positives.

### D. Plain absolute difference of aligned images

*Rejected.* Tried first, in the evaluation harness, because it is the obvious
baseline. It fails the moment anything about the photograph changes: exposure,
white balance, focus, or a two-pixel shift. Raw residual survives in the final
design only as a low-weight tie-breaker, and §5 explains why its weight is low.

### E. Template matching alone (NCC per ROI)

*Rejected as a sole signal*, kept as the strongest single channel. Correlation
is beautifully invariant to brightness, but on a bare footprint the remaining
solder pads often preserve enough of the component's rectangular silhouette to
score 0.5–0.6 — too high to threshold safely on its own. It needs corroboration
from colour and outline, which is the argument for fusion.

---

## 4. Data strategy

No public set fits, and the brief permits generating data. Options were:

1. **Photograph a real board** and remove parts. Most faithful, but I do not
   have a board I am willing to desolder, and it yields a handful of images
   with no ground truth beyond my notes.
2. **Generate images with an AI image tool** (explicitly allowed for practice
   data). Rejected: a diffusion model will not hold a fixed viewpoint and
   identical board layout across a set while changing exactly one component —
   and viewpoint consistency is the single property the task depends on.
3. **Render the boards procedurally** with OpenCV — chosen.

Rendering gives exact ground truth for free, a *controllable nuisance model*
(so I can measure where the engine breaks instead of guessing), and a
reproducible benchmark that ships in the repo and needs no download. The
nuisance model covers what actually varies on a line: sub-degree rotation,
few-pixel translation, exposure and white-balance drift, vignetting, a diagonal
light gradient, defocus, sensor noise and JPEG compression.

**The honest limitation:** synthetic boards are cleaner than real photographs.
Real solder joints glint, real components sit at slight angles, real boards
have conformal coating and dust. The numbers in the README should be read as
evidence that the *design* is sound and the thresholds are not knife-edge —
not as a prediction of field accuracy. That is also why sensitivity is an
operator-facing control rather than a constant.

---

## 5. Technique choices within the chosen approach

| Decision | Alternatives considered | Why |
|---|---|---|
| **SIFT + RANSAC homography**, ECC fallback, identity last | Phase correlation; ECC alone; assume no motion | Phase correlation handles translation only. ECC is robust but slow and needs a decent starting guess. Features give rotation and scale too; ECC covers the low-texture boards where features fail. The brief's fixed camera makes identity *almost* right, which is exactly the trap — "almost" is what produces false positives |
| **Validate the homography** against a plausibility envelope | Trust RANSAC's inlier count | Solder mask is covered in near-identical repeated corners. RANSAC will happily return a confident, wrong fit. A scale/rotation/shear/perspective sanity check rejects transforms that are not a plausible camera nudge |
| **Quality = post-warp board correlation**, not inlier ratio | Inlier ratio | The inlier ratio measures the fit's self-consistency. What the pipeline depends on is whether the boards actually overlap afterwards. Measure the thing you depend on |
| **Global robust photometric match** (median/MAD per channel) | Local/per-ROI normalisation; histogram matching | A local correction would cheerfully normalise away the very patch where the component went missing. Global + robust statistics cannot |
| **Sharpness equalisation** | Ignore focus; use blur-invariant features only | Comparing a sharp reference against a soft line photo inflates every difference, worst on small parts ringed by bright pads. This single effect produced *every* false alarm in one benchmark run. A comparison may only use information both images contain |
| **CIELAB chroma histograms** | HSV hue/saturation | Hue is undefined for near-neutral colours and a PCB is mostly neutral — black IC bodies, white connectors, grey pads. The HSV version returned ~1.0 for *identical* patches. Lab chroma degrades gracefully; neutrals simply cluster near the origin |
| **Gradient-magnitude correlation** | Dilated Canny edge IoU | Blur pushes gradients below the Canny threshold, the test edge map empties out, and a present part reads as absent. Correlation of gradient magnitudes is invariant to the uniform amplitude loss blur causes |
| **Weighted fusion + presence vetoes** | Single metric; voting; logistic regression | Weights are set by measured discriminating power on the benchmark. Vetoes exist because false alarms and missed removals are not symmetric: a tool that cries wolf gets ignored, and an ignored tool catches nothing. A learned combiner would need labelled data I do not have at setup time |
| **Capped scene compensation** | None; uncapped robust z-scores | If *every* ROI looks slightly wrong, the photograph changed, not forty components. But uncapped population normalisation would hide a genuinely stripped board, so the correction is capped and reported |

---

## 6. Architecture-diagram tooling

The brief specifies [Archify](https://github.com/tt-a1i/archify) for
`ARCHITECTURE.md`. See that file for what was produced and the one step that
needs the repository owner's approval.

---

## 7. What I would do next

- **Real photographs.** Everything above is validated on rendered boards. The
  first field data would likely move thresholds and might add a signal
  (specular highlight presence is a strong missing-part cue on real solder).
- **Per-part thresholds.** A 170 px QFP and a 20 px chip resistor currently
  share one threshold. The benchmark shows small parts are the harder case, so
  scaling the threshold with ROI size is the obvious next refinement.
- **Wrong-part and misplacement detection.** The engine already measures enough
  to flag "something is there, but it is not what was there before"; the brief
  only asks for missing, so that is reported as *needs review* rather than as
  its own verdict.
- **Calibration from the line.** Every inspection produces confidences. After a
  few hundred boards, the operating point could be chosen from the plant's own
  data instead of my synthetic benchmark.

---

## Sources

- DeepPCB dataset — <https://github.com/tangsanli5201/DeepPCB>
- Tang et al., *Online PCB Defect Detector On a New PCB Defect Dataset* — <https://arxiv.org/abs/1902.06197>
- Huang & Wei, *A PCB Dataset for Defects Detection and Classification* (HRIPCB) — <https://arxiv.org/abs/1901.08204>, journal version <https://robotics.pkusz.edu.cn/static/papers/ACAIT2019-huangweibo1.pdf>
- MVTec AD — <https://www.mvtec.com/company/research/datasets/mvtec-ad> and MVTec AD 2 — <https://www.mvtec.com/company/research/datasets/mvtec-ad-2>
- anomalib — <https://github.com/open-edge-platform/anomalib>, paper <https://arxiv.org/pdf/2202.08341v1.pdf>, docs <https://anomalib.readthedocs.io/en/v1/markdown/get_started/anomalib.html>
- OpenCV documentation — <https://docs.opencv.org/>
- Archify — <https://github.com/tt-a1i/archify>
- Wang, Bovik, Sheikh & Simoncelli, *Image Quality Assessment: From Error Visibility to Structural Similarity*, IEEE Transactions on Image Processing 13(4), 2004 — the SSIM formulation implemented in `app/vision/metrics.py`
