#!/usr/bin/env node
/**
 * Windows/macOS/Linux launcher for the agent-handoff skill.
 *
 * Why this exists: Codex CLI on this machine runs commands with a PowerShell
 * execution policy that blocks .ps1 files, and no `python` is on PATH. This
 * launcher finds a usable Python 3.8+, remembers it in `.agent-state/local.json`
 * (machine local, git ignored) and forwards to the real Python CLIs.
 *
 * The Python scripts stay the canonical, agent-agnostic interface:
 *     python .agents/skills/agent-handoff/scripts/<command>.py --help
 *
 * Usage:
 *     node scripts/handoff.mjs <command> [args...]
 *     node scripts/handoff.mjs setup [--agent codex] [--no-global]
 *     node scripts/handoff.mjs uninstall
 *
 * Commands: start save release resume finish inspect doctor tests simulate setup uninstall
 */

import { spawnSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const IS_WIN = process.platform === "win32";
const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(scriptDir, "..");
const stateDir = path.join(repoRoot, ".agent-state");
const localConfigPath = path.join(stateDir, "local.json");
const skillScripts = path.join(repoRoot, ".agents", "skills", "agent-handoff", "scripts");

const COMMAND_SCRIPTS = {
  start: "checkpoint.py",
  save: "checkpoint.py",
  release: "checkpoint.py",
  resume: "resume.py",
  finish: "finish.py",
  inspect: "inspect.py",
  doctor: "doctor.py",
  tests: "run_tests.py",
  simulate: "simulate_handoff.py",
};

const USAGE = `agent-handoff launcher

  node scripts/handoff.mjs <command> [args...]

commands
  start | save | release      checkpoint.py (create task, checkpoint, release lock)
  resume                      compare the checkpoint with git and the work tree
  finish                      verify, commit, archive
  inspect                     read-only report
  doctor                      environment self check
  tests                       run the test suite
  simulate                    end-to-end handoff simulation in work/

setup
  node scripts/handoff.mjs setup [--agent codex] [--no-global]
      find Python, write .agent-state/local.json, append the managed block to
      $CODEX_HOME/AGENTS.md (skip with --no-global), then verify Codex sees the skill.

  node scripts/handoff.mjs uninstall
      remove the managed block and local.json (the skill and state are kept).
`;

const GLOBAL_BLOCK_START = "<!-- agent-handoff:begin (managed by scripts/handoff.mjs) -->";
const GLOBAL_BLOCK_END = "<!-- agent-handoff:end -->";

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function readLocal() {
  try {
    return JSON.parse(fs.readFileSync(localConfigPath, "utf8"));
  } catch {
    return {};
  }
}

function writeLocal(data) {
  fs.mkdirSync(stateDir, { recursive: true });
  const payload = { ...data, updated_at: new Date().toISOString() };
  fs.writeFileSync(localConfigPath, JSON.stringify(payload, null, 2) + "\n", "utf8");
}

function probe(candidate) {
  if (!candidate || !candidate.exe) return null;
  const args = [...(candidate.prefix || []), "-c", "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)"];
  const result = spawnSync(candidate.exe, args, { stdio: "ignore", timeout: 20000, windowsHide: true });
  return result.status === 0 ? candidate : null;
}

function resolveOnPath(name) {
  const probeCmd = IS_WIN ? "where" : "which";
  const result = spawnSync(probeCmd, [name], { encoding: "utf8", windowsHide: true });
  if (result.status !== 0 || !result.stdout) return null;
  const first = result.stdout.split(/\r?\n/).map((line) => line.trim()).find(Boolean);
  if (!first) return null;
  if (IS_WIN && /\\WindowsApps\\/i.test(first)) return null; // Microsoft Store alias stub
  return first;
}

function globPython() {
  const patterns = [
    path.join(os.homedir(), ".dsh", "dsh-runtimes"),
    path.join(process.env.LOCALAPPDATA || "", "Programs", "Python"),
    process.env.ProgramFiles || "C:\\Program Files",
  ];
  const found = [];
  const runtimeRoot = patterns[0];
  try {
    for (const name of fs.readdirSync(runtimeRoot)) {
      const candidate = path.join(runtimeRoot, name, "dependencies", "python", "python.exe");
      if (fs.existsSync(candidate)) found.push(candidate);
    }
  } catch {
    /* no DSH runtime: fine */
  }
  for (const root of patterns.slice(1)) {
    try {
      for (const name of fs.readdirSync(root)) {
        if (!/^Python3/i.test(name)) continue;
        const candidate = path.join(root, name, "python.exe");
        if (fs.existsSync(candidate)) found.push(candidate);
      }
    } catch {
      /* not installed: fine */
    }
  }
  return found;
}

function findPython(local) {
  const candidates = [];
  if (process.env.AGENT_HANDOFF_PYTHON) {
    candidates.push({ exe: process.env.AGENT_HANDOFF_PYTHON, prefix: [], source: "AGENT_HANDOFF_PYTHON" });
  }
  if (local.python) {
    candidates.push({ exe: local.python, prefix: Array.isArray(local.python_prefix) ? local.python_prefix : [], source: "local.json" });
  }
  const py = resolveOnPath("py");
  if (py) candidates.push({ exe: "py", prefix: ["-3"], source: "py launcher" });
  const python = resolveOnPath("python");
  if (python) candidates.push({ exe: python, prefix: [], source: "PATH python" });
  const python3 = resolveOnPath("python3");
  if (python3) candidates.push({ exe: python3, prefix: [], source: "PATH python3" });
  for (const exe of globPython()) {
    candidates.push({ exe, prefix: [], source: "known install location" });
  }
  for (const candidate of candidates) {
    const ok = probe(candidate);
    if (ok) return ok;
  }
  return null;
}

function pythonVersion(exe, prefix) {
  const result = spawnSync(exe, [...(prefix || []), "-c", "import sys; print(sys.version.split()[0])"], {
    encoding: "utf8",
    windowsHide: true,
  });
  return result.status === 0 ? result.stdout.trim() : "unknown";
}

function runChild(exe, args, options = {}) {
  return spawnSync(exe, args, {
    cwd: options.cwd || process.cwd(),
    stdio: options.stdio || "inherit",
    env: options.env || process.env,
    encoding: "utf8",
    windowsHide: false,
  });
}

function runCmdLine(commandLine, options = {}) {
  if (IS_WIN) {
    return spawnSync("cmd.exe", ["/d", "/s", "/c", commandLine], {
      cwd: options.cwd || process.cwd(),
      encoding: "utf8",
      windowsHide: true,
      timeout: options.timeout || 120000,
      maxBuffer: 32 * 1024 * 1024,
    });
  }
  return spawnSync("/bin/sh", ["-c", commandLine], {
    cwd: options.cwd || process.cwd(),
    encoding: "utf8",
    timeout: options.timeout || 120000,
    maxBuffer: 32 * 1024 * 1024,
  });
}

function collectTexts(node, out) {
  if (Array.isArray(node)) {
    for (const item of node) collectTexts(item, out);
  } else if (node && typeof node === "object") {
    for (const [key, value] of Object.entries(node)) {
      if (key === "text" && typeof value === "string") out.push(value);
      else collectTexts(value, out);
    }
  }
}

function hasAgent() {
  return process.argv.slice(2).some((arg) => arg === "--agent" || arg.startsWith("--agent="));
}

// ---------------------------------------------------------------------------
// setup / uninstall
// ---------------------------------------------------------------------------
function codexHome() {
  return process.env.CODEX_HOME || path.join(os.homedir(), ".codex");
}

function codexVisible() {
  const version = runCmdLine("codex --version", { cwd: repoRoot });
  if (version.status !== 0) {
    return { ok: false, reason: "codex CLI not found on PATH" };
  }
  const result = runCmdLine('codex debug prompt-input "resume-task"', { cwd: repoRoot, timeout: 180000 });
  if (result.status !== 0) {
    return { ok: false, reason: "codex debug prompt-input failed", output: (result.stderr || "").slice(0, 400) };
  }
  const text = result.stdout || "";
  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  let skillsSeen = false;
  let agentsSeen = false;
  if (start >= 0 && end > start) {
    try {
      const data = JSON.parse(text.slice(start, end + 1));
      const texts = [];
      collectTexts(data, texts);
      const expected = path.join(repoRoot, ".agents", "skills", "agent-handoff", "SKILL.md").replace(/\\/g, "/").toLowerCase();
      skillsSeen = texts.some((item) => item.includes("<skills_instructions>") && item.split("\n").some((line) => /^- agent-handoff:/.test(line) && line.replace(/\\/g, "/").toLowerCase().includes(expected)));
      agentsSeen = texts.some((item) => item.includes("Agent Handoff"));
    } catch {
      skillsSeen = false;
    }
  } else {
    skillsSeen = false;
  }
  return { ok: skillsSeen, skills: skillsSeen, agentsMd: agentsSeen, version: version.stdout.trim() };
}

function globalAgentsPath() {
  return path.join(codexHome(), "AGENTS.md");
}

function managedBlock(repo) {
  return [
    GLOBAL_BLOCK_START,
    "## 多 Agent 接力（agent-handoff）",
    "",
    "当仓库里存在 `.agents/skills/agent-handoff/SKILL.md` 时，用它管理跨 Agent 的长期任务：",
    "",
    "- 开工前先读仓库 `AGENTS.md` 和该 `SKILL.md`；动态状态只在 `.agent-state/`，不要写进 `AGENTS.md`。",
    "- Windows 上在仓库根运行 `scripts\\Handoff.cmd <start|save|resume|finish|inspect|doctor> --agent codex`（不需要 python 在 PATH）。",
    "- 恢复任务先 `resume`，按输出的 MODE 与检查清单核对真实代码；是否提交遵循用户要求和项目约定。",
    "- 禁止 `git reset --hard`、`git clean`、强推；真实代码与 Git 高于 checkpoint 描述。",
    GLOBAL_BLOCK_END,
  ].join("\n");
}

function installGlobalBlock(repo, quiet) {
  const target = globalAgentsPath();
  const existing = fs.existsSync(target) ? fs.readFileSync(target, "utf8") : "";
  if (existing.includes(GLOBAL_BLOCK_START)) {
    const start = existing.indexOf(GLOBAL_BLOCK_START);
    const end = existing.indexOf(GLOBAL_BLOCK_END);
    if (end > start) {
      const replacement = existing.slice(0, start) + managedBlock(repo) + existing.slice(end + GLOBAL_BLOCK_END.length);
      if (replacement !== existing) fs.writeFileSync(target, replacement, "utf8");
      if (!quiet) console.log(`[ok]   global instructions already installed: ${target}`);
      return target;
    }
  }
  const separator = existing.endsWith("\n") || existing === "" ? "" : "\n";
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, existing + separator + "\n" + managedBlock(repo) + "\n", "utf8");
  if (!quiet) console.log(`[ok]   appended the handoff block to ${target}`);
  return target;
}

