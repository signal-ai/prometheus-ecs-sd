.PHONY: format
format:
	poetry run black .

.PHONY: dev-start
dev-start:
	rm -rf ./targets
	@mkdir -p ./targets
	poetry run python discoverecs.py --directory $$PWD/targets --default-scrape-interval-prefix default --tags-to-labels "*"
