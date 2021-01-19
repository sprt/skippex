import argparse
from getpass import getpass
import logging
import logging.config
import re
from shlex import quote
import subprocess
import sys
from types import TracebackType
from typing import List, NamedTuple, Optional, Type, cast

import subprocess_tee


# Disable third-party loggers.
# Make sure this is done before defining any local logger.
logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': True,
})

logger = logging.getLogger(__name__)

# Reference: https://semver.org/
re_semver = re.compile(r'^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$')


class Rollback(Exception):
    pass


class Command(NamedTuple):
    commit: str
    rollback: Optional[str]
    pure: bool


class Transaction:
    def __init__(self) -> None:
        self._executed: List[Command] = []
        self.committed: Optional[bool] = None

    def _execute(self, cmd: str) -> subprocess.CompletedProcess:
        logger.info(f'--> {cmd}')
        p = subprocess_tee.run(cmd, shell=True)
        # check=True isn't supported by subprocess_tee.run():
        # https://github.com/pycontribs/subprocess-tee/issues/26
        p.check_returncode()
        return p

    def execute(self, commit: str, rollback: Optional[str] = None, pure: bool = False) -> subprocess.CompletedProcess:
        if pure and rollback:
            raise ValueError('cannot both be pure and have a rollback')

        p = self._execute(commit)
        command = Command(commit=commit, rollback=rollback, pure=pure)
        self._executed.append(command)

        return p

    def __enter__(self) -> 'Transaction':
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_value: Optional[BaseException],
        exc_traceback: Optional[TracebackType],
    ) -> bool:
        if not exc_type:
            self.committed = True
            logger.info('Transaction committed')
            return True

        if exc_type is subprocess.CalledProcessError:
            exc_value = cast(subprocess.CalledProcessError, exc_value)
            logger.warn(f'Command {exc_value.cmd!r} exited with status {exc_value.returncode}')
        else:
            logger.warn('Exception raised:', exc_info=True)

        logger.info('Rolling back transaction...')
        num_rollback_fails = 0

        while self._executed:
            command = self._executed.pop()

            if command.pure:
                logger.info(f'Command {command.commit!r} is pure, no rollback')
            elif not command.rollback:
                logger.error(f'Command {command.commit!r} has no rollback')
            else:
                try:
                    logger.info(f'Rolling back command {command.commit!r}...')
                    self._execute(command.rollback)
                except BaseException as e:
                    if isinstance(e, subprocess.CalledProcessError):
                        logger.error(
                            f'Rollback of command {command.commit!r} failed: '
                            f'{command.rollback!r} exited with status {e.returncode}'
                        )
                    else:
                        logger.exception(f'Rollback of command {command.commit!r} failed:')
                    num_rollback_fails += 1
                else:
                    logger.info(f'Rollback of command {command.commit!r} succeeded')

        logger.info('Transaction rolled back')
        if num_rollback_fails:
            logger.error(f'But failed to rollback {num_rollback_fails} commands')

        self.committed = False
        return True


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.DEBUG,
        format='[%(levelname)-8s] %(message)s',
    )

    parser = argparse.ArgumentParser()
    parser.add_argument('version', help='version passed to poetry version')
    args = parser.parse_args()

    pypi_username = input('PyPI username: ')
    pypi_password = getpass('PyPI password: ')

    tx = Transaction()

    with tx:
        # Prerequisites:

        # Ensure we're on the main branch.
        tx.execute('[ "$(git rev-parse --symbolic-full-name --abbrev-ref HEAD)" = "main" ]', pure=True)
        # Ensure the repo is clean.
        tx.execute('[ -z "$(git status --porcelain)" ]', pure=True)
        # Ensure the tests pass.
        tx.execute('PY_COLORS=1 tox -- --color=yes', pure=True)

        # Actual release process:

        # Bump the version in pyproject.toml.
        p_poetry_version = tx.execute(
            f'poetry version {quote(args.version)}',
            rollback='git checkout HEAD -- pyproject.toml',
        )

        version = p_poetry_version.stdout.split()[-1]
        assert re_semver.match(version), f'not a valid semver: {version}'
        git_tag = f'v{version}'
        docker_tag_version = f'ghcr.io/sprt/skippex:{version}'
        docker_tag_latest = f'ghcr.io/sprt/skippex:latest'

        # Commit pyproject.toml.
        tx.execute(
            f'git commit -m "v{version} release" pyproject.toml',
            rollback='git checkout HEAD^ -- pyproject.toml',
        )

        # Tag the commit.
        tx.execute(
            f'git tag -a {git_tag} -m "v{version} release"',
            rollback=f'git tag -d {git_tag}',
        )

        # Create the Docker image.
        tx.execute(
            f'docker build -t {docker_tag_version} .',
            rollback=f'docker rmi {docker_tag_version}',
        )

        # Test the image.
        tx.execute(
            f'docker run --rm --network host --entrypoint sh {docker_tag_version}'
            f' -c ". /venv/bin/activate && python -m pytest"',
            pure=True,
        )

        # Tag the image with "latest".
        # TODO: Reassign the latest tag to its original image in rollback?
        tx.execute(
            f'docker tag {docker_tag_version} {docker_tag_latest}',
            rollback=f'docker rmi {docker_tag_latest}'
        )

        # Publish on PyPI.
        tx.execute(
            f'yes | poetry publish --build --username {pypi_username!r}'
            f' --password {quote(pypi_password)}'
        )

        # Publish on GitHub Container Registry.
        tx.execute(f'docker push {docker_tag_version}')
        tx.execute(f'docker push {docker_tag_latest}')
        # TODO: Delete older local tags?

        # Push to git repo.
        tx.execute('git push --follow-tags')

    sys.exit(int(not tx.committed))
