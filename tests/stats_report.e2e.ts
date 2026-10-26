import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';
import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

const repositoryRoot = dirname(fileURLToPath(import.meta.url));
const python = process.env.SELC_PYTHON ?? '/opt/homebrew/bin/python3.11';
const reportPath = resolve(repositoryRoot, '../.e2e/report/stats-report.html');
const reportPathFromProjectRoot = '.e2e/report/stats-report.html';
const reportUrl = 'http://127.0.0.1:9/report.html';
const maliciousSuite = '</script><!--<script><img src=x onerror="window.injected=true">';
let expectedReportHash = '';

function createReport(): void {
  execFileSync(python, ['tests/stats_report_fixture.py', '--output', reportPath], {
    cwd: resolve(repositoryRoot, '..'),
    stdio: 'pipe',
  });
}

async function cardMetrics(browser: { evaluate: (fn: () => unknown) => Promise<unknown> }): Promise<number[][]> {
  return browser.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>('#metrics .metric-card')).map((card) =>
      Array.from(card.querySelectorAll('dd')).map((value) => Number(value.textContent)),
    ),
  ) as Promise<number[][]>;
}

async function cardRates(browser: { evaluate: (fn: () => unknown) => Promise<unknown> }): Promise<string[]> {
  return browser.evaluate(() =>
    Array.from(document.querySelectorAll<HTMLElement>('#metrics .metric-card p')).map(
      (card) => card.textContent ?? '',
    ),
  ) as Promise<string[]>;
}

function assertAllCards(metrics: number[][], expected: number[]): void {
  if (metrics.length !== 4 || metrics.some((card) => card.join(',') !== expected.join(','))) {
    throw new Error(`unexpected metric cards: ${JSON.stringify(metrics)}`);
  }
}

test.beforeEach(async ({ browser }) => {
  createReport();
  expectedReportHash = createHash('sha256').update(readFileSync(reportPath)).digest('hex');

  await browser.route('**/*', async (route) => {
    await route.abort();
  });
  await browser.route(reportUrl, async (route) => {
    await route.fulfill({
      path: reportPathFromProjectRoot,
      contentType: 'text/html; charset=utf-8',
      headers: { 'x-selc-report-sha256': expectedReportHash },
    });
  });
});

test('opens the generated file report at desktop width without external resources', async ({ app, browser, screen }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open();
  const response = browser.waitForResponse(reportUrl);
  await browser.goto(reportUrl);
  const fulfilledResponse = await response;
  if (fulfilledResponse.headers['x-selc-report-sha256'] !== expectedReportHash) {
    throw new Error('the fulfilled report response did not retain its source hash');
  }
  await expect(browser).toHaveURL(reportUrl);
  await expect(screen.getByRole('heading', '本地统计快照')).toBeVisible();
  await expect(
    screen.getByText('suite-alpha · 1.0 · codex · manual', { exact: true }),
  ).toBeVisible();
  await expect(screen.getByText('SOURCE_NOT_FOUND', { exact: false })).toBeVisible();
  const alphaFirstMetric = await browser.evaluate(() =>
    Array.from(document.querySelector('#metrics .metric-card')?.querySelectorAll('dd') ?? []).map(
      (value) => Number(value.textContent),
    ),
  );
  if (alphaFirstMetric.join(',') !== '3,2,3,0') {
    throw new Error(`unexpected alpha first metric: ${JSON.stringify(alphaFirstMetric)}`);
  }
  await app.screenshot('desktop-overview');

  const resourceCount = await browser.evaluate(() => performance.getEntriesByType('resource').length);
  if (resourceCount !== 0) throw new Error(`expected no external resource entries, got ${resourceCount}`);
});

test('filters locally, supports keyboard focus, and resets at tablet width', async ({ app, browser, screen }) => {
  await browser.setViewport({ width: 768, height: 900 });
  await app.open();
  const project = screen.getByLabel('项目');
  await project.selectOption({ value: 'project-2' });
  await expect(
    screen.getByText('suite-beta · 2.0 · workbuddy · manual', { exact: true }),
  ).toBeVisible();
  await screen.getByLabel('包含测试样本').check();
  await expect(screen.getByText('测试', { exact: true })).toBeVisible();
  await screen.getByLabel('版本').selectOption({ value: '2.0' });
  await screen.getByLabel('运行环境').selectOption({ value: 'codex' });
  await expect(screen.getByText('当前筛选范围没有样本。', { exact: true })).toBeVisible();
  await screen.getByRole('button', '重置筛选').focus();
  await browser.keyboard.press('Enter');
  await expect(
    screen.getByText('suite-beta · 2.0 · workbuddy · manual', { exact: true }),
  ).toBeVisible();
  await app.screenshot('tablet-filtered');
});

