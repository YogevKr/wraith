"""Offline tests for the reCAPTCHA-v3 helper layer.

No browser, no network. They assert that the public API imports and carries
the agreed shapes:

* ``harvest_token`` / ``score`` are callable with the agreed signatures,
* ``SolverService`` is an abstract base (cannot be instantiated),
* ``CapSolver`` / ``TwoCaptcha`` are concrete and instantiable (with a key),
  and reject an empty key, and
* the symbols are re-exported on the top-level ``wraith`` package.
"""

from __future__ import annotations

import abc
import inspect

import pytest

from wraith.recaptcha import (
    CapSolver,
    SolverService,
    TwoCaptcha,
    harvest_token,
    score,
)


# --------------------------------------------------------------------------- #
# Module-level functions
# --------------------------------------------------------------------------- #

def test_harvest_token_signature():
    assert callable(harvest_token)
    sig = inspect.signature(harvest_token)
    params = sig.parameters
    assert list(params)[:3] == ["page", "sitekey", "action"]
    assert params["action"].default == "submit"
    assert params["timeout"].default == 30.0
    assert params["enterprise"].default is False


def test_score_is_callable():
    assert callable(score)
    sig = inspect.signature(score)
    assert "target" in sig.parameters


# --------------------------------------------------------------------------- #
# SolverService hierarchy
# --------------------------------------------------------------------------- #

def test_solverservice_is_abstract():
    assert issubclass(SolverService, abc.ABC)
    with pytest.raises(TypeError):
        SolverService("k")  # abstract solve() — cannot instantiate


def test_concrete_solvers_are_subclasses():
    assert issubclass(CapSolver, SolverService)
    assert issubclass(TwoCaptcha, SolverService)


@pytest.mark.parametrize("cls", [CapSolver, TwoCaptcha])
def test_concrete_solver_instantiable_with_key(cls):
    svc = cls("test-api-key")
    assert svc.api_key == "test-api-key"
    assert callable(svc.solve)


@pytest.mark.parametrize("cls", [CapSolver, TwoCaptcha])
def test_concrete_solver_rejects_empty_key(cls):
    with pytest.raises(ValueError):
        cls("")


@pytest.mark.parametrize("cls", [CapSolver, TwoCaptcha])
def test_solve_signature(cls):
    sig = inspect.signature(cls.solve)
    params = sig.parameters
    assert "sitekey" in params
    assert "url" in params
    assert params["action"].default == "submit"
    assert params["min_score"].default == 0.5


# --------------------------------------------------------------------------- #
# Top-level re-exports
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name",
    ["harvest_token", "score", "SolverService", "CapSolver", "TwoCaptcha"],
)
def test_reexported_on_package(name):
    import wraith

    assert hasattr(wraith, name), f"wraith.{name} missing"
    assert name in wraith.__all__, f"{name} not in wraith.__all__"
