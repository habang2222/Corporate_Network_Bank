# llooggeerr 연동 인터페이스와 미정 사항

이 저장소는 llooggeerr를 시험하기 위한 은행망이다. eBPF 수집기 본체를 구현하거나 예전 프로젝트의 코드를 가져오지 않는다. 현재의 `receiver`는 **모의 중앙 수신 서버**이고 실제 수집 성능은 미연동 상태다. 중앙 구조 A·B·C, 중요 이벤트 선정 정책, 보존 기간, 수집 손실 허용치, 성능 합격 수치를 이 환경에서 확정하지 않는다.

## 배치

기본 업무 서비스에 privileged, 호스트 PID namespace, Docker socket, eBPF 권한을 추가하지 않는다. 센서가 준비되면 우선 해당 Linux 호스트에서 별도로 실행하고 필요한 kernel/BTF/tracefs·capability 조건을 센서 버전에 맞추어 검증한다. Windows 프로세스가 아니라 실제 컨테이너가 사용하는 Linux 커널을 관측해야 한다. WSL2와 일반 Linux 서버의 관측 가능 범위는 각각 기록한다.

사용자가 명시한 중요 자원 예시는 `config`에 두며 센서의 최종 규칙으로 자동 변환하지 않는다. 커널 원본, 애플리케이션 감사 기록, 컨테이너 런타임 정보, 센서 자체 진단을 별도 출처로 보존한다. 로그 전달 전용 구성요소만 로그망으로 연결하며 일반 직원·고객 클라이언트에는 원본 저장소를 제공하지 않는다.

## 모의 수신 API

`X-Service-Key`는 부트스트랩에서 생성한 `/secrets/service-key` 값이다. 외부 실제 서비스에 전송하는 기능은 없다. 이 키는 격리 시험망의 개발용 비밀값이며 인터넷 운영 인증 방식이 아니다.

| 요청 | 의미 |
|---|---|
| `GET /health` | 프로세스와 SQLite 연결 상태. 수집기 상태와 별개 |
| `POST /events` | `{server_id,event_id,payload:{...}}` 수신 |
| `GET /events/{server_id}/{event_id}` | 저장 완료된 시험 이벤트 조회 |
| `GET /status` | 저장된 고유 이벤트와 수신 시도 상태별 개수 |
| `POST /control` | `{delay_seconds:0..5,disconnect:false,write_failure:false}` 모의 장애. 생략한 필드는 기본값으로 초기화 |

메시지는 65,536바이트까지 받는다. 이 상한은 모의 프로토콜의 시험용 한도다. 실제 센서의 원본 길이·문자열 정책을 확정한 것이 아니며, 초과 메시지는 413으로 명시적으로 거부한다. 수락한 요청의 원래 HTTP 본문 바이트는 receipts와 최초 events 레코드에 보관한다. 본문 문자열의 정규화본으로 원본을 대체하지 않는다.

새 메시지는 201, 같은 `(server_id,event_id)`와 의미상 같은 JSON payload의 재전송은 200·`duplicate=true`이다. 같은 ID에 다른 payload를 보내면 409로 기록하고 기존 이벤트를 변경하지 않는다. 각 재전송 시도도 별도 receipt와 원래 본문을 남긴다. 서로 다른 JSON 숫자 표현처럼 canonical payload가 달라지는 경우는 충돌로 취급할 수 있으므로 실제 수집기 adapter는 재전송할 원본 메시지를 유지해야 한다.

## 수신과 저장 완료의 경계

