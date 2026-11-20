/**
 * Interactive box editor for naming components on the reference board.
 *
 * The brief has the operator point out and name each part once, and at the
 * live demo someone who has never seen this app does exactly that while being
 * watched. So the canvas deliberately copies the conventions people already
 * have in their fingers from Figma, Illustrator and every map on the web:
 *
 *   scroll / two-finger drag ....... pan
 *   ctrl or cmd + scroll, pinch .... zoom at the pointer
 *   space drag, middle drag, right drag ... pan
 *   double-click empty board ....... fit the whole board back in view
 *   drag on empty .................. new part
 *   shift + drag on empty .......... marquee select
 *   shift-click .................... add or remove from selection
 *   alt + drag ..................... duplicate
 *   arrows / shift+arrows .......... nudge 1 px / 10 px
 *   ctrl+A, ctrl+D, Del, F2 ........ select all, duplicate, delete, rename
 *   shift+1 / shift+2 / shift+0 .... zoom fit / to selection / 100%
 *
 * There is deliberately no tool palette. Figma needs one because it draws a
 * dozen kinds of object; here there is exactly one, and a mode switch the user
 * can leave in the wrong state is a trap, not a feature. Dragging on empty
 * board always draws a part, which is the task; holding shift marquee-selects,
 * which matches shift-click already meaning "extend the selection".
 *
 * Renaming happens in the side list rather than in an in-canvas text field: a
 * real input gets native caret handling, selection and IME for free.
 */

import { CanvasView, drawLabel, roundedRectPath } from './view.js';
import { cssVar } from './ui.js';

const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'];
const HANDLE_HIT = 9;        // screen px
const HANDLE_SIZE = 7;       // screen px
const MIN_BOX = 6;           // image px
const MAX_HISTORY = 80;
const SNAP_SCREEN_PX = 6;    // snapping tolerance, in screen px so it feels
                             // the same at every zoom level
const MARQUEE_MIN = 4;       // screen px before a click becomes a drag

let nextLocalId = 1;
const makeId = () => `n${(nextLocalId++).toString(36)}${Date.now().toString(36).slice(-4)}`;

