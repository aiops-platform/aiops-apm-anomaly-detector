"""配置模型：检测规则（M6 用于 ``domain_config`` 写入校验，M5 加载规则）。

契约在 M1 冻结，之后只允许增加可选字段。
"""

from pydantic import BaseModel, Field


class DetectorSpec(BaseModel):
    """单个检测器规则（signal 匹配 + 插件 + 参数 + 严重度）。"""

    signal: str | dict  # 结构化 matcher 或信号名
    plugin: str
    params: dict = Field(default_factory=dict)
    severity: str = "warning"


class SuppressorSpec(BaseModel):
    """单个抑制器规则。"""

    name: str
    params: dict = Field(default_factory=dict)


class CorrelationSpec(BaseModel):
    """L2 关联窗口参数。"""

    metric_log_window_sec: int = 300
    change_window_sec: int = 300


class VerifySpec(BaseModel):
    """L3 验证参数（持续性轮数 / 误报率闸门 / 最小样本数）。"""

    persistence_rounds: int = 2
    false_positive_threshold: float = 0.6
    min_samples: int = 20


class DomainConfig(BaseModel):
    """一个 domain 的完整检测配置（解析 ``domain_config`` 表的 JSON）。"""

    detectors: list[DetectorSpec]
    suppressors: list[SuppressorSpec] = Field(default_factory=list)
    correlation: CorrelationSpec = Field(default_factory=CorrelationSpec)
    verify: VerifySpec = Field(default_factory=VerifySpec)
