// Keyboard support for non-<button> controls marked role="button" tabindex="0":
// Enter or Space activates them, as it would a real button.
document.addEventListener('keydown', e => {
  const t = e.target;
  if ((e.key === 'Enter' || e.key === ' ') && t.tagName !== 'BUTTON' && t.getAttribute?.('role') === 'button') {
    e.preventDefault();
    t.click();
  }
});

// ...and show where keyboard focus is.
document.head.appendChild(Object.assign(document.createElement('style'), {
  textContent: '[role="button"]:focus-visible{outline:2px solid #74F5B8;outline-offset:2px}',
}));
