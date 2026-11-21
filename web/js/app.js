/**
 * Application shell: wiring, navigation and state.
 *
 * Three screens in a fixed order, because the task itself is ordered -- you
 * cannot name parts without a reference, or inspect without named parts. The
 * stepper disables what is not yet possible rather than letting someone reach
 * a screen that will only tell them off.
 */

import { api, ApiError } from './api.js';
import {
  toast, confirmAction, promptText, loadImage, imageFilesFrom, downloadBlob, formatBytes,
} from './ui.js';
import { Annotator, isPlaceholder } from './annotator.js';
import { PartView } from './partview.js';
import { Review } from './review.js';
import {
  DetailView, renderBatchSummary, renderCards, buildReport,
} from './results.js';
import {
  WELCOME_STEPS, markTourSeen, runTour, showHelpStrip, tourSeen,
} from './tour.js';

const $ = (selector) => document.querySelector(selector);

const state = {
  config: null,
  project: null,
  step: 'reference',
  results: [],
  annotator: null,
  detail: null,
  saveTimer: null,
  dirty: false,
};

/* =========================================================================
   Theme
   ========================================================================= */

function initTheme() {
  // Light by default rather than following the OS. This is shown on a
  // projector and over screen share, where dark loses contrast; someone who
  // wants dark chooses it once and it is remembered.
  const stored = localStorage.getItem('pcb-theme');
  document.documentElement.dataset.theme = stored || 'light';

  $('#theme-toggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
    document.documentElement.dataset.theme = next;
    localStorage.setItem('pcb-theme', next);
    // Canvas colours are sampled from CSS variables, so both canvases have to
    // be repainted after a theme change.
    state.annotator?.requestDraw();
    state.detail?.draw();
  });
}

/* =========================================================================
   Navigation
   ========================================================================= */

function canEnter(step) {
  if (step === 'parts') return Boolean(state.project?.reference);
  if (step === 'inspect') return Boolean(state.project?.reference) && state.project.parts.length > 0;
  return true;
}

function goTo(step) {
  if (!canEnter(step)) return;
  state.step = step;

  for (const panel of document.querySelectorAll('.panel')) {
    panel.classList.toggle('is-hidden', panel.dataset.step !== step);
  }

  const order = ['reference', 'parts', 'inspect'];
  for (const button of document.querySelectorAll('.stepper__item')) {
    const target = button.dataset.step;
    button.classList.toggle('is-active', target === step);
    button.classList.toggle(
      'is-done',
      order.indexOf(target) < order.indexOf(step) && canEnter(target),
    );
    button.disabled = !canEnter(target);
  }

  if (step === 'parts') {
    // The canvas was display:none until now, so it has no size yet.
    requestAnimationFrame(() => {
      state.annotator?.view.resize();
      state.annotator?.view.fit();
    });
    // Explained in place rather than in a modal: a dialog in the middle of the
    // annotation screen would cover the thing it is describing.
    refreshNamingButton();
    showHelpStrip(
      $('#step-parts'),
      'canvas',
      '<b>Drag a box</b> around a component, then type its name on the right. '
      + '<b>Scroll</b> to move around and <b>Ctrl</b>+scroll to zoom, exactly '
      + 'like a design tool. Boxes snap to each other as you drag, and '
      + '<b>Ctrl</b>+<b>Z</b> undoes anything.',
    );
  }
}

/* =========================================================================
   Project
   ========================================================================= */

async function refreshProject(project) {
  state.project = project;

  $('#project-name').textContent = project.name;

  const hasReference = Boolean(project.reference);
  $('#reference-drop').classList.toggle('is-hidden', hasReference);
  $('.card--guide').classList.toggle('is-hidden', hasReference);
  $('#reference-preview').classList.toggle('is-hidden', !hasReference);

  if (hasReference) {
    const url = project.reference_url;
    $('#reference-img').src = url;
    $('#reference-filename').textContent = project.reference.original_filename || 'reference.png';
    $('#reference-size').textContent =
      `${project.reference.width} × ${project.reference.height} px`;
    $('#reference-parts').textContent = String(project.parts.length);

    const image = await loadImage(url);
    if (!state.annotator) createAnnotator();
    state.annotator.setImage(image);
    state.annotator.setParts(project.parts);
    renderPartList();
  }

  $('#sensitivity').value = project.sensitivity;
  updateStatus();
  goTo(state.step);
}

/* =========================================================================
   Reference upload
   ========================================================================= */

