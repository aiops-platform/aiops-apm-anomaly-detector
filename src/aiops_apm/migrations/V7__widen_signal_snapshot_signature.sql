-- V7：signal_snapshot.signature 加宽 —— 长堆栈日志签名可超 255 字符。
-- 根因：签名 = 异常类型 + 顶部 N 帧（去行号），深包名可轻松到 300-500 字符
-- （实测 Spring 异常 376 字符），varchar(255) 写库报错 → 采集降级。
-- 加宽只消除溢出，不改变 L1 signature_aggregate 分组语义（代码侧另有 MAX_SIGNATURE_LEN 兜底）。
-- PG 的 ALTER COLUMN TYPE 只改元数据，不重写表。

ALTER TABLE signal_snapshot ALTER COLUMN signature TYPE VARCHAR(1024);

COMMENT ON COLUMN signal_snapshot.signature IS '日志堆栈签名';
