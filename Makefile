.PHONY: build fetch extract app test eval
build:            ## load xlsx, chunk, index, revisions (offline)
	python -m ingest.load && python -m ingest.chunk && python -m ingest.revisions
fetch:            ## download Federal Register full text (needs internet)
	python -m ingest.fetch
extract:          ## applicability rules (LLM if ANTHROPIC_API_KEY set, else regex)
	python -m rules.extract
app:
	streamlit run app/streamlit_app.py
test:
	python -m pytest -q tests
eval:
	python -m eval.run
