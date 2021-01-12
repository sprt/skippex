# Skippex

Skippex skips intros automatically for you on Plex, with support for the
Chromecast.

**IMPORTANT NOTE**: This is still pretty much beta software. Except bugs and
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

Once that's done, you can simply start Skippex and it'll start monitoring your
playback sessions and automatically skip intros for you:

```shell
$ skippex
```

Et voilà!

## Things to know

 * **Clients need to have "Advertize as player" enabled.**
 * Only skips once per playback session.
 * Only tested for one account on the local network.
 * Might only work on the local network for standard Plex clients.
 * Most likely works on the local network for Chromecasts.
 * Solely based on the intro markers detected by Plex; Skippex does not attempt
   to detect intros itself.

## Tested and supported players

 * Plex Web App
 * Plex for iOS (both iPhone and iPad)
 * Chromecast v3

## Known issues

 * With a Chromecast, when seeking to a position, the WebSocket only receives
   the notification 10 seconds later. Likewise, the HTTP API starts returning
   the correct position only after 10 seconds. This means that if, before the
   intro, the user seeks to within 10 seconds of the intro, they may view it for
   a few seconds (before the notification comes in and saves us).

   One workaround would be to listen to Chromecast status updates using
   `pychromecast`, but that would necessitate a rearchitecture of the code.
