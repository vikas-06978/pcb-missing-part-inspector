/**
 * Shared pan / zoom plumbing for the two canvases (part editor, result viewer).
 *
 * Keeps one source of truth for the image -> screen mapping so that hit
 * testing, drawing and the zoom readout can never disagree, and handles the
 * device-pixel-ratio bookkeeping that otherwise makes everything look soft on
 * a high-DPI laptop -- which is most of them.
 */

export class CanvasView {
  constructor(canvas, host) {
    this.canvas = canvas;
    this.host = host;
    this.ctx = canvas.getContext('2d');

    this.contentWidth = 0;
    this.contentHeight = 0;

    this.scale = 1;
    this.tx = 0;
    this.ty = 0;

    this.minScale = 0.05;
    this.maxScale = 24;

    this.width = 0;   // CSS pixels
    this.height = 0;

    this._animation = null;
    this.userMoved = false;

    this.onChange = () => {};

    this._observer = new ResizeObserver(() => this.resize());
    this._observer.observe(host);
    this.resize();
  }

  destroy() {
    this.cancelAnimation();
    this._observer.disconnect();
  }

  // ---- animation ---------------------------------------------------------

  cancelAnimation() {
    if (this._animation) {
      cancelAnimationFrame(this._animation);
      this._animation = null;
    }
  }

