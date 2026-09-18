"""诊断视图模型：两个适配器的映射契约。

这些用例锁的是**字段位置**，不是字段值——位置错了 UI 就静默不渲染（模板里是
``c.recommended_fix`` / ``snap.tasks``，位置一变就是空白区块，不报错）。

⚠️ fixture 必须照 **``fix-planner`` 的真实输出**写，不能照 ``agents/schemas.py`` 里
顺手看到的那个 schema——同一目录下并排放着多个 agent 的 schema，而 ``problem-log-diagnose``
用的是 ``fix-planner``：它的 rca **没有 ``summary``**（根因在 ``hypotheses`` 里）、
plan **多包了一层 ``plan`` 键**。照错 schema 写的 fixture 会让测试全绿而页面全空。
"""

from __future__ import annotations

from aiops_apm.diagnosis import from_agentflow, from_spike
from aiops_apm.diagnosis.viewmodel import (
    STATUS_ANALYZING,
    STATUS_CLOSED_MANUAL,
    STATUS_COMPLETED,
    STATUS_FAILED,
    agentflow_status,
)

# ── 状态映射 ──────────────────────────────────────────────────────────────────


def test_agentflow_status_maps_terminal_and_active():
    assert agentflow_status("success") == STATUS_COMPLETED
    assert agentflow_status("failed") == STATUS_FAILED
    assert agentflow_status("cancelled") == STATUS_CLOSED_MANUAL
    assert agentflow_status("running") == STATUS_ANALYZING
    assert agentflow_status("queued") == STATUS_ANALYZING


def test_waiting_approval_is_completed_not_analyzing():
    """结论此时已产出，只是卡在审批门上——归 analyzing 会让用户看不到已生成的根因。"""
    assert agentflow_status("waiting_approval") == STATUS_COMPLETED


def test_unknown_status_is_conservative_analyzing():
    """未知状态若归终态，UI 会停轮询并把还在跑的诊断显示成"已完成"。"""
    assert agentflow_status("some_new_status") == STATUS_ANALYZING
    assert agentflow_status(None) == STATUS_ANALYZING


# ── agentflow → 视图模型（fixture = fix-planner 的真实形状）────────────────────


def _run(**over) -> dict:
    run = {
        "run_id": "run-1",
        "workflow": "problem-log-diagnose",
        "status": "waiting_approval",
        "created_at": "2026-09-18T01:00:00Z",
        "updated_at": "2026-09-18T01:05:00Z",
        "nodes": {
            "triage": {"name": "症状分类", "status": "done"},
            "logs": {"name": "日志证据", "status": "done", "output": {"summary": "502 集中在 order-service"}},
            "know": {"name": "历史知识", "status": "done", "output": {"summary": "INC0001 同类"}},
            "locate": {"name": "代码定位", "status": "done", "output": {"summary": "QuotationService.java:61"}},
            # RootCauseSchema：没有 summary，根因在 hypotheses[0]
            "rca": {
                "name": "根因",
                "status": "done",
                "output": {
                    "root_cause_type": "code_bug",
                    "confidence": 0.92,
                    "hypotheses": ["template 为 null 时直接 trim()，缺少空值校验", "上游 rpc 透传 null"],
                    "ruled_out": ["infrastructure", "network"],
                },
            },
            # FixPlanSchema：内容**包在 plan 键下**，步骤只有 type/target/action/expected
            "plan": {
                "name": "修复计划",
                "status": "done",
                "output": {
                    "plan": {
                        "summary": "先止血再根治",
                        "steps": [
                            {
                                "type": "infra_action",
                                "target": "order-service",
                                "action": "紧急回滚至上一稳定版本",
                                "expected": "5xx 停止扩散",
                            },
                            {
                                "type": "code_fix",
                                "target": "QuotationService.java:61",
                                "action": "补空值保护",
                                "expected": "不再抛 NPE",
                            },
                        ],
                    }
                },
            },
            "approve-plan": {"name": "审核修复计划", "status": "waiting_approval"},
        },
        "pending_approvals": [{"node_id": "approve-plan", "trigger": "rca"}],
    }
    run.update(over)
    return run


