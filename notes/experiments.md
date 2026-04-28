# What other studies say

> LSync reports a single headline number, 24.84 ms average sync precision, measured by embedding a known timing signal into the content, then measuring detection error at the receiver. This means that LSync can measure error independently of the system under test. 

> PTP uses hardware NIC timestamp as ground truth, which we don't use in our algorithm. 

# Tests for clock_sync.py

- Does `measure_offset` estimate the clock skew accurately?
  > Introduce a known artificial offset, then check that the estimated offset matches it. In `single_cam.py`'s `read()`, temporarily add a constant to `ts_ms`:

  `ts_ms = int(now.timestamp() * 1000) + 200 # simulate sender clock 200 ms ahead`

  Run the full pipe. `clock_sync.measure_offset` should return ~200. Then check that `sycn_error_ms` in the CSV is back to our baseline (~78msp50) rather than ~278ms. That confirms the correction is actually working, not just running silently.

  Run this for several known offsets: 0ms, 50ms, 200ms, 500ms. Plot estimated vs. injected offset: the relation should be linear and close to y = x.

- How stable and repeatable is the estimate?
  > Run `measure_offset` 30 times in a row on the same connection (with a small loop in a test script) and record all returned values. We want to see:
    - Low variance (tight distribution) = the median of 8 strategy is working
    - No systematic drift over time

  Also log the RTT per round (add `rtt = (T4 - T1) - (T3 - T2)` to `clock_sync.py`). High RTT variance is the main enemy of offset accuracy. If individual round RTTs span 50ms, the offset estimate has ~25ms of error regardless of how many rounds we run.

# What measurements to take (and log)
Our CSV currently logs: `frame_index, cam_a_ts_ms, cam_b_ts_ms, playback_time_ms, latency_a_ms, latency_b_ms, sync_error_ms`. We can add these or create a separate clock_sync log:

|New Measurement | How to collect | What it tells us |
|---------|---------|----------|
| `clock_offset_ms` per camera | Already printed; write to a log file at session start | How much skew was present and corrected |
|RTT per round during handshake | Add `rtt = (T4-T1)-(T3-T2)` in `measure_offset` | Quality of each offset sample; high RTT = noisy estimate |
| `sync_error_ms` with correction disabled | Run `offset_ms=0` as a control | Qunatifies what the correction actually did |
| `sync_error_ms` wuth known injected offset | As described above | Validates accuracy of the estimation |
| Offset at session start vs end | Call `measure_offset` again after streaming | Detects clock drift during long sessions |
| | | |

# The Experiment Structure
Three experiments, directly comparable to your existing buffer-depth experiments:

**Experiment A** — Control (same machine, no offset)
Both cameras on one machine. `measure_offset` should return ≈0. `sync_error_ms` should match your existing Phase 1 p50 (~78ms). This is a regression test.

**Experiment B** — Simulated offset (same machine, injected offset)
Add a known constant to one camera's `ts_ms`. Run three sub-experiments with injected offsets of 50ms, 200ms, 500ms. For each: record estimated offset, record `sync_error_ms` with correction on vs. off. This is your validation of the algorithm.

Experiment C — Real two-machine (the actual target scenario)
One machine runs `transport.py` with a real camera, another runs `get_frame.py`. Log the reported `clock_offset_ms`, then compare `sync_error_ms` against your Experiment A baseline. The gap between A and C (after correction) tells you your residual error in a real distributed setup.

# The Comparison Baseline
LSync achieves **24.84 ms** average precision without any clock infrastructure. After adding `clock_sync.py`, your Phase 1 p50 should ideally drop from ~78ms toward something closer to your jitter floor — which with ±30ms jitter is around 30–60ms. If you get below LSync's 24.84ms with a tighter jitter setting, that's a meaningful result. If you can't beat it, the difference is attributable to the asymmetric-network assumption in your offset formula, which is the main caveat to document.