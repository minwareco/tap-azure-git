"""
Provide stub minware_singer_utils module when the real package is not installed.
This allows unit tests to run in CI without access to the private repo.
"""
import sys
from types import ModuleType

try:
    import minware_singer_utils  # noqa: F401
except ImportError:
    stub = ModuleType('minware_singer_utils')
    gitlocal_stub = ModuleType('minware_singer_utils.gitlocal')

    class GitLocalException(Exception):
        pass

    class GitLocalRepoNotFoundException(GitLocalException):
        pass

    class SecureLogger:
        def __init__(self, logger):
            self._logger = logger

        def __getattr__(self, name):
            return getattr(self._logger, name)

    class GitLocal:
        pass

    stub.GitLocal = GitLocal
    stub.GitLocalException = GitLocalException
    stub.GitLocalRepoNotFoundException = GitLocalRepoNotFoundException
    stub.SecureLogger = SecureLogger

    gitlocal_stub.GitLocalException = GitLocalException
    gitlocal_stub.GitLocalRepoNotFoundException = GitLocalRepoNotFoundException

    sys.modules['minware_singer_utils'] = stub
    sys.modules['minware_singer_utils.gitlocal'] = gitlocal_stub
