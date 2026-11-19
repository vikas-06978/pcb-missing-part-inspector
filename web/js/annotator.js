/**
 * Interactive box editor for naming components on the reference board.
 *
 * The brief has the operator point out and name each part once, and at the
 * live demo someone who has never seen this app will do exactly that while
 * being watched. So the interaction has to be the one everybody already
 * knows from image editors: drag to draw, click to select, handles to resize,
 * Delete to remove, Ctrl+Z to undo, wheel to zoom.
 *
 * Renaming deliberately happens in the side list rather than in an in-canvas
 * text field: a real input gets native caret handling, selection and IME for
 * free, and a double-click on the canvas simply focuses it.
 */

import { CanvasView, drawLabel, roundedRectPath } from './view.js';
import { cssVar } from './ui.js';

const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const HANDLE_HIT = 9;       // screen px
const HANDLE_SIZE = 7;      // screen px
const MIN_BOX = 6;          // image px
const MAX_HISTORY = 60;

let nextLocalId = 1;
const makeId = () => `n${(nextLocalId++).toString(36)}${Date.now().toString(36).slice(-4)}`;

export class Annotator {
  constructor({ canvas, host, emptyHint, onChange, onSelect, onRenameRequest }) {
    this.view = new CanvasView(canvas, host);
    this.host = host;
    this.emptyHint = emptyHint;
    this.onChange = onChange || (() => {});
    this.onSelect = onSelect || (() => {});
    this.onRenameRequest = onRenameRequest || (() => {});

    this.image = null;
    this.parts = [];
    this.selectedId = null;

    this.mode = 'idle';
    this.draft = null;
    this.spaceDown = false;
    this._drag = null;
    this._frame = null;

    this.history = [];
    this.future = [];

    this.view.onChange = () => this.requestDraw();
    this._bindPointer();
    this._bindKeyboard();
  }

  // ---- content ----------------------------------------------------------

  setImage(image) {
    this.image = image;
    this.view.setContent(image.naturalWidth, image.naturalHeight);
    this.view.fit();
    this.requestDraw();
  }

  setParts(parts, { resetHistory = true } = {}) {
    this.parts = parts.map((part) => ({
      id: part.id || makeId(),
      name: part.name || '',
      bbox: part.bbox.map((value) => Math.round(value)),
    }));
    if (resetHistory) {
      this.history = [];
      this.future = [];
    }
    this.selectedId = null;
    this._emit();
  }

  getParts() {
    return this.parts.map((part) => ({ ...part, bbox: [...part.bbox] }));
  }

  get count() {
    return this.parts.length;
  }

  // ---- mutation with undo ------------------------------------------------

  _snapshot() {
    this.history.push(JSON.stringify(this.parts));
    if (this.history.length > MAX_HISTORY) this.history.shift();
    this.future.length = 0;
  }

  undo() {
    if (!this.history.length) return false;
    this.future.push(JSON.stringify(this.parts));
    this.parts = JSON.parse(this.history.pop());
    if (!this.parts.some((p) => p.id === this.selectedId)) this.selectedId = null;
    this._emit();
    return true;
  }

  redo() {
    if (!this.future.length) return false;
    this.history.push(JSON.stringify(this.parts));
    this.parts = JSON.parse(this.future.pop());
    this._emit();
    return true;
  }

  addParts(boxes, namePrefix = 'Part') {
    this._snapshot();
    let index = this.parts.length;
    for (const bbox of boxes) {
      index += 1;
      this.parts.push({ id: makeId(), name: `${namePrefix} ${index}`, bbox: bbox.map(Math.round) });
    }
    this._emit();
  }

  renamePart(id, name) {
    const part = this.parts.find((p) => p.id === id);
    if (!part || part.name === name) return;
    this._snapshot();
    part.name = name;
    this._emit();
  }

  deletePart(id) {
    const index = this.parts.findIndex((p) => p.id === id);
    if (index < 0) return;
    this._snapshot();
    this.parts.splice(index, 1);
    if (this.selectedId === id) this.selectedId = null;
    this._emit();
  }

  clearParts() {
    if (!this.parts.length) return;
    this._snapshot();
    this.parts = [];
    this.selectedId = null;
    this._emit();
  }

  autoName(prefix = 'Part') {
    this._snapshot();
    // Reading order: top to bottom, then left to right, so the numbering
    // matches how someone scans the board rather than when boxes were drawn.
    const ordered = [...this.parts].sort((a, b) => {
      const rowA = Math.round(a.bbox[1] / 40);
      const rowB = Math.round(b.bbox[1] / 40);
      return rowA === rowB ? a.bbox[0] - b.bbox[0] : rowA - rowB;
    });
    ordered.forEach((part, index) => {
      part.name = `${prefix} ${index + 1}`;
    });
    this.parts = ordered;
    this._emit();
  }

