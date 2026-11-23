// The edit's scores: a chip on the timeline that opens the evaluation.
import { $, register } from './dom.js';

const number = (value, unit = '', digits = 0) =>
  (Number.isFinite(value) ? `${value.toFixed(digits)}${unit}` : '');

const NOTHING_S = 0.05;
const untouched = report => (report.time_savings?.time_saved_s ?? 1) < NOTHING_S;

const sections = report => {
  const quality = report.quality_score ?? {}, saved = report.time_savings ?? {}, pauses = report.pauses ?? {};
  const speech = report.speech ?? {}, fillers = report.fillers ?? {}, speed = report.performance ?? {};
  const why = report.decision_breakdown ?? {}, input = report.input_file ?? {};
  return [
    ['QUALITY', [
      ['Overall', untouched(report) ? 'Nothing needed cutting' : `${quality.grade ?? ''} ${number(quality.overall, ' / 100')}`.trim()],
      ['Time saved score', number(quality.time_saved)],
      ['Pacing score', number(quality.pacing)],
      ['Filler score', number(quality.filler_removal)],
      ['Silence score', number(quality.silence_reduction)],
    ]],
    ['TIME', [
      ['Original length', number(saved.original_duration_s, ' s', 1)],
      ['Edited length', number(saved.edited_duration_s, ' s', 1)],
      ['Time saved', number(saved.time_saved_s, ' s', 1)],
      ['Shorter', number(saved.time_saved_pct, '%')],
    ]],
    ['PAUSES', [
      ['Found', number(pauses.total_detected)],
      ['Cut', number(pauses.cut)],
      ['Compressed', number(pauses.compressed)],
      ['Kept', number(pauses.kept)],
      ['Saved by compressing', number(pauses.compress_saved_s, ' s', 1)],
      ['Average pause before', number(pauses.avg_pause_original_s, ' s', 2)],
      ['Average pause after', number(pauses.avg_pause_edited_s, ' s', 2)],
      ['Silence before', number(pauses.silence_ratio_original_pct, '%', 1)],
      ['Silence after', number(pauses.silence_ratio_edited_pct, '%', 1)],
    ]],
    ['SPEECH', [
      ['Words', number(speech.total_words)],
      ['Words per minute before', number(speech.wpm_original)],
      ['Words per minute after', number(speech.wpm_edited)],
      ['Faster', number(speech.wpm_increase_pct, '%', 1)],
      ['Transcription quality', speech.transcription_quality ?? ''],
      ['Fillers found', number(fillers.total_filler_words_detected)],
      ['Fillers per minute', number(fillers.filler_rate_per_minute, '', 1)],
      ['Filler time removed', number(fillers.filler_time_removed_s, ' s', 1)],
    ]],
    ['WHAT DROVE THE DECISIONS', [
      ['Pause length', number(why.duration_threshold)],
      ['Filler before the pause', number(why.filler_word_penalty)],
      ['Sentence boundary', number(why.boundary_boost_rescue)],
      ['Speech density', number(why.speech_density_modifier)],
      ['Audio energy', number(why.rms_energy_penalty)],
    ]],
    ['PROCESSING', [
      ['Source', input.width ? `${input.width} x ${input.height}` : ''],
      ['Kept segments', number(speed.segments_rendered)],
      ['Processing time', number(speed.processing_time_s, ' s', 1)],
      ['Times real time', number(speed.realtime_factor, 'x', 2)],
    ]],
  ].map(([title, rows]) => [title, rows.filter(([, value]) => value !== '')]).filter(([, rows]) => rows.length);
};

function close() {
  $('reportPop').hidden = true;
  $('statChip').setAttribute('aria-expanded', 'false');
}

export function render(report) {
  const quality = report?.quality_score, saved = report?.time_savings;
  const chip = $('statChip');
  chip.hidden = !quality || !saved;
  if (chip.hidden) return close();
  // nothing to remove is not a failing grade
  chip.textContent = untouched(report) ? 'Nothing needed cutting'
    : `${quality.grade} · ${saved.time_saved_s.toFixed(1)}s saved · ${Math.round(saved.time_saved_pct)}% shorter`;
  $('reportPop').innerHTML = sections(report).map(([title, rows]) =>
    `<h3 class="label">${title}</h3><dl>${rows.map(([name, value]) => `<dt>${name}</dt><dd>${esc(value)}</dd>`).join('')}</dl>`).join('');
}

export function init() {
  register({
    toggleReport: el => {
      const pop = $('reportPop');
      pop.hidden = !pop.hidden;
      el.setAttribute('aria-expanded', String(!pop.hidden));
    },
  });
  document.addEventListener('click', event => {
    if (!event.target.closest('#reportPop, #statChip')) close();
  });
}
