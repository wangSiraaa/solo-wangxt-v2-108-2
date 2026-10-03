<script context="module">
  const PLAIN_REASON = (code) =>
    ({
      missing_anchor: '缺少锚点',
      anchor_order: '锚点顺序异常',
      crosses_long_gap: '跨越长断档',
      insufficient_measured_coverage: '实测覆盖不足',
      too_much_interpolation: '插值占比过高',
    })[code] || code;
</script>

<script>
  // Readable deviation review for the batch's CURRENT plan binding.
  //  - stored judgment (not a live recompute) is shown by default;
  //  - after an event correction the row is flagged 需要重新审阅;
  //  - obsolete/historical judgments stay expandable with their full basis;
  //  - unevaluated segments show the reason and never a green verdict.
  import { createEventDispatcher } from 'svelte';
  import { fmtTime, PLAN_VERDICT_LABELS, listBindings, rerunPlanReview } from './api.js';

  export let batchId = null;
  // plan section as returned inside /series (binding + current_evaluation + stale)
  export let planSection = null;

  const dispatch = createEventDispatcher();

  let history = [];
  let showHistory = false;
  let busy = false;
  let error = '';

  const verdictClass = (v) =>
    v === 'within_tolerance' ? 'ok' : v === 'outside_tolerance' ? 'bad' : 'neutral';

  const verdictText = (v) => PLAN_VERDICT_LABELS[v] || v || '—';

  function anchorLabel(name) {
    return {
      charge: '下豆',
      turning_point: '回温点',
      first_crack_start: '一爆开始',
      first_crack_end: '一爆结束',
      drop: '出锅',
    }[name] || name;
  }

  async function loadHistory() {
    if (!batchId) return;
    try {
      history = await listBindings(batchId);
    } catch (e) {
      error = e.message;
    }
  }

  async function toggleHistory() {
    showHistory = !showHistory;
    if (showHistory && !history.length) await loadHistory();
  }

  async function reReview() {
    busy = true;
    error = '';
    try {
      await rerunPlanReview(batchId, '界面请求重新审阅');
      dispatch('changed');
    } catch (e) {
      error = e.message;
    } finally {
      busy = false;
    }
  }

  $: {
    if (showHistory && batchId) loadHistory();
  }

  // switching batches resets the review view to the current conclusion
  $: if (batchId !== lastBatchId) {
    lastBatchId = batchId;
    showHistory = false;
    history = [];
    error = '';
  }
  let lastBatchId = batchId;
</script>

