# llooggeerr 검증용 가상 은행망

실제 HTTP 요청, PostgreSQL 거래, 직원 파일 작업, Linux 프로세스·권한·소켓 이벤트를 만드는 독립 시험 환경입니다. 모든 데이터는 가상입니다. 이전 프로젝트의 코드를 복사하거나 수정하지 않았습니다.

**범위:** 은행 시험망과 모의 중앙 수신 서버를 구현합니다. `llooggeerr` eBPF 수집기 본체는 아직 연결하지 않았습니다. 수집 지표는 `null` / `not_integrated`이며 은행 작업 수를 커널 이벤트 수로 대체하지 않습니다. 중앙 전송 구조 A·B·C, 중요도 최종 정책, 성능 합격 수치는 결정하지 않습니다.

## 빠른 실행

Linux Docker Engine, Docker Compose v2+, Python 3가 필요합니다. Windows에서는 **Docker가 동작하는 WSL 터미널**에서 아래 명령을 실행하세요. 첫 빌드 시 이미지와 Python 의존성 다운로드에는 인터넷 연결이 필요합니다. 업무·시험 클라이언트는 내부 전용 네트워크를 사용하며, 웹 게이트웨이만 localhost 포트 게시용 `web-entry` 브리지에 추가 연결됩니다.

```bash
git clone https://github.com/habang2222/llooggeerr.git
cd llooggeerr/bank-lab
python3 scripts/lab.py doctor
docker compose up -d --build
python3 scripts/lab.py status
python3 scripts/lab.py credentials
```

