// Between the edited timeline (what plays) and the original one (the uploads end to end). Pure.

const EPS = 1e-3;

// `side`: at a join, 'start' is the range after it and 'end' the one before.
export function toOriginal(segments, t, side = 'end') {
  const before = side === 'end' ? EPS : -EPS;
  for (const segment of segments) {
    if (t <= segment.edited_end + before) return Math.min(segment.original_end, segment.original_start + Math.max(0, t - segment.edited_start));
  }
  return segments.length ? segments[segments.length - 1].original_end : t;
}

// A removed moment lands on the join it left behind.
export function toEdited(segments, t) {
  for (const segment of segments) {
    if (t < segment.original_start) return segment.edited_start;
    if (t <= segment.original_end) return Math.min(segment.edited_end, segment.edited_start + t - segment.original_start);
  }
  return segments.length ? segments[segments.length - 1].edited_end : t;
}

export function clipAt(timeline, t, view = 'edited') {
  const spans = timeline?.clips ?? [];
  for (let i = spans.length - 1; i >= 0; i--) {
    if (t >= spans[i][`${view}_start`] - EPS) return spans[i];
  }
  return spans[0] ?? null;
}

const distance = (t, start, end) => Math.max(0, start - t, t - end);

function nearest(list, measure, reach) {
  let best = null, least = Infinity;
  for (const item of list) {
    const away = measure(item);
    if (away < least) [best, least] = [item, away];
  }
  return least <= reach ? best : null;
}

const NEAR_S = 1.5;
const SHORTEST_S = 0.1;

// A timed comment -> the correction it asks for: {start, end} on the original timeline, or {problem}.
// Times are read off the timeline on screen (`view`).
export function resolveRequest({ timeline, request, view, speech }) {
  const { original_timeline: all, edited_timeline: kept, pauses = [] } = timeline;
  const removed = all.filter(segment => !segment.in_edit);
  const original = (t, side) => (view === 'original' ? t : toOriginal(kept, t, side));
  const span = (start, end, problem) => (end - start >= SHORTEST_S ? { start, end } : { problem });

  if (request.kind === 'range') {
    const limit = view === 'original' ? timeline.original_duration : timeline.edited_duration;
    if (request.start >= limit) return { problem: `The ${view} timeline is only ${limit.toFixed(1)}s long.` };
    if (request.action === 'CUT' || view === 'original') {
      return span(original(request.start, 'start'), original(Math.min(request.end, limit), 'end'), 'That range is empty.');
    }
    // putting back: what was removed inside that stretch
    const inside = removed.filter(r => {
      const join = toEdited(kept, r.start);
      return join >= request.start - 0.25 && join <= request.end + 0.25;
    });
    return inside.length ? { start: inside[0].start, end: inside[inside.length - 1].end }
      : { problem: 'Nothing was removed there. Switch the timeline to Original to pick removed footage.' };
  }

  if (request.kind === 'edge') {
    const atStart = request.edge === 'start';
    if (request.action === 'KEEP') {
      const edge = atStart ? removed.find(r => r.start <= 0.05)
        : removed.find(r => r.end >= timeline.original_duration - 0.05);
      return edge ? { start: edge.start, end: edge.end } : { problem: `Nothing was removed at the ${request.edge}.` };
    }
    if (!speech) return { problem: 'There is no speech to measure that silence from.' };
    const quiet = atStart ? [0, speech.first] : [speech.last, timeline.edited_duration];
    return span(toOriginal(kept, quiet[0], 'start'), toOriginal(kept, quiet[1], 'end'),
      `There is no silence left at the ${request.edge}.`);
  }

  const t = request.at;
  if (request.action === 'KEEP') {
    const found = nearest(removed, r => (view === 'original' ? distance(t, r.start, r.end)
      : Math.abs(toEdited(kept, r.start) - t)), NEAR_S);
    return found ? { start: found.start, end: found.end } : { problem: `Nothing was removed near ${t}s.` };
  }
  const at = original(t, 'start');
  const quiet = pauses.filter(p => kept.some(k => p.start < k.original_end - EPS && p.end > k.original_start + EPS));
  const pause = nearest(quiet, p => distance(at, p.start, p.end), NEAR_S);
  return pause ? { start: pause.start, end: pause.end }
    : { problem: `There is no pause near ${t}s. Give a range instead, for example "cut 0:05 to 0:08".` };
}
