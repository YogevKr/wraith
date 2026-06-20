"""Offline tests for the GENERAL reCAPTCHA-v3 reputation-lift capability.

No browser, no network. These assert the agreed shapes compose:

* ``RecaptchaParams`` / ``PlacementSpec`` construct with the agreed fields,
* ``UNPARTITION_PREFS`` encodes the engine-side reliability fix
  (``cookieBehavior`` 0, network-state un-partitioned, first-party isolate off),
* ``ReputationSource`` is an abstract base; the three concrete sources
  instantiate,
* ``ensure_high_score`` is importable and callable with the agreed signature,
* ``identity.inject_cookies(..., third_party=True)`` forces ``secure=True`` +
  ``sameSite="None"`` on every injected cookie (the proven 3rd-party delivery
  fix) while the default preserves the cookie's real attributes,
* ``identity.extract_google_reputation`` filters to
  ``GOOGLE_REPUTATION_COOKIES``,
* ``detect.recaptcha_params`` is importable, and
* the new symbols are re-exported on the top-level ``wraith`` package.
"""

from __future__ import annotations

import abc
import inspect

import pytest

from wraith.recaptcha_v3 import (
    BorrowedGoogleCookies,
    PersistentGrecaptcha,
    PlacementSpec,
    RecaptchaParams,
    ReputationSource,
    UNPARTITION_PREFS,
    WarmedAccount,
    ensure_high_score,
)


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

def test_recaptchaparams_constructs_with_defaults():
    p = RecaptchaParams()
    assert p.version == "none"
    assert p.enterprise is False
    assert p.sitekey == ""
    assert p.actions == []
    # each instance gets its own list (default_factory, not a shared mutable)
    p.actions.append("login")
    assert RecaptchaParams().actions == []


def test_recaptchaparams_constructs_with_values():
    p = RecaptchaParams(
        version="v3",
        enterprise=True,
        sitekey="6Lc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        actions=["login", "submit"],
        host="www.google.com",
    )
    assert p.version == "v3"
    assert p.enterprise is True
    assert p.sitekey.startswith("6Lc_")
    assert p.actions == ["login", "submit"]
    assert p.host == "www.google.com"
    # field shape exactly matches the agreed interface
    assert set(RecaptchaParams.__dataclass_fields__) == {
        "version",
        "enterprise",
        "sitekey",
        "actions",
        "host",
    }


def test_placementspec_constructs():
    spec = PlacementSpec(where="context", name="SAPISID")
    assert spec.where == "context"
    assert spec.name == "SAPISID"
    assert set(PlacementSpec.__dataclass_fields__) == {"where", "name"}


# --------------------------------------------------------------------------- #
# UNPARTITION_PREFS — the engine-side reliability fix
# --------------------------------------------------------------------------- #

def test_unpartition_prefs_shape():
    assert UNPARTITION_PREFS["network.cookie.cookieBehavior"] == 0
    assert UNPARTITION_PREFS["privacy.partition.network_state"] is False
    assert UNPARTITION_PREFS["privacy.firstparty.isolate"] is False


# --------------------------------------------------------------------------- #
# ReputationSource hierarchy
# --------------------------------------------------------------------------- #

def test_reputationsource_is_abstract():
    assert issubclass(ReputationSource, abc.ABC)
    with pytest.raises(TypeError):
        ReputationSource()  # abstract prepare() — cannot instantiate


@pytest.mark.parametrize(
    "cls",
    [BorrowedGoogleCookies, PersistentGrecaptcha, WarmedAccount],
)
def test_concrete_sources_are_subclasses(cls):
    assert issubclass(cls, ReputationSource)


def test_borrowed_google_cookies_instantiates():
    src = BorrowedGoogleCookies()
    assert src.profile_substring is None
    src2 = BorrowedGoogleCookies(profile_substring="default-release")
    assert src2.profile_substring == "default-release"
    # prepare is the abstract method the source implements
    assert callable(src.prepare)


@pytest.mark.parametrize("cls", [PersistentGrecaptcha, WarmedAccount])
def test_profile_backed_sources_instantiate(cls):
    src = cls("/tmp/wraith-profile")
    assert src.profile_dir == "/tmp/wraith-profile"
    # prepare is a documented no-op for profile-backed sources
    assert src.prepare(object()) == 0


@pytest.mark.parametrize("cls", [PersistentGrecaptcha, WarmedAccount])
def test_profile_backed_sources_require_profile_dir(cls):
    with pytest.raises(ValueError):
        cls("")


# --------------------------------------------------------------------------- #
# ensure_high_score
# --------------------------------------------------------------------------- #

def test_ensure_high_score_signature():
    assert callable(ensure_high_score)
    sig = inspect.signature(ensure_high_score)
    params = sig.parameters
    assert list(params)[0] == "page"
    assert params["source"].default is None
    assert params["self_check"].default is False
    assert params["verify_reload_cookies"].default is True


def test_ensure_high_score_runs_offline_with_no_recaptcha():
    """A stub page with no reCAPTCHA returns version='none' without raising."""

    class _StubContext:
        def add_cookies(self, payload):  # pragma: no cover - not reached here
            pass

    class _StubPage:
        def __init__(self):
            self.context = _StubContext()

        def evaluate(self, *_a, **_k):
            # No reCAPTCHA on the page.
            return None

        def query_selector_all(self, *_a, **_k):
            return []

        def query_selector(self, *_a, **_k):
            return None

        def content(self):
            return "<html></html>"

        def on(self, *_a, **_k):
            pass

    # No source, no recaptcha -> emits a "nothing to lift" warning, returns none.
    with pytest.warns(RuntimeWarning):
        params = ensure_high_score(_StubPage(), source=None)
    assert params.version == "none"


