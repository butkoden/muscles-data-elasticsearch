# `muscles-data-elasticsearch` RC checklist

The package ships the Elasticsearch implementation of `SearchIndexPort`.
The dependency on `muscles-data` is versioned as `>=0.1.0,<1.0.0`.

Before publishing a GitHub Release, run:

```bash
PYTHONPATH=../muscles-data/src:src python -m pytest -q
python -m build --wheel --sdist
```

The integration scenario is enabled with `MUSCLES_DATA_INTEGRATION=1` and a
running Elasticsearch service configured through `ELASTICSEARCH_URL`.
Credentials and endpoint details must stay out of diagnostics and telemetry.

The PyPI workflow publishes only after a GitHub Release is published. It uses
the versioned `muscles-data` dependency and trusted publishing.
