import { execFileSync, spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromium } from 'playwright-core';

const repositoryRoot = resolve(import.meta.dirname, '..');
const python = process.env.SELC_PYTHON ?? '/opt/homebrew/bin/python3.11';
const artifactRoot = resolve(process.env.SELC_FILE_SMOKE_DIR ?? '.e2e/file-smoke-final');
const reportPath = resolve(process.argv[2] ?? `${artifactRoot}/stats-report.html`);
const reportUrl = pathToFileURL(reportPath).href;
const requests = [];
const consoleErrors = [];
const maliciousSuite = '</script><!--<script><img src=x onerror="window.injected=true">';

function runPython(arguments_) {
  execFileSync(python, arguments_, { cwd: repositoryRoot, stdio: 'pipe' });
}

function createFixture() {
  runPython(['tests/stats_report_fixture.py', '--output', reportPath]);
}

function regenerateReport() {
  const root = resolve(reportPath, '..');
  runPython([
    'scripts/selc.py',
    'stats-report',
    '--project-root',
    `${root}/alpha`,
    '--project-root',
    `${root}/beta`,
    '--project-root',
    `${root}/unavailable`,
    '--project-root',
    `${root}/basic`,
    '--project-root',
    `${root}/conflict`,
    '--project-root',
    `${root}/malicious`,
    '--project-root',
    `${root}/unknown`,
    '--output',
    reportPath,
  ]);
}

function mutateAlphaSource() {
  const mutation = `
import sys
from pathlib import Path
sys.path.insert(0, 'src')
from selc import records
database = records.open_database(Path(sys.argv[1]))
try:
    database.execute("UPDATE tasks SET current_result = 'pass' WHERE task_id = 'task-main'")
    database.execute("UPDATE calls SET result = 'pass' WHERE call_id = 'call-main'")
    database.commit()
finally:
    database.close()
`;
  runPython(['-c', mutation, resolve(reportPath, '..', 'alpha')]);
}

function expectMetricCards(values, expected) {
  if (values.length !== 4 || values.some((value) => value.join(',') !== expected.join(','))) {
    throw new Error(`unexpected metric cards: ${JSON.stringify(values)}`);
  }
}

function expectExactMetricCards(values, expected) {
  if (values.length !== expected.length || values.some((value, index) => value.join(',') !== expected[index].join(','))) {
    throw new Error(`unexpected metric cards: ${JSON.stringify(values)}`);
  }
}

function contrastRatio(foreground, background) {
  const relativeLuminance = (hex) => {
    const channels = [1, 3, 5].map((offset) => Number.parseInt(hex.slice(offset, offset + 2), 16) / 255);
    const linear = channels.map((channel) =>
      channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4,
    );
    return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
  };
  const first = relativeLuminance(foreground);
  const second = relativeLuminance(background);
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05);
}

function checkAaContrast(colors) {
  const pairs = {
    inkOnPaper: [colors.ink, colors.paper],
    mutedOnSurface: [colors.muted, colors.surface],
    metaOnAccentSoft: ['#164e63', colors.accentSoft],
    dangerOnDangerSoft: [colors.danger, colors.dangerSoft],
    tableOnHeader: ['#334155', '#edf2f7'],
  };
  const results = Object.fromEntries(
    Object.entries(pairs).map(([name, colors]) => [
      name,
      Number(contrastRatio(colors[0], colors[1]).toFixed(2)),
    ]),
  );
  if (Object.values(results).some((ratio) => ratio < 4.5)) {
    throw new Error(`AA contrast failure: ${JSON.stringify(results)}`);
  }
  return results;
}

mkdirSync(artifactRoot, { recursive: true });
createFixture();
const initialBytes = readFileSync(reportPath);
const initialHash = createHash('sha256').update(initialBytes).digest('hex');
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text());
});
page.on('request', (request) => requests.push(request.url()));