{#if !planSection}
  <div class="muted">该批次尚未绑定已确认方案版本。在上方“烘焙方案版本”面板完成绑定后，这里按锚点对齐展示每段偏差与容差。</div>
{:else}
  {@const binding = planSection.binding}
  {@const ver = binding.plan_version}
  {@const ev = planSection.current_evaluation}
  {@const result = ev?.result}

  <div class="row" style="align-items:baseline;gap:10px;flex-wrap:wrap">
    <h2 style="margin:0">方案偏差审阅</h2>
    <span class="tag">方案版本 v{ver.version_no} · {ver.status}</span>
    <span class="muted" style="font-size:11px">内容指纹 {ver.content_sha256.slice(0, 16)}…</span>
    <span class="muted" style="font-size:11px">{ver.change_note}</span>
  </div>

  {#if planSection.stale}
    <div class="warn" style="margin-top:8px">
      ⚠ 该判断<b>需要重新审阅</b>：{planSection.stale_reason}
      <button on:click={reReview} disabled={busy} style="margin-left:10px">
        {busy ? '重新计算中…' : '按当前锚点重新生成判断（旧判断保留）'}
      </button>
    </div>
  {/if}
  {#if error}<div class="warn">⚠ {error}</div>{/if}

  {#if !ev}
    <div class="muted" style="margin-top:6px">绑定记录存在，但尚无存储的判断。</div>
  {:else}
    <div class="row" style="gap:8px;align-items:center;margin-top:6px">
      <span class="muted">判断状态：</span>
      <span class="tag {ev.review_state === 'current' ? 'ok' : 'neutral'}">
        {ev.review_state === 'current' ? '当前' : ev.review_state === 'needs_review' ? '需要重新审阅' : '已作废'}
      </span>
      <span class="muted" style="font-size:11px">
        整体结论：<b class="{verdictClass(result.overall_verdict)}">{verdictText(result.overall_verdict)}</b>
      </span>
      <span class="muted" style="font-size:11px">生成于 {ev.created_at.replace('T', ' ').slice(0, 19)}</span>
      <button class="ghost" on:click={toggleHistory}>
        {showHistory ? '隐藏历史判断' : '查看历史绑定与判断'}
      </button>
    </div>

    <table style="margin-top:8px">
      <tr>
        <th>目标段（按锚点对齐）</th>
        <th>起→止</th>
        <th>实测时长 / 目标</th>
        <th>曲线最大偏差</th>
        <th>判定</th>
        <th>未评估原因 / 依据</th>
      </tr>
      {#each result.segments as seg}
        <tr style={seg.verdict === 'unevaluated' ? 'background:rgba(168,155,140,.06)' : ''}>
          <td>
            <b>{seg.label}</b>
            <div class="muted" style="font-size:10px">
              {anchorLabel(seg.from_anchor)} → {anchorLabel(seg.to_anchor)} · ±{seg.temp_tolerance_c}°C
            </div>
          </td>
          <td style="font-size:11px">
            {seg.from_t_s !== null ? `${fmtTime(seg.from_t_s)} → ${fmtTime(seg.to_t_s)}` : '—'}
          </td>
          <td>
            {seg.duration_s !== null ? fmtTime(seg.duration_s) : '—'}
            {#if seg.target_duration_s}
              <span class="muted" style="font-size:11px">
                / 目标 {fmtTime(seg.target_duration_s)}±{fmtTime(seg.duration_tolerance_s)}
              </span>
            {/if}
            <div class="{seg.duration_verdict === 'outside_tolerance' ? 'bad' : 'muted'}"
                 style="font-size:10px">
              {seg.duration_verdict ? (seg.duration_verdict === 'within_tolerance' ? '时长在容差内' : `时长偏差 ${seg.duration_deviation_s > 0 ? '+' : ''}${seg.duration_deviation_s.toFixed(0)}s`) : ''}
            </div>
          </td>
          <td>
            {seg.max_abs_deviation_c !== null ? `${seg.max_abs_deviation_c.toFixed(1)}°C` : '—'}
            <div class="muted" style="font-size:10px">
              实测格 {seg.n_measured_bins} · 插值格 {seg.n_interpolated_bins} · 长缺测格 {seg.n_long_gap_bins}
            </div>
          </td>
          <td><span class="tag {verdictClass(seg.verdict)}">{verdictText(seg.verdict)}</span></td>
          <td style="font-size:11px;max-width:340px">
            {#if seg.verdict === 'unevaluated'}
              {#each seg.reasons as r}
                <div>
                  <b class="neutral">[{PLAIN_REASON(r.code)}]</b> {r.message}
                </div>
              {/each}
            {:else}
              <span class="muted">
                仅以 {seg.n_measured_bins} 个实测时间格为依据；
                {seg.curve_verdict === 'within_tolerance' ? '全部实测格在容差带内' : '存在超出容差带的实测格'}。
              </span>
            {/if}
          </td>
        </tr>
        {#if seg.bins.length}
          <tr>
            <td colspan="6" style="padding:0 10px 8px">
              <div class="bins">
                {#each seg.bins as b}
                  <span class="bin {b.kind} {b.kind === 'measured' ? (b.within_tolerance ? 'in' : 'out') : ''}"
                    title="{b.kind === 'measured' ? `实测 ${b.observed_temp_c?.toFixed(1)}°C / 目标 ${b.target_temp_c?.toFixed(1)}°C / 偏差 ${b.deviation_c?.toFixed(1)}°C` : ({measured:'实测',interpolated:'插值(非依据)',long_gap:'长缺测(非依据)',no_data:'无数据'}[b.kind])}">
                    {b.kind === 'measured' ? (b.within_tolerance ? '✓' : '✗') : b.kind === 'interpolated' ? '◇' : b.kind === 'long_gap' ? '—' : '·'}
                  </span>
                {/each}
              </div>
            </td>
          </tr>
        {/if}
      {/each}
    </table>

    <div class="muted" style="font-size:11px;margin-top:6px">
      依据策略：{result.evidence_policy}
    </div>
    <div class="warn" style="margin-top:4px;font-size:12px">{result.disclaimer}</div>

    {#if showHistory}
      <h3 style="margin:12px 0 4px">历史绑定与判断（只读）</h3>
      <table>
        <tr><th>绑定</th><th>版本</th><th>判断时间</th><th>状态</th><th>整体结论</th><th>依据指纹</th></tr>
        {#each history as b, bi}
          {#each b.evaluations as e}
            <tr>
              <td>#{b.id}{b.superseded ? '（已被取代）' : '（当前）'}</td>
              <td>v{b.plan_version?.version_no}</td>
              <td style="font-size:11px">{e.created_at.replace('T', ' ').slice(0, 19)}</td>
              <td>
                <span class="tag {e.review_state === 'current' ? 'ok' : 'neutral'}">
                  {{ current: '当前', needs_review: '待重审', obsolete: '已作废' }[e.review_state]}
                </span>
              </td>
              <td>{verdictText(e.result.overall_verdict)}</td>
              <td class="muted" style="font-size:10px">{e.anchor_fingerprint.slice(0, 12)}…</td>
            </tr>
          {/each}
        {/each}
      </table>
    {/if}
  {/if}
{/if}

<style>
  .bins {
    display: flex;
    gap: 3px;
    padding: 4px 0;
  }
  .bin {
    flex: 1;
    text-align: center;
    font-size: 10px;
    border-radius: 3px;
    padding: 2px 0;
    min-width: 18px;
  }
  .bin.in {
    background: rgba(95, 208, 138, 0.22);
    color: #5fd08a;
  }
  .bin.out {
    background: rgba(227, 93, 93, 0.25);
    color: #e35d5d;
  }
  .bin.interpolated {
    background: rgba(243, 201, 139, 0.16);
    color: #f3c98b;
    border: 1px dashed rgba(243, 201, 139, 0.5);
  }
  .bin.long_gap {
    background: rgba(168, 155, 140, 0.18);
    color: #a89b8c;
  }
  .bin.no_data {
    color: #6a6159;
  }
</style>
