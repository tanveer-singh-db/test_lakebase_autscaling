"""Unit tests for shared URL / auth helpers in lakebase_utils._common."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from lakebase_utils._common import _make_ws, resolve_auth, resolve_base_url


pytestmark = pytest.mark.unit


class TestResolveBaseUrl:
    def test_from_parts(self, clean_env):
        url = resolve_base_url(None, "ep-xx.database.example.com", "9999", "db")
        assert url == "https://ep-xx.database.example.com/api/2.0/workspace/9999/rest/db"

    def test_explicit_wins_over_parts(self, clean_env):
        explicit = "https://explicit.example.com/custom/path"
        url = resolve_base_url(explicit, "other-host.com", "1", "other-db")
        assert url == explicit

    def test_explicit_wins_over_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("LAKEBASE_API_URL", "https://env.example.com/x")
        explicit = "https://explicit.example.com/y"
        assert resolve_base_url(explicit, None, None, None) == explicit

    def test_from_env_fallback(self, clean_env, monkeypatch):
        monkeypatch.setenv("LAKEBASE_API_URL", "https://env.example.com/base/")
        assert resolve_base_url(None, None, None, None) == "https://env.example.com/base"

    def test_missing_raises_value_error(self, clean_env):
        with pytest.raises(ValueError, match="base URL is required"):
            resolve_base_url(None, None, None, None)

    def test_partial_parts_do_not_resolve(self, clean_env):
        with pytest.raises(ValueError):
            resolve_base_url(None, "host.example.com", "999", None)  # missing database


class TestResolveAuth:
    def test_auto_prefers_explicit_token(self, clean_env, mock_workspace_client):
        with patch("lakebase_utils._common._make_ws") as make_ws:
            static, ws = resolve_auth(None, token="mine")
        assert static == "mine"
        assert ws is None
        make_ws.assert_not_called()

    def test_auto_falls_back_to_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("LAKEBASE_API_TOKEN", "from-env")
        with patch("lakebase_utils._common._make_ws") as make_ws:
            static, ws = resolve_auth(None)
        assert static == "from-env"
        assert ws is None
        make_ws.assert_not_called()

    def test_auto_falls_back_to_sdk(self, clean_env, mock_workspace_client):
        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client) as make_ws:
            static, ws = resolve_auth(None, profile="my-profile")
        assert static is None
        assert ws is mock_workspace_client
        make_ws.assert_called_once_with(
            host=None, profile="my-profile", client_id=None, client_secret=None,
        )

    def test_oauth_token_requires_token(self, clean_env):
        with pytest.raises(ValueError, match="auth_mode='oauth_token' requires: token"):
            resolve_auth("oauth_token")

    def test_oauth_token_with_explicit_token(self, clean_env):
        static, ws = resolve_auth("oauth_token", token="explicit-token")
        assert static == "explicit-token"
        assert ws is None

    def test_oauth_token_from_env(self, clean_env, monkeypatch):
        monkeypatch.setenv("LAKEBASE_API_TOKEN", "env-token")
        static, ws = resolve_auth("oauth_token")
        assert static == "env-token"
        assert ws is None

    def test_user_oauth_uses_sdk(self, clean_env, mock_workspace_client):
        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client) as make_ws:
            static, ws = resolve_auth(
                "user_oauth", profile="tanveer", workspace_host="https://ws.example.com",
            )
        assert static is None
        assert ws is mock_workspace_client
        make_ws.assert_called_once_with(host="https://ws.example.com", profile="tanveer")

    def test_sp_oauth_requires_client_id_and_secret(self, clean_env):
        with pytest.raises(ValueError, match="auth_mode='sp_oauth' requires: client_id, client_secret"):
            resolve_auth("sp_oauth")
        with pytest.raises(ValueError, match="auth_mode='sp_oauth' requires: client_secret"):
            resolve_auth("sp_oauth", client_id="cid")

    def test_sp_oauth_uses_sdk(self, clean_env, mock_workspace_client):
        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client) as make_ws:
            static, ws = resolve_auth(
                "sp_oauth",
                client_id="cid", client_secret="secret",
                workspace_host="https://ws.example.com",
            )
        assert static is None
        assert ws is mock_workspace_client
        make_ws.assert_called_once_with(
            host="https://ws.example.com", client_id="cid", client_secret="secret",
        )

    def test_bogus_mode_raises(self, clean_env):
        with pytest.raises(ValueError, match="unknown auth_mode: 'wat'"):
            resolve_auth("wat")


class TestMakeWs:
    def test_filters_falsy_kwargs(self):
        with patch("databricks.sdk.WorkspaceClient") as ws_cls:
            _make_ws(host="", profile=None, client_id="cid", client_secret="secret")
        ws_cls.assert_called_once_with(client_id="cid", client_secret="secret")

    def test_no_kwargs_means_ambient_auth(self):
        with patch("databricks.sdk.WorkspaceClient") as ws_cls:
            _make_ws()
        ws_cls.assert_called_once_with()
