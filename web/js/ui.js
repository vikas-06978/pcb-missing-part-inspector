/** Small shared interface primitives: toasts, confirmations, formatting. */

const toastHost = () => document.getElementById('toasts');

/**
 * Show a transient message.
 *
 * Errors stay noticeably longer than confirmations: an operator who glances
 * away must not miss the reason something failed, whereas "Saved" can go.
 */
export function toast(title, body = '', kind = 'info') {
  const host = toastHost();
  if (!host) return;

  const node = document.createElement('div');
  node.className = `toast toast--${kind}`;
  node.innerHTML = `
    <span class="toast__bar"></span>
    <div class="toast__text">
      <b></b>
      ${body ? '<span></span>' : ''}
    </div>`;
  node.querySelector('b').textContent = title;
  if (body) node.querySelector('span:last-child').textContent = body;

  host.appendChild(node);

  const lifetime = kind === 'error' ? 8000 : 3800;
  const timer = setTimeout(() => dismiss(node), lifetime);
  node.addEventListener('click', () => {
    clearTimeout(timer);
    dismiss(node);
  });
}

function dismiss(node) {
  node.classList.add('is-leaving');
  node.addEventListener('animationend', () => node.remove(), { once: true });
}

/**
 * Promise-based confirmation using a real <dialog>.
 *
 * Deliberately not window.confirm: a native dialog cannot say which board is
 * about to lose its parts, and the destructive action here (replacing a
 * reference) throws away work the operator spent several minutes on.
 */
export function confirmAction({ title, body, confirmLabel = 'Confirm', danger = false }) {
  return new Promise((resolve) => {
    const dialog = document.createElement('dialog');
    dialog.className = 'sheet';
    dialog.innerHTML = `
      <h2></h2>
      <p class="sheet__lead"></p>
      <div class="row row--end">
        <button class="btn btn--ghost" value="cancel" type="button">Cancel</button>
        <button class="btn ${danger ? '' : 'btn--primary'}" value="ok" type="button"></button>
      </div>`;

    dialog.querySelector('h2').textContent = title;
    dialog.querySelector('.sheet__lead').textContent = body;

    const okButton = dialog.querySelector('[value="ok"]');
    okButton.textContent = confirmLabel;
    if (danger) {
      okButton.style.background = 'var(--missing)';
      okButton.style.borderColor = 'transparent';
      okButton.style.color = '#fff';
    }

    const close = (result) => {
      dialog.close();
      dialog.remove();
      resolve(result);
    };

    okButton.addEventListener('click', () => close(true));
    dialog.querySelector('[value="cancel"]').addEventListener('click', () => close(false));
    dialog.addEventListener('cancel', (event) => {
      event.preventDefault();
      close(false);
    });

    document.body.appendChild(dialog);
    dialog.showModal();
    okButton.focus();
  });
}

/** Read a themed colour token so canvas drawing matches the stylesheet. */
export function cssVar(name, fallback = '#888') {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name);
  return value.trim() || fallback;
}

export function stateColour(state) {
  if (state === 'missing') return cssVar('--missing', '#fb5d6d');
  if (state === 'review') return cssVar('--review', '#f5a524');
  return cssVar('--ok', '#2bd47d');
}

export function formatBytes(bytes) {
  if (!bytes) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

export function formatTime(seconds) {
  return new Date(seconds * 1000).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** Load a URL into an HTMLImageElement, resolving only once it has decoded. */
export function loadImage(src) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`Could not load image: ${src}`));
    image.src = src;
  });
}

export function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoke on the next frame so the download has certainly started.
  requestAnimationFrame(() => URL.revokeObjectURL(url));
}

/** Filter a FileList down to usable images, warning about what was dropped. */
export function imageFilesFrom(fileList) {
  const files = Array.from(fileList || []);
  const images = files.filter((file) => file.type.startsWith('image/'));
  if (images.length < files.length) {
    toast(
      'Some files were ignored',
      `${files.length - images.length} file(s) were not images.`,
      'warn',
    );
  }
  return images;
}