# --------------------------------------------------------------------------- #
# identity.inject_cookies(..., third_party=True) — the delivery fix
# --------------------------------------------------------------------------- #

class _CaptureContext:
    """Duck-typed BrowserContext that records the add_cookies payload."""

    def __init__(self):
        self.added = None

    def add_cookies(self, payload):
        self.added = payload


def test_inject_cookies_third_party_forces_secure_and_samesite_none():
    from wraith import identity

    cookies = [
        identity.Cookie(
            name="SAPISID",
            value="abc",
            domain=".google.com",
            path="/",
            secure=False,
            http_only=False,
            same_site="Lax",
        ),
        identity.Cookie(
            name="HSID",
            value="def",
            domain=".google.com",
            path="/",
            secure=False,
            http_only=True,
            same_site="Strict",
        ),
    ]
    ctx = _CaptureContext()
    n = identity.inject_cookies(ctx, cookies, third_party=True)
    assert n == 2
    assert ctx.added is not None
    for c in ctx.added:
        assert c["secure"] is True
        assert c["sameSite"] == "None"


def test_inject_cookies_default_preserves_attributes():
    """Back-compat: default (third_party=False) preserves real attributes."""
    from wraith import identity

    cookies = [
        identity.Cookie(
            name="session",
            value="xyz",
            domain="example.com",
            path="/",
            secure=False,
            http_only=False,
            same_site="Lax",
        ),
    ]
    ctx = _CaptureContext()
    n = identity.inject_cookies(ctx, cookies)
    assert n == 1
    c = ctx.added[0]
    assert c["secure"] is False
    assert c["sameSite"] == "Lax"


def test_inject_cookies_third_party_does_not_mutate_caller_dicts():
    """When given pre-converted dicts, the caller's dicts are not mutated."""
    from wraith import identity

    raw = [{"name": "SID", "value": "v", "secure": False, "sameSite": "Lax"}]
    ctx = _CaptureContext()
    identity.inject_cookies(ctx, raw, third_party=True)
    # the injected payload is forced...
    assert ctx.added[0]["secure"] is True
    assert ctx.added[0]["sameSite"] == "None"
    # ...but the caller's original dict is untouched
    assert raw[0]["secure"] is False
    assert raw[0]["sameSite"] == "Lax"


def test_inject_cookies_empty_returns_zero():
    from wraith import identity

    ctx = _CaptureContext()
    assert identity.inject_cookies(ctx, [], third_party=True) == 0
    assert ctx.added is None  # add_cookies never called for an empty payload


# --------------------------------------------------------------------------- #
# extract_google_reputation filters to GOOGLE_REPUTATION_COOKIES
# --------------------------------------------------------------------------- #

def test_extract_google_reputation_filters_to_set(monkeypatch):
    from wraith import identity

    everything = [
        identity.Cookie(name="SAPISID", value="1", domain=".google.com", path="/"),
        identity.Cookie(name="SID", value="2", domain=".google.com", path="/"),
        identity.Cookie(name="_GRECAPTCHA", value="3", domain=".google.com", path="/"),
        # not a reputation cookie -> must be filtered out
        identity.Cookie(name="OGPC", value="4", domain=".google.com", path="/"),
        identity.Cookie(name="some_random", value="5", domain=".google.com", path="/"),
    ]

    captured = {}

    def _fake_extract_cookies(profile_path, domain_filter=None):
        captured["domain_filter"] = domain_filter
        return everything

    monkeypatch.setattr(identity, "extract_cookies", _fake_extract_cookies)

    out = identity.extract_google_reputation("/fake/profile")
    names = {c.name for c in out}
    # only the reputation-set cookies survive
    assert names == {"SAPISID", "SID", "_GRECAPTCHA"}
    assert names <= set(identity.GOOGLE_REPUTATION_COOKIES)
    # it filtered to the google.com domain
    assert captured["domain_filter"] == "google.com"


def test_google_reputation_cookies_contents():
    from wraith import identity

    expected = {
        "SID",
        "HSID",
        "SSID",
        "APISID",
        "SAPISID",
        "__Secure-1PSID",
        "__Secure-3PSID",
        "NID",
        "SIDCC",
        "__Secure-3PSIDCC",
        "_GRECAPTCHA",
    }
    assert set(identity.GOOGLE_REPUTATION_COOKIES) == expected


# --------------------------------------------------------------------------- #
# detect.recaptcha_params importable
# --------------------------------------------------------------------------- #

def test_recaptcha_params_importable():
    from wraith.detect import recaptcha_params

    assert callable(recaptcha_params)


# --------------------------------------------------------------------------- #
# Top-level re-exports
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "name",
    [
        "ensure_high_score",
        "ReputationSource",
        "BorrowedGoogleCookies",
        "PersistentGrecaptcha",
        "WarmedAccount",
        "RecaptchaParams",
        "UNPARTITION_PREFS",
        "recaptcha_params",
        "extract_google_reputation",
        "GOOGLE_REPUTATION_COOKIES",
    ],
)
def test_reexported_on_package(name):
    import wraith

    assert hasattr(wraith, name), f"wraith.{name} missing"
    assert name in wraith.__all__, f"{name} not in wraith.__all__"
