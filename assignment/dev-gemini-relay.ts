// Local-development only. Not used in production, not part of the Docker image.
//
// Google geo-blocks the Gemini API from some countries: every call answers
// 400 FAILED_PRECONDITION "User location is not supported for the API use.",
// keyed to the source IP of the request. The VPS is in a supported country, so
// the fix is to make the request leave from there instead of from this laptop.
//
// The obvious route — an SSH SOCKS proxy — doesn't work, because Bun's fetch
// rejects socks5:// in HTTPS_PROXY (UnsupportedProxyProtocol). So instead:
//
//   ssh -N -L 8443:generativelanguage.googleapis.com:443 alex@212.113.119.153
//
// ...forwards a raw TCP port through the VPS to Google, and this relay speaks
// plain HTTP on 8099 and re-issues each request over that tunnel. Point the
// reader at it with GEMINI_BASE_URL=http://127.0.0.1:8099 and nothing else
// changes.
//
// TLS stays end-to-end: the tunnel carries bytes, and the `servername` +
// `Host` below make the handshake and certificate check happen against the
// real Google hostname even though the socket connects to localhost. Full
// verification is on — the API key is never exposed to anything but Google.
//
//   Terminal 1: ssh -N -L 8443:generativelanguage.googleapis.com:443 alex@212.113.119.153
//   Terminal 2: bun run dev-gemini-relay.ts
//   Terminal 3: bun --env-file=.env run server.ts

import { request } from "node:https";

const UPSTREAM = "generativelanguage.googleapis.com";
const PORT = Number(process.env.RELAY_PORT ?? 8099);
const TUNNEL_HOST = process.env.TUNNEL_HOST ?? "127.0.0.1";
const TUNNEL_PORT = Number(process.env.TUNNEL_PORT ?? 8443);

Bun.serve({
    port: PORT,
    hostname: "127.0.0.1", // never expose this: it forwards whatever it is given
    idleTimeout: 120,
    async fetch(req) {
        const url = new URL(req.url);
        const body = req.method === "GET" || req.method === "HEAD"
            ? undefined
            : Buffer.from(await req.arrayBuffer());

        // Only the headers Google needs. Hop-by-hop headers and Bun's own
        // host/connection values would be wrong for the upstream request.
        const headers: Record<string, string> = { host: UPSTREAM };
        for (const h of ["content-type", "x-goog-api-key", "accept"]) {
            const v = req.headers.get(h);
            if (v) headers[h] = v;
        }
        if (body) headers["content-length"] = String(body.byteLength);

        return await new Promise<Response>((resolve) => {
            const up = request(
                {
                    host: TUNNEL_HOST, // the socket goes to the SSH tunnel...
                    port: TUNNEL_PORT,
                    servername: UPSTREAM, // ...but SNI + cert check are Google's
                    method: req.method,
                    path: url.pathname + url.search,
                    headers,
                },
                (res) => {
                    const chunks: Buffer[] = [];
                    res.on("data", (c) => chunks.push(c));
                    res.on("end", () => {
                        console.log(`${req.method} ${url.pathname} -> ${res.statusCode}`);
                        resolve(
                            new Response(Buffer.concat(chunks), {
                                status: res.statusCode ?? 502,
                                headers: {
                                    "content-type":
                                        res.headers["content-type"] ?? "application/json",
                                },
                            }),
                        );
                    });
                },
            );
            up.on("error", (e: any) => {
                // Nearly always the tunnel not being up — say so, rather than
                // letting it surface as an opaque fetch failure three layers up.
                const msg = `relay -> ${TUNNEL_HOST}:${TUNNEL_PORT} failed: ${e?.message ?? e}. Is the ssh -L tunnel running?`;
                console.error(msg);
                resolve(
                    new Response(JSON.stringify({ error: { message: msg } }), {
                        status: 502,
                        headers: { "content-type": "application/json" },
                    }),
                );
            });
            if (body) up.write(body);
            up.end();
        });
    },
});

console.log(`gemini relay on http://127.0.0.1:${PORT}`);
console.log(`  -> ${TUNNEL_HOST}:${TUNNEL_PORT} (tls sni: ${UPSTREAM})`);
console.log(`  set GEMINI_BASE_URL=http://127.0.0.1:${PORT}`);