function removeGlobalBlock(quiet) {
  const target = globalAgentsPath();
  if (!fs.existsSync(target)) {
    if (!quiet) console.log(`[skip] ${target} does not exist`);
    return;
  }
  const existing = fs.readFileSync(target, "utf8");
  const start = existing.indexOf(GLOBAL_BLOCK_START);
  const end = existing.indexOf(GLOBAL_BLOCK_END);
  if (start < 0 || end < start) {
    if (!quiet) console.log("[skip] no managed block found");
    return;
  }
  const cleaned = (existing.slice(0, start) + existing.slice(end + GLOBAL_BLOCK_END.length))
    .replace(/\n{3,}/g, "\n\n")
    .replace(/\s+$/, "\n");
  fs.writeFileSync(target, cleaned, "utf8");
  if (!quiet) console.log(`[ok]   removed the handoff block from ${target}`);
}

function setup(argv) {
  const quiet = argv.includes("--quiet");
  const global = !argv.includes("--no-global");
  const agentIndex = argv.indexOf("--agent");
  const agent = agentIndex >= 0 && argv[agentIndex + 1] ? argv[agentIndex + 1] : "codex";
  const local = readLocal();
  const python = findPython(local);

  if (!fs.existsSync(path.join(skillScripts, "doctor.py"))) {
    console.error(`agent-handoff skill is missing from ${skillScripts}`);
    return 2;
  }

  if (!python) {
    console.error("No Python 3.8+ interpreter found.");
    console.error("Install Python (https://www.python.org/downloads/) or point the launcher at one:");
    console.error('  set AGENT_HANDOFF_PYTHON=C:\\path\\to\\python.exe');
    console.error("Then run: node scripts/handoff.mjs setup");
    return 2;
  }

  writeLocal({ python: python.exe, python_prefix: python.prefix || [], python_source: python.source, agent });
  const version = pythonVersion(python.exe, python.prefix);
  if (!quiet) {
    console.log("agent-handoff setup");
    console.log(`  repo          ${repoRoot}`);
    console.log(`  python        ${python.exe}  (${version}, via ${python.source})`);
    console.log(`  local config  ${localConfigPath}  (machine local, git ignored)`);
    console.log(`  launcher      ${path.join(scriptDir, "Handoff.cmd")}  +  ${path.join(scriptDir, "handoff.mjs")}`);
  }

  let globalPath = null;
  if (global) globalPath = installGlobalBlock(repoRoot, quiet);

  // doctor gives the environment a real check; its exit code is reported, not hidden
  const doctor = runChild(
    python.exe,
    [...(python.prefix || []), path.join(skillScripts, "doctor.py"), "--fix", "--agent", agent],
    { cwd: repoRoot }
  );
  const doctorCode = typeof doctor.status === "number" ? doctor.status : 2;

  const visibility = codexVisible();
  if (!quiet) {
    console.log("");
    console.log("verification");
    console.log(`  doctor        exit ${doctorCode}${doctorCode === 0 ? " (healthy)" : " (see warnings above)"}`);
    console.log(
      `  codex skill   ${visibility.ok ? "visible: agent-handoff is in the Codex skill list" : "NOT confirmed: " + (visibility.reason || "skill not found in prompt")}`
    );
    if (visibility.ok) {
      console.log(`  codex agents  ${visibility.agentsMd ? "project AGENTS.md handoff section is in the prompt" : "handoff section not detected in AGENTS.md"}`);
    }
    console.log("");
    console.log(doctorCode >= 2 ? "Setup incomplete: fix the doctor errors above." : "Setup complete. In Codex (repo root) say: 继续没做完的任务  /  开始做 <任务>");
    console.log("or run:  scripts\\Handoff.cmd resume --agent codex");
    console.log("Remove the global block later with: node scripts/handoff.mjs uninstall");
  }
  if (doctorCode >= 2) return doctorCode;
  return visibility.ok ? 0 : 1;
}

