## Running the tests

If using pyenv, ensure all minor Python versions >=3.6 are referenced in
`.python-version` (and reload your shell after making the change), then:

```console
$ tox
```

## Releasing

```console
$ python release.py
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
