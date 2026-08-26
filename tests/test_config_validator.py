"""UC-7.3 配置写入侧校验：detector/suppressor 参数表驱动，非法 → ConfigValidationError。"""

import pytest

from aiops_apm.config.validator import ConfigValidationError, validate_domain_config
from aiops_apm.models.config import DetectorSpec, DomainConfig, SuppressorSpec
from aiops_apm.plugins.registry import PluginRegistry


def _cfg(detectors: list[DetectorSpec], suppressors: list[SuppressorSpec] | None = None) -> DomainConfig:
    return DomainConfig(detectors=detectors, suppressors=suppressors or [])


def _det(*, plugin: str, params: dict, signal: str | dict = "cpu_usage") -> DetectorSpec:
    return DetectorSpec(signal=signal, plugin=plugin, params=params)


def test_valid_static_threshold_passes() -> None:
    reg = PluginRegistry().load()
    cfg = _cfg([_det(plugin="static_threshold", params={"threshold": 0.9})])
    validate_domain_config(cfg, reg)  # 不抛


def test_static_threshold_missing_threshold_rejected() -> None:
    reg = PluginRegistry().load()
    cfg = _cfg([_det(plugin="static_threshold", params={})])
    with pytest.raises(ConfigValidationError, match="threshold"):
        validate_domain_config(cfg, reg)


def test_simple_compare_empty_params_rejected() -> None:
    reg = PluginRegistry().load()
    cfg = _cfg([_det(plugin="simple_compare", params={})])
    with pytest.raises(ConfigValidationError, match="baseline"):
        validate_domain_config(cfg, reg)


def test_simple_compare_baseline_or_ratio_passes() -> None:
    reg = PluginRegistry().load()
    for params in ({"baseline": 50.0}, {"ratio": 1.2}, {"baseline": 50.0, "ratio": 1.2}):
        validate_domain_config(_cfg([_det(plugin="simple_compare", params=params)]), reg)


def test_signature_aggregate_nonpositive_min_count_rejected() -> None:
    reg = PluginRegistry().load()
    cfg = _cfg([_det(plugin="signature_aggregate", params={"min_count": 0, "n_frames": 5})])
    with pytest.raises(ConfigValidationError, match="min_count"):
        validate_domain_config(cfg, reg)


def test_signature_aggregate_valid_passes() -> None:
    reg = PluginRegistry().load()
    validate_domain_config(_cfg([_det(plugin="signature_aggregate", params={"min_count": 3, "n_frames": 5})]), reg)


def test_maintenance_window_bad_duration_rejected() -> None:
    reg = PluginRegistry().load()
    cfg = _cfg(
        [_det(plugin="static_threshold", params={"threshold": 0.9})],
        suppressors=[SuppressorSpec(name="maintenance_window", params={"duration_minutes": -5})],
    )
    with pytest.raises(ConfigValidationError, match="duration_minutes"):
        validate_domain_config(cfg, reg)


def test_suppressor_without_params_passes() -> None:
    # seed 形态 {name: maintenance_window} 无 params → 参数未显式给出，不强制校验
    reg = PluginRegistry().load()
    cfg = _cfg(
        [_det(plugin="static_threshold", params={"threshold": 0.9})],
        suppressors=[SuppressorSpec(name="maintenance_window")],
    )
    validate_domain_config(cfg, reg)


def test_unknown_detector_rejected() -> None:
    reg = PluginRegistry().load()
    cfg = _cfg([_det(plugin="no_such_detector", params={})])
    with pytest.raises(ConfigValidationError, match="not found"):
        validate_domain_config(cfg, reg)


def test_config_error_maps_to_400() -> None:
    from aiops_apm._app import _status_for_code
    from aiops_apm.exceptions import AppException, ErrorCode

    exc = ConfigValidationError("bad config")
    assert isinstance(exc, AppException)
    assert exc.code == ErrorCode.CONFIG_ERROR
    assert _status_for_code(ErrorCode.CONFIG_ERROR) == 400