async function uploadReference(file) {
  if (state.project?.reference && state.project.parts.length) {
    const ok = await confirmAction({
      title: 'Replace the reference photo?',
      body: `The ${state.project.parts.length} parts you named belong to the current `
          + 'photo and will be removed, along with the inspection history for this board.',
      confirmLabel: 'Replace',
      danger: true,
    });
    if (!ok) return;
  }

  try {
    toast('Loading reference…', file.name);
    const project = await api.uploadReference(file);
    await refreshProject(project);
    state.results = [];
    $('#results').innerHTML = '';
    refreshDropzoneSize();
    $('#batch-summary').classList.add('is-hidden');
    $('#inspect-empty').classList.remove('is-hidden');
    $('#export-report').disabled = true;
    toast('Reference loaded', 'Now draw a box around each part you want checked.', 'success');
    goTo('parts');
  } catch (error) {
    toast('Could not load that photo', error.message, 'error');
  }
}

/* =========================================================================
   Parts editor
   ========================================================================= */

function createAnnotator() {
  state.annotator = new Annotator({
    canvas: $('#annotator'),
    host: $('#canvas-host'),
    emptyHint: $('#canvas-empty'),
    onChange: () => {
      renderPartList();
      scheduleSave();
      updateZoomReadout();
    },
    onSelect: () => renderPartList(),
    onRenameRequest: (id, fromRect) => openPartView(id, fromRect),
  });
  state.annotator.isActive = () => state.step === 'parts';
  state.annotator.view.onChange = () => {
    state.annotator.requestDraw();
    updateZoomReadout();
  };
}

/**
 * Show the label-reading button only if something can actually serve it.
 *
 * Two routes can: an offline text reader that ships with the app, and the
 * optional local vision model. The server picks between them and names the
 * one it chose, which is worth keeping hold of because they are not equally
 * trustworthy and the wording at the end of a run says so.
 *
 * Checked when the Parts screen opens rather than at startup, so installing
 * either one later and coming back is enough; and failing silently, because a
 * missing optional tool is not an error the operator needs to hear about.
 */
async function refreshNamingButton() {
  const button = $('#read-names');
  try {
    const status = await api.namingStatus();
    state.namingRoute = status.route || '';
    button.classList.toggle('is-hidden', !status.available);
    if (status.available) button.title = `Read printed designators using ${status.model}`;
  } catch {
    state.namingRoute = '';
    button.classList.add('is-hidden');
  }
  balanceSideActions();
}

/**
 * Let the last action span the row when there is an odd number of them.
 *
 * The panel's buttons sit in a two-column grid. CSS can spot an odd count on
 * its own with :nth-of-type, but that counts elements the browser is not
 * drawing, and the label reader's button is hidden whenever it is not
 * installed. Counting what is actually visible is the only version that is
 * right in both cases, and a hole in the corner of a four-button block is
 * exactly the sort of thing that makes a panel look unfinished.
 */
function balanceSideActions() {
  const actions = $('#review-names')?.parentElement;
  if (!actions) return;
  const visible = [...actions.children].filter((b) => !b.classList.contains('is-hidden'));
  actions.classList.toggle('is-odd', visible.length % 2 === 1);
}

/**
 * Shrink the drop target once there is something to look at.
 *
 * Called wherever the result list changes, so the Inspect screen gives its
 * height to the boards rather than to an empty rectangle.
 */
function refreshDropzoneSize() {
  const zone = $('#test-drop');
  if (!zone) return;
  zone.classList.toggle('dropzone--slim', state.results.length > 0);
}

/**
 * Open the name review, creating it the first time it is asked for.
 *
 * Built lazily for the same reason the part viewer is: a board that is only
 * being inspected, with its parts already named, never needs it.
 */
function openReview() {
  const annotator = state.annotator;
  if (!annotator?.image || !annotator.count) return;
  if (!state.review) {
    state.review = new Review({
      root: $('#review'),
      onRequestParts: () => state.annotator.getParts(),
      onRename: (id, name) => state.annotator.renamePart(id, name),
      onOpenPart: (id) => {
        state.review.close();
        state.annotator.focusPart(id);
        openPartView(id, null);
      },
    });
  }
  state.review.setImage(annotator.image);
  state.review.open();
}

/**
 * Open the part inspector, creating it the first time it is needed.
 *
 * Kept out of boot so a board that is only being inspected never builds it.
 */
function openPartView(partId, fromRect) {
  if (!state.annotator?.image) return;
  if (!state.partView) {
    state.partView = new PartView({
      root: $('#partview'),
      onRename: (id, name) => state.annotator.renamePart(id, name),
      onRequestParts: () => state.annotator.getParts(),
      onFocusPart: (id) => state.annotator.focusPart(id),
    });
  }
  state.partView.setImage(state.annotator.image);
  state.partView.open(partId, fromRect);
}

/**
 * Keep the status bar honest about what the app is working on.
 *
 * Called whenever the project changes rather than on a timer, so it never
 * shows a state the app has already left.
 */
