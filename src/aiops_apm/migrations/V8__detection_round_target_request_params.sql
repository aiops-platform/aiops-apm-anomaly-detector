-- V8：detection_round_target 补 request_params JSON 列 —— 本轮采集实际下发的出站请求参数快照。
-- 采集器在 params 完全构造后（时间窗口/水位线下推/时区转换）写入 {method, url, params}，
-- 供审计「这一轮到底请求了什么」——尤其排查 startTime/endTime 是否按源时区正确下发
-- （Spring 等源按本地墙钟解析，时区配错会漂移 8 小时导致重复采集）。
-- params 只含 URL 查询参数，不含 headers（resolved 后可能带明文凭据，落库有泄密风险）。

USE aiops_apm_runtime;

ALTER TABLE detection_round_target ADD COLUMN request_params JSON DEFAULT NULL AFTER error;
