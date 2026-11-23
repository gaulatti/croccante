// gaulatti/croccante#40 and #41: the remote deploy script runs under `sh -c '...'` because the
// deploy host's login shell is fish. These dry-run checks extract the script from deploy.yml and
// check the wrapper, the single-quote rule, the POSIX syntax, that every required variable is
// checked by name before any container is touched, and that a missing variable fails with its
// name printed. They run no deploy and touch no server.
import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, writeFileSync, chmodSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";

const workflow = readFileSync(new URL("../../.github/workflows/deploy.yml", import.meta.url), "utf8");
const REQUIRED = ["GHCR_TOKEN", "GHCR_USER", "IMAGE_REF", "IMAGE_LATEST", "PROGRAM_ID", "AWS_REGION"];

// The `script: |` block of the appleboy/ssh-action step, de-indented.
function remoteScript(text) {
  const lines = text.split("\n");
  const start = lines.findIndex((line) => /^\s*script: \|\s*$/.test(line));
  assert.notEqual(start, -1, "deploy.yml has a `script: |` block");
  const indent = lines[start].match(/^\s*/)[0].length;
  const body = [];
  for (let i = start + 1; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.trim() !== "" && line.match(/^\s*/)[0].length <= indent) break;
    body.push(line.slice(Math.min(indent + 2, line.match(/^\s*/)[0].length)));
  }
  return body.join("\n").trimEnd();
}

// The text inside `sh -c '...'`.
function wrappedBody(script) {
  return script.replace(/^sh -c '\n/, "").replace(/'\s*$/, "");
}

test("the remote script is wrapped in sh -c, so a fish login shell cannot parse it", () => {
  const script = remoteScript(workflow);
  assert.match(script, /^sh -c '\n/, "the script begins with sh -c '");
  assert.match(script.trimEnd(), /'$/, "the wrapper closes with a single quote");
});

test("the wrapped body contains no single quote (the sh -c wrapper would end early)", () => {
  assert.equal(wrappedBody(remoteScript(workflow)).includes("'"), false);
});

test("the body is valid POSIX sh (dry run with sh -n, nothing executed)", () => {
  const file = join(mkdtempSync(join(tmpdir(), "croccante-deploy-")), "deploy.sh");
  writeFileSync(file, wrappedBody(remoteScript(workflow)) + "\n");
  execFileSync("sh", ["-n", file], { stdio: "pipe" });
});

test("every required variable is checked by name before any container is stopped", () => {
  const body = wrappedBody(remoteScript(workflow));
  const loop = body.match(/for name in ([A-Z_ ]+); do\n\s*require_var "\$name"/);
  assert.ok(loop, "the required variables are checked in a loop that calls require_var");
  assert.deepEqual(loop[1].trim().split(/\s+/), REQUIRED, "the loop names exactly the required variables");
  const check = body.indexOf("for name in");
  assert.ok(check >= 0 && check < body.indexOf("docker stop croccante"), "the checks run before the stop");
  assert.ok(body.indexOf("docker pull") < body.indexOf("docker stop croccante"), "the pull still runs before the stop");
});

test("every step announces itself, and a missing required variable fails naming it (dry run, docker stubbed)", () => {
  const body = wrappedBody(remoteScript(workflow));
  const dir = mkdtempSync(join(tmpdir(), "croccante-dry-"));
  const script = join(dir, "deploy.sh");
  writeFileSync(script, body + "\n");
  // A docker that would record any call. It must never run: the variable check fails first.
  const docker = join(dir, "docker");
  writeFileSync(docker, `#!/bin/sh\necho "docker $*" >> "${dir}/calls"\nexit 0\n`);
  chmodSync(docker, 0o755);
  const env = { PATH: `${dir}:${process.env.PATH}`, GHCR_TOKEN: "t", GHCR_USER: "u", IMAGE_REF: "i", IMAGE_LATEST: "l", AWS_REGION: "r" };
  const result = spawnSync("sh", [script], { env, encoding: "utf8" });
  assert.notEqual(result.status, 0, "a missing PROGRAM_ID fails the script");
  assert.match(result.stderr, /required variable PROGRAM_ID is not set/, "the failure names the variable");
  assert.match(result.stdout, /==> checking required variables/, "the step is announced");
  // The only docker call allowed is the logout in the EXIT trap; nothing pulls, stops or runs.
  const calls = readFileSync(join(dir, "calls"), "utf8").trim().split("\n");
  assert.deepEqual(calls.filter((call) => !call.startsWith("docker logout")), [], "no pull, stop or run happened");
});

test("a missing host credential file fails naming the file, before any container is touched (dry run, docker stubbed)", () => {
  const body = wrappedBody(remoteScript(workflow));
  const dir = mkdtempSync(join(tmpdir(), "croccante-file-"));
  // Point the two host paths at the temporary directory: the control token is absent, the credentials present.
  writeFileSync(join(dir, "aws-credentials"), "[default]\n");
  const script = join(dir, "deploy.sh");
  writeFileSync(
    script,
    body
      .replaceAll("/opt/croccante/control-token", join(dir, "control-token"))
      .replaceAll("/opt/croccante/aws-credentials", join(dir, "aws-credentials")) + "\n",
  );
  const docker = join(dir, "docker");
  writeFileSync(docker, `#!/bin/sh\necho "docker $*" >> "${dir}/calls"\nexit 0\n`);
  chmodSync(docker, 0o755);
  const env = { PATH: `${dir}:${process.env.PATH}`, GHCR_TOKEN: "t", GHCR_USER: "u", IMAGE_REF: "i", IMAGE_LATEST: "l", PROGRAM_ID: "p", AWS_REGION: "r" };
  const result = spawnSync("sh", [script], { env, encoding: "utf8" });
  assert.notEqual(result.status, 0, "a missing control token fails the script");
  assert.match(result.stderr, new RegExp(`required file ${join(dir, "control-token")} is missing or empty`), "the failure names the file");
  const calls = existsSync(join(dir, "calls")) ? readFileSync(join(dir, "calls"), "utf8").trim().split("\n") : [];
  assert.deepEqual(calls.filter((call) => !call.startsWith("docker logout")), [], "no login, pull, stop or run happened");
});