  /**
   * Ease the viewport to a target transform.
   *
   * Jumping the canvas instantly loses the reader: after "zoom to fit" or
   * clicking a part in the list, you have to re-find where you were. A short
   * eased move keeps the board's identity continuous, which is why every
   * design tool does it.
   */
  animateTo(scale, tx, ty, duration = 300) {
    this.cancelAnimation();

    const fromScale = this.scale;
    const fromTx = this.tx;
    const fromTy = this.ty;

    // Nothing meaningful to animate: just set it.
    if (
      Math.abs(fromScale - scale) < 1e-4 &&
      Math.abs(fromTx - tx) < 0.5 &&
      Math.abs(fromTy - ty) < 0.5
    ) {
      return;
    }

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduced || duration <= 0) {
      this.scale = scale;
      this.tx = tx;
      this.ty = ty;
      this.onChange();
      return;
    }

    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      // easeOutCubic: fast commit, gentle settle.
      const e = 1 - Math.pow(1 - t, 3);
      this.scale = fromScale + (scale - fromScale) * e;
      this.tx = fromTx + (tx - fromTx) * e;
      this.ty = fromTy + (ty - fromTy) * e;
      this.onChange();
      this._animation = t < 1 ? requestAnimationFrame(step) : null;
    };
    this._animation = requestAnimationFrame(step);
  }

  /** Match the backing store to the element size and the pixel ratio. */
  resize() {
    const rect = this.host.getBoundingClientRect();
    if (!rect.width || !rect.height) return;

    const ratio = window.devicePixelRatio || 1;
    this.width = rect.width;
    this.height = rect.height;

    this.canvas.width = Math.round(rect.width * ratio);
    this.canvas.height = Math.round(rect.height * ratio);
    this.canvas.style.width = `${rect.width}px`;
    this.canvas.style.height = `${rect.height}px`;

    this.ctx.setTransform(ratio, 0, 0, ratio, 0, 0);

    // Until the user has panned or zoomed themselves, keep the whole board in
    // view as the panel resizes. Without this, anything that changes the
    // layout after the first fit -- a help note appearing, the window being
    // resized -- silently leaves the board cropped.
    if (!this.userMoved && this.contentWidth) this.fit();
    else this.onChange();
  }

  setContent(width, height) {
    this.contentWidth = width;
    this.contentHeight = height;
  }

  /** Scale the content to fit, centred, with a little breathing room. */
  fit(padding = 24, { animate = false } = {}) {
    if (!this.contentWidth || !this.contentHeight || !this.width) return;
    const scale = Math.max(
      Math.min(
        (this.width - padding * 2) / this.contentWidth,
        (this.height - padding * 2) / this.contentHeight,
      ),
      this.minScale,
    );
    const tx = (this.width - this.contentWidth * scale) / 2;
    const ty = (this.height - this.contentHeight * scale) / 2;
    this.userMoved = false;

    if (animate) {
      this.animateTo(scale, tx, ty);
      return;
    }
    this.cancelAnimation();
    this.scale = scale;
    this.tx = tx;
    this.ty = ty;
    this.onChange();
  }

  /** Zoom to an exact factor about the viewport centre. */
  zoomTo(scale, { animate = true } = {}) {
    this.userMoved = true;
    const next = Math.min(this.maxScale, Math.max(this.minScale, scale));
    const cx = this.width / 2;
    const cy = this.height / 2;
    const point = this.toImage(cx, cy);
    const tx = cx - point.x * next;
    const ty = cy - point.y * next;
    if (animate) {
      this.animateTo(next, tx, ty);
      return;
    }
    this.cancelAnimation();
    this.scale = next;
    this.tx = tx;
    this.ty = ty;
    this.onChange();
  }

  /** Frame an arbitrary image-space rectangle, like Figma's zoom-to-selection. */
  zoomToRect(x, y, width, height, { padding = 80, animate = true, maxScale = 8 } = {}) {
    this.userMoved = true;
    if (!this.width || width <= 0 || height <= 0) return;
    const scale = Math.min(
      this.maxScale,
      Math.max(
        this.minScale,
        Math.min(
          (this.width - padding * 2) / width,
          (this.height - padding * 2) / height,
          maxScale,
        ),
      ),
    );
    const tx = this.width / 2 - (x + width / 2) * scale;
    const ty = this.height / 2 - (y + height / 2) * scale;
    if (animate) {
      this.animateTo(scale, tx, ty);
      return;
    }
    this.cancelAnimation();
    this.scale = scale;
    this.tx = tx;
    this.ty = ty;
    this.onChange();
  }

  toScreen(x, y) {
    return { x: x * this.scale + this.tx, y: y * this.scale + this.ty };
  }

  toImage(x, y) {
    return { x: (x - this.tx) / this.scale, y: (y - this.ty) / this.scale };
  }

  /** Zoom about a screen point, so the pixel under the cursor stays put. */
  zoomAt(factor, screenX, screenY) {
    this.userMoved = true;
    const next = Math.min(this.maxScale, Math.max(this.minScale, this.scale * factor));
    if (next === this.scale) return;
    this.cancelAnimation();
    const before = this.toImage(screenX, screenY);
    this.scale = next;
    this.tx = screenX - before.x * this.scale;
    this.ty = screenY - before.y * this.scale;
    this.onChange();
  }

  zoomBy(factor) {
    this.zoomTo(this.scale * factor);
  }

  panBy(dx, dy) {
    this.userMoved = true;
    this.cancelAnimation();
    this.tx += dx;
    this.ty += dy;
    this.onChange();
  }

  /** Centre a region of the image, zooming in if it is very small. */
  focusOn(x, y, width, height, padding = 110, { animate = true } = {}) {
    this.zoomToRect(x, y, width, height, { padding, animate, maxScale: 7 });
  }

  /** Pointer position in CSS pixels relative to the canvas. */
  pointer(event) {
    const rect = this.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  clear() {
    this.ctx.clearRect(0, 0, this.width, this.height);
  }
}

/** Rounded rectangle path, used by both canvases. */
export function roundedRectPath(ctx, x, y, width, height, radius) {
  const r = Math.max(0, Math.min(radius, Math.abs(width) / 2, Math.abs(height) / 2));
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

/** A label chip that stays inside the canvas even when the box is at an edge. */
export function drawLabel(ctx, text, x, y, colour, options = {}) {
  const { fontSize = 11.5, canvasWidth = Infinity } = options;
  ctx.font = `600 ${fontSize}px -apple-system, "Segoe UI", Roboto, sans-serif`;
  const paddingX = 6;
  const width = ctx.measureText(text).width + paddingX * 2;
  const height = fontSize + 8;

  let left = x;
  let top = y - height - 4;
  if (top < 2) top = y + 4;                       // flip below when clipped at the top
  if (left + width > canvasWidth - 2) left = canvasWidth - width - 2;
  if (left < 2) left = 2;

  ctx.fillStyle = colour;
  roundedRectPath(ctx, left, top, width, height, 4);
  ctx.fill();

  ctx.fillStyle = '#ffffff';
  ctx.textBaseline = 'middle';
  ctx.fillText(text, left + paddingX, top + height / 2 + 0.5);
}
