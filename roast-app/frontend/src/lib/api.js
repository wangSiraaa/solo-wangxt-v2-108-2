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

// --- roasting plans: versions, lifecycle, bindings, stored conclusions -----

async function jsonOrThrow(r) {
  const txt = await r.text();
  if (r.ok) return txt ? JSON.parse(txt) : null;
  let detail = txt;
  try {
    detail = JSON.parse(txt).detail || txt;
  } catch {
    /* keep raw text */
  }
  const err = new Error(detail);
  err.status = r.status;
  throw err;
}

export async function listPlans() {
  const r = await fetch('/api/plans');
  return jsonOrThrow(r);
}

export async function seedDefaultPlan() {
  const r = await fetch('/api/plans/seed-default', { method: 'POST' });
  return jsonOrThrow(r);
}

export async function createPlan(body) {
  const r = await fetch('/api/plans', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(r);
}

export async function createPlanVersion(planId, body) {
  const r = await fetch(`/api/plans/${planId}/versions`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(r);
}

export async function setPlanVersionStatus(planId, versionNo, action, body = {}) {
  const r = await fetch(`/api/plans/${planId}/versions/${versionNo}/${action}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(r);
}

export async function bindPlan(batchId, planVersionId) {
  const r = await fetch(`/api/batches/${batchId}/bind-plan`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ plan_version_id: planVersionId }),
  });
  return jsonOrThrow(r);
}

export async function listBindings(batchId) {
  const r = await fetch(`/api/batches/${batchId}/bindings`);
  return jsonOrThrow(r);
}

export async function rerunPlanReview(batchId, note = '') {
  const r = await fetch(`/api/batches/${batchId}/plan-review`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ note }),
  });
  return jsonOrThrow(r);
}

export async function createBatch(body) {
  const r = await fetch('/api/batches', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  return jsonOrThrow(r);
}

export const PLAN_VERDICT_LABELS = {
  within_tolerance: '在容差内',
  outside_tolerance: '超出容差',
  unevaluated: '未评估',
  incomplete: '不完整（含未评估段）',
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
