from __future__ import annotations

import pytest
from muscles_data.catalog import DataAdapterCatalog
from muscles_data.config import DataConfig
from muscles_data.models import DataCapability
from muscles_data.ports import SearchIndexPort
from muscles_data.runtime import DataRuntime

from muscles_data_elasticsearch import (
    ElasticsearchClientMissingError,
    ElasticsearchConnectionError,
    ElasticsearchFilterError,
    ElasticsearchSearchFactory,
    elasticsearch_filter_from_mapping,
)


class FakeIndices:
    def __init__(self, client: "FakeElasticsearchClient") -> None:
        self.client = client

    def exists(self, *, index: str) -> bool:
        self.client.index_checks.append(index)
        if self.client.fail_health:
            raise TimeoutError("elastic password=secret timed out")
        return index == "docs"


class FakeElasticsearchClient:
    def __init__(self, *, fail_health: bool = False) -> None:
        self.fail_health = fail_health
        self.searches: list[dict] = []
        self.indexes: list[dict] = []
        self.deletes: list[dict] = []
        self.delete_queries: list[dict] = []
        self.index_checks: list[str] = []
        self.closed = False
        self.indices = FakeIndices(self)

    def search(self, **kwargs):
        self.searches.append(kwargs)
        return {
            "hits": {
                "hits": [
                    {
                        "_id": "doc-1",
                        "_score": 4.2,
                        "_source": {"text": "Muscles data ports", "metadata": {"section": "docs"}},
                        "highlight": {"text": ["<em>Muscles</em> data ports"]},
                    }
                ]
            }
        }

    def index(self, **kwargs):
        self.indexes.append(kwargs)
        return {"result": "created"}

    def delete(self, **kwargs):
        self.deletes.append(kwargs)
        return {"result": "deleted"}

    def delete_by_query(self, **kwargs):
        self.delete_queries.append(kwargs)
        return {"deleted": 3}

    def ping(self) -> bool:
        if self.fail_health:
            raise TimeoutError("elastic password=secret timed out")
        return True

    def close(self) -> None:
        self.closed = True


def _config(url: str = "https://elastic.example") -> dict:
    return {
        "data": {
            "resources": {
                "search.elastic": {
                    "type": "elasticsearch",
                    "url": url,
                    "api_key": "elastic-secret",
                    "index": "docs",
                    "native_client": True,
                }
            }
        }
    }


def _runtime(client: FakeElasticsearchClient | None, url: str = "https://elastic.example") -> DataRuntime:
    catalog = DataAdapterCatalog.with_defaults()
    catalog.register(ElasticsearchSearchFactory(client_factory=lambda _config: client))
    return DataRuntime(config=DataConfig.from_raw(_config(url)), catalog=catalog)


def test_elasticsearch_external_adapter_maps_search_index_delete_and_native_access():
    client = FakeElasticsearchClient()
    runtime = _runtime(client)

    listed = runtime.list_resources()[0]
    assert listed["type"] == "elasticsearch"
    assert {"keyword_search", "document_index"} <= set(listed["capabilities"])
    assert listed["initialized"] is False

    search = runtime.require_port("search.elastic", SearchIndexPort)
    hits = search.search_text("muscles", filters={"section": "docs"}, limit=2, options={"highlight": True})
    write = search.upsert_documents([{"id": "doc-1", "text": "Muscles data ports", "metadata": {"section": "docs"}}])
    deleted = search.delete_documents(filters={"section": ["docs", "notes"]})

    assert [hit.id for hit in hits] == ["doc-1"]
    assert hits[0].highlights["text"] == ["<em>Muscles</em> data ports"]
    assert client.searches[0]["query"]["bool"]["filter"] == [{"term": {"metadata.section": "docs"}}]
    assert write.written == 1
    assert client.indexes[0]["document"]["metadata"] == {"section": "docs"}
    assert deleted.deleted == 3
    assert client.delete_queries[0]["query"]["bool"]["filter"][0] == {"terms": {"metadata.section": ["docs", "notes"]}}
    assert runtime.require_resource("search.elastic", DataCapability.NATIVE_CLIENT).native_client() is client
    assert runtime.doctor()["status"] == "ok"
    assert client.index_checks == ["docs"]
    assert runtime.close()["status"] == "ok"
    assert client.closed is True


def test_elasticsearch_external_adapter_filters_and_safe_failures():
    translated = elasticsearch_filter_from_mapping({"score": {"gte": 0.5}, "$not": {"archived": True}})
    assert translated[0] == {"bool": {"must_not": [{"term": {"metadata.archived": True}}]}}
    assert translated[1] == {"range": {"metadata.score": {"gte": 0.5}}}
    with pytest.raises(ElasticsearchFilterError):
        elasticsearch_filter_from_mapping({"score": {"near": 1.0}})

    with pytest.raises(ElasticsearchClientMissingError):
        _runtime(None).require_port("search.elastic", SearchIndexPort).search_text("x")

    failing = _runtime(FakeElasticsearchClient(fail_health=True), "https://user:secret@elastic.example").doctor()
    assert failing["status"] == "failed"
    assert "secret" not in repr(failing)

    bad_client = FakeElasticsearchClient()
    bad_client.search = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("network unavailable"))
    with pytest.raises(ElasticsearchConnectionError):
        _runtime(bad_client).require_port("search.elastic", SearchIndexPort).search_text("x")
