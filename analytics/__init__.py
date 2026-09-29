"""Phân tích & thống kê: SEQ, anomaly, ping, tương quan MQTT ↔ ping."""

from .anomaly_detector import (
    ALERT_LABELS,
    LEVEL_CRITICAL,
    LEVEL_INFO,
    LEVEL_LABELS,
    LEVEL_ORDER,
    LEVEL_WARNING,
    AlertRecord,
    AlertRule,
    AnomalyDetector,
)
from .correlation import (
    VERDICT_GATEWAY_SILENT,
    VERDICT_LABELS,
    VERDICT_LINK_DOWN,
    VERDICT_NO_DATA,
    VERDICT_OK,
    VERDICT_PARTIAL,
    VERDICT_PING_BLOCKED,
    CorrelationReport,
    GatewayCorrelation,
    correlate,
)
from .gateway_result import (
    CRITERIA,
    DEFAULTS,
    FAIL,
    NA,
    PASS,
    RESULT_FAIL,
    RESULT_PASS,
    TOTAL_UID,
    GatewayResult,
    GatewayResultEvaluator,
    compute_coverage,
    expected_for_gateway,
)
from .ping_stats import PingStats, PingTracker, summarize_ping_csv
from .sequence_tracker import SeqOutcome, SeqState, SequenceTracker

__all__ = [
    # SEQ
    "SequenceTracker",
    "SeqState",
    "SeqOutcome",
    # Cảnh báo
    "AnomalyDetector",
    "AlertRule",
    "AlertRecord",
    "LEVEL_INFO",
    "LEVEL_WARNING",
    "LEVEL_CRITICAL",
    "LEVEL_ORDER",
    "LEVEL_LABELS",
    "ALERT_LABELS",
    # Ping
    "PingTracker",
    "PingStats",
    "summarize_ping_csv",
    # Tương quan
    "correlate",
    "CorrelationReport",
    "GatewayCorrelation",
    "VERDICT_OK",
    "VERDICT_LINK_DOWN",
    "VERDICT_GATEWAY_SILENT",
    "VERDICT_PING_BLOCKED",
    "VERDICT_PARTIAL",
    "VERDICT_NO_DATA",
    "VERDICT_LABELS",
    # Đạt / không đạt
    "GatewayResultEvaluator",
    "GatewayResult",
    "compute_coverage",
    "expected_for_gateway",
    "CRITERIA",
    "DEFAULTS",
    "PASS",
    "FAIL",
    "NA",
    "RESULT_PASS",
    "RESULT_FAIL",
    "TOTAL_UID",
]
