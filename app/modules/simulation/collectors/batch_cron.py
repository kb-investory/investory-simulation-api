"""
================================================================================
[Investory Engine Module] batch_cron.py
================================================================================
■ 전체 기능 설명:
  - 금융감독원 OpenDART 공시 데이터 및 일별 주가 시세 증분 수집 백그라운드 자동 배치 스케줄러입니다.
  - 백그라운드 데몬 스레드에서 지정된 주기(기본: 1시간 또는 16:30 장마감 후)에 실행됩니다.
================================================================================
"""

import threading
import datetime
from zoneinfo import ZoneInfo
from app.modules.simulation.collectors.dart_collector import DartCollector
from app.modules.simulation.collectors.fundamentals_collector import FundamentalsCollector
from app.modules.simulation.persistence.db_persistence import get_db_connection

_scheduler_thread = None
_stop_event = threading.Event()
KST = ZoneInfo("Asia/Seoul")

# uvicorn --workers N으로 띄우면 프로세스마다 이 모듈이 별도로 로드되고, 각자 자기만의
# 스케줄러 스레드를 띄운다 — 그래서 N개 프로세스가 전부 같은 순간(16:30 KST)에 깨어나
# 이 배치를 동시에 실행하려 든다. MySQL 네임드 락(GET_LOCK)으로 그중 하나만 실제로
# 실행하게 막는다 — 작업이 끝날 때까지(락을 쥔 채로) 진행하므로, 먼저 획득 못 한 나머지
# 프로세스는 그 순간 곧바로 스킵하고 다음 날 스케줄로 넘어간다(같은 날 다시 시도 안 함).
_BATCH_JOB_LOCK_NAME = "investory_daily_batch_job"


def _run_batch_job():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT GET_LOCK(%s, 0)", (_BATCH_JOB_LOCK_NAME,))
            acquired = cur.fetchone()[0] == 1
        if not acquired:
            print("[Batch Cron] 다른 워커 프로세스가 오늘 배치를 이미 실행 중 — 이 워커는 건너뜀.")
            return

        try:
            _run_batch_job_locked()
        finally:
            with conn.cursor() as cur:
                cur.execute("SELECT RELEASE_LOCK(%s)", (_BATCH_JOB_LOCK_NAME,))
    finally:
        conn.close()


def _run_batch_job_locked():
    print(f"[Batch Cron] Starting daily data collection batch at {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}...")

    # 1. OpenDART 당일 공시 수집 및 AI 영향 분석 DB 반영
    try:
        collector = DartCollector()
        count = collector.fetch_and_save_daily_dart_disclosures()
        print(f"[Batch Cron] OpenDART batch finished: {count} new disclosures processed.")
    except Exception as e:
        print(f"[Batch Cron Error] Failed OpenDART batch: {e}")

    # 2. 당일 정기보고서가 있으면 시점 재무 데이터를 증분 반영
    try:
        result = FundamentalsCollector().update_from_disclosure_date()
        print(f"[Batch Cron] OpenDART fundamentals batch finished: {result['savedReportCount']} reports saved.")
    except Exception as e:
        print(f"[Batch Cron Error] Failed fundamentals batch: {e}")

def seconds_until_next_run(
    now: datetime.datetime | None = None,
    run_hour: int = 16,
    run_minute: int = 30,
) -> float:
    current = now or datetime.datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    target = current.replace(hour=run_hour, minute=run_minute, second=0, microsecond=0)
    if target <= current:
        target += datetime.timedelta(days=1)
    return (target - current).total_seconds()


def _scheduler_loop(run_hour: int = 16, run_minute: int = 30):
    print(f"[Batch Cron Daemon] Daily OpenDART scheduler started ({run_hour:02d}:{run_minute:02d} KST).")
    while not _stop_event.is_set():
        wait_seconds = seconds_until_next_run(run_hour=run_hour, run_minute=run_minute)
        if _stop_event.wait(wait_seconds):
            break
        _run_batch_job()

    print("[Batch Cron Daemon] Background batch scheduler thread terminated cleanly.")

def start_batch_scheduler(run_hour: int = 16, run_minute: int = 30):
    global _scheduler_thread, _stop_event
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        print("[Batch Cron] Scheduler is already running.")
        return

    _stop_event.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(run_hour, run_minute),
        daemon=True,
        name="opendart-daily-scheduler",
    )
    _scheduler_thread.start()
    print("[Batch Cron] Background scheduler launched successfully.")

def stop_batch_scheduler():
    global _stop_event
    _stop_event.set()
    print("[Batch Cron] Stopping background scheduler requested.")
