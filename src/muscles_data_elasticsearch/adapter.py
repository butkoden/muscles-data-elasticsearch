from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, Mapping
from urllib.parse import urlsplit

from muscles_data.config import DataResourceConfig
from muscles_data.errors import DataError
from muscles_data.models import DataCapability, HealthResult, InspectResult, SearchHit, WriteResult


_CLIENT_UNSET = object()
_RANGE_OPERATORS = {"gt", "gte", "lt", "lte"}
_ALLOWED_OPTIONS = {
    "url",
    "api_key",
    "username",
    "password",
    "basic_auth",
    "index",
    "timeout",
    "verify_certs",
    "native_client",
    "text_field",
    "metadata_field",
}


class ElasticsearchAdapterError(DataError):
    """Base error for Elasticsearch search adapter failures."""


class ElasticsearchConfigError(ValueError, ElasticsearchAdapterError):
    """Raised when an Elasticsearch resource config cannot be mapped safely."""


class ElasticsearchClientMissingError(ElasticsearchAdapterError):
    """Raised when Elasticsearch adapter is used without an available client."""


class ElasticsearchConnectionError(ElasticsearchAdapterError):
    """Raised when an Elasticsearch operation cannot reach or use the backend."""


class ElasticsearchFilterError(ElasticsearchAdapterError):
    """Raised when a data filter cannot be translated to Elasticsearch."""


