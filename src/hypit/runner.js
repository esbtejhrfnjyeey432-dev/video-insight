"use strict";

const { spawn } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");

function run(command, args, { cwd, env = process.env, onLine = () => {} } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { cwd, env, shell: false, windowsHide: true });
    let stdout = "", stderr = "";
    child.stdout.on("data", chunk => { const text = chunk.toString(); stdout += text; text.split(/\r?\n/).filter(Boolean).forEach(onLine); });
    child.stderr.on("data", chunk => { const text = chunk.toString(); stderr += text; text.split(/\r?\n/).filter(Boolean).forEach(onLine); });
    child.on("error", reject);
    child.on("close", code => code === 0 ? resolve({ stdout, stderr }) : reject(Object.assign(new Error(`构建引擎命令失败（${code}）：${stderr || stdout}`), { code, stdout, stderr })));
  });
}

function parseBuildId(output) {
  try { const value = JSON.parse(output); return value.buildId || value.id || value.build?.id; } catch (_) {}
  const match = output.match(/(?:build(?:\s+id)?|id)\s*[:=]\s*([A-Za-z0-9._-]+)/i);
  if (!match) throw new Error("构建完成但没有找到任务编号");
  return match[1];
}

function createRunner({ bin = process.env.HYPIT_BIN || "hypit", workspace = process.cwd(), logger = console } = {}) {
  const invoke = (args) => run(bin, args, { cwd: workspace, onLine: line => logger.info(`[video-engine] ${line}`) });
  return {
    async available() { try { await invoke(["version"]); return true; } catch (_) { return false; } },
    runtimeUse(runtimePath) { return invoke(["runtime", "use", runtimePath]); },
    check(runPath) { return invoke(["check", runPath]); },
    plan(runPath) { return invoke(["plan", runPath]); },
    pricing(runPath) { return invoke(["pricing", runPath]); },
    async build(runPath) { const result = await invoke(["build", runPath, "--follow"]); return { ...result, buildId: parseBuildId(result.stdout + "\n" + result.stderr) }; },
    get(buildId, outputPath) { return invoke(["get", buildId, "--output", "final.video", "--to", outputPath]); },
    async execute({ runPath, runtimePath, outputPath, cloud = false, paidAuthorized = false }) {
      if (!await this.available()) throw new Error("服务器尚未安装视频构建引擎");
      await fs.mkdir(path.dirname(outputPath), { recursive: true });
      await this.runtimeUse(runtimePath); await this.check(runPath); await this.plan(runPath);
      let pricing = null;
      if (cloud) {
        pricing = await this.pricing(runPath);
        logger.info(`[video-engine pricing]\n${pricing.stdout || pricing.stderr}`);
        if (!paidAuthorized) throw new Error("已完成费用查询，但服务器尚未授权付费构建");
      }
      const built = await this.build(runPath); await this.get(built.buildId, outputPath);
      return { buildId: built.buildId, outputPath, pricing: pricing && (pricing.stdout || pricing.stderr) };
    }
  };
}

module.exports = { createRunner, parseBuildId, run };
