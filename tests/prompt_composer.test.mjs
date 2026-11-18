import { test } from "node:test";
import assert from "node:assert/strict";
import { buildPrompt } from "../pages/prompt.mjs";
import { DEFAULT_MODEL_GROUPS, proxyModelGroups } from "../pages/model-catalog.mjs";

function commandArgs(prompt, command) {
  const prefix = `game-l10n ${command} `;
  const lines = prompt.split("\n").filter(line => line.startsWith(prefix));
  assert.equal(lines.length, 1, `expected one ${command} instruction`);
  return lines[0].slice(prefix.length).match(/"(?:\\.|[^"\\])*"|\S+/g)
    .map(token => token.startsWith('"') ? JSON.parse(token) : token);
}

test("skip, logic, font and mod options compose without the existing-Korean stop policy", () => {
  const prompt = buildPrompt({ gameType: "logic", koreanPolicy: "skip", fonts: true, delivery: "mod",
    fallbackModel: "configured/exact-model:high", fallbackAttempts: "7", gamePath: "C:/Fixture Game" });
  assert.ok(prompt.includes("존재 여부 검사를 생략"));
  assert.ok(!prompt.includes("새 번역은 시작하지 마라"));
  assert.ok(prompt.includes("번역·검수·엔진 및 번역 대상 조사를 생략하는 뜻이 아니다"));
  assert.ok(prompt.includes("게임에 맞는 한국어 폰트 설정"));
  assert.ok(prompt.includes("모드 형태"));
  assert.ok(prompt.includes("대사가 없는 부분"));
  assert.ok(prompt.includes("폴백 검수 모델: configured/exact-model:high\n폴백 검수 최대 횟수: 7회"));
  assert.ok(prompt.includes("C:/Fixture Game"));
});

test("incomplete or nonpositive fallback settings cannot produce a prompt", () => {
  for (const options of [
    { fallbackModel: "fixture-model" }, { fallbackAttempts: "2" },
    { fallbackModel: "fixture-model", fallbackAttempts: "0" },
    { fallbackModel: "fixture-model", fallbackAttempts: "1.5" },
    { fallbackModel: "fixture-model", fallbackAttempts: "-2" },
  ]) assert.throws(() => buildPrompt(options));
});

test("invalid native model selections are rejected before producing executable instructions", () => {
  for (const value of [String.raw`C:\Fixture Game\Data`, "/tmp/fixture-game", "fixture/model name", "default", "inherit", "task", "smol", "slow", "luna", "sol", "@fixture"]) {
    for (const role of ["baseModel", "triageModel", "strongModel", "contextModel", "fallbackModel"]) {
      assert.throws(() => buildPrompt({ [role]: value, ...(role === "fallbackModel" ? { fallbackAttempts: "2" } : {}) }),
        /폴더 경로나 별칭 대신 정확한 모델 ID/);
    }
  }
  assert.equal(buildPrompt({ analyzeContext: false, contextModel: String.raw`C:\Fixture Game\Data` }),
    buildPrompt({ analyzeContext: false }));
});

test("migration binds the supplied repository without starting a blanket new localization", () => {
  const repositoryUrl = "https://github.com/fixture-owner/previous-project.git";
  const prompt = buildPrompt({ operation: "migrate", repositoryUrl, fonts: true, delivery: "mod", koreanPolicy: "skip" });
  assert.ok(prompt.includes(`이전 레포지토리 URL:\n${repositoryUrl}`));
  assert.ok(prompt.includes("업데이트와 마이그레이션"));
  assert.ok(prompt.includes("기존 로컬 변경"));
  assert.ok(prompt.includes("새 검증 결과와 체크포인트"));
  assert.ok(!prompt.includes("존재 여부 검사를 생략"));
  assert.ok(!prompt.includes("결과물은 모드 형태"));
  assert.ok(!prompt.includes("게임 폴더는 이 요청에 첨부한 폴더를 사용하라"));
});

test("migration rejects missing, executable or credential-bearing repository URLs", () => {
  for (const repositoryUrl of ["", "not-a-url", "javascript:alert(1)", "file:///fixture", "https://user:secret@example.test/repo", "https://example.test/repo?token=secret"]) {
    assert.throws(() => buildPrompt({ operation: "migrate", repositoryUrl }));
  }
  assert.ok(buildPrompt({ operation: "localize", repositoryUrl: "ignored" }).includes("한국어 패치"));
});

