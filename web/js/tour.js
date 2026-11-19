/**
 * First-run guided tour.
 *
 * The people this is for are not technical, and at the live demo it will be
 * driven by someone who has never seen it. A README they will not read is not
 * an answer; the explanation has to be in the product, once, on first run, and
 * then get out of the way.
 *
 * Deliberately small: four steps, each anchored to a real control, skippable
 * at any point, and re-openable from the menu. A tour that cannot be dismissed
 * is worse than no tour.
 */

const SEEN_KEY = 'pcb-tour-seen';

export const WELCOME_STEPS = [
  {
    target: '#stepper',
    title: 'Three steps, in order',
    body: 'Upload one good board, name the parts on it, then check new boards '
        + 'against it. The app only unlocks a step once the one before it is done.',
    placement: 'bottom',
  },
  {
    target: '#reference-drop, #reference-preview',
    title: 'Start with a board you trust',
    body: 'Drop in a photo of a complete board. Everything is compared against '
        + 'this one, so take it the way your line camera sees the board — square '
        + 'on, evenly lit, in focus.',
    placement: 'top',
  },
  {
    target: '#sensitivity',
    title: 'How fussy should it be?',
    body: 'Balanced suits a fixed camera on a stable line. Move it to '
        + 'Conservative if false alarms are expensive, or Sensitive if you would '
        + 'rather check a few extra boards by hand.',
    placement: 'bottom',
  },
  {
    target: '#menu-btn',
    title: 'Everything else lives here',
    body: 'Save a configured board to a file, load one back, clear history, or '
        + 'reopen this guide. Nothing leaves your computer at any point.',
    placement: 'bottom',
  },
];

export function tourSeen() {
  try {
    return localStorage.getItem(SEEN_KEY) === '1';
  } catch {
    return false;      // private window: show it, it is only four steps
  }
}

export function markTourSeen() {
  try {
    localStorage.setItem(SEEN_KEY, '1');
  } catch {
    /* nothing to do; the tour simply runs again next time */
  }
}

/**
 * Run a tour. Resolves when the user finishes or skips it.
 */
export function runTour(steps) {
  return new Promise((resolve) => {
    const usable = steps.filter((step) => findTarget(step.target));
    if (!usable.length) {
      resolve(false);
      return;
    }

    const backdrop = document.createElement('div');
    backdrop.className = 'tour-backdrop';

    const spot = document.createElement('div');
    spot.className = 'tour-spot';

    const card = document.createElement('div');
    card.className = 'tour-card';
    card.setAttribute('role', 'dialog');
    card.setAttribute('aria-live', 'polite');

    document.body.append(backdrop, spot, card);

    let index = 0;

    const close = (completed) => {
      window.removeEventListener('resize', reposition);
      window.removeEventListener('keydown', onKey, true);
      backdrop.remove();
      spot.remove();
      card.remove();
      resolve(completed);
    };

    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        close(false);
      } else if (event.key === 'ArrowRight' || event.key === 'Enter') {
        event.preventDefault();
        next();
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault();
        back();
      }
    };

    const next = () => {
      if (index >= usable.length - 1) close(true);
      else { index += 1; render(); }
    };
    const back = () => {
      if (index > 0) { index -= 1; render(); }
    };

    function render() {
      const step = usable[index];
      const dots = usable
        .map((_, i) => `<i class="${i === index ? 'is-on' : ''}"></i>`)
        .join('');
      const last = index === usable.length - 1;

      card.innerHTML = `
        <span class="tour-card__step">Step ${index + 1} of ${usable.length}</span>
        <h3></h3>
        <p></p>
        <div class="tour-card__foot">
          <span class="tour-card__dots">${dots}</span>
          ${index > 0 ? '<button class="btn btn--ghost btn--sm" data-act="back" type="button">Back</button>' : ''}
          <button class="btn btn--ghost btn--sm" data-act="skip" type="button">Skip</button>
          <button class="btn btn--primary btn--sm" data-act="next" type="button">${last ? 'Got it' : 'Next'}</button>
        </div>`;
      card.querySelector('h3').textContent = step.title;
      card.querySelector('p').textContent = step.body;

      card.querySelector('[data-act="next"]').addEventListener('click', next);
      card.querySelector('[data-act="skip"]').addEventListener('click', () => close(false));
      card.querySelector('[data-act="back"]')?.addEventListener('click', back);

      reposition();
      card.querySelector('[data-act="next"]').focus();
    }

    function reposition() {
      const step = usable[index];
      const target = findTarget(step.target);
      if (!target) {
        next();
        return;
      }

      target.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      const rect = target.getBoundingClientRect();
      const pad = 7;

      spot.style.top = `${rect.top - pad}px`;
      spot.style.left = `${rect.left - pad}px`;
      spot.style.width = `${rect.width + pad * 2}px`;
      spot.style.height = `${rect.height + pad * 2}px`;

      const cardRect = card.getBoundingClientRect();
      const gap = 16;
      const below = step.placement !== 'top';

      let top = below ? rect.bottom + gap : rect.top - cardRect.height - gap;
      // Flip if it would fall off the viewport.
      if (top < 12) top = rect.bottom + gap;
      if (top + cardRect.height > window.innerHeight - 12) {
        top = Math.max(12, rect.top - cardRect.height - gap);
      }

      let left = rect.left + rect.width / 2 - cardRect.width / 2;
      left = Math.max(16, Math.min(left, window.innerWidth - cardRect.width - 16));

      card.style.top = `${top}px`;
      card.style.left = `${left}px`;
    }

    window.addEventListener('resize', reposition);
    window.addEventListener('keydown', onKey, true);

    // Deliberately NOT click-to-dismiss. This runs once, on someone's first
    // minute with the app, and a stray click on the backdrop would throw the
    // explanation away for good. Leaving is explicit: Skip, or Escape.
    backdrop.addEventListener('click', (event) => event.stopPropagation());

    render();
  });
}

/** First visible element matching any selector in a comma list. */
function findTarget(selector) {
  for (const part of selector.split(',')) {
    const element = document.querySelector(part.trim());
    if (element && element.offsetParent !== null) return element;
  }
  return null;
}

/**
 * A dismissible inline note, shown once per key.
 *
 * Used for the canvas controls: a modal in the middle of the annotation screen
 * would block the thing it is describing.
 */
export function showHelpStrip(host, key, html) {
  const storageKey = `pcb-help-${key}`;
  // Navigating back to a screen must not stack a second copy of its note.
  if (host.querySelector(`.help-strip[data-help="${key}"]`)) return;
  try {
    if (localStorage.getItem(storageKey) === '1') return;
  } catch {
    /* fall through and show it */
  }

  const strip = document.createElement('div');
  strip.className = 'help-strip';
  strip.dataset.help = key;
  strip.innerHTML = `
    <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.6v.1"/></svg>
    <span class="help-strip__text">${html}</span>
    <button class="help-strip__close" type="button">Got it</button>`;

  strip.querySelector('button').addEventListener('click', () => {
    try {
      localStorage.setItem(storageKey, '1');
    } catch {
      /* dismissal just will not persist */
    }
    strip.remove();
  });

  host.prepend(strip);
}
