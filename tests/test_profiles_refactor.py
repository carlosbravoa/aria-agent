"""Characterization of model-profile listing/switching (inheritance, defaults,
first-match on duplicate names) — guards the _env_profiles() refactor."""


def _setenv(monkeypatch):
    monkeypatch.setenv("LLM_PROFILE1_NAME", " Fast ")
    monkeypatch.setenv("LLM_PROFILE1_MODEL", "m1")
    monkeypatch.setenv("LLM_PROFILE3_MODEL", "m3")                 # unnamed → profile3
    monkeypatch.setenv("LLM_PROFILE3_BASE_URL", "http://three")
    monkeypatch.setenv("LLM_PROFILE3_API_KEY", "k3")
    monkeypatch.setenv("LLM_PROFILE4_NAME", "fast")                # duplicate name
    monkeypatch.setenv("LLM_PROFILE4_MODEL", "m4")
    monkeypatch.setenv("LLM_PROFILE5_NAME", "nomodel")             # no MODEL → ignored


def test_list_profiles(minimal_env, monkeypatch):
    from aria.agent import Agent
    _setenv(monkeypatch)
    a = Agent(window_key="t", terminal=False)
    assert a.list_profiles() == [
        {"key": "default", "name": "default", "model": "test-model",
         "base_url": "http://test.invalid", "active": True},
        {"key": "fast", "name": "fast", "model": "m1",
         "base_url": "http://test.invalid", "active": False},
        {"key": "profile3", "name": "profile3", "model": "m3",
         "base_url": "http://three", "active": False},
        {"key": "fast", "name": "fast", "model": "m4",
         "base_url": "http://test.invalid", "active": False},
    ]


def test_switch_profile(minimal_env, monkeypatch):
    from aria.agent import Agent
    _setenv(monkeypatch)
    a = Agent(window_key="t", terminal=False)
    assert a.switch_profile("FAST") == "Switched to fast (m1)"      # first match wins
    assert (a._base_url, a._api_key, a.model) == ("http://test.invalid", "test-key", "m1")
    assert a.switch_profile("profile3") == "Switched to profile3 (m3)"
    assert (a._base_url, a._api_key) == ("http://three", "k3")
    assert a.switch_profile("nomodel") == (
        "Profile 'nomodel' not found. Available: default, fast, profile3, fast")
    assert a.switch_profile("default") == "Switched to default (test-model)"