try {
  await page.goto(reportUrl, { waitUntil: 'load' });
  await page.getByRole('heading', { name: '本地统计快照' }).waitFor();
  await page.screenshot({ path: resolve(artifactRoot, 'desktop-1440.png'), fullPage: true });

  mutateAlphaSource();
  if (!readFileSync(reportPath).equals(initialBytes)) {
    throw new Error('the source mutation changed an existing static HTML snapshot');
  }
  await page.reload({ waitUntil: 'load' });
  expectExactMetricCards(
    await page.locator('#metrics .metric-card').evaluateAll((cards) =>
      cards.map((card) => Array.from(card.querySelectorAll('dd')).map((value) => Number(value.textContent))),
    ),
    [
      [3, 2, 3, 0],
      [3, 1, 3, 0],
      [1, 1, 1, 0],
      [1, 0, 1, 0],
    ],
  );

  regenerateReport();
  const regeneratedBytes = readFileSync(reportPath);
  if (regeneratedBytes.equals(initialBytes)) {
    throw new Error('regeneration did not replace the static report after a source mutation');
  }
  await page.reload({ waitUntil: 'load' });
  const finalTaskCard = await page.locator('#metrics .metric-card').nth(1).evaluate((card) =>
    Array.from(card.querySelectorAll('dd')).map((value) => Number(value.textContent)),
  );
  if (finalTaskCard.join(',') !== '3,2,3,0') {
    throw new Error(`regenerated task-final metric was stale: ${JSON.stringify(finalTaskCard)}`);
  }

  const unavailable = resolve(reportPath, '..', 'unavailable');
  const failedRegeneration = spawnSync(
    python,
    ['scripts/selc.py', 'stats-report', '--project-root', unavailable, '--output', reportPath],
    { cwd: repositoryRoot, encoding: 'utf-8' },
  );
  if (failedRegeneration.status === 0) {
    throw new Error('all-unavailable regeneration unexpectedly succeeded');
  }
  if (!readFileSync(reportPath).equals(regeneratedBytes)) {
    throw new Error('failed regeneration changed the report that was already generated');
  }

  await page.selectOption('#project-select', 'project-5');
  await page.selectOption('#suite-filter', 'conflict-suite');
  expectMetricCards(
    await page.locator('#metrics .metric-card').evaluateAll((cards) =>
      cards.map((card) => Array.from(card.querySelectorAll('dd')).map((value) => Number(value.textContent))),
    ),
    [2, 2, 2, 0],
  );
  const productionRates = await page.locator('#metrics .metric-card p').allTextContents();
  if (productionRates.some((rate) => rate !== 'S/N 100.0% · S/K 100.0% · K/N 100.0%')) {
    throw new Error(`unexpected production-only rates: ${JSON.stringify(productionRates)}`);
  }
  await page.check('#include-test');
  expectMetricCards(
    await page.locator('#metrics .metric-card').evaluateAll((cards) =>
      cards.map((card) => Array.from(card.querySelectorAll('dd')).map((value) => Number(value.textContent))),
    ),
    [2, 0, 0, 2],
  );
  const testInclusiveRates = await page.locator('#metrics .metric-card p').allTextContents();
  if (
    testInclusiveRates.some(
      (rate) => rate !== 'S/N 0.0% · S/K 无已知结果样本 · K/N 0.0%',
    )
  ) {
    throw new Error(`unexpected test-inclusive rates: ${JSON.stringify(testInclusiveRates)}`);
  }

  await page.selectOption('#project-select', 'project-6');
  await page.locator('#group-table').getByText(maliciousSuite, { exact: false }).first().waitFor();
  const hostileState = await page.evaluate(() => ({
    injected: window.injected,
    scripts: document.querySelectorAll('script').length,
    resources: performance.getEntriesByType('resource').length,
  }));
  if (hostileState.injected !== undefined || hostileState.scripts !== 2 || hostileState.resources !== 0) {
    throw new Error(`unsafe hostile text rendering: ${JSON.stringify(hostileState)}`);
  }

  await page.selectOption('#project-select', 'project-7');
  await page.getByText('S/K 无已知结果样本', { exact: false }).first().waitFor();
  await page.getByText('无样本，比例不适用', { exact: true }).first().waitFor();

  await page.setViewportSize({ width: 375, height: 800 });
  const layout = await page.evaluate(() => ({
    bodyWidth: document.body.scrollWidth,
    viewportWidth: window.innerWidth,
  }));
  if (layout.bodyWidth > layout.viewportWidth) {
    throw new Error(`page overflowed: ${layout.bodyWidth}px > ${layout.viewportWidth}px`);
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: resolve(artifactRoot, 'mobile-375.png'), fullPage: true });

  if (consoleErrors.length > 0) {
    throw new Error(`browser console errors: ${consoleErrors.join('; ')}`);
  }
  if (requests.some((url) => url !== reportUrl)) {
    throw new Error(`unexpected non-document request: ${requests.join(', ')}`);
  }
  const colors = await page.evaluate(() => {
    const style = getComputedStyle(document.documentElement);
    return {
      ink: style.getPropertyValue('--ink').trim(),
      muted: style.getPropertyValue('--muted').trim(),
      paper: style.getPropertyValue('--paper').trim(),
      surface: style.getPropertyValue('--surface').trim(),
      accentSoft: style.getPropertyValue('--accent-soft').trim(),
      danger: style.getPropertyValue('--danger').trim(),
      dangerSoft: style.getPropertyValue('--danger-soft').trim(),
    };
  });
  console.log(
    JSON.stringify({
      reportUrl,
      initialHash,
      regeneratedHash: createHash('sha256').update(regeneratedBytes).digest('hex'),
      requests,
      layout,
      contrast: checkAaContrast(colors),
      consoleErrors,
    }),
  );
} finally {
  await browser.close();
}