def test_root_cause_comes_from_hypotheses_not_a_missing_summary():
    """rca 没有 summary 字段。取错位置 → conclusion 整体为 None → 页面全空（真实踩过）。"""
    vm = from_agentflow.build(_run())
    c = vm["conclusion"]
    assert c is not None, "conclusion 不能为空：根因在 rca.hypotheses[0]"
    assert c["root_cause"] == "template 为 null 时直接 trim()，缺少空值校验"


def test_plan_payload_is_unwrapped_from_the_plan_key():
    """plan 内容包在 ``plan`` 键下；不拆层则 summary/steps 全取不到。"""
    vm = from_agentflow.build(_run())
    c = vm["conclusion"]
    assert c["summary"] == "先止血再根治"
    assert len(c["recommended_fix"]) == 1
    assert len(c["recommended_fix"][0]["steps"]) == 2


def test_fix_options_land_in_the_position_ui_reads():
    """UI: dgxFixOptions 读 c.recommended_fix。"""
    c = from_agentflow.build(_run())["conclusion"]
    assert c["confidence"] == "high"  # 0.92 → 档位字符串
    step = c["recommended_fix"][0]["steps"][0]
    assert step["action"] == "紧急回滚至上一稳定版本"
    assert step["target"] == "order-service"
    assert step["type"] == "infra_action"


def test_step_expected_maps_to_expected_effect():
    """渲染器读的是 s.expected_effect；源字段叫 expected。名字不对就整行不显示。"""
    c = from_agentflow.build(_run())["conclusion"]
    assert c["recommended_fix"][0]["steps"][0]["expected_effect"] == "5xx 停止扩散"


def test_numeric_confidence_is_converted_to_ui_band():
    """UI 的 dgxConfChip 只认 high/medium/low；给数值会落到 medium 兜底，
    等于把"很确信"和"没把握"渲染成同一个样子。"""
    c = from_agentflow.build(_run())["conclusion"]
    assert isinstance(c["confidence"], str)
    assert c["confidence"] in ("high", "medium", "low")


def test_confidence_score_is_surfaced_alongside_the_band():
    """档位供配色，分值供人读数——只给档位时审批人分不出 0.92 和 0.51。

    上游 ``rca.confidence`` 是 0~1 小数（本 fixture 为 0.92）；``_confidence_chip``
    压成档位后原值就丢了，故另带 ``confidence_score``（0~100）。两者必须同源。
    """
    c = from_agentflow.build(_run())["conclusion"]
    assert c["confidence"] == "high"
    assert c["confidence_score"] == 92


def test_confidence_score_absent_when_only_a_band_is_available():
    """spike 历史快照只有档位串、没有分值——如实留空，不编一个数出来。"""
    run = _run()
    run["nodes"]["rca"]["output"]["confidence"] = "medium"
    c = from_agentflow.build(run)["conclusion"]
    assert c["confidence"] == "medium"
    assert c["confidence_score"] is None


def test_confidence_score_scales_and_junk():
    """0~1 比例 → 百分数；已是 0~100 的原样；量纲不明或非法值一律 None。"""
    from aiops_apm.diagnosis.from_agentflow import _confidence_score

    assert _confidence_score(0.86) == 86
    assert _confidence_score(86) == 86  # 已经是百分数
    assert _confidence_score("0.5") == 50  # 数字字符串
    assert _confidence_score(1) == 100
    assert _confidence_score(0) == 0
    assert _confidence_score("high") is None  # 档位串：没有分值
    assert _confidence_score(None) is None
    assert _confidence_score(500) is None  # 超量纲，无从判断是 500% 还是别的
    assert _confidence_score(float("nan")) is None
    assert _confidence_score(True) is None  # bool 是 int 子类，别被当成 1


