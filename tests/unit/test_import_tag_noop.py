import _guard  # noqa: F401

import json

import pytest

from agora import cli, store
from test_agent_codex import SID, document, lab, rollout, run  # noqa: F401


@pytest.mark.parametrize('change', ['none', 'new_tag', 'raw', 'updates'])
def test_reimport_tag_effects(rollout, lab, capsys, monkeypatch, tmp_path, change):
    args = ['import', 'session', '--agent', 'codex', '--external-session-id', SID,
            '--add-tag', 'role:R']
    code, aid, err = run(capsys, *args)
    assert code == 0, err
    path = store.Paths.from_env().mirror / aid.split(':')[1] / 'session.md'
    before = path.read_bytes()
    old = document(aid)[0]
    calls = []
    save = cli._save

    def counted_save(*a, **kw):
        calls.append(kw)
        return save(*a, **kw)

    monkeypatch.setattr(cli, '_save', counted_save)
    monkeypatch.setattr(cli, '_now_iso', lambda: '2099-01-01T00:00:00Z')
    if change == 'raw':
        rollout.write_bytes(rollout.read_bytes() + json.dumps(
            {'type': 'future_event', 'payload': {}}).encode() + b'\n')
    if change == 'new_tag':
        args += ['--add-tag', 'role:S', '--add-tag', 'role:S']
    if change == 'updates':
        args += ['--header', 'title=updated']
    log = tmp_path / 'calls.log'
    before_uploads = [line for line in log.read_text().splitlines()
                      if '"copy"' in line or '"copyto"' in line]
    assert before_uploads  # The fake Drive observed the initial upload.
    code, out, err = run(capsys, *args)
    assert code == 0, err
    assert out == aid
    hdr = document(aid)[0]
    if change == 'none':
        assert calls == []
        assert path.read_bytes() == before
        assert hdr['agora']['updated_at'] == old['agora']['updated_at']
        assert [line for line in log.read_text().splitlines()
                if '"copy"' in line or '"copyto"' in line] == before_uploads
    else:
        assert len(calls) == 1 and calls[0]['update'] is True
        assert path.read_bytes() != before
        assert hdr['agora']['updated_at'] == '2099-01-01T00:00:00Z'
        assert hdr['tags'].count('role:R') == 1
        if change == 'new_tag':
            assert hdr['tags'] == old['tags'] + ['role:S']
        elif change == 'raw':
            assert hdr['agora']['raw']['md5'] != old['agora']['raw']['md5']
        else:
            assert hdr['title'] == 'updated'
