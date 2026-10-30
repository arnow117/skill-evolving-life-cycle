"""Static HTML, CSS, and safe DOM code for SELC statistics reports."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

_STYLE = """
:root {
  color-scheme: light;
  --ink: #172033;
  --muted: #566074;
  --paper: #f4f6f9;
  --surface: #ffffff;
  --line: #ccd4df;
  --accent: #075985;
  --accent-soft: #dff3ff;
  --danger: #9f1239;
  --danger-soft: #ffe4e6;
  --focus: #0f766e;
}

* {
  box-sizing: border-box;
}

body {
  margin: 0;
  background: var(--paper);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  font-size: 16px;
  line-height: 1.5;
}

button,
select,
input {
  font: inherit;
}

button,
select {
  min-height: 2.75rem;
  border: 1px solid #8794a7;
  border-radius: .5rem;
  background: var(--surface);
  color: var(--ink);
  padding: .45rem .65rem;
}

button {
  cursor: pointer;
  font-weight: 650;
}

button:hover {
  border-color: var(--accent);
  background: #f0faff;
}

button:focus-visible,
select:focus-visible,
input:focus-visible {
  outline: 3px solid var(--focus);
  outline-offset: 2px;
}

main {
  width: min(100% - 2rem, 78rem);
  margin: 0 auto;
  padding: 2rem 0 4rem;
}

header,
.panel {
  border: 1px solid var(--line);
  border-radius: .9rem;
  background: var(--surface);
}

header {
  padding: 1.6rem;
}

h1,
h2 {
  margin: 0;
  letter-spacing: -.02em;
  line-height: 1.15;
}

h1 {
  font-size: clamp(1.8rem, 5vw, 3rem);
}

h2 {
  margin-bottom: .45rem;
  font-size: 1.25rem;
}

p {
  max-width: 72ch;
  margin: .55rem 0 0;
  color: var(--muted);
}

.meta,
.notice {
  margin-top: 1rem;
  border-radius: .5rem;
  background: var(--accent-soft);
  color: #164e63;
  padding: .8rem;
}

.notice[hidden] {
  display: none;
}

.notice.error {
  background: var(--danger-soft);
  color: var(--danger);
}

.workspace {
  display: grid;
  gap: 1rem;
  margin-top: 1.4rem;
}

.panel {
  min-width: 0;
  padding: 1.25rem;
}

.toolbar {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  align-items: end;
  gap: .75rem;
}

.control {
  display: grid;
  gap: .3rem;
  color: var(--muted);
  font-size: .9rem;
}

.toggle {
  display: flex;
  min-height: 2.75rem;
  align-items: center;
  gap: .5rem;
  color: var(--ink);
}

.toggle input {
  inline-size: 1.1rem;
  block-size: 1.1rem;
  accent-color: var(--focus);
}

.actions {
  display: flex;
  align-items: end;
}

.table-shell {
  overflow-x: auto;
  border: 1px solid var(--line);
  border-radius: .55rem;
}

table {
  width: 100%;
  min-width: 46rem;
  border-collapse: collapse;
}

.overview-table {
  min-width: 70rem;
}

th,
td {
  border-bottom: 1px solid var(--line);
  padding: .7rem .8rem;
  text-align: left;
  vertical-align: top;
}

th {
  background: #edf2f7;
  color: #334155;
  font-size: .85rem;
  letter-spacing: .01em;
}

tbody tr:last-child td {
  border-bottom: 0;
}

.numeric {
  white-space: nowrap;
  text-align: right;
  font-variant-numeric: tabular-nums;
}

.status {
  font-weight: 650;
}

.status.error {
  color: var(--danger);
}

.status.empty {
  color: #7c2d12;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 1rem;
}

.metric-card {
  border: 1px solid var(--line);
  border-radius: .65rem;
  padding: 1rem;
}

.metric-card h3 {
  margin: 0;
  font-size: 1rem;
}

.metric-card dl {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: .6rem;
  margin: .8rem 0 0;
}

.metric-card dt {
  color: var(--muted);
  font-size: .82rem;
}

.metric-card dd {
  margin: .05rem 0 0;
  font-size: 1.15rem;
  font-variant-numeric: tabular-nums;
}

.metric-copy,
.empty {
  color: var(--muted);
}

