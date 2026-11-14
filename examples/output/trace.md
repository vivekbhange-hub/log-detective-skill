# Stack trace groups

- Files: `examples/logs/worker.log`, `examples/logs/payments.log`
- Traces found: 48 in 3 groups (formats: java 4, python 44)
- Application code: frames matching `shop/`, `com.fernhill`

| # | Count | Exception | First seen | Last seen | Top application frame |
|--:|--:|---|---|---|---|
| 1 | 40 | `psycopg2.OperationalError` | 2026-11-12T14:02:57.430Z | 2026-11-12T14:09:27.256Z | `/app/shop/db.py:41` in `connect` |
| 2 | 4 | `java.net.SocketTimeoutException` | 2026-11-12T13:51:05.311Z | 2026-11-12T14:11:20.733Z | `GatewayClient.java:147` in `com.fernhill.payments.GatewayClient.post` |
| 3 | 4 | `smtplib.SMTPServerDisconnected` | 2026-11-12T13:51:20.330Z | 2026-11-12T14:11:55.061Z | `/app/shop/worker/emails.py:57` in `send_confirmation` |

## 1. psycopg2.OperationalError x40

- Root cause: `psycopg2.OperationalError: connection to server at "db.example.com" (203.0.113.20), port 5432 failed: FATAL:  sorry, too many clients already`
- Surfaced as: `shop.db.DatabaseUnavailable: cannot open connection`
- Log message: `shop.worker.inventory: inventory_sync failed for batch 37`
- First at `examples/logs/worker.log:93` (python format)
- Application frames, innermost first:
  1. `/app/shop/db.py:41` in `connect`
  2. `/app/shop/db.py:47` in `connect`
  3. `/app/shop/worker/inventory.py:88` in `run_batch`

```text
Traceback (most recent call last):
  File "/app/shop/db.py", line 41, in connect
    return psycopg2.connect(dsn, connect_timeout=3)
  File "/usr/local/lib/python3.12/site-packages/psycopg2/__init__.py", line 122, in connect
    conn = _connect(dsn, connection_factory=connection_factory, **kwasync)
psycopg2.OperationalError: connection to server at "db.example.com" (203.0.113.20), port 5432 failed: FATAL:  sorry, too many clients already

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "/app/shop/worker/inventory.py", line 88, in run_batch
    conn = db.connect()
  File "/app/shop/db.py", line 47, in connect
    raise DatabaseUnavailable("cannot open connection")
shop.db.DatabaseUnavailable: cannot open connection
```

## 2. java.net.SocketTimeoutException x4

- Root cause: `java.net.SocketTimeoutException: Read timed out`
- Surfaced as: `com.fernhill.payments.GatewayException: Gateway call failed after 3 attempts`
- Log message: `com.fernhill.payments.ChargeService - charge failed for order ord_70211`
- First at `examples/logs/payments.log:5` (java format)
- Application frames, innermost first:
  1. `GatewayClient.java:147` in `com.fernhill.payments.GatewayClient.post`
  2. `GatewayClient.java:104` in `com.fernhill.payments.GatewayClient.charge`
  3. `GatewayClient.java:112` in `com.fernhill.payments.GatewayClient.charge`

```text
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
	at java.base/sun.nio.ch.NioSocketImpl.implRead(NioSocketImpl.java:304)
	at org.apache.http.impl.io.SocketInputBuffer.fillBuffer(SocketInputBuffer.java:82)
	at com.fernhill.payments.GatewayClient.post(GatewayClient.java:147)
	at com.fernhill.payments.GatewayClient.charge(GatewayClient.java:104)
	... 6 more
```

## 3. smtplib.SMTPServerDisconnected x4

- Root cause: `smtplib.SMTPServerDisconnected: Connection unexpectedly closed`
- Log message: `shop.worker.emails: failed to send confirmation for order ord_70215 to ben.okafor@example.com`
- First at `examples/logs/worker.log:6` (python format)
- Application frames, innermost first:
  1. `/app/shop/worker/emails.py:57` in `send_confirmation`

```text
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
