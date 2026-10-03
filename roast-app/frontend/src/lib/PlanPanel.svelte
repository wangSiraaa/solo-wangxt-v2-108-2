<script>
  // Roast-plan management: immutable versions, draft/confirmed/retired
  // lifecycle, batch binding, and explicit optimistic-concurrency errors.
  import { createEventDispatcher, onMount } from 'svelte';
  import {
    bindPlan,
    createPlanVersion,
    listPlans,
    seedDefaultPlan,
    setPlanVersionStatus,
  } from './api.js';

  export let batches = [];
  export let bindBatchId = null;

  const dispatch = createEventDispatcher();

  let plans = [];
  let loading = '';
  let error = '';
  let conflict = '';

  // "new version" form
  let selectedPlanId = null;
  let baseVersionNo = null;
  let editorText = '';
  let changeNote = '';
  let bindVersionId = '';

  $: headOf = (p) =>
    p.versions.reduce((a, v) => (v.version_no > (a?.version_no ?? 0) ? v : a), null);

  onMount(reload);

  export async function reload() {
    error = '';
    loading = '加载方案…';
    try {
      plans = await listPlans();
      if (!selectedPlanId && plans.length) selectedPlanId = plans[0].id;
      syncEditor();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  function syncEditor() {
    const p = plans.find((x) => x.id === selectedPlanId);
    if (p) {
      const head = headOf(p);
      baseVersionNo = head.version_no;
      editorText = JSON.stringify(head.content, null, 2);
      bindVersionId =
        p.versions.find((v) => v.status === 'confirmed')?.id ?? '';
    }
  }

  $: if (selectedPlanId) syncEditor();

  async function doSeedDefault() {
    loading = '载入内置模板（草稿）…';
    error = '';
    conflict = '';
    try {
      await seedDefaultPlan();
      await reload();
    } catch (e) {
      error = e.message;
    } finally {
      loading = '';
    }
  }

  async function confirmVersion(p, v) {
    error = '';
    conflict = '';
    try {
      await setPlanVersionStatus(p.id, v.version_no, 'confirm');
      await reload();
      dispatch('changed');
    } catch (e) {
      conflict = e.message;
    }
  }

  async function retireVersion(p, v) {
    error = '';
    conflict = '';
    try {
      await setPlanVersionStatus(p.id, v.version_no, 'retire');
      await reload();
    } catch (e) {
      conflict = e.message;
    }
  }

  async function submitNewVersion() {
    let content;
    conflict = '';
    error = '';
    try {
      content = JSON.parse(editorText);
    } catch (e) {
      error = `JSON 解析失败：${e.message}`;
      return;
    }
    loading = `基于 v${baseVersionNo} 提交新版本…`;
    try {
      const v = await createPlanVersion(selectedPlanId, {
        base_version_no: Number(baseVersionNo),
        content,
        change_note: changeNote || '界面编辑',
        created_by: 'roast-lead(界面)',
      });
      changeNote = '';
      await reload();
      // keep the editor based on the freshly created HEAD
      baseVersionNo = v.version_no;
      dispatch('changed');
    } catch (e) {
      if (e.status === 409) {
        // The later editor must see the explicit conflict and rebase; the
        // early submit is never overwritten.
        conflict = e.message;
      } else {
        error = e.message;
      }
    } finally {
      loading = '';
    }
  }

  async function doBind() {
    if (!bindBatchId || !bindVersionId) return;
    loading = '绑定并生成偏差判断…';
    error = '';
    conflict = '';
    try {
      await bindPlan(bindBatchId, Number(bindVersionId));
      dispatch('changed');
    } catch (e) {
      conflict = e.message;
    } finally {
      loading = '';
    }
  }

  const statusTag = (s) =>
    s === 'confirmed' ? 'confirmed' : s === 'retired' ? 'retired' : 'draft';
  const statusLabel = (s) =>
    ({ draft: '草稿', confirmed: '已确认', retired: '已退役' })[s] || s;
</script>

<section class="panel">
  <h2>烘焙方案版本（不可变 · 可复审）</h2>
  <div class="row" style="align-items:center;gap:10px">
    <button on:click={doSeedDefault}>③ 载入内置目标曲线模板（草稿）</button>
    <button class="ghost" on:click={reload}>刷新方案列表</button>
    {#if loading}<span class="muted">{loading}</span>{/if}
  </div>
  {#if conflict}
    <div class="warn" style="margin-top:8px;white-space:pre-wrap">⛔ {conflict}</div>
  {/if}
  {#if error}
    <div class="warn" style="margin-top:8px">⚠ {error}</div>
  {/if}

  {#if plans.length === 0}
    <div class="muted" style="margin-top:8px">还没有方案。载入模板后先确认，再绑定批次。</div>
  {:else}
    <div class="row" style="margin-top:10px;align-items:flex-end;gap:12px">
      <div>
        <div class="muted">方案</div>
        <select bind:value={selectedPlanId}>
          {#each plans as p}
            <option value={p.id}>{p.name}（v{headOf(p).version_no}）</option>
          {/each}
        </select>
      </div>
      <div>
        <div class="muted">将批次绑定到已确认版本</div>
        <div class="row" style="gap:6px">
          <select bind:value={bindBatchId}>
            <option value={null}>选择批次…</option>
            {#each batches as b}
              <option value={b.id}>{b.name}</option>
            {/each}
          </select>
          {#if selectedPlanId}
            <select bind:value={bindVersionId}>
              <option value="">选择版本…</option>
              {#each plans.find((x) => x.id === selectedPlanId)?.versions ?? [] as v}
                <option value={v.id} disabled={v.status !== 'confirmed'}>
                  v{v.version_no} · {statusLabel(v.status)}
                </option>
              {/each}
            </select>
          {/if}
          <button on:click={doBind} disabled={!bindVersionId || !bindBatchId}>绑定并重算偏差</button>
        </div>
      </div>
    </div>

    {#each plans as p}
      {#if p.id === selectedPlanId}
        <table style="margin-top:10px">
          <tr>
            <th>版本</th><th>状态</th><th>变更说明</th><th>创建人</th><th>基准</th><th>操作</th>
          </tr>
          {#each p.versions as v}
            <tr style={v.status === 'retired' ? 'opacity:.55' : ''}>
              <td>
                <b>v{v.version_no}</b>
                <div class="muted" style="font-size:10px">{v.content_sha256.slice(0, 12)}</div>
              </td>
              <td><span class="tag {statusTag(v.status)}">{statusLabel(v.status)}</span></td>
              <td style="max-width:220px">{v.change_note}</td>
              <td style="font-size:11px">{v.created_by}</td>
              <td style="font-size:11px">{v.base_version_no ? `基于 v${v.base_version_no}` : '—'}</td>
              <td>
                {#if v.status === 'draft' && v.id === headOf(p).id}
                  <button class="ghost" on:click={() => confirmVersion(p, v)}>确认</button>
                {:else if v.status === 'confirmed'}
                  <button class="ghost" on:click={() => retireVersion(p, v)}>退役</button>
                {:else}
                  <span class="muted">内容冻结</span>
                {/if}
              </td>
            </tr>
          {/each}
        </table>

        <div style="margin-top:12px">
          <div class="muted">
            基于
            <select bind:value={baseVersionNo} style="width:auto">
              {#each p.versions as v}
                <option value={v.version_no}
                  disabled={v.version_no !== headOf(p).version_no}>
                  v{v.version_no}{v.version_no === headOf(p).version_no ? '（HEAD）' : '（非 HEAD，提交将冲突）'}
                </option>
              {/each}
            </select>
            编辑内容后提交新版本（旧版本不会被改写）
          </div>
          <div class="row" style="gap:8px;margin-top:4px">
            <input bind:value={changeNote} placeholder="变更说明（如：收紧发展期容差）"
              style="flex:1;min-width:220px" />
            <button on:click={submitNewVersion}>提交为新版本</button>
          </div>
          <textarea bind:value={editorText} rows="10" spellcheck="false"
            style="width:100%;margin-top:6px;font-family:monospace;font-size:11px;
                   background:#241f1a;color:#d8cfc3;border:1px solid var(--line);
                   border-radius:6px;padding:8px"></textarea>
        </div>
      {/if}
    {/each}
  {/if}
</section>
