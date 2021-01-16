# Skippex

Skippex skips intros automatically for you on Plex, with support for the
Chromecast.

**IMPORTANT NOTE**: This is still pretty much beta software. Expect bugs and
please report them!

## Installation

**Docker installation coming soon.**

As this is a Python application, I personally recommend using [pipx][pipx],
which will install Skippex in its own virtual environment:

```shell
$ pipx install skippex
```

Or you can just use pip:

```shell
$ pip install --user skippex
```

[pipx]: https://pipxproject.github.io/pipx/

## Usage

The first time you use Skippex, you'll first have to authorize the application
with Plex using the following command. This will open a new tab in your Web
browser allowing you to authenticate and authorize the application to access
your Plex account.

```shell
$ skippex auth
```

Once that's done, you can simply run Skippex and it'll start monitoring your
playback sessions and automatically skip intros for you on supported devices:

```shell
$ skippex run
```

Et voilà! When this command says "Ready", Skippex is monitoring your shows and
will automatically skip intros for you.

## Things to know

 * **Clients need to have "Advertize as player" enabled.**
 * Only skips once per playback session.
 * Only tested for one account on the local network.
 * Might only work on the local network for standard Plex clients.
 * Only works on the local network for Chromecasts.
 * Solely based on the intro markers detected by Plex; Skippex does not attempt
   to detect intros itself.

## Tested and supported players

 * Plex Web App
 * Plex for iOS (both iPhone and iPad)
 * Chromecast v3

The NVIDIA SHIELD might be supported as well, but I don't have one so I can't
test it. Other players might also be supported. In any case, please inform me
by [creating a new issue][new_issue], so I can add your player to this list.

[new_issue]: https://github.com/sprt/skippex/issues/new

## Known issues

 * With a Chromecast, when seeking to a position, the WebSocket only receives
   the notification 10 seconds later. Likewise, the HTTP API starts returning
   the correct position only after 10 seconds. This means that if, before the
   intro, the user seeks to within 10 seconds of the intro, they may view it for
   a few seconds (before the notification comes in and saves us).

   One workaround would be to listen to Chromecast status updates using
   `pychromecast`, but that would necessitate a rearchitecture of the code.
