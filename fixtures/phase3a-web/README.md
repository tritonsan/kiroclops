# Local panel fixture

`reference/` is private engineering input. Never put it in the model workspace.
`target/` is the clean interactive app. Only `src/panel-motion.js` is editable
in the first protocol; the controller is used by both ordinary play and capture.
The handle, bookmark and star are genuine UI elements, not coordinate markers.
All assets are local. Motion uses elapsed time; there is no capture-only path.

Open through a local HTTP server; no hosted product or AWS account is required.
No browser is launched by importing the Python adapter.

