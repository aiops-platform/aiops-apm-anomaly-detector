-- V10：problem_record.service 加宽 —— M9 跨服务合并后它是**逗号拼接的服务名列表**。
--
-- 原先 VARCHAR(64) 只装得下一个服务名。三个服务名拼接（如
-- "gateway-service,order-service,warranty-service" = 44 字符）勉强，再多一个或服务名
-- 更长就会超长报 22001（value too long），**整轮采集失败**。加宽到 255 留足余量。
--
-- group_key / open_group_key 生成列 / uk_open_group_key 索引**不需要**跟着加宽：
-- M9 让 ProblemRecord.group_key 的 service 段取自新增字段 group_key_service
-- （= 排序后的第一个服务名，仍 ≤64），与对外展示的 service 列解耦。
-- 见 models/record.py 的 group_key 属性与 pipeline/grouping.representative_service。
--
-- PG 的 ALTER COLUMN TYPE 只改元数据，不重写表。
--
-- 注意：这里**不写** SET search_path —— schema 由 MigrationRunner 按 settings.db_schema
-- 注入（tests/test_migrations.py::test_scripts_do_not_hardcode_schema 守着这条）。

ALTER TABLE problem_record ALTER COLUMN service TYPE VARCHAR(255);
