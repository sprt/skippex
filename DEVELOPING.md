## Running the tests

If using pyenv, ensure all minor Python versions >=3.6 are referenced in
`.python-version` (and reload your shell after making the change), then:

```console
$ tox
```

## Releasing

Prerequisites:

 1. Ensure we're on branch main (`git branch`).
 1. Ensure the repo is clean (`git status`).
 1. Ensure all the tests pass.

```console
$ poetry version <bump>
$ git add pyproject.tml && git commit -m "vX.X.X release"
$ git tag -a vX.X.X -m "vX.X.X release"

$ # Create Docker image
$ docker build -t ghcr.io/sprt/skippex:vX.X.X .
$ # TODO: Test image.
$ # Push to repo.
$ git push --follow-tags
$ # Publish on PYPI.
$ poetry publish --build
$ # Publish on GitHub Container Registry.
$ docker push ghcr.io/sprt/skippex:vX.X.X
```

## Building the Docker image

```console
$ docker build -t ghcr.io/sprt/skippex:untagged .
```
