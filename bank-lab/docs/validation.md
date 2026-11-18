# 실행 검증 결과

2026-11-18(한국 시각)에 WSL2 Ubuntu 24.04의 Docker Engine에서 실제 컨테이너를 실행하여 확인했다. 최종 전체 통합 검증은 **259개 통과, 0개 실패**다. 기대값과 실제값은 [test-results.json](validation/test-results.json)에 모두 남겼다.

## 기능·격리·복구

- 고객 인증·로그아웃·계좌 소유권, 실제 이체, 부족 잔액, 동시 반대 방향 이체, 동시 중복 요청, 요청 ID 충돌.
- 직원 역할별 업무·보고서·파일·운영 작업, 테스트 계좌 생성, 파일 경로·타입 경계.
- localhost 웹 페이지와 실제 127.0.0.1 포트 게시, 허용 TCP/HTTP, 금지 목적지의 모든 실제 IP 연결 실패.
- 웹·고객 API·계정계·DB 정지 중 실패와 다른 서비스 영향, 재기동 후 잔액·멱등성 보존.
- 수신기 재기동·중복·내용 충돌·지연·모의 저장 실패, 실제 로그 네트워크 분리/복구, 이벤트 작업기 SIGKILL/복구.
- 파일·프로세스·권한·소켓 작업과 제한 CPU·메모리, 16 MiB tmpfs에서 실제 ENOSPC.
- 독립 intent/result 정답 집계, 동일 seed 반복, SIGTERM 정리. 최종 부하 재검증은 [test-load-final.json](validation/test-load-final.json).
- 인증·입력·세션 갱신·낮은 RPS·출력 예산·SQLite 저장 경계 단위 테스트 **18개 통과**: [실행 로그](validation/final-unit-tests.log).
- 64 MiB 일회용 컨테이너의 실제 OOM 결과: [oom-result.json](validation/oom-result.json). 호스트 전체 OOM 시험은 아니다.

## 짧은 기준선 실험

모두 수집기 미실행 상태다. 예비 실행값이며 성능 합격 기준을 정하거나 장시간 안정성을 보장하지 않는다. 요청 지연은 HTTP 요청부터 응답까지이며 정답 fsync 비용은 제외한다. 정답 기록 비용은 생성기의 실제 처리량에 영향을 준다. 의도한 401/403/422 등도 HTTP 실패에 포함하므로 예상 밖 결과 수를 따로 표시했다.

| 시나리오 | 요청 시간(초) | 기준 목표 RPS | 실제 요청 수 | 실제 RPS | p95(ms) | p99(ms) | 예상 밖 결과 |
|---|---:|---:|---:|---:|---:|---:|---:|
| burst | 10 | 10 | 80 | 7.15 | 1658.84 | 2365.05 | 2 |
| critical | 6 | 4 | 24 | 3.97 | 618.36 | 1050.04 | 0 |
| hotspot | 5 | 2 | 10 | 2.00 | 25.24 | 32.69 | 0 |
| multi | 6 | 4 | 24 | 3.90 | 456.15 | 506.09 | 0 |
| normal | 12 | 8 | 65 | 5.31 | 1359.38 | 1840.37 | 0 |
| sustained | 30 | 8 | 143 | 4.60 | 1638.91 | 2171.89 | 0 |

burst의 중간 20% 구간은 기준 목표의 5배이며 총 목표량은 각 summary에 별도로 기록된다. CPU·메모리·블록 I/O 표본은 `validation/resources-*.jsonl`, 실행 설정과 상세 통계는 각 `*-summary.json`에 있다. [정상 실행의 전체 독립 정답](validation/normal-truth.jsonl)은 수집기 출력으로 만들지 않았다.

burst에서 예상 밖 응답 2건은 이벤트 작업기가 이미 작업 중일 때 반환한 HTTP 429였다. 한 번에 하나의 제한 작업만 허용하는 보호 동작으로 파일·소켓 작업 요청이 거절된 것이다. [해당 독립 결과](validation/burst-unexpected.json)를 남겼으며 이를 수집기 누락으로 분류하지 않는다. 목표 RPS와 실제 RPS의 차이도 숨기지 않았다.

## 재현 명령

```bash
docker compose build bootstrap
python3 scripts/lab.py test
docker compose exec -T auth python -m unittest services.bank.test_boundaries -v
docker compose exec -T load python -m unittest workloads.test_workloads services.receiver.test_receiver -v
python3 scripts/lab.py load --duration 12 --users 4 --rps 8 --failure-ratio 0.2 --seed 42 --scenario normal
python3 scripts/lab.py load --duration 10 --users 6 --rps 10 --failure-ratio 0.15 --seed 42 --scenario burst
python3 scripts/lab.py oom
python3 scripts/lab.py report
```

## 발견·수정한 문제와 미확정 사항

1. 모든 네트워크를 internal로만 만들었을 때 Docker가 localhost 포트를 실제로 게시하지 않았다. 웹만 `web-entry` 브리지에 연결하고 실제 HTTP/포트 검사를 추가하여 확인했다. 게이트웨이 외부 송신 경로의 한계는 접근 표에 명시했다.
2. 인증과 모의 수신기의 SQLite 연결 종료, 비ASCII 잘못된 인증 입력, 장시간 세션 갱신, 낮은 RPS의 종료 시간, Linux 호스트 결과 디렉터리 권한 문제를 보완했다. 관련 단위/통합 검증을 실행했다.
3. 최초 전체 실행은 192개 통과 후 SIGTERM 부하 검사에서 프로세스 종료 코드 -15로 실패했다. [최초 실패 기록](validation/initial-failure.json)을 보존한다. 호스트 중단·이미지 변경이 있었으나 원인은 확정하지 않았고, 새 기동 후 해당 검사 및 최종 전체 검증에서는 재현되지 않았다.
4. 실제 `llooggeerr` eBPF 센서·원본 저장 어댑터는 미연동이다. 수집 대상 이벤트 분모·보존율·손실 원인·센서 오버헤드는 측정 불가이며 0이나 가상 수치로 채우지 않았다.
5. 수시간 이상의 지속 시험, 물리 Linux 서버, 전원 상실·커널 패닉은 미검증이다. 30분 세션 갱신은 가상 시각 단위 검사로 검증했으며 1시간 이상 실제 실행한 것으로 표시하지 않는다.

[호스트 기록](validation/environment.json)에는 CPU·RAM·디스크·커널·Docker·Compose가 있다. BTF 파일은 존재했지만 해당 실행에서는 tracefs 이벤트 목록 접근을 확인하지 못했다. eBPF 지원·권한을 실제 센서 설치 때 별도로 검증해야 한다.
