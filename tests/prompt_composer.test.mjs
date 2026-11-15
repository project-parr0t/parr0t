import { test } from "node:test";
import assert from "node:assert/strict";
import { buildPrompt } from "../pages/prompt.mjs";
import { DEFAULT_MODEL_GROUPS, proxyModelGroups } from "../pages/model-catalog.mjs";

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

test("proxy catalogs require the bound schema and only replace defaults with valid model IDs", () => {
  assert.deepEqual(DEFAULT_MODEL_GROUPS.map(group => group.label), ["GPT", "Claude"]);
  assert.equal(proxyModelGroups({ models: [{ id: "fixture/model" }] }), null);
  assert.equal(proxyModelGroups({ error: { message: "unavailable" } }), null);
  assert.deepEqual(proxyModelGroups({ object: "list", data: [{ id: "fixture/model:high" }, { id: "fixture/model:high" }, { id: "bad\nmodel" }] }), [{ label: "OpenCodex", models: ["fixture/model:high"] }]);
  assert.throws(() => buildPrompt({ fallbackModel: "gpt-6.1-sol", fallbackAttempts: "3" }));
});
