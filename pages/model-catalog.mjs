// Public model IDs only; listing is not an availability or execution-permission claim.
// Claude IDs: https://platform.claude.com/docs/en/models/overview
export const DEFAULT_MODEL_GROUPS = [
  { label: "GPT", models: ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra"] },
  { label: "Claude", models: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-fable-5-2"] },
];


export function exactModelSelector(value) {
  return typeof value === "string" && value.length > 0 && value === value.trim()
    && !/[\p{C}\\*?\[\]]/u.test(value)
    && !/^[/.~@]/.test(value) && !value.includes("://") && !/^[A-Za-z]:\//.test(value)
    && !value.split("/").some(part => part === "." || part === "..")
    && !["default", "inherit", "task", "smol", "slow", "luna", "sol"].includes(value.toLowerCase());
}
export function proxyModelGroups(catalog) {
  if (!Array.isArray(catalog?.data)) return null;
  const models = [...new Set(catalog.data.map(model => model?.id).filter(exactModelSelector))];
  return models.length ? [{ label: "OpenCodex", models }] : null;
}
