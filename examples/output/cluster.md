# Log clusters

- Files: `examples/logs/web.jsonl`, `examples/logs/worker.log`, `examples/logs/db.log`, `examples/logs/payments.log`
- Records: 858 from 1562 lines (formats: json 454, text 404)
- Time range: 2026-11-12T13:51:05.311Z to 2026-11-12T14:12:11.905Z
- Rate histogram: 24 buckets of about 53s each
- Level filter: warning and above (437 records below it skipped)
- Clusters: 15 (showing 8)

| # | Level | Count | First seen | Last seen | Rate | Signature |
|--:|---|--:|---|---|---|---|
| 1 | critical | 140 | 2026-11-12T14:03:00.140Z | 2026-11-12T14:09:28.243Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁▅██████▇▁▁▁` | `sorry, too many clients already` |
| 2 | critical | 1 | 2026-11-12T14:02:57.390Z | 2026-11-12T14:02:57.390Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁█▁▁▁▁▁▁▁▁▁▁` | `remaining connection slots are reserved for non-replication superuser connections` |
| 3 | error | 131 | 2026-11-12T14:03:01.800Z | 2026-11-12T14:09:34.999Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁▅███████▂▁▁` | `checkout failed: psycopg2.OperationalError: connection to server at "<str>" (<ip>), port <n> failed: FATAL: sorry, too many clients already` |
| 4 | error | 40 | 2026-11-12T14:02:57.430Z | 2026-11-12T14:09:27.256Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁▅▇█▇▇█▇▇▁▁▁` | `shop.worker.inventory: inventory_sync failed for batch <n> [psycopg2.OperationalError]` |
| 5 | error | 4 | 2026-11-12T13:51:05.311Z | 2026-11-12T14:11:20.733Z | `█▁▁▁▁▁▁▁█▁▁▁▁▁▁▁█▁▁▁▁▁▁█` | `com.fernhill.payments.ChargeService - charge failed for order ord_<n> [java.net.SocketTimeoutException]` |
| 6 | error | 4 | 2026-11-12T13:51:20.330Z | 2026-11-12T14:11:55.061Z | `█▁▁▁█▁▁▁▁▁▁▁▁▁▁█▁▁▁▁▁▁▁█` | `shop.worker.emails: failed to send confirmation for order ord_<n> to <email>` |
| 7 | warning | 40 | 2026-11-12T14:02:57.432Z | 2026-11-12T14:09:27.258Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁▅▇█▇▇█▇▇▁▁▁` | `shop.worker.inventory: retrying inventory_sync batch <n> in <n>s (attempt <n>)` |
| 8 | warning | 39 | 2026-11-12T14:03:10Z | 2026-11-12T14:09:30.869Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁▄▇█▇▇▇█▇▁▁▁` | `readiness probe failed` |

## Samples

### 1. critical x140

`examples/logs/db.log:19`

```text
2026-11-12 14:03:00.140 UTC [3564] FATAL:  sorry, too many clients already
```

### 2. critical x1

`examples/logs/db.log:18`

```text
2026-11-12 14:02:57.390 UTC [3119] FATAL:  remaining connection slots are reserved for non-replication superuser connections
```

### 3. error x131

`examples/logs/web.jsonl:151`

```text
{"ts":"2026-11-12T14:03:01.800Z","level":"error","msg":"checkout failed","service":"web","host":"web-1","order_id":"ord_88264","request_id":"req_e1eae507","error":"psycopg2.OperationalError: connection to server at \"db.example.com\" (203.0.113.20), port 5432 failed: FATAL:  sorry, too many clients already","status":500}
```

### 4. error x40

`examples/logs/worker.log:92`

```text
2026-11-12 14:02:57,430 ERROR   shop.worker.inventory: inventory_sync failed for batch 37
Traceback (most recent call last):
  File "/app/shop/db.py", line 41, in connect
    return psycopg2.connect(dsn, connect_timeout=3)
  File "/usr/local/lib/python3.12/site-packages/psycopg2/__init__.py", line 122, in connect
    conn = _connect(dsn, connection_factory=connection_factory, **kwasync)
psycopg2.OperationalError: connection to server at "db.example.com" (203.0.113.20), port 5432 failed: FATAL:  sorry, too many clients already

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/shop/worker/inventory.py", line 88, in run_batch
... (4 more lines)
```

### 5. error x4

`examples/logs/payments.log:3`

```text
2026-11-12 13:51:05,311 [http-nio-8081-exec-6] ERROR com.fernhill.payments.ChargeService - charge failed for order ord_70211
com.fernhill.payments.GatewayException: Gateway call failed after 3 attempts
	at com.fernhill.payments.GatewayClient.charge(GatewayClient.java:112)
	at com.fernhill.payments.ChargeService.charge(ChargeService.java:58)
	at com.fernhill.payments.api.ChargeController.post(ChargeController.java:34)
	at java.base/jdk.internal.reflect.DirectMethodHandleAccessor.invoke(DirectMethodHandleAccessor.java:103)
	at java.base/java.lang.reflect.Method.invoke(Method.java:580)
	at org.springframework.web.servlet.FrameworkServlet.service(FrameworkServlet.java:884)
	at org.apache.catalina.core.StandardWrapperValve.invoke(StandardWrapperValve.java:167)
	at java.base/java.lang.Thread.run(Thread.java:1583)
Caused by: java.net.SocketTimeoutException: Read timed out
	at java.base/sun.nio.ch.NioSocketImpl.timedRead(NioSocketImpl.java:278)
... (5 more lines)
```

### 6. error x4

`examples/logs/worker.log:5`

```text
2026-11-12 13:51:20,330 ERROR   shop.worker.emails: failed to send confirmation for order ord_70215 to ben.okafor@example.com
Traceback (most recent call last):
  File "/app/shop/worker/emails.py", line 57, in send_confirmation
    server.sendmail(sender, [recipient], message.as_string())
  File "/usr/local/lib/python3.12/smtplib.py", line 887, in sendmail
    (code, resp) = self.data(msg)
  File "/usr/local/lib/python3.12/smtplib.py", line 580, in data
    (code, repl) = self.getreply()
  File "/usr/local/lib/python3.12/smtplib.py", line 405, in getreply
    raise SMTPServerDisconnected("Connection unexpectedly closed")
smtplib.SMTPServerDisconnected: Connection unexpectedly closed
```

### 7. warning x40

`examples/logs/worker.log:108`

```text
2026-11-12 14:02:57,432 WARNING shop.worker.inventory: retrying inventory_sync batch 37 in 10s (attempt 1)
```

### 8. warning x39

`examples/logs/web.jsonl:156`

```text
{"ts":"2026-11-12T14:03:10.000Z","level":"warning","msg":"readiness probe failed","service":"web","host":"web-1","check":"db_ping","detail":"db ping timeout after 2000ms"}
```
