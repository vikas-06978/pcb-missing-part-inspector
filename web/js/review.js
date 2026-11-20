/**
 * Name review: every part as a picture, with the name you gave it underneath.
 *
 * The part list is the right tool for typing names and the wrong one for
 * checking them. Forty rows of text all look correct, including the three
 * where the name of the part above ended up on the part below, which is the
 * mistake this workflow actually produces: you type down a list while looking
 * at a board, and one slip puts every later name one row out. Nothing in a
 * list of text can show you that. A grid of photographs with names on them
 * shows it immediately, because a capacitor labelled R4 looks wrong.
 *
 * So this is deliberately not a summary. It is the same pixels the engine
 * will compare, cropped to each box, at a size you can actually judge.
 */

const THUMB = 132;

export class Review {
  constructor({ root, onRequestParts, onRename, onOpenPart }) {
    this.root = root;
    this.onRequestParts = onRequestParts || (() => []);
    this.onRename = onRename || (() => {});
    this.onOpenPart = onOpenPart || (() => {});

    this.image = null;
    this.grid = root.querySelector('#review-grid');
    this.sub = root.querySelector('#review-sub');
    this.empty = root.querySelector('#review-empty');
    this.onlyUnnamed = root.querySelector('#review-only-unnamed');

    this.onlyUnnamed.addEventListener('change', () => this.render());
    root.querySelector('#review-close').addEventListener('click', () => this.close());
    root.querySelector('#review-done').addEventListener('click', () => this.close());
    root.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') { event.preventDefault(); this.close(); }
    });
  }

  setImage(image) {
    this.image = image;
  }

  get isOpen() {
    return this.root.classList.contains('is-open');
  }

  open() {
    this.root.classList.add('is-open');
    this.root.removeAttribute('inert');
    this.render();
    requestAnimationFrame(() => this.root.querySelector('#review-done')?.focus());
  }

  close() {
    if (!this.isOpen) return;
    this.root.classList.remove('is-open');
    this.root.setAttribute('inert', '');
  }

  /** True when the app generated this name rather than a person choosing it. */
  static isPlaceholder(name) {
    return /^part\s*\d+$/i.test(String(name).trim());
  }

  render() {
    const all = this.onRequestParts();
    const unnamed = all.filter((p) => Review.isPlaceholder(p.name)).length;
    const parts = this.onlyUnnamed.checked
      ? all.filter((p) => Review.isPlaceholder(p.name))
      : all;

    this.sub.textContent = unnamed
      ? `${all.length - unnamed} named, ${unnamed} still to go. `
        + 'Click any part to open it up close.'
      : `All ${all.length} named. Check each name against its picture.`;

    this.grid.innerHTML = '';
    this.empty.classList.toggle('is-hidden', parts.length > 0);

    parts.forEach((part) => {
      const index = all.indexOf(part);
      const placeholder = Review.isPlaceholder(part.name);
      const card = document.createElement('div');
      card.className = 'review-card' + (placeholder ? ' is-unnamed' : '');
      card.dataset.id = part.id;
      card.innerHTML = `
        <button class="review-card__thumb" type="button">
          <canvas></canvas>
        </button>
        <input class="review-card__name" maxlength="40" spellcheck="false"
               aria-label="Name for part ${index + 1}">
        <span class="review-card__index">${index + 1} of ${all.length}</span>`;

      // As a property rather than in the template: see the same note on the
      // part list. Names can arrive from an imported project file.
      card.querySelector('.review-card__thumb').title = `Open ${part.name} up close`;

      const input = card.querySelector('.review-card__name');
      input.value = placeholder ? '' : part.name;
      input.placeholder = placeholder ? part.name : '';
      input.addEventListener('change', () => {
        const value = input.value.trim();
        if (value && value !== part.name) this.onRename(part.id, value);
        this.render();
      });
      input.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter') return;
        event.preventDefault();
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });

      card.querySelector('.review-card__thumb')
        .addEventListener('click', () => this.onOpenPart(part.id));

      this.grid.appendChild(card);
      this._paint(card.querySelector('canvas'), part);
    });
  }

  /** Crop this part out of the reference, with enough around it to place it. */
  _paint(canvas, part) {
    if (!this.image) return;
    const [x, y, w, h] = part.bbox;
    const pad = Math.round(Math.max(w, h) * 0.3) + 4;
    const sx = Math.max(0, x - pad);
    const sy = Math.max(0, y - pad);
    const sw = Math.min(this.image.naturalWidth - sx, w + pad * 2);
    const sh = Math.min(this.image.naturalHeight - sy, h + pad * 2);
    if (sw <= 0 || sh <= 0) return;

    // Every part arrives the same size on screen whatever its size on the
    // board, so a 0402 is as judgeable as a QFP.
    const ratio = window.devicePixelRatio || 1;
    const scale = THUMB / Math.max(sw, sh);
    canvas.width = Math.round(sw * scale * ratio);
    canvas.height = Math.round(sh * scale * ratio);
    canvas.style.width = `${Math.round(sw * scale)}px`;
    canvas.style.height = `${Math.round(sh * scale)}px`;

    const ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = scale < 2;
    ctx.drawImage(this.image, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);

    // Mark the box itself, so a generous crop still shows what was selected.
    ctx.strokeStyle = 'rgba(47, 111, 235, 0.95)';
    ctx.lineWidth = Math.max(1.5, 1.5 * ratio);
    ctx.strokeRect(
      (x - sx) * scale * ratio, (y - sy) * scale * ratio,
      w * scale * ratio, h * scale * ratio,
    );
  }
}