@media (max-width: 48rem) {
  main {
    width: min(100% - 1rem, 78rem);
    padding-top: .75rem;
  }

  header,
  .panel {
    padding: 1rem;
  }

  .toolbar,
  .metric-grid {
    grid-template-columns: 1fr;
  }

  .actions {
    align-items: stretch;
  }

  .actions button {
    width: 100%;
  }
}

@media (prefers-reduced-motion: no-preference) {
  .panel {
    animation: reveal .22s ease-out both;
  }

  @keyframes reveal {
    from {
      opacity: .98;
      transform: translateY(2px);
    }

    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
}
""".strip()

_SCRIPT = """
const report = JSON.parse(document.getElementById('report-data').textContent);
const initialProject = report.projects.find((project) => project.status !== 'error');
const state = {
  projectId: initialProject?.id ?? '',
  includeTest: report.include_test,
  suite: '',
  version: '',
  harness: '',
};
const byId = (id) => document.getElementById(id);
const metricKeys = ['N', 'S', 'K', 'U'];

function element(name, text) {
  const node = document.createElement(name);
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) {
  node.replaceChildren();
}

function selectedProject() {
  return report.projects.find((project) => project.id === state.projectId);
}

function selectedProjection(project) {
  const key = state.includeTest ? 'with_test' : 'without_test';
  return project?.projections?.[key] ?? { cells: [], metrics: null };
}

function selectedCells(project) {
  return selectedProjection(project).cells.filter((cell) => {
    const matchesSuite = !state.suite || cell.group.suite_id === state.suite;
    const matchesVersion = !state.version || cell.group.suite_version === state.version;
    const matchesHarness = !state.harness || cell.group.harness === state.harness;
    return matchesSuite && matchesVersion && matchesHarness;
  });
}

function sum(cells, kind, axis) {
  const result = { N: 0, S: 0, K: 0, U: 0 };
  for (const cell of cells) {
    if (cell.kind !== kind) continue;
    for (const key of metricKeys) result[key] += cell[axis][key];
  }
  return result;
}

function rateText(metric) {
  if (metric.N === 0) return '无样本，比例不适用';
  const knownRate = metric.K === 0 ? 'S/K 无已知结果样本' : `S/K ${percentage(metric.S, metric.K)}`;
  return [
    `S/N ${percentage(metric.S, metric.N)}`,
    knownRate,
    `K/N ${percentage(metric.K, metric.N)}`,
  ].join(' · ');
}

function percentage(numerator, denominator) {
  if (denominator === 0) return '不适用';
  return `${((numerator / denominator) * 100).toFixed(1)}%`;
}

function metricText(metric) {
  if (metric.N === 0) return 'N 0 · 无样本，比例不适用';
  return `N ${metric.N} · S ${metric.S} · K ${metric.K} · U ${metric.U} · ${rateText(metric)}`;
}

function optionValues(project, field) {
  const projections = Object.values(project?.projections ?? {});
  const values = projections.flatMap((projection) => projection.cells ?? [])
    .map((cell) => cell.group[field])
    .filter((value) => typeof value === 'string');
  return [...new Set(values)].sort();
}

function setOptions(select, values, current, label) {
  clear(select);
  select.append(new Option(label, ''));
  for (const value of values) select.append(new Option(value, value));
  select.value = current;
}

function renderOverview() {
  const body = byId('project-overview');
  clear(body);
  for (const project of report.projects) {
    const row = element('tr');
    const statusText = projectStatusText(project);
    const status = element('td', statusText);
    status.className = `status ${project.status === 'error' ? 'error' : project.status}`;
    row.append(
      element('td', project.name),
      status,
      element('td', project.read_at),
      element('td', modeDescription(project)),
      overviewMetric(project, 'task', 'one_shot'),
      overviewMetric(project, 'task', 'final'),
      overviewMetric(project, 'call', 'one_shot'),
      overviewMetric(project, 'call', 'final'),
    );
    body.append(row);
  }
}

function projectStatusText(project) {
  if (project.status === 'error') {
    return `${project.error.code}：${project.error.message} · 恢复：${project.error.recovery}`;
  }
  if (project.status === 'basic_no_records') return '基础模式：默认不采集';
  if (selectedProjection(project).cells.length === 0) {
    const scope = state.includeTest ? '包含测试样本' : '不含测试样本';
    return `可读记录库：当前统计范围（${scope}）暂无样本`;
  }
  return '可读快照';
}

function projectionScopeText() {
  return state.includeTest ? '当前统计范围包含测试样本。' : '当前统计范围不包含测试样本。';
}

function modeDescription(project) {
  if (project.status === 'basic_no_records') return '基础模式，默认不采集统计记录';
  if (project.mode === 'observed') return '实验采集模式；不代表采集健康';
  if (project.mode === 'basic') return '基础模式；不代表采集健康';
  return '安装记录未核实；不代表采集健康';
}

function overviewMetric(project, kind, axis) {
  const cell = element('td');
  cell.className = 'metric-copy';
  if (project.status === 'error') {
    cell.textContent = '—';
  } else if (project.status === 'basic_no_records') {
    cell.textContent = '默认不采集';
  } else {
    cell.textContent = metricText(sum(selectedProjection(project).cells, kind, axis));
  }
  return cell;
}

function renderControls(project) {
  const selector = byId('project-select');
  clear(selector);
  for (const projectItem of report.projects) {
    const option = new Option(projectItem.name, projectItem.id);
    option.disabled = projectItem.status === 'error';
    selector.append(option);
  }
  selector.value = state.projectId;
  byId('include-test').checked = state.includeTest;
  setOptions(byId('suite-filter'), optionValues(project, 'suite_id'), state.suite, '全部套件');
  setOptions(byId('version-filter'), optionValues(project, 'suite_version'), state.version, '全部版本');
  setOptions(byId('harness-filter'), optionValues(project, 'harness'), state.harness, '全部运行环境');
}

function renderMetrics(project) {
  const target = byId('metrics');
  clear(target);
  const cells = selectedCells(project);
  const definitions = [
    ['task', 'one_shot', '任务：首次结果'],
    ['task', 'final', '任务：最终结果'],
    ['call', 'one_shot', '调用：首次结果'],
    ['call', 'final', '调用：最终结果'],
  ];
  for (const [kind, axis, title] of definitions) {
    const metric = sum(cells, kind, axis);
    const card = element('section');
    const list = element('dl');
    card.className = 'metric-card';
    card.append(element('h3', title));
    for (const key of metricKeys) {
      const slot = element('div');
      slot.append(element('dt', key), element('dd', String(metric[key])));
      list.append(slot);
    }
    card.append(list, element('p', rateText(metric)));
    target.append(card);
  }
}

function groupDescription(cell) {
  const group = cell.group;
  const taskFields = [group.suite_id, group.suite_version, group.harness, group.initiator];
  const callFields = [...taskFields, group.skill, group.executor];
  return (cell.kind === 'task' ? taskFields : callFields).join(' · ');
}

function renderGroups(project) {
  const body = byId('group-table');
  const cells = selectedCells(project);
  clear(body);
  if (cells.length === 0) {
    const row = element('tr');
    const cell = element('td', '当前筛选范围没有样本。');
    cell.colSpan = 5;
    cell.className = 'empty';
    row.append(cell);
    body.append(row);
    return;
  }
  for (const cellData of cells) {
    const row = element('tr');
    row.append(
      element('td', cellData.kind === 'task' ? '任务' : '调用'),
      element('td', cellData.is_test ? '测试' : '非测试'),
      element('td', groupDescription(cellData)),
    );
    for (const axis of ['one_shot', 'final']) {
      const metric = cellData[axis];
      const metricCell = element(
        'td',
        `N ${metric.N} / S ${metric.S} / K ${metric.K} / U ${metric.U}`,
      );
      metricCell.className = 'numeric';
      row.append(metricCell);
    }
    body.append(row);
  }
}

function renderDetail() {
  const project = selectedProject();
  const notice = byId('detail-notice');
  renderOverview();
  renderControls(project);
  if (!project || project.status === 'error') {
    notice.hidden = false;
    notice.className = 'notice error';
    notice.textContent = '请选择可读取的项目来查看嵌入的快照。';
    byId('metrics').replaceChildren();
    byId('group-table').replaceChildren();
    return;
  }
  if (project.status === 'basic_no_records') {
    notice.hidden = false;
    notice.className = 'notice';
    notice.textContent = '此项目已核实为基础模式，默认不采集统计记录；这不是零样本统计，也不代表采集健康。';
    byId('metrics').replaceChildren();
    renderNonCollectionMessage();
    return;
  }
  notice.hidden = false;
  notice.className = 'notice';
  notice.textContent = `快照读取于 ${project.read_at}。${projectionScopeText()}筛选只重新计算本报告内的数据；需要更新记录时请重新生成。${modeDescription(project)}`;
  renderMetrics(project);
  renderGroups(project);
}

function renderNonCollectionMessage() {
  const body = byId('group-table');
  const row = element('tr');
  const cell = element('td', '基础模式默认不采集统计记录。');
  clear(body);
  cell.colSpan = 5;
  cell.className = 'empty';
  row.append(cell);
  body.append(row);
}

function resetFilters() {
  state.includeTest = report.include_test;
  state.suite = '';
  state.version = '';
  state.harness = '';
  renderDetail();
}

byId('project-select').addEventListener('change', (event) => {
  state.projectId = event.target.value;
  resetFilters();
});
byId('include-test').addEventListener('change', (event) => {
  state.includeTest = event.target.checked;
  renderDetail();
});
byId('suite-filter').addEventListener('change', (event) => {
  state.suite = event.target.value;
  renderDetail();
});
byId('version-filter').addEventListener('change', (event) => {
  state.version = event.target.value;
  renderDetail();
});
byId('harness-filter').addEventListener('change', (event) => {
  state.harness = event.target.value;
  renderDetail();
});
byId('reset-filters').addEventListener('click', resetFilters);
renderDetail();
byId('generated-at').textContent = report.generated_at;
""".strip()


def render_report(report: dict[str, Any]) -> str:
    """Embed count-only JSON in a CSP-restricted HTML report."""

    projects = report["projects"]
    for index, project in enumerate(projects, start=1):
        project["id"] = f"project-{index}"
    serialized = json.dumps(report, ensure_ascii=True, separators=(",", ":"))
    serialized = serialized.translate(
        str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026"})
    )
    style_hash = _csp_hash(_STYLE)
    script_hash = _csp_hash(_SCRIPT)
    csp = (
        "default-src 'none'; base-uri 'none'; connect-src 'none'; font-src 'none'; "
        "form-action 'none'; img-src 'none'; object-src 'none'; "
        f"script-src 'sha256-{script_hash}'; style-src 'sha256-{style_hash}'"
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>SELC 本地统计快照</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
  <header>
    <h1>本地统计快照</h1>
    <p>这是一个离线的项目计数快照。它不会刷新来源数据；需要查看较新的记录时请重新生成。</p>
    <div class="meta">生成时间：<time id="generated-at"></time>。N 为全部样本，S 为通过，K 为已知结果（通过或失败），U 为未知。</div>
  </header>
  <section class="workspace" aria-label="统计工作区">
    <section class="panel" aria-labelledby="overview-title">
      <h2 id="overview-title">已选择项目</h2>
      <p>概览按当前“包含测试样本”开关显示每个项目的四个统计轴。项目保持独立，因为来源可能重叠；这里不计算跨项目总计。</p>
      <div class="table-shell"><table class="overview-table"><thead><tr><th>项目</th><th>状态</th><th>读取时间</th><th>采集说明</th><th>任务：首次</th><th>任务：最终</th><th>调用：首次</th><th>调用：最终</th></tr></thead><tbody id="project-overview"></tbody></table></div>
    </section>
    <section class="panel" aria-labelledby="detail-title">
      <h2 id="detail-title">项目详情</h2>
      <div class="toolbar">
        <label class="control" for="project-select">项目<select id="project-select"></select></label>
        <label class="toggle" for="include-test"><input id="include-test" type="checkbox"> 包含测试样本</label>
        <label class="control" for="suite-filter">套件<select id="suite-filter"></select></label>
        <label class="control" for="version-filter">版本<select id="version-filter"></select></label>
        <label class="control" for="harness-filter">运行环境<select id="harness-filter"></select></label>
        <div class="actions"><button id="reset-filters" type="button">重置筛选</button></div>
      </div>
      <div id="detail-notice" class="notice" role="status"></div>
      <div id="metrics" class="metric-grid" aria-live="polite"></div>
    </section>
    <section class="panel" aria-labelledby="groups-title">
      <h2 id="groups-title">分组计数指标</h2>
      <p>不确定的来源保留为“unknown”。空范围会明确显示没有样本，不会展示编造的百分比。</p>
      <div class="table-shell"><table><thead><tr><th>对象</th><th>范围</th><th>分组</th><th class="numeric">首次结果</th><th class="numeric">最终结果</th></tr></thead><tbody id="group-table"></tbody></table></div>
    </section>
  </section>
</main>
<script id="report-data" type="application/json">{serialized}</script>
<script>{_SCRIPT}</script>
</body>
</html>
"""


def _csp_hash(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")
