/**
 * Inspection results: summary strip, board cards, and the detail inspector.
 *
 * The verdict is shown three times at decreasing distance, because three
 * different people read it: the card banner answers "is this board good?"
 * across the room, the part list answers "which part?", and the expanded row
 * answers "why should I believe you?" with the two crops side by side and the
 * evidence that produced the number. The last one is what earns the operator's
 * trust the first time the tool disagrees with them.
 */

import { CanvasView, drawLabel, roundedRectPath } from './view.js';
import { loadImage, stateColour, formatTime, downloadBlob } from './ui.js';

const STATE_LABEL = {
  present: 'Present', review: 'Review', missing: 'Missing', not_visible: 'Not in view',
};

const ICONS = {
  ok: '<path d="M4 12.5 9.5 18 20 6.5"/>',
  review: '<path d="M12 8v5.5M12 17.2v.1"/><path d="M10.3 3.9 2.6 17.4A2 2 0 0 0 4.3 20.4h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/>',
  missing: '<circle cx="12" cy="12" r="8.6"/><path d="M8.8 8.8l6.4 6.4M15.2 8.8l-6.4 6.4"/>',
  not_visible: '<path d="M3 12s3.6-6.4 9-6.4S21 12 21 12s-3.6 6.4-9 6.4S3 12 3 12Z"/><path d="M4 4l16 16"/>',
};

const bannerKind = (status) => (status === 'present' ? 'ok' : status);

/* -------------------------------------------------------------------------
   Overlay rendering
   ------------------------------------------------------------------------- */

/**
 * Draw the board with its verdict boxes at the image's own resolution.
 * Used for card thumbnails and for the exported report.
 */
export function renderOverlayCanvas(image, verdicts, { lineScale = 1 } = {}) {
  const canvas = document.createElement('canvas');
  canvas.width = image.naturalWidth;
  canvas.height = image.naturalHeight;
  const ctx = canvas.getContext('2d');
  ctx.drawImage(image, 0, 0);

  const base = Math.max(canvas.width, canvas.height) / 600;
  const lineWidth = Math.max(1.6, base * 2.1 * lineScale);

  // Present parts first so that a missing box is never hidden under a
  // neighbouring green one.
  const rank = (v) => (v.state === 'missing' ? 3 : v.state === 'review' ? 2 : v.state === 'present' ? 1 : 0);
  const ordered = [...verdicts].sort((a, b) => rank(a) - rank(b));

  for (const verdict of ordered) {
    const [x, y, w, h] = verdict.bbox;
    const colour = stateColour(verdict.state);
    const alarming = verdict.state === 'missing' || verdict.state === 'review';
    ctx.save();
    roundedRectPath(ctx, x, y, w, h, 3 * base);
    if (alarming) {
      ctx.fillStyle = colour.replace(')', ', 0.22)').replace('rgb', 'rgba');
      ctx.globalAlpha = 0.25;
      ctx.fillStyle = colour;
      ctx.fill();
      ctx.globalAlpha = 1;
    }
    ctx.lineWidth = alarming ? lineWidth : lineWidth * 0.62;
    ctx.globalAlpha = verdict.state === 'not_visible' ? 0.55 : 1;
    ctx.strokeStyle = colour;
    ctx.stroke();
    ctx.restore();

    if (alarming) {
      drawLabel(ctx, verdict.name, x, y, colour, {
        fontSize: Math.max(11, base * 11),
        canvasWidth: canvas.width,
      });
    }
  }
  return canvas;
}

/* -------------------------------------------------------------------------
   Batch summary + cards
   ------------------------------------------------------------------------- */

