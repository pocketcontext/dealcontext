# DealContext brand exploration

100 selection-stage vector identities, numbered 001–100. This standalone review
page follows PocketContext's green/cream palette, IBM Plex Sans typography, and
brand asset formats. It does not change the application reader or its adopted
identity. The concept families explore the application’s own domain and workflows.

Each direction has 9 SVGs, 9 PNGs and a ZIP kit: logo, favicon, outlined wordmark,
social card, LinkedIn logo/banner and Luma logo/banner/social. Favicon strokes
are increased 20%; solid geometry receives a small outline. Cards show 16, 32
and 48px previews. Refine the chosen design optically before application release.

## Rebuild

From this directory, use Python with `fonttools` and `brotli` installed, plus
Node.js. `render.mjs` uses the existing website checkout's installed `sharp`.

```sh
python generate.py
node render.mjs
python package.py
python -m http.server 8769 --bind 127.0.0.1 --directory dist
```

Generated `dist/` stays ignored. Serve only `dist/`, never the repository root.
To share temporarily, run `cloudflared tunnel --url http://127.0.0.1:8769`.
The public tunnel lasts only while that process and the HTTP server run.
Only generated design files are exposed; there is no application data access.

Favorites live in browser local storage and are not sent anywhere. Copy the
shortlist into the conversation to communicate choices. Each concept supports
an anchor such as `#concept-013`. Assets contain no externally loaded resources.
Wordmarks and social lettering are outlined from the bundled licensed font.

Build validation checks concept numbering, unique SVG geometry and export generation.
Review the gallery in a browser before adopting a final identity.
