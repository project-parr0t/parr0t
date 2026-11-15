// Public model IDs only; listing is not an availability or execution-permission claim.
// Claude IDs: https://platform.claude.com/docs/en/models/overview
export const DEFAULT_MODEL_GROUPS = [
  { label: "GPT", models: ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra"] },
  { label: "Claude", models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"] },
];

export function proxyModelGroups(catalog) {
  if (!Array.isArray(catalog?.data)) return null;
  const models = [...new Set(catalog.data.map(model => model?.id).filter(id => typeof id === "string" && /^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,159}$/.test(id)))];
  return models.length ? [{ label: "OpenCodex", models }] : null;
}
