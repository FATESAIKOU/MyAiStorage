"""`model_chain` tries AGORA_TEST_MODEL first (the integration tests' contract).

The free endpoints can be out of quota for the whole account, and a run that
tries them first waits out their full timeout before reaching the model the
person running the tests actually set. The wrapper (tests/fakes) fixes the
order, tests/integration/test_e2e_fork.py, test_e2e_opencode.py and
test_opencode_real.py follow it, so it is pinned here: with AGORA_TEST_MODEL set
it comes first - carrying the third-party `--variant max` when it is that model
- with the rest of the chain after it and no model tried twice; without it the
chain is exactly what it was.

`models_of` and the environment are faked; nothing here runs opencode.
"""

from __future__ import annotations

import sys as _s, pathlib as _p; _s.path.insert(0, str(_p.Path(__file__).resolve().parent.parent)); import _guard  # noqa: E402,F401  (T8: these helpers need isolation)

import importlib.util
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parent.parent / "fakes" / "opencode_noninteractive.py"


def _wrapper():
    """The fake the integration tests import, loaded from its own file (not a package)."""
    module = importlib.util.module_from_spec(spec := importlib.util.spec_from_file_location(
        "opencode_noninteractive_under_test", WRAPPER))
    spec.loader.exec_module(module)
    return module


W = _wrapper()
THIRD = W.THIRD_PARTY[0]
OTHER = "somewhere/other-model"


def chain_with(monkeypatch, *, chosen, provider_models=frozenset()):
    """model_chain() with the environment faked and the provider probe answered."""
    monkeypatch.setattr(W, "models_of", lambda provider: frozenset(provider_models))
    if chosen is None:
        monkeypatch.delenv("AGORA_TEST_MODEL", raising=False)
    else:
        monkeypatch.setenv("AGORA_TEST_MODEL", chosen)
    return W.model_chain()


def test_the_env_model_is_first_and_carries_its_variant(monkeypatch):
    chain = chain_with(monkeypatch, chosen=THIRD, provider_models={THIRD})
    assert chain[0] == ["-m", THIRD, *W.THIRD_PARTY[1]]
    assert [flags[1] for flags in chain] == [THIRD, W.PRIMARY, W.FALLBACK]


def test_the_env_model_is_first_even_when_the_probe_does_not_know_it(monkeypatch):
    """An explicit AGORA_TEST_MODEL is the runner's call: it does not wait on this
    machine's `opencode models` output either."""
    chain = chain_with(monkeypatch, chosen=THIRD, provider_models=set())
    assert chain[0] == ["-m", THIRD, *W.THIRD_PARTY[1]]
    assert [flags[1] for flags in chain] == [THIRD, W.PRIMARY, W.FALLBACK]


def test_an_env_model_from_another_provider_is_first_and_plain(monkeypatch):
    chain = chain_with(monkeypatch, chosen=OTHER, provider_models={THIRD})
    assert chain[0] == ["-m", OTHER]
    assert [flags[1] for flags in chain] == [OTHER, W.PRIMARY, W.FALLBACK, THIRD]


@pytest.mark.parametrize("chosen", [W.PRIMARY, W.FALLBACK, THIRD])
def test_the_env_model_is_not_tried_twice(monkeypatch, chosen):
    chain = chain_with(monkeypatch, chosen=chosen, provider_models={THIRD})
    models = [flags[1] for flags in chain]
    assert models[0] == chosen
    assert models.count(chosen) == 1


def test_without_the_env_the_chain_is_unchanged(monkeypatch):
    assert chain_with(monkeypatch, chosen=None, provider_models={THIRD}) == [
        ["-m", W.PRIMARY], ["-m", W.FALLBACK], ["-m", THIRD, *W.THIRD_PARTY[1]]]
    assert chain_with(monkeypatch, chosen=None, provider_models=set()) == [
        ["-m", W.PRIMARY], ["-m", W.FALLBACK]]


def test_an_empty_env_var_counts_as_unset(monkeypatch):
    """`export AGORA_TEST_MODEL=` must not put `-m ''` in front of the chain."""
    assert chain_with(monkeypatch, chosen="", provider_models={THIRD}) == [
        ["-m", W.PRIMARY], ["-m", W.FALLBACK], ["-m", THIRD, *W.THIRD_PARTY[1]]]
