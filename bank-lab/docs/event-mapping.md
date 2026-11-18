# 독립 정답과 관측 이벤트 대조

이 환경의 `truth.jsonl`은 요청 생성기가 직접 작성한 **작업 정답**이다. llooggeerr의 수집 출력이나 모의 중앙 수신 서버의 저장 내용을 정답 생성에 사용하지 않는다. HTTP 요청 수를 Linux 이벤트 수로 바꾸어 보존율을 계산하지 않는다.

## 데이터 출처

| 출처 | 실제로 확인하는 것 | 확인하지 못하는 것 |
|---|---|---|
| 생성기 intent/result | 요청 시도, 응답 코드, 지연, 실패 이유 | 그 요청에서 발생한 전체 syscall 수 |
| 이벤트 작업 응답의 subevents | 고정 작업 함수의 반환·errno·자식 종료 코드 | eBPF에서 같은 사건을 수집했는지 여부 |
| 서비스 JSON 감사 로그 | 로그인·권한 검사·거래 처리·파일 업무 | 커널에서 직접 관측한 원본 |
| PostgreSQL 거래·업무 테이블 및 접속 로그 | DB 내부에서 확정한 업무 상태 | Linux 커널 이벤트 전체 |
| Docker inspect/events 및 호스트 계측 | 컨테이너 상태·재시작·cgroup 제한·자원 계측 | 컨테이너 내부 업무 성공 여부 |
| 모의 수신 서버 | 시험 메시지 수신·저장·중복 | llooggeerr의 실제 수집·로컬 저장 성공 |
| 향후 센서 자체 진단 | 센서 시작·중지·설정 적용·실패 | 강제 종료 직후 자기 상태를 항상 기록할 수 있다는 보장 |

## 7개 분야의 발생 방법과 후보 매칭

| 분야 | 실제 발생 작업 | 관측 후보와 대조 단서 | 주의 |
|---|---|---|---|
| 파일 | 생성·쓰기·읽기·append·rename·unlink·없는 파일 읽기 | open/openat, read/write, rename/unlink; 작업 경로·컨테이너·시간 구간·반환값 | Python 한 함수가 여러 syscall을 만들 수 있고 내부 파일도 접근한다. |
| 프로세스 | 고정 Python 자식 실행, exit 0, exit 3, SIGTERM 종료 | fork/clone, exec, exit; 자식 PID·부모 PID·시작 시간·실행 파일 | 컨테이너 PID와 호스트 PID를 별도로 연결해야 한다. exit 3과 시그널 종료를 구분한다. |
| 권한 | chmod 000, 읽기 EACCES, root 소유자 변경 EPERM | chmod/chown/open 관련 결과·UID·경로 | 거부는 의도한 시험 결과이며 업무 요청 HTTP 성공과 구분한다. |
| 네트워크 | loopback bind/listen/connect/accept/send/recv, 비수신 포트 연결 거부 | socket 관련 관측점·소켓 주소·방향·PID·errno | 패킷·연결·HTTP 요청은 서로 다른 단위다. 전용 작업은 외부 주소를 입력받지 않는다. |
| 컨테이너 | 테스트 러너의 시작·재시작·강제 종료·복구 | Docker 런타임 상태와 메타데이터, cgroup/namespace | Docker 이벤트는 eBPF 원본이 아니다. |
| 센서 관리 | 센서 연동 상태·외부 실행 구간 표시 | 향후 센서의 실행 ID, 설정 버전, 자체 진단과 외부 감시 | 현재 센서 미연동. 센서 지표는 null이다. |
| 시스템 | 제한된 CPU·메모리 작업, 전용 tmpfs ENOSPC, 별도 OOM 프로브 | 자원 계측과 syscall 오류, OOM 관련 커널/런타임 근거 | 시스템 부팅·커널 패닉·전원 손실은 독립 VM 등 별도 환경이 필요하다. |

## 정답 파일 형식

각 실행은 `results/<run_id>/truth.jsonl`, `summary.json`, `metrics.sqlite`를 남긴다. `metrics.sqlite`는 모든 측정 지연을 저장하여 전체 표본의 정확한 p50·p95·p99를 계산한다. 메모리에서 응답 전체를 누적하거나 표본을 버리는 방식은 사용하지 않는다.

- `record_type=intent`: 요청을 보내기 전에 기록·flush·fsync한다. `success=null`은 아직 결과를 모른다는 뜻이다.
- `record_type=result`: 동일 `run_id`·`operation_id`로 응답 결과를 기록한다. `return_code`는 HTTP 코드이며 연결 실패는 null과 예외 사유를 기록한다. 이벤트 응답 `subevents`의 syscall 반환값과 혼동하지 않는다.
- `phase=setup`: 인증 세션 준비와 장시간 실행 중 갱신. 요약의 `setup_requests`로 따로 센다.
- `phase=workload`: 측정한 업무 요청. 요약의 `total_requests`와 대응한다.
- `record_type=schedule_skip`: 동시 실행 상한 때문에 보내지 않은 계획 슬롯. `attempted=false`; 수집 누락으로 세지 않는다.
- `record_type=run_end`: 실제 처리량과 종료 사유. 없으면 중단된 실행일 수 있다.

요청 대상, 시각, 작업 유형, 서비스, 시도 여부, 성공 여부, 반환 코드, 사유, 목표 RPS, 당시 누적 완료 수, 시나리오를 기록한다. `success`는 HTTP 2xx 여부이다. `expectation_met`는 예상한 거부 코드 및 이벤트 하위 작업 결과까지 평가한다. 예를 들어 잘못된 로그인 401은 `success=false`, `expectation_met=true`이다. 비밀번호·세션 토큰은 정답에 기록하지 않는다.