  select(id) {
    this.selectedId = id;
    this.onSelect(id);
    this.requestDraw();
  }

  focusPart(id) {
    const part = this.parts.find((p) => p.id === id);
    if (!part) return;
    const [x, y, w, h] = part.bbox;
    this.view.focusOn(x, y, w, h);
    this.select(id);
  }

  _emit() {
    this.onChange(this.getParts());
    this.requestDraw();
  }

  // ---- hit testing -------------------------------------------------------

  _screenRect(part) {
    const [x, y, w, h] = part.bbox;
    const topLeft = this.view.toScreen(x, y);
    return { x: topLeft.x, y: topLeft.y, w: w * this.view.scale, h: h * this.view.scale };
  }

  _handlePoints(rect) {
    const { x, y, w, h } = rect;
    return {
      nw: { x, y },
      n: { x: x + w / 2, y },
      ne: { x: x + w, y },
      e: { x: x + w, y: y + h / 2 },
      se: { x: x + w, y: y + h },
      s: { x: x + w / 2, y: y + h },
      sw: { x, y: y + h },
      w: { x, y: y + h / 2 },
    };
  }

  _handleAt(point) {
    if (!this.selectedId) return null;
    const part = this.parts.find((p) => p.id === this.selectedId);
    if (!part) return null;
    const points = this._handlePoints(this._screenRect(part));
    for (const key of HANDLES) {
      const handle = points[key];
      if (Math.abs(point.x - handle.x) <= HANDLE_HIT && Math.abs(point.y - handle.y) <= HANDLE_HIT) {
        return key;
      }
    }
    return null;
  }

  _partAt(point) {
    // Last drawn is topmost, so search backwards.
    for (let i = this.parts.length - 1; i >= 0; i -= 1) {
      const rect = this._screenRect(this.parts[i]);
      if (
        point.x >= rect.x && point.x <= rect.x + rect.w &&
        point.y >= rect.y && point.y <= rect.y + rect.h
      ) {
        return this.parts[i];
      }
    }
    return null;
  }

  // ---- pointer interaction ----------------------------------------------

  _bindPointer() {
    const canvas = this.view.canvas;

    canvas.addEventListener('pointerdown', (event) => {
      if (!this.image) return;
      canvas.setPointerCapture(event.pointerId);
      const point = this.view.pointer(event);

      if (event.button === 1 || this.spaceDown) {
        this.mode = 'pan';
        this._drag = { startX: event.clientX, startY: event.clientY };
        this.host.classList.add('is-panning');
        return;
      }
      if (event.button !== 0) return;

      const handle = this._handleAt(point);
      if (handle) {
        const part = this.parts.find((p) => p.id === this.selectedId);
        this._snapshot();
        this.mode = 'resize';
        this._drag = { handle, origin: [...part.bbox], part };
        return;
      }

      const hit = this._partAt(point);
      if (hit) {
        this.select(hit.id);
        this._snapshot();
        this.mode = 'move';
        const image = this.view.toImage(point.x, point.y);
        this._drag = { part: hit, origin: [...hit.bbox], grabX: image.x, grabY: image.y };
        return;
      }

      this.select(null);
      this.mode = 'draw';
      const image = this.view.toImage(point.x, point.y);
      this.draft = { x0: image.x, y0: image.y, x1: image.x, y1: image.y };
    });

    canvas.addEventListener('pointermove', (event) => {
      if (!this.image) return;
      const point = this.view.pointer(event);

      if (this.mode === 'idle') {
        this._updateCursor(point);
        return;
      }

      if (this.mode === 'pan') {
        this.view.panBy(event.clientX - this._drag.startX, event.clientY - this._drag.startY);
        this._drag.startX = event.clientX;
        this._drag.startY = event.clientY;
        return;
      }

      const image = this.view.toImage(point.x, point.y);

      if (this.mode === 'draw') {
        this.draft.x1 = image.x;
        this.draft.y1 = image.y;
        this.requestDraw();
        return;
      }

      if (this.mode === 'move') {
        const { part, origin, grabX, grabY } = this._drag;
        part.bbox = this._clampBox([
          Math.round(origin[0] + (image.x - grabX)),
          Math.round(origin[1] + (image.y - grabY)),
          origin[2],
          origin[3],
        ]);
        this.requestDraw();
        return;
      }

      if (this.mode === 'resize') {
        const { handle, origin, part } = this._drag;
        part.bbox = this._clampBox(this._resized(origin, handle, image));
        this.requestDraw();
      }
    });

    const finish = () => {
      if (this.mode === 'draw' && this.draft) {
        const { x0, y0, x1, y1 } = this.draft;
        const box = this._clampBox([
          Math.round(Math.min(x0, x1)),
          Math.round(Math.min(y0, y1)),
          Math.round(Math.abs(x1 - x0)),
          Math.round(Math.abs(y1 - y0)),
        ]);
        this.draft = null;
        // A click, not a drag: treat as "deselect" rather than creating a
        // one-pixel part nobody can see or hit again.
        if (box[2] >= MIN_BOX && box[3] >= MIN_BOX) {
          this._snapshot();
          const part = { id: makeId(), name: `Part ${this.parts.length + 1}`, bbox: box };
          this.parts.push(part);
          this.selectedId = part.id;
          this._emit();
          this.onSelect(part.id);
          this.onRenameRequest(part.id);
        }
      } else if (this.mode === 'move' || this.mode === 'resize') {
        this._emit();
      }

      this.mode = 'idle';
      this._drag = null;
      this.host.classList.remove('is-panning');
      this.requestDraw();
    };

    canvas.addEventListener('pointerup', finish);
    canvas.addEventListener('pointercancel', finish);

    canvas.addEventListener('dblclick', (event) => {
      const hit = this._partAt(this.view.pointer(event));
      if (hit) {
        this.select(hit.id);
        this.onRenameRequest(hit.id);
      }
    });

    canvas.addEventListener('wheel', (event) => {
      if (!this.image) return;
      event.preventDefault();
      const point = this.view.pointer(event);
      // Trackpads send many small deltas; the exponential keeps the response
      // even across a mouse wheel's coarse clicks and a trackpad's fine ones.
      this.view.zoomAt(Math.exp(-event.deltaY * 0.0022), point.x, point.y);
    }, { passive: false });
  }

