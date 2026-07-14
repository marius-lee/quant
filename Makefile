.PHONY: test lint eval smoke web clean

PYTHON = .venv/bin/python3
PYTHONPATH = .

test:
	$(PYTHON) -m pytest test/ -v --tb=short

lint:
	$(PYTHON) -m flake8 quant/ --max-line-length=120 --exclude=__pycache__

eval:
	PYTHONPATH=$(PYTHONPATH) bash scripts/eval_standard.sh

smoke:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) scripts/smoke_test.py

web:
	PYTHONPATH=$(PYTHONPATH) $(PYTHON) quant/web/app.py

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete
	rm -rf .pytest_cache
