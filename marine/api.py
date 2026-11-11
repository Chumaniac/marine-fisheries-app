"""Authenticated API and local web host. Demo data is opt-in and explicitly labelled."""

import json
import math
import os
import secrets
import time
from collections import Counter
from contextlib import asynccontextmanager
import asyncio
from marine.lifecycle import ForecastService
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, Query
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBearer
from starlette.middleware.gzip import GZipMiddleware
from jsonschema import Draft202012Validator, FormatChecker
from pydantic import BaseModel, ConfigDict, Field, field_validator

from marine.forecast import FEATURES
from marine import __version__
from marine.storage import Store, canonical, digest, CAPACITIES

ROOT = Path(__file__).resolve().parents[1]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EventBatch(Strict):
    events: list[dict] = Field(min_length=1, max_length=500)


class Label(Strict):
    siteId: str = Field(default="synthetic-area-A", min_length=1, max_length=128)
    speciesId: str = Field(default="saithe", min_length=1, max_length=128)
    id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    originAt: float
    labelAvailableAt: float
    cpue: float = Field(ge=0, le=100000)
    quality: float = Field(ge=0, le=1, default=1)
    features: dict[str, float]
    sourceEventIds: list[str] = Field(default_factory=list, max_length=500)
    dataSource: str = Field(pattern=r"^(synthetic|field)$")

    @field_validator("features")
    @classmethod
    def features_valid(cls, value):
        if set(value) != set(FEATURES) or not all(math.isfinite(v) for v in value.values()):
            raise ValueError("Invalid features")
        return value


class LabelBatch(Strict):
    labels: list[Label] = Field(min_length=1, max_length=1000)