export function renderBatchSummary(host, batch) {
  host.classList.remove('is-hidden');
  host.innerHTML = `
    <div class="stat">
      <span class="stat__value">${batch.count}</span>
      <span class="stat__label">Boards checked</span>
    </div>
    <span class="stat__sep"></span>
    <div class="stat stat--ok">
      <span class="stat__value">${batch.ok_count}</span>
      <span class="stat__label">Complete</span>
    </div>
    <span class="stat__sep"></span>
    <div class="stat stat--missing">
      <span class="stat__value">${batch.missing_boards}</span>
      <span class="stat__label">With missing parts</span>
    </div>
    <span class="stat__sep"></span>
    <div class="stat">
      <span class="stat__value">${batch.total_missing_parts}</span>
      <span class="stat__label">Parts missing in total</span>
    </div>`;
}

export async function renderCards(host, results, onOpen) {
  for (const [index, result] of results.entries()) {
    const card = document.createElement('article');
    card.className = 'board-card';
    card.style.animationDelay = `${Math.min(index * 60, 360)}ms`;
    card.tabIndex = 0;
    card.setAttribute('role', 'button');

    const { status, headline, missing_names: missing, review_names: review } = result.summary;
    const kind = bannerKind(status);

    const chips = [
      ...missing.map((name) => `<span class="chip chip--missing">${escapeHtml(name)}</span>`),
      ...review.map((name) => `<span class="chip chip--review">${escapeHtml(name)}</span>`),
    ];
    if (!chips.length) {
      chips.push(
        `<span class="chip chip--muted">${result.summary.checked_count} parts verified</span>`,
      );
    }
    if (result.summary.not_visible_count) {
      chips.push(
        `<span class="chip chip--muted">${result.summary.not_visible_count} not in view</span>`,
      );
    }

    card.innerHTML = `
      <div class="board-card__media"><canvas></canvas></div>
      <div class="board-card__banner banner--${kind}">
        <svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[kind]}</svg>
        <span>${escapeHtml(headline)}</span>
      </div>
      <div class="board-card__body">
        <p class="board-card__file" title="${escapeHtml(result.filename)}">${escapeHtml(result.filename)}</p>
        <div class="chips">${chips.join('')}</div>
        <div class="board-card__foot">
          <span>Alignment ${(result.alignment.quality * 100).toFixed(0)}%</span>
          <span>${result.elapsed_ms.toFixed(0)} ms</span>
        </div>
      </div>`;

    card.addEventListener('click', () => onOpen(result));
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onOpen(result);
      }
    });

    host.prepend(card);

    // Paint the thumbnail after the card is in the DOM so the layout settles
    // first and the cards appear immediately rather than after every decode.
    loadImage(result.images.test)
      .then((image) => {
        const source = renderOverlayCanvas(image, result.verdicts);
        const target = card.querySelector('canvas');
        const ratio = window.devicePixelRatio || 1;
        const box = target.getBoundingClientRect();
        target.width = Math.round(box.width * ratio);
        target.height = Math.round(box.height * ratio);
        const ctx = target.getContext('2d');
        const scale = Math.min(target.width / source.width, target.height / source.height);
        const w = source.width * scale;
        const h = source.height * scale;
        ctx.drawImage(source, (target.width - w) / 2, (target.height - h) / 2, w, h);
        result._overlay = source;
      })
      .catch(() => { /* thumbnail is cosmetic; the verdict is already shown */ });
  }
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/* -------------------------------------------------------------------------
   Detail inspector
   ------------------------------------------------------------------------- */

export class DetailView {
  constructor(dialog) {
    this.dialog = dialog;
    this.canvas = dialog.querySelector('#detail-canvas');
    this.host = dialog.querySelector('#viewer');
    this.handle = dialog.querySelector('#compare-handle');
    this.view = new CanvasView(this.canvas, this.host);
    this.view.onChange = () => this.draw();

    this.result = null;
    this.images = null;
    this.mode = 'overlay';
    this.split = 0.5;
    this.hovered = null;

    this._bind();
  }