function updateStatus(engine) {
  const project = state.project;
  const led = $('#status-led');

  if (engine) {
    $('#status-engine').textContent = engine;
    led.className = 'led led--busy';
  } else {
    $('#status-engine').textContent = 'Engine ready';
    led.className = 'led led--ok';
  }

  $('#status-board').textContent = project?.reference
    ? `${project.reference.width}x${project.reference.height}`
    : 'none loaded';
  // The canvas is the truth here, not the last saved copy. Parts are drawn
  // immediately and saved a moment later, so reading the project would show a
  // stale count for that moment, and auto-detect made it show zero right
  // after adding forty boxes.
  $('#status-parts').textContent =
    String(state.annotator?.count ?? project?.parts?.length ?? 0);

  const preset = state.config?.sensitivity_presets
    ?.find((p) => p.key === (project?.sensitivity || state.config?.default_sensitivity));
  $('#status-sensitivity').textContent = preset
    ? `${preset.label} (${preset.missing_threshold.toFixed(2)})`
    : '-';
}

function updateZoomReadout() {
  if (!state.annotator) return;
  $('#zoom-readout').textContent = `${Math.round(state.annotator.view.scale * 100)}%`;
}

let renderingPartList = false;

//: Set when Enter should walk to the next name field. The rebuild honours it
//: instead of restoring focus to the row just left, which is deterministic --
//: the previous version raced a requestAnimationFrame against the rebuild's
//: own focus restoration and usually lost.
let pendingFocusIndex = null;

function renderPartList() {
  const annotator = state.annotator;
  if (!annotator) return;

  // Re-entrancy guard. Rebuilding this list restores focus to the row that
  // had it, focusing a name field selects its part, and selecting re-renders
  // the list -- a cycle that blew the stack the moment Enter started walking
  // between fields.
  if (renderingPartList) return;
  renderingPartList = true;
  try {
    renderPartListInner(annotator);
  } finally {
    renderingPartList = false;
  }
}

function renderPartListInner(annotator) {

  const parts = annotator.getParts();
  const list = $('#part-list');

  // Naming is the long part of the job and it has no natural end in sight:
  // forty rows all look alike, and "Part 31" reads like a name until you
  // notice it is not. So the count of what is left is shown while you work,
  // and every row still carrying a generated name is marked as one.
  const unnamed = parts.filter((p) => isPlaceholder(p.name)).length;
  const remaining = $('#parts-remaining');
  remaining.textContent = unnamed ? `${unnamed} to name` : 'All named';
  remaining.classList.toggle('is-done', unnamed === 0 && parts.length > 0);
  remaining.classList.toggle('is-hidden', parts.length === 0);
  $('#review-names').disabled = parts.length === 0;

  $('#parts-count').textContent = String(parts.length);
  $('#parts-empty').classList.toggle('is-hidden', parts.length > 0);
  $('#reference-parts').textContent = String(parts.length);
  $('#status-parts').textContent = String(parts.length);
  $('#to-inspect').disabled = parts.length === 0;

  const active = document.activeElement;
  const activeId = active?.closest?.('.part-row')?.dataset.id;
  const caret = active instanceof HTMLInputElement ? active.selectionStart : null;

  list.innerHTML = '';
  parts.forEach((part, index) => {
    const row = document.createElement('li');
    row.className = 'part-row';
    row.dataset.id = part.id;
    if (annotator.isSelected(part.id)) row.classList.add('is-selected');
    if (isPlaceholder(part.name)) row.classList.add('is-unnamed');

    row.innerHTML = `
      <span class="part-row__index">${index + 1}</span>
      <input class="part-row__name" value="" maxlength="40" spellcheck="false"
             aria-label="Part name">
      <span class="part-row__dims">${part.bbox[2]}×${part.bbox[3]}</span>
      <button class="part-row__del" type="button" aria-label="Delete part">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>
      </button>`;

    const input = row.querySelector('.part-row__name');
    input.value = part.name;
    // Set as a property, never interpolated into the template above. A part
    // name is the one piece of this screen somebody else can choose: project
    // files are made to be exchanged, and a name like
    //   " onmouseover="…
    // dropped into a title attribute escapes it and runs. Assigning the
    // property hands the string to the DOM as text and cannot do that.
    row.querySelector('.part-row__del').title = `Delete ${part.name}`;

    input.addEventListener('focus', () => {
      // Only act on a real change of selection, so restoring focus during a
      // rebuild does not kick off another one.
      if (annotator.selectedIds.size === 1 && annotator.isSelected(part.id)) return;
      annotator.select(part.id);
    });
    input.addEventListener('change', () => annotator.renamePart(part.id, input.value.trim()));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault();
        // Naming forty parts is the slowest thing in the whole workflow, so
        // Enter walks to the next one instead of just dismissing the field.
        pendingFocusIndex = index + 1;
        input.dispatchEvent(new Event('change', { bubbles: true }));
        if (pendingFocusIndex !== null) {
          // The name did not actually change, so no rebuild happened.
          pendingFocusIndex = null;
          const next = list.querySelectorAll('.part-row__name')[index + 1];
          if (next) { next.focus(); next.select(); } else input.blur();
        }
      }
      event.stopPropagation();      // let the editor keep Delete / arrows
    });

    row.addEventListener('click', (event) => {
      if (event.target.closest('.part-row__del')) return;
      if (event.target === input) return;
      // Shift / ctrl-click extends the selection, matching the canvas and
      // every file list the operator has ever used.
      if (event.shiftKey || event.metaKey || event.ctrlKey) {
        annotator.select(part.id, { additive: true });
        return;
      }
      // Open the part rather than just selecting it. Clicking a row used to
      // pan the canvas to the box, which still leaves you squinting at a
      // 20 px component trying to read the designator beside it. The whole
      // reason the viewer exists is that this is the moment you need it, so
      // asking for a second click on a separate button was making people
      // work for the thing the feature was for.
      annotator.focusPart(part.id);
      openPartView(part.id, row.getBoundingClientRect());
    });

    row.querySelector('.part-row__del').addEventListener('click', (event) => {
      event.stopPropagation();
      annotator.deletePart(part.id);
    });

    list.appendChild(row);
  });

  if (pendingFocusIndex !== null) {
    const target = list.querySelectorAll('.part-row__name')[pendingFocusIndex];
    pendingFocusIndex = null;
    if (target) {
      target.focus();
      target.select();
      target.closest('.part-row').scrollIntoView({ block: 'nearest' });
    } else {
      document.activeElement?.blur();
    }
  } else if (activeId) {
    const restored = list.querySelector(`.part-row[data-id="${activeId}"] .part-row__name`);
    if (restored) {
      restored.focus();
      if (caret !== null) restored.setSelectionRange(caret, caret);
    }
  }
}

