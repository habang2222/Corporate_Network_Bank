# 재사용 가능한 가상 은행 회사망

Docker Compose로 실행하는 독립적인 회사망 예시입니다. 고객 웹, 직원 업무, 사내 메일·공유 문서, 인증, 계정계, PostgreSQL, 경계 방화벽, 중앙 보안 감사 기록으로 구성됩니다. 모든 계정과 거래는 가상 데이터입니다. 특정 로거나 부하 생성기를 실행할 필요가 없습니다.

## 실행

Linux Docker Engine과 Compose v2가 필요합니다. Windows에서는 Docker Desktop의 Linux 엔진을 켜고 실행합니다.

```bash
cd bank-lab
docker compose up -d --build
docker compose ps
```

고객 화면은 `http://127.0.0.1:8080`입니다. 방화벽 컨테이너만 호스트 포트를 열며, 웹·직원·DB·보안 기록 서비스는 호스트에 게시되지 않습니다. 개발용 계정과 비밀번호는 최초 구동 때 Docker `secrets` 볼륨에 생성됩니다. 로컬 관리자가 확인하려면 `docker compose exec auth cat /secrets/users.json`을 실행합니다. 이 출력은 외부에 공유하지 마세요.

기존 데이터 볼륨을 유지한 채 내리려면 `docker compose down`을 사용합니다. 다른 복사본을 동시에 띄울 때는 `.env.example`을 복사하고 `COMPOSE_PROJECT_NAME`, `WEB_PORT`, `NETWORK_OCTET`을 각기 다르게 설정합니다. 내부망은 기본적으로 `10.240.1.0/28`부터 `10.240.8.0/28`을 사용합니다. 기존 VPN·사내망과 겹치면 `NETWORK_OCTET`을 사용하지 않는 값으로 바꿉니다.

## 서비스와 접근 경로

| 서비스 | 역할 | 접근 |
| --- | --- | --- |
| firewall | 공개 경계의 고정 경로·메서드 정책, 허용/거절 감사 | 호스트 localhost:8080 |
| web | 고객 화면 및 고객 API 역방향 프록시 | firewall에서만 |
| customer | 고객 계좌·거래 요청 | web에서만 |
| employee | 직원 고객 조회, 보고서, 개인 업무 파일, 계좌 개설 요청 | 내부 직원망 |
| office | 사내 메시지와 직원 공동 문서 | 내부 직원망 |
| auth | 고객·직원 세션 및 역할 확인 | 필요한 서비스의 내부망 |
| core | 계좌 소유권, 이체, 거래·업무 감사 | 고객·직원 서비스 |
| db | 계좌·거래 영속 저장 | core 전용망 |
| security | 서비스·방화벽의 중앙 감사 이벤트 영속 저장 | 서비스별 감사망 |

고객은 `alice`, `bob`; 직원은 `teller`, `auditor`, `operator`입니다. 고객은 자기 계좌만 조회·이체할 수 있습니다. `operator`는 가상 고객의 잔액 0원 계좌를 만들 수 있습니다. 사내 메시지와 공유 문서는 직원 역할로만 사용할 수 있습니다.

직원망을 수동으로 살펴볼 때만 도구 컨테이너를 띄웁니다.

```bash
docker compose --profile tools up -d employee-client
docker compose exec -T employee-client python scripts/smoke.py staff
```

평상시에는 `employee-client`를 실행할 필요가 없습니다. 사내 API는 `http://employee:8080`, `http://office:8080`이며 직원망 컨테이너에서만 이름을 해석할 수 있습니다.

## 방화벽과 보안 기록

`firewall`은 모든 공개 HTTP 요청의 유일한 경유지입니다. `config/edge-routes.json`의 경로·메서드만 `web`으로 전달하고, 최대 64 KiB 요청 본문을 허용합니다. 각 허용·거절을 구조화된 이벤트로 `security`에 전송합니다. 이 컨테이너는 HTTP 경계 정책이며, Docker 호스트 전체의 IP 패킷을 검사하는 커널 방화벽이라고 주장하지 않습니다. 내부망 연결은 Compose 네트워크와 애플리케이션 인증·역할 검사로 제한합니다.

은행 서비스도 업무 감사 이벤트를 `security`에 보냅니다. 수신기는 SQLite에 원본 JSON, SHA-256, 수신 시각을 저장하며 같은 이벤트 ID의 재전송을 중복으로 구분합니다. `GET /events?after_id=...&limit=...`는 서비스 키가 있는 내부 호출에만 응답합니다. 각 서비스는 자신의 stdout에도 감사 기록을 남기고 중앙 전송이 실패하면 실패 상태를 출력합니다. 중앙 기록은 보안 조사 자료이며 침입 차단·자동 탐지 제품은 아닙니다.

## 다른 프로젝트에서 재사용

1. 이 디렉터리를 독립 Compose 프로젝트로 복사하고 `COMPOSE_PROJECT_NAME`과 포트를 정합니다.
2. `firewall`과 `security`를 유지하고, 고객 웹과 업무 서비스를 새 프로젝트 서비스로 교체합니다.
3. `WEB_URL`과 `config/edge-routes.json`을 새 웹 API에 맞춥니다. 공개 경로는 허용 목록에 명시합니다.
4. 새 서비스가 중앙 기록을 쓰려면 `security`와 공유하는 내부망에서 `POST /events`에 구조화된 JSON을 보냅니다. 필수 필드는 `event_id`, `source`, `service`, `event`입니다. `X-Service-Key`는 자동 생성된 `service-key`를 사용합니다.
5. 인증·DB·사내망 연결은 필요한 서비스에만 부여합니다. 외부 로거를 붙일 경우 중앙 감사 API와 서비스 stdout을 관측 출처로 사용할 수 있지만 이 회사망은 로거 설치를 요구하지 않습니다.

망 구조와 경계는 [architecture.md](docs/architecture.md), 서비스별 접근은 [network-matrix.md](docs/network-matrix.md), 사후 조사에 필요한 기록은 [security-events.md](docs/security-events.md)에 설명했습니다.

## 간단 확인

```bash
docker compose config --quiet
docker compose up -d --build
docker compose exec -T firewall python scripts/smoke.py customer
docker compose --profile tools up -d employee-client
docker compose exec -T employee-client python scripts/smoke.py staff
docker compose exec -T security python scripts/smoke.py audit
docker compose exec -T auth python -m unittest services.bank.test_boundaries -v
```

이 확인은 일반 고객·직원 업무와 보안 기록 저장을 검증합니다. 부하나 장애를 만들지 않습니다. Docker Desktop이 꺼져 있으면 `docker compose config`와 Python 정적 검사는 가능하지만 실제 서비스 실행은 할 수 없습니다.

이 프로젝트는 개발용 회사망입니다. 단일 호스트 Docker 네트워크는 물리 망 분리나 실운영 은행의 방화벽·비밀관리·고가용성을 대신하지 않습니다.