def create_app(data_dir=None, mode=None):
    if mode is None:
        mode = os.getenv("MARINE_MODE", "local")
        if mode not in {"local", "production"}:
            raise RuntimeError("Unknown runtime mode")
    if mode == "production":
        from marine.config import require_production

        require_production()

    @asynccontextmanager
    async def lifespan(app):
        from marine.writer_lease import WriterLease

        lease = WriterLease(folder) if app.state.mode == "production" else None
        stopping = asyncio.Event()

        async def refresh():
            while not stopping.is_set():
                try:
                    from marine.operations import maintenance

                    await asyncio.to_thread(maintenance, app)
                except Exception:
                    app.state.scheduler_error = True
                    try:
                        app.state.operations.heartbeat("maintenance", False)
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass

        task = asyncio.create_task(refresh())
        try:
            yield
        finally:
            stopping.set()
            try:
                await task
            finally:
                if lease:
                    lease.close()

    app = FastAPI(
        lifespan=lifespan,
        title="Marine Fisheries API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
    )
    app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)
    folder = Path(data_dir or os.getenv("MARINE_DATA_DIR", "artifacts/database")).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    store = Store(folder / "marine.sqlite3")
    key_path = os.getenv("MARINE_JWT_KEY_FILE")
    from marine.identity import ExternalIdentity, register_devices, check_device

    external_identity = ExternalIdentity() if mode != "local" else None
    signing_key = Path(key_path).read_bytes() if key_path else secrets.token_bytes(48)
    if len(signing_key) < 32:
        raise RuntimeError("Signing key too short")
    app.state.forecasts = ForecastService(store, bool(os.getenv("KAFKA_BOOTSTRAP")))
    app.state.store = store
    from marine.operations import Operations

    app.state.operations = Operations(store)
    app.state.mode = mode
    app.state.classifier = None
    if mode == "production":
        from marine.inference import Classifier

        app.state.classifier = Classifier(os.environ["MARINE_MODEL_DIR"])
    from marine.monitoring import Monitor

    app.state.monitor = Monitor(store)
    sites = []
    if os.getenv("MARINE_SITES_FILE"):
        sites = json.loads(Path(os.environ["MARINE_SITES_FILE"]).read_text())
        if not isinstance(sites, list) or len(sites) > 1000:
            raise ValueError("Invalid site registry")
        for site in sites:
            if (
                not -180 <= float(site["longitude"]) <= 180
                or not -90 <= float(site["latitude"]) <= 90
            ):
                raise ValueError("Invalid site coordinate")
    validator = Draft202012Validator(
        json.loads((ROOT / "contracts/events.schema.json").read_text()),
        format_checker=FormatChecker(),
    )

    @app.middleware("http")
    async def bounded_request(request, call_next):
        # Reject DNS rebinding in local mode; do not trust forwarded client/host headers.
        host = request.url.hostname
        if mode == "local" and host not in {"127.0.0.1", "localhost", "testserver"}:
            return JSONResponse({"code": "HOST_REJECTED"}, status_code=403)
        if mode == "production" and host != urlsplit(os.environ["MARINE_PUBLIC_URL"]).hostname:
            return JSONResponse({"code": "HOST_REJECTED"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != request.url.netloc:
            return JSONResponse({"code": "ORIGIN_REJECTED"}, status_code=403)
        if request.method in {"POST", "PUT", "PATCH"}:
            limit = (
                8 * 1024 * 1024 + 65536 if request.url.path == "/v1/classifications" else 512 * 1024
            )
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return JSONResponse({"code": "INVALID_LENGTH"}, status_code=400)
            if length > limit:
                return JSONResponse({"code": "PAYLOAD_TOO_LARGE"}, status_code=413)
            # Read incrementally to enforce limit even without Content-Length.
            chunks = []
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    return JSONResponse({"code": "PAYLOAD_TOO_LARGE"}, status_code=413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/v1"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        code = str(error)
        return JSONResponse(
            {
                "code": code
                if code
                in {
                    "IDEMPOTENCY_CONFLICT",
                    "REVISION_CONFLICT",
                    "INVALID_STATE",
                    "MASS_CONSERVATION",
                    "INGEST_CAPACITY_REACHED",
                }
                else "INVALID_INPUT",
                "message": "Input validation or state conflict",
                "retryable": False,
            },
            status_code=429
            if code == "INGEST_CAPACITY_REACHED"
            else 409
            if "CONFLICT" in code
            else 422,
        )

    @app.exception_handler(KeyError)
    async def not_found(request, error):
        return JSONResponse({"code": "NOT_FOUND"}, status_code=404)

    def actor_view(claims):
        return {
            "tenant": claims["tenant"],
            "subject": claims["sub"],
            "role": claims["role"],
            "organization": claims.get("organization", "Org1MSP"),
            "expiresAt": claims["exp"],
            "mode": mode,
            "scopeHash": digest(
                {
                    "tenant": claims["tenant"],
                    "subject": claims["sub"],
                    "role": claims["role"],
                    "organization": claims.get("organization", "Org1MSP"),
                    "policyVersion": 2,
                }
            ),
        }

    from marine.browser_login import register_login

    browser_login = register_login(app, store, external_identity)

    bearer = HTTPBearer(auto_error=False)

    def identity(request: Request, credentials=Depends(bearer)):
        token = credentials.credentials if credentials else request.cookies.get("marine_session")
        try:
            claims = (
                browser_login.decode(request.cookies.get("marine_browser"))
                if external_identity and not credentials and browser_login
                else external_identity.decode(token)
                if external_identity
                else jwt.decode(
                    token,
                    signing_key,
                    algorithms=["HS256"],
                    audience="marine-api",
                    issuer="marine-local",
                    options={"require": ["exp", "iat", "sub", "tenant", "role"]},
                )
            )
        except (jwt.PyJWTError, ValueError, KeyError, OSError, TypeError):
            raise HTTPException(401, "Authentication required") from None
        if claims["role"] not in {"viewer", "operator", "auditor", "device"}:
            raise HTTPException(403, "Role rejected")
        supplied_scope = request.headers.get("x-marine-scope")
        expected_scope = actor_view(claims)["scopeHash"]
        if supplied_scope and supplied_scope != expected_scope:
            raise HTTPException(403, "Session scope changed")
        if (
            credentials is None
            and request.method not in {"GET", "HEAD", "OPTIONS"}
            and supplied_scope != expected_scope
        ):
            raise HTTPException(403, "Cookie write requires a bound session scope")
        if (
            claims["role"] == "device"
            and request.url.path not in {"/v1/me", "/v1/event-batches", "/v1/edge-samples"}
            and not (request.method == "GET" and request.url.path.startswith("/v1/edge-samples/"))
        ):
            raise HTTPException(403, "Device endpoint denied")
        return claims

    @app.get("/v1/me")
    def me(claims=Depends(identity)):
        return actor_view(claims)

    def operator(claims=Depends(identity)):
        if claims["role"] != "operator":
            raise HTTPException(403, "Operator required")
        return claims

    @app.get("/health")
    def health():
        return {
            "status": "degraded" if getattr(app.state, "scheduler_error", False) else "ok",
            "forecastSchedulerHealthy": not getattr(app.state, "scheduler_error", False),
            "mode": mode,
            "storage": "sqlite-local",
            "streamTransport": "kafka-outbox"
            if os.getenv("KAFKA_BOOTSTRAP")
            else "local-persisted",
            "realWorldValidated": False,
        }

    @app.post("/v1/session")
    def session(
        request: Request,
        organization: str = Query(default="Org1MSP", pattern=r"^(Org1MSP|Org2MSP)$"),
    ):
        if (
            mode != "local"
            or request.client.host not in {"127.0.0.1", "::1", "testclient"}
            or request.headers.get("x-requested-with") != "marine-web"
        ):
            raise HTTPException(403, "Local demo session unavailable")
        now = int(time.time())
        claims = {
            "sub": "local-operator",
            "organization": organization,
            "tenant": "demo",
            "role": "operator",
            "iss": "marine-local",
            "aud": "marine-api",
            "iat": now,
            "exp": now + 3600,
        }
        token = jwt.encode(claims, signing_key, algorithm="HS256")
        response = JSONResponse(actor_view(claims))
        response.set_cookie(
            "marine_session",
            token,
            httponly=True,
            samesite="strict",
            max_age=3600,
            secure=request.url.scheme == "https",
        )
        return response

    @app.delete("/v1/session")
    def logout(request: Request):
        if browser_login:
            browser_login.logout(request.cookies.get("marine_browser"))
        response = JSONResponse({"ok": True})
        response.delete_cookie("marine_session")
        response.delete_cookie("marine_browser", secure=True, httponly=True, samesite="strict")
        return response

    register_devices(app, store, operator)

    def producer(claims=Depends(identity)):
        if claims["role"] not in {"operator", "device"}:
            raise HTTPException(403, "Producer required")
        return claims

    @app.post("/v1/event-batches", status_code=202)
    def ingest(body: EventBatch, claims=Depends(producer)):
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        normalized = []
        for event in body.events:
            if event.get("tenantId") != claims["tenant"]:
                raise HTTPException(403, "Tenant mismatch")
            if event.get("eventType") not in {"fish.observation", "environment.reading"}:
                raise HTTPException(403, "Clients cannot publish model or ledger confirmations")
            check_device(store, claims, event, mode != "local")
            event = {**event, "receivedAt": now}
            errors = list(validator.iter_errors(event))
            if errors or len(canonical(event)) > 8192:
                raise HTTPException(422, "Event schema rejected")
            at = datetime.fromisoformat(event["occurredAt"].replace("Z", "+00:00")).timestamp()
            if at > time.time() + 30:
                raise HTTPException(422, "Future event time")
            normalized.append(event)
        accepted = store.accept(claims["tenant"], normalized)
        return {
            "acceptedEventIds": accepted,
            "status": "persisted",
            "kafkaStatus": "pending" if os.getenv("KAFKA_BOOTSTRAP") else "not_configured",
        }

    @app.get("/v1/data-quality")
    def data_quality(
        siteId: str = Query(default="synthetic-area-A", min_length=1, max_length=128),
        speciesId: str = Query(default="saithe", min_length=1, max_length=128),
        claims=Depends(identity),
    ):
        from marine.data_quality import check_source_data

        return JSONResponse(check_source_data(store, validator, claims["tenant"], siteId, speciesId),
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/labels", status_code=201)
    def labels(body: LabelBatch, claims=Depends(operator)):
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for label in body.labels:
                if not label.originAt < label.labelAvailableAt <= time.time():
                    raise HTTPException(422, "Label must be mature and later than origin")
                if label.dataSource == "field" and not label.sourceEventIds:
                    raise HTTPException(422, "Field labels require sourceEventIds")
                for event_id in label.sourceEventIds:
                    source = db.execute(
                        "SELECT payload FROM events WHERE tenant=? AND id=?",
                        (claims["tenant"], event_id),
                    ).fetchone()
                    if not source or json.loads(source[0])["siteId"] != label.siteId:
                        raise HTTPException(422, "Unknown or mismatched source event")
                    source_event = json.loads(source[0])
                    if (
                        datetime.fromisoformat(
                            source_event["occurredAt"].replace("Z", "+00:00")
                        ).timestamp()
                        > label.originAt
                    ):
                        raise HTTPException(422, "Forecast features cannot use future observations")
                payload = canonical(label.model_dump()).decode()
                old = db.execute(
                    "SELECT payload FROM labels WHERE tenant=? AND id=?",
                    (claims["tenant"], label.id),
                ).fetchone()
                if old and old["payload"] != payload:
                    raise ValueError("IDEMPOTENCY_CONFLICT")
                if (
                    not old
                    and db.execute(
                        "SELECT count(*) FROM labels WHERE tenant=?", (claims["tenant"],)
                    ).fetchone()[0]
                    >= CAPACITIES['labels']
                ):
                    raise HTTPException(
                        429, "Label capacity reached; archive a training snapshot first"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO labels VALUES(?,?,?)",
                    (claims["tenant"], label.id, payload),
                )
        return {"count": len(body.labels)}

    @app.post("/v1/forecast-models")
    def train_regression(
        siteId: str = Query(default="synthetic-area-A", max_length=128),
        speciesId: str = Query(default="saithe", max_length=128),
        claims=Depends(operator),
    ):
        payload = app.state.forecasts.train(claims["tenant"], siteId, speciesId)
        app.state.forecasts.publish(claims["tenant"], siteId, speciesId)
        return {k: v for k, v in payload.items() if k != "model"}

    @app.post("/v1/forecast-models/{version}/promote")
    def promote(version: str, claims=Depends(operator)):
        return app.state.forecasts.promote(claims["tenant"], version)

    from marine.workbench import register_workbench, registered_sites

    register_workbench(
        app, store, identity, operator, ingest, EventBatch, labels, LabelBatch, folder
    )

    from marine.collection_orders import register_collections

    register_collections(app, store, identity, operator, external_identity, validator)

    @app.get("/v1/dashboard")
    def dashboard(
        siteId: str = Query(default="synthetic-area-A", max_length=128),
        speciesId: str = Query(default="saithe", max_length=128),
        batchAfter: str = Query(default="", max_length=128),
        batchBefore: str = Query(default="", max_length=128),
        claims=Depends(identity),
    ):
        if batchAfter and batchBefore:
            raise HTTPException(422, "Choose one batch cursor")
        now = time.time()
        metrics = store.dashboard_metrics(claims["tenant"], siteId, speciesId, now)
        env = metrics["environment"]
        if env and env["sensorQuality"] != "valid":
            env = None
        with store.connect() as db:
            windows = [
                json.loads(row["payload"])
                for row in db.execute(
                    "SELECT payload FROM windows WHERE tenant=? "
                    "AND json_extract(payload,'$.siteId')=? "
                    "AND json_extract(payload,'$.speciesId')=? ORDER BY json_extract(payload,'$.windowStart') DESC,id DESC LIMIT 1000",
                    (claims["tenant"], siteId, speciesId),
                )
            ]
        stream_configured = bool(os.getenv("KAFKA_BOOTSTRAP"))
        model = store.model(claims["tenant"], siteId, speciesId)
        prediction = app.state.forecasts.latest(claims["tenant"], siteId, speciesId)
        counts = Counter(metrics["series"])
        latest = metrics["latest"]
        with store.connect() as db:
            window_scope = "tenant=? AND json_extract(payload,'$.siteId')=? AND json_extract(payload,'$.speciesId')=?"
            args = (claims["tenant"], siteId, speciesId)
            window_total = db.execute(
                "SELECT count(*) FROM windows WHERE " + window_scope, args
            ).fetchone()[0]
            if stream_configured:
                counts = Counter(
                    dict(
                        db.execute(
                            "SELECT strftime('%Y-%m-%dT%H:%M',json_extract(payload,'$.windowStart')/1000,'unixepoch') AS minute,sum(json_extract(payload,'$.observationCount')) FROM windows WHERE "
                            + window_scope
                            + " GROUP BY minute ORDER BY minute DESC LIMIT 600",
                            args,
                        ).fetchall()
                    )
                )
        with store.connect() as db:
            # Tenant/id is indexed. Seek in either direction without retaining an unbounded page history.
            condition, args = "tenant=?", [claims["tenant"]]
            if batchAfter:
                condition += " AND id>?"
                args.append(batchAfter)
            elif batchBefore:
                condition += " AND id<?"
                args.append(batchBefore)
            order = "DESC" if batchBefore else "ASC"
            batches = [
                json.loads(row["payload"])
                for row in db.execute(
                    "SELECT payload FROM batches WHERE "
                    + condition
                    + " ORDER BY id "
                    + order
                    + " LIMIT 100",
                    args,
                )
            ]
            if batchBefore:
                batches.reverse()
            previous_batch_cursor = next_batch_cursor = None
            if batches:
                first, last = batches[0]["batchId"], batches[-1]["batchId"]
                if db.execute(
                    "SELECT 1 FROM batches WHERE tenant=? AND id<? LIMIT 1",
                    (claims["tenant"], first),
                ).fetchone():
                    previous_batch_cursor = first
                if db.execute(
                    "SELECT 1 FROM batches WHERE tenant=? AND id>? LIMIT 1",
                    (claims["tenant"], last),
                ).fetchone():
                    next_batch_cursor = last
            has_more_batches = next_batch_cursor is not None
            identifiers = [batch["batchId"] for batch in batches]
            holds = (
                dict(
                    db.execute(
                        "SELECT batch,count(*) FROM cold_issues WHERE tenant=? AND status='OPEN' AND batch IN ("
                        + ",".join("?" for _ in identifiers)
                        + ") GROUP BY batch",
                        (claims["tenant"], *identifiers),
                    ).fetchall()
                )
                if identifiers
                else {}
            )
            for batch in batches:
                batch["openColdIssues"] = holds.get(batch["batchId"], 0)
            pending = db.execute(
                "SELECT count(*) FROM outbox WHERE tenant=? AND status='pending'",
                (claims["tenant"],),
            ).fetchone()[0]
        return {
            "tenant": claims["tenant"],
            "generatedAt": now,
            "siteId": siteId,
            "speciesId": speciesId,
            "lastEventAt": latest or None,
            "stale": now - latest > 300,
            "observationCount": metrics["observationCount"],
            "trackCount": metrics["trackCount"],
            "environment": env,
            "prediction": prediction,
            "modelReport": None if not model else {k: v for k, v in model.items() if k != "model"},
            "series": [{"minute": k, "count": v} for k, v in sorted(counts.items())][-600:],
            "batches": batches,
            "pendingEvents": pending,
            "streamWindows": window_total,
            "hasMoreBatches": has_more_batches,
            "previousBatchCursor": previous_batch_cursor,
            "nextBatchCursor": next_batch_cursor,
            "coverage": {
                "eventCounters": "complete",
                "trendBucketLimit": 600,
                "displayedStreamWindows": len(windows),
                "totalStreamWindows": window_total,
                "siteCountsComplete": metrics["siteCountsComplete"],
            },
            "heatmap": [
                {
                    "siteId": w["siteId"],
                    "minute": datetime.fromtimestamp(
                        w["windowStart"] / 1000, timezone.utc
                    ).isoformat()[:16],
                    "count": w["observationCount"],
                }
                for w in sorted(windows, key=lambda w: w["windowStart"])[-480:]
            ],
            "siteLocations": [
                {
                    "siteId": site["siteId"],
                    "longitude": round(float(site["longitude"]), 1),
                    "latitude": round(float(site["latitude"]), 1),
                    "dataSource": site.get("dataSource", "field"),
                    "count": metrics["siteCounts"].get(
                        site["siteId"], 0 if metrics["siteCountsComplete"] else None
                    ),
                }
                for site in {
                    s["siteId"]: s
                    for s in [*sites, *registered_sites(store, claims["tenant"])]
                    if s.get("tenantId") == claims["tenant"]
                }.values()
            ],
            "streamMode": "flink" if stream_configured else "local-reference",
        }

    @app.post("/v1/classifications")
    def classify(image: UploadFile, claims=Depends(operator)):
        directory = Path(os.getenv("MARINE_MODEL_DIR", "artifacts/models/norfisk"))
        if not (directory / "model.onnx").exists():
            raise HTTPException(503, "Classifier model is not installed")
        if app.state.classifier is None:
            from marine.inference import Classifier

            app.state.classifier = Classifier(directory)
        data = image.file.read(8 * 1024 * 1024 + 1)
        started = time.perf_counter()
        result = app.state.classifier.predict(data, include_embedding=True)
        app.state.monitor.record(claims["tenant"], result, time.perf_counter() - started, len(data))
        review = app.state.reviews.add(claims["tenant"], data, result)
        return {**result, **review}

    @app.get("/v1/monitoring")
    def monitoring(claims=Depends(identity)):
        reference = None
        directory = Path(os.getenv("MARINE_MODEL_DIR", "artifacts/models/norfisk"))
        if (directory / "calibration.json").exists():
            reference = json.loads((directory / "calibration.json").read_text())
        return app.state.monitor.report(claims["tenant"], reference)

    from marine.trace import register_trace

    register_trace(app, store, identity, operator, folder)
    from marine.review import register_review

    register_review(app, store, identity, operator)
    from marine.edge_samples import register_edge

    register_edge(app, store, identity, producer, ingest, EventBatch, validator)
    from marine.operations import register_operations

    register_operations(app, store, identity, operator)
    web = ROOT / "web"
    if web.exists():
        app.mount("/", StaticFiles(directory=web, html=True), name="web")
    return app