function scheduleSave() {
  state.dirty = true;
  clearTimeout(state.saveTimer);
  // Autosave rather than relying on a Save button: losing forty named parts
  // because someone closed a tab is not a recoverable mistake for them.
  state.saveTimer = setTimeout(savePartsNow, 700);
}

async function savePartsNow() {
  if (!state.dirty || !state.annotator) return;
  const parts = state.annotator.getParts().map((part, index) => ({
    id: part.id,
    name: part.name.trim() || `Part ${index + 1}`,
    bbox: part.bbox,
  }));
  try {
    state.project = await api.saveParts(parts);
    state.dirty = false;
    updateStatus();
    goTo(state.step);
  } catch (error) {
    toast('Could not save the parts', error.message, 'error');
  }
}

/**
 * Read the designators printed on the board, so they are not all typed.
 *
 * Strictly a typing shortcut. It fills in names and nothing else; the
 * missing/present decision never touches it. Anything the reader is unsure
 * about is left alone for the operator, which is also what happens to every
 * part if neither reader is installed.
 */
async function readNames() {
  const button = $('#read-names');
  const annotator = state.annotator;
  if (!annotator?.count) return;

  if (state.readingNames) {
    state.readingNames = false;            // second press stops it
    return;
  }

  const parts = annotator.getParts();
  const original = button.innerHTML;
  state.readingNames = true;

  // Each suggestion is applied as it arrives, so the list visibly fills in
  // while the model works. At roughly a minute a part, a run that showed
  // nothing until the end would look like it had hung.
  //
  // Conflicts are settled afterwards. Whichever route read the board, its
  // mistakes are almost entirely neighbour confusion: labels sit between
  // components, and the wrong one gets picked up. Two parts cannot share a
  // designator, so when several claim the same one, at least one is wrong and
  // nothing says which. Every claimant is then put back to its placeholder
  // rather than guessed between. The offline reader has not produced a
  // conflict on either test board; the vision model produced several.
  //
  // Settling as they arrive does not work: the mistaken answer usually lands
  // before the real part's turn, so it rejects the correct name and keeps the
  // wrong one. Tried that; it dropped correct names without removing a single
  // wrong one.
  const suggestions = new Map();
  const placeholders = new Map(parts.map((p) => [p.id, p.name]));
  let unreadable = 0;

  try {
    for (const [index, part] of parts.entries()) {
      if (!state.readingNames) break;
      button.textContent = `Stop (${index + 1}/${parts.length})`;
      try {
        const { name } = await api.suggestName(part.id);
        if (name) {
          suggestions.set(part.id, name.trim());
          annotator.renamePart(part.id, name.trim());
        } else {
          unreadable += 1;
        }
      } catch (error) {
        toast('Stopped reading labels', error.message, 'error');
        break;
      }
    }

    const claims = new Map();
    for (const name of suggestions.values()) {
      const key = name.toLowerCase();
      claims.set(key, (claims.get(key) || 0) + 1);
    }

    let contested = 0;
    for (const [partId, name] of suggestions) {
      if (claims.get(name.toLowerCase()) > 1) {
        annotator.renamePart(partId, placeholders.get(partId) || 'Part');
        contested += 1;
      }
    }
    await savePartsNow();

    const read = suggestions.size - contested;
    const left = unreadable + contested;
    if (read) {
      toast(
        `Read ${read} of ${parts.length} labels`,
        `${left ? `${left} left as placeholders${contested ? ` (${contested} contested)` : ''}. ` : ''}`
        + (state.namingRoute === 'silkscreen'
          ? 'Nothing it was unsure of was guessed, but check the names against '
            + 'the board before you rely on the report.'
          : 'Check every name against the board: roughly one in ten can still '
            + 'be the neighbouring part’s label.'),
        'warn',
      );
    } else {
      toast(
        'No labels could be read',
        'The printed designators could not be made out. Name the parts by hand.',
        'warn',
      );
    }
  } finally {
    state.readingNames = false;
    button.innerHTML = original;
  }
}

