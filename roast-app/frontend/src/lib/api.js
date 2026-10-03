// Thin API wrapper. All data is offline/synthetic — the backend never talks to
// a roaster.
const qs = (p) =>
  new URLSearchParams(Object.entries(p).filter(([, v]) => v !== undefined && v !== null));

export async function getBatches() {
  const r = await fetch('/api/batches');
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function seed() {
  const r = await fetch('/api/seed', { method: 'POST' });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getSeries(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/series?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function getCompare(a, b, params = {}) {
  const r = await fetch(`/api/compare?${qs({ a, b, ...params })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function addEvent(batchId, ev) {
  const r = await fetch(`/api/batches/${batchId}/events`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(ev),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function listEvents(batchId, includeHistory = false) {
  const r = await fetch(`/api/batches/${batchId}/events?${qs({ include_history: includeHistory })}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function exportBatch(batchId, params = {}) {
  const r = await fetch(`/api/batches/${batchId}/export?${qs(params)}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

export async function recompute(body) {
  const r = await fetch('/api/recompute', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// ---------------------------------------------------------------------------
// roast plan versions
// ---------------------------------------------------------------------------

async function jsonOrThrow(r) {
  const text = await r.text();
  let body;
  try {
    body = text ? JSON.parse(text) : {};
  } catch {
    body = { raw: text };
  }
  if (!r.ok) {
    const err = new Error(
      body?.detail?.message ||
        body?.detail?.error ||
        (typeof body.detail === 'string' ? body.detail : `HTTP ${r.status}`)
    );
    err.status = r.status;
    err.detail = body.detail || body;
    throw err;
  }
  return body;
}

export async function listPlans() {
  return jsonOrThrow(await fetch('/api/plans'));
}

export async function createPlan(body) {
  return jsonOrThrow(
    await fetch('/api/plans', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function seedDemoPlan() {
  return jsonOrThrow(await fetch('/api/seed-demo-plan', { method: 'POST' }));
}

export async function newDraftVersion(planId, body) {
  return jsonOrThrow(
    await fetch(`/api/plans/${planId}/versions`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function editDraft(planId, versionId, body) {
  return jsonOrThrow(
    await fetch(`/api/plans/${planId}/versions/${versionId}`, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function confirmVersion(planId, versionId, body = {}) {
  return jsonOrThrow(
    await fetch(`/api/plans/${planId}/versions/${versionId}/confirm`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function retireVersion(planId, versionId, body = {}) {
  return jsonOrThrow(
    await fetch(`/api/plans/${planId}/versions/${versionId}/retire`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function bindBatch(batchId, planVersionId, extra = {}) {
  return jsonOrThrow(
    await fetch(`/api/batches/${batchId}/plan-binding`, {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ plan_version_id: planVersionId, ...extra }),
    })
  );
}

export async function getBinding(batchId) {
  return jsonOrThrow(await fetch(`/api/batches/${batchId}/plan-binding`));
}

export async function getBindingHistory(batchId) {
  return jsonOrThrow(await fetch(`/api/batches/${batchId}/plan-binding/history`));
}

export async function runAssessment(batchId, body = {}) {
  return jsonOrThrow(
    await fetch(`/api/batches/${batchId}/plan-assessments`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
  );
}

export async function getAssessment(batchId) {
  return jsonOrThrow(await fetch(`/api/batches/${batchId}/plan-assessment`));
}

export async function getAssessmentHistory(batchId) {
  return jsonOrThrow(await fetch(`/api/batches/${batchId}/plan-assessment/history`));
}

export const PLAN_STATUS_LABELS = {
  draft: '草稿',
  confirmed: '已确认',
  retired: '已退役',
};

export const SEGMENT_LABELS = {
  drying: '脱水期',
  maillard: '梅纳期',
  development: '发展期',
};

export const VERDICT_LABELS = {
  all_within_tolerance: '全部在容差内',
  deviation_outside_tolerance: '存在超出容差',
  unevaluable_segments: '含未评估段',
  partial_deviation: '部分超出容差',
};

export const SEG_STATUS_LABELS = {
  pass: '在容差内',
  fail: '超出容差',
  unevaluable: '未评估',
};

export const REVIEW_STATE_LABELS = {
  current: '当前',
  needs_review: '需要重新审阅',
  superseded: '已被新判断取代',
};

export const EVENT_LABELS = {
  charge: '下豆/开火',
  turning_point: '回温点',
  first_crack_start: '一爆开始',
  first_crack_end: '一爆结束',
  drop: '出锅',
  damper_change: '风门变化',
  custom: '自定义',
};

export function fmtTime(s) {
  if (s === null || s === undefined) return '—';
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, '0')}`;
}