브라우저에서 [http://127.0.0.1:8080](http://127.0.0.1:8080)에 접속합니다. `credentials`가 보여주는 개발용 `alice` 또는 `bob` 비밀번호로 로그인해 잔액 조회·거래 내역 조회·이체·로그아웃을 실행할 수 있습니다. 비밀번호는 소스 코드에 없으며, 첫 실행 때 생성한 전용 볼륨에 보관됩니다. 이 명령의 출력을 공개 저장소나 결과 보고서에 넣지 마세요.

`.env` 없이 실행할 수 있습니다. 포트 변경은 `.env.example`을 `.env`로 복사한 뒤 `WEB_PORT`를 변경합니다. DB·직원·관리·계정계·로그 서버는 호스트 포트를 공개하지 않습니다. 직원 API는 직원망의 시험 클라이언트로 호출합니다.

## 실행 명령

모든 명령은 `bank-lab/`에서 실행합니다. `make up`, `make test`처럼 Makefile도 사용할 수 있습니다.

| 작업 | 명령 |
|---|---|
| 호스트·커널·Docker·자원 기록 | `python3 scripts/lab.py doctor` |
| 이미지 빌드 | `python3 scripts/lab.py build` |
| 기동 및 health 대기 | `python3 scripts/lab.py up` |
| 상태 | `python3 scripts/lab.py status` |
| 초기 데이터 준비, 기존 잔액 보존 | `python3 scripts/lab.py seed` |
| 짧은 정상 업무 | `python3 scripts/lab.py simulate` |
| 전체 통합 검증 | `python3 scripts/lab.py test` |
| 네트워크 격리 검증 | `python3 scripts/lab.py network` |
| 장애·복구 검증 | `python3 scripts/lab.py recovery` |
| 컨테이너·호스트 PID·namespace 연결 기록 | `python3 scripts/lab.py context` |
| 결과 목록·성능 요약 생성 | `python3 scripts/lab.py report` |
| 볼륨 안의 부하 원본을 호스트로 내보내기 | `python3 scripts/lab.py export` |
| 영속 볼륨을 보존하며 종료 | `python3 scripts/lab.py down` |

`test` / `recovery`는 이 프로젝트 서비스의 정지·재시작과 시험 데이터 변경을 수행합니다. 다른 Docker 프로젝트를 대상으로 하지 않습니다. 전용 볼륨을 지우려면 별도로 `python3 scripts/lab.py reset --confirm ERASE-LAB-DATA`를 입력해야 합니다. 일반 실행·테스트는 전체 데이터 삭제를 수행하지 않습니다.

## 구성과 자원 제한

기본 서비스는 `web`, `customer`, `auth`, `core`, `employee`, `db`와 한 번 실행 후 종료하는 `bootstrap`입니다. 정상 종료한 bootstrap은 장애가 아닙니다. 애플리케이션은 UID 10001, 읽기 전용 루트 파일시스템, 모든 capability 제거, `no-new-privileges`로 실행합니다. PostgreSQL 공식 이미지의 초기화는 root로 시작해 DB 사용자로 내려갑니다.

기본 상시 서비스 메모리 제한 합계는 약 **1,120 MiB**, CPU 할당 상한 합계는 **3.5코어**입니다. 예약량이나 성능 요구사항이 아닙니다. 부하/시험 프로필을 모두 켜면 약 1 GiB의 추가 메모리 상한이 생깁니다. 먼저 `doctor`로 여유 자원을 확인하고 `compose.override.example.yaml`을 참고하여 조정하세요.

| 프로필 | 추가 구성 | 시작만 했을 때 |
|---|---|---|
| `load` | `events`, `load` | 부하 생성기는 대기, 자동 폭주 없음 |
| `logs` | `receiver`, `log-forwarder` | 모의 수신 서버와 전송용 클라이언트 대기 |
| `test` | 격리된 고객·직원 클라이언트, manager, events, receiver, forwarder | 시험 명령 대기 |
| `tools` | manager | 키가 필요한 고정 상태 조회 API |
| `fault` | `oom-probe` | 별도 명령 전까지 대기 |

업무용 Docker 네트워크는 `internal: true`이며 서비스마다 연결망을 명시합니다. 예외인 `web-entry`에는 웹 게이트웨이만 연결하여 `127.0.0.1` 포트를 게시합니다. 이 게이트웨이에는 외부 egress 경로가 있으나 구현된 API는 고정된 내부 고객 서비스로만 요청을 전달합니다. 호스트 전체의 외부 통신 차단을 보장하는 방화벽은 아닙니다. 다중 연결 서비스가 다른 망을 자동 중계하지 않습니다. 허용 정책은 공유 네트워크의 TCP 접근과 애플리케이션 권한 검사를 함께 사용합니다. 같은 브리지의 연결은 방향성 방화벽 규칙이 아니며, Docker 관리자·호스트 root는 격리를 우회할 수 있습니다.

구성도와 전체 연결 표: [architecture.md](docs/architecture.md), [network-matrix.md](docs/network-matrix.md).

## 가상 계정과 거래

| 계정 | 역할 | 허용 업무 |
|---|---|---|
| alice | customer | A001 조회·출금 |
| bob | customer | A002 조회·출금 |
| teller | teller | 가상 고객 조회, 보고서, 자기 파일 |
| auditor | auditor | 보고서, 자기 파일 |
| operator | operator | 고정 운영 작업, 잔액 0인 테스트 계좌 생성 |

초기 A001/A002 잔액은 각각 1,000,000 최소 화폐 단위입니다. 계좌 생성/seed 재실행으로 기존 잔액을 초기화하지 않습니다. 금액은 정수로 처리하고, 이체 시 정렬된 계좌 잠금과 DB 트랜잭션을 사용합니다. 같은 요청 ID·같은 본문은 한 번만 적용하며, 다른 본문에 같은 ID를 쓰면 409를 반환합니다. 동시 요청과 실패 기록은 자동 테스트로 검증합니다. 실제 은행 수준의 금융 안정성이나 규제 적합성을 주장하지 않습니다.

인증 세션은 서버 측 SQLite에 해시로 보관하고 로그아웃 시 폐기합니다. 고객·직원·운영자의 권한을 구분하며 내부 계정계 API도 서비스 키와 사용자 세션을 확인합니다. 개발용 비밀 볼륨을 읽을 수 있는 시험 하네스는 신뢰된 관리 구성요소입니다. 실운영 자격증명 관리 모델이 아닙니다.

## 부하와 이벤트

```bash
# 정상/실패 업무 혼합; 실제 처리량과 목표량을 별도 기록
python3 scripts/lab.py load --duration 30 --users 4 --rps 10 --failure-ratio 0.15 --seed 42 --scenario normal

# 중간 구간에 5배 목표 요청률
python3 scripts/lab.py load --duration 20 --users 8 --rps 10 --seed 42 --scenario burst

# 이벤트 작업 집중: 파일·프로세스·권한·네트워크
python3 scripts/lab.py load --duration 10 --users 2 --rps 4 --scenario hotspot --focus permission --seed 42

# 지속 부하 (시간·동시성은 장비 사양에 맞춰 명시적으로 변경)
python3 scripts/lab.py load --duration 60 --users 4 --rps 10 --scenario sustained --seed 42

# 한 번만 실행하는 64 MiB 제한 컨테이너 OOM 검사
python3 scripts/lab.py oom
```

시나리오: `normal`, `burst`, `sustained`, `hotspot`, `critical`, `multi`. `--focus`로 고객/직원/이벤트 서비스 또는 개별 이벤트를 선택하고 `--user-types customer,employee,events`로 가상 사용자의 업무 유형을 지정합니다. 장애 시나리오는 `recovery` 명령으로 실행합니다. 프리셋 설명은 [scenarios](scenarios/)를 확인하세요.

Ctrl+C/SIGTERM은 새 요청 생성을 중단하고 진행 중 결과를 정리합니다. 강제 종료로 intent만 남으면 미확정입니다. 30분마다 인증 세션을 갱신하고 별도 setup 기록을 남깁니다. 출력 한도는 기본 128 MiB(`--max-output-mib`)이며 남은 결과 기록 공간을 확보할 수 없으면 `storage_budget`으로 새 작업을 중단합니다. 기존 정답을 삭제하거나 샘플링하지 않습니다. 이것은 부하 생성기 보호 한도이며 수집기의 최종 보존 정책이 아닙니다. 시드는 요청 계획을 재현하며 OS 스케줄링, 네트워크 지연, DB 시작 상태까지 동일하게 만들지는 않습니다.

디스크 시험은 16 MiB `/scratch` tmpfs에서 실제 ENOSPC를 발생시킨 후 시험 파일을 지웁니다. 영속 호스트 디스크를 채우지 않습니다. CPU 작업은 최대 3초, 메모리 작업은 최대 32 MiB입니다. OOM은 네트워크·업무 볼륨이 없는 별도의 64 MiB cgroup에서만 실행합니다. 커널 패닉·전체 부팅은 실행하지 않습니다.

## 결과와 수집기 연동

`results/<run-id>/truth.jsonl`은 부하 생성기의 독립 intent/result 기록입니다. 컨테이너에서는 UID 10001 소유의 `results-data` 영속 볼륨에 기록하며, CLI의 load/test 종료 시 호스트 `results/`로 내보냅니다. Docker Compose나 테스트 러너를 직접 호출했다면 `python3 scripts/lab.py export`로 내보내세요. `summary.json`에는 목표/실제 RPS, 완료·실패·예상 결과 일치 건수, 평균/p50/p95/p99가 있습니다. HTTP 실패율에는 의도한 실패 요청도 포함되므로 `unexpected_outcome_count`와 함께 해석하세요. latency의 측정 구간은 summary에 명시되어 있습니다.

`resources-*.jsonl`에는 Docker CPU·메모리·블록 I/O 표본, `container-context.json`에는 PID/cgroup/namespace 메타데이터가 기록됩니다. `test-results.json`은 기대값/실제값과 재현 명령을 남깁니다. 원본 실행 결과는 `.gitignore`로 공개 커밋에서 제외하며, 검토 후 선택한 검증 증거만 `docs/validation/`에 보관합니다.

모의 로그 수신 서버는 `docker compose --profile logs up -d`로 실행합니다. 전송용 `log-forwarder`만 수신망에 붙고, 수신 완료와 SQLite FULL/WAL 커밋 완료를 구분합니다. 중복·충돌·지연·쓰기 실패·실제 네트워크 단절을 시험합니다. 커밋 확인은 전원 상실이나 저장장치 고장에 대한 무손실 보증이 아닙니다.

이벤트 출처·대조 규칙: [event-mapping.md](docs/event-mapping.md). 연동 계약·미정 정책: [collector-integration.md](docs/collector-integration.md). 한 작업이 여러 syscall을 만들 수 있으므로 작업 건수를 수집 대상 이벤트의 분모로 사용하지 않습니다.

향후 수집기는 Linux 호스트에 별도로 설치합니다. `python3 scripts/lab.py sensor-hook start EXTERNAL_RUN_ID` 등으로 외부 실행 사실을 주석으로 기록할 수 있지만, 이 명령이 수집기를 실행한 것으로 간주하지 않습니다. 동일 조건의 baseline/collector 실행을 비교하되, 수집기 미연동 상태에서 보존율이나 손실을 만들어 채우지 않습니다.

## 검증 범위와 남은 작업

자동 검증 방법은 [test-plan.md](docs/test-plan.md), 실제 실행 결과는 [validation.md](docs/validation.md), 기술적 한계는 [limitations.md](docs/limitations.md)에 기록합니다. 이미지 빌드/health 성공, 실제 기능 통과, 수집기 성능 검증은 별개입니다.

남은 수집기 측 작업은 eBPF 프로그램·권한 설정, 이벤트별 관측 지점 확인, 독립 정답과 수집 원본의 매칭 어댑터, 원본 저장/손실 원인 계측, 장기 반복 실험과 사전 합격 기준 확정입니다. WSL 시험 결과를 별도 물리 Linux 서버나 실제 은행 환경의 보장으로 확대하지 않습니다.

## 기술 출처

신규 구현은 FastAPI(MIT), Uvicorn(BSD-3-Clause), HTTPX(BSD-3-Clause), Psycopg(LGPL-3.0), PostgreSQL(PostgreSQL License), Python(PSF License)을 사용합니다. 직접 의존성은 `requirements.txt`, 실제 빌드의 전체 버전은 `requirements.lock`에 고정합니다. 기본 이미지도 검증한 digest로 고정합니다.

- [Docker Compose 네트워크](https://docs.docker.com/reference/compose-file/networks/): 명시적 연결망과 내부망 구성.
- [Docker 자원 제한](https://docs.docker.com/engine/containers/resource_constraints/): 메모리·CPU·swap 제한.
- [PostgreSQL 잠금](https://www.postgresql.org/docs/current/explicit-locking.html): 동시 거래의 행 잠금과 교착 회피.
- [Linux event tracing](https://docs.kernel.org/trace/events.html): 호스트별 관측 가능 trace event 확인.