/** Intersection over union of two [x, y, w, h] boxes. */
function overlaps(a, b) {
  const x0 = Math.max(a[0], b[0]);
  const y0 = Math.max(a[1], b[1]);
  const x1 = Math.min(a[0] + a[2], b[0] + b[2]);
  const y1 = Math.min(a[1] + a[3], b[1] + b[3]);
  if (x1 <= x0 || y1 <= y0) return 0;
  const intersection = (x1 - x0) * (y1 - y0);
  return intersection / (a[2] * a[3] + b[2] * b[3] - intersection);
}

async function suggestParts() {
  const button = $('#suggest-parts');
  button.disabled = true;
  button.textContent = 'Detecting…';
  updateStatus('Detecting components');
  try {
    const { suggestions } = await api.suggest();
    if (!suggestions.length) {
      toast('Nothing detected', 'Draw the boxes by hand, it only takes a moment.', 'warn');
      return;
    }

    // Auto-detect looks at the board, not at what is already on it, so it
    // returns the same components every time it runs. Adding them blindly
    // meant a second press silently doubled the part list, and a few presses
    // buried the board under hundreds of stacked boxes.
    //
    // Anything that already has a box is skipped. The detector stays
    // stateless and this screen decides what is actually new, which also
    // makes it safe to run again after drawing a few boxes by hand.
    const existing = state.annotator.getParts();
    const fresh = suggestions
      .map((s) => s.bbox)
      .filter((bbox) => !existing.some((part) => overlaps(bbox, part.bbox) > 0.3));
    const already = suggestions.length - fresh.length;

    if (!fresh.length) {
      toast(
        'Everything detected already has a box',
        `All ${suggestions.length} components found are already in your list.`,
      );
      return;
    }

    state.annotator.addParts(fresh);
    toast(
      `${fresh.length} part${fresh.length === 1 ? '' : 's'} found and numbered`,
      already
        ? `${already} already had a box and were left alone. These are placeholder numbers: type the real designators next.`
        : 'These are placeholder numbers in reading order. Type the real '
          + 'designators next: click a part, type, press Enter for the next one.',
      'success',
    );
  } catch (error) {
    toast('Auto-detect failed', error.message, 'error');
  } finally {
    updateStatus();
    button.disabled = false;
    button.innerHTML =
      '<svg viewBox="0 0 24 24" class="btn__icon" aria-hidden="true">'
      + '<path d="m12 3 2.1 5.2L19.5 10l-5.4 1.8L12 17l-2.1-5.2L4.5 10l5.4-1.8Z"/>'
      + '<path d="M18.5 15.5 19.4 18l2.5.9-2.5.9-.9 2.5-.9-2.5-2.5-.9 2.5-.9Z"/></svg>Auto-detect';
  }
}

/* =========================================================================
   Inspection
   ========================================================================= */

