import assert from 'node:assert/strict';
import {cpSync, existsSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {spawnSync} from 'node:child_process';
import {tmpdir} from 'node:os';
import {basename, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {parse} from 'yaml';

test('the real pinned coverage action accepts the application report and rejects an uncovered production line', t => {
  const root = fileURLToPath(new URL('../../', import.meta.url));
  const engine = join(root, '.clouseau-ci');
  const workflow = parse(readFileSync(join(root, '.github/workflows/ci.yml'), 'utf8'));
  const action = parse(readFileSync(join(engine, 'action.yml'), 'utf8'));
  const invocation = workflow.jobs.verify.steps.find(step => step.with?.operation === 'coverage');
  const implementation = action.runs.steps.find(step => step.name === 'Enforce 100% line and branch coverage');
  assert.ok(action.inputs['coverage-format'], 'the pinned action must support this consumer’s actual native report format');
  assert.equal(invocation.with['coverage-format'], 'coveragepy');
  const reportPath = join(root, 'coverage.json');
  assert.ok(existsSync(reportPath), 'the unit job produces the native report before this workflow test');

  // Reproduce the verify job layout: the application beside the engine, with
  // the report in the workspace, so only application sources are inventoried.
  const workspace = mkdtempSync(join(tmpdir(), 'croccante-coverage-consumer-'));
  t.after(() => rmSync(workspace, {recursive: true, force: true}));
  cpSync(root, join(workspace, invocation.with['source-directory']), {
    recursive: true,
    filter: source => !['.git', '.clouseau-ci', 'node_modules', '.coverage', 'coverage.json', '__pycache__'].includes(basename(source)),
  });
  symlinkSync(engine, join(workspace, '.clouseau-ci'), 'dir');
  const invoke = report => spawnSync('bash', ['--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', implementation.run], {
    cwd: workspace, encoding: 'utf8', env: {...process.env, CLOUSEAU_ROOT: engine,
      COVERAGE_REPORT: report, SOURCE_DIRECTORY: invocation.with['source-directory'], COVERAGE_FORMAT: invocation.with['coverage-format']}});

  const passed = invoke(reportPath);
  assert.equal(passed.status, 0, passed.stderr);
  for (const metric of ['lines', 'branches']) {
    const counts = passed.stdout.match(new RegExp(`${metric}: ([0-9]+)/([0-9]+)`));
    assert.ok(counts, passed.stdout); assert.ok(Number(counts[1]) > 0); assert.equal(counts[1], counts[2]);
  }

  const report = JSON.parse(readFileSync(reportPath, 'utf8'));
  const entry = Object.values(report.files).find(item => item.executed_lines.length > 0);
  assert.ok(entry, 'the actual application must contain measured lines');
  const line = entry.executed_lines.pop();
  entry.missing_lines.push(line);
  entry.summary.covered_lines -= 1;
  entry.summary.missing_lines += 1;
  const uncovered = join(workspace, 'uncovered.json');
  writeFileSync(uncovered, JSON.stringify(report));
  const rejected = invoke(uncovered);
  assert.notEqual(rejected.status, 0);
  assert.match(rejected.stderr, /Coverage gate failed.*lines: 1 uncovered/);
});