class ElasticsearchSearchAdapter:
    resource_type = "elasticsearch"

    def __init__(
        self,
        config: DataResourceConfig,
        *,
        client_factory: Callable[[DataResourceConfig], Any] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._client: Any = _CLIENT_UNSET
        self._lock = threading.RLock()
        self.closed = False

    def search_text(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        limit: int = 10,
        options: Mapping[str, Any] | None = None,
    ) -> list[SearchHit]:
        options = dict(options or {})
        kwargs: dict[str, Any] = {
            "index": self.index_name(),
            "query": self._query(query, filters),
            "size": max(0, int(limit)),
        }
        if options.pop("highlight", False):
            fields = options.pop("highlight_fields", [self.text_field()])
            kwargs["highlight"] = {"fields": {str(field): {} for field in fields}}
        for key in ("from_", "sort", "track_total_hits", "routing", "preference", "search_after"):
            if key in options:
                kwargs[key] = options[key]
        try:
            response = self._client_instance().search(**kwargs)
        except (ElasticsearchClientMissingError, ElasticsearchConfigError, ElasticsearchFilterError):
            raise
        except Exception as exc:
            raise ElasticsearchConnectionError(self._safe_error(exc)) from exc
        return [
            _hit_from_response(hit, text_field=self.text_field(), metadata_field=self.metadata_field())
            for hit in _hits(response)
        ]

    def upsert_documents(self, items: list[Mapping[str, Any]], options: Mapping[str, Any] | None = None) -> WriteResult:
        options = dict(options or {})
        client = self._client_instance()
        try:
            for item in items:
                document_id = str(item["id"])
                kwargs: dict[str, Any] = {
                    "index": self.index_name(),
                    "id": document_id,
                    "document": self._document_from_item(item),
                }
                for key in ("refresh", "routing", "pipeline"):
                    if key in options:
                        kwargs[key] = options[key]
                client.index(**kwargs)
        except KeyError as exc:
            raise ElasticsearchConfigError(f"Elasticsearch document item requires {exc.args[0]}") from exc
        except (ElasticsearchClientMissingError, ElasticsearchConfigError, ElasticsearchFilterError):
            raise
        except Exception as exc:
            raise ElasticsearchConnectionError(self._safe_error(exc)) from exc
        return WriteResult(written=len(items), matched=len(items))

    def delete_documents(
        self,
        ids: list[str] | None = None,
        filters: Mapping[str, Any] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> WriteResult:
        options = dict(options or {})
        if ids is None and not filters:
            return WriteResult()
        client = self._client_instance()
        try:
            if ids is not None:
                deleted = 0
                for document_id in ids:
                    kwargs: dict[str, Any] = {"index": self.index_name(), "id": str(document_id)}
                    for key in ("refresh", "routing"):
                        if key in options:
                            kwargs[key] = options[key]
                    client.delete(**kwargs)
                    deleted += 1
                return WriteResult(deleted=deleted, matched=deleted)
            query = self._filter_query(filters)
            kwargs = {"index": self.index_name(), "query": query}
            for key in ("refresh", "routing", "conflicts"):
                if key in options:
                    kwargs[key] = options[key]
            response = client.delete_by_query(**kwargs)
        except (ElasticsearchClientMissingError, ElasticsearchConfigError, ElasticsearchFilterError):
            raise
        except Exception as exc:
            raise ElasticsearchConnectionError(self._safe_error(exc)) from exc
        deleted = int(_mapping_get(response, "deleted", 0) or 0)
        return WriteResult(deleted=deleted, matched=deleted)

    def inspect(self) -> dict[str, Any]:
        return asdict(
            InspectResult(
                name=self.config.name,
                type=self.config.type,
                capabilities=[],
                initialized=self._client is not _CLIENT_UNSET,
                status="ok",
                options=self.config.safe_options(),
                details={"backend": "elasticsearch", "index": self.index_name()},
            )
        )

    def health(self) -> HealthResult:
        try:
            client = self._client_instance()
            ping = getattr(client, "ping", None)
            if callable(ping) and not bool(ping()):
                return HealthResult(
                    status="failed",
                    message="Elasticsearch ping failed",
                    details={"index": self.index_name()},
                )
            indices = getattr(client, "indices", None)
            exists = True
            if indices is not None and hasattr(indices, "exists"):
                exists = bool(indices.exists(index=self.index_name()))
        except Exception as exc:
            return HealthResult(status="failed", message=self._safe_error(exc), details={"index": self.index_name()})
        if not exists:
            return HealthResult(
                status="failed",
                message=f"Elasticsearch index '{self.index_name()}' is not available",
                details={"index": self.index_name()},
            )
        return HealthResult(status="ok", message="Elasticsearch index is available", details={"index": self.index_name()})

    def close(self) -> None:
        if self._client is _CLIENT_UNSET:
            self.closed = True
            return
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
        self.closed = True

    def native_client(self):
        return self._client_instance()

    def index_name(self) -> str:
        return str(self.config.options["index"])

    def text_field(self) -> str:
        return str(self.config.options.get("text_field", "text"))

    def metadata_field(self) -> str:
        return str(self.config.options.get("metadata_field", "metadata"))

    def _query(self, query: str, filters: Mapping[str, Any] | None) -> dict[str, Any]:
        clauses: dict[str, Any] = {}
        if str(query).strip():
            clauses["must"] = [{"match": {self.text_field(): {"query": str(query)}}}]
        else:
            clauses["must"] = [{"match_all": {}}]
        filter_clauses = elasticsearch_filter_from_mapping(filters or {}, field_prefix=self.metadata_field())
        if filter_clauses:
            clauses["filter"] = filter_clauses
        return {"bool": clauses}

    def _filter_query(self, filters: Mapping[str, Any] | None) -> dict[str, Any]:
        filter_clauses = elasticsearch_filter_from_mapping(filters or {}, field_prefix=self.metadata_field())
        return {"bool": {"filter": filter_clauses}} if filter_clauses else {"match_all": {}}

    def _document_from_item(self, item: Mapping[str, Any]) -> dict[str, Any]:
        metadata = dict(item.get("payload", {}) or {})
        metadata.update(dict(item.get("metadata", {}) or {}))
        document = {
            self.text_field(): str(item.get("text", "")),
            self.metadata_field(): metadata,
        }
        fields = item.get("fields")
        if isinstance(fields, Mapping):
            document.update(dict(fields))
        return document

    def _client_instance(self):
        if self._client is _CLIENT_UNSET:
            with self._lock:
                if self._client is _CLIENT_UNSET:
                    self._validate_options()
                    client = (
                        self._client_factory(self.config)
                        if self._client_factory
                        else _default_elasticsearch_client(self.config)
                    )
                    if client is None:
                        raise ElasticsearchClientMissingError("Elasticsearch client is not available")
                    self._client = client
        return self._client

    def _validate_options(self) -> None:
        unknown = sorted(set(self.config.options) - _ALLOWED_OPTIONS)
        if unknown:
            names = ", ".join(unknown)
            raise ElasticsearchConfigError(f"Unsupported Elasticsearch resource options: {names}")
        basic_auth = self.config.options.get("basic_auth")
        if basic_auth is not None and (not isinstance(basic_auth, (list, tuple)) or len(basic_auth) != 2):
            raise ElasticsearchConfigError("Elasticsearch basic_auth must contain username and password")

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        for key, value in self.config.options.items():
            if key in {"url", "api_key", "password", "basic_auth"} and value:
                if isinstance(value, (list, tuple)):
                    for item in value:
                        message = message.replace(str(item), "***")
                else:
                    message = message.replace(str(value), "***")
                    if key == "url":
                        parsed = urlsplit(str(value))
                        for item in (parsed.username, parsed.password):
                            if item:
                                message = message.replace(str(item), "***")
        return message


class ElasticsearchSearchFactory:
    resource_type = "elasticsearch"

    def __init__(self, *, client_factory: Callable[[DataResourceConfig], Any] | None = None) -> None:
        self._client_factory = client_factory

    def capabilities(self, config: DataResourceConfig) -> set[DataCapability]:
        native = {DataCapability.NATIVE_CLIENT} if bool(config.options.get("native_client")) else set()
        return {DataCapability.KEYWORD_SEARCH, DataCapability.DOCUMENT_INDEX, DataCapability.HEALTHCHECK} | native

    def create(self, config: DataResourceConfig) -> ElasticsearchSearchAdapter:
        return ElasticsearchSearchAdapter(config, client_factory=self._client_factory)


def elasticsearch_filter_from_mapping(
    filters: Mapping[str, Any],
    *,
    field_prefix: str | None = "metadata",
) -> list[dict[str, Any]]:
    clauses: list[dict[str, Any]] = []
    for key in sorted(filters, key=_filter_sort_key):
        value = filters[key]
        if key == "$and":
            clauses.append({"bool": {"filter": _logical_conditions(value, field_prefix=field_prefix)}})
        elif key == "$or":
            clauses.append(
                {
                    "bool": {
                        "should": _logical_conditions(value, field_prefix=field_prefix),
                        "minimum_should_match": 1,
                    }
                }
            )
        elif key == "$not":
            clauses.append({"bool": {"must_not": _logical_conditions(value, field_prefix=field_prefix)}})
        else:
            clauses.append(_field_condition(str(key), value, field_prefix=field_prefix))
    return clauses


def _logical_conditions(value: Any, *, field_prefix: str | None) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        return elasticsearch_filter_from_mapping(value, field_prefix=field_prefix)
    if isinstance(value, list):
        conditions: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, Mapping):
                raise ElasticsearchFilterError("Elasticsearch logical filter items must be mappings")
            conditions.extend(elasticsearch_filter_from_mapping(item, field_prefix=field_prefix))
        return conditions
    raise ElasticsearchFilterError("Elasticsearch logical filters must be mappings or lists of mappings")


def _field_condition(key: str, value: Any, *, field_prefix: str | None) -> dict[str, Any]:
    field = _field_name(key, field_prefix)
    if isinstance(value, Mapping):
        operators = set(value)
        if operators <= _RANGE_OPERATORS:
            return {"range": {field: dict(value)}}
        if operators == {"eq"}:
            return {"term": {field: value["eq"]}}
        if operators == {"any"}:
            return {"terms": {field: list(value["any"])}}
        unsupported = ", ".join(sorted(str(operator) for operator in operators))
        raise ElasticsearchFilterError(f"Unsupported Elasticsearch filter operator for '{key}': {unsupported}")
    if isinstance(value, (list, tuple, set)):
        return {"terms": {field: list(value)}}
    return {"term": {field: value}}


def _field_name(key: str, field_prefix: str | None) -> str:
    if not field_prefix or "." in key:
        return key
    return f"{field_prefix}.{key}"


def _filter_sort_key(key: Any) -> tuple[int, str]:
    order = {"$and": 0, "$or": 1, "$not": 2}
    key_text = str(key)
    return (order.get(key_text, 10), key_text)


def _hit_from_response(hit: Mapping[str, Any], *, text_field: str, metadata_field: str) -> SearchHit:
    source = dict(hit.get("_source", {}) or {})
    metadata = dict(source.get(metadata_field, {}) or {})
    highlights = {
        str(key): [str(item) for item in value]
        for key, value in dict(hit.get("highlight", {}) or {}).items()
    }
    return SearchHit(
        id=str(hit.get("_id", "")),
        score=float(hit.get("_score", 0.0) or 0.0),
        text=source.get(text_field),
        metadata=metadata,
        highlights=highlights,
    )


def _hits(response: Any) -> list[Mapping[str, Any]]:
    return list(_mapping_get(_mapping_get(response, "hits", {}), "hits", []) or [])


def _mapping_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, default)
    try:
        return value[key]
    except Exception:
        return default


def _default_elasticsearch_client(config: DataResourceConfig):
    try:
        client_module = importlib.import_module("elasticsearch")
    except Exception as exc:
        raise ElasticsearchClientMissingError("Install elasticsearch or muscles-data-elasticsearch to use type=elasticsearch") from exc

    kwargs: dict[str, Any] = {}
    if config.options.get("api_key"):
        kwargs["api_key"] = config.options["api_key"]
    if config.options.get("basic_auth"):
        kwargs["basic_auth"] = tuple(config.options["basic_auth"])
    elif config.options.get("username") or config.options.get("password"):
        kwargs["basic_auth"] = (config.options.get("username"), config.options.get("password"))
    if config.options.get("timeout") is not None:
        kwargs["request_timeout"] = config.options["timeout"]
    if config.options.get("verify_certs") is not None:
        kwargs["verify_certs"] = bool(config.options["verify_certs"])

    try:
        return client_module.Elasticsearch(config.options["url"], **kwargs)
    except Exception as exc:
        raise ElasticsearchConnectionError(str(exc)) from exc
