Things to know:

 * **Clients need to have "Advertize as player" enabled.**
 * Only skips once per playback session.
 * Only tested for one account on the local network.
 * Might only work on the local network for standard Plex clients.
 * Most likely works on the local network for Chromecasts.
 * Solely based on the intro markers detected by Plex; Skippex does not attempt
   to detect intros itself.

Tested and supported clients:

 * Plex Web App
 * Plex for iOS (both iPhone and iPad)
 * Chromecast v3

Known issues:

 * With a Chromecast, when seeking to a position, the WebSocket only receives
   the notification 10 seconds later. Likewise, the HTTP API starts returning
   the correct position only after 10 seconds. This means that if, before the
   intro, the user seeks to within 10 seconds of the intro, they may view it for
   a few seconds (before the notification comes in and saves us).

   One workaround would be to listen to Chromecast status updates using
   `pychromecast`, but that would necessitate a rearchitecture of the code.
