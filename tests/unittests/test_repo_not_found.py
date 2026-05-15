import unittest
from unittest import mock
from minware_singer_utils import GitLocalRepoNotFoundException
from minware_singer_utils.gitlocal import GitLocalException
import tap_azure_git


class TestRepoNotFoundHandling(unittest.TestCase):
    """Test that missing/inaccessible repos are skipped in do_sync."""

    def _make_catalog(self, stream_ids):
        """Build a minimal catalog with the given stream IDs selected."""
        streams = []
        for sid in stream_ids:
            streams.append(
                {
                    "tap_stream_id": sid,
                    "schema": {},
                    "key_properties": ["id"],
                    "metadata": [{"breadcrumb": [], "metadata": {"selected": True}}],
                }
            )
        return {"streams": streams}

    def _make_config(self, repos):
        return {
            "repository": repos,
            "start_date": "2024-01-01",
            "org": "testorg",
            "access_token": "token",
            "user_name": "user",
        }

    @mock.patch("tap_azure_git.GitLocal")
    @mock.patch("tap_azure_git.validate_dependencies")
    @mock.patch("tap_azure_git.get_selected_streams", return_value=["commits"])
    @mock.patch("singer.write_schema")
    @mock.patch("singer.write_state")
    @mock.patch.dict(
        tap_azure_git.SYNC_FUNCTIONS, {"commits": mock.MagicMock(return_value={})}
    )
    def test_skips_repo_on_api_not_found(
        self,
        mock_write_state,
        mock_write_schema,
        mock_get_selected,
        mock_validate,
        mock_git_local_cls,
    ):
        """When a sync function raises NotFoundException (API 404), the repo
        is skipped and sync continues to the next repo."""
        sync_func = tap_azure_git.SYNC_FUNCTIONS["commits"]
        sync_func.side_effect = [
            tap_azure_git.NotFoundException(
                "HTTP-error-code: 404, Error: The resource you have specified cannot be found"
            ),
            {},  # second repo succeeds
        ]

        config = self._make_config(["project/deleted-repo", "project/good-repo"])
        catalog = self._make_catalog(["commits"])

        tap_azure_git.do_sync(config, {}, catalog)

        self.assertEqual(sync_func.call_count, 2)

    @mock.patch("tap_azure_git.GitLocal")
    @mock.patch("tap_azure_git.validate_dependencies")
    @mock.patch("tap_azure_git.get_selected_streams", return_value=["commits"])
    @mock.patch("singer.write_schema")
    @mock.patch("singer.write_state")
    @mock.patch.dict(
        tap_azure_git.SYNC_FUNCTIONS, {"commits": mock.MagicMock(return_value={})}
    )
    def test_state_not_written_for_api_not_found_repo(
        self,
        mock_write_state,
        mock_write_schema,
        mock_get_selected,
        mock_validate,
        mock_git_local_cls,
    ):
        """Sync function raised but didn't crash the job."""
        sync_func = tap_azure_git.SYNC_FUNCTIONS["commits"]
        sync_func.side_effect = tap_azure_git.NotFoundException(
            "HTTP-error-code: 404, Error: The resource you have specified cannot be found"
        )

        config = self._make_config(["project/deleted-repo"])
        catalog = self._make_catalog(["commits"])

        tap_azure_git.do_sync(config, {}, catalog)

        self.assertEqual(sync_func.call_count, 1)

    @mock.patch("tap_azure_git.GitLocal")
    @mock.patch("tap_azure_git.validate_dependencies")
    @mock.patch("tap_azure_git.get_selected_streams", return_value=["commits"])
    @mock.patch("singer.write_schema")
    @mock.patch("singer.write_state")
    @mock.patch.dict(
        tap_azure_git.SYNC_FUNCTIONS, {"commits": mock.MagicMock(return_value={})}
    )
    def test_skips_repo_when_not_found_on_clone(
        self,
        mock_write_state,
        mock_write_schema,
        mock_get_selected,
        mock_validate,
        mock_git_local_cls,
    ):
        """When GitLocalRepoNotFoundException is raised during sync, the repo
        is skipped and sync continues to the next repo."""
        sync_func = tap_azure_git.SYNC_FUNCTIONS["commits"]
        sync_func.side_effect = [
            GitLocalRepoNotFoundException("repo not found"),
            {},  # second repo succeeds
        ]

        config = self._make_config(["project/missing-repo", "project/good-repo"])
        catalog = self._make_catalog(["commits"])

        tap_azure_git.do_sync(config, {}, catalog)

        self.assertEqual(sync_func.call_count, 2)

    @mock.patch("tap_azure_git.GitLocal")
    @mock.patch("tap_azure_git.validate_dependencies")
    @mock.patch("tap_azure_git.get_selected_streams", return_value=["commits"])
    @mock.patch("singer.write_schema")
    @mock.patch("singer.write_state")
    @mock.patch.dict(
        tap_azure_git.SYNC_FUNCTIONS, {"commits": mock.MagicMock(return_value={})}
    )
    def test_other_exceptions_still_raise(
        self,
        mock_write_state,
        mock_write_schema,
        mock_get_selected,
        mock_validate,
        mock_git_local_cls,
    ):
        """A generic GitLocalException (not repo-not-found) should still propagate."""
        sync_func = tap_azure_git.SYNC_FUNCTIONS["commits"]
        sync_func.side_effect = GitLocalException("network timeout")

        config = self._make_config(["project/some-repo"])
        catalog = self._make_catalog(["commits"])

        with self.assertRaises(GitLocalException):
            tap_azure_git.do_sync(config, {}, catalog)

    @mock.patch("tap_azure_git.GitLocal")
    @mock.patch("tap_azure_git.validate_dependencies")
    @mock.patch("tap_azure_git.get_selected_streams", return_value=["commits"])
    @mock.patch("singer.write_schema")
    @mock.patch("singer.write_state")
    @mock.patch.dict(
        tap_azure_git.SYNC_FUNCTIONS, {"commits": mock.MagicMock(return_value={})}
    )
    def test_other_azure_exceptions_still_raise(
        self,
        mock_write_state,
        mock_write_schema,
        mock_get_selected,
        mock_validate,
        mock_git_local_cls,
    ):
        """A non-404 AzureException (e.g. AuthException) should still propagate."""
        sync_func = tap_azure_git.SYNC_FUNCTIONS["commits"]
        sync_func.side_effect = tap_azure_git.AuthException("forbidden")

        config = self._make_config(["project/some-repo"])
        catalog = self._make_catalog(["commits"])

        with self.assertRaises(tap_azure_git.AuthException):
            tap_azure_git.do_sync(config, {}, catalog)


if __name__ == "__main__":
    unittest.main()
