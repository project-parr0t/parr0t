import { test } from "node:test";
import assert from "node:assert/strict";
import { buildOmpCommand, buildPrompt } from "../pages/prompt.mjs";
import { DEFAULT_MODEL_GROUPS, proxyModelGroups } from "../pages/model-catalog.mjs";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

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

test("nonpositive fallback settings and counts without a model cannot produce a prompt", () => {
  for (const options of [
    { fallbackAttempts: "2" },
    { fallbackModel: "fixture-model", fallbackAttempts: "0" },
    { fallbackModel: "fixture-model", fallbackAttempts: "1.5" },
    { fallbackModel: "fixture-model", fallbackAttempts: "-2" },
  ]) assert.throws(() => buildPrompt(options));
});

test("invalid native model selections are rejected before producing executable instructions", () => {
  for (const value of [String.raw`C:\Fixture Game\Data`, "/tmp/fixture-game", "fixture/*", "fixture/../model", "default", "inherit", "task", "smol", "slow", "luna", "sol", "@fixture"]) {
    for (const role of ["baseModel", "triageModel", "strongModel", "rewriteModel", "contextModel", "fallbackModel"]) {
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
  assert.ok(buildPrompt({ strongMaxWorkers: "65" }).includes("65 (routing.strong_max_workers)"));
});

test("explicit models produce exact role override and fallback policy commands in either operation", () => {
  const options = {
    baseModel: "fixture/translator@fixture-route:low",
    triageModel: "fixture/triager:medium",
    strongModel: "fixture/strong@fixture-route:high",
    rewriteModel: "공급자/수정(버전)+모델@경로:high",
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
      "--role-model", `rewrite=${options.rewriteModel}`,
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
  for (const [option, role] of [["baseModel", "translation"], ["triageModel", "triage"], ["strongModel", "strong-review"], ["rewriteModel", "rewrite"], ["contextModel", "context"]]) {
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
  const prompt = buildPrompt({ fallbackModel: "gpt-6.1-sol" });
  assert.deepEqual(commandArgs(prompt, "review-policy"), [
    "--workspace", "<workspace>", "--fallback-model", "gpt-6.1-sol", "--user-requested",
  ]);
  assert.deepEqual(commandArgs(buildPrompt({ fallbackModel: "gpt-6.1-sol", fallbackAttempts: "7" }), "review-policy"), [
    "--workspace", "<workspace>", "--fallback-model", "gpt-6.1-sol", "--max-calls", "7", "--user-requested",
  ]);
});

test("explicit Codex retains the default semantic request and native command contract", () => {
  for (const operation of ["localize", "migrate"]) {
    const options = { operation, repositoryUrl: "https://example.test/project.git", baseModel: "fixture/base@route:high",
      gamePath: "C:/Fixture Game", fonts: true, images: true };
    const prompt = buildPrompt(options);
    assert.equal(buildPrompt({ ...options, host: "codex" }), prompt);
    assert.equal(commandArgs(prompt, "native-runtime")[3], "<current-native-host>");
    assert.ok(prompt.startsWith("다음 Parr0t 저장소의 AGENTS.md와 docs/agent-usage.md를 읽고,"));
    assert.ok(!prompt.includes("parr0t_session"));
    assert.ok(!prompt.includes("bootstrap"));
  }
});

test("OMP setup binds the actual job before official entry and migration never replaces it", () => {
  const prompt = buildPrompt({ host: "omp", baseModel: "fixture/base@route:high" });
  assert.equal(commandArgs(prompt, "native-runtime")[3], "omp");
  assert.ok(prompt.includes("user-request 바인딩·검증을 완료한 뒤"));
  assert.ok(prompt.includes("parr0t_session({workspace: <실제 작업 절대 경로>, user_requested: true})"));
  assert.ok(prompt.includes("일반 bootstrap의 존재는 현지화 통신 정책의 강제나 워커 격리 증거가 아니다"));
  const migration = buildPrompt({ host: "omp", operation: "migrate", repositoryUrl: "https://example.test/project.git" });
  assert.ok(migration.includes("기존 프로젝트 위에 init-workspace를 실행하거나"));
  assert.ok(migration.includes("유효한 성공·체크포인트를 보존"));
  assert.throws(() => buildPrompt({ host: "other" }), /호스트/);
  for (const corePath of ["relative/core", "C:relative", "bad\0path"]) {
    assert.throws(() => buildOmpCommand({ corePath }), /절대 경로/);
  }
});

// Exercise the actual OS shell parser and file consumer without starting OMP,
// importing an extension, making model calls, or relying on installed plugins.
function runPowerShellCommand(t, options = {}, { missingCore = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), "parr0t-composer-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const corePath = join(root, "Core 한글 ' $literal [brackets]");
  const fixtureFiles = ["AGENTS.md", "docs/agent-usage.md", "docs/control-plane-contract.md",
    "src/game_l10n/templates/native-python.py", "src/game_l10n/templates/omp-native-process.mjs",
    "src/game_l10n/templates/framework-mode-approval.mjs"];
  if (!missingCore) {
    for (const file of fixtureFiles) {
      const path = join(corePath, file);
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, "");
    }
  }
  const commandPath = join(root, "paste.txt");
  const capturePath = join(root, "received.json");
  const commandOptions = { ...options, corePath };
  writeFileSync(commandPath, buildOmpCommand(commandOptions), "utf8");
  const harness = String.raw`
$ErrorActionPreference = 'Stop'
function global:omp {
  $received = @($args)
  $attachments = @()
  foreach ($argument in $received) {
    if ($argument.StartsWith('@')) {
      $path = $argument.Substring(1)
      $attachments += @{ path = $path; bytes = [Convert]::ToBase64String([IO.File]::ReadAllBytes($path)) }
    }
  }
  $configuration = $null
  $configIndex = [Array]::IndexOf($received, '--config')
  if ($configIndex -ge 0) {
    $configuration = [IO.File]::ReadAllText($received[$configIndex + 1]) | ConvertFrom-Json
  }
  $record = @{ argv = $received; cwd = (Get-Location).ProviderPath; attachments = $attachments; config = $configuration }
  [IO.File]::WriteAllText($env:PARR0T_COMPOSER_CAPTURE, ($record | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))
}
& ([ScriptBlock]::Create([IO.File]::ReadAllText($env:PARR0T_COMPOSER_COMMAND)))
`;
  const result = spawnSync("powershell.exe", ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", harness], {
    cwd: root, encoding: "utf8", timeout: 30000,
    env: { ...process.env, PARR0T_COMPOSER_COMMAND: commandPath, PARR0T_COMPOSER_CAPTURE: capturePath },
  });
  let received;
  if (existsSync(capturePath)) {
    received = JSON.parse(readFileSync(capturePath, "utf8"));
    t.after(() => rmSync(received.cwd, { recursive: true, force: true }));
  }
  return { result, received, commandOptions, root, corePath };
}

const windowsShell = { skip: process.platform !== "win32" && "Requires Windows built-in PowerShell" };

test("PowerShell delivers long Unicode and hostile field text losslessly through UTF-8 @files", windowsShell, t => {
  const hostile = String.raw`C:\synthetic\'; throw 'INJECTED'; # $("injected") ` + "\r\n'@\n\"@\n끝 😀 e\u0301\n";
  const { result, received, commandOptions, root, corePath } = runPowerShellCommand(t, {
    gamePath: hostile.repeat(1500), images: true, fonts: true,
  });
  assert.equal(result.status, 0, result.error?.message ?? result.stderr);
  assert.ok(received);
  const request = buildPrompt({ ...commandOptions, host: "omp" });
  assert.ok(Buffer.byteLength(request, "utf8") > 32767);
  assert.equal(received.attachments.length, 2);
  assert.deepEqual(Buffer.from(received.attachments[1].bytes, "base64"), Buffer.from(request, "utf8"));
  assert.deepEqual(readFileSync(received.attachments[1].path), Buffer.from(request, "utf8"));
  assert.equal(received.argv.at(-1), `@${received.attachments[1].path}`);
  assert.ok(received.argv.every(argument => argument.length < 4096));
  const value = flag => received.argv[received.argv.indexOf(flag) + 1];
  assert.equal(resolve(value("--cwd")), resolve(received.cwd));
  assert.notEqual(resolve(received.cwd), resolve(root));
  assert.notEqual(resolve(received.cwd), resolve(corePath));
  assert.equal(dirname(value("--session-dir")), received.cwd);
  assert.ok(existsSync(value("--session-dir")));
  assert.equal(value("--trusted-extension"), join(corePath, "src/game_l10n/templates/omp-native-process.mjs"));
  assert.ok(received.argv.includes("--no-prewalk"));
  assert.ok(!received.argv.includes("--model"));
  assert.ok(!received.argv.includes("--config"));
  assert.equal(received.config, null);
  const setup = Buffer.from(received.attachments[0].bytes, "base64").toString("utf8");
  assert.ok(setup.includes("NOT the actual job workspace"));
  assert.ok(setup.includes("NEVER initialize a replacement job"));
  assert.ok(setup.includes("parr0t_session"));
  assert.ok(setup.includes("does NOT enforce"));
});

test("PowerShell stages exact selected models as additive command-scoped operator roles", windowsShell, t => {
  const selections = {
    baseModel: "fixture/base@route:low", triageModel: "fixture/triage:medium",
    strongModel: "fixture/strong@route:high", rewriteModel: "공급자/수정(버전)+모델@경로:high",
    fallbackModel: "fixture/fallback@route:high", fallbackAttempts: "7",
    analyzeContext: false, contextModel: "fixture/ignored:high",
    operation: "migrate", repositoryUrl: "https://example.test/existing.git",
  };
  const { result, received } = runPowerShellCommand(t, selections);
  assert.equal(result.status, 0, result.error?.message ?? result.stderr);
  assert.deepEqual(Object.keys(received.config), ["modelRoles"]);
  const roles = received.config.modelRoles;
  assert.equal(Object.keys(roles).length, 5);
  assert.ok(Object.keys(roles).every(key => /^parr0t_requested_[0-9a-f]{32}_(?:translation|triage|strong_review|rewrite|review_fallback)$/.test(key)));
  assert.deepEqual(Object.values(roles).sort(), [selections.baseModel, selections.triageModel,
    selections.strongModel, selections.rewriteModel, selections.fallbackModel].sort());
  assert.ok(!Object.hasOwn(roles, "default"));
  assert.ok(!received.argv.includes("--model"));
  const configPath = received.argv[received.argv.indexOf("--config") + 1];
  assert.equal(dirname(configPath), received.cwd);
  assert.deepEqual(JSON.parse(readFileSync(configPath, "utf8")), received.config);
  assert.ok(received.attachments.every(attachment => existsSync(attachment.path)));
});

test("missing current Core fails before invoking even the synthetic OMP consumer", windowsShell, t => {
  const { result, received } = runPowerShellCommand(t, {}, { missingCore: true });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /Current complete Parr0t Core checkout required/);
  assert.equal(received, undefined);
});
