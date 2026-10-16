# VideoMind media acquisition via PolyDL / dy-downloader

Experimental implementation, 2026-10-16. All changes are isolated; not deployed to production.

## Purpose
Stop maintaining Douyin SSR as primary acquisition. Reuse two independent upstream
MIT-declared Node packages from the dYm ecosystem:
- \`polydl@0.5.3\`, currently used by dYm desktop; package includes a LICENSE.
- \`dy-downloader@0.2.3\`, used by dYm-web; package declares MIT in its manifest
  but its npm archive did not contain a LICENSE file in our spot check.

They are installed as dependencies, not copied or modified; dYm app source itself
is GPLv3 and was not imported.

## Architecture
Android capture -> FastAPI job -> analyze_video -> media_providers.py
   -> Node resolve.mjs -> PolyDL or dy-downloader
   -> local temporary video/images -> video-first Qwen3-VL + OCR -> Knowledge DB.

## Isolated installation
Use Node 20+:

    cd src-server/media_bridge
    npm ci --ignore-scripts --no-audit --no-fund

Do not commit node_modules or Cookie files.

Set on a protected server-only environment:

    DOUYIN_MEDIA_PROVIDER=auto
    DOUYIN_DOWNLOADER_PACKAGE=polydl
    DOUYIN_NODE_BIN=/path/to/node-22
    DOUYIN_COOKIE_FILE=/private/path/to/netscape_cookies.txt

Modes:
- ssr (default): old pipeline unchanged
- dy: use upstream provider only; on failure mark failed and avoid fake success
- auto: try upstream provider, then fall back to original SSR if unavailable

Cookies are read from a 0600 file, translated to a Cookie header within the
Node process, and never passed on a command line or included in JSON results.
Only user-supplied Douyin URLs and original media are supported.
Temporary downloads are stored under \`data/tmp/dy-provider\`.
Downloaded media is validated with ffprobe. Output destinations are restricted
to the media temp root via a server-side environment guard.

## Tests run
- Baseline reliability failures: 4/4
- Fixed reliability + feature + provider regression tests: 11/11 passing
- Local isolated VLM frame-coverage benchmarking done previously: 3 videos, 6 runs.
- Upstream live metadata acquisition of historical test IDs
  7637313450288401716 and 7601100018069589289:
  dy-downloader: DETAIL_EMPTY_ID; polydl sample: DETAIL_UNAVAILABLE.
  No real media downloaded in this session by the borrowed providers.
  Cookie expiration, content availability, signed request behavior and/or
  remote connectivity remain unverified causes; do not call this end-to-end success.

## Known risks and next validation steps
- End-to-end resolution of a currently accessible, user-authorized Douyin video.
- Safe Cookie-refresh workflow and handling platform access limitations.
- Full Android -> API -> authenticated download -> GPU -> Knowledge DB integration.
- Attribution/licensing review for distributable builds.
- Exception and cleanup handling for persistent provider failures.
- UI must distinguish collected / resolving / downloaded / analyzed / failed.