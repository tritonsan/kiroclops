# Independent polyline example

This fixture is separate from the 32-record panel/SVG benchmark. Its ordinary
HTML/SVG application supports dragging, adding and removing up to six idea cards.
The protected renderer draws actual multipart SVG polylines; the editable
`src/layout-motion.js` module controls live card motion through `advanceLayout`.

- `reference/` is private engineering input. Only its recorded pixels and input
  receipts may enter a model session.
- `target/` starts with a visibly slower response. Its renderer and appearance
  match the independent reference; only the motion module differs.
- Use explicit connection selections with `method: "polyline"`, a stroke RGB
  sampled from a raw frame, and the existing confidence/search limits. The main
  benchmark's automatic straight-line selector is unchanged.
- Link colors distinguish real visible route categories. They are not timing,
  identity or coordinate telemetry. The shared core deliberately rejects
  ambiguous same-color branches and disconnected strokes.
- Private SVG geometry receipts use the actual geometry API and never enter
  pixel tracking. Painted visible endpoints and card borders remain image-only
  measurements; native attachment and visible attachment are separate checks.

## Visible route ports

The first raw control set (`polyline-capture-01`) exposed a rendering defect:
after the fourth card appears, its green route paints over the blue route near
the central card. A hidden endpoint cannot be counted as a visible connection.
Those original recordings and their failed-observability finding are preserved.

The current renderer uses fixed ordinary side ports: `b=-16`, `c=0`, `d=16`,
`e=-8`, `f=8` pixels vertically relative to the central card. Each path genuinely
starts at the card center, reaches its port through an interior vertical segment,
then exits horizontally. Same-side visible ports are at least 8px apart for 3px
strokes. This preserves real center attachment and the existing private oracle;
the interior segment is covered by the opaque card. It carries no timing or
coordinate telemetry and does not depend on test scenarios.

The change prevents the observed shared entry segment; it is not a claim that
every possible moving route is unoccluded. Fresh `polyline-capture-02` recordings,
pixel measurement and independent review are required before acceptance.

No runtime qualification or successful transfer is claimed by these source
files. Headless positive, detached-stroke and ambiguity controls must be recorded
and independently reviewed before making such claims. Preserve failed controls.
