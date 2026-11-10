// Serve the panel on its own for local checks and screenshots:
//   AGENT_ENV_STATUS_FILE=panel/fixtures/drift.json [PANEL_LANG=zh] [PANEL_HEADER=bar] node panel/dev-server.ts [port]
// Uses the same createPanelHandler the portal mounts; allowedHosts is 127.0.0.1:<port> only. With LOCAL_TOOLS_PORTAL
// set to a running portal's address, /_shared/* is fetched from it, so the page gets the real theme tokens.
import { createServer } from "node:http";
import { createPanelHandler } from "./index.ts";

const port = Number(process.argv[2] ?? process.env.PORT ?? 17392);
const host = "127.0.0.1";
const portal = process.env.LOCAL_TOOLS_PORTAL;
// PANEL_HEADER=bar draws the page as the portal does, with the title in the portal's own header bar.
const handle = createPanelHandler({
  allowedHosts: [`${host}:${port}`],
  lang: (process.env.PANEL_LANG as "zh" | "en" | undefined) ?? "auto",
  theme: { header: process.env.PANEL_HEADER === "bar" ? "bar" : "plain" },
});

const server = createServer(async (req, res) => {
  if (req.url?.startsWith("/_shared/")) {
    if (!portal) {
      res.writeHead(404).end();
      return;
    }
    try {
      const r = await fetch(portal + req.url);
      res.writeHead(r.status, { "content-type": r.headers.get("content-type") ?? "text/plain" });
      res.end(Buffer.from(await r.arrayBuffer()));
    } catch {
      res.writeHead(404).end();
    }
    return;
  }
  handle(req, res).then(
    (handled) => {
      if (!handled) res.writeHead(404).end("not found");
    },
    (e) => res.writeHead(500).end(String((e as Error)?.message ?? e)),
  );
});
server.listen(port, host, () => console.log(`env panel: http://${host}:${port}/`));
