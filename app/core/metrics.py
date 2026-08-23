"""
================================================================================
[Metrics] app/core/metrics.py
================================================================================
■ 역할:
  - 프로메테우스로 스크레이핑할 지표(Counter/Histogram) 정의를 한곳에 모아둡니다.
  - HTTP 요청 전체에 대한 지표는 app/main.py의 미들웨어에서, POST /simulations/run의
    단계별 지표는 simulation_run_service.py의 기존 perf_counter 구간에서 채웁니다
    (#24 참고).
================================================================================
"""

from prometheus_client import Counter, Histogram

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests received",
    ["method", "path", "status_code"],
)

HTTP_REQUEST_LATENCY_SECONDS = Histogram(
    "http_request_latency_seconds",
    "HTTP request latency in seconds",
    ["method", "path", "status_code"],
)

# 시뮬레이션 실행 단계별 소요시간. 기존 executionTimingMs 응답 필드와 같은 구간을
# 재는 것이지만, 여기서는 요청 시작 시점부터의 누적이 아니라 각 단계 자체의
# 순수 소요시간만 관측한다.
SIMULATION_STAGE_LATENCY_SECONDS = Histogram(
    "simulation_stage_latency_seconds",
    "POST /simulations/run stage latency in seconds",
    ["stage"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)
