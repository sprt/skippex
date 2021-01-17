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
 1. Ensure all the tests pass (`tox`).

```console
$ poetry version <bump>
$ git add pyproject.tml && git commit -m "vX.X.X release"
$ git tag -a vX.X.X -m "vX.X.X release"

$ # Create Docker image.
$ docker build -t ghcr.io/sprt/skippex:X.X.X .
$ # Test image.
$ docker run --rm --network host --entrypoint sh ghcr.io/sprt/skippex:X.X.X -c ". /venv/bin/activate && python -m pytest"
$ # Tag image with "latest".
$ docker tag ghcr.io/sprt/skippex:X.X.X ghcr.io/sprt/skippex:latest
$ # Publish on PYPI.
$ poetry publish --build
$ # Publish on GitHub Container Registry.
$ docker push ghcr.io/sprt/skippex:X.X.X
$ docker push ghcr.io/sprt/skippex:latest
$ # Push to git repo.
$ git push --follow-tags
```

## Building a development Docker image

```console
$ docker build -t ghcr.io/sprt/skippex:dev .
```

And later running this dev image:

```console
$ docker run --rm -v skippex-dev:/config --network host ghcr.io/sprt/skippex:dev run
```

Running the tests inside it:

```console
$ docker run --rm --network host --entrypoint sh ghcr.io/sprt/skippex:dev -c ". /venv/bin/activate && python -m pytest"
```

Inspecting it with a shell:

```console
$ docker run --rm --network host --entrypoint sh -it ghcr.io/sprt/skippex:dev
```
