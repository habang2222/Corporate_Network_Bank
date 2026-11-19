.PHONY: up down status check staff

up:
	docker compose up -d --build

down:
	docker compose down

status:
	docker compose ps

staff:
	docker compose --profile tools up -d employee-client

check:
	docker compose config --quiet
	docker compose exec -T firewall python scripts/smoke.py customer
	docker compose --profile tools up -d employee-client
	docker compose exec -T employee-client python scripts/smoke.py staff
	docker compose exec -T security python scripts/smoke.py audit
