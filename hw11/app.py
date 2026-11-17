"""HW11: observable CPU simulator continuing HW8910's pinned, stateless API.

Every duration describes chunks of simulated work, never tokens of a model.
Use one Uvicorn worker: concurrency and the bounded prefix cache are per Pod.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import time
from typing import Annotated
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, CONTENT_TYPE_LATEST, generate_latest

ARTIFACT_HASHES = {
    'A': '0144a05a7ba9306da19204796bdc3dd930d0c0d771a40353e7db3b8090281886',
    'B': '8efab4e985d6ff843ff34545a37edf189ad0732d8e8afcc8c20427e84f73e6b0',
}
BUCKETS = (.01, .025, .05, .1, .2, .4, .8, 1.5, 3, 6, 12, 24, 48, 90, 180)


@dataclass
class Config:
    model_version: str = 'A'
    artifact_path: str = ''
    artifact_sha256: str = ''
    pod: str = 'local'
    warmup_seconds: float = 20
    max_concurrency: int = 1
    queue_limit: int = 64
    queue_timeout: float = 60
    cache_size: int = 16
    hit_seconds: float = .04
    miss_seconds: float = .20
    cpu_seconds: float = .02
    chunk_seconds: float = .05
    probe_mode: str = 'fixed'
    never_ready: bool = False

    @classmethod
    def from_env(cls):
        values = {}
        for key, field in cls.__dataclass_fields__.items():
            value = os.getenv(key.upper())
            if value is not None:
                if field.type in ('float', float): value = float(value)
                elif field.type in ('int', int): value = int(value)
                elif field.type in ('bool', bool): value = value.lower() in ('1', 'true', 'yes')
                values[key] = value
        values['pod'] = os.getenv('POD_NAME', socket.gethostname())
        return cls(**values)

    def validate(self):
        if self.model_version not in ARTIFACT_HASHES:
            raise ValueError('MODEL_VERSION must be A or B')
        if not 1 <= self.max_concurrency <= 32 or not 0 <= self.queue_limit <= 1024:
            raise ValueError('concurrency/queue outside limits')
        if not 0 <= self.cache_size <= 1024:
            raise ValueError('CACHE_SIZE outside limits')
        if any(value < 0 or value > 600 for value in (
            self.warmup_seconds, self.queue_timeout, self.hit_seconds,
            self.miss_seconds, self.cpu_seconds, self.chunk_seconds)):
            raise ValueError('durations outside [0,600]')
        if self.probe_mode not in ('fixed', 'broken'):
            raise ValueError('PROBE_MODE must be fixed or broken')


class Message(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Annotated[str, Field(pattern='^(system|user|assistant)$')]
    content: Annotated[str, Field(min_length=1, max_length=4096)]


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: Annotated[str, Field(min_length=1, max_length=128)] = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: Annotated[str, Field(min_length=1, max_length=128)] = 'anonymous'
    prompt: Annotated[str, Field(min_length=1, max_length=8192)]
    history: Annotated[list[Message], Field(max_length=32)] = Field(default_factory=list)
    max_chunks: Annotated[int, Field(strict=True, ge=1, le=100)] = 4
    chunk_seconds: Annotated[float | None, Field(ge=0, le=1)] = None


def cpu_work(seconds: float):
    """Consume thread CPU time rather than wall sleep, visible to HPA/cAdvisor."""
    deadline = time.thread_time() + seconds
    result = 0
    while time.thread_time() < deadline:
        for number in range(400):
            result = (result + number * number) % 1000000007
    return result


class State:
    def __init__(self, config):
        config.validate()
        self.config = config
        self.warm = False
        self.draining = False
        self.load_error = None
        self.loaded_sha256 = None
        self.loaded_version = None
        self.waiting = 0
        self.running = 0
        self.cache = OrderedDict()
        self.semaphore = asyncio.Semaphore(config.max_concurrency)
        self.registry = CollectorRegistry()
        self.labels = (config.pod, config.model_version)
        names = ('pod', 'model_version')
        self.requests = Counter('hw11_requests_total', 'Terminal request outcomes', names + ('outcome',), registry=self.registry)
        self.ttfc = Histogram('hw11_ttfc_seconds', 'Time to first content chunk, completed responses only', names, buckets=BUCKETS, registry=self.registry).labels(*self.labels)
        self.total = Histogram('hw11_total_seconds', 'Full response duration, completed responses only', names, buckets=BUCKETS, registry=self.registry).labels(*self.labels)
        self.queue = Histogram('hw11_queue_seconds', 'Queue duration of completed responses', names, buckets=BUCKETS, registry=self.registry).labels(*self.labels)
        self.running_metric = Gauge('hw11_running', 'Currently generating requests', names, registry=self.registry).labels(*self.labels)
        self.waiting_metric = Gauge('hw11_waiting', 'Requests waiting for a generation slot', names, registry=self.registry).labels(*self.labels)
        self.ready_metric = Gauge('hw11_ready', 'Backend ready and accepting admission', names, registry=self.registry).labels(*self.labels)
        self.cache_outcomes = Counter('hw11_cache_total', 'Explicitly simulated prefix cache accesses', names + ('result',), registry=self.registry)
        for outcome in ('complete', 'rejected', 'aborted'):
            self.requests.labels(*self.labels, outcome).inc(0)

    @property
    def ready(self):
        return self.warm and not self.draining and not self.config.never_ready and self.load_error is None

    def log(self, event, **fields):
        print(json.dumps(dict(event=event, time_epoch=time.time(), monotonic=time.monotonic(),
                             pod=self.config.pod, model_version=self.loaded_version,
                             backend_mode='simulated', **fields), ensure_ascii=False), flush=True)

    async def load(self):
        try:
            path = Path(self.config.artifact_path) if self.config.artifact_path else Path(__file__).parent / 'artifacts' / f'model-{self.config.model_version}.json'
            raw = path.read_bytes()
            self.loaded_sha256 = hashlib.sha256(raw).hexdigest()
            expected = self.config.artifact_sha256 or ARTIFACT_HASHES[self.config.model_version]
            if self.loaded_sha256 != expected:
                raise ValueError('Artifact SHA256 mismatch')
            artifact = json.loads(raw)
            if artifact['model_version'] != self.config.model_version or artifact['backend_mode'] != 'simulated':
                raise ValueError('Artifact identity mismatch')
            self.loaded_version = artifact['model_version']
            self.log('warmup_started', warmup_seconds=self.config.warmup_seconds, artifact_sha256=self.loaded_sha256)
            await asyncio.sleep(self.config.warmup_seconds)
            self.warm = True
            self.ready_metric.set(int(self.ready))
            self.log('warmup_complete', ready=self.ready)
        except Exception as exc:
            self.load_error = f'{type(exc).__name__}: {exc}'
            self.log('load_failed', error=self.load_error)

    def identity(self, request):
        return dict(request_id=request.request_id, session_id=request.session_id,
                    pod=self.config.pod, model_version=self.loaded_version,
                    artifact_sha256=self.loaded_sha256, backend_mode='simulated')

    def reject(self, request, status, reason):
        self.requests.labels(*self.labels, 'rejected').inc()
        self.log('request_rejected', request_id=request.request_id, session_id=request.session_id,
                 status=status, reason=reason, warm=self.warm)
        return JSONResponse(dict(**self.identity(request), error=reason, warm=self.warm), status_code=status)


def create_app(config: Config | None = None):
    state = State(config or Config.from_env())

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(state.load())
        yield
        state.draining = True
        state.ready_metric.set(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title='HW11 observable CPU simulator', lifespan=lifespan)
    app.state.backend = state

    @app.middleware('http')
    async def bound_body(request: Request, call_next):
        if request.method == 'POST':
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                return JSONResponse({'error': 'Invalid content-length'}, status_code=400)
            if size < 0 or size > 196608 or len(await request.body()) > 196608:
                return JSONResponse({'error': 'Body too large'}, status_code=413)
        return await call_next(request)

    @app.get('/health')
    async def health():
        return dict(alive=True, backend_mode='simulated', pod=state.config.pod)

    async def readiness():
        ready = not state.draining if state.config.probe_mode == 'broken' else state.ready
        return JSONResponse(dict(ready=ready, warm=state.warm, draining=state.draining,
                                 pod=state.config.pod, model_version=state.loaded_version,
                                 artifact_sha256=state.loaded_sha256, backend_mode='simulated',
                                 load_error=state.load_error), status_code=200 if ready else 503)
    app.get('/ready')(readiness)
    app.get('/startup')(readiness)

    @app.api_route('/drain', methods=['GET', 'POST'])
    async def drain():
        state.draining = True
        state.ready_metric.set(0)
        state.log('drain_started', running=state.running, waiting=state.waiting)
        return dict(draining=True, running=state.running, waiting=state.waiting)

    @app.get('/metrics')
    async def metrics():
        return Response(generate_latest(state.registry), headers={'Content-Type': CONTENT_TYPE_LATEST})

    @app.post('/generate')
    async def generate(body: GenerateRequest, request: Request):
        started = time.monotonic()
        state.log('request_admitted', request_id=body.request_id, session_id=body.session_id,
                  warm=state.warm, ready=state.ready, client_ip=request.client.host if request.client else None,
                  prompt_chars=len(body.prompt), max_chunks=body.max_chunks)
        if not state.ready:
            return state.reject(body, 503, 'backend_not_ready')
        # An available slot is admitted immediately even when queue_limit == 0.
        if state.semaphore.locked() and state.waiting >= state.config.queue_limit:
            return state.reject(body, 429, 'queue_full')
        state.waiting += 1
        state.waiting_metric.set(state.waiting)
        try:
            await asyncio.wait_for(state.semaphore.acquire(), timeout=state.config.queue_timeout)
        except TimeoutError:
            return state.reject(body, 503, 'queue_timeout')
        except asyncio.CancelledError:
            state.requests.labels(*state.labels, 'aborted').inc()
            state.log('request_aborted', request_id=body.request_id, session_id=body.session_id,
                      chunks=0, phase='queue')
            raise
        finally:
            state.waiting -= 1
            state.waiting_metric.set(state.waiting)
        queue_seconds = time.monotonic() - started
        state.running += 1
        state.running_metric.set(state.running)
        prefix = hashlib.sha256(body.prompt[:64].encode()).hexdigest()
        cache_hit = prefix in state.cache
        state.cache_outcomes.labels(*state.labels, 'hit' if cache_hit else 'miss').inc()
        if cache_hit:
            state.cache.move_to_end(prefix)
        context = '\n'.join(message.content for message in body.history)
        chunk_interval = state.config.chunk_seconds if body.chunk_seconds is None else body.chunk_seconds
        first_at = None
        sent = 0
        identity = state.identity(body)

        async def stream():
            nonlocal first_at, sent
            completed = False
            try:
                await asyncio.sleep(state.config.hit_seconds if cache_hit else state.config.miss_seconds)
                if state.config.cache_size:
                    state.cache[prefix] = True
                    state.cache.move_to_end(prefix)
                    while len(state.cache) > state.config.cache_size:
                        state.cache.popitem(last=False)
                for index in range(body.max_chunks):
                    if index:
                        await asyncio.sleep(chunk_interval)
                    await asyncio.to_thread(cpu_work, state.config.cpu_seconds)
                    if await request.is_disconnected():
                        return
                    stamp = time.monotonic()
                    if first_at is None:
                        first_at = stamp
                    sent += 1
                    text = f'SIMULATED {state.loaded_version}: {context} {body.prompt}' if index == 0 else f'chunk {index + 1}'
                    yield json.dumps(dict(**identity, chunk=index + 1, text=text,
                                          done=False, cache_hit=cache_hit, queue_seconds=queue_seconds,
                                          warm_at_admission=True), ensure_ascii=False) + '\n'
                yield json.dumps(dict(**identity, done=True, chunks=sent, cache_hit=cache_hit,
                                      queue_seconds=queue_seconds)) + '\n'
                completed = True
                duration = time.monotonic() - started
                state.ttfc.observe(first_at - started)
                state.total.observe(duration)
                state.queue.observe(queue_seconds)
                state.requests.labels(*state.labels, 'complete').inc()
                state.log('request_complete', request_id=body.request_id, session_id=body.session_id,
                          chunks=sent, cache_hit=cache_hit, queue_seconds=queue_seconds,
                          ttfc_seconds=first_at - started, total_seconds=duration)
            finally:
                if not completed:
                    state.requests.labels(*state.labels, 'aborted').inc()
                    state.log('request_aborted', request_id=body.request_id, session_id=body.session_id,
                              chunks=sent, queue_seconds=queue_seconds)
                state.running -= 1
                state.running_metric.set(state.running)
                state.semaphore.release()

        return StreamingResponse(stream(), media_type='application/x-ndjson', headers={
            'X-Request-ID': body.request_id, 'X-Pod-Name': state.config.pod,
            'X-Model-Version': state.loaded_version, 'X-Backend-Mode': 'simulated',
            'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no',
        })

    return app


app = create_app()