  _bind() {
    dialogCloseOn(this.dialog, this.dialog.querySelector('#detail-close'));

    this.dialog.querySelector('#detail-view').addEventListener('click', (event) => {
      const button = event.target.closest('button[data-view]');
      if (!button) return;
      this.setMode(button.dataset.view);
    });

    // --- viewer pan / zoom ---
    let panning = null;
    this.canvas.addEventListener('pointerdown', (event) => {
      this.canvas.setPointerCapture(event.pointerId);
      panning = { x: event.clientX, y: event.clientY };
      this.canvas.style.cursor = 'grabbing';
    });
    this.canvas.addEventListener('pointermove', (event) => {
      if (!panning) return;
      this.view.panBy(event.clientX - panning.x, event.clientY - panning.y);
      panning = { x: event.clientX, y: event.clientY };
    });
    const endPan = () => { panning = null; this.canvas.style.cursor = 'grab'; };
    this.canvas.addEventListener('pointerup', endPan);
    this.canvas.addEventListener('pointercancel', endPan);
    this.canvas.style.cursor = 'grab';

    this.canvas.addEventListener('wheel', (event) => {
      event.preventDefault();
      const point = this.view.pointer(event);
      this.view.zoomAt(Math.exp(-event.deltaY * 0.0022), point.x, point.y);
    }, { passive: false });

    // --- compare slider ---
    let dragging = false;
    const move = (event) => {
      if (!dragging) return;
      const rect = this.host.getBoundingClientRect();
      this.split = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
      this.positionHandle();
      this.draw();
    };
    this.handle.addEventListener('pointerdown', (event) => {
      dragging = true;
      this.handle.setPointerCapture(event.pointerId);
      event.stopPropagation();
    });
    this.handle.addEventListener('pointermove', move);
    this.handle.addEventListener('pointerup', () => { dragging = false; });
  }

  async open(result) {
    this.result = result;
    this.mode = 'overlay';
    this.split = 0.5;
    this.hovered = null;

    this.dialog.querySelector('#detail-title').textContent = result.filename;
    this.dialog.querySelector('#detail-sub').textContent =
      `Inspected ${formatTime(result.created_at)} · ${result.summary.total_parts} parts · `
      + `${result.sensitivity} sensitivity`;

    this._syncModeButtons();
    this.renderSide();

    if (!this.dialog.open) this.dialog.showModal();

    this.images = {
      test: await loadImage(result.images.aligned),
      reference: await loadImage(result.images.reference),
    };

    this.view.resize();
    this.view.setContent(this.images.test.naturalWidth, this.images.test.naturalHeight);
    this.view.fit(16);
    this.positionHandle();
    this.draw();
  }

  setMode(mode) {
    this.mode = mode;
    this._syncModeButtons();
    this.handle.classList.toggle('is-hidden', mode !== 'compare');
    this.positionHandle();
    this.draw();
  }

  _syncModeButtons() {
    for (const button of this.dialog.querySelectorAll('#detail-view button')) {
      button.classList.toggle('is-active', button.dataset.view === this.mode);
    }
    this.handle.classList.toggle('is-hidden', this.mode !== 'compare');
  }

  positionHandle() {
    this.handle.style.left = `${this.split * 100}%`;
  }

  highlight(partId) {
    this.hovered = partId;
    this.draw();
  }

  focusPart(partId) {
    const verdict = this.result?.verdicts.find((v) => v.part_id === partId);
    if (!verdict) return;
    const [x, y, w, h] = verdict.bbox;
    this.view.focusOn(x, y, w, h, 140);
    this.highlight(partId);
  }

