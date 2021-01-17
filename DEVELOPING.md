## Running the tests

If using pyenv, ensure all minor Python versions >=3.6 are referenced in
`.python-version`, then:

```console
$ tox
```

## Releasing

Run the tests, then:

```console
$ poetry version <bump>
$ git add pyproject.tml && git commit -m "vX.X.X release"
$ git tag -a vX.X.X -m "vX.X.X release"
$ git push --follow-tags
$ poetry publish --build
```

TODO: Docker build and publish.

## Building the Docker image

```console
$ docker build -t sprt/skippex .
```
