import { buildPrompt } from "./prompt.mjs";
import { DEFAULT_MODEL_GROUPS, proxyModelGroups } from "./model-catalog.mjs";

const form = document.querySelector("#composer");
const output = document.querySelector("#prompt");
const status = document.querySelector("#status");
const copy = document.querySelector("#copy");
const download = document.querySelector("#download");
const picker = document.querySelector("#fallback-preset");
const proxyToggle = document.querySelector("#use-proxy");
const catalogStatus = document.querySelector("#catalog-status");
let activeProxyModels = false;
let catalogRequest;

function showModels(groups) {
  const selected = form.elements.fallbackModel.value;
  picker.replaceChildren(new Option("미지정", ""), new Option("직접 입력", "__custom__"));
  for (const group of groups) {
    const element = document.createElement("optgroup");
    element.label = group.label;
    for (const id of group.models) element.append(new Option(id, id));
    picker.append(element);
  }
  picker.value = selected && [...picker.options].some(option => option.value === selected) ? selected : selected ? "__custom__" : "";
  update();
}

async function refreshCatalog() {
  catalogRequest?.abort();
  activeProxyModels = false;
  showModels(DEFAULT_MODEL_GROUPS);
  catalogStatus.textContent = proxyToggle.checked ? "로컬 연결 확인 중" : "GPT / Claude";
  if (!proxyToggle.checked) return;
  const request = new AbortController();
  catalogRequest = request;
  try {
    const response = await fetch("http://localhost:10100/v1/models", { credentials: "omit", cache: "no-store", targetAddressSpace: "loopback", signal: request.signal });
    if (!response.ok) throw new Error("unavailable");
    const groups = proxyModelGroups(await response.json());
    if (request.signal.aborted) return;
    if (!groups) throw new Error("unavailable");
    activeProxyModels = true;
    showModels(groups);
    catalogStatus.textContent = "OpenCodex 모델 감지";
  } catch {
    if (!request.signal.aborted) catalogStatus.textContent = "로컬 연결 미확인 · 기본 목록";
  }
}

function message(text, error = false) {
  status.textContent = text;
  status.dataset.error = String(error);
}

function update() {
  document.querySelector("#fallback-model-field").hidden = picker.value !== "__custom__";
  const migration = form.elements.operation.value === "migrate";
  for (const label of form.querySelectorAll("[data-localization]")) {
    label.hidden = migration;
    for (const control of label.querySelectorAll("input, select")) control.disabled = migration;
  }
  document.querySelector("#repository-field").hidden = !migration;
  form.elements.repositoryUrl.disabled = !migration;
  form.elements.repositoryUrl.required = migration;
  document.querySelector("#context-model-field").hidden = !form.elements.analyzeContext.checked;
  form.elements.contextModel.disabled = !form.elements.analyzeContext.checked;
  try {
    const values = Object.fromEntries(new FormData(form));
    values.fonts = form.elements.fonts.checked;
    values.images = form.elements.images.checked;
    values.analyzeContext = form.elements.analyzeContext.checked;
    values.fallbackViaOpenCodex = activeProxyModels && picker.value === values.fallbackModel && Boolean(values.fallbackModel);
    output.value = buildPrompt(values);
    copy.disabled = download.disabled = false;
    message("");
  } catch (error) {
    output.value = "";
    copy.disabled = download.disabled = true;
    message(error.message, true);
  }
}

form.addEventListener("submit", event => event.preventDefault());
form.addEventListener("input", update);
form.addEventListener("change", update);
picker.addEventListener("change", () => {
  if (picker.value !== "__custom__") form.elements.fallbackModel.value = picker.value;
  update();
});
proxyToggle.addEventListener("change", refreshCatalog);
copy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(output.value);
    message("복사했습니다.");
  } catch {
    output.focus();
    output.select();
    message("자동 복사가 차단되었습니다. 선택된 텍스트를 직접 복사하세요.", true);
  }
});
download.addEventListener("click", () => {
  const url = URL.createObjectURL(new Blob([output.value], { type: "text/plain;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "parr0t-prompt.txt";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  message("프롬프트 파일 저장을 요청했습니다.");
});
refreshCatalog();
