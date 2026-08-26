"""DomainConfig 写入侧校验（UC-7.3，M6 遗留项落地）。

``PUT /v1/config/{domain}`` 时对 ``DomainConfig`` 做表驱动的结构校验：
- detector 插件参数按类型校验（``static_threshold``/``simple_compare``/``signature_aggregate``）；
- suppressor 插件参数按类型校验（``maintenance_window``/``blacklist``）；
- 插件名不存在 → ``ConfigValidationError``（400）；
- 自定义插件（registry 可解析但无内置 schema）跳过结构校验（无法结构约束）。

注意：校验只在**写入**时执行；seed（domains.yaml）只做 ``DomainConfig.model_validate``
基础校验（M6 既有行为），不在此处强制——因此 suppressor 的 ``duration_minutes``/``pattern``
等仅在显式给出时校验，避免拒绝 seed 形态的合法配置。
"""

from __future__ import annotations

from collections.abc import Callable
from numbers import Real

from aiops_apm.exceptions import AppException, ErrorCode
from aiops_apm.models.config import DomainConfig
from aiops_apm.plugins.registry import PluginRegistry


class ConfigValidationError(AppException):
    """配置非法：错误码 ``CONFIG_ERROR``，HTTP 400。"""

    def __init__(self, reason: str) -> None:
        super().__init__(ErrorCode.CONFIG_ERROR, reason)


def _require_numeric(params: dict, key: str, *, positive: bool = False) -> None:
    if key not in params:
        return
    value = params[key]
    if not isinstance(value, Real) or isinstance(value, bool):
        raise ConfigValidationError(f"param {key!r} must be a number, got {type(value).__name__}")
    if positive and value <= 0:
        raise ConfigValidationError(f"param {key!r} must be positive, got {value}")


def _require_nonempty_str(params: dict, key: str) -> None:
    if key not in params:
        return
    value = params[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigValidationError(f"param {key!r} must be a non-empty string")


def _validate_static_threshold(params: dict) -> None:
    if "threshold" not in params:
        raise ConfigValidationError("static_threshold requires param 'threshold'")
    _require_numeric(params, "threshold")


def _validate_simple_compare(params: dict) -> None:
    if not params:
        raise ConfigValidationError("simple_compare requires param 'baseline' or 'ratio'")
    if "baseline" not in params and "ratio" not in params:
        raise ConfigValidationError("simple_compare requires param 'baseline' or 'ratio'")
    _require_numeric(params, "baseline")
    _require_numeric(params, "ratio", positive=True)


def _validate_signature_aggregate(params: dict) -> None:
    _require_numeric(params, "min_count", positive=True)
    _require_numeric(params, "n_frames", positive=True)


def _validate_maintenance_window(params: dict) -> None:
    _require_numeric(params, "duration_minutes", positive=True)


def _validate_blacklist(params: dict) -> None:
    _require_nonempty_str(params, "pattern")


# 表驱动：内置 detector / suppressor 插件名 -> 参数校验器（未知插件跳过）
_DETECTOR_SCHEMAS: dict[str, Callable[[dict], None]] = {
    "static_threshold": _validate_static_threshold,
    "simple_compare": _validate_simple_compare,
    "signature_aggregate": _validate_signature_aggregate,
}
_SUPPRESSOR_SCHEMAS: dict[str, Callable[[dict], None]] = {
    "maintenance_window": _validate_maintenance_window,
    "blacklist": _validate_blacklist,
}


def validate_domain_config(cfg: DomainConfig, registry: PluginRegistry) -> None:
    """校验 ``DomainConfig``；非法抛 ``ConfigValidationError``（→ HTTP 400）。"""
    for spec in cfg.detectors:
        try:
            plugin = registry.get("detector", spec.plugin)
        except AppException as exc:  # noqa: BLE001 -- 包装为配置错误（插件不存在）
            raise ConfigValidationError(f"detector plugin not found: {spec.plugin}") from exc
        checker = _DETECTOR_SCHEMAS.get(plugin.name)
        if checker is not None:
            checker(spec.params)

    for sup in cfg.suppressors:
        try:
            plugin = registry.get("suppressor", sup.name)
        except AppException as exc:  # noqa: BLE001 -- 包装为配置错误（插件不存在）
            raise ConfigValidationError(f"suppressor plugin not found: {sup.name}") from exc
        checker = _SUPPRESSOR_SCHEMAS.get(plugin.name)
        if checker is not None:
            checker(sup.params)
