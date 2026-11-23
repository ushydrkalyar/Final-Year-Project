// How Studio's models tell the page what changed. Only main.js subscribes.

const handlers = new Map();

export function on(event, handler) {
  handlers.set(event, [...(handlers.get(event) || []), handler]);
}

export function emit(event, payload) {
  for (const handler of handlers.get(event) || []) handler(payload);
}
