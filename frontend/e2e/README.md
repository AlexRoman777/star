# Local Brave visual checks

The suite renders the complete authenticated interface with synthetic API responses in Brave at
Xiaomi 15 Pro, 16-inch MacBook Pro, and 3440 by 1440 ultrawide viewports.

Synthetic visual captures are written only to the ignored `frontend/test-results/` directory and
are never accepted as repository baselines. The additional private-corpus check reads 25 retained benchmark JPEGs only from
`private-benchmarks/photo-rating-v1/corpus`, serves them through intercepted loopback requests,
and verifies image decoding and gallery layout. Its explicitly local screenshots are written to
the ignored `private/visual-evidence/` tree; it produces no traces, videos, filenames in logs, or
committed image-bearing reports.

Run it after a frontend build:

```sh
pnpm exec playwright test --config playwright.brave.config.ts
```