1. 입력 검증 후 receipt를 SQLite 별도 트랜잭션으로 저장한다.
2. 이벤트 저장 트랜잭션에서 중복·충돌을 확인하고 이벤트를 삽입한다.
3. WAL 모드, `synchronous=FULL`의 SQLite commit이 성공한 뒤에만 `stored=true`를 반환한다. 그 전에는 200/201을 보내지 않는다.
4. `received_at`은 서버가 요청을 처리한 시각이다. DB의 `committed_at`은 commit에 함께 기록한 논리적 저장 시각이며, 응답의 `storage_ack_at`이 commit 반환을 실제 확인한 시각이다. 시간 정밀도와 동기화 오차는 별도 고려한다.
5. 저장 오류는 507·`stored=false`로 표현한다. 실제 SQLite 오류로 receipt 저장 여부도 확인할 수 없으면 `receipt_persisted=unknown`을 반환한다.

SQLite FULL은 WAL commit마다 동기화를 수행하도록 요청한다. 이는 해당 파일시스템·가상 디스크·저장장치가 동기화 약속을 지킨다는 전제에 의존한다. 전원 손실, 호스트 파일시스템 손상, 하드웨어 내구성까지 보장하는 의미는 아니다. [SQLite synchronous 공식 문서](https://www.sqlite.org/pragma.html#pragma_synchronous)

응답이 유실되면 송신자는 저장 여부를 알 수 없다. 동일 ID/내용을 재전송하여 중복 결과를 확인한다. 모의 `disconnect=true`는 HTTP 503을 반환하는 가용성 장애이고 실제 TCP 단절이 아니다. 실제 로그망 disconnect/reconnect 시험은 호스트 테스트 러너가 Docker network에서 별도로 수행한다. `write_failure=true`는 receipt를 보존하고 이벤트 저장을 생략하여 507을 내는 **모의 저장 실패**이다. 전용 이벤트 tmpfs의 실제 ENOSPC 시험과 구분한다. 제어 설정은 메모리에 있고 receiver 재시작 시 초기화되며, 저장된 이벤트와 중복 판정은 영속 볼륨에 남는다.

이 서버의 저장 성공만으로 센서의 로컬 수집·원본 저장 성공을 판단하지 않는다. 중앙 연결 장애, 로컬 수집 실패, 로컬 저장 실패, 은행 업무 실패는 독립된 상태다.

## 기준선과 수집기 실행 비교

1. 수집기 없이 호스트·이미지·설정·DB 초기 상태를 기록하고 부하를 실행한다. 실제 요청 수, 성공/실패, p50·p95·p99 및 Docker CPU·메모리 계측을 보관한다.
2. 외부 센서를 운영자가 실행하고 센서 버전·실행 ID·관측 지점·규칙·시간 동기화 근거를 기록한다. 같은 seed, 사용자 수, 요청 목표, 시간, 실패 비율, 시나리오, focus, user-types와 초기 상태로 다시 실행한다.
3. `python -m workloads.compare results/<기준선>/summary.json results/<비교>/summary.json --output results/comparison.json`으로 서비스 지표 차이를 계산한다. 조건(출력 예산·세션 갱신 간격 포함)이 다르거나 중단된 실행이면 도구가 거부한다. `storage_budget` 종료도 완료된 동일 조건 시험으로 취급하지 않는다.
4. 비교 라벨은 운영자가 입력한 설명이며 센서가 실제 실행되었다는 증명이 아니다. 도구는 서비스 차이만 계산하고 수집 지표는 검증 전 null로 둔다. CPU/메모리 파일은 외부 Docker 계측 결과를 함께 검토한다.
5. 실제 센서 원본을 연결하는 adapter와 이벤트별 매칭 규칙이 검증된 뒤에만 보존율·고유 수집 수·중복·미확정·원인별 손실·수신/저장 지연·backlog/복구 시간을 계산한다. 작업 요청 수를 이 지표의 분모로 사용하지 않는다.

남은 연동 작업은 센서 배치 권한 검증, host/container 식별 연결, 원본 형식과 adapter, 수집 대상별 독립 이벤트 정답 정의, 시각 대조, 중요도 규칙 연동, 로컬 저장 완료 증거, 중앙 전송 구조 선택, 예비실험 이후 성능 목표 결정이다.
