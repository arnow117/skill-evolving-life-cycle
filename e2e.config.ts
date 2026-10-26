import { web } from '@e2e-dev/web';
import type { E2EConfig } from 'e2e';

// The test fulfills this fixed navigation from the generated file's unchanged
// bytes. No process listens at this URL.
const reportUrl = 'http://127.0.0.1:9/report.html';

export default {
  projectId: 'dev.selc.stats-report',
  tests: 'tests/stats_report.e2e.ts',
  retries: 0,
  workers: 1,
  cache: 'off',
  targets: [
    {
      name: 'local-report',
      platform: 'web',
      engine: web(),
      app: { url: reportUrl },
    },
  ],
} satisfies E2EConfig;