  _updateCursor(point) {
    const canvas = this.view.canvas;
    if (this.spaceDown) {
      canvas.style.cursor = 'grab';
      return;
    }
    const handle = this._handleAt(point);
    if (handle) {
      const cursors = {
        nw: 'nwse-resize', se: 'nwse-resize',
        ne: 'nesw-resize', sw: 'nesw-resize',
        n: 'ns-resize', s: 'ns-resize',
        e: 'ew-resize', w: 'ew-resize',
      };
      canvas.style.cursor = cursors[handle];
      return;
    }
    canvas.style.cursor = this._partAt(point) ? 'move' : 'crosshair';
  }

  _resized(origin, handle, image) {
    let [x, y, w, h] = origin;
    const right = x + w;
    const bottom = y + h;
    const px = Math.round(image.x);
    const py = Math.round(image.y);

    if (handle.includes('w')) { x = Math.min(px, right - MIN_BOX); w = right - x; }
    if (handle.includes('e')) { w = Math.max(MIN_BOX, px - x); }
    if (handle.includes('n')) { y = Math.min(py, bottom - MIN_BOX); h = bottom - y; }
    if (handle.includes('s')) { h = Math.max(MIN_BOX, py - y); }

    return [x, y, w, h];
  }

  _clampBox([x, y, w, h]) {
    const maxW = this.image ? this.image.naturalWidth : w;
    const maxH = this.image ? this.image.naturalHeight : h;
    w = Math.max(MIN_BOX, Math.min(w, maxW));
    h = Math.max(MIN_BOX, Math.min(h, maxH));
    x = Math.max(0, Math.min(x, maxW - w));
    y = Math.max(0, Math.min(y, maxH - h));
    return [x, y, w, h];
  }

  // ---- keyboard ----------------------------------------------------------