  draw() {
    if (!this.images) return;
    const { ctx, scale, tx, ty } = this.view;
    this.view.clear();

    const { test, reference } = this.images;
    const w = test.naturalWidth * scale;
    const h = test.naturalHeight * scale;

    if (this.mode === 'compare') {
      const splitX = this.split * this.view.width;

      ctx.save();
      ctx.beginPath();
      ctx.rect(0, 0, splitX, this.view.height);
      ctx.clip();
      ctx.drawImage(reference, tx, ty, w, h);
      ctx.restore();

      ctx.save();
      ctx.beginPath();
      ctx.rect(splitX, 0, this.view.width - splitX, this.view.height);
      ctx.clip();
      ctx.drawImage(test, tx, ty, w, h);
      ctx.restore();
    } else {
      ctx.imageSmoothingEnabled = scale <= 2;
      ctx.drawImage(test, tx, ty, w, h);
    }

    const rank = (v) => (v.state === 'missing' ? 3 : v.state === 'review' ? 2 : v.state === 'present' ? 1 : 0);
    const ordered = [...this.result.verdicts].sort((a, b) => rank(a) - rank(b));

    for (const verdict of ordered) {
      const [bx, by, bw, bh] = verdict.bbox;
      const topLeft = this.view.toScreen(bx, by);
      const rect = { x: topLeft.x, y: topLeft.y, w: bw * scale, h: bh * scale };
      const colour = stateColour(verdict.state);
      const isHovered = this.hovered === verdict.part_id;
      const alarming = verdict.state === 'missing' || verdict.state === 'review';

      ctx.save();
      roundedRectPath(ctx, rect.x, rect.y, rect.w, rect.h, 3);
      if (alarming || isHovered) {
        ctx.globalAlpha = isHovered ? 0.3 : 0.2;
        ctx.fillStyle = colour;
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.lineWidth = isHovered ? 3 : alarming ? 2 : 1.2;
      ctx.globalAlpha = verdict.state === 'not_visible' && !isHovered ? 0.55 : 1;
      ctx.strokeStyle = colour;
      ctx.stroke();
      ctx.restore();

      if (alarming || isHovered) {
        drawLabel(ctx, verdict.name, rect.x, rect.y, colour, { canvasWidth: this.view.width });
      }
    }
  }

  renderSide() {
    const result = this.result;
    const { status, headline } = result.summary;
    const kind = bannerKind(status);

    const verdictBox = this.dialog.querySelector('#detail-verdict');
    verdictBox.className = `verdict verdict--${kind}`;
    verdictBox.innerHTML = `
      <h3>${escapeHtml(headline)}</h3>
      <p>${
        status === 'missing'
          ? `Missing: ${escapeHtml(result.summary.missing_names.join(', '))}`
          : status === 'review'
            ? `Please check: ${escapeHtml(result.summary.review_names.join(', '))}`
            : status === 'not_visible'
              ? 'None of the named parts are inside this photograph.'
              : result.summary.not_visible_count
                ? `All ${result.summary.checked_count} parts in view are present. `
                  + `${result.summary.not_visible_count} were outside the photo.`
                : 'Every named part was found on this board.'
      }</p>`;

    const alignment = result.alignment;
    const sharpness = result.sharpness || {};
    const warnings = (result.warnings || [])
      .map((text) => `
        <div class="warn-note">
          <svg viewBox="0 0 24 24"><path d="M12 8v5.5M12 17.2v.1"/><path d="M10.3 3.9 2.6 17.4A2 2 0 0 0 4.3 20.4h15.4a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>
          <span>${escapeHtml(text)}</span>
        </div>`)
      .join('');

    this.dialog.querySelector('#detail-diagnostics').innerHTML = `
      ${warnings}
      <dl class="diag-row"><dt>Alignment quality</dt><dd>${(alignment.quality * 100).toFixed(0)}%</dd></dl>
      <dl class="diag-row"><dt>Method</dt><dd>${escapeHtml(alignment.method.toUpperCase())}</dd></dl>
      <dl class="diag-row"><dt>Shift corrected</dt><dd>${alignment.translation_px.toFixed(1)} px · ${alignment.rotation_deg.toFixed(2)}°</dd></dl>
      <dl class="diag-row"><dt>Exposure correction</dt><dd>×${(result.photometric.gain ?? 1).toFixed(2)}</dd></dl>
      ${sharpness.sigma ? `<dl class="diag-row"><dt>Focus equalised</dt><dd>σ ${sharpness.sigma.toFixed(2)} px</dd></dl>` : ''}
      ${result.scene_compensation > 0.01 ? `<dl class="diag-row"><dt>Scene compensation</dt><dd>−${result.scene_compensation.toFixed(2)}</dd></dl>` : ''}
      <dl class="diag-row"><dt>Processing time</dt><dd>${result.elapsed_ms.toFixed(0)} ms</dd></dl>`;

    const list = this.dialog.querySelector('#verdict-list');
    list.innerHTML = '';

    const order = { missing: 0, review: 1, present: 2, not_visible: 3 };
    const sorted = [...result.verdicts].sort(
      (a, b) => order[a.state] - order[b.state] || b.confidence - a.confidence,
    );

    for (const verdict of sorted) {
      list.appendChild(this._verdictRow(verdict));
    }
  }

  _verdictRow(verdict) {
    const item = document.createElement('li');
    item.className = `vrow vrow--${verdict.state}`;

    const thresholds = this.result.thresholds;
    const colour = stateColour(verdict.state);

    const evidence = verdict.evidence
      .map((channel) => `
        <div class="ev-row">
          <span class="ev-row__label">${escapeHtml(channel.label)}</span>
          <span class="ev-row__track">
            <span class="ev-row__fill" style="width:${(channel.score * 100).toFixed(0)}%;background:${colour}"></span>
          </span>
          <span class="ev-row__value">${channel.score.toFixed(2)}</span>
        </div>`)
      .join('');

    item.innerHTML = `
      <button class="vrow__head" type="button">
        <span class="vrow__dot"></span>
        <span class="vrow__name">${escapeHtml(verdict.name)}</span>
        <span class="vrow__state">${STATE_LABEL[verdict.state]}</span>
      </button>
      <div class="vrow__body">
        <p class="vrow__reason">${escapeHtml(verdict.reason)}</p>
        <div class="confidence-bar">
          <div class="confidence-bar__track">
            <span class="confidence-bar__fill" style="width:${(verdict.confidence * 100).toFixed(0)}%;background:${colour}"></span>
            <span class="confidence-bar__mark" style="left:${(thresholds.review * 100).toFixed(0)}%"></span>
            <span class="confidence-bar__mark" style="left:${(thresholds.missing * 100).toFixed(0)}%"></span>
          </div>
          <div class="confidence-bar__legend">
            <span>confidence ${verdict.confidence.toFixed(2)}</span>
            <span>flags at ${thresholds.missing.toFixed(2)}</span>
          </div>
        </div>
        <div class="crops">
          <figure class="crop">
            <img loading="lazy" alt="Reference crop of ${escapeHtml(verdict.name)}"
                 src="/api/runs/${this.result.run_id}/crop/${verdict.part_id}/reference.png">
            <figcaption>Reference</figcaption>
          </figure>
          <figure class="crop">
            <img loading="lazy" alt="This board at ${escapeHtml(verdict.name)}"
                 src="/api/runs/${this.result.run_id}/crop/${verdict.part_id}/test.png">
            <figcaption>This board</figcaption>
          </figure>
        </div>
        <div class="evidence">${evidence}</div>
      </div>`;

    const head = item.querySelector('.vrow__head');
    head.addEventListener('click', () => {
      const open = item.classList.toggle('is-open');
      if (open) this.focusPart(verdict.part_id);
    });
    item.addEventListener('mouseenter', () => this.highlight(verdict.part_id));
    item.addEventListener('mouseleave', () => this.highlight(null));

    // Missing parts start expanded: that is the row the operator opened the
    // dialog to read, and making them click again to see the proof is friction
    // at exactly the wrong moment.
    if (verdict.state === 'missing') item.classList.add('is-open');

    return item;
  }
}

function dialogCloseOn(dialog, button) {
  button.addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', (event) => {
    // Clicking the backdrop (outside the dialog's own box) closes it.
    const rect = dialog.getBoundingClientRect();
    const inside =
      event.clientX >= rect.left && event.clientX <= rect.right &&
      event.clientY >= rect.top && event.clientY <= rect.bottom;
    if (!inside) dialog.close();
  });
}

