/**
 * Part inspector: lift one component off the board, turn it, name it.
 *
 * Double-clicking a box flies it out of the board into the middle of the
 * screen as a slab you can rotate by dragging, with the name field on the
 * slab itself. The point is not decoration. Naming forty parts means forty
 * times deciding "which component is this, and what does the silkscreen next
 * to it say", and on a 20 px passive at 70% zoom that is genuinely hard. This
 * puts the part big, lit and in front of you, with the field right there.
 *
 * An honest note about the "3D", because it matters in an inspection tool.
 * We have exactly one photograph, taken from above. The sides of a component
 * were never captured and cannot be recovered, so they are *not* drawn as if
 * they had been: the top face is the real photographed pixels, and the edges
 * are a plain neutral material with no invented detail. Tilting it shows you
 * a photograph on a slab, which is what it is. Anything else would be the
 * tool inventing evidence, which is the one thing it must never do.
 */

import { loadImage } from './ui.js';

const MAX_TILT = 26;      // degrees; past this the top face foreshortens away
const SPRING_BACK_MS = 420;

export class PartView {
  constructor({ root, onRename, onRequestParts, onFocusPart }) {
    this.root = root;
    this.onRename = onRename || (() => {});
    this.onRequestParts = onRequestParts || (() => []);
    this.onFocusPart = onFocusPart || (() => {});

    this.image = null;
    this.partId = null;
    this.rotX = -14;
    this.rotY = 18;
    this.zoom = 1;
    this.drag = null;

    this.scene = root.querySelector('.partview__scene');
    this.slab = root.querySelector('.partview__slab');
    this.topCanvas = root.querySelector('#partview-top');
    this.nameInput = root.querySelector('#partview-name');

    this._bind();
  }

  setImage(image) {
    this.image = image;
  }

  get isOpen() {
    return this.root.classList.contains('is-open');
  }

  // ---- open / close ------------------------------------------------------

  open(partId, fromRect) {
    const part = this.onRequestParts().find((p) => p.id === partId);
    if (!part || !this.image) return;

    this.partId = partId;
    this.rotX = -14;
    this.rotY = 18;
    this.zoom = 1;

    this._paint(part);
    this._fill(part);

    this.root.classList.add('is-open');
    this.root.removeAttribute('inert');

    // Fly out of the board: start the slab at the box's place on the canvas,
    // then let the transition carry it to the middle. Without this the part
    // just appears, and you lose track of which one you opened.
    if (fromRect) {
      const target = this.scene.getBoundingClientRect();
      const dx = (fromRect.left + fromRect.width / 2) - (target.left + target.width / 2);
      const dy = (fromRect.top + fromRect.height / 2) - (target.top + target.height / 2);
      const scale = Math.max(0.12, Math.min(1, fromRect.width / Math.max(target.width, 1)));
      this.slab.style.transition = 'none';
      this.slab.style.transform =
        `translate3d(${dx}px, ${dy}px, 0) scale(${scale}) rotateX(0deg) rotateY(0deg)`;
      requestAnimationFrame(() => {
        this.slab.style.transition = '';
        this._applyTransform();
      });
    } else {
      this._applyTransform();
    }

    requestAnimationFrame(() => {
      this.nameInput.focus();
      this.nameInput.select();
    });
  }

  close() {
    if (!this.isOpen) return;
    this._commitName();
    this.root.classList.remove('is-open');
    this.root.setAttribute('inert', '');
    this.partId = null;
  }

  // ---- content -----------------------------------------------------------

  /** Draw the part's own pixels, with a little context, onto the top face. */
  _paint(part) {
    const [x, y, w, h] = part.bbox;
    const pad = Math.round(Math.max(w, h) * 0.22) + 4;

    const sx = Math.max(0, x - pad);
    const sy = Math.max(0, y - pad);
    const sw = Math.min(this.image.naturalWidth - sx, w + pad * 2);
    const sh = Math.min(this.image.naturalHeight - sy, h + pad * 2);

    // Render at a fixed size so every part arrives the same size on screen,
    // however big it is on the board. A 0402 and a QFP both get a good look.
    const longest = 420;
    const scale = longest / Math.max(sw, sh);
    const canvas = this.topCanvas;
    const ratio = window.devicePixelRatio || 1;

    canvas.width = Math.round(sw * scale * ratio);
    canvas.height = Math.round(sh * scale * ratio);
    canvas.style.width = `${Math.round(sw * scale)}px`;
    canvas.style.height = `${Math.round(sh * scale)}px`;

    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = scale < 2;
    ctx.drawImage(this.image, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);

    // Outline where the box actually sits, so a wide context crop still shows
    // exactly what was selected.
    ctx.save();
    ctx.strokeStyle = 'rgba(47, 111, 235, 0.95)';
    ctx.lineWidth = Math.max(2, 2 * ratio);
    ctx.strokeRect(
      (x - sx) * scale * ratio, (y - sy) * scale * ratio,
      w * scale * ratio, h * scale * ratio,
    );
    ctx.restore();

    this.slab.style.setProperty('--slab-w', `${Math.round(sw * scale)}px`);
    this.slab.style.setProperty('--slab-h', `${Math.round(sh * scale)}px`);
  }