def test_plan_without_decisions_yields_single_recommended_option():
    opts = from_agentflow.build(_run())["conclusion"]["recommended_fix"]
    assert len(opts) == 1
    assert opts[0]["recommended"] is True


def test_decisions_schema_is_supported_when_present():
    """防御性：若将来换成 remediation-planning-analyst（带 decisions 的富 schema），
    互斥选项应变成方案页签、推荐项带 ★。"""
    run = _run()
    run["nodes"]["plan"]["output"] = {
        "summary": "二选一",
        "steps": [{"action": "改代码", "target": "A.java", "risk": "low"}],
        "decisions": [
            {
                "id": "D1",
                "question": "清理策略",
                "recommended": "A",
                "accept_criteria": "磁盘 5 分钟内回落",
                "options": [
                    {"id": "A", "title": "定时清理", "pros": ["简单"]},
                    {"id": "B", "title": "写时清理", "cons": ["侵入业务"]},
                ],
            }
        ],
    }
    opts = from_agentflow.build(run)["conclusion"]["recommended_fix"]
    assert len(opts) == 2
    assert [o["recommended"] for o in opts] == [True, False]
    assert opts[0]["applies_when"] == "磁盘 5 分钟内回落"


def test_steps_carry_no_suggested_diff():
    """已知能力缺口：工作流不产出 diff。UI 有存在性判断，不会渲染空代码块。"""
    c = from_agentflow.build(_run())["conclusion"]
    assert c["recommended_fix"][0]["steps"][0]["suggested_diff"] == ""


def test_tasks_and_tool_calls_are_top_level():
    """dgxChain 读的是 snap.tasks / snap.tool_calls（顶层），不是嵌套对象。"""
    vm = from_agentflow.build(_run(), traces=[_trace_tool()])
    assert vm["tasks"][0]["title"] == "症状分类"
    assert vm["tool_calls"][0]["tool"] == "search_knowledge"


def test_nodes_without_name_fall_back_to_chinese_labels():
    """``GET /runs/{id}`` 的节点字典**不含** ``name``（实测）——不映射就直接把节点 id
    印在页面上给使用者看，等于没标签。"""
    run = _run()
    for node in run["nodes"].values():
        node.pop("name", None)
    titles = [t["title"] for t in from_agentflow.build(run)["tasks"]]
    assert "症状分类" in titles
    assert "修复计划" in titles
    assert "rca" not in titles


def _trace_tool(name="search_knowledge", state="success", result="{\"found\": true}"):
    """照**真实 trace** 的形状：工具信息在 payload 里，name 对 MCP 调用自带前缀。"""
    return {
        "kind": "tool_call",
        "node_id": "know",
        "name": name,
        "payload": {
            "server": "aiops-datasource" if name.startswith("mcp__") else None,
            "is_mcp": name.startswith("mcp__"),
            "input": {"service": "order-service"},
            "result_state": state,
            "result": result,
        },
    }


def test_tool_calls_use_the_renderers_field_names():
    """渲染器读的是 status / args / result_summary（源头叫 result_state / input / result）。
    名字不对**不报错**，只是状态、参数、结果摘要三处全渲染成空白。"""
    calls = from_agentflow.build(_run(), traces=[_trace_tool()])["tool_calls"]
    c = calls[0]
    assert c["status"] == "ok"
    assert c["args"] == {"service": "order-service"}
    assert c["result_summary"].startswith('{"found"')


def test_tool_error_state_maps_to_error():
    calls = from_agentflow.build(_run(), traces=[_trace_tool(state="error")])["tool_calls"]
    assert calls[0]["status"] == "error"


def test_mcp_prefix_survives_so_server_grouping_works():
    """渲染器的 dgxToolParts 靠 ``mcp__{server}__{tool}`` 前缀识别 MCP；
    剥掉前缀会让「使用的 MCP Server」整块消失（dgxMcpServers 返回空）。"""
    calls = from_agentflow.build(
        _run(), traces=[_trace_tool(name="mcp__aiops-datasource__query_logs")]
    )["tool_calls"]
    assert calls[0]["tool"].startswith("mcp__aiops-datasource__")


