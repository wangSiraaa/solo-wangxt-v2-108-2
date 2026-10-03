<script>
  // 方案偏差审阅面板（验收①③⑤）：
  //  - 按锚点对齐逐段展示目标/实际/偏差/容差/状态与可读原因；
  //  - 未评估段（缺锚点、跨长断档、只有插值证据）显式标灰并给原因，
  //    绝无“插值=通过”的呈现；
  //  - 手工修正锚点后显示“需要重新审阅”横幅，历史结论与依据仍可展开回看。
  import { createEventDispatcher } from 'svelte';
  import {
    runAssessment,
    getAssessmentHistory,
    getBindingHistory,
    fmtTime,
    SEGMENT_LABELS,
    VERDICT_LABELS,
    SEG_STATUS_LABELS,
    REVIEW_STATE_LABELS,
  } from './api.js';

  export let batchId = null;
  export let block = null; // series.plan: binding + version_snapshot + assessment

  const dispatch = createEventDispatcher();
  let busy = false;
  let errMsg = '';
  let history = [];
  let bindings = [];
  let showHistory = false;

  async function reassess() {
    if (!batchId) return;
    busy = true;
    errMsg = '';
    try {
      await runAssessment(batchId, { assessed_by: '操作员(界面)' });
      dispatch('changed');
    } catch (e) {
      errMsg = e.message;
    } finally {
      busy = false;
    }
  }

  async function toggleHistory() {
    showHistory = !showHistory;
    if (showHistory && batchId) {
      try {
        [history, bindings] = await Promise.all([
          getAssessmentHistory(batchId),
          getBindingHistory(batchId),
        ]);
      } catch (e) {
        errMsg = e.message;
      }
    }
  }

  $: assessment = block?.assessment || null;
  $: result = assessment?.result || null;
  $: segments = result?.segments || [];
  $: anchors = result?.anchors || {};

  const STATUS_CLASS = { pass: 'ok', fail: 'bad', unevaluable: 'na' };

  function durCheck(seg) {
    return seg.checks?.find((c) => c.kind === 'duration');
  }
  function tempChecks(seg) {
    return seg.checks?.filter((c) => c.kind !== 'duration') || [];
  }
  function fmtDev(v, unit) {
    if (v === null || v === undefined) return '—';
    const s = v > 0 ? `+${v}` : `${v}`;
    return `${s}${unit}`;
  }
  function fmtDateTime(iso) {
    if (!iso) return '—';
    return new Date(iso).toLocaleString('zh-CN', { hour12: false });
  }
  function verdictOf(a) {
    return VERDICT_LABELS[a?.result?.verdict] || a?.result?.verdict || '—';
  }
</script>

