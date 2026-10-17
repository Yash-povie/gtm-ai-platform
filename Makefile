# Windows: psycopg needs the selector event loop, so launch uvicorn through python (Linux/Docker can use uvicorn directly).
api:
	uv run python -c "import asyncio,uvicorn; asyncio.set_event_loop_policy(getattr(asyncio,'WindowsSelectorEventLoopPolicy',asyncio.DefaultEventLoopPolicy)()); uvicorn.run('apps.api_gateway.main:app', port=8000, loop='none')"

worker:
	uv run arq apps.worker.main.WorkerSettings

.PHONY: api worker up down migrate seed test lint mcp-inspect demo-all

up:
	docker compose up -d postgres redis neo4j n8n phoenix

down:
	docker compose down

migrate:
	uv run alembic upgrade head
	uv run python scripts/apply_neo4j.py

seed:
	uv run python scripts/seed_sandbox_data.py

test:
	uv run pytest

lint:
	uv run ruff check . && uv run mypy packages systems

mcp-inspect:
	npx @modelcontextprotocol/inspector uv run python -m sys3_command_center_mcp.server

demo-all:
	uv run python scripts/demo_all.py
