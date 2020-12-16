.PHONY: format
format:
	poetry run black .

.PHONY: dev-start
dev-start:
	@mkdir -p ./targets
	poetry run python discoverecs.py --directory $$PWD/targets