async function inspect(files) {
  if (!files.length) return;

  const limit = state.config.max_test_images;
  if (files.length > limit) {
    toast('Too many photos', `Only the first ${limit} will be inspected.`, 'warn');
    files = files.slice(0, limit);
  }

  await savePartsNow();

  const progress = $('#inspect-progress');
  const fill = $('#progress-fill');
  const label = $('#progress-label');

  progress.classList.remove('is-hidden');
  $('#inspect-empty').classList.add('is-hidden');
  label.textContent = files.length === 1
    ? 'Aligning and inspecting 1 board…'
    : `Aligning and inspecting ${files.length} boards…`;

  // The server returns the whole batch at once, so this is an indicator of
  // activity rather than true progress. It is paced to the measured ~0.7 s per
  // board so it does not sit at 90% for ten seconds, and it never reaches
  // 100% until the response actually lands.
  let elapsed = 0;
  const expected = files.length * 850;
  const ticker = setInterval(() => {
    elapsed += 100;
    fill.style.width = `${Math.min(92, (elapsed / expected) * 100)}%`;
  }, 100);

  updateStatus(`Inspecting ${files.length} board${files.length === 1 ? '' : 's'}`);

  try {
    const response = await api.inspect(files, $('#sensitivity').value);
    fill.style.width = '100%';

    state.results.push(...response.results);
    renderBatchSummary($('#batch-summary'), cumulativeBatch());
    await renderCards($('#results'), response.results, openDetail);
    refreshDropzoneSize();
    $('#export-report').disabled = state.results.length === 0;

    const bad = response.batch.missing_boards;
    if (bad === 0) {
      toast('All boards complete', `${response.batch.count} checked, nothing missing.`, 'success');
    } else {
      toast(
        `${bad} board${bad === 1 ? '' : 's'} with missing parts`,
        `${response.batch.total_missing_parts} part(s) not found. Open a board for detail.`,
        'error',
      );
    }
  } catch (error) {
    toast('Inspection failed', error.message, 'error');
    if (!state.results.length) $('#inspect-empty').classList.remove('is-hidden');
  } finally {
    clearInterval(ticker);
    updateStatus();
    setTimeout(() => {
      progress.classList.add('is-hidden');
      fill.style.width = '0%';
    }, 420);
  }
}

/**
 * Re-display the inspections already on disk for this board.
 *
 * Runs are persisted server-side, so losing them on a page refresh would be a
 * self-inflicted wound: someone who reloads mid-shift should find their last
 * results where they left them.
 */
async function restoreRuns() {
  try {
    const { runs } = await api.listRuns();
    if (!runs.length) return;

    const recent = runs.slice(0, state.config.max_test_images).reverse();
    const loaded = [];
    for (const summary of recent) {
      try {
        loaded.push(await api.getRun(summary.run_id));
      } catch {
        /* a run whose files were removed by hand is simply skipped */
      }
    }
    if (!loaded.length) return;

    state.results = loaded;
    $('#inspect-empty').classList.add('is-hidden');
    renderBatchSummary($('#batch-summary'), cumulativeBatch());
    await renderCards($('#results'), loaded, openDetail);
    $('#export-report').disabled = false;
    refreshDropzoneSize();
  } catch {
    /* restoring history is a convenience; never block startup on it */
  }
}

function cumulativeBatch() {
  return {
    count: state.results.length,
    ok_count: state.results.filter((r) => r.summary.status === 'present').length,
    missing_boards: state.results.filter((r) => r.summary.missing_count > 0).length,
    total_missing_parts: state.results.reduce((sum, r) => sum + r.summary.missing_count, 0),
    total_review_parts: state.results.reduce((sum, r) => sum + r.summary.review_count, 0),
  };
}

function openDetail(result) {
  if (!state.detail) state.detail = new DetailView($('#detail'));
  state.detail.open(result).catch((error) => {
    toast('Could not open that board', error.message, 'error');
  });
}

/* =========================================================================
   Drag, drop and paste
   ========================================================================= */

function wireDropzone(element, input, handler, { multiple = false } = {}) {
  element.addEventListener('click', () => input.click());
  element.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      input.click();
    }
  });

  input.addEventListener('change', () => {
    const files = imageFilesFrom(input.files);
    if (files.length) handler(multiple ? files : files[0]);
    input.value = '';
  });

  // dragenter/dragleave fire for child elements too; a depth counter keeps the
  // highlight from flickering as the pointer crosses the icon.
  let depth = 0;
  element.addEventListener('dragenter', (event) => {
    event.preventDefault();
    depth += 1;
    element.classList.add('is-over');
  });
  element.addEventListener('dragover', (event) => event.preventDefault());
  element.addEventListener('dragleave', () => {
    depth = Math.max(0, depth - 1);
    if (!depth) element.classList.remove('is-over');
  });
  element.addEventListener('drop', (event) => {
    event.preventDefault();
    depth = 0;
    element.classList.remove('is-over');
    const files = imageFilesFrom(event.dataTransfer.files);
    if (files.length) handler(multiple ? files : files[0]);
  });
}

