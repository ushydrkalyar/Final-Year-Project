// The range selected on the Cuts lane. No DOM.
import { emit } from './bus.js';

let range = null; // {start, end, type, reason, in_edit} on the original timeline

export const selected = () => range;

export function select(next) {
  range = next;
  emit('range:selected', next);
}
