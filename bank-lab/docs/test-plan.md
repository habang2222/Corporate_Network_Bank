# 통합 검증 계획과 실행

실행 중이라는 상태와 검증을 통과했다는 결과를 구분한다. `tests/run.py`는 Linux Docker 호스트에서 실행하며 테스트 클라이언트 컨테이너 내부에서 실제 HTTP/TCP 요청을 발생시킨다. 테스트가 실패하면 종료 코드는 1이다. 테스트의 기대 값·실제 값·출발 컨테이너·오류를 `results/test-results.json`에 함께 기록한다. 각 검사에는 실행 ID, 독립 작업 ID, UTC 기록 시각, `independent_test_harness` 출처가 포함된다. 검사 작업 1건은 커널 이벤트 1건을 의미하지 않는다. 자동화 중단·예외도 실패로 기록한다.

```bash
docker compose --profile test --profile load --profile logs up -d --build --wait --wait-timeout 180
python3 tests/run.py --suite functional --output results/functional.json
python3 tests/run.py --suite network --output results/network.json
python3 tests/run.py --suite recovery --output results/recovery.json
python3 tests/run.py --suite load --output results/load-tests.json
# 또는 순차 전체 실행
python3 tests/run.py --suite all
# 직접 호출한 부하 시험의 원본을 호스트로 복사
python3 scripts/lab.py export
```

테스트 러너의 검사 결과 JSON은 호스트 `results/`에 기록한다. 부하 생성기의 `truth.jsonl`·`summary.json`·`metrics.sqlite`는 `results-data` named volume에 먼저 저장되므로 위의 내보내기 명령으로 호스트에서 읽을 수 있게 한다. `python3 scripts/lab.py test` 또는 `load`를 사용하면 종료 시 자동으로 내보낸다. 볼륨 안 원본은 내보내기 후에도 보존된다.

장애 시험은 해당 프로젝트의 서비스만 중지·복구한다. 실행 중인 다른 부하 시험과 병렬 실행하지 않는다. 거래 테스트는 가상 계좌 잔액과 거래 이력을 실제 변경한다. 기존 볼륨을 삭제하거나 테스트 전 초기화하지 않는다. 반복 실행은 새 요청 ID를 사용하며 각 시험 시작 시 잔액을 읽어 상대 변화량을 확인한다.

| 분야 | 기대 결과 | 실제 검증 방법 |
|---|---|---|
| 인증 | 올바른 로그인 200, 잘못된 비밀번호 401, 로그아웃 토큰 401 | 고객 클라이언트 HTTP |
| 계좌 접근 | 본인 계좌만 노출, 타인 계좌 403 | 계좌 목록·개별 조회 |
| 실제 이체 | 출금·입금 정수 금액 일치 | 양쪽 잔액 전후 비교 |
| 실패 이체 | 부족 잔액 409, 0원 422, 잔액 불변 | HTTP와 DB 기반 잔액 조회 |
| 동시 이체 | 양방향 요청 성공, 잔액 합계·정확한 변화량 일치 | 12개 worker, 24개 요청 |
| 중복 처리 | 같은 요청 한 번 반영, 다른 내용 충돌 409 | 순차·동시 재전송 |
| 직원 권한 | teller 고객 조회, auditor 보고서, operator 고정 작업 | 역할별 허용·거절 HTTP |
| 파일 | 생성·수정·조회·삭제, 다른 직원 파일 접근 불가 | 실제 파일 API |
| 작업 제한 | 미등록 명령·경로 거절 | enum·경로 검증 |
| 이벤트 | 파일·프로세스·권한·소켓 작업 결과 존재 | 제한 작업기의 실제 반환값 |
| 디스크 실패 | 전용 tmpfs에서 ENOSPC | 유한한 쓰기와 errno 검사 |
| 네트워크 | 허용 TCP/HTTP 성공, 금지 실제 IP 연결 실패 | 모든 목적 IP에 소켓 연결 |
| 노출 제한 | web loopback만 게시, 명시 망, privileged 없음 | Compose 실제 설정 검사 |
| 호스트 웹 접근 | 실제 게시 포트 loopback, 건강 상태·홈페이지 200 | Docker 런타임 검사와 호스트 HTTP |
| 은행 복구 | 장애 관측, 수신기는 정상, 복구 후 잔액 보존 | web/customer/core/db 중지·기동 |
| DB 영속 멱등성 | DB 재시작 후 같은 이체 재전송은 추가 적용 안 됨 | 잔액·응답 비교 |
| 중앙 저장 | 수신과 commit 구분, 중복 200, 충돌 409 | SQLite 기반 수신 API |
| 중앙 장애 | 503·지연·507 구분, 은행 서비스 지속 | 모의 제어 API와 독립 고객 요청 |
| 중앙 망 단절 | 전송 실제 실패, 관리·은행 정상, 재연결 후 중복 식별 | 전용 Docker network 분리·복구 |
| 시험 작업기 종료 | SIGKILL 종료137, 은행 정상, 작업기 복구 | 한정된 events 서비스 강제 종료 |
| 부하 생성기 | 독립 정답 수·성공·실패 일치, seed 반복, SIGTERM 안전 중단 | 실제 CLI 3회 실행과 JSONL 재열람 |

## 실패 재현

JSON의 `name`, `source_container`, `destination_ip`, `expected`, `actual`를 확인한다. 서비스 로그에는 비밀번호나 토큰을 출력하지 않는다.

```bash
docker compose ps -a
docker compose logs --tail 150 customer core auth employee db receiver events
python3 tests/run.py --suite functional --output results/retry-functional.json
```

의존 서비스가 준비되지 않았거나 프로파일이 시작되지 않으면 해당 suite 실행 실패로 남는다. 실패를 자동 성공 처리하거나 건너뛰지 않는다. 복구 시험은 `finally`에서 중지한 서비스와 단절한 네트워크를 복원한다. 호스트 강제 종료 등으로 Python 자체가 실행을 이어갈 수 없으면 `docker compose --profile test --profile load --profile logs up -d` 후 상태를 확인한다.

## 부하·수집 성능

부하 생성기는 목표 RPS와 실제 시도 수·처리량을 분리하고 성공·실패 및 p50/p95/p99를 기록한다. 독립 정답 파일의 intent와 result를 대조하여 시도 미완료를 표시한다. 같은 seed는 작업 계획의 재현에 사용하며 운영체제 스케줄링·네트워크 지연까지 동일하게 만들지는 않는다.

`llooggeerr` 연결 전의 결과는 수집기 미실행 기준선이다. 수집된 고유 커널 이벤트 수, 중요 이벤트 보존율, 큐 적체·수집 손실은 수집기 미연동 상태로 남긴다. 임의 EPS·p99 합격 기준은 정하지 않는다. 커널·서비스 이미지·설정·시나리오·seed·시험 시간을 고정한 후 수집기를 연결하여 비교한다.

검증 결과 파일은 해당 실행에 대한 근거다. 이후 다른 호스트·커널·설정에서도 같은 결과가 보장된다는 뜻은 아니다.
