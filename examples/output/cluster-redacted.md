# Log clusters

- Files: `examples/logs/web.jsonl`, `examples/logs/payments.log`
- Records: 498 from 562 lines (formats: json 454, text 44)
- Time range: 2026-11-12T13:51:05.311Z to 2026-11-12T14:12:11.905Z
- Rate histogram: 24 buckets of about 53s each
- Level filter: warning and above (304 records below it skipped)
- Clusters: 8 (showing 8)

| # | Level | Count | First seen | Last seen | Rate | Signature |
|--:|---|--:|---|---|---|---|
| 1 | error | 131 | 2026-11-12T14:03:01.800Z | 2026-11-12T14:09:34.999Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁▅███████▂▁▁` | `checkout failed: psycopg2.OperationalError: connection to server at "<str>" (<ip>), port <n> failed: FATAL: sorry, too many clients already` |
| 2 | error | 4 | 2026-11-12T13:51:05.311Z | 2026-11-12T14:11:20.733Z | `█▁▁▁▁▁▁▁█▁▁▁▁▁▁▁█▁▁▁▁▁▁█` | `com.fernhill.payments.ChargeService - charge failed for order ord_<n> [java.net.SocketTimeoutException]` |
| 3 | warning | 39 | 2026-11-12T14:03:10Z | 2026-11-12T14:09:30.869Z | `▁▁▁▁▁▁▁▁▁▁▁▁▁▄▇█▇▇▇█▇▁▁▁` | `readiness probe failed` |
| 4 | warning | 13 | 2026-11-12T13:51:40.120Z | 2026-11-12T14:12:11.905Z | `█▁█▁█▁█▁█▁█▁█▁██▁▁█▁█▁██` | `payment gateway slow response` |
| 5 | warning | 4 | 2026-11-12T13:56:14.077Z | 2026-11-12T14:10:44.920Z | `▁▁▁▁▁█▁▁▁▁▁█▁▁▁▁▁█▁▁▁▁█▁` | `com.fernhill.payments.GatewayClient - slow response from gateway: <n>ms` |
| 6 | warning | 1 | 2026-11-12T13:59:10.330Z | 2026-11-12T13:59:10.330Z | `▁▁▁▁▁▁▁▁▁█▁▁▁▁▁▁▁▁▁▁▁▁▁▁` | `session refresh rejected` |
| 7 | warning | 1 | 2026-11-12T14:00:15.500Z | 2026-11-12T14:00:15.500Z | `▁▁▁▁▁▁▁▁▁▁█▁▁▁▁▁▁▁▁▁▁▁▁▁` | `com.fernhill.payments.ChargeService - charge declined for order ord_<n>: card <n> expired` |
| 8 | warning | 1 | 2026-11-12T14:00:41.900Z | 2026-11-12T14:00:41.900Z | `▁▁▁▁▁▁▁▁▁▁█▁▁▁▁▁▁▁▁▁▁▁▁▁` | `token verify failed: expired` |

## Samples

### 1. error x131

`examples/logs/web.jsonl:151`

```text
{"ts":"2026-11-12T14:03:01.800Z","level":"error","msg":"checkout failed","service":"web","host":"web-1","order_id":"ord_88264","request_id":"req_e1eae507","error":"psycopg2.OperationalError: connection to server at \"db.example.com\" (<ip>), port 5432 failed: FATAL:  sorry, too many clients already","status":500}
```

### 2. error x4

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

### 3. warning x39

`examples/logs/web.jsonl:156`

```text
{"ts":"2026-11-12T14:03:10.000Z","level":"warning","msg":"readiness probe failed","service":"web","host":"web-1","check":"db_ping","detail":"db ping timeout after 2000ms"}
```

### 4. warning x13

`examples/logs/web.jsonl:19`

```text
{"ts":"2026-11-12T13:51:40.120Z","level":"warning","msg":"payment gateway slow response","service":"web","host":"web-1","gateway":"payments.example.com","gateway_ms":2844}
```

### 5. warning x4

`examples/logs/payments.log:27`

```text
2026-11-12 13:56:14,077 [http-nio-8081-exec-7] WARN  com.fernhill.payments.GatewayClient - slow response from gateway: 2311ms
```

### 6. warning x1

`examples/logs/web.jsonl:108`

```text
{"ts":"2026-11-12T13:59:10.330Z","level":"warning","msg":"session refresh rejected","service":"web","host":"web-1","user":"<email>","detail":"Authorization: <redacted> rejected for <email>"}
```

### 7. warning x1

`examples/logs/payments.log:51`

```text
2026-11-12 14:00:15,500 [http-nio-8081-exec-2] WARN  com.fernhill.payments.ChargeService - charge declined for order ord_70290: card <card> expired
```

### 8. warning x1

`examples/logs/web.jsonl:124`

```text
{"ts":"2026-11-12T14:00:41.900Z","level":"warning","msg":"token verify failed","service":"web","host":"web-1","token":"<redacted>","reason":"expired"}
```
