// The one way pages call the server.

class ApiError extends Error {
  constructor(status, data, fallback) {
    super(status ? detailText(data, fallback) : 'Could not reach the server.');
    this.status = status;       // 0: no response at all
    this.data = data;
  }
}

// Resolves with the Response only when it is OK; otherwise throws ApiError.
//   json:     a body to send as JSON (POST unless a method is given)
//   fallback: the message when the server sends none
//   redirect: false on pages where 401 is an answer (login). Elsewhere an expired session
//             goes to sign in and the call never settles, so page code stops there.
async function api(path, { json, fallback, redirect = true, ...init } = {}) {
  if (json !== undefined) {
    init.method ??= 'POST';
    init.headers = { 'Content-Type': 'application/json', ...init.headers };
    init.body = JSON.stringify(json);
  }
  let r;
  try {
    r = await fetch(path, { credentials: 'include', ...init });
  } catch (_) {
    throw new ApiError(0);
  }
  if (r.ok) return r;
  const data = await r.json().catch(() => null);
  if (r.status === 401 && redirect && data?.detail !== 'wrong_password') {
    goToLogin();
    return new Promise(() => {});
  }
  throw new ApiError(r.status, data, fallback);
}

const apiJson = async (path, options) => (await api(path, options)).json();
