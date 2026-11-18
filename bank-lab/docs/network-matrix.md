# 네트워크 접근 표

모든 서비스에 네트워크를 명시한다. `default` 공유망에 암묵적으로 연결하지 않는다. 네트워크 이름에는 Compose 프로젝트 접두사가 붙으므로 테스트는 `docker inspect`에서 실제 이름과 IP를 읽는다.

| 네트워크 | 연결 서비스 | 목적 |
|---|---|---|
| web-entry | web만 연결 | 호스트 loopback 웹 포트 게시용 비내부 브리지 |
| customer-net | web, customer-client, load | 가상 고객 → 웹 |
| dmz-api | web, customer | DMZ → 고객 API |
| api-auth | customer, auth | 고객 인증 |
| api-core | customer, core | 고객 계정계 호출 |
| employee-net | employee, employee-client, load | 직원 클라이언트 → 직원 API |
| employee-auth | employee, auth | 직원 인증 |
| employee-core | employee, core | 권한 있는 직원 계정계 호출 |
| core-auth | core, auth | 계정계의 세션 재검증 |
| core-db | core, db | 계정계 → PostgreSQL |
| core-management | core, manager | 명시된 계정계 상태 확인 |
| management | manager, events | 관리 → 이벤트 작업기 |
| log-net | receiver, log-forwarder | 로그 전송 시험 |
| log-control | receiver, manager | 수신기 시험 상태 제어 |
| event-net | events, load | 부하 생성 → 제한 이벤트 작업기 |

`web-entry`를 제외한 모든 내부 전용망에는 `internal: true`를 적용한다. 이 예외는 실제 loopback 포트 게시를 위한 것이며 `web`의 외부 송신 가능성도 생긴다. 시험 도구는 외부 호스트에 연결하는 임의 URL·임의 목적지 API를 제공하지 않고 Reverse Proxy의 전달 목적지는 내부 고객 API로 고정한다. 따라서 업무 요청은 시험장 안에 한정하지만 웹 컨테이너의 네트워크 계층까지 완전한 외부 송신 차단이라고 주장하지 않는다. 외부 이미지·의존성 다운로드는 빌드 단계이며 업무 런타임의 시험 요청과 구분한다.

## 허용과 거절

| 출발 | 목적지 | TCP | HTTP/권한 조건 |
|---|---|---:|---|
| 개발자 호스트 loopback | web 게시 포트 | 허용 | 실제 `/health`·고객 웹 화면 확인 |
| 고객 클라이언트 | web:8080 | 허용 | 로그인·본인 계좌 업무 |
| web | customer:8080 | 허용 | 고정된 전달 경로 |
| customer | auth:8080 | 허용 | 세션 검증 |
| customer | core:8080 | 허용 | 내부 키와 고객 세션, 본인 계좌 |
| 직원 클라이언트 | employee:8080 | 허용 | 직원 로그인·역할 검사 |
| employee | auth:8080 | 허용 | 세션 검증 |
| employee | core:8080 | 허용 | 내부 키와 허용된 직원 역할 |
| core | auth:8080 | 허용 | 세션 재검증 |
| core | db:5432 | 허용 | PostgreSQL 자격 증명 |
| manager | core / events / receiver:8080 | 허용 | 상태 조회 또는 내부 키가 필요한 고정 시험 API |
| log-forwarder | receiver:8080 | 허용 | 내부 키가 필요한 수신 API |
| 고객 클라이언트 | db / core / manager / receiver | 차단 | 공유 네트워크 없음 |
| web | db / manager | 차단 | 공유 네트워크 없음 |
| 일반 직원 클라이언트 | db / receiver | 차단 | 공유 네트워크 없음 |
| customer / employee | manager | 차단 | 불필요한 관리 연결 없음 |
| log-forwarder | db | 차단 | 공유 네트워크 없음 |
| 호스트 외부 | DB·계정계·관리·수신기 | 게시 안 함 | 호스트 포트 매핑 없음 |

일반 직원 클라이언트에는 로그 원본 볼륨도 마운트하지 않는다. 관리 상태 API에 도달할 수 있다는 사실만으로 계정계 업무 API 호출 권한이 생기지 않는다.

## 실제 검증

```bash
docker compose --profile test --profile load --profile logs up -d --wait --wait-timeout 180
python3 tests/run.py --suite network
```

허용 연결은 같은 네트워크의 실제 IP로 TCP 연결한다. 금지 연결은 목적 컨테이너의 **모든 네트워크 IP**에 직접 TCP 연결하여 DNS 이름이 보이지 않는 것만으로 격리를 통과시키지 않는다. 허용 HTTP 상태와 내부 키 없는 계정계 요청 거절도 별도로 검증한다. Docker 런타임에서 실제 게시 포트가 존재하고 `127.0.0.1`에 바인딩되었는지 확인한 뒤, Docker 호스트에서 `/health`와 고객 웹 화면을 직접 요청한다. Compose의 포트 설정만 보고 실제 접근 가능성을 통과시키지 않는다. 실제 값이 기대 값과 다르면 비정상 종료 코드와 JSON 실패 기록을 남긴다.

Docker 호스트 관리자와 Docker socket 접근자는 이 논리적 격리를 우회할 수 있다. 공유 브리지는 방향별 방화벽이 아니며 컨테이너 자체가 침해된 상황까지 보호하는 물리 망 분리를 뜻하지 않는다. 네트워크에 명시되지 않은 우회 접속 여부는 Docker 버전·호스트 방화벽에 따라 달라질 수 있으므로 배포 호스트마다 테스트를 다시 실행한다.
