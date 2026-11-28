#!/usr/bin/env python3
"""Offline GitHub request probe. Run: uv run python tests/perf/measure_github_api_calls.py.

Uses a temporary playground beside the team worktree, and never opens a socket.
Counts HTTP requests, including conditional 304 responses, not quota consumption.
"""
import contextlib
import io
import json
from types import SimpleNamespace
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

def main():
    with tempfile.TemporaryDirectory(prefix='probe-590-') as tmp:
        root = Path(tmp).resolve()
        for key in list(os.environ):
            if key.startswith('XDG_') or key in {'CLAUDE_CONFIG_DIR', 'OPENCODE_CONFIG_CONTENT', 'CODEX_HOME', 'ANGAREION_IDENTITY'}:
                os.environ.pop(key, None)
        for key, value in {'HOME':'home', 'TMPDIR':'tmp', 'AGORA_CONFIG':'config',
                           'AGORA_CACHE_DIR':'cache', 'AGORA_STATE_DIR':'state',
                           'AGORA_CODEX_HOME':'codex', 'ANGAREION_CONFIG':'ang-config',
                           'ANGAREION_STATE_DIR':'ang-state', 'ANGAREION_TOKEN_FILE':'token'}.items():
            os.environ[key] = str(root / value)
        (root / 'tmp').mkdir()
        from agora.store import Paths
        p = Paths.from_env()
        for name, path in [('config', p.config), ('cache', p.cache), ('state', p.state),
                           ('HOME', Path(os.environ['HOME'])), ('TMPDIR', Path(os.environ['TMPDIR']))]:
            assert path.resolve().is_relative_to(root), f'NOT ISOLATED: {name}={path}'
        from angareion.backend import BackendRef, Entity, Target
        from angareion.client import Angareion
        from angareion.channel_cli import cmd_channel_create
        from angareion.config import Config
        from angareion.decision_cli import cmd_list
        from angareion.registry import RegistryClient
        from angareion.github import GitHubIssuesBackend
        from angareion.log_cache import LogCache
        from angareion.messages import Message, serialize_message
        from angareion.rollover import MemoryPendingPages, PageWriter
        from angareion.state import CursorStore
        from angareion.transport import HttpResponse

        class Store:
            def __init__(self): self.items = [Entity('general', 'main', 'example/repo', 10)]
            def entity_for(self, name): return next(e for e in self.items if e.channel == name)
            def entities(self): return self.items
            def ensure_fresh(self): pass
            def channels(self): return {}
            def choose_backend_for_create(self, name): return BackendRef("main", "github", "example/repo")
            def poll_for_channel(self, name): return True

        class Transport:
            def __init__(self): self.calls = []; self.comments = []; self.pages = 1
            def request(self, method, url, headers=None, body=None, timeout=None):
                path = urlsplit(url).path
                kind = 'issue comments' if path.endswith('/comments') else ('issues' if path.endswith('/issues') else 'issue')
                self.calls.append((method, kind))
                if method == 'POST':
                    return HttpResponse(201, {}, json.dumps({'number': 11, 'id': 1}).encode())
                if kind == 'issue': return HttpResponse(200, {}, b'{"comments":0,"title":"general"}')
                assert method == 'GET' and kind == 'issue comments', (method, path)
                if headers and headers.get('If-None-Match') == '"v1"' and self.pages == 1 and not self.comments:
                    return HttpResponse(304, {}, b'')
                page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
                h = {'etag':'"v1"', 'date':'Tue, 27 Oct 2026 05:04:03 GMT'}
                if page < self.pages: h['link'] = '<https://example.invalid/repos/example/repo/issues/10/comments?per_page=100&page=2>; rel="next"'
                return HttpResponse(200, h, json.dumps(self.comments if page == 1 else []).encode())

        store = Store(); t = Transport()
        backend = GitHubIssuesBackend(store, transport=t, base_url='https://example.invalid')
        writer = PageWriter(backend, None, pending=MemoryPendingPages(), backends=lambda: {}, refresh=store.entity_for)
        app = Angareion(backend, CursorStore(path=root/'cursor'), store, 'demo-pm-w1', writer=writer)
        entity = store.items[0]
        msg = Message(from_='demo-impl-w1-1', to='demo-pm-w1', channel='general', content='hello')
        def row(label, fn):
            t.calls.clear(); fn(); counts = {}
            for method, kind in t.calls: counts[f'{method} {kind}'] = counts.get(f'{method} {kind}', 0) + 1
            print(f'{label}: {len(t.calls)} | ' + (', '.join(f'{k}={v}' for k,v in sorted(counts.items())) or '-'))
        def read(): return app.read(Target(channel='general'))
        print('scenario: requests | method path-class=count')
        row('send single', lambda: app.send('general', msg))
        msg.content = 'hello\nsecond line'
        row('send multiline', lambda: app.send('general', msg))
        row('read empty', read)
        t.comments = [{'id': 101, 'body': serialize_message(msg), 'created_at':'2026-10-27T05:00:00Z'}]
        row('read new', read)
        row('read since', lambda: app.read(Target(channel='general'), since='2026-10-01T00:00:00Z'))
        t.pages = 2
        row('read two HTTP pages', read)
        t.pages = 1; t.comments = []
        row('wait first empty poll', lambda: app.wait(Target(channel='general'), 0))
        row('wait conditional 304 poll', lambda: app.wait(Target(channel='general'), 0))
        row('inbox one channel', lambda: app.read(Target(recipient='demo-pm-w1')))
        cache = LogCache(state_dir=root/'log', identity='demo-pm-w1')
        row('log cold one channel', lambda: cache.sync(backend, entity))
        row('log warm one channel', lambda: cache.sync(backend, entity))
        config = Config(identity='demo-pm-w1', config_dir=root, registry_repo='example/repo', registry_issue=20, state_dir=root/'decision')
        registry = RegistryClient('example/repo', 20, transport=t, base_url='https://example.invalid')
        def create():
            with contextlib.redirect_stdout(io.StringIO()):
                assert cmd_channel_create(SimpleNamespace(name='general', backend=None), config, app, registry, store, '', sleeper=lambda _: None) == 0
        def decisions():
            with contextlib.redirect_stdout(io.StringIO()):
                assert cmd_list(SimpleNamespace(channel=['general'], all=False, json=True), config, app, backend, '') == 0
        row('decision list cold', decisions)
        row('decision list warm', decisions)
        row('channel create complete', create)
        row('channel create issue', lambda: app.create_channel('general', BackendRef('main','github','example/repo')))
        row('channel verify issue', lambda: app.verify_channel('general', BackendRef('main','github','example/repo'), 10))
        row('channel list local resolver', lambda: store.entities())
        print('Scope: fake transport only; git fetch, CLI setup, decision writes and full tick are not measured.')

if __name__ == '__main__':
    main()
