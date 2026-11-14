# Incident timeline

- Window: 2026-11-12T13:53:00Z to 2026-11-12T14:13:00Z (around 2026-11-12T14:03:00Z, 10m before and 10m after)
- Times are UTC. Showing warning and above; error level for first occurrences and spikes: error.
- Records in window shown: 44 of 566 (repeats within the fold gap are merged)

## Sources

| Source | File | Format | Records | In window | First | Last |
|---|---|---|--:|--:|---|---|
| web | `examples/logs/web.jsonl` | json 454 | 454 | 397 | 2026-11-12T13:50:00Z | 2026-11-12T14:14:57.082Z |
| worker | `examples/logs/worker.log` | text 181 | 181 | 167 | 2026-11-12T13:50:00Z | 2026-11-12T14:14:36.691Z |
| db | `examples/logs/db.log` | text 179 | 179 | 173 | 2026-11-12T13:50:00.201Z | 2026-11-12T14:14:18.580Z |
| nginx-access | `examples/logs/nginx-access.log` | access 625 | 625 | 523 | 2026-11-12T13:50:00Z | 2026-11-12T14:14:59Z |
| payments | `examples/logs/payments.log` | text 44 | 44 | 36 | 2026-11-12T13:50:20Z | 2026-11-12T14:14:39.739Z |

## First bad lines

The earliest error-level signature in the window is `db:18` at 2026-11-12T14:02:57.390Z (not seen before the window).

| Time | Source | Line | Count in window | New | Signature |
|---|---|--:|--:|---|---|
| 2026-11-12T13:55:10.218Z | worker | 25 | 3 | no | `shop.worker.emails: failed to send confirmation for order ord_<n> to <email>` |
| 2026-11-12T13:58:31.902Z | payments | 31 | 3 | no | `com.fernhill.payments.ChargeService - charge failed for order ord_<n> [java.net.SocketTimeoutException]` |
| 2026-11-12T14:02:57.390Z | db | 18 | 1 | yes | `remaining connection slots are reserved for non-replication superuser connections` |
| 2026-11-12T14:02:57.430Z | worker | 92 | 40 | yes | `shop.worker.inventory: inventory_sync failed for batch <n> [psycopg2.OperationalError]` |
| 2026-11-12T14:03:00.140Z | db | 19 | 140 | yes | `sorry, too many clients already` |
| 2026-11-12T14:03:01.000Z | nginx-access | 261 | 135 | yes | `POST /api/checkout 500` |
| 2026-11-12T14:03:01.800Z | web | 151 | 131 | yes | `checkout failed: psycopg2.OperationalError: connection to server at "<str>" (<ip>), port <n> failed: FATAL: sorry, too many clients already` |
| 2026-11-12T14:03:20.000Z | nginx-access | 273 | 7 | yes | `GET /api/orders/<id> 503` |

## Error-rate spikes

Buckets of 30s; median/MAD baseline over 10 buckets: typical 0 errors per bucket (scale 0.25); a spike needs score >= 3.5 and at least 3 errors.

| Start | End | Peak errors per bucket | Total errors | Peak score |
|---|---|--:|--:|--:|
| 2026-11-12T14:03:00Z | 2026-11-12T14:10:00Z | 37 | 454 | 147.6 |

## Timeline

