# 네트워크 접근 표

| 출발 서비스 | 목적 서비스 | 경유 네트워크 | 용도 |
| --- | --- | --- | --- |
| 호스트 localhost | firewall | web-entry | 공개 고객 HTTP |
| firewall | web | edge-web | 허용된 요청 전달 |
| web | customer | dmz-api | 고객 API 전달 |
| customer | auth / core | customer-backend | 세션 검사·업무 |
| employee-client | employee / office | staff-net | 내부 직원 업무 |
| employee | auth / core | staff-auth / staff-core | 직원 인증·계정계 |
| office | auth | staff-auth | 직원 인증 |
| core | auth / db | customer-backend / core-db | 권한 검사·거래 저장 |
| 각 업무 서비스와 firewall | security | 두 서비스가 공유하는 기존 내부망 | 구조화 감사 기록 |

`db`는 `core-db`에만 연결됩니다. `security`에는 호스트 포트가 없고 `core-db` 접근도 없습니다. 고객 웹은 직원·DB에 직접 연결되지 않습니다. `employee-client`는 수동 관리용 `tools` 프로필이며 평상시 상시 실행하지 않습니다.

Docker의 `internal: true` 브리지는 외부 접근 경로를 줄이지만 같은 브리지에 연결된 컨테이너 사이의 방향성 규칙을 만들지 않습니다. 호스트 관리자나 Docker 관리자 권한에는 이 구성이 보안 경계가 아닙니다. 공개 `web-entry` 브리지는 호스트 포트 게시를 위해 비내부망이며, 해당 망에는 방화벽 컨테이너만 둡니다.