const CURSORS = {
  nw: 'nwse-resize', se: 'nwse-resize',
  ne: 'nesw-resize', sw: 'nesw-resize',
  n: 'ns-resize', s: 'ns-resize',
  e: 'ew-resize', w: 'ew-resize',
};

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
    this.selectedIds = new Set();
    this.hoveredId = null;

    this.mode = 'idle';
    this.draft = null;
    this.marquee = null;
    this.guides = [];
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
    this.selectedIds.clear();
    this._emit();
  }

  getParts() {
    return this.parts.map((part) => ({ ...part, bbox: [...part.bbox] }));
  }

  get count() {
    return this.parts.length;
  }

  /** Back-compat for callers that only care about a single selection. */
  get selectedId() {
    return this.selectedIds.size === 1 ? [...this.selectedIds][0] : null;
  }

  isSelected(id) {
    return this.selectedIds.has(id);
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
    this._pruneSelection();
    this._emit();
    return true;
  }

  redo() {
    if (!this.future.length) return false;
    this.history.push(JSON.stringify(this.parts));
    this.parts = JSON.parse(this.future.pop());
    this._pruneSelection();
    this._emit();
    return true;
  }

  _pruneSelection() {
    const ids = new Set(this.parts.map((p) => p.id));
    for (const id of [...this.selectedIds]) {
      if (!ids.has(id)) this.selectedIds.delete(id);
    }
  }

  addParts(boxes, namePrefix = 'Part') {
    this._snapshot();
    let index = this.parts.length;
    const added = [];
    for (const bbox of boxes) {
      index += 1;
      const part = {
        id: makeId(),
        name: `${namePrefix} ${index}`,
        bbox: this._clampBox(bbox.map(Math.round)),
      };
      this.parts.push(part);
      added.push(part.id);
    }
    // Deliberately leave nothing selected. Auto-detect can add forty boxes at
    // once; if they all arrived selected, one arrow key would move the whole
    // board's annotation, and the canvas would be a wall of highlight before
    // the operator has looked at anything.
    this.selectedIds.clear();
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
    this.selectedIds.delete(id);
    this._emit();
  }

  deleteSelected() {
    if (!this.selectedIds.size) return;
    this._snapshot();
    this.parts = this.parts.filter((p) => !this.selectedIds.has(p.id));
    this.selectedIds.clear();
    this._emit();
  }

  duplicateSelected(offset = 14) {
    if (!this.selectedIds.size) return;
    this._snapshot();
    const copies = [];
    for (const part of this.parts.filter((p) => this.selectedIds.has(p.id))) {
      copies.push({
        id: makeId(),
        name: `${part.name} copy`,
        bbox: this._clampBox([
          part.bbox[0] + offset, part.bbox[1] + offset, part.bbox[2], part.bbox[3],
        ]),
      });
    }
    this.parts.push(...copies);
    this.selectedIds = new Set(copies.map((c) => c.id));
    this._emit();
  }

  clearParts() {
    if (!this.parts.length) return;
    this._snapshot();
    this.parts = [];
    this.selectedIds.clear();
    this._emit();
  }

  selectAll() {
    this.selectedIds = new Set(this.parts.map((p) => p.id));
    this.onSelect(this.selectedId);
    this.requestDraw();
  }

  autoName(prefix = 'Part') {
    if (!this.parts.length) return;
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

  select(id, { additive = false } = {}) {
    if (id === null) {
      this.selectedIds.clear();
    } else if (additive) {
      if (this.selectedIds.has(id)) this.selectedIds.delete(id);
      else this.selectedIds.add(id);
    } else {
      this.selectedIds = new Set([id]);
    }
    this.onSelect(this.selectedId);
    this.requestDraw();
  }

  focusPart(id) {
    const part = this.parts.find((p) => p.id === id);
    if (!part) return;
    const [x, y, w, h] = part.bbox;
    this.view.focusOn(x, y, w, h);
    this.select(id);
  }

  zoomToSelection() {
    const box = this._selectionBounds();
    if (!box) {
      this.view.fit(24, { animate: true });
      return;
    }
    this.view.zoomToRect(box.x, box.y, box.w, box.h, { padding: 110, maxScale: 7 });
  }

  _selectionBounds() {
    const chosen = this.parts.filter((p) => this.selectedIds.has(p.id));
    if (!chosen.length) return null;
    let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
    for (const { bbox } of chosen) {
      x0 = Math.min(x0, bbox[0]);
      y0 = Math.min(y0, bbox[1]);
      x1 = Math.max(x1, bbox[0] + bbox[2]);
      y1 = Math.max(y1, bbox[1] + bbox[3]);
    }
    return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
  }

  _emit() {
    this.onChange(this.getParts());
    this.requestDraw();
  }

  // ---- hit testing -------------------------------------------------------

  /** Where a part's box sits in page coordinates, for fly-out animations. */
  _clientRect(part) {
    const rect = this._screenRect(part);
    const canvas = this.view.canvas.getBoundingClientRect();
    return {
      left: canvas.left + rect.x,
      top: canvas.top + rect.y,
      width: rect.w,
      height: rect.h,
    };
  }

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
    // Resize handles only make sense for exactly one selected box.
    const id = this.selectedId;
    if (!id) return null;
    const part = this.parts.find((p) => p.id === id);
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

  // ---- snapping ----------------------------------------------------------

  /**
   * Nudge a moving rectangle onto its neighbours' edges and centres.
   *
   * Boards are laid out on a grid, so parts genuinely do line up; without
   * snapping, forty hand-drawn boxes end up a pixel or two out from each other
   * and the annotation looks sloppy even when it is accurate enough.
   */
  _snap(box, ignoreIds) {
    const tolerance = SNAP_SCREEN_PX / this.view.scale;
    const [x, y, w, h] = box;

    const vTargets = [0, this.image.naturalWidth];
    const hTargets = [0, this.image.naturalHeight];
    for (const part of this.parts) {
      if (ignoreIds.has(part.id)) continue;
      const [px, py, pw, ph] = part.bbox;
      vTargets.push(px, px + pw / 2, px + pw);
      hTargets.push(py, py + ph / 2, py + ph);
    }

    const pick = (candidates, targets) => {
      let best = null;
      for (const { value, offset } of candidates) {
        for (const target of targets) {
          const delta = target - value;
          if (Math.abs(delta) <= tolerance && (!best || Math.abs(delta) < Math.abs(best.delta))) {
            best = { delta, target, offset };
          }
        }
      }
      return best;
    };

    const vBest = pick(
      [{ value: x, offset: 0 }, { value: x + w / 2, offset: w / 2 }, { value: x + w, offset: w }],
      vTargets,
    );
    const hBest = pick(
      [{ value: y, offset: 0 }, { value: y + h / 2, offset: h / 2 }, { value: y + h, offset: h }],
      hTargets,
    );

    const guides = [];
    let nx = x;
    let ny = y;
    if (vBest) {
      nx = Math.round(vBest.target - vBest.offset);
      guides.push({ axis: 'v', at: vBest.target });
    }
    if (hBest) {
      ny = Math.round(hBest.target - hBest.offset);
      guides.push({ axis: 'h', at: hBest.target });
    }

    this.guides = guides;
    return [nx, ny, w, h];
  }

  // ---- pointer interaction ----------------------------------------------

  _bindPointer() {
    const canvas = this.view.canvas;

    canvas.addEventListener('contextmenu', (event) => event.preventDefault());

    canvas.addEventListener('pointerdown', (event) => {
      if (!this.image) return;
      canvas.setPointerCapture(event.pointerId);
      this.view.cancelAnimation();
      const point = this.view.pointer(event);

      // Pan: middle button, right button, or space held, all three are
      // muscle memory somewhere, and none of them conflict with drawing.
      if (event.button === 1 || event.button === 2 || this.spaceDown) {
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
      const additive = event.shiftKey || event.metaKey || event.ctrlKey;

      if (hit) {
        if (additive) {
          this.select(hit.id, { additive: true });
          if (!this.selectedIds.has(hit.id)) return;   // just removed it
        } else if (!this.selectedIds.has(hit.id)) {
          this.select(hit.id);
        }

        if (event.altKey) this.duplicateSelected(0);
        else this._snapshot();

        const image = this.view.toImage(point.x, point.y);
        this.mode = 'move';
        this._drag = {
          anchor: hit.id,
          grabX: image.x,
          grabY: image.y,
          origins: new Map(
            this.parts
              .filter((p) => this.selectedIds.has(p.id))
              .map((p) => [p.id, [...p.bbox]]),
          ),
        };
        return;
      }

      // Empty canvas.
      if (!additive) this.select(null);
      const image = this.view.toImage(point.x, point.y);

      if (event.shiftKey) {
        this.mode = 'marquee';
        this.marquee = { x0: point.x, y0: point.y, x1: point.x, y1: point.y };
        this._drag = { base: new Set(this.selectedIds) };
      } else {
        this.mode = 'draw';
        this.draft = { x0: image.x, y0: image.y, x1: image.x, y1: image.y };
      }
    });

    canvas.addEventListener('pointermove', (event) => {
      if (!this.image) return;
      const point = this.view.pointer(event);

      if (this.mode === 'idle') {
        const hit = this._partAt(point);
        const hoveredId = hit ? hit.id : null;
        if (hoveredId !== this.hoveredId) {
          this.hoveredId = hoveredId;
          this.requestDraw();
        }
        this._applyCursor(point);
        return;
      }

      if (this.mode === 'pan') {
        this.view.panBy(event.clientX - this._drag.startX, event.clientY - this._drag.startY);
        this._drag.startX = event.clientX;
        this._drag.startY = event.clientY;
        return;
      }

      if (this.mode === 'marquee') {
        this.marquee.x1 = point.x;
        this.marquee.y1 = point.y;
        this._updateMarqueeSelection();
        this.requestDraw();
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
        const { grabX, grabY, origins, anchor } = this._drag;
        let dx = Math.round(image.x - grabX);
        let dy = Math.round(image.y - grabY);

        // Snap using the anchor box, then shift the whole selection by the
        // same correction so relative positions never change.
        const anchorOrigin = origins.get(anchor);
        if (anchorOrigin && !event.altKey) {
          const proposed = [anchorOrigin[0] + dx, anchorOrigin[1] + dy, anchorOrigin[2], anchorOrigin[3]];
          const snapped = this._snap(proposed, this.selectedIds);
          dx += snapped[0] - proposed[0];
          dy += snapped[1] - proposed[1];
        } else {
          this.guides = [];
        }

        for (const part of this.parts) {
          const origin = origins.get(part.id);
          if (!origin) continue;
          part.bbox = this._clampBox([origin[0] + dx, origin[1] + dy, origin[2], origin[3]]);
        }
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
        const dragged = [Math.abs(x1 - x0), Math.abs(y1 - y0)];
        const box = this._clampBox([
          Math.round(Math.min(x0, x1)),
          Math.round(Math.min(y0, y1)),
          Math.round(dragged[0]),
          Math.round(dragged[1]),
        ]);
        this.draft = null;
        // Measure the drag BEFORE clamping. _clampBox enforces a minimum
        // size, so clamping first turned every stray click on the board into
        // a 6x6 part -- invisible, unhittable, and quietly in the part list.
        if (dragged[0] >= MIN_BOX && dragged[1] >= MIN_BOX) {
          this._snapshot();
          const part = { id: makeId(), name: `Part ${this.parts.length + 1}`, bbox: box };
          this.parts.push(part);
          this.selectedIds = new Set([part.id]);
          // From the first box onward the framing is the operator's business.
          // Auto-fit on layout changes would shift the board under their hand.
          this.view.userMoved = true;
          this._emit();
          this.onSelect(part.id);
          this.onRenameRequest(part.id, this._clientRect(part));
        }
      } else if (this.mode === 'marquee') {
        this.marquee = null;
        this.onSelect(this.selectedId);
      } else if (this.mode === 'move' || this.mode === 'resize') {
        this.view.userMoved = true;
        this._emit();
      }

      this.mode = 'idle';
      this._drag = null;
      this.guides = [];
      this.host.classList.remove('is-panning');
      this._applyCursor();
      this.requestDraw();
    };

    canvas.addEventListener('pointerup', finish);
    canvas.addEventListener('pointercancel', finish);

    canvas.addEventListener('dblclick', (event) => {
      const point = this.view.pointer(event);
      const hit = this._partAt(point);
      if (hit) {
        this.select(hit.id);
        this.onRenameRequest(hit.id, this._clientRect(hit));
        return;
      }
      // Double-click on empty board brings the whole thing back into view.
      // Getting lost while zoomed in is the easiest way to feel stuck, and
      // this is the gesture people already try.
      this.view.fit(24, { animate: true });
    });

    canvas.addEventListener('wheel', (event) => {
      if (!this.image) return;
      event.preventDefault();
      const point = this.view.pointer(event);

      // A trackpad pinch arrives as a wheel event with ctrlKey set; a plain
      // two-finger drag does not. Following that distinction gives pinch-zoom
      // and two-finger pan for free, which is what every canvas app does and
      // what a first-time user will try.
      if (event.ctrlKey || event.metaKey) {
        this.view.zoomAt(Math.exp(-event.deltaY * 0.01), point.x, point.y);
        return;
      }

      const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? this.view.height : 1;
      let dx = event.deltaX * unit;
      let dy = event.deltaY * unit;
      if (event.shiftKey && dx === 0) {
        dx = dy;
        dy = 0;
      }
      this.view.panBy(-dx, -dy);
    }, { passive: false });
  }

  _updateMarqueeSelection() {
    const { x0, y0, x1, y1 } = this.marquee;
    if (Math.abs(x1 - x0) < MARQUEE_MIN && Math.abs(y1 - y0) < MARQUEE_MIN) return;

    const left = Math.min(x0, x1);
    const right = Math.max(x0, x1);
    const top = Math.min(y0, y1);
    const bottom = Math.max(y0, y1);

    const next = new Set(this._drag.base);
    for (const part of this.parts) {
      const rect = this._screenRect(part);
      // Touch-select rather than fully-enclose: on a dense board the fully
      // enclosing rule makes you drag across the whole canvas to catch a row.
      const hits =
        rect.x < right && rect.x + rect.w > left &&
        rect.y < bottom && rect.y + rect.h > top;
      if (hits) next.add(part.id);
    }
    this.selectedIds = next;
  }

  _applyCursor(point) {
    const canvas = this.view.canvas;
    if (this.spaceDown || this.mode === 'pan') {
      canvas.style.cursor = this.mode === 'pan' ? 'grabbing' : 'grab';
      return;
    }
    if (point) {
      if (this._handleAt(point)) {
        canvas.style.cursor = CURSORS[this._handleAt(point)];
        return;
      }
      if (this._partAt(point)) {
        canvas.style.cursor = 'move';
        return;
      }
    }
    canvas.style.cursor = 'crosshair';
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
        if (!this.spaceDown) {
          this.spaceDown = true;
          this.host.classList.add('is-pannable');
          this._applyCursor();
        }
        event.preventDefault();
      }
      if (!this.isActive || !this.isActive()) return;
      if (typing(event.target)) return;

      const key = event.key.toLowerCase();
      const accel = event.ctrlKey || event.metaKey;

      if (accel && key === 'z') {
        event.preventDefault();
        if (event.shiftKey) this.redo();
        else this.undo();
        return;
      }
      if (accel && key === 'y') { event.preventDefault(); this.redo(); return; }
      if (accel && key === 'a') { event.preventDefault(); this.selectAll(); return; }
      if (accel && key === 'd') { event.preventDefault(); this.duplicateSelected(); return; }

      if (event.key === 'Delete' || event.key === 'Backspace') {
        if (!this.selectedIds.size) return;
        event.preventDefault();
        this.deleteSelected();
        return;
      }
      if ((event.key === 'F2' || event.key === 'Enter') && this.selectedId) {
        event.preventDefault();
        const part = this.parts.find((p) => p.id === this.selectedId);
        this.onRenameRequest(this.selectedId, part ? this._clientRect(part) : null);
        return;
      }
      if (event.key === 'Escape') {
        if (this.selectedIds.size) this.select(null);
        return;
      }

      if (event.shiftKey && key === '!') { this.view.fit(24, { animate: true }); return; }
      if (event.shiftKey && key === '@') { this.zoomToSelection(); return; }
      if (event.shiftKey && key === ')') { this.view.zoomTo(1); return; }
      if (key === '1' && event.shiftKey) { this.view.fit(24, { animate: true }); return; }
      if (key === '2' && event.shiftKey) { this.zoomToSelection(); return; }
      if (key === '0' && event.shiftKey) { this.view.zoomTo(1); return; }
      if (key === '+' || key === '=') { this.view.zoomBy(1.25); return; }
      if (key === '-') { this.view.zoomBy(0.8); return; }
      if (key === '0') { this.view.fit(24, { animate: true }); return; }

      // Arrow keys nudge the selection, which is the only practical way to
      // place a box exactly on a 20 px passive.
      if (this.selectedIds.size && event.key.startsWith('Arrow')) {
        event.preventDefault();
        const step = event.shiftKey ? 10 : 1;
        const delta = {
          ArrowLeft: [-step, 0], ArrowRight: [step, 0],
          ArrowUp: [0, -step], ArrowDown: [0, step],
        }[event.key];
        this._snapshot();
        for (const part of this.parts) {
          if (!this.selectedIds.has(part.id)) continue;
          part.bbox = this._clampBox([
            part.bbox[0] + delta[0], part.bbox[1] + delta[1], part.bbox[2], part.bbox[3],
          ]);
        }
        this._emit();
      }
    });

    window.addEventListener('keyup', (event) => {
      if (event.code === 'Space') {
        this.spaceDown = false;
        this.host.classList.remove('is-pannable');
        this._applyCursor();
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
    const accent = cssVar('--brand-alt', '#a78bfa');
    const ok = cssVar('--ok', '#2bd47d');

    for (const part of this.parts) {
      const rect = this._screenRect(part);
      const selected = this.selectedIds.has(part.id);
      const hovered = this.hoveredId === part.id && !selected;
      const colour = selected ? brand : ok;

      ctx.save();
      roundedRectPath(ctx, rect.x, rect.y, rect.w, rect.h, 3);
      ctx.fillStyle = selected
        ? 'rgba(110,139,255,0.17)'
        : hovered ? 'rgba(43,212,125,0.18)' : 'rgba(43,212,125,0.10)';
      ctx.fill();
      ctx.lineWidth = selected ? 2 : hovered ? 1.8 : 1.4;
      ctx.strokeStyle = colour;
      ctx.stroke();
      ctx.restore();

      // A label needs the box to be wide enough to own the space under it,
      // otherwise forty of them collide into noise. Selection and hover
      // always win, because then the operator is asking about that one part.
      if (selected || hovered || rect.w >= 54) {
        drawLabel(ctx, part.name || 'Unnamed', rect.x, rect.y, colour, {
          canvasWidth: this.view.width,
        });
      }
    }

    // Handles only for a single selection; for several, outline the group.
    if (this.selectedIds.size === 1) {
      const part = this.parts.find((p) => p.id === this.selectedId);
      if (part) this._drawHandles(ctx, this._screenRect(part), brand);
    } else if (this.selectedIds.size > 1) {
      const box = this._selectionBounds();
      if (box) {
        const topLeft = this.view.toScreen(box.x, box.y);
        ctx.save();
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1.4;
        ctx.strokeStyle = brand;
        ctx.strokeRect(
          topLeft.x - 3, topLeft.y - 3,
          box.w * scale + 6, box.h * scale + 6,
        );
        ctx.restore();
      }
    }

    // Snap guides.
    for (const guide of this.guides) {
      ctx.save();
      ctx.setLineDash([3, 4]);
      ctx.lineWidth = 1;
      ctx.strokeStyle = accent;
      ctx.beginPath();
      if (guide.axis === 'v') {
        const x = this.view.toScreen(guide.at, 0).x;
        ctx.moveTo(x, 0);
        ctx.lineTo(x, this.view.height);
      } else {
        const y = this.view.toScreen(0, guide.at).y;
        ctx.moveTo(0, y);
        ctx.lineTo(this.view.width, y);
      }
      ctx.stroke();
      ctx.restore();
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

    if (this.marquee) {
      const { x0, y0, x1, y1 } = this.marquee;
      ctx.save();
      ctx.lineWidth = 1;
      ctx.strokeStyle = brand;
      ctx.fillStyle = 'rgba(110,139,255,0.10)';
      const x = Math.min(x0, x1);
      const y = Math.min(y0, y1);
      ctx.fillRect(x, y, Math.abs(x1 - x0), Math.abs(y1 - y0));
      ctx.strokeRect(x, y, Math.abs(x1 - x0), Math.abs(y1 - y0));
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
