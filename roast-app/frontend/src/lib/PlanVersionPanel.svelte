<script>
  // 烘焙方案版本管理面板。
  //
  // 关键不变量（界面只允许这些路径）：
  //  - 已确认/退役版本没有“保存修改”按钮，目标调整只能“基于某版本创建新版本”；
  //  - 批次只能绑定已确认版本；绑定是只追加行，改绑保留旧行；
  //  - 两个编辑端可以拿着同一个旧版本同时改：后到者得到显式版本冲突（409），
  //    先到版本不被覆盖；点“同步到最新基础版本”后才能重新提交。
  import { createEventDispatcher } from 'svelte';
  import {
    listPlans,
    createPlan,
    confirmVersion,
    retireVersion,
    newDraftVersion,
    editDraft,
    seedDemoPlan,
    bindBatch,
    PLAN_STATUS_LABELS,
    SEGMENT_LABELS,
  } from './api.js';

  export let plans = [];
  export let batchId = null;
  export let batchPlanBlock = null;

  const dispatch = createEventDispatcher();
  let busy = '';
  let panelError = '';
  let notice = '';

  // new-plan mini form
  let newPlanName = '';
  let newDur = [50, 435, 120];
  let newDurTol = [25, 30, 30];
  let newEndTemp = [106, 192, 202];
  let newEndTol = [12, 10, 8];
  const SEG_IDS = ['drying', 'maillard', 'development'];

  // editor cards: each has its OWN base-version pointer and its OWN edits,
  // simulating two independent editing clients.
  function freshEditor(name) {
    return {
      name,
      by: name === '编辑端 A' ? 'roast-lead-A' : 'roast-lead-B',
      baseVersionId: null,
      dur: newDur.slice(),
      durTol: newDurTol.slice(),
      endTemp: newEndTemp.slice(),
      endTol: newEndTol.slice(),
      msg: '',
      kind: '', // ok | err
    };
  }
  let editorA = freshEditor('编辑端 A');
  let editorB = freshEditor('编辑端 B');

  // draft editing (revision-based conflict)
  let draftEdits = new Map(); // versionId -> {revision, dur...}

  export async function refresh() {
    try {
      plans = await listPlans();
      syncEditorBases(editorA);
      syncEditorBases(editorB);
      initDraftEdits();
    } catch (e) {
      panelError = e.message;
    }
  }

  function syncEditorBases(ed) {
    // default base = latest version of the first plan; keep an explicit
    // user choice otherwise (so a stale client stays stale until "同步").
    if (!plans.length) return;
    const latest = plans[0].versions[plans[0].versions.length - 1];
    if (ed.baseVersionId === null) ed.baseVersionId = latest.id;
  }

  function planOfVersion(vid) {
    return plans.find((p) => p.versions.some((v) => v.id === vid)) || null;
  }
  function versionOf(vid) {
    const p = planOfVersion(vid);
    return p ? p.versions.find((v) => v.id === vid) : null;
  }

  function initDraftEdits() {
    for (const p of plans) {
      for (const v of p.versions) {
        if (v.status === 'draft' && !draftEdits.has(v.id)) {
          draftEdits.set(v.id, {
            revision: v.revision,
            dur: v.definition.segments.map((s) => s.target_duration_s),
            durTol: v.definition.segments.map((s) => s.duration_tolerance_s),
            endTemp: v.definition.segments.map((s) => s.end_temp_c ?? 200),
            endTol: v.definition.segments.map((s) => s.end_temp_tolerance_c ?? 8),
            msg: '',
            kind: '',
          });
        }
      }
    }
  }

  function buildDefinition(ed) {
    return {
      segments: SEG_IDS.map((id, i) => ({
        id,
        target_duration_s: Number(ed.dur[i]),
        duration_tolerance_s: Number(ed.durTol[i]),
        end_temp_c: Number(ed.endTemp[i]),
        end_temp_tolerance_c: Number(ed.endTol[i]),
      })),
    };
  }

  function buildDefinitionFromDraft(d) {
    return {
      segments: SEG_IDS.map((id, i) => ({
        id,
        target_duration_s: Number(d.dur[i]),
        duration_tolerance_s: Number(d.durTol[i]),
        end_temp_c: Number(d.endTemp[i]),
        end_temp_tolerance_c: Number(d.endTol[i]),
      })),
    };
  }

  async function doSeedDemo() {
    busy = '生成并绑定演示方案…';
    panelError = '';
    try {
      await seedDemoPlan();
      await refresh();
      dispatch('changed');
      notice = '演示方案已确认并绑定两个合成批次，已生成初始偏差结论。';
    } catch (e) {
      panelError = e.message;
    } finally {
      busy = '';
    }
  }

  async function doCreatePlan() {
    busy = '创建方案草稿…';
    panelError = '';
    try {
      const def = buildDefinition({
        dur: newDur, durTol: newDurTol, endTemp: newEndTemp, endTol: newEndTol,
      });
      await createPlan({
        name: newPlanName || `PLAN-${Date.now()}`,
        created_by: 'roast-lead',
        definition: def,
      });
      newPlanName = '';
      await refresh();
    } catch (e) {
      panelError = e.message;
    } finally {
      busy = '';
    }
  }

  async function doConfirm(p, v) {
    panelError = '';
    try {
      await confirmVersion(p.id, v.id, { by: 'roast-lead' });
      await refresh();
    } catch (e) {
      panelError = e.message;
    }
  }

  async function doRetire(p, v) {
    panelError = '';
    try {
      await retireVersion(p.id, v.id, { by: 'roast-lead' });
      await refresh();
    } catch (e) {
      panelError = e.message;
    }
  }

  async function submitVersion(ed) {
    panelError = '';
    ed.msg = '';
    const base = versionOf(ed.baseVersionId);
    const plan = planOfVersion(ed.baseVersionId);
    if (!base || !plan) {
      ed.kind = 'err';
      ed.msg = '请先选择基础版本';
      return;
    }
    busy = `${ed.name} 提交新版本…`;
    try {
      const v = await newDraftVersion(plan.id, {
        based_on_version_id: base.id,
        definition: buildDefinition(ed),
        created_by: ed.by,
        note: `${ed.name} 基于 v${base.version_no} 的目标调整`,
      });
      ed.kind = 'ok';
      ed.msg = `已创建草稿 v${v.version_no}（基于 v${base.version_no}）。确认后才会成为已确认版本。`;
      await refresh();
    } catch (e) {
      ed.kind = 'err';
      if (e.status === 409) {
        const d = e.detail || {};
        ed.msg =
          d.message ||
          '版本冲突：另一端已基于该旧版本生成了新版本，本次改动未写入。请点“同步到最新基础版本”后合并重提。';
      } else {
        ed.msg = e.message;
      }
    } finally {
      busy = '';
    }
  }

  async function syncBase(ed) {
    panelError = '';
    const latest = plans[0].versions[plans[0].versions.length - 1];
    ed.baseVersionId = latest.id;
    const v = versionOf(latest.id);
    v.definition.segments.forEach((s, i) => {
      ed.dur[i] = s.target_duration_s;
      ed.durTol[i] = s.duration_tolerance_s;
      ed.endTemp[i] = s.end_temp_c ?? ed.endTemp[i];
      ed.endTol[i] = s.end_temp_tolerance_c ?? ed.endTol[i];
    });
    ed.kind = '';
    ed.msg = `已同步到最新基础版本 v${latest.version_no}（${PLAN_STATUS_LABELS[latest.status]}），请在此内容上合并你的改动。`;
  }

  async function saveDraftEdit(p, v) {
    const d = draftEdits.get(v.id);
    d.msg = '';
    panelError = '';
    busy = `保存草稿 v${v.version_no}…`;
    try {
      const updated = await editDraft(p.id, v.id, {
        definition: buildDefinitionFromDraft(d),
        expected_revision: d.revision,
      });
      d.revision = updated.revision;
      d.kind = 'ok';
      d.msg = `已保存，revision=${updated.revision}（确认前内容仍可改，确认后不可变）。`;
      await refresh();
    } catch (e) {
      d.kind = 'err';
      if (e.status === 409) {
        d.msg =
          e.detail?.message ||
          '草稿已被另一端更新：版本冲突，已拒绝覆盖。请刷新取最新 revision 后合并。';
      } else {
        d.msg = e.message;
      }
    } finally {
      busy = '';
    }
  }

  async function doBind(v) {
    if (!batchId) return;
    panelError = '';
    try {
      await bindBatch(batchId, v.id, { bound_by: 'operator(界面)' });
      notice = `批次已绑定 ${planOfVersion(v.id).name} v${v.version_no}（不可变链接）。`;
      dispatch('changed');
    } catch (e) {
      panelError = e.message;
    }
  }

  function setDraft(versionId, field, i, value) {
    const d = draftEdits.get(versionId);
    if (d) {
      d[field][i] = value === '' ? value : Number(value);
      draftEdits = draftEdits;
    }
  }
  function setEd(ed, field, i, value) {
    ed[field][i] = value === '' ? value : Number(value);
  }

  function statusTagClass(status) {
    return status === 'confirmed' ? 'st-confirmed' : status === 'draft' ? 'st-draft' : 'st-retired';
  }
  function shortHash(h) {
    return h ? h.slice(0, 10) : '';
  }
