# Simulation Report V12 contract

`GET /api/v1/simulations/{simulationId}/report`는 `reportVersion: DETERMINISTIC_V12`를 반환합니다.

## 원칙 준수 복기

`principleReviewSummary`는 전체 실제 거래를 집계합니다.

```json
{
  "followedCount": 2,
  "violatedCount": 1,
  "decisionDifferenceCount": 3,
  "unassessedCount": 1,
  "assessedTradeCount": 3,
  "totalTradeCount": 7
}
```

`decisionReviews[]` 주요 신규 필드:

- `principleJudgment`: `FOLLOWED`, `VIOLATED`, `DECISION_DIFFERENCE`, `NOT_APPLICABLE`, `INSUFFICIENT_DATA`
- `matchedPrinciple`: 원칙 제목·원문·출처·실행 규칙·기대 행동
- `marketOutcome`: 실제 종목 가격으로 계산한 5거래일·20거래일 결과
- `reviewCase`: 원칙 판단과 5거래일 가격 결과를 결합한 4분류

`reviewCase` 값:

| 값 | 의미 |
|---|---|
| `GOOD_PROCESS_GOOD_OUTCOME` | 원칙 준수, 유리한 가격 결과 |
| `GOOD_PROCESS_BAD_OUTCOME` | 원칙 준수, 불리한 가격 결과 |
| `BAD_PROCESS_LUCKY_OUTCOME` | 원칙 위반, 유리한 가격 결과 |
| `BAD_PROCESS_BAD_OUTCOME` | 원칙 위반, 불리한 가격 결과 |
| `UNASSESSED` | 원칙 또는 가격 데이터 부족 |

개인 원칙봇과 행동이 다르더라도 명시적 사용자 원칙 또는 실행 규칙 위반 근거가 연결되지 않으면 `VIOLATED`가 아니라 `DECISION_DIFFERENCE`입니다.

## 근거 검증

`evidenceReviews[]`는 실제 거래 전체를 반환합니다.

- `databaseBasisType`: DB 원본 `rationale_label_type`
- `basisType`: 화면용 정규화 유형
- `basisTypeSource`: `DATABASE`, `DETERMINISTIC_KEYWORD_FALLBACK`, `NOT_CLASSIFIED`
- `verifiability`: `VERIFIABLE`, `AMBIGUOUS`, `UNVERIFIABLE`
- `webVerdict`: `PENDING`, `NOT_SELECTED`, `CONFIRMED`, `PARTIAL`, `CONTRADICTED`, `UNCONFIRMED`
- `marketOutcome`: 근거 사실 판정과 분리된 실제 가격 결과

DB 유형이 `UNCLASSIFIED`일 때만 키워드 기반 보조 분류를 사용합니다.

근거 유형은 별도 DB 컬럼을 추가하지 않고 기존 `simulation_runs.analytics_json`의 `rationaleTypeSnapshots`에 저장합니다. 최종 화면용 판정은 기존 `report_json`에도 포함됩니다. 시뮬레이션 거래를 DB에서 다시 불러올 때 거래 ID가 달라질 수 있으므로 종목·매매 방향·체결 시각·수량·가격·근거 문장으로 생성한 안정적인 매칭 키를 사용해 유형을 복원합니다.

웹 검색은 변동 폭 기준 핵심 거래 최대 3건에 수행합니다. 검색 담당은 출처와 사실만 수집하고 판단 담당은 검색 자료만으로 근거를 판정합니다. 주가 수익률은 웹 근거 판정에 전달하지 않습니다.

## 종목별 그래프

`securityEvidenceReviews[]`는 종목별로 다음을 제공합니다.

- `priceSeries`: 시뮬레이션 기간 일별 종가
- `evidenceReviews`: 해당 종목 거래 근거
- `chartAnnotations`: 매수·매도, 5일·20일 평가점, 검색으로 확인한 근거 자료 발행일

`chartAnnotations[].type`:

- `BUY`, `ADD`, `SELL`, `REDUCE`
- `OUTCOME_CHECKPOINT`
- `EVIDENCE_EVENT`

## 비동기 상태

결정론적 원칙·가격 판정은 즉시 생성하고 웹 검증은 기존 백그라운드 리포트 보강 과정에서 수행합니다. 웹 검색 실패 또는 API 미설정 시에도 원칙 준수 판정, 가격 결과, DB 근거 유형과 그래프는 유지됩니다.
