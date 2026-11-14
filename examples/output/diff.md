# Log clusters

- Files: `examples/logs/after.log`
- Records: 144 from 144 lines (formats: text 144)
- Time range: 2026-11-11T10:30:21.533Z to 2026-11-11T10:39:55.690Z
- Rate histogram: 24 buckets of about 24s each
- Clusters: 7 (showing 7)
- Baseline: `examples/logs/before.log` (108 records, 6 clusters)
- Versus baseline: 2 new, 1 spiking (rate x3 or more), 3 steady, 1 down, 1 vanished

| # | Status | Level | Count | Before | First seen | Last seen | Rate | Signature |
|--:|---|---|--:|--:|---|---|---|---|
| 1 | NEW | error | 14 | 0 | 2026-11-11T10:30:22.550Z | 2026-11-11T10:39:55.690Z | `█▁▁▅▁▁▁▁▅█▁▁█▁▁▁▁▅▅▁▅▁▅█` | `shop.api.tls: ssl handshake failed with upstream <ip>: certificate verify failed` |
| 2 | NEW | warning | 6 | 0 | 2026-11-11T10:30:25.535Z | 2026-11-11T10:39:48.965Z | `█▁▁▁▁▁▁▁▁▁▁██▁▁▁█▁▁▁▁█▁█` | `shop.api.search: index shard <n> unavailable, falling back to scan` |
| 3 | SPIKING (x14) | warning | 41 | 3 | 2026-11-11T10:30:21.533Z | 2026-11-11T10:39:23.498Z | `▄▅▄▄▅▁▃▃▄▄▄█▃▃▃▅▃▃▄▄▃▃▃▁` | `shop.api.cache: cache miss for key product:<n>` |
| 4 | STEADY | error | 5 | 5 | 2026-11-11T10:30:25.786Z | 2026-11-11T10:39:32.695Z | `▅▁▁▁▁▁▅▁▁▁▁▁█▁▁▁▁▁▁▁▁▁▁▅` | `shop.api.payments: payment gateway timeout after <n>ms for order ord_<n>` |
| 5 | STEADY | info | 70 | 70 | 2026-11-11T10:30:25.070Z | 2026-11-11T10:39:50.691Z | `▇█▄▃▄▆▆▁▇▇▁▄▄▇▇▃▇▃▇▆██▆▇` | `shop.api: request handled: GET <path> status=<n> duration=<n>ms` |
| 6 | STEADY | info | 6 | 6 | 2026-11-11T10:32:35.393Z | 2026-11-11T10:39:08.144Z | `▁▁▁▁▁▅▁▁▅▁▁▁▁▁▅█▁▁▁▁▁▁▅▁` | `shop.api: request handled: POST <path> status=<n> duration=<n>ms` |
| 7 | DOWN (x0.1) | info | 2 | 20 | 2026-11-11T10:35:06.225Z | 2026-11-11T10:35:21.463Z | `▁▁▁▁▁▁▁▁▁▁▁██▁▁▁▁▁▁▁▁▁▁▁` | `shop.api: retrying request to payments (attempt <n>)` |

## Samples

### 1. error x14

`examples/logs/after.log:2`

```text
2026-11-11 10:30:22,550 ERROR   shop.api.tls: ssl handshake failed with upstream 203.0.113.30:443: certificate verify failed
```

### 2. warning x6

`examples/logs/after.log:4`

```text
2026-11-11 10:30:25,535 WARNING shop.api.search: index shard 6495 unavailable, falling back to scan
```

### 3. warning x41

`examples/logs/after.log:1`

```text
2026-11-11 10:30:21,533 WARNING shop.api.cache: cache miss for key product:3540
```

### 4. error x5

`examples/logs/after.log:5`

```text
2026-11-11 10:30:25,786 ERROR   shop.api.payments: payment gateway timeout after 3000ms for order ord_6053
```

### 5. info x70

`examples/logs/after.log:3`

```text
2026-11-11 10:30:25,070 INFO    shop.api: request handled: GET /api/orders status=200 duration=362ms
```

### 6. info x6

`examples/logs/after.log:35`

```text
2026-11-11 10:32:35,393 INFO    shop.api: request handled: POST /api/checkout status=201 duration=152ms
```

### 7. info x2

`examples/logs/after.log:68`

```text
2026-11-11 10:35:06,225 INFO    shop.api: retrying request to payments (attempt 2)
```

## Vanished (in the baseline, not in this log)

| Level | Count before | Last seen | Signature |
|---|--:|---|---|
| warning | 4 | 2026-11-11T10:08:06.232Z | `shop.api.cache: using deprecated redis client v3, set REDIS_V4=<n>` |
