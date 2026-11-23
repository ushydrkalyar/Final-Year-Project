// Safe rendering helpers shared by every page.

// Text for innerHTML, in element content or inside a quoted attribute.
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Escaping cannot protect a value inside onclick="...('here')" or url(here): allow only known-safe shapes.
const safeId = v => String(v ?? '').replace(/[^\w-]/g, '');
const safeImg = v => /^data:image\/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/.test(v || '') ? v : '';

// '#RRGGBB' from '#rgb', 'rgb', '#rrggbb' or 'rrggbb'; null for anything else.
function normHex(v) {
  const m = /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(typeof v === 'string' ? v.trim() : '');
  if (!m) return null;
  return '#' + (m[1].length === 3 ? [...m[1]].map(c => c + c).join('') : m[1]).toUpperCase();
}

// The readable message in an API error body ({detail: "text"} or a list of validation errors).
function detailText(data, fallback = 'Something went wrong. Try again.') {
  const d = data?.detail;
  if (Array.isArray(d)) {
    const text = d.map(e => String(e?.msg ?? '').replace(/^Value error, /, '')).filter(Boolean).join('. ');
    return text || fallback;
  }
  return typeof d === 'string' && d ? d : fallback;
}

// A short message: the page's #toast, or a stacked one where the page has a #toasts container.
function toast(message, type = 'info') {
  const stack = document.getElementById('toasts');
  const el = stack ? document.createElement('div') : document.getElementById('toast');
  if (!el) return;
  if (stack) {
    for (const text of [{ success: '✓', error: '✕' }[type] || '→', message]) {
      el.append(Object.assign(document.createElement('span'), { textContent: text }));
    }
    el.className = `toast ${type}`;
    stack.append(el);
    setTimeout(() => el.remove(), 4000);
    return;
  }
  el.textContent = message;
  el.className = `toast show ${type}`;
  clearTimeout(el.hideTimer);
  el.hideTimer = setTimeout(() => el.classList.remove('show'), 3200);
}
