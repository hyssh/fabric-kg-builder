# Project landing page

Source for [hyssh.github.io/fabric-kg-builder](https://hyssh.github.io/fabric-kg-builder/).
This is a zero-dependency static page: no build step or external fonts, scripts,
images, or runtime requests.

- `index.html`: semantic, anchor-linked sections; conceptual inline SVG topology;
  exact Clawpilot light/dark tokens and early theme detection.
- `styles.css`: responsive layout, Segoe UI typography, focus states, and
  reduced-motion support. Component colors use the inline theme tokens.
- `app.js`: progressive mobile navigation and copy buttons with live feedback.
- `images/surface-go-2-data-agent.png`: unchanged, user-supplied Data Agent
  example, shown in full at reduced size with a link to the original image.
- `.nojekyll`: keeps GitHub Pages from processing the site with Jekyll.

All substantive content and native disclosure controls work without JavaScript.
JavaScript adds a mobile menu that closes on selection, Escape, or outside click.
Copy failure tells readers to select and copy the code manually.

## Local preview

From the repository root:

```bash
python -m http.server --directory site 8080
```

Open `http://localhost:8080`. The theme follows the system preference; use
`?clawpilotTheme=light` or `?clawpilotTheme=dark` for explicit previews.
Other parameter values fall back to the system preference.

## Editing and checks

Keep public content aligned with the root README and changelog. Label 0.2.7 as
a development version, distinguish local derivation from remote publication,
and do not present the topology illustration as real data or quality evidence.

```bash
node --check site/app.js
```

Check at 375px and 1440px in both themes, with and without JavaScript. Verify
keyboard focus, the skip link, anchor history, mobile menu, and clipboard success
and failure. Native Leiden examples require an existing sealed L4 input.

## Publishing

The repository's GitHub Pages workflow handles publication. Configure Pages to
use GitHub Actions in repository settings before publishing; editing this folder
alone does not establish that the public site is live.
