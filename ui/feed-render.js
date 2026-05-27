/** Shared feed card rendering for index.html and run.html */

function fmtPct(n) {
  if (n == null || isNaN(n)) return '—';
  return Math.round(n * 100) + '%';
}

function safe(v) {
  if (v == null) return '';
  return String(v).replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
}

function fmtTimestamp(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return String(ts);
    return d.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch (_e) {
    return String(ts);
  }
}

const VERDICT_CLASSES = {
  underpriced: 'verdict-underpriced',
  overpriced: 'verdict-overpriced',
  fair: 'verdict-fair',
};

function renderCard(card) {
  const m = card.market || {};
  const a = card.analysis || {};
  const sub = card.sub_prediction || {};
  const v = card.verdict || {};
  const meta = card.meta || {};
  const layer3 = card.layer3 || {};
  const mode = card.mode || 'fresh';

  const el = document.createElement('div');
  el.className = 'card';

  const verdictCall = (v.call || '').toLowerCase();
  const verdictClass = VERDICT_CLASSES[verdictCall] || 'verdict-pending';
  const statusClass = card.status === 'failed' ? 'status-failed' : '';
  let verdictHtml = '';
  if (verdictCall && VERDICT_CLASSES[verdictCall]) {
    verdictHtml = `<span class="pill ${verdictClass}">${safe(v.call)}</span>`;
  } else if (card.status !== 'failed') {
    verdictHtml = '<span class="pill verdict-pending">pending</span>';
  }

  const components = layer3.components_researched || {};
  const compHtml = Object.keys(components).map(name => {
    const c = components[name];
    return `<div class="component">
      <div class="name">${safe(name)} <span class="dir-${safe(c.direction || 'neutral')}">${safe(c.direction || '')}</span></div>
      <div>${safe(c.finding || '')}</div>
      ${c.source ? `<div style="margin-top:4px"><a href="${safe(c.source)}" target="_blank" rel="noopener">source</a></div>` : ''}
    </div>`;
  }).join('');

  const basisHtml = (sub.basis || []).map(b => `<li>${safe(b)}</li>`).join('');

  let changesHtml = '';
  if (mode === 'reanalysis' && card.changes_since_prior) {
    const cs = card.changes_since_prior;
    const newDev = (cs.new_developments || []).map(d => `<li>${safe(d)}</li>`).join('');
    changesHtml = `<div class="changes">
      <h4>Changes since prior analysis</h4>
      <div>Probability delta: <strong>${safe(cs.probability_delta)}</strong></div>
      ${cs.prior_prediction_event_resolved !== undefined ? `<div>Prior event resolved: <strong>${cs.prior_prediction_event_resolved}</strong></div>` : ''}
      ${newDev ? `<ul>${newDev}</ul>` : ''}
    </div>`;
  }

  let failureHtml = '';
  if (card.status === 'failed') {
    failureHtml = `<div class="sub-pred" style="border-color:rgba(255,118,118,0.4);margin-bottom:14px">
      <h3 class="section">Run failed</h3>
      <div style="font-size:13px;color:var(--muted)">${safe(card.failure_reason || 'Unknown error')}</div>
      ${card.failed_at_layer != null ? `<div style="font-size:13px;color:var(--muted);margin-top:6px">Stopped after layer <strong>${card.failed_at_layer}</strong>.</div>` : ''}
    </div>`;
  }

  const selectionHtml = m.selection_reason
    ? `<div style="font-size:13px;color:var(--muted);margin-top:8px"><strong style="color:var(--text)">Why this market:</strong> ${safe(m.selection_reason)}</div>`
    : '';

  el.innerHTML = `
    <div class="card-head">
      <p class="question">${safe(m.question)}</p>
      <div class="meta-row">
        ${verdictHtml}
        ${statusClass ? `<span class="pill ${statusClass}">${safe(card.status)}</span>` : ''}
        ${mode === 'reanalysis' ? `<span class="pill mode-reanalysis">re-analysis #${card.analysis_number || ''}</span>` : ''}
        <span class="pill">prob ${fmtPct(m.probability_now)}</span>
        <span class="pill">vol 24h $${Math.round(m.volume_24hr || 0).toLocaleString()}</span>
        ${m.category ? `<span class="pill">${safe(m.category)}</span>` : ''}
        ${m.polymarket_url ? `<a class="pill" href="${safe(m.polymarket_url)}" target="_blank" rel="noopener">polymarket ↗</a>` : ''}
        <span class="pill pill-timestamp">${safe(fmtTimestamp(card.timestamp))}</span>
      </div>
      ${selectionHtml}
    </div>
    <div class="card-body">
      <div>
        ${failureHtml}
        <h3 class="section">Plain English</h3>
        <p class="plain-english">${safe(a.plain_english || '')}</p>
        ${a.key_event ? `<div style="margin-bottom:14px; font-size:13px; color:var(--muted)"><strong style="color:var(--text)">Key event:</strong> ${safe(a.key_event)}</div>` : ''}

        <div class="sub-pred">
          <h3 class="section">Sub-prediction</h3>
          <div class="value">${safe(sub.predicted_value || '—')} <span class="range">${safe(sub.predicted_range || '')}</span></div>
          <div class="conf-bar"><span style="width:${Math.round((sub.confidence || 0) * 100)}%"></span></div>
          <div style="font-size:12px;color:var(--muted);margin-top:4px">confidence ${fmtPct(sub.confidence)}</div>
          ${basisHtml ? `<ul>${basisHtml}</ul>` : ''}
          ${sub.invalidation_condition ? `<div class="invalidation"><strong>Invalidation:</strong> ${safe(sub.invalidation_condition)}</div>` : ''}
          ${sub.actual_value ? `<div style="margin-top:8px;font-size:13px"><strong>Actual:</strong> ${safe(sub.actual_value)} — ${sub.prediction_correct ? 'correct' : 'incorrect'}</div>` : ''}
        </div>

        <div class="verdict">
          <div class="call">Verdict: ${safe(v.call || '—')} (${fmtPct(v.confidence)})</div>
          <div>${safe(v.reasoning || '')}</div>
        </div>
        ${v.watch_up ? `<div style="font-size:13px;color:var(--muted);margin-top:10px"><strong style="color:var(--text)">Watch up:</strong> ${safe(v.watch_up)}</div>` : ''}
        ${v.watch_down ? `<div style="font-size:13px;color:var(--muted)"><strong style="color:var(--text)">Watch down:</strong> ${safe(v.watch_down)}</div>` : ''}
        ${changesHtml}
      </div>
      <div>
        <h3 class="section">Layer 3 components</h3>
        <div class="components">${compHtml || '<div class="component">No component data.</div>'}</div>
        ${a.news_url ? `<div style="margin-top:14px;font-size:13px"><strong>News:</strong> <a href="${safe(a.news_url)}" target="_blank" rel="noopener">${safe(a.news_headline || a.news_url)}</a> <span style="color:var(--muted)">(${safe(a.news_source || '')})</span></div>` : ''}
        <div style="margin-top:14px;font-size:12px;color:var(--muted)">
          tools: ${(meta.tools_called || []).join(', ') || '—'}<br>
          tokens: in ${meta.total_input_tokens || 0} / out ${meta.total_output_tokens || 0}<br>
          turns: ${meta.turns || 0} · duration ${meta.run_duration_seconds || '?'}s
        </div>
      </div>
    </div>
  `;
  return el;
}

function renderStats(scorecard) {
  const sp = scorecard.sub_predictions || {};
  const mv = scorecard.market_verdicts || {};
  return `
    <div class="stat">Total analyses: <strong>${sp.total || 0}</strong></div>
    <div class="stat">Sub-predictions resolved: <strong>${sp.resolved || 0}</strong></div>
    <div class="stat">Sub-prediction accuracy: <strong>${fmtPct(sp.accuracy)}</strong></div>
    <div class="stat">Verdict accuracy: <strong>${fmtPct(mv.accuracy)}</strong></div>
  `;
}
