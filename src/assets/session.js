// Session navigation shared by every page.

// Where to go after signing in: a path on this site, or the fallback. Rejects other origins,
// script URLs, and paths that only become protocol-relative once normalised ("/..//host").
function safeNext(raw, fallback = '/home.html') {
  if (typeof raw !== 'string' || !/^\/(?![\/\\])/.test(raw)) return fallback;
  let url;
  try { url = new URL(raw, location.origin); } catch (_) { return fallback; }
  if (url.origin !== location.origin || url.pathname.startsWith('//')) return fallback;
  return url.pathname + url.search + url.hash;
}

// Off to sign in, and back to exactly this page afterwards.
function goToLogin() {
  location.href = '/login.html?next=' + encodeURIComponent(location.pathname + location.search);
}

// Gated pages start hidden (<style id="authGate">). Resolves with the user and shows the page;
// otherwise it redirects, or offers a retry, and never resolves, so page code stops right there.
async function requireUser() {
  const never = new Promise(() => {});
  try {
    const { user } = await apiJson('/auth/me');
    document.getElementById('authGate')?.remove();
    return user;
  } catch (_) {
    const panel = document.createElement('div');
    panel.style.cssText = 'visibility:visible;position:fixed;inset:0;z-index:99999;display:flex;flex-direction:column;'
      + 'align-items:center;justify-content:center;gap:14px;background:#0a0a0b;color:#fff;font:15px system-ui';
    const retry = Object.assign(document.createElement('button'), { textContent: 'Retry', onclick: () => location.reload() });
    panel.append('Could not reach the server.', retry);
    document.body.append(panel);
    return never;
  }
}

// Login and signup: someone already signed in has nothing to do there.
async function leaveIfSignedIn(to = '/home.html') {
  try { await api('/auth/me', { redirect: false }); } catch (_) { return; }
  location.href = to;
}

async function logout() {
  try { await api('/auth/logout', { method: 'POST', redirect: false }); } catch (_) { /* leave anyway */ }
  location.href = '/welcome.html';
}
