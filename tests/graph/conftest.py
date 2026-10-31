"""Graph-test fixtures: a report on the test database, local blobs, a stub public-API transport, GraphDeps."""
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

from statbrief.auth.users import create_user
from statbrief.domain.dataset import DatasetRef
from statbrief.graph.deps import GraphDeps
from statbrief.sources.catalog import CATALOG, IndicatorSpec
from statbrief.sources.worldbank import GEO_TO_ISO3
from statbrief.storage.blobs import LocalBlobStore, blob_key
from statbrief.storage.db import bootstrap, create_pool
from statbrief.storage.repos import FileRepo, ReportRepo

FIXED_NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


def jsonstat(spec: IndicatorSpec, series: dict[str, dict[str, float]]) -> dict[str, Any]:
    """A Eurostat JSON-stat 2.0 response for one catalog spec: {geo: {period: value}}."""
    geos = sorted(series)
    times = sorted({p for values in series.values() for p in values})
    filters = sorted(spec.filters)
    ids = ["freq", *filters, "geo", "time"]
    dims = {"freq": [spec.frequency], **{k: [spec.filters[k]] for k in filters}, "geo": geos, "time": times}
    values = {str(gi * len(times) + ti): series[g][t] for gi, g in enumerate(geos) for ti, t in enumerate(times)
              if t in series[g]}
    return {"version": "2.0", "class": "dataset", "label": spec.label, "updated": "2026-10-16T23:00:00+0200",
            "id": ids, "size": [len(dims[d]) for d in ids], "value": values,
            "dimension": {d: {"category": {"index": {c: i for i, c in enumerate(dims[d])},
                                           "label": {c: c for c in dims[d]}}} for d in ids}}


def worldbank_page(spec: IndicatorSpec, series: dict[str, dict[str, float]]) -> list[Any]:
    """A one-page World Bank v2 response: {geo: {year: value}}."""
    rows = [{"indicator": {"id": spec.dataset, "value": spec.label}, "country": {"id": geo, "value": geo},
             "countryiso3code": GEO_TO_ISO3[geo], "date": year, "value": v, "unit": "", "obs_status": "",
             "decimal": 1} for geo, years in sorted(series.items()) for year, v in sorted(years.items())]
    return [{"page": 1, "pages": 1, "per_page": 1000, "total": len(rows), "lastupdated": "2026-10-08"}, rows]


@dataclass
class ApiStub:
    """Serves stub API responses by dataset code; records every request; listed datasets fail with HTTP 404."""

    payloads: dict[str, Any] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)
    requests: list[str] = field(default_factory=list)

    def serve(self, code: str, series: dict[str, dict[str, float]]) -> None:
        """Serve `series` for a catalog indicator code."""
        spec = CATALOG[code]
        self.payloads[spec.dataset] = (jsonstat(spec, series) if spec.source == "eurostat"
                                       else worldbank_page(spec, series))

    def fail(self, code: str) -> None:
        """Make requests for a catalog indicator fail."""
        self.failing.add(CATALOG[code].dataset)

    def handler(self, request: httpx.Request) -> httpx.Response:
        """MockTransport handler: the dataset is the last URL path segment."""
        self.requests.append(str(request.url))
        dataset = request.url.path.rstrip("/").split("/")[-1]
        if dataset in self.failing or dataset not in self.payloads:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, content=json.dumps(self.payloads[dataset]).encode(),
                              headers={"content-type": "application/json"})

    def client(self) -> httpx.Client:
        """An httpx client on this stub (the http_client factory of GraphDeps)."""
        return httpx.Client(transport=httpx.MockTransport(self.handler))


@dataclass
class World:
    """One user's report on the test database with blob storage, an API stub and a deps factory."""

    pool: Any
    settings: Any
    user_id: str
    other_user_id: str
    report_id: str
    blobs: LocalBlobStore
    api: ApiStub

    def add_file(self, filename: str, data: bytes, kind: str = "csv") -> DatasetRef:
        """Store an uploaded file (blob + files row) in the report."""
        fid = str(uuid.uuid4())
        key = blob_key(self.user_id, self.report_id, fid, filename)
        self.blobs.put(key, data, "application/octet-stream")
        ref = DatasetRef(file_id=fid, report_id=self.report_id, user_id=self.user_id, filename=filename, kind=kind,
                         size_bytes=len(data), sha256="test", blob_key=key)
        FileRepo(self.pool).add(ref)
        return ref

    def deps(self, *, chat_model: Any = None, retriever: Callable | None = None, **settings_overrides: Any) -> GraphDeps:
        """GraphDeps on this world (settings overrides applied to a copy)."""
        settings = self.settings.model_copy(update=settings_overrides) if settings_overrides else self.settings
        return GraphDeps.from_pool(self.pool, settings, blobs=self.blobs, chat_model=chat_model,
                                   retriever=retriever or (lambda query: []), http_client=self.api.client,
                                   now=lambda: FIXED_NOW, sleep=lambda s: None)

    def base_state(self, params: Any) -> dict[str, Any]:
        """Initial graph input for this report."""
        return {"report_id": self.report_id, "user_id": self.user_id, "params": params}


@pytest.fixture
def world(settings, tmp_path):
    """A migrated test database with two users and one report of the first, local blobs and an API stub."""
    bootstrap(settings)
    pool = create_pool(settings)
    alice = create_user(pool, "alice@example.com", "long-enough-pw", 10, 128)
    bob = create_user(pool, "bob@example.com", "long-enough-pw", 10, 128)
    report = ReportRepo(pool).create(alice.id, "Labour market Q1")
    yield World(pool=pool, settings=settings, user_id=alice.id, other_user_id=bob.id, report_id=str(report["id"]),
                blobs=LocalBlobStore(tmp_path / "blobs"), api=ApiStub())
    pool.close()