function wirePaste() {
  window.addEventListener('paste', (event) => {
    const files = Array.from(event.clipboardData?.files || [])
      .filter((file) => file.type.startsWith('image/'));
    if (!files.length) return;
    event.preventDefault();
    if (state.step === 'inspect' && canEnter('inspect')) inspect(files);
    else uploadReference(files[0]);
  });
}

/* =========================================================================
   Menu
   ========================================================================= */

function wireMenu() {
  const menu = $('.menu');
  const button = $('#menu-btn');

  button.addEventListener('click', (event) => {
    event.stopPropagation();
    const open = menu.classList.toggle('is-open');
    button.setAttribute('aria-expanded', String(open));
  });
  document.addEventListener('click', () => {
    menu.classList.remove('is-open');
    button.setAttribute('aria-expanded', 'false');
  });

  $('#menu-panel').addEventListener('click', async (event) => {
    const action = event.target.closest('button')?.dataset.action;
    if (!action) return;
    menu.classList.remove('is-open');

    if (action === 'new') {
      const ok = await confirmAction({
        title: 'Start a new board?',
        body: 'The current board stays saved on disk; this creates a fresh one.',
        confirmLabel: 'Create',
      });
      if (!ok) return;
      state.results = [];
      $('#results').innerHTML = '';
    refreshDropzoneSize();
      $('#batch-summary').classList.add('is-hidden');
      $('#inspect-empty').classList.remove('is-hidden');
      await refreshProject(await api.createProject());
      goTo('reference');
      toast('New board created', '', 'success');
    }

    if (action === 'export') {
      try {
        const payload = await api.exportProject();
        downloadBlob(
          new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }),
          `${state.project.name.replace(/[^\w\- ]+/g, '') || 'board'}.pcbproj.json`,
        );
        toast('Setup exported', 'Reference photo and part names are inside the file.', 'success');
      } catch (error) {
        toast('Export failed', error.message, 'error');
      }
    }

    if (action === 'import') $('#file-import').click();

    if (action === 'clear-runs') {
      await api.clearRuns();
      state.results = [];
      $('#results').innerHTML = '';
    refreshDropzoneSize();
      $('#batch-summary').classList.add('is-hidden');
      $('#inspect-empty').classList.remove('is-hidden');
      $('#export-report').disabled = true;
      toast('History cleared', '', 'success');
    }

    if (action === 'about') $('#about').showModal();

    if (action === 'tour') {
      goTo('reference');
      await runTour(WELCOME_STEPS);
      markTourSeen();
    }
  });

  $('#about').querySelector('[data-close]').addEventListener('click', () => $('#about').close());

  $('#file-import').addEventListener('change', async () => {
    const file = $('#file-import').files[0];
    $('#file-import').value = '';
    if (!file) return;
    try {
      const project = await api.importProject(file);
      state.results = [];
      $('#results').innerHTML = '';
    refreshDropzoneSize();
      await refreshProject(project);
      toast('Setup imported', `${project.parts.length} parts restored.`, 'success');
      goTo(project.parts.length ? 'inspect' : 'parts');
    } catch (error) {
      toast('Import failed', error.message, 'error');
    }
  });
}

/* =========================================================================
   Boot
   ========================================================================= */

