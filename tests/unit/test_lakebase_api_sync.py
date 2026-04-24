"""Regression tests for `LakebaseDataApiClient` after the `_common.py`
refactor. Locks down URL/auth behaviour and paginate semantics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from lakebase_utils.lakebase_api import LakebaseDataApiClient


pytestmark = pytest.mark.unit


def _mock_response(json_payload):
    resp = MagicMock()
    resp.json.return_value = json_payload
    resp.raise_for_status.return_value = None
    return resp


class TestSyncClientConstruction:
    def test_url_from_parts(self, clean_env):
        c = LakebaseDataApiClient(
            host="ep-x.example.com", workspace_id="9",
            database="db", auth_mode="oauth_token", token="t",
        )
        assert c.base_url == "https://ep-x.example.com/api/2.0/workspace/9/rest/db"

    def test_missing_url_raises(self, clean_env):
        with pytest.raises(ValueError, match="base URL is required"):
            LakebaseDataApiClient(auth_mode="oauth_token", token="t")

    def test_missing_token_for_oauth_mode(self, clean_env):
        with pytest.raises(ValueError, match="auth_mode='oauth_token' requires"):
            LakebaseDataApiClient(base_url="https://x", auth_mode="oauth_token")

    def test_bogus_mode(self, clean_env):
        with pytest.raises(ValueError, match="unknown auth_mode"):
            LakebaseDataApiClient(base_url="https://x", auth_mode="wat")


class TestSyncGet:
    def test_oauth_token_mode_sends_bearer(self, clean_env):
        c = LakebaseDataApiClient(base_url="https://x", auth_mode="oauth_token", token="tok")
        with patch.object(c._session, "get", return_value=_mock_response([{"id": 1}])) as get:
            rows = c.get("public", "widgets", params={"limit": 5})
        assert rows == [{"id": 1}]
        get.assert_called_once()
        kwargs = get.call_args.kwargs
        assert kwargs["headers"]["Authorization"] == "Bearer tok"
        assert kwargs["headers"]["Accept"] == "application/json"
        assert kwargs["params"] == {"limit": 5}
        assert get.call_args.args[0] == "https://x/public/widgets"


class TestSyncPaginate:
    def _client_with_pages(self, pages):
        c = LakebaseDataApiClient(base_url="https://x", auth_mode="oauth_token", token="tok")
        responses = [_mock_response(p) for p in pages]
        c._session.get = MagicMock(side_effect=responses)
        return c

    def test_stops_on_short_page(self, clean_env):
        c = self._client_with_pages([
            [{"id": 1}, {"id": 2}],
            [{"id": 3}],           # short → stop
        ])
        rows = list(c.paginate("public", "t", page_size=2))
        assert [r["id"] for r in rows] == [1, 2, 3]
        assert c._session.get.call_count == 2

    def test_strips_caller_limit_offset(self, clean_env):
        c = self._client_with_pages([[{"id": 1}]])
        list(c.paginate("public", "t", page_size=10,
                        params={"limit": 999, "offset": 42, "select": "id"}))
        sent_params = c._session.get.call_args.kwargs["params"]
        assert sent_params == {"select": "id", "limit": 10, "offset": 0}

    def test_honors_max_rows(self, clean_env):
        c = self._client_with_pages([
            [{"id": 1}, {"id": 2}, {"id": 3}],
            [{"id": 4}, {"id": 5}, {"id": 6}],
        ])
        rows = list(c.paginate("public", "t", page_size=3, max_rows=4))
        assert [r["id"] for r in rows] == [1, 2, 3, 4]

    def test_empty_first_page_returns_nothing(self, clean_env):
        c = self._client_with_pages([[]])
        assert list(c.paginate("public", "t", page_size=3)) == []

    def test_fetch_all_is_list(self, clean_env):
        c = self._client_with_pages([
            [{"id": 1}, {"id": 2}],
            [],
        ])
        rows = c.fetch_all("public", "t", page_size=2)
        assert rows == [{"id": 1}, {"id": 2}]


class TestSyncEndpointPathAuth:
    """`endpoint_path` makes the client mint a JWT via
    `WorkspaceClient.postgres.generate_database_credential`, which is required
    under Databricks notebook ambient auth."""

    def test_endpoint_path_uses_generate_database_credential(self, clean_env, mock_workspace_client):
        cred = MagicMock(token="endpoint-jwt-token", expire_time=None)
        mock_workspace_client.postgres.generate_database_credential.return_value = cred

        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client):
            c = LakebaseDataApiClient(
                base_url="https://x",
                auth_mode="user_oauth",
                endpoint_path="projects/p/branches/b/endpoints/e",
            )
        with patch.object(c._session, "get", return_value=_mock_response([])) as get:
            c.get("public", "t")

        mock_workspace_client.postgres.generate_database_credential.assert_called_once_with(
            endpoint="projects/p/branches/b/endpoints/e",
        )
        # config.authenticate() must NOT be called when endpoint_path is present.
        mock_workspace_client.config.authenticate.assert_not_called()
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer endpoint-jwt-token"

    def test_endpoint_path_caches_until_expiring(self, clean_env, mock_workspace_client):
        # Credential with no expire_time → always considered expiring → re-mint per call.
        # Give it an expire_time far in the future to verify caching.
        future = MagicMock()
        future.seconds = int(datetime.now(timezone.utc).timestamp()) + 3600
        cred = MagicMock(token="cached-token", expire_time=future)
        mock_workspace_client.postgres.generate_database_credential.return_value = cred

        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client):
            c = LakebaseDataApiClient(
                base_url="https://x",
                auth_mode="user_oauth",
                endpoint_path="projects/p/branches/b/endpoints/e",
            )

        with patch.object(c._session, "get", return_value=_mock_response([])):
            c.get("public", "t")
            c.get("public", "t")
            c.get("public", "t")
        assert mock_workspace_client.postgres.generate_database_credential.call_count == 1

    def test_without_endpoint_path_falls_back_to_authenticate(self, clean_env, mock_workspace_client):
        with patch("lakebase_utils._common._make_ws", return_value=mock_workspace_client):
            c = LakebaseDataApiClient(base_url="https://x", auth_mode="user_oauth")
        with patch.object(c._session, "get", return_value=_mock_response([])) as get:
            c.get("public", "t")

        mock_workspace_client.config.authenticate.assert_called()
        mock_workspace_client.postgres.generate_database_credential.assert_not_called()
        assert get.call_args.kwargs["headers"]["Authorization"] == "Bearer mocked-sdk-token"
