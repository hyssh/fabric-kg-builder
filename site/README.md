# Project landing page

This folder is the source of the published project site at
**https://hyssh.github.io/fabric-kg-builder/**.

It is a self-contained static site — no build step, no dependencies:

- `index.html` — the page
- `styles.css` — styling (dark, modern, responsive)
- `app.js` — mobile nav toggle + copy-to-clipboard
- `.nojekyll` — tells GitHub Pages to serve files as-is

## Publishing

Deployment is automated by `.github/workflows/pages.yml` on every push to `main`
that touches `site/**`.

**One-time setup:** in the GitHub repo, go to **Settings → Pages → Build and
deployment** and set **Source = "GitHub Actions"**. The next push (or a manual
*Run workflow*) publishes the site.

## Local preview

```bash
# from the repo root
python -m http.server -d site 8080
# open http://localhost:8080
```

## Editing

Plain HTML/CSS — edit `index.html` and `styles.css` directly. Keep it dependency-free
so it stays fast and easy to host.
