# House Finances — public demo.
#
# One target matters: `make demo`. It brings up Postgres, applies the single
# baseline migration, seeds a fictional household, and starts the app. No .env
# to edit first: docker/docker-compose.yml carries demo-safe defaults for every
# variable the app needs to boot, including AUTH_SECRET.
#
# Port 8080 by default; override when something already owns it:
#     make demo DEMO_PORT=8003
# Only `make demo` needs it. The stack is identified by the project name in
# docker/docker-compose.yml, so demo-down/logs/psql find it either way.

DEMO_PORT ?= 8080
export DEMO_PORT

COMPOSE := docker compose -f docker/docker-compose.yml

.PHONY: demo demo-down logs psql test

## Clone to a populated UI. Safe to re-run: it stops at the seeder if the
## database already has data, which is the seeder refusing to touch a ledger.
demo:
	$(COMPOSE) build app
	$(COMPOSE) up -d --wait db
	$(COMPOSE) run --rm -T app alembic upgrade head
	$(COMPOSE) run --rm -T app python scripts/seed_demo.py
	$(COMPOSE) up -d --wait app
	@echo ""
	@echo "  House Finances demo is up:  http://localhost:$(DEMO_PORT)"
	@echo "  Log in with  alex@example.com  /  demo1234"
	@echo ""

## Containers AND the database volume, so the next `make demo` starts clean.
demo-down:
	$(COMPOSE) down -v

logs:
	$(COMPOSE) logs app --tail 100

psql:
	$(COMPOSE) exec db psql -U finances -d finances

## Needs `make demo` first. Runs the suite inside the app image against a
## SCRATCH database beside the demo one: the fixture refuses any database whose
## name does not end in _test, so a mispasted URL cannot drop a real schema.
## The tree is mounted rather than baked in, because the image excludes tests/.
test:
	-$(COMPOSE) exec -T db createdb -U finances finances_demo_test
	$(COMPOSE) run --rm -T \
	  -e DATABASE_URL=postgresql+psycopg://finances:finances@db:5432/finances_demo_test \
	  -v $$(pwd)/backend:/work -w /work \
	  app sh -c "pip install --quiet --user -r requirements-dev.txt \
	             && python -m pytest tests/ -q -p no:cacheprovider"