function uninstall(argv) {
  const quiet = argv.includes("--quiet");
  removeGlobalBlock(quiet);
  if (fs.existsSync(localConfigPath)) {
    fs.rmSync(localConfigPath);
    if (!quiet) console.log(`[ok]   removed ${localConfigPath}`);
  }
  if (!quiet) {
    console.log("The skill (.agents/skills/agent-handoff) and .agent-state/ were kept.");
    console.log("Reinstall with: node scripts/handoff.mjs setup");
  }
  return 0;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
function main() {
  const argv = process.argv.slice(2);
  if (argv.length === 0 || argv[0] === "--help" || argv[0] === "-h" || argv[0] === "help") {
    console.log(USAGE);
    return 0;
  }
  const command = argv[0];
  const rest = argv.slice(1);

  if (command === "setup") return setup(rest);
  if (command === "uninstall") return uninstall(rest);

  const scriptName = COMMAND_SCRIPTS[command];
  if (!scriptName) {
    console.error(`unknown command: ${command}`);
    console.error(USAGE);
    return 2;
  }
  const scriptPath = path.join(skillScripts, scriptName);
  if (!fs.existsSync(scriptPath)) {
    console.error(`agent-handoff script missing: ${scriptPath}`);
    console.error("Is the skill installed in this repository (.agents/skills/agent-handoff)?");
    return 2;
  }

  const local = readLocal();
  const python = findPython(local);
  if (!python) {
    console.error("No Python 3.8+ interpreter found for agent-handoff.");
    console.error('Run "node scripts/handoff.mjs setup" or set AGENT_HANDOFF_PYTHON.');
    return 2;
  }
  if (local.python !== python.exe) {
    try {
      writeLocal({
        python: python.exe,
        python_prefix: python.prefix || [],
        python_source: python.source,
        agent: local.agent || "codex",
      });
    } catch {
      /* read-only checkout: not fatal */
    }
  }

  const env = { ...process.env };
  if (!env.AGENT_HANDOFF_AGENT && !hasAgent()) {
    env.AGENT_HANDOFF_AGENT = local.agent || "codex";
  }
  env.PYTHONUTF8 = env.PYTHONUTF8 || "1";
  env.PYTHONIOENCODING = env.PYTHONIOENCODING || "utf-8";

  const result = runChild(python.exe, [...(python.prefix || []), scriptPath, ...rest], {
    env,
    cwd: repoRoot,
  });
  return typeof result.status === "number" ? result.status : 2;
}

process.exit(main());
