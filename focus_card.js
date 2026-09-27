/* focus_card.js: a card glides to the centre and grows while the page fades to white.
 * The fade starts when the card starts moving and finishes exactly when it stops.
 *
 *   const focus = focusCard(cardElement);   // animate in
 *   focus.close();                           // animate back (also: click anywhere, Esc)
 *   focus.close({ instant: true });          // remove at once (e.g. before re-rendering)
 *
 * Hooks for content that changes once the card is focused (the copy is what moves; the card stays put):
 *   prepare(copy)          -> px   build extra content in the copy before it is measured; returns the
 *                                  minimum height the focused card needs (in the card's own px)
 *   onOpen(card, copy)             the card has landed
 *   onCloseStart(card, copy)       an animated close is starting (not called for instant closes)
 */
(function () {
  const EASE = 'cubic-bezier(0.65, 0, 0.35, 1)';   // slow start, fast middle, soft landing

  const DEFAULTS = {
    duration: 700,           // ms for the move and the fade (they always run together)
    widthRatio: 0.49,        // focused card width as a share of the viewport
    maxHeightRatio: 0.82,    // never taller than this share of the viewport
    maxScale: 3,
    background: null,        // null = white (dark theme: dark page colour)
    cardBackground: null,    // null = white (dark theme: slate-900)
    closeOnClick: true,
    closeOnEscape: true,
    prepare: null,
    onOpen: null,
    onCloseStart: null,
    onClose: null
  };

  let active = null;
  const dark = () => document.documentElement.classList.contains('dark');
  const reducedMotion = () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

  // Where the card is now, and where / how big it lands
  function geometry(original, clone, o, minHeight) {
    const r = original.getBoundingClientRect();
    const vw = window.innerWidth, vh = window.innerHeight;
    const prev = clone.style.height;
    clone.style.height = 'auto';                    // focused card hugs its content
    const naturalH = Math.max(clone.offsetHeight, minHeight || 0);
    clone.style.height = prev;
    const targetW = vw < 640 ? vw - 32 : vw * o.widthRatio;
    const scale = Math.max(1, Math.min(targetW / r.width, (vh * o.maxHeightRatio) / naturalH, o.maxScale));
    const left = (vw - r.width * scale) / 2;
    const top = (vh - naturalH * scale) / 2;
    return { r, naturalH, scale, dx: left - r.left, dy: top - r.top };
  }

  function keyframes(g, fromBg, toBg) {
    return [
      { transform: 'translate(0px, 0px) scale(1)', height: `${g.r.height}px`,
        backgroundColor: fromBg, boxShadow: '0 0 0 rgba(15, 23, 42, 0)' },
      { transform: `translate(${g.dx}px, ${g.dy}px) scale(${g.scale})`, height: `${g.naturalH}px`,
        backgroundColor: toBg, boxShadow: '0 12px 32px -12px rgba(15, 23, 42, 0.18)' }
    ];
  }

  function focusCard(card, options = {}) {
    if (!card) throw new Error('focusCard: no element');
    if (active) return active;
    const o = { ...DEFAULTS, ...options };
    const duration = reducedMotion() ? 0 : o.duration;
    const pageBg = o.background || (dark() ? '#020617' : '#ffffff');
    const endCardBg = o.cardBackground || (dark() ? '#0f172a' : '#ffffff');
    const startCardBg = getComputedStyle(card).backgroundColor;

    // 1. White layer over the page, invisible at first
    const veil = document.createElement('div');
    Object.assign(veil.style, { position: 'fixed', inset: '0', zIndex: '9998', background: pageBg, opacity: '0' });

    // 2. A copy of the card laid exactly over the original
    const r0 = card.getBoundingClientRect();
    const clone = card.cloneNode(true);
    clone.removeAttribute('id');
    clone.querySelectorAll('[id]').forEach(n => n.removeAttribute('id'));
    clone.setAttribute('inert', '');
    clone.setAttribute('aria-hidden', 'true');
    Object.assign(clone.style, {
      position: 'fixed', left: `${r0.left}px`, top: `${r0.top}px`, width: `${r0.width}px`, height: `${r0.height}px`,
      margin: '0', boxSizing: 'border-box', zIndex: '9999', transformOrigin: '0 0',
      backgroundColor: startCardBg, overflow: 'hidden'
    });
    document.body.append(veil, clone);
    card.style.visibility = 'hidden';               // the card "leaves" the grid

    // Click / focus catcher on top
    const layer = document.createElement('div');
    layer.setAttribute('role', 'dialog');
    layer.setAttribute('aria-modal', 'true');
    layer.setAttribute('aria-label', (card.getAttribute('aria-label') || card.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 200));
    layer.tabIndex = -1;
    Object.assign(layer.style, { position: 'fixed', inset: '0', zIndex: '10000', outline: 'none',
      cursor: o.closeOnClick ? 'zoom-out' : '' });
    document.body.append(layer);
    const returnFocus = document.activeElement;
    layer.focus({ preventScroll: true });
    const stopScroll = e => e.preventDefault();
    layer.addEventListener('wheel', stopScroll, { passive: false });
    layer.addEventListener('touchmove', stopScroll, { passive: false });

    // 3. Move + grow the card and fade the page over exactly the same time
    const g = geometry(card, clone, o, o.prepare?.(clone));
    const timing = { duration, easing: EASE, fill: 'forwards' };
    const moveIn = clone.animate(keyframes(g, startCardBg, endCardBg), timing);
    const fadeIn = veil.animate([{ opacity: 0 }, { opacity: 1 }], timing);

    let state = 'opening';
    const opened = Promise.all([moveIn.finished, fadeIn.finished]).then(() => {
      if (state !== 'opening') return;
      state = 'open';
      o.onOpen?.(card, clone);
    }).catch(() => {});

    let resolveClosed;
    const closed = new Promise(res => (resolveClosed = res));

    function close({ instant = false } = {}) {
      if (state === 'closed') return closed;
      if (instant || !document.contains(card)) { moveIn.cancel(); fadeIn.cancel(); finish(); return closed; }
      if (state === 'closing') return closed;
      state = 'closing';
      layer.style.cursor = '';
      o.onCloseStart?.(card, clone);
      // reverse() plays back from wherever the animation is now, so closing mid-open glides straight back
      moveIn.reverse();
      fadeIn.reverse();
      Promise.all([moveIn.finished, fadeIn.finished]).then(finish, () => {});   // rejects only if cancelled
      return closed;
    }

    // Take everything down and hand the page back
    function finish() {
      if (state === 'closed') return;
      state = 'closed';
      document.removeEventListener('keydown', onKey, true);
      veil.remove();
      clone.remove();
      layer.remove();
      card.style.visibility = '';
      if (active === handle) active = null;
      if (returnFocus?.focus && document.contains(returnFocus)) returnFocus.focus({ preventScroll: true });
      o.onClose?.(card);
      resolveClosed();
    }

    function onKey(e) {
      if (e.key === 'Escape' && o.closeOnEscape) { e.preventDefault(); close(); }
      else if (e.key === 'Tab') e.preventDefault();   // aria-modal: focus stays on the layer
    }
    document.addEventListener('keydown', onKey, true);
    if (o.closeOnClick) layer.addEventListener('click', () => close());

    const handle = { card, opened, closed, close };
    active = handle;
    return handle;
  }

  window.focusCard = focusCard;
})();