</script>

<div class="plan-panel">
  {#if panelError}<div class="warn">⚠ {panelError}</div>{/if}
  {#if notice}<div class="ok-note">✔ {notice}</div>{/if}
  {#if busy}<div class="muted">{busy}</div>{/if}

  <div class="row" style="align-items:flex-end;gap:10px">
    <button class="ghost" on:click={doSeedDemo}>③ 生成并绑定演示烘焙方案（已确认版本）</button>
    <div class="muted" style="font-size:12px">
      批次只能绑定<b>已确认</b>版本；方案模板后续修改只产生新版本，不改变本批次已绑定的历史版本。
    </div>
  </div>

  <!-- new plan -->
  <div class="sub">
    <div class="muted">新建方案（产生 v1 草稿）</div>
    <div class="row" style="gap:8px;align-items:center">
      <input bind:value={newPlanName} placeholder="方案名称，如 PLAN-2026-10" style="width:200px" />
      <button on:click={doCreatePlan}>创建草稿 v1</button>
    </div>
  </div>

  {#each plans as p}
    <div class="plan-chain">
      <div class="row" style="justify-content:space-between">
        <b>{p.name}</b>
        <span class="muted" style="font-size:12px">{p.description}</span>
      </div>

      <!-- draft revision editing -->
      {#each p.versions.filter((v) => v.status === 'draft') as v}
        {@const d = draftEdits.get(v.id)}
        <div class="draft-box">
          <div class="row" style="justify-content:space-between">
            <span class="tag st-draft">草稿 v{v.version_no} · revision {v.revision}</span>
            <span class="muted" style="font-size:11px">
              {v.based_on_version_id ? `基于 ${planOfVersion(v.based_on_version_id)?.name || ''} 的历史版本` : '初始草稿'}
            </span>
          </div>
          {#if d}
            <table class="seg-edit">
              <tr><th>段</th><th>目标时长 s</th><th>时长容差 ±s</th><th>段末温度 °C</th><th>温度容差 ±°C</th></tr>
              {#each SEG_IDS as sid, i}
                <tr>
                  <td>{SEGMENT_LABELS[sid]}</td>
                  <td><input type="number" value={d.dur[i]} on:input={(e) => setDraft(v.id, 'dur', i, e.target.value)} style="width:80px" /></td>
                  <td><input type="number" value={d.durTol[i]} on:input={(e) => setDraft(v.id, 'durTol', i, e.target.value)} style="width:80px" /></td>
                  <td><input type="number" value={d.endTemp[i]} on:input={(e) => setDraft(v.id, 'endTemp', i, e.target.value)} style="width:80px" /></td>
                  <td><input type="number" value={d.endTol[i]} on:input={(e) => setDraft(v.id, 'endTol', i, e.target.value)} style="width:80px" /></td>
                </tr>
              {/each}
            </table>
            <div class="row" style="gap:8px;margin-top:6px">
              <button on:click={() => saveDraftEdit(p, v)}>保存草稿（expected_revision={d.revision}）</button>
              <button class="ghost" on:click={() => doConfirm(p, v)}>确认此版本 → 已确认（不可变）</button>
              {#if d.msg}<span class="{d.kind === 'ok' ? 'ok-note' : 'warn'}" style="display:inline-block">{d.msg}</span>{/if}
            </div>
          {/if}
        </div>
      {/each}

      <!-- version timeline -->
      <table style="margin-top:6px">
        <tr><th>版本</th><th>状态</th><th>目标时长(脱水/梅纳/发展 s)</th><th>内容哈希</th><th>操作</th></tr>
        {#each p.versions as v}
          <tr style={v.status === 'retired' ? 'opacity:.55' : ''}>
            <td>v{v.version_no}</td>
            <td><span class="tag {statusTagClass(v.status)}">{PLAN_STATUS_LABELS[v.status]}</span></td>
            <td style="font-size:12px">
              {v.definition.segments.map((s) => s.target_duration_s).join(' / ')}
              <span class="muted">（±{v.definition.segments.map((s) => s.duration_tolerance_s).join('/')}）</span>
            </td>
            <td class="mono" style="font-size:11px">{shortHash(v.content_hash)}</td>
            <td>
              {#if v.status === 'draft'}
                <span class="muted" style="font-size:11px">在上方编辑/确认</span>
              {:else if v.status === 'confirmed'}
                <button class="ghost" on:click={() => doBind(v)} disabled={!batchId}>绑定当前批次</button>
                <button class="ghost" on:click={() => doRetire(p, v)}>退役</button>
              {:else}
                <button class="ghost" on:click={() => doBind(v)} disabled title="退役版本不能新绑定">已退役</button>
              {/if}
            </td>
          </tr>
        {/each}
      </table>
    </div>
  {/each}

  <!-- two independent editors: optimistic-concurrency demo -->
  <div class="sub">
    <div class="muted">两个编辑端基于同一旧版本提交不同改动（验收④）</div>
    <div class="row" style="gap:12px;align-items:flex-start">
      {#each [editorA, editorB] as ed}
        <div class="editor-card">
          <b>{ed.name}</b>
          <div style="margin:4px 0">
            <span class="muted" style="font-size:12px">基础版本：</span>
            <select bind:value={ed.baseVersionId}>
              {#each plans[0]?.versions || [] as v}
                <option value={v.id}>
                  v{v.version_no} · {PLAN_STATUS_LABELS[v.status]}
                </option>
              {/each}
            </select>
          </div>
          <table class="seg-edit">
            <tr><th>段</th><th>目标 s</th><th>±s</th></tr>
            {#each SEG_IDS as sid, i}
              <tr>
                <td>{SEGMENT_LABELS[sid]}</td>
                <td><input type="number" value={ed.dur[i]} on:input={(e) => setEd(ed, 'dur', i, e.target.value)} style="width:72px" /></td>
                <td><input type="number" value={ed.durTol[i]} on:input={(e) => setEd(ed, 'durTol', i, e.target.value)} style="width:64px" /></td>
              </tr>
            {/each}
          </table>
          <div class="row" style="gap:6px;margin-top:6px">
            <button on:click={() => submitVersion(ed)}>基于所选版本提交新版本</button>
            <button class="ghost" on:click={() => syncBase(ed)}>同步到最新基础版本</button>
          </div>
          {#if ed.msg}
            <div class="{ed.kind === 'ok' ? 'ok-note' : 'warn'}" style="margin-top:6px">{ed.msg}</div>
          {/if}
        </div>
      {/each}
    </div>
  </div>

  {#if batchPlanBlock}
    <div class="sub">
      <div class="muted">当前批次 A 绑定</div>
      <div>
        <b>{batchPlanBlock.version_snapshot.plan_name}</b>
        <span class="tag st-confirmed">v{batchPlanBlock.version_snapshot.version_no} · {PLAN_STATUS_LABELS[batchPlanBlock.version_snapshot.status]}</span>
        <span class="mono muted" style="font-size:11px">hash {shortHash(batchPlanBlock.version_snapshot.content_hash)}</span>
      </div>
      <div class="muted" style="font-size:12px">
        绑定人 {batchPlanBlock.binding.bound_by}；快照即判断依据，不随后续版本修改。
      </div>
    </div>
  {/if}
</div>

<style>
  .plan-panel { display: flex; flex-direction: column; gap: 10px; }
  .sub { border-top: 1px dashed var(--line); padding-top: 8px; margin-top: 2px; }
  .plan-chain { border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; }
  .draft-box { background: #2a2418; border: 1px solid #5a4a22; border-radius: 8px; padding: 8px; margin: 6px 0; }
  .editor-card { flex: 1; min-width: 300px; border: 1px solid var(--line); border-radius: 8px; padding: 8px 10px; background: #251f1a; }
  .seg-edit input { width: 100%; }
  .mono { font-family: ui-monospace, Menlo, Consolas, monospace; }
  .st-draft { color: #e6c46a; border-color: #6b5a26; }
  .st-confirmed { color: #5fd08a; border-color: #2f6b45; }
  .st-retired { color: #a89b8c; border-color: #4a423a; }
  .ok-note {
    background: #1f2c22; border: 1px solid #2f6b45; color: #8fe0aa;
    border-radius: 8px; padding: 6px 10px; font-size: 12px;
  }
  button:disabled { opacity: .4; cursor: not-allowed; }
</style>