/* -------------------------------------------------------------------------
   Offline report
   ------------------------------------------------------------------------- */

/**
 * Build a single self-contained HTML file with the images inlined.
 *
 * A report that referenced /api/... would be blank the moment the server
 * stopped, which is precisely when someone wants to read it. Inlining keeps
 * it readable on any machine, forever, with no application at all.
 */
export async function buildReport(results, projectName) {
  const sections = [];

  for (const result of results) {
    const image = result._overlay || renderOverlayCanvas(
      await loadImage(result.images.test), result.verdicts,
    );
    const dataUrl = image.toDataURL('image/jpeg', 0.82);
    const { status, headline } = result.summary;

    const rows = result.verdicts
      .slice()
      .sort((a, b) => b.confidence - a.confidence)
      .map((verdict) => `
        <tr class="s-${verdict.state}">
          <td>${escapeHtml(verdict.name)}</td>
          <td>${STATE_LABEL[verdict.state]}</td>
          <td>${verdict.confidence.toFixed(3)}</td>
          <td>${escapeHtml(verdict.reason)}</td>
        </tr>`)
      .join('');

    sections.push(`
      <section>
        <h2 class="s-${status}">${escapeHtml(result.filename)}: ${escapeHtml(headline)}</h2>
        <p class="meta">
          Inspected ${new Date(result.created_at * 1000).toLocaleString()} ·
          sensitivity ${escapeHtml(result.sensitivity)} ·
          alignment ${(result.alignment.quality * 100).toFixed(0)}% ·
          ${result.elapsed_ms.toFixed(0)} ms
        </p>
        <img src="${dataUrl}" alt="Inspected board">
        <table>
          <thead><tr><th>Part</th><th>State</th><th>Confidence</th><th>Reason</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </section>`);
  }

  const html = `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Inspection report: ${escapeHtml(projectName)}</title>
<style>
  body{font:15px/1.6 -apple-system,"Segoe UI",Roboto,sans-serif;margin:0;padding:40px;
       background:#f6f8fb;color:#101722;max-width:980px;margin-inline:auto}
  h1{font-size:1.7rem;margin:0 0 4px}
  .lead{color:#5b6b80;margin:0 0 32px}
  section{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:22px;margin-bottom:22px}
  h2{font-size:1.1rem;margin:0 0 4px}
  .meta{color:#6b7c93;font-size:.82rem;margin:0 0 14px}
  img{width:100%;border-radius:10px;border:1px solid #e2e8f0;display:block;margin-bottom:16px}
  table{width:100%;border-collapse:collapse;font-size:.86rem}
  th{text-align:left;color:#6b7c93;font-weight:600;border-bottom:2px solid #e2e8f0;padding:7px 8px}
  td{padding:7px 8px;border-bottom:1px solid #eef2f7}
  .s-missing{color:#d12c43}.s-review{color:#b26a00}.s-present{color:#0f8f52}
  h2.s-present{color:#0f8f52}h2.s-missing{color:#d12c43}h2.s-review{color:#b26a00}
  footer{color:#8494a8;font-size:.78rem;text-align:center;margin-top:30px}
</style></head>
<body>
  <h1>Inspection report</h1>
  <p class="lead">${escapeHtml(projectName)} · generated ${new Date().toLocaleString()}</p>
  ${sections.join('')}
  <footer>Produced offline by PCB Inspector. Images are embedded in this file.</footer>
</body></html>`;

  downloadBlob(
    new Blob([html], { type: 'text/html' }),
    `inspection-report-${new Date().toISOString().slice(0, 10)}.html`,
  );
}