`intent`만 남고 `result`가 없으면 완료 여부 **미확정**이다. 프로세스 강제 종료 중 파일 마지막 줄이 잘렸다면 원본을 보존하고 파싱 실패 구간을 별도 처리해야 한다. 정상 종료는 실행 중인 요청의 제한된 타임아웃을 기다린 후 요약을 쓴다. SIGKILL에 대한 완전한 종료 기록은 보장하지 않는다.

## 매칭 절차

1. 시험 전에 센서의 수집 대상, 관측 지점, 필수 필드, 시간 오차, 중복 관측 규칙을 확정하고 버전과 실행 ID를 보관한다.
2. Docker의 컨테이너 ID·호스트 PID·시작 시각·이미지·네트워크와 cgroup/namespace 정보를 실행 구간별로 저장한다. 이벤트 응답의 `/proc/self/cgroup`은 cgroup namespace 때문에 `/`로 보일 수 있어 이것만으로 컨테이너를 식별하지 않는다.
3. 서비스는 `X-Run-ID`, `X-Operation-ID`를 애플리케이션 감사 기록에 전달한다. 이 헤더가 커널 이벤트에 자동 포함된다고 가정하지 않는다.
4. 전용 파일 작업은 고유 작업 경로, 프로세스 작업은 PID·실행 파일·수명, 네트워크 작업은 주소·방향·시간 구간으로 **후보**를 찾는다. UID, cgroup 및 namespace가 일치하는지 추가 확인한다.
5. 시도와 완료, 실제 성공과 예상 거부, 같은 사건의 여러 관측 단계를 구분한다. 시간만 가까운 후보가 여러 개면 미확정으로 남긴다.
6. 매칭 근거가 없는 작업·이벤트를 수집 성공이나 누락으로 임의 판정하지 않는다. 미지원, 미관측 대상, 수집 실패, 저장 실패, 전송 실패, 원인 불명은 별도 집계한다.

커널의 실제 지원 이벤트와 필드는 호스트의 `available_events` 및 각 이벤트의 `format`으로 확인한다. 이 저장소는 특정 커널의 tracepoint 지원을 보장하지 않는다. [Linux Event Tracing 공식 문서](https://docs.kernel.org/trace/events.html)

## 부하 재현과 안전 범위

인증 세션은 1,800초마다 새 세션으로 갱신하여 기본 3,600초 만료 전에 교체한다. 갱신 요청도 고유 작업 ID와 `operation_type=session_refresh`, `phase=setup`으로 intent/result를 남긴다. `setup_requests`에는 최초 로그인과 갱신을 모두 포함하며, 갱신에 쓰인 시간은 실제 전체 실행 시간에 포함한다. 매우 낮은 RPS에서도 대기 시간을 시험 종료 시각과 세션 갱신 시각으로 제한한다.

`--max-output-mib`는 기본 128MiB(설정 범위 4~1,024MiB)이다. 생성기는 새 작업 전에 현재 결과 파일 크기와 파일시스템 여유 공간을 확인하고, 진행 중인 요청 결과 및 최종 요약을 쓸 여유를 남긴다. 부족하면 `stop_reason=storage_budget`으로 새 요청 생성을 중단하고 진행 중인 요청 결과를 기록한다. 이미 기록한 정답을 삭제하거나 샘플링하지 않는다. 이는 생성기의 보호 한도이며 수집기 보존 정책이 아니다. 예상치 못한 응답 크기나 다른 프로세스의 동시 쓰기까지 절대적인 디스크 사용 상한으로 보장하지 않는다. 지속 실행에서 더 큰 출력이 필요하면 여유 공간을 확인한 뒤 이 옵션을 명시적으로 조정한다.

`--seed`는 계획 슬롯의 작업 선택을 재현한다. 스레드/네트워크 스케줄, 저장장치 지연, 이전 실행으로 바뀐 DB 상태까지 동일하게 만들지는 않는다. 동일 조건 비교 전에 초기 DB 상태와 호스트 동시 부하를 기록해야 한다. `--user-types customer,employee,events`는 가상 사용자 번호별 작업 분야를 순환 지정한다. `--focus customer|employee|events|file|process|permission|network|cpu|memory|disk`는 집중 대상을 정한다.

`--preset`은 `scenarios/*.json`의 값을 읽으며 명시한 CLI 옵션이 우선한다. burst는 전체 시간의 40~60% 구간에서 기준 RPS의 5배를 **목표**로 한다. 요약에는 목표와 실제 요청 수·RPS를 분리한다. 큐를 무한히 늘리지 않고 동시 사용자 상한에서 계획 요청을 건너뛰며 근거를 남긴다. 생성기 자체 fsync와 컨테이너 CPU 제한으로 목표보다 적게 실행할 수 있다.

이벤트 작업은 한 번에 하나만 실행하며 중첩 요청은 429로 기록한다. CPU는 요청당 최대 3초, 메모리는 최대 32MiB·3초다. 디스크 시험은 `/scratch`가 16MiB 이하의 전용 tmpfs인지 검사한 뒤 최대 20MiB까지만 쓰며 ENOSPC를 확인하고 자신의 시험 파일만 제거한다. 이 tmpfs 시험은 실제 디스크 장치의 고갈이나 물리적 쓰기 실패를 검증하지 않는다. CPU·메모리 상한은 Compose와 Docker에서 별도로 제한한다. [Docker 자원 제한 공식 문서](https://docs.docker.com/engine/containers/resource_constraints/)