test('keeps controls operable and page-width safe at mobile width', async ({ app, browser, screen }) => {
  await browser.setViewport({ width: 375, height: 800 });
  await app.open();
  await screen.getByLabel('套件').selectOption({ value: 'suite-alpha' });
  await expect(
    screen.getByText('suite-alpha · 1.0 · codex · manual', { exact: true }),
  ).toBeVisible();
  const layout = await browser.evaluate(() => ({ body: document.body.scrollWidth, viewport: window.innerWidth }));
  if (layout.body > layout.viewport) throw new Error(`page overflowed: ${layout.body}px > ${layout.viewport}px`);
  await app.screenshot('mobile');

  await screen.getByLabel('项目').selectOption({ value: 'project-4' });
  await expect(screen.getByText('基础模式默认不采集统计记录。', { exact: true })).toBeVisible();
  const basicMetricText = await browser.evaluate(() => document.querySelector('#metrics')?.textContent);
  if (basicMetricText?.trim()) throw new Error('basic mode displayed fabricated zero-value metrics');
});

test('switches between pre-deduplicated test projections without losing production samples', async ({ app, browser, screen }) => {
  await browser.setViewport({ width: 768, height: 900 });
  await app.open();
  await screen.getByLabel('项目').selectOption({ value: 'project-5' });
  await screen.getByLabel('套件').selectOption({ value: 'conflict-suite' });
  assertAllCards(await cardMetrics(browser), [2, 2, 2, 0]);
  if ((await cardRates(browser)).some((rate) => rate !== 'S/N 100.0% · S/K 100.0% · K/N 100.0%')) {
    throw new Error(`unexpected production-only rates: ${JSON.stringify(await cardRates(browser))}`);
  }

  await screen.getByLabel('包含测试样本').check();
  assertAllCards(await cardMetrics(browser), [2, 0, 0, 2]);
  if (
    (await cardRates(browser)).some(
      (rate) => rate !== 'S/N 0.0% · S/K 无已知结果样本 · K/N 0.0%',
    )
  ) {
    throw new Error(`unexpected test-inclusive rates: ${JSON.stringify(await cardRates(browser))}`);
  }

  await screen.getByLabel('项目').selectOption({ value: 'project-7' });
  await expect(screen.getByText('S/K 无已知结果样本', { exact: false }).first()).toBeVisible();
  await expect(screen.getByText('无样本，比例不适用', { exact: true }).first()).toBeVisible();
});

test('keeps a test-only filter selected when returning to production-only data', async ({ app, browser, screen }) => {
  await browser.setViewport({ width: 768, height: 900 });
  await app.open();
  await screen.getByLabel('项目').selectOption({ value: 'project-1' });
  await screen.getByLabel('包含测试样本').check();
  await screen.getByLabel('套件').selectOption({ value: 'suite-test' });
  await screen.getByLabel('版本').selectOption({ value: '1.0' });
  await screen.getByLabel('运行环境').selectOption({ value: 'workbuddy' });

  await screen.getByLabel('包含测试样本').uncheck();
  const filters = await browser.evaluate(() => ({
    suite: (document.querySelector('#suite-filter') as HTMLSelectElement).value,
    version: (document.querySelector('#version-filter') as HTMLSelectElement).value,
    harness: (document.querySelector('#harness-filter') as HTMLSelectElement).value,
  }));
  if (JSON.stringify(filters) !== JSON.stringify({ suite: 'suite-test', version: '1.0', harness: 'workbuddy' })) {
    throw new Error(`test-only filters were unexpectedly reset: ${JSON.stringify(filters)}`);
  }
  await expect(screen.getByText('当前筛选范围没有样本。', { exact: true })).toBeVisible();

  await screen.getByRole('button', '重置筛选').click();
  await expect(
    screen.getByText('suite-alpha · 1.0 · codex · manual', { exact: true }),
  ).toBeVisible();
});

test('renders hostile grouping text as text under the report CSP', async ({ app, browser, screen }) => {
  await browser.setViewport({ width: 1440, height: 900 });
  await app.open();
  await screen.getByLabel('项目').selectOption({ value: 'project-6' });
  const hostileTextVisible = await browser.evaluate(() =>
    Array.from(document.querySelectorAll('#group-table td')).some((cell) =>
      cell.textContent?.includes('</script><!--<script><img src=x onerror="window.injected=true">') &&
      getComputedStyle(cell).visibility !== 'hidden',
    ),
  );
  if (!hostileTextVisible) throw new Error(`hostile grouping text was not visible: ${maliciousSuite}`);
  const browserSafety = await browser.evaluate(() => ({
    injected: Object.hasOwn(window, 'injected'),
    resourceCount: performance.getEntriesByType('resource').length,
    scriptCount: document.querySelectorAll('script').length,
  }));
  if (browserSafety.injected || browserSafety.resourceCount !== 0 || browserSafety.scriptCount !== 2) {
    throw new Error(`unsafe hostile-data rendering: ${JSON.stringify(browserSafety)}`);
  }
});