test("role models, tier limits and requested images combine while disabled context ignores its stale model", () => {
  const prompt = buildPrompt({
    baseModel: "fixture/base:low", triageModel: "fixture/triage", strongModel: "fixture/strong:high",
    analyzeContext: false, contextModel: "STALE_CONTEXT_MODEL", images: true,
    triageMaxCalls: "1", strongMaxCalls: "2", reviewMaxWorkers: "4", triageMaxWorkers: "3", strongMaxWorkers: "2",
  });
  for (const model of ["fixture/base:low", "fixture/triage", "fixture/strong:high"]) assert.ok(prompt.includes(model));
  assert.ok(!prompt.includes("STALE_CONTEXT_MODEL"));
  assert.ok(prompt.includes("새 모델 분석 생략"));
  assert.ok(prompt.includes("이미지 번역: 요청함"));
  assert.ok(!prompt.includes("이미지 번역: 요청하지 않음"));
  for (const key of ["triage_max_calls", "strong_max_calls", "review_max_workers", "triage_max_workers", "strong_max_workers"]) assert.ok(prompt.includes(`routing.${key}`));
  assert.ok(!buildPrompt().includes("이미지 번역: 요청함"));
  assert.throws(() => buildPrompt({ reviewMaxWorkers: "0" }));
  assert.throws(() => buildPrompt({ strongMaxWorkers: "65" }));
});

test("explicit models produce exact role override and fallback policy commands in either operation", () => {
  const options = {
    baseModel: "fixture/translator@fixture-route:low",
    triageModel: "fixture/triager:medium",
    strongModel: "fixture/strong@fixture-route:high",
    contextModel: "fixture/context:low",
    fallbackModel: "fixture/fallback@fixture-route:high", fallbackAttempts: "007",
  };
  for (const operation of ["localize", "migrate"]) {
    const prompt = buildPrompt({ ...options, operation, repositoryUrl: "https://example.test/fixture.git" });
    assert.deepEqual(commandArgs(prompt, "native-runtime"), [
      "--workspace", "<workspace>", "--host", "<current-native-host>",
      "--role-model", `translation=${options.baseModel}`,
      "--role-model", `triage=${options.triageModel}`,
      "--role-model", `strong-review=${options.strongModel}`,
      "--role-model", `context=${options.contextModel}`,
      "--role-model", `review-fallback=${options.fallbackModel}`,
      "--user-requested-models",
    ]);
    assert.deepEqual(commandArgs(prompt, "review-policy"), [
      "--workspace", "<workspace>", "--fallback-model", options.fallbackModel,
      "--max-calls", "7", "--user-requested",
    ]);
  }
});

test("sparse role selections never fill unspecified roles with preset defaults", () => {
  for (const [option, role] of [["baseModel", "translation"], ["triageModel", "triage"], ["strongModel", "strong-review"], ["contextModel", "context"]]) {
    const prompt = buildPrompt({ [option]: "  fixture/selected@fixture-route:high  " });
    assert.deepEqual(commandArgs(prompt, "native-runtime"), [
      "--workspace", "<workspace>", "--host", "<current-native-host>", "--role-model", `${role}=fixture/selected@fixture-route:high`, "--user-requested-models",
    ]);
    assert.ok(!prompt.split("\n").some(line => line.startsWith("game-l10n review-policy ")));
  }
  for (const options of [{}, { analyzeContext: false, contextModel: "fixture/stale:high" }]) {
    const prompt = buildPrompt(options);
    assert.ok(!prompt.split("\n").some(line => /^game-l10n (?:native-runtime|review-policy) /.test(line)));
  }
});

test("fallback-only selection binds the requested model and count without changing other roles or transport", () => {
  const model = "fixture/fallback@fixture-proxy:high";
  const prompt = buildPrompt({ fallbackModel: model, fallbackAttempts: "4", fallbackViaOpenCodex: true,
    analyzeContext: false, contextModel: "fixture/stale-context" });
  assert.deepEqual(commandArgs(prompt, "native-runtime"), [
    "--workspace", "<workspace>", "--host", "<current-native-host>", "--role-model", `review-fallback=${model}`, "--user-requested-models",
  ]);
  assert.deepEqual(commandArgs(prompt, "review-policy"), [
    "--workspace", "<workspace>", "--fallback-model", model, "--max-calls", "4", "--user-requested",
  ]);
  assert.ok(!prompt.includes("fixture/stale-context"));
});

test("proxy catalogs require the bound schema and only replace defaults with valid model IDs", () => {
  assert.deepEqual(DEFAULT_MODEL_GROUPS.map(group => group.label), ["GPT", "Claude"]);
  assert.equal(proxyModelGroups({ models: [{ id: "fixture/model" }] }), null);
  assert.equal(proxyModelGroups({ error: { message: "unavailable" } }), null);
  assert.deepEqual(proxyModelGroups({ object: "list", data: [{ id: "fixture/model:high" }, { id: "fixture/model:high" }, { id: "bad\nmodel" }] }), [{ label: "OpenCodex", models: ["fixture/model:high"] }]);
  assert.throws(() => buildPrompt({ fallbackModel: "gpt-6.1-sol", fallbackAttempts: "3" }));
});
