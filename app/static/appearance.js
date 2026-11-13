(() => {
  const key = 'linkpilot-appearance';
  const styles = ['nura', 'pillio', 'nova', 'fusion'];
  const root = document.documentElement;
  const normalize = (value) => styles.includes(value) ? value : 'fusion';
  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let entrance;

  let saved;
  try { saved = localStorage.getItem(key); } catch {}
  root.dataset.appearance = normalize(saved);

  document.addEventListener('DOMContentLoaded', () => {
    const menu = document.getElementById('appearanceMenu');
    if (!menu) return;
    const trigger = menu.querySelector('.appearance-label');
    const wings = menu.querySelectorAll('.appearance-wing');
    const buttons = [...menu.querySelectorAll('[data-appearance-choice]')];
    function setExpanded(expanded) {
      menu.toggleAttribute('data-expanded', expanded);
      trigger.setAttribute('aria-expanded', String(expanded));
      for (const wing of wings) wing.inert = !expanded;
    }

    const syncMenu = () => {
      for (const button of buttons) {
        button.setAttribute('aria-pressed', String(button.dataset.appearanceChoice === root.dataset.appearance));
      }
    };
    syncMenu();

    function switchAppearance(value, save = true) {
      const next = normalize(value);
      if (next === root.dataset.appearance) return;
      root.dataset.appearance = next;
      syncMenu();
      if (save) {
        try { localStorage.setItem(key, next); } catch {}
      }
      document.dispatchEvent(new Event('linkpilot:appearance'));

      // Animate only the content, leaving the menu clickable throughout.
      entrance?.cancel();
      if (motion.matches) return;
      const detailOpen = document.body.classList.contains('app-detail-open');
      entrance = document.querySelector('main')?.animate?.([
        {opacity: .55, transform: detailOpen ? 'none' : 'translateY(8px)'},
        {opacity: 1, transform: 'none'},
      ], {duration: 300, easing: 'cubic-bezier(.2,.68,.3,1)'});
    }

    menu.addEventListener('pointerenter', (event) => {
      if (event.pointerType === 'mouse') setExpanded(true);
    });
    menu.addEventListener('pointerleave', (event) => {
      if (event.pointerType === 'mouse') setExpanded(false);
    });
    trigger.addEventListener('click', (event) => {
      const hoverClick = event.pointerType === 'mouse' && menu.matches(':hover');
      setExpanded(hoverClick || !menu.hasAttribute('data-expanded'));
    });
    document.addEventListener('pointerdown', (event) => {
      if (!menu.contains(event.target)) setExpanded(false);
    }, true);
    document.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || !menu.hasAttribute('data-expanded')) return;
      if (menu.contains(document.activeElement)) {
        event.preventDefault();
        trigger.focus();
      }
      setExpanded(false);
    });
    menu.addEventListener('focusout', (event) => {
      if (event.relatedTarget && !menu.contains(event.relatedTarget)) setExpanded(false);
    });
    menu.addEventListener('click', (event) => {
      const button = event.target.closest('[data-appearance-choice]');
      if (button) switchAppearance(button.dataset.appearanceChoice);
    });
    menu.addEventListener('keydown', (event) => {
      const index = buttons.indexOf(event.target);
      if (index < 0) return;
      const last = buttons.length - 1;
      const next = {
        ArrowLeft: index === 0 ? last : index - 1,
        ArrowRight: index === last ? 0 : index + 1,
        Home: 0,
        End: last,
      }[event.key];
      if (!Number.isInteger(next)) return;
      event.preventDefault();
      buttons[next].focus();
      switchAppearance(buttons[next].dataset.appearanceChoice);
    });
    window.addEventListener('storage', (event) => {
      if (event.key === key) switchAppearance(event.newValue, false);
    });
  });
})();