  _fill(part) {
    const parts = this.onRequestParts();
    const index = parts.findIndex((p) => p.id === part.id);
    const unnamed = /^part \d+$/i.test(part.name.trim());

    this.nameInput.value = unnamed ? '' : part.name;
    this.nameInput.placeholder = unnamed ? part.name : 'Name this part';

    this.root.querySelector('#partview-pos').textContent =
      `${part.bbox[0]}, ${part.bbox[1]}`;
    this.root.querySelector('#partview-size').textContent =
      `${part.bbox[2]} x ${part.bbox[3]} px`;
    // How much is left, not just where you are. Working through forty parts
    // one slab at a time, "12 of 41" says nothing about how many of the other
    // twenty-nine still need typing, and that is the question being asked.
    const left = parts.filter((p) => /^part \d+$/i.test(p.name.trim())).length;
    this.root.querySelector('#partview-index').textContent =
      left ? `${index + 1} of ${parts.length} · ${left} to name`
           : `${index + 1} of ${parts.length} · all named`;
    this.root.querySelector('#partview-state').textContent =
      unnamed ? 'Not named yet' : 'Named';
    this.root.querySelector('#partview-state').classList.toggle('is-unnamed', unnamed);
  }

  _commitName() {
    if (!this.partId) return;
    const value = this.nameInput.value.trim();
    if (value) this.onRename(this.partId, value);
  }

  /** Move to the next part, preferring ones that still need a name. */
  step(direction, onlyUnnamed = false) {
    this._commitName();
    const parts = this.onRequestParts();
    if (!parts.length) return;

    const current = parts.findIndex((p) => p.id === this.partId);
    const count = parts.length;

    for (let i = 1; i <= count; i += 1) {
      const next = parts[(current + direction * i + count * i) % count];
      if (!next) continue;
      if (onlyUnnamed && !/^part \d+$/i.test(next.name.trim())) continue;
      this.partId = next.id;
      this._paint(next);
      this._fill(next);
      this.onFocusPart(next.id);
      this.nameInput.focus();
      this.nameInput.select();
      return;
    }
    // Everything is named: just move by one.
    const next = parts[(current + direction + count) % count];
    this.partId = next.id;
    this._paint(next);
    this._fill(next);
    this.onFocusPart(next.id);
  }

  // ---- rotation ----------------------------------------------------------

  _applyTransform() {
    this.slab.style.transform =
      `rotateX(${this.rotX}deg) rotateY(${this.rotY}deg) scale(${this.zoom})`;
  }

  _bind() {
    const scene = this.scene;

    scene.addEventListener('pointerdown', (event) => {
      if (event.target.closest('.partview__card')) return;
      scene.setPointerCapture(event.pointerId);
      this.drag = { x: event.clientX, y: event.clientY, moved: false };
      this.slab.style.transition = 'none';
      scene.classList.add('is-turning');
    });

    scene.addEventListener('pointermove', (event) => {
      if (!this.drag) return;
      const dx = event.clientX - this.drag.x;
      const dy = event.clientY - this.drag.y;
      this.drag.x = event.clientX;
      this.drag.y = event.clientY;
      if (Math.abs(dx) + Math.abs(dy) > 2) this.drag.moved = true;

      // Clamped rather than free: past about 30 degrees the photographed face
      // foreshortens into uselessness, and this exists to look at that face.
      this.rotY = clamp(this.rotY + dx * 0.4, -MAX_TILT * 1.6, MAX_TILT * 1.6);
      this.rotX = clamp(this.rotX - dy * 0.4, -MAX_TILT, MAX_TILT);
      this._applyTransform();
    });

    const release = () => {
      if (!this.drag) return;
      this.drag = null;
      scene.classList.remove('is-turning');
      this.slab.style.transition = `transform ${SPRING_BACK_MS}ms cubic-bezier(.16,1,.3,1)`;
    };
    scene.addEventListener('pointerup', release);
    scene.addEventListener('pointercancel', release);

    scene.addEventListener('dblclick', () => {
      this.rotX = -14;
      this.rotY = 18;
      this.zoom = 1;
      this._applyTransform();
    });

    scene.addEventListener('wheel', (event) => {
      event.preventDefault();
      this.zoom = clamp(this.zoom * Math.exp(-event.deltaY * 0.0012), 0.5, 2.4);
      this._applyTransform();
    }, { passive: false });

    this.root.querySelector('#partview-close').addEventListener('click', () => this.close());
    this.root.querySelector('#partview-prev').addEventListener('click', () => this.step(-1));
    this.root.querySelector('#partview-next').addEventListener('click', () => this.step(1));
    this.root.querySelector('#partview-next-unnamed')
      .addEventListener('click', () => this.step(1, true));

    this.root.addEventListener('pointerdown', (event) => {
      if (event.target === this.root) this.close();
    });

    this.nameInput.addEventListener('keydown', (event) => {
      event.stopPropagation();
      if (event.key === 'Enter') {
        event.preventDefault();
        this.step(1, !event.shiftKey);
      } else if (event.key === 'Escape') {
        event.preventDefault();
        this.close();
      }
    });

    window.addEventListener('keydown', (event) => {
      if (!this.isOpen) return;
      if (event.key === 'Escape') { event.preventDefault(); this.close(); }
    });
  }
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}
