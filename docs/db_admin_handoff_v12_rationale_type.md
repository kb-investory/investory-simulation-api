# V12 시뮬레이션 근거 유형 컬럼 DB 적용 요청서

## 목적

원본 투자일지의 `journal_trade_notes.rationale_label_type` 값을 시뮬레이션 실행 결과에도 보존합니다. 시뮬레이션을 DB에서 다시 조회해 리포트를 재생성해도 당시 근거 유형이 유지되어야 합니다.

## 변경 범위

- 변경 대상: 시뮬레이션 전용 테이블 `simulated_trades`
- 추가 컬럼: `rationale_label_type VARCHAR(40) NOT NULL DEFAULT 'UNCLASSIFIED'`
- 원본 테이블 `trades`, `journal_trade_notes`는 변경하지 않습니다.
- 신규 테이블을 만들지 않습니다.

## 적용 스크립트

`migrations/20260818_add_simulated_trade_rationale_type.sql`

스크립트는 현재 선택된 데이터베이스의 `INFORMATION_SCHEMA`를 확인한 뒤 컬럼이 없을 때만 추가하도록 작성되어 있습니다. DB 위치가 변경되는 경우 대상 스키마를 먼저 선택한 다음 실행해 주세요.

## 배포 순서

1. 대상 DB와 `journal_trade_notes.rationale_label_type`, `simulated_trades` 존재 여부 확인
2. 마이그레이션 SQL 실행
3. 아래 검증 SQL로 컬럼 확인
4. V12 애플리케이션 배포

애플리케이션의 시뮬레이션 저장 쿼리가 새 컬럼을 사용하므로 DB 마이그레이션을 먼저 적용해야 합니다.

## 사전 확인 및 검증 SQL

```sql
SELECT DATABASE();

SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'journal_trade_notes'
  AND COLUMN_NAME = 'rationale_label_type';

SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_DEFAULT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'simulated_trades'
  AND COLUMN_NAME = 'rationale_label_type';
```

첫 번째 조회에서 원본 `journal_trade_notes.rationale_label_type`이 확인되지 않으면 애플리케이션 배포를 중단하고 원본 DB 스키마 담당자와 먼저 확인해 주세요. 이 전달 스크립트는 원본 테이블을 변경하지 않습니다.

적용 후 기대 결과:

```text
TABLE_NAME        simulated_trades
COLUMN_NAME       rationale_label_type
DATA_TYPE         varchar
IS_NULLABLE       NO
COLUMN_DEFAULT    UNCLASSIFIED
```

## 데이터 처리 방식

- 실제 사용자 거래: `journal_trade_notes.rationale_label_type` 값을 복사
- 개인 원칙봇·비교봇 거래: 기본값 `UNCLASSIFIED`
- 기존 시뮬레이션 거래: 컬럼 추가 시 기본값 `UNCLASSIFIED`
- 기존 데이터에 대한 임의 역분류 또는 일괄 업데이트는 수행하지 않습니다.

## 롤백 참고

애플리케이션을 V11 이하로 되돌린 뒤 아래 컬럼을 제거할 수 있습니다. 삭제 전 해당 컬럼 데이터가 더 이상 필요하지 않은지 확인해 주세요.

```sql
ALTER TABLE simulated_trades DROP COLUMN rationale_label_type;
```