{#if !block}
  <div class="muted" style="font-size:13px">
    本批次尚未绑定烘焙方案版本。绑定一个<b>已确认</b>版本后，这里按锚点对齐展示逐段偏差、容差带，并持久保存结论。
  </div>
{:else}
  <div class="row" style="justify-content:space-between;align-items:center">
    <div>
      <b>{block.version_snapshot.plan_name}</b>
      <span class="tag st-confirmed">v{block.version_snapshot.version_no} · 已确认不可变</span>
      <span class="muted" style="font-size:11px">hash {block.version_snapshot.content_hash.slice(0, 12)}</span>
    </div>
    <div class="row" style="gap:8px">
      <button on:click={reassess} disabled={busy}>
        {busy ? '重算中…' : '按当前事件重算并持久保存结论'}
      </button>
      <button class="ghost" on:click={toggleHistory}>
        {showHistory ? '隐藏历史判断' : '查看历史判断/绑定'}
      </button>
    </div>
  </div>

  {#if errMsg}<div class="warn">⚠ {errMsg}</div>{/if}

  {#if !assessment}
    <div class="muted">已绑定但还没有结论，点上方按钮生成首次偏差判断。</div>
  {:else}
    {#if assessment.review_state === 'needs_review'}
      <div class="warn review-banner">
        ⚠ <b>该方案判断需要重新审阅</b>（{REVIEW_STATE_LABELS.needs_review}）。
        原因：{assessment.needs_review_reason}
        <div style="margin-top:4px">
          下方展示的仍是<b>修正前的历史结论及其锚点依据</b>；确认/修正事件后请点“重算”生成新结论，
          历史行不会被覆盖。
        </div>
      </div>
    {/if}

    <div class="row" style="gap:8px;align-items:center;font-size:13px">
      <span>总体：<b>{VERDICT_LABELS[result.verdict] || result.verdict}</b></span>
      <span class="tag ok">在容差内 {result.counts.pass}</span>
      <span class="tag bad">超出容差 {result.counts.fail}</span>
      <span class="tag na">未评估 {result.counts.unevaluable}（不视为通过）</span>
      <span class="muted" style="font-size:12px">{result.summary}</span>
    </div>

    <table class="deviation">
      <tr>
        <th>目标段（锚点相对）</th>
        <th>锚点区间</th>
        <th>实际/目标时长</th>
        <th>时长偏差</th>
        <th>温度检查</th>
        <th>状态</th>
      </tr>
      {#each segments as seg}
      <tr class="row-{STATUS_CLASS[seg.status]}">
        <td>
          <b>{SEGMENT_LABELS[seg.id] || seg.id}</b>
          <div class="muted" style="font-size:11px">{seg.from_anchor} → {seg.to_anchor}</div>
        </td>
        <td style="font-size:12px">
          {#if seg.missing_anchors?.length}
            <span class="muted">起/止锚点：</span>
            {seg.start_s === null ? '—' : fmtTime(seg.start_s)}
            {' → '}
            {seg.end_s === null ? '—' : fmtTime(seg.end_s)}
          {:else}
            {fmtTime(seg.start_s)} → {fmtTime(seg.end_s)}
          {/if}
        </td>
        <td style="font-size:12px">
          {#if seg.observed_duration_s === null}
            <span class="muted">无法定位</span>
          {:else}
            <b>{fmtTime(seg.observed_duration_s)}</b>
            <span class="muted"> / 目标 {fmtTime(durCheck(seg)?.target_s)}</span>
          {/if}
        </td>
        <td>
          {#if durCheck(seg)}
            {#if seg.status === 'unevaluable'}
              <span class="muted">{fmtDev(durCheck(seg).deviation_s, 's')}</span>
              <div class="muted" style="font-size:11px">时长可观测，但整段未评估，不判达标（容差 ±{durCheck(seg).tolerance_s}s）</div>
            {:else}
              <span class="{durCheck(seg).within_tolerance ? 'ok' : 'bad'}">
                {fmtDev(durCheck(seg).deviation_s, 's')}
              </span>
              <div class="muted" style="font-size:11px">容差 ±{durCheck(seg).tolerance_s}s</div>
            {/if}
          {:else}
            <span class="muted">—</span>
          {/if}
        </td>
        <td style="font-size:11px">
          {#each tempChecks(seg) as c}
            <div>
              {c.kind === 'end_temp' ? '段末温度' : '曲线点'}：
              {#if c.status === 'unevaluable'}
                <span class="na">未评估</span>
              {:else}
                <span class="{c.within_tolerance ? 'ok' : 'bad'}">{fmtDev(c.deviation_c, '°C')}</span>
                <span class="muted"> / ±{c.tolerance_c}°C</span>
              {/if}
            </div>
          {/each}
        </td>
        <td>
          <span class="seg-badge {STATUS_CLASS[seg.status]}">{SEG_STATUS_LABELS[seg.status]}</span>
          {#if seg.reason_zh}
            <div class="reason">{seg.reason_zh}</div>
          {/if}
        </td>
      </tr>
      {/each}
    </table>

    <details>
      <summary class="muted" style="cursor:pointer;font-size:12px">
        判断依据（锚点来源、取证窗口、原始哈希）
      </summary>
      <div class="basis">
        <div class="muted" style="font-size:12px;margin:4px 0">
          锚点：
          {#each Object.entries(anchors) as [name, info]}
            <span class="tag">{name}: {info === null ? '缺失' : `${fmtTime(info.t_s)} (${info.source})`}</span>
          {/each}
        </div>
        <div class="muted" style="font-size:12px">
          温度取证：检查时刻 ±{result.data_basis.temperature_evidence_window_s}s 内必须有<b>实测</b>豆温点；
          插值点计为证据：否；缺测计为通过：否。
          长缺测桥接上限 {result.data_basis.max_gap_fill_s}s。
        </div>
        <div class="muted mono" style="font-size:11px;margin-top:4px">
          assessment hash: {assessment.content_hash}
        </div>
        <div class="disclaimer">{result.disclaimer}</div>
      </div>
    </details>

    {#if showHistory}
      <div class="history">
        <b>历史判断（只追加，不覆盖）</b>
        <table style="margin-top:4px">
          <tr><th>#</th><th>时间</th><th>状态</th><th>结论</th><th>版本</th><th>备注/原因</th></tr>
          {#each history as h}
            <tr>
              <td>{h.id}</td>
              <td style="font-size:11px">{fmtDateTime(h.created_at)}</td>
              <td>
                <span class="seg-badge {h.review_state === 'current' ? 'ok' : h.review_state === 'needs_review' ? 'na' : ''}">
                  {REVIEW_STATE_LABELS[h.review_state] || h.review_state}
                </span>
              </td>
              <td style="font-size:12px">{verdictOf(h)}</td>
              <td style="font-size:11px">v{h.result?.basis?.plan_version_no ?? h.plan_version_id}</td>
              <td class="muted" style="font-size:11px;max-width:360px">{h.needs_review_reason || '—'}</td>
            </tr>
          {/each}
        </table>
        <b style="display:block;margin-top:8px">绑定历史（旧绑定保留）</b>
        <table style="margin-top:4px">
          <tr><th>#</th><th>时间</th><th>方案版本</th><th>是否当前</th><th>绑定人</th></tr>
          {#each bindings as bg}
            <tr style={bg.superseded ? 'opacity:.55' : ''}>
              <td>{bg.id}</td>
              <td style="font-size:11px">{fmtDateTime(bg.created_at)}</td>
              <td style="font-size:11px">{bg.plan_version?.plan_id && ''}version #{bg.plan_version_id}（v{bg.plan_version?.version_no} · {bg.plan_version?.status}）</td>
              <td>{bg.superseded ? '已被改绑取代（保留）' : '当前'}</td>
              <td style="font-size:11px">{bg.bound_by}</td>
            </tr>
          {/each}
        </table>
      </div>
    {/if}
  {/if}
{/if}

<style>
  .deviation td, .deviation th { vertical-align: top; }
  .seg-badge {
    display: inline-block; border-radius: 999px; padding: 1px 10px;
    font-size: 11px; border: 1px solid var(--line);
  }
  .seg-badge.ok, .ok { color: #8fe0aa; }
  .seg-badge.bad, .bad { color: #ef8f8f; }
  .seg-badge.na, .na { color: #c3b6a6; }
  .tag.ok { color: #8fe0aa; border-color: #2f6b45; }
  .tag.bad { color: #ef8f8f; border-color: #6b2f2f; }
  .tag.na { color: #c3b6a6; border-color: #4a423a; }
  .st-confirmed { color: #5fd08a; border-color: #2f6b45; }
  .row-pass { background: rgba(95, 208, 138, 0.05); }
  .row-fail { background: rgba(227, 93, 93, 0.06); }
  .row-unevaluable { background: rgba(168, 155, 140, 0.07); }
  .reason { font-size: 11px; color: #c3b6a6; margin-top: 3px; max-width: 300px; }
  .review-banner { font-size: 13px; }
  .disclaimer {
    margin-top: 6px; font-size: 12px; color: #f3c98b;
    border-top: 1px dashed var(--line); padding-top: 6px;
  }
  .basis { padding: 4px 0; }
  .history { margin-top: 10px; border-top: 1px dashed var(--line); padding-top: 8px; }
  .mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
  button:disabled { opacity: .4; cursor: not-allowed; }
</style>
