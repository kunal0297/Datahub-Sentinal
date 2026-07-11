.PHONY: venv install lint format typecheck test test-unit test-integration \
        datahub-up datahub-down datahub-status seed seed-heal demo clean

VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip

venv:
	python3 -m venv $(VENV)
	$(PIP) install -q --upgrade pip

install: venv
	$(PIP) install -q -e ".[dev]"

lint:
	$(VENV)/bin/ruff check src tests seed
	$(VENV)/bin/ruff format --check src tests seed

format:
	$(VENV)/bin/ruff format src tests seed
	$(VENV)/bin/ruff check --fix src tests seed

typecheck:
	$(VENV)/bin/mypy src/sentinel

test-unit:
	$(VENV)/bin/pytest tests/unit -v

test-integration:
	RUN_INTEGRATION_TESTS=1 $(VENV)/bin/pytest tests/integration -v

test: test-unit

# DataHub OSS is brought up via its own maintained quickstart rather than a
# hand-rolled compose file — see ARCHITECTURE.md "DataHub API surface" for why.
datahub-up:
	$(VENV)/bin/datahub docker quickstart

datahub-down:
	$(VENV)/bin/datahub docker quickstart --stop

datahub-status:
	$(VENV)/bin/datahub docker check

seed: install
	$(PY) seed/seed_datahub.py

# Re-seed the health signals as HEALTHY — run after a failing `sentinel
# quality run` / `sentinel ml-check` pass to demo incident auto-resolution.
seed-heal: install
	$(PY) seed/seed_datahub.py --heal

demo: install datahub-up
	@echo "Waiting for DataHub GMS to become healthy..."
	$(VENV)/bin/datahub docker check
	$(MAKE) seed
	@echo ""
	@echo "DataHub is up at http://localhost:9002 (datahub/datahub) and seeded (unhealthy demo state)."
	@echo ""
	@echo "Tier 1:"
	@echo "  $(PY) -m sentinel.cli pr-impact --repo seed/sample_repo --base-ref HEAD~1"
	@echo "  $(PY) -m sentinel.cli migrate --from 'urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v1,PROD)' --to 'urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.orders_v2,PROD)' --repo seed/sample_repo"
	@echo ""
	@echo "Tier 2:"
	@echo "  $(PY) -m sentinel.cli ml-check --urn 'urn:li:dataset:(urn:li:dataPlatform:postgres,raw.orders,PROD)'"
	@echo "  $(PY) -m sentinel.cli quality run     # fails on seeded data; then: make seed-heal && re-run to auto-resolve"
	@echo "  $(PY) -m sentinel.cli enrich --urn 'urn:li:dataset:(urn:li:dataPlatform:snowflake,analytics.customer_revenue_summary,PROD)'"

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -name "__pycache__" -type d -exec rm -rf {} +
