// gaulatti/croccante#40: the remote deploy script runs under `sh -c '...'` because the deploy
// host's login shell is fish. This dry-run check extracts the script from deploy.yml and
// checks the wrapper, the single-quote rule, and the POSIX syntax of the body. It runs no
// deploy and touches no server.
import { execFileSync } from "node:child_process";
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import assert from "node:assert/strict";

const workflow = readFileSync(new URL("../../.github/workflows/deploy.yml", import.meta.url), "utf8");

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

function wrappedBody(script) {
  return script.replace(/^sh -c '\n/, "").replace(/'\s*$/, "");
}

test("the remote script is wrapped in sh -c, so a fish login shell cannot parse it", () => {
  const script = remoteScript(workflow);
  assert.match(script, /^sh -c '\n/, "the script begins with sh -c '");
  assert.match(script.trimEnd(), /'$/, "the wrapper closes with a single quote");
});

test("the wrapped body contains no single quote (the sh -c wrapper would end early)", () => {
  const inner = wrappedBody(remoteScript(workflow));
  assert.equal(inner.includes("'"), false, "no single quote inside the sh -c body");
});

test("the body is valid POSIX sh (dry run with sh -n, nothing executed)", () => {
  const inner = wrappedBody(remoteScript(workflow));
  const file = join(mkdtempSync(join(tmpdir(), "croccante-deploy-")), "deploy.sh");
  writeFileSync(file, inner + "\n");
  execFileSync("sh", ["-n", file], { stdio: "pipe" });
});

test("the body checks the required deploy variables before any container is stopped", () => {
  const script = remoteScript(workflow);
  for (const name of ["PROGRAM_ID", "AWS_REGION"]) {
    assert.match(script, new RegExp(`test -n "\\$${name}"`), `${name} is checked`);
  }
  const stop = script.indexOf("docker stop croccante");
  assert.ok(stop > script.indexOf('test -n "$PROGRAM_ID"'), "guards run before the stop");
});
