import { timingSafeEqual } from "node:crypto";
import type { Config } from "../config.ts";
import { json, type Ctx, type Reply } from "../http/router.ts";

/** Bearer shared secret for the operator API. Missing secret on the server = everything 503. */
export function requireAuth(ctx: Ctx, config: Config): Reply | null {
  if (!config.enqueueAuthSecret) return json(503, { error: "auth_not_configured" });
  const header = ctx.headers.authorization ?? "";
  const m = /^Bearer\s+(.+)$/i.exec(Array.isArray(header) ? header[0] ?? "" : header);
  if (!m) return json(401, { error: "unauthorized" });
  const given = Buffer.from(m[1] ?? "");
  const expected = Buffer.from(config.enqueueAuthSecret);
  if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
    return json(401, { error: "unauthorized" });
  }
  return null;
}
