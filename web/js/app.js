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
  toast, confirmAction, loadImage, imageFilesFrom, downloadBlob, formatBytes,
} from './ui.js';
import { Annotator } from './annotator.js';
import {
  DetailView, renderBatchSummary, renderCards, buildReport,
} from './results.js';

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
  const stored = localStorage.getItem('pcb-theme');
  const preferred = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  document.documentElement.dataset.theme = stored || preferred;

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
    onRenameRequest: (id) => {
      const input = document.querySelector(`.part-row[data-id="${id}"] .part-row__name`);
      if (input) {
        input.focus();
        input.select();
        input.scrollIntoView({ block: 'nearest' });
      }
    },
  });
  state.annotator.isActive = () => state.step === 'parts';
  state.annotator.view.onChange = () => {
    state.annotator.requestDraw();
    updateZoomReadout();
  };
}

function updateZoomReadout() {
  if (!state.annotator) return;
  $('#zoom-readout').textContent = `${Math.round(state.annotator.view.scale * 100)}%`;
}

function renderPartList() {
  const annotator = state.annotator;
  if (!annotator) return;

  const parts = annotator.getParts();
  const list = $('#part-list');
  $('#parts-count').textContent = String(parts.length);
  $('#parts-empty').classList.toggle('is-hidden', parts.length > 0);
  $('#reference-parts').textContent = String(parts.length);
  $('#to-inspect').disabled = parts.length === 0;

  const active = document.activeElement;
  const activeId = active?.closest?.('.part-row')?.dataset.id;
  const caret = active instanceof HTMLInputElement ? active.selectionStart : null;

  list.innerHTML = '';
  parts.forEach((part, index) => {
    const row = document.createElement('li');
    row.className = 'part-row';
    row.dataset.id = part.id;
    if (part.id === annotator.selectedId) row.classList.add('is-selected');

    row.innerHTML = `
      <span class="part-row__index">${index + 1}</span>
      <input class="part-row__name" value="" maxlength="40" spellcheck="false"
             aria-label="Part name">
      <span class="part-row__dims">${part.bbox[2]}×${part.bbox[3]}</span>
      <button class="part-row__del" type="button" title="Delete ${part.name}" aria-label="Delete part">
        <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6 6 18"/></svg>
      </button>`;

    const input = row.querySelector('.part-row__name');
    input.value = part.name;

    input.addEventListener('focus', () => annotator.select(part.id));
    input.addEventListener('change', () => annotator.renamePart(part.id, input.value.trim()));
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        input.blur();
        event.preventDefault();
      }
      event.stopPropagation();      // let the editor keep Delete / arrows
    });

    row.addEventListener('click', (event) => {
      if (event.target.closest('.part-row__del')) return;
      if (event.target === input) return;
      annotator.focusPart(part.id);
    });

    row.querySelector('.part-row__del').addEventListener('click', (event) => {
      event.stopPropagation();
      annotator.deletePart(part.id);
    });

    list.appendChild(row);
  });

  if (activeId) {
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
    goTo(state.step);
  } catch (error) {
    toast('Could not save the parts', error.message, 'error');
  }
}

async function suggestParts() {
  const button = $('#suggest-parts');
  button.disabled = true;
  button.textContent = 'Detecting…';
  try {
    const { suggestions } = await api.suggest();
    if (!suggestions.length) {
      toast('Nothing detected', 'Draw the boxes by hand — it only takes a moment.', 'warn');
      return;
    }
    state.annotator.addParts(suggestions.map((s) => s.bbox));
    toast(
      `${suggestions.length} candidate parts added`,
      'Rename the ones you care about and delete the rest.',
      'success',
    );
  } catch (error) {
    toast('Auto-detect failed', error.message, 'error');
  } finally {
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

  try {
    const response = await api.inspect(files, $('#sensitivity').value);
    fill.style.width = '100%';

    state.results.push(...response.results);
    renderBatchSummary($('#batch-summary'), cumulativeBatch());
    await renderCards($('#results'), response.results, openDetail);
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
      $('#batch-summary').classList.add('is-hidden');
      $('#inspect-empty').classList.remove('is-hidden');
      $('#export-report').disabled = true;
      toast('History cleared', '', 'success');
    }

    if (action === 'about') $('#about').showModal();
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
    goTo('inspect');
  });

  $('#zoom-in').addEventListener('click', () => state.annotator?.view.zoomBy(1.25));
  $('#zoom-out').addEventListener('click', () => state.annotator?.view.zoomBy(0.8));
  $('#zoom-fit').addEventListener('click', () => state.annotator?.view.fit());

  $('#suggest-parts').addEventListener('click', suggestParts);
  $('#rename-all').addEventListener('click', () => state.annotator?.autoName());

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
    const name = prompt('Name this board', state.project.name);
    if (name === null) return;
    state.project = await api.patchProject({ name: name.trim() || state.project.name });
    $('#project-name').textContent = state.project.name;
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
}

main();
