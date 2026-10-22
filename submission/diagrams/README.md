# Diagrams

| file | content |
|---|---|
| `architecture.png` / `.pdf` | System architecture: local KiroClops harness (Kiro agent, MCP server, OpenCV 5 measurement core, capture, evidence) and the AWS analysis service (Cognito, API Gateway, Lambda, S3, DynamoDB, SQS, Graviton4 worker on the COOL Marketplace image), with a numbered flow. |
| `agent-loop.png` / `.pdf` | The perception, decision and action loop of one agent session, the exit after sealing, the recorded `v6p01` timeline and the human-control mechanisms. |

The diagrams are HTML pages (`*.html`, shared `diagram.css` and `diagram.js`) rendered by Chromium:

```powershell
.\.venv-local\Scripts\python.exe submission\diagrams\build_diagrams.py
```

`diagram.js` draws every arrow from the boxes' measured positions and audits the page before export: each glyph inside its box and the canvas, no text below 16 px, no overlapping boxes, badges or labels, no arrow through an unrelated box, no touching arrows, group labels clear of their contents, every box at least 12 px inside its group. A page with any problem is written only as `<name>.draft.png` and the script exits non-zero; `<name>.audit.json` records the result of the last build.

AWS icons: official AWS Architecture Icons, `Icon-package_07312026.zip` (SHA-256 `d2d166c453526471749d520e0db022c459abef759d2946cf2dd1d1c992dc6526`) from https://aws.amazon.com/architecture/icons/. AWS permits their use in architecture diagrams; the icon files themselves are not redistributed here, and the rendered PNG and PDF already contain them. To rebuild `architecture.png`, copy the API Gateway, Cognito, DynamoDB, EC2, Lambda, Marketplace, S3 and SQS service icons and the AWS Cloud and Region group icons from that package into `icons/` as `api-gateway.svg`, `cognito.svg`, `dynamodb.svg`, `ec2.svg`, `lambda.svg`, `marketplace.svg`, `s3.svg`, `sqs.svg`, `aws-cloud.svg` and `region.svg`. Other glyphs are simple line drawings made for these diagrams.
