.PHONY: up down logs test demo dashboard ps

up:            ## start the whole stack
	@test -f .env || cp .env.example .env
	docker compose up -d --build

down:          ## stop everything (keeps data)
	docker compose down

logs:          ## follow engine + simulator logs
	docker compose logs -f engine simulator

ps:
	docker compose ps

test:          ## run unit tests
	cd engine && python -m pytest -q

demo:          ## run the fault-injection walkthrough
	./scripts/demo.sh

dashboard:     ## regenerate the Grafana dashboard JSON
	python scripts/build_dashboard.py