async function main() {
  initTheme();
  wireMenu();
  wirePaste();

  try {
    state.config = await api.config();
  } catch (error) {
    toast('Cannot reach the local server', error.message, 'error');
    return;
  }

  const select = $('#sensitivity');
  select.innerHTML = state.config.sensitivity_presets
    .map((preset) => `<option value="${preset.key}" title="${preset.description}">${preset.label}</option>`)
    .join('');
  select.addEventListener('change', async () => {
    try {
      state.project = await api.patchProject({ sensitivity: select.value });
      const preset = state.config.sensitivity_presets.find((p) => p.key === select.value);
      updateStatus();
      toast(`Sensitivity: ${preset.label}`, preset.description);
    } catch (error) {
      toast('Could not change sensitivity', error.message, 'error');
    }
  });

  $('#test-drop-hint').innerHTML =
    `or <u>browse</u> &middot; up to ${state.config.max_test_images} at once`;

  wireDropzone($('#reference-drop'), $('#file-reference'), uploadReference);
  wireDropzone($('#test-drop'), $('#file-tests'), inspect, { multiple: true });

  $('#stepper').addEventListener('click', (event) => {
    const button = event.target.closest('.stepper__item');
    if (button) goTo(button.dataset.step);
  });

  $('#to-parts').addEventListener('click', () => goTo('parts'));
  $('#replace-reference').addEventListener('click', () => $('#file-reference').click());
  $('#back-to-parts').addEventListener('click', () => goTo('parts'));
  $('#to-inspect').addEventListener('click', async () => {
    await savePartsNow();

    // Last chance to look at the names, offered only when some are still
    // generated. A placeholder cannot make the engine wrong, but it does make
    // the report say "Part 7 is missing" to someone who has to then work out
    // which component that was, so it is worth one question. Asking on every
    // run, including the ones where everything is named, would just be a
    // dialog people learn to dismiss without reading.
    const unnamed = (state.annotator?.getParts() || [])
      .filter((p) => isPlaceholder(p.name)).length;
    if (unnamed) {
      const review = await confirmAction({
        title: `${unnamed} ${unnamed === 1 ? 'part still has' : 'parts still have'}`
          + ' a placeholder name',
        body: 'Names never affect whether a part is found missing, but the '
          + 'report will say "Part 7" instead of "U3". You can look at each '
          + 'part as a picture and fill the rest in now.',
        confirmLabel: 'Check the names',
        cancelLabel: 'Inspect anyway',
      });
      if (review) { openReview(); return; }
    }
    goTo('inspect');
  });

  $('#zoom-in').addEventListener('click', () => state.annotator?.view.zoomBy(1.25));
  $('#zoom-out').addEventListener('click', () => state.annotator?.view.zoomBy(0.8));
  $('#zoom-fit').addEventListener('click', () => state.annotator?.view.fit(24, { animate: true }));
  $('#zoom-readout').addEventListener('click', () => state.annotator?.view.zoomTo(1));
  $('#zoom-selection').addEventListener('click', () => state.annotator?.zoomToSelection());

  $('#suggest-parts').addEventListener('click', suggestParts);
  $('#read-names').addEventListener('click', readNames);
  $('#review-names').addEventListener('click', openReview);
  $('#rename-all').addEventListener('click', () => {
    const annotator = state.annotator;
    if (!annotator?.count) return;
    annotator.autoName();
    toast(
      `Renumbered ${annotator.count} parts`,
      'These are placeholders in reading order, not the designators printed on '
      + 'the board. Double-click a part to see it closely and give it a real name.',
    );
  });

  $('#inspect-part').addEventListener('click', () => {
    const annotator = state.annotator;
    const parts = annotator?.getParts() || [];
    if (!parts.length) return;
    // Start on the first part still waiting for a name, since that is what
    // this is for.
    const target = parts.find((p) => /^part \d+$/i.test(p.name.trim())) || parts[0];
    annotator.select(target.id);
    openPartView(target.id, null);
  });

  $('#clear-parts').addEventListener('click', async () => {
    if (!state.annotator?.count) return;
    const ok = await confirmAction({
      title: 'Remove every part?',
      body: `All ${state.annotator.count} named parts will be deleted. Ctrl+Z undoes this.`,
      confirmLabel: 'Remove all',
      danger: true,
    });
    if (ok) state.annotator.clearParts();
  });

  $('#export-report').addEventListener('click', async () => {
    const button = $('#export-report');
    button.disabled = true;
    try {
      await buildReport(state.results, state.project.name);
      toast('Report saved', 'A self-contained HTML file with the images inside.', 'success');
    } catch (error) {
      toast('Could not build the report', error.message, 'error');
    } finally {
      button.disabled = false;
    }
  });

  $('#project-name').addEventListener('click', async () => {
    const name = await promptText({
      title: 'Name this board',
      body: 'This goes at the top of the exported report, so use whatever '
        + 'identifies the board to whoever reads it.',
      label: 'Board name',
      value: state.project.name === 'Untitled board' ? '' : state.project.name,
      confirmLabel: 'Save name',
    });
    if (name === null) return;
    state.project = await api.patchProject({ name });
    $('#project-name').textContent = state.project.name;
    toast('Board renamed', `Reports will say "${state.project.name}".`);
  });

  // Save pending part edits if the tab is closed or hidden mid-session.
  window.addEventListener('beforeunload', () => {
    if (state.dirty) navigator.sendBeacon?.('/api/health');
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') savePartsNow();
  });

  try {
    await refreshProject(await api.getProject());
    if (state.project.reference && state.project.parts.length) {
      goTo('inspect');
      await restoreRuns();
    } else if (state.project.reference) {
      goTo('parts');
    }
  } catch (error) {
    if (error instanceof ApiError) toast('Could not load the project', error.message, 'error');
  }

  // Only on a genuinely fresh start. Someone returning to a configured board
  // does not need to be told what the app is.
  if (!tourSeen() && !state.project?.reference) {
    // One frame so the layout has settled and the spotlight lands correctly.
    requestAnimationFrame(async () => {
      await runTour(WELCOME_STEPS);
      markTourSeen();
    });
  }
}

main();