def test_evidence_chain_is_inside_conclusion_and_uses_finding():
    """dgxChain 读 snap.conclusion.evidence，且结论字段是 ``finding``。"""
    evs = from_agentflow.build(_run())["conclusion"]["evidence"]
    assert [e["source"] for e in evs] == ["日志证据", "历史知识", "代码定位"]
    assert evs[0]["finding"].startswith("502 集中在")


def test_task_status_uses_ui_vocabulary_not_engine_enum():
    """节点状态 → dgxTaskView 认的 todo/in_progress/done/... 不能漏出原始枚举。"""
    statuses = {t["status"] for t in from_agentflow.build(_run())["tasks"]}
    assert statuses <= {"todo", "in_progress", "done", "failed", "cancelled"}


def test_approval_available_only_when_run_is_gated():
    vm = from_agentflow.build(_run())
    assert vm["approval"]["available"] is True
    assert vm["approval"]["node_id"] == "approve-plan"

    vm2 = from_agentflow.build(_run(status="success", pending_approvals=[]))
    assert vm2["approval"]["available"] is False


def test_run_id_is_exposed_as_both_ref_and_session_id():
    """历史快照用 session_id 存轮次标识；实时路径沿用同名字段承接 run_id，
    让状态头不必为引擎分支。"""
    vm = from_agentflow.build(_run())
    assert vm["ref"] == "run-1"
    assert vm["ref_kind"] == "run"
    assert vm["session_id"] == "run-1"


def test_node_error_surfaces_as_viewmodel_error():
    run = _run(status="failed")
    run["nodes"]["rca"]["error"] = "NodeInputError: missing logs"
    assert "NodeInputError" in from_agentflow.build(run)["error"]


def test_unavailable_degrades_instead_of_raising():
    """读路径失败不该让弹窗整个打不开。"""
    vm = from_agentflow.build_unavailable(issue_title="[log] 502")
    assert vm["status"] == STATUS_ANALYZING
    assert vm["error"]
    assert vm["issue_title"] == "[log] 502"


# ── spike → 视图模型 ──────────────────────────────────────────────────────────


def test_spike_snapshot_passes_through_with_engine_fields():
    snap = {
        "status": "completed",
        "session_id": "abc123",
        "trigger": "log",
        "issue_title": "[log] 报价单 500",
        "conclusion": {"root_cause": "磁盘满", "recommended_fix": [{"title": "清盘"}]},
        "tasks": [{"name": "取日志", "status": "done"}],
        "tool_calls": [{"tool": "es_query"}],
        "remediation": {"status": "pending_review"},
    }
    vm = from_spike.build(snap, executions=[{"run_id": "r1"}])
    assert vm["engine"] == "spike"
    assert vm["ref"] == "abc123"
    assert vm["ref_kind"] == "session"
    assert vm["status"] == STATUS_COMPLETED
    # 直通：老路径的结论/方案/链路原样保留（历史条目与实时渲染共用一套）
    assert vm["conclusion"] is snap["conclusion"]
    assert vm["tasks"] == snap["tasks"]
    assert vm["tool_calls"] == snap["tool_calls"]
    assert vm["approval"]["available"] is True
    assert vm["executions"] == [{"run_id": "r1"}]


def test_spike_approval_not_available_when_remediation_settled():
    snap = {"status": "completed", "session_id": "s", "remediation": {"status": "approved"}}
    vm = from_spike.build(snap)
    assert vm["approval"]["available"] is False
    assert vm["approval"]["state"] == "approved"


def test_spike_missing_optional_sections_do_not_crash():
    vm = from_spike.build({"status": "analyzing", "session_id": "s"})
    assert vm["conclusion"] is None
    assert vm["tasks"] == []
    assert vm["tool_calls"] == []
    assert vm["approval"]["available"] is False
