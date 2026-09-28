import type { IncomingMessage, ServerResponse } from "node:http";

export type Ctx = {
  method: string;
  url: URL;
  /** Path + query exactly as received (what Twilio signed against, minus origin). */
  rawUrl: string;
  params: Record<string, string>;
  headers: IncomingMessage["headers"];
  rawBody: string;
  json: () => unknown;
  form: () => URLSearchParams;
};

export type Reply = { status: number; headers?: Record<string, string>; body: string };

export type Handler = (ctx: Ctx) => Promise<Reply> | Reply;

type Route = { method: string; pattern: RegExp; keys: string[]; handler: Handler };

export function json(status: number, body: unknown): Reply {
  return {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify(body),
  };
}
export function xml(body: string): Reply {
  return { status: 200, headers: { "content-type": "text/xml; charset=utf-8" }, body };
}
export function text(status: number, body: string): Reply {
  return { status, headers: { "content-type": "text/plain; charset=utf-8" }, body };
}

const MAX_BODY = 256 * 1024;

export class Router {
  private routes: Route[] = [];

  add(method: string, path: string, handler: Handler): this {
    const keys: string[] = [];
    const src = path.replace(/:([a-zA-Z_]+)/g, (_m, k: string) => {
      keys.push(k);
      return "([^/]+)";
    });
    this.routes.push({ method, pattern: new RegExp(`^${src}/?$`), keys, handler });
    return this;
  }
  get(path: string, h: Handler): this {
    return this.add("GET", path, h);
  }
  post(path: string, h: Handler): this {
    return this.add("POST", path, h);
  }

  /** Resolve a request to a reply. Exposed for tests; the node listener wraps it. */
  async dispatch(
    method: string,
    rawUrl: string,
    headers: IncomingMessage["headers"],
    rawBody: string,
  ): Promise<Reply> {
    const url = new URL(rawUrl, "http://internal");
    let pathMatched = false;
    for (const r of this.routes) {
      const m = r.pattern.exec(url.pathname);
      if (!m) continue;
      pathMatched = true;
      if (r.method !== method) continue;
      const params: Record<string, string> = {};
      r.keys.forEach((k, i) => (params[k] = decodeURIComponent(m[i + 1] ?? "")));
      const ctx: Ctx = {
        method,
        url,
        rawUrl,
        params,
        headers,
        rawBody,
        json: () => (rawBody ? JSON.parse(rawBody) : {}),
        form: () => new URLSearchParams(rawBody),
      };
      try {
        return await r.handler(ctx);
      } catch (err) {
        if (err instanceof SyntaxError) return json(400, { error: "invalid_json" });
        throw err;
      }
    }
    return json(pathMatched ? 405 : 404, { error: pathMatched ? "method_not_allowed" : "not_found" });
  }

  listener(onError: (err: unknown) => void) {
    return (req: IncomingMessage, res: ServerResponse): void => {
      const chunks: Buffer[] = [];
      let size = 0;
      req.on("data", (c: Buffer) => {
        size += c.length;
        if (size > MAX_BODY) {
          res.writeHead(413).end();
          req.destroy();
          return;
        }
        chunks.push(c);
      });
      req.on("end", () => {
        const body = Buffer.concat(chunks).toString("utf8");
        this.dispatch(req.method ?? "GET", req.url ?? "/", req.headers, body)
          .then((reply) => {
            res.writeHead(reply.status, {
              "cache-control": "no-store",
              ...(reply.headers ?? {}),
            });
            res.end(reply.body);
          })
          .catch((err) => {
            onError(err);
            if (!res.headersSent) res.writeHead(500, { "content-type": "application/json" });
            res.end(JSON.stringify({ error: "internal" }));
          });
      });
    };
  }
}
