import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import type { Config } from "../config.ts";
import { log } from "../log.ts";
import type { ScriptContext } from "../persona/script.ts";
import { fill } from "../persona/script.ts";
import type { TranscriptTurn } from "../store/store.ts";

/**
 * Optional LLM used for one thing only: a short, in-persona answer to an off-script
 * question (e.g. "hva gjør dere egentlig?"). Consent, opt-out, outcomes and every closing
 * line are deterministic and never depend on it. With LLM_PROVIDER=none the secretary
 * answers with the fixed escalation line instead.
 */
export type Llm = {
  readonly provider: string;
  reply(ctx: ScriptContext, transcript: TranscriptTurn[], question: string): Promise<string | null>;
};

const SYSTEM_PATH = fileURLToPath(new URL("../persona/system-nb.md", import.meta.url));
let systemCache: string | null = null;
export function systemPrompt(ctx: ScriptContext): string {
  systemCache ??= readFileSync(SYSTEM_PATH, "utf8");
  return fill(systemCache, ctx);
}

const MAX_CHARS = 280;
const FORBIDDEN = /\b(kr|kroner|nok|€|\$|pris(en|er)? er|koster \d|per måned|pr mnd|gratis for alltid|live på (nettsiden|siden) deres)\b/i;

/** Keep a generated reply short and drop anything that smells like invented pricing/claims. */
export function sanitizeReply(text: string | null | undefined): string | null {
  if (!text) return null;
  const t = text.replace(/\s+/g, " ").trim();
  if (!t || FORBIDDEN.test(t)) return null;
  const sentences = t.match(/[^.!?]+[.!?]?/g) ?? [t];
  return sentences.slice(0, 2).join(" ").trim().slice(0, MAX_CHARS);
}

export const NO_LLM: Llm = {
  provider: "none",
  async reply() {
    return null;
  },
};

function history(transcript: TranscriptTurn[]): { role: "user" | "assistant"; content: string }[] {
  return transcript
    .filter((t) => t.role !== "system")
    .slice(-8)
    .map((t) => ({ role: t.role === "agent" ? "assistant" : "user", content: t.text }));
}

export function createLlm(config: Config, fetchImpl: typeof fetch = fetch): Llm {
  const { provider, apiKey, model, baseUrl } = config.llm;
  if (provider === "none" || !apiKey) return NO_LLM;

  if (provider === "openai") {
    const base = (baseUrl || "https://api.openai.com/v1").replace(/\/+$/, "");
    const useModel = model || "gpt-4o-mini";
    return {
      provider: "openai",
      async reply(ctx, transcript, question) {
        try {
          const res = await fetchImpl(`${base}/chat/completions`, {
            method: "POST",
            headers: { authorization: `Bearer ${apiKey}`, "content-type": "application/json" },
            body: JSON.stringify({
              model: useModel,
              max_tokens: 120,
              temperature: 0.3,
              messages: [
                { role: "system", content: systemPrompt(ctx) },
                ...history(transcript),
                { role: "user", content: question },
              ],
            }),
          });
          const body = (await res.json()) as { choices?: { message?: { content?: string } }[] };
          return sanitizeReply(body.choices?.[0]?.message?.content);
        } catch (err) {
          log("warn", "llm reply failed", { provider: "openai", error: String(err) });
          return null;
        }
      },
    };
  }

  const base = (baseUrl || "https://api.anthropic.com").replace(/\/+$/, "");
  const useModel = model || "claude-haiku-4-5-20251001";
  return {
    provider: "anthropic",
    async reply(ctx, transcript, question) {
      try {
        const res = await fetchImpl(`${base}/v1/messages`, {
          method: "POST",
          headers: {
            "x-api-key": apiKey,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
          },
          body: JSON.stringify({
            model: useModel,
            max_tokens: 120,
            system: systemPrompt(ctx),
            messages: [...history(transcript), { role: "user", content: question }],
          }),
        });
        const body = (await res.json()) as { content?: { type: string; text?: string }[] };
        const text = body.content?.find((c) => c.type === "text")?.text;
        return sanitizeReply(text);
      } catch (err) {
        log("warn", "llm reply failed", { provider: "anthropic", error: String(err) });
        return null;
      }
    },
  };
}
