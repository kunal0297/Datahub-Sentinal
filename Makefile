.PHONY: venv install lint format typecheck test test-unit test-integration \
        datahub-up datahub-down datahub-status seed demo clean

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

demo: install datahub-up
	@echo "Waiting for DataHub GMS to become healthy..."
	$(VENV)/bin/datahub docker check
	$(MAKE) seed
	@echo ""
	@echo "DataHub is up at http://localhost:9002 (datahub/datahub) and seeded."
	@echo "Try a Tier 1 feature:"
	@echo "  $(PY) -m sentinel.cli pr-impact --repo seed/sample_repo --base-ref HEAD~1"
	@echo "  $(PY) -m sentinel.cli migrate --from <old_urn> --to <new_urn> --repo seed/sample_repo"

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache .mypy_cache
	find . -name "__pycache__" -type d -exec rm -rf {} +
