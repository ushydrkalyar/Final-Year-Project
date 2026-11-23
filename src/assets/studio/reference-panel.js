// The Reference tool's result: what was measured from the reference (Step 4: Style Adaptation).
import { $ } from './dom.js';

export function render(reference) {
  const box = $('refResult');
  if (!reference) {
    box.innerHTML = '';
    return;
  }
  const show = (value, format) => (Number.isFinite(value) ? format(value) : '');
  const rows = [
    ['Style match', show(reference.style_match_pct, v => `${v.toFixed(0)}%`) || 'Not measured'],
    ['Cuts pauses over', show(reference.max_duration, v => `${v.toFixed(2)} s`)],
    ['Shortens pauses over', show(reference.compress_duration, v => `${v.toFixed(2)} s`)],
    ['Speaking rate', show(reference.speaking_rate, v => `${v.toFixed(1)} words/s`)],
    ['Pauses at sentence ends', show(reference.boundary_ratio, v => `${(v * 100).toFixed(0)}%`)],
    ['Cut frequency', show(reference.cuts_per_minute, v => `${v.toFixed(1)}/min`)],
  ].filter(([, value]) => value);
  box.innerHTML = `<h3 class="label">MEASURED FROM THE REFERENCE</h3><dl class="facts">${rows.map(([name, value]) => `<dt>${name}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`;
}