  _bindKeyboard() {
    const typing = (target) =>
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      target instanceof HTMLSelectElement ||
      target?.isContentEditable;

    window.addEventListener('keydown', (event) => {
      if (event.code === 'Space' && !typing(event.target)) {
        this.spaceDown = true;
        this.host.classList.add('is-pannable');
        event.preventDefault();
      }
      if (!this.isActive || !this.isActive()) return;
      if (typing(event.target)) return;

      const key = event.key.toLowerCase();

      if ((event.ctrlKey || event.metaKey) && key === 'z') {
        event.preventDefault();
        if (event.shiftKey) this.redo();
        else this.undo();
        return;
      }
      if ((event.ctrlKey || event.metaKey) && key === 'y') {
        event.preventDefault();
        this.redo();
        return;
      }
      if ((event.key === 'Delete' || event.key === 'Backspace') && this.selectedId) {
        event.preventDefault();
        this.deletePart(this.selectedId);
        return;
      }
      if (event.key === 'F2' && this.selectedId) {
        event.preventDefault();
        this.onRenameRequest(this.selectedId);
        return;
      }
      if (event.key === 'Escape' && this.selectedId) {
        this.select(null);
        return;
      }
      if (key === '+' || key === '=') { this.view.zoomBy(1.25); return; }
      if (key === '-') { this.view.zoomBy(0.8); return; }
      if (key === '0') { this.view.fit(); return; }

      // Arrow keys nudge the selected box, which is the only practical way to
      // place a box exactly on a 20 px passive.
      if (this.selectedId && event.key.startsWith('Arrow')) {
        event.preventDefault();
        const part = this.parts.find((p) => p.id === this.selectedId);
        if (!part) return;
        const step = event.shiftKey ? 10 : 1;
        const delta = {
          ArrowLeft: [-step, 0], ArrowRight: [step, 0],
          ArrowUp: [0, -step], ArrowDown: [0, step],
        }[event.key];
        this._snapshot();
        part.bbox = this._clampBox([
          part.bbox[0] + delta[0], part.bbox[1] + delta[1], part.bbox[2], part.bbox[3],
        ]);
        this._emit();
      }
    });

    window.addEventListener('keyup', (event) => {
      if (event.code === 'Space') {
        this.spaceDown = false;
        this.host.classList.remove('is-pannable');
      }
    });
  }

  // ---- drawing -----------------------------------------------------------

  requestDraw() {
    if (this._frame) return;
    this._frame = requestAnimationFrame(() => {
      this._frame = null;
      this.draw();
    });
  }

  draw() {
    const { ctx, scale } = this.view;
    this.view.clear();
    if (!this.image) return;

    // Above 2x, show the real pixels rather than a smoothed guess: the
    // operator is zoomed in to place a box on a tiny part, and interpolation
    // moves the apparent edge.
    ctx.imageSmoothingEnabled = scale <= 2;
    ctx.drawImage(
      this.image,
      this.view.tx, this.view.ty,
      this.image.naturalWidth * scale, this.image.naturalHeight * scale,
    );

    const brand = cssVar('--brand', '#6e8bff');
    const ok = cssVar('--ok', '#2bd47d');

    for (const part of this.parts) {
      const rect = this._screenRect(part);
      const selected = part.id === this.selectedId;
      const colour = selected ? brand : ok;

      ctx.save();
      roundedRectPath(ctx, rect.x, rect.y, rect.w, rect.h, 3);
      ctx.fillStyle = selected ? 'rgba(110,139,255,0.17)' : 'rgba(43,212,125,0.10)';
      ctx.fill();
      ctx.lineWidth = selected ? 2 : 1.4;
      ctx.strokeStyle = colour;
      ctx.stroke();
      ctx.restore();

      if (rect.w > 26 || selected) {
        drawLabel(ctx, part.name || 'Unnamed', rect.x, rect.y, colour, {
          canvasWidth: this.view.width,
        });
      }

      if (selected) this._drawHandles(ctx, rect, brand);
    }

    if (this.draft) {
      const a = this.view.toScreen(this.draft.x0, this.draft.y0);
      const b = this.view.toScreen(this.draft.x1, this.draft.y1);
      ctx.save();
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = brand;
      ctx.fillStyle = 'rgba(110,139,255,0.13)';
      const x = Math.min(a.x, b.x);
      const y = Math.min(a.y, b.y);
      ctx.fillRect(x, y, Math.abs(b.x - a.x), Math.abs(b.y - a.y));
      ctx.strokeRect(x, y, Math.abs(b.x - a.x), Math.abs(b.y - a.y));
      ctx.restore();
    }

    if (this.emptyHint) {
      this.emptyHint.classList.toggle('is-hidden', this.parts.length > 0);
    }
  }

  _drawHandles(ctx, rect, colour) {
    const points = this._handlePoints(rect);
    ctx.save();
    ctx.fillStyle = '#ffffff';
    ctx.strokeStyle = colour;
    ctx.lineWidth = 1.6;
    for (const key of HANDLES) {
      const point = points[key];
      ctx.beginPath();
      ctx.rect(
        point.x - HANDLE_SIZE / 2, point.y - HANDLE_SIZE / 2,
        HANDLE_SIZE, HANDLE_SIZE,
      );
      ctx.fill();
      ctx.stroke();
    }
    ctx.restore();
  }
}