```text
2026-11-12T13:53:22.480Z  web           WARN   payment gateway slow response  [web.jsonl:40]
2026-11-12T13:53:48.000Z  nginx-access  WARN   GET /api/products/1998 404 from 203.0.113.58  [nginx-access.log:77]
2026-11-12T13:55:05.905Z  web           WARN   payment gateway slow response  [web.jsonl:59]
2026-11-12T13:55:10.218Z  worker        ERROR  shop.worker.emails: failed to send confirmation for order ord_70231 to chloe.rossi@example.com (+10 lines)  [worker.log:25]  <- first in window, seen before it
2026-11-12T13:55:52.000Z  nginx-access  WARN   GET /static/app.3f9a1c.js 404 from 203.0.113.77  [nginx-access.log:119]
2026-11-12T13:56:14.077Z  payments      WARN   com.fernhill.payments.GatewayClient - slow response from gateway: 2311ms  [payments.log:27]
2026-11-12T13:56:47.012Z  web           WARN   payment gateway slow response  [web.jsonl:77]
2026-11-12T13:57:37.000Z  nginx-access  WARN   GET /static/app.3f9a1c.js 404 from 203.0.113.17  [nginx-access.log:155]
2026-11-12T13:58:30.640Z  web           WARN   payment gateway slow response  [web.jsonl:99]
2026-11-12T13:58:31.902Z  payments      ERROR  com.fernhill.payments.ChargeService - charge failed for order ord_70277 (+16 lines: java.net.SocketTimeoutException)  [payments.log:31]  <- first in window, seen before it
2026-11-12T13:59:10.330Z  web           WARN   session refresh rejected  [web.jsonl:108]
2026-11-12T14:00:09.201Z  web           WARN   payment gateway slow response  [web.jsonl:118]
2026-11-12T14:00:15.500Z  payments      WARN   com.fernhill.payments.ChargeService - charge declined for order ord_70290: card 4242424242424242 expired  [payments.log:51]
2026-11-12T14:00:41.900Z  web           WARN   token verify failed: expired  [web.jsonl:124]
2026-11-12T14:01:30.640Z  payments      WARN   com.fernhill.payments.GatewayClient - slow response from gateway: 2500ms  [payments.log:53]
2026-11-12T14:01:52.330Z  web           WARN   payment gateway slow response  [web.jsonl:138]
2026-11-12T14:02:57.390Z  db            CRIT   remaining connection slots are reserved for non-replication superuser connections  [db.log:18]  <- first of its kind, new in window
2026-11-12T14:02:57.430Z  worker        ERROR  shop.worker.inventory: inventory_sync failed for batch 37 (+15 lines: psycopg2.OperationalError) (x40 until 14:09:27.256Z)  [worker.log:92]  <- first of its kind, new in window
2026-11-12T14:02:57.432Z  worker        WARN   shop.worker.inventory: retrying inventory_sync batch 37 in 10s (attempt 1) (x40 until 14:09:27.258Z)  [worker.log:108]
2026-11-12T14:03:00.000Z  -- error spike starts: peak 37 per 30s bucket --
2026-11-12T14:03:00.140Z  db            CRIT   sorry, too many clients already (x140 until 14:09:28.243Z)  [db.log:19]  <- first of its kind, new in window
2026-11-12T14:03:01.000Z  nginx-access  ERROR  POST /api/checkout 500 from 203.0.113.9 (x135 until 14:09:32.000Z)  [nginx-access.log:261]  <- first of its kind, new in window
2026-11-12T14:03:01.800Z  web           ERROR  checkout failed: psycopg2.OperationalError: connection to server at "db.example.com" (203.0.113.20), port 5432 failed: FATAL:  sorry, too many clients already (x131 until 14:09:34.999Z)  [web.jsonl:151]  <- first of its kind, new in window
2026-11-12T14:03:10.000Z  web           WARN   readiness probe failed (x39 until 14:09:30.869Z)  [web.jsonl:156]
2026-11-12T14:03:20.000Z  nginx-access  ERROR  GET /api/orders/88966 503 from 203.0.113.23  [nginx-access.log:273]  <- first of its kind, new in window
2026-11-12T14:03:36.077Z  web           WARN   payment gateway slow response  [web.jsonl:172]
2026-11-12T14:04:22.000Z  nginx-access  ERROR  GET /api/orders/88262 503 from 203.0.113.5 (x4 until 14:06:38.000Z)  [nginx-access.log:313]
2026-11-12T14:04:40.507Z  worker        ERROR  shop.worker.emails: failed to send confirmation for order ord_70301 to finn.walker@example.com (+10 lines)  [worker.log:283]
2026-11-12T14:05:03.580Z  web           WARN   payment gateway slow response  [web.jsonl:224]
2026-11-12T14:05:44.120Z  payments      ERROR  com.fernhill.payments.ChargeService - charge failed for order ord_70308 (+16 lines: java.net.SocketTimeoutException)  [payments.log:60]
2026-11-12T14:06:12.008Z  payments      WARN   com.fernhill.payments.GatewayClient - slow response from gateway: 2616ms  [payments.log:78]
2026-11-12T14:07:31.014Z  web           WARN   payment gateway slow response  [web.jsonl:311]
2026-11-12T14:07:44.000Z  nginx-access  ERROR  GET /api/orders/88985 503 from 203.0.113.58  [nginx-access.log:444]
2026-11-12T14:08:21.000Z  nginx-access  WARN   GET /api/products/2616 404 from 203.0.113.166  [nginx-access.log:470]
2026-11-12T14:08:52.000Z  nginx-access  ERROR  GET /api/orders/88508 503 from 203.0.113.42  [nginx-access.log:491]
2026-11-12T14:09:02.760Z  web           WARN   payment gateway slow response  [web.jsonl:368]
2026-11-12T14:09:28.300Z  worker        WARN   shop.worker: SIGTERM received, shutting down  [worker.log:795]
2026-11-12T14:09:29.120Z  worker        WARN   shop.worker.inventory: 108 db connections still open at shutdown  [worker.log:796]
2026-11-12T14:10:00.000Z  -- error spike ends --
2026-11-12T14:10:40.411Z  web           WARN   payment gateway slow response  [web.jsonl:403]
2026-11-12T14:10:44.920Z  payments      WARN   com.fernhill.payments.GatewayClient - slow response from gateway: 2859ms  [payments.log:85]
2026-11-12T14:10:46.000Z  nginx-access  WARN   GET /static/app.3f9a1c.js 404 from 203.0.113.5  [nginx-access.log:541]
2026-11-12T14:11:20.733Z  payments      ERROR  com.fernhill.payments.ChargeService - charge failed for order ord_70355 (+16 lines: java.net.SocketTimeoutException)  [payments.log:87]
2026-11-12T14:11:55.061Z  worker        ERROR  shop.worker.emails: failed to send confirmation for order ord_70350 to hugo.silva@example.com (+10 lines)  [worker.log:804]
2026-11-12T14:12:11.905Z  web           WARN   payment gateway slow response  [web.jsonl:423]
2026-11-12T14:12:59.000Z  nginx-access  WARN   GET /api/products/1560 404 from 203.0.113.17  [nginx-access.log:584]
```
