from __future__ import annotations

import os
from uuid import uuid4

import pytest
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.models import DataCapability
from muscles_data.ports import SearchIndexPort
from muscles_data.runtime import DataRuntime

from muscles_data_elasticsearch import ElasticsearchSearchFactory


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("MUSCLES_DATA_INTEGRATION"), reason="backend integration is disabled"),
]


def test_elasticsearch_real_search_index_lifecycle():
    index = f"muscles-data-it-{uuid4().hex[:12]}"
    config = DataConfig.from_raw(
        {
            "data": {
                "resources": {
                    "search.elastic": {
                        "type": "elasticsearch",
                        "url_env": "ELASTICSEARCH_URL",
                        "index": index,
                        "native_client": True,
                    }
                }
            }
        }
    )
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(ElasticsearchSearchFactory())
    runtime = DataRuntime(config=config, catalog=catalog)

    client = None
    try:
        search = runtime.require_port("search.elastic", SearchIndexPort)
        assert search.upsert_documents(
            [
                {"id": "alpha", "title": "Alpha", "text": "alpha document", "metadata": {"status": "ready"}},
                {"id": "beta", "title": "Beta", "text": "beta document", "metadata": {"status": "draft"}},
            ],
            options={"refresh": "wait_for"},
        ).written == 2
        hits = search.search_text("alpha", filters={"status": "ready"}, options={"highlight": True})
        assert [hit.id for hit in hits] == ["alpha"]
        assert hits[0].highlights
        assert runtime.doctor()["status"] == "ok"
        assert search.delete_documents(ids=["alpha"], options={"refresh": "wait_for"}).deleted == 1
        assert search.search_text("alpha") == []
        contracts = pytest.importorskip("muscles_data.contracts")
        contract = getattr(contracts, "assert_search_index_contract", None)
        if contract is not None:
            contract(lambda: search)
    finally:
        try:
            if client is None:
                try:
                    client = runtime.require_resource("search.elastic", DataCapability.NATIVE_CLIENT).native_client()
                except Exception:
                    client = None
            if client is not None:
                client.options(ignore_status=404).indices.delete(index=index)
        finally:
            runtime.close()
