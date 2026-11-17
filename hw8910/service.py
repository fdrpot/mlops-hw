"""Local FastAPI serving: accepted pointer is read once, weights stay pinned.

The injectable stub exercises HTTP and process boundaries without model packages.
Run with --backend real for the evaluated model; debug is disabled for that API.
"""
import argparse
from contextlib import asynccontextmanager
import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from typing import Annotated
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

import release


class StubBackend:
    """An explicit synthetic backend, never presented as model evaluation."""

    def __init__(self, manifest, root=None):
        self.loaded_id = manifest['model_id']
        self.runtime_sha256 = manifest['runtime_sha256']
        self.calls = 0

    def warmup(self):
        pass

    def generate(self, prompt, limit):
        self.calls += 1
        time.sleep(.01)
        return {'answer': f'STUB ONLY: {self.loaded_id}', 'output_tokens': 1}


class GenerateRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt: Annotated[str, Field(strict=True, min_length=1, max_length=8192)]
    max_new_tokens: Annotated[StrictInt, Field(ge=1, le=32)] = 32

    @field_validator('prompt')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('prompt must contain non-whitespace characters')
        return value


class ServiceState:
    def __init__(self, root, warmup_seconds, backend_factory=StubBackend, real=False):
        self.root = Path(root)
        self.warmup_seconds = warmup_seconds
        self.backend_factory = backend_factory
        self.real = real
        self.backend = None
        self.manifest = None
        self.load_error = None
        self.warmup_finished = threading.Event()
        self.stopping = threading.Event()
        self.generation_slot = threading.BoundedSemaphore(1)
        self.worker = None

    @property
    def ready(self):
        return self.warmup_finished.is_set() and self.backend is not None

    def start(self):
        def load():
            try:
                manifest = json.loads((self.root / 'accepted.json').read_text())
                runtime = manifest.get('runtime')
                if (type(manifest.get('schema_version')) is not int
                        or manifest['schema_version'] != 1
                        or not isinstance(runtime, dict) or not runtime
                        or hashlib.sha256(release.canonical(runtime)).hexdigest()
                        != manifest.get('runtime_sha256')):
                    raise ValueError('Invalid accepted manifest/runtime')
                release.verify_artifacts(manifest, self.root)
                if self.real and runtime.get('implementation_sha256') != release.file_digest(
                        Path(__file__).with_name('real_backend.py')):
                    raise ValueError('Generation implementation changed; evaluation required')
                self.manifest = copy.deepcopy(manifest)
                self.backend = self.backend_factory(self.manifest, self.root)
                if self.backend.loaded_id != manifest['model_id'] or self.backend.runtime_sha256 != manifest['runtime_sha256']:
                    raise ValueError('Loaded backend identity differs from manifest')
                self.backend.warmup()
                if not self.stopping.wait(self.warmup_seconds):
                    self.warmup_finished.set()
            except Exception as exc:
                self.load_error = f'{type(exc).__name__}: {exc}'
                print('LOAD_FAILED', self.load_error, flush=True)
        self.worker = threading.Thread(target=load, name='model-loader', daemon=True)
        self.worker.start()

    def close(self):
        self.stopping.set()
        self.warmup_finished.clear()
        if self.worker:
            self.worker.join(timeout=1)

    def generate(self, request):
        started = time.perf_counter()
        if not self.ready:
            raise HTTPException(503, 'Model is loading or warming up')
        if self.real and request.max_new_tokens != self.manifest['runtime']['generation']['max_new_tokens']:
            raise HTTPException(422, 'This release only admits the evaluated max_new_tokens')
        if hasattr(self.backend, 'validate_prompt'):
            try:
                self.backend.validate_prompt(request.prompt)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        if not self.generation_slot.acquire(timeout=60):
            raise HTTPException(503, 'Generation queue timed out')
        acquired = time.perf_counter()
        try:
            result = self.backend.generate(request.prompt, request.max_new_tokens)
        finally:
            finished = time.perf_counter()
            self.generation_slot.release()
        result.update(
            request_id=str(uuid.uuid4()), model_id=self.backend.loaded_id,
            runtime_sha256=self.backend.runtime_sha256,
            queue_seconds=acquired - started,
            generation_seconds=finished - acquired,
            total_seconds=finished - started,
        )
        return result


def create_app(root=Path('.lab'), warmup_seconds=0, backend_factory=StubBackend, *, real=False, debug=False):
    state = ServiceState(root, warmup_seconds, backend_factory, real)

    @asynccontextmanager
    async def lifespan(app):
        state.start()
        yield
        state.close()

    app = FastAPI(title='Evaluated model API', lifespan=lifespan)
    app.state.service = state

    @app.middleware('http')
    async def bounded_body(request: Request, call_next):
        if request.method == 'POST':
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                return JSONResponse({'error': 'Invalid content-length'}, status_code=400)
            if size < 0 or size > 65536:
                return JSONResponse({'error': 'Body too large'}, status_code=413)
            if len(await request.body()) > 65536:
                return JSONResponse({'error': 'Body too large'}, status_code=413)
        return await call_next(request)

    @app.get('/health')
    def health():
        return {'alive': True, 'backend': 'real' if real else 'STUB ONLY'}

    @app.get('/ready')
    def ready():
        return JSONResponse({'ready': state.ready}, status_code=200 if state.ready else 503)

    if debug or not real:
        @app.get('/debug')
        def debug_state():
            return {'loaded_id': state.backend.loaded_id if state.backend else None,
                    'calls': state.backend.calls if state.backend else 0,
                    'warmup_finished': state.warmup_finished.is_set(),
                    'load_error': state.load_error}

    @app.post('/generate')
    def generate(request: GenerateRequest):
        return state.generate(request)

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, default=Path('.lab'))
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--warmup-seconds', type=float, default=5)
    parser.add_argument('--backend', choices=['stub', 'real'], default='stub')
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()
    if args.warmup_seconds < 0:
        parser.error('--warmup-seconds must be nonnegative')
    factory = StubBackend
    if args.backend == 'real':
        from real_backend import RealBackend
        factory = RealBackend
    import uvicorn
    app = create_app(args.workspace, args.warmup_seconds, factory,
                     real=args.backend == 'real', debug=args.debug)
    uvicorn.run(app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
