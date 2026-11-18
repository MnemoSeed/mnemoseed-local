# CI `test` job timeout measurement

The `test` job runs only on `ubuntu-latest`. Windows full-suite timings
(~18-22 min on a local machine) do not apply to this job.

Source: GitHub Actions `jobs[].started_at` / `completed_at` for the `test`
job of the 11 most recent successful `ci` workflow runs (2026-11-17 to
2026-11-18), read with the REST API.

| run id | started (UTC) | completed (UTC) | duration (s) |
| --- | --- | --- | --- |
| 44242840720 | 05:14:28 | 05:22:24 | 476 |
| 44237039119 | 04:23:58 | 04:31:16 | 438 |
| 44225814838 | 02:47:35 | 02:55:16 | 461 |
| 44224826117 | 02:39:12 | 02:47:11 | 479 |
| 44224458838 | 02:36:27 | 02:42:05 | 338 |
| 44219213737 | 01:53:33 | 02:00:27 | 414 |
| 44213739816 | 01:10:31 | 01:18:33 | 482 |
| 44192014803 | 22:38:31 | 22:44:40 | 369 |
| 44188034241 | 22:14:20 | 22:21:36 | 436 |
| 44185690648 | 22:00:58 | 22:08:54 | 476 |
| 44097403996 | 13:54:28 | 14:02:00 | 452 |

Statistics (seconds): n=11, mean=438.3 (7m18s), min=338 (5m38s),
max=482 (8m02s), standard deviation=45.3.

- The worst observed run uses 80.3% of the 10-minute limit, leaving only
  118 s (about 2 min) of headroom.
- mean + 3 sd = 574 s (9m34s), within 26 s of the 10-minute wall, so a
  slow-but-ordinary run can approach or breach it.
- The job includes network-dependent setup (`setup-uv`, `uv sync`) and the
  suite is still growing, so upward variance is expected.

Raising the execution limit to 15 minutes leaves 418 s (about 7 min) of
headroom over the worst observed run and 326 s over the mean + 3 sd bound.
This is a job-level execution limit only: no quality gate is relaxed and no
test is skipped.
