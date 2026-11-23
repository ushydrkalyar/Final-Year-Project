// Caption edits follow the footage into a new cut: a restore, a trim, a reorder, an added clip. Pure.

const EPS = 1e-3;
const MOSTLY = 0.85;
const SHORTEST_S = 0.12;

// An edit's kept ranges, in each clip's own seconds, beside where they play.
export function kept(timeline) {
  return (timeline?.edited_timeline ?? []).map(segment => {
    const clip = timeline.clips[segment.clip];
    return {
      key: clip.key,
      from: segment.original_start - clip.original_start,
      to: segment.original_end - clip.original_start,
      at: segment.edited_start,
      until: segment.edited_end,
    };
  });
}

function sources(caption, ranges) {
  const out = [];
  let from = caption.s;
  for (const range of ranges) {
    const s = Math.max(from, range.at), e = Math.min(caption.e, range.until);
    if (e - s <= EPS) continue;
    out.push({ key: range.key, a: range.from + s - range.at, b: range.from + e - range.at, offset: range.at - range.from });
    from = e; // under a transition two clips play: count each moment once
  }
  return out;
}

function place(caption, oldRanges, newRanges) {
  const parts = sources(caption, oldRanges);
  if (!parts.length) return null;
  const lengths = new Map();
  for (const part of parts) lengths.set(part.key, (lengths.get(part.key) ?? 0) + part.b - part.a);
  const key = [...lengths].sort((x, y) => y[1] - x[1])[0][0]; // a caption across two clips goes with its larger share

  const spans = [];
  for (const part of parts) {
    if (part.key !== key) continue;
    for (const range of newRanges) {
      if (range.key !== key) continue;
      const a = Math.max(part.a, range.from), b = Math.min(part.b, range.to);
      if (b - a > EPS) spans.push({ a, b, s: range.at + a - range.from, e: range.at + b - range.from, offset: part.offset });
    }
  }
  if (!spans.length) return null;
  const surviving = spans.reduce((sum, span) => sum + span.b - span.a, 0);
  let text = caption.t;
  if (surviving < lengths.get(key) * MOSTLY) {
    // its words share the caption's time by length, as the server does with an edited caption
    const words = caption.t.split(/\s+/).filter(Boolean);
    const weights = words.map(word => Math.max(2, word.length));
    const total = weights.reduce((x, y) => x + y, 0);
    let seen = 0;
    text = words.filter((word, i) => {
      const middle = caption.s + ((seen + weights[i] / 2) / total) * (caption.e - caption.s);
      seen += weights[i];
      return spans.some(span => middle >= span.a + span.offset && middle <= span.b + span.offset);
    }).join(' ');
    if (!text) return null;
  }
  return { s: Math.min(...spans.map(span => span.s)), e: Math.max(...spans.map(span => span.e)), t: text };
}

// `fresh`: the new cut's untouched captions, used for footage the old cut didn't have.
export function rebase(list, oldRanges, newRanges, fresh) {
  const out = list.map(caption => place(caption, oldRanges, newRanges)).filter(Boolean);
  const carried = [...out];
  for (const caption of fresh) {
    const middle = (caption.s + caption.e) / 2;
    const range = newRanges.find(r => middle >= r.at && middle <= r.until);
    if (!range) continue;
    const source = range.from + middle - range.at;
    if (oldRanges.some(r => r.key === range.key && source >= r.from && source <= r.to)) continue;
    let { s, e } = caption;
    for (const other of carried) {
      if (other.e <= s || other.s >= e) continue;
      if (other.s <= middle && other.e >= middle) e = s;
      else if (other.e <= middle) s = other.e;
      else e = other.s;
    }
    if (e - s >= SHORTEST_S) out.push({ s, e, t: caption.t });
  }
  out.sort((x, y) => x.s - y.s);
  for (let i = 0; i < out.length - 1; i++) out[i].e = Math.min(out[i].e, out[i + 1].s);
  return out.filter(caption => caption.e - caption.s > EPS);
}
