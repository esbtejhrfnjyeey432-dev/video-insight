"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");
const { createRemixSvml, createRun, directionSheet, lookSheet, runtimeConfig } = require("../hypit/remixTemplate");

function validVideoId(value) { return /^[A-Za-z0-9_-]{1,80}$/.test(value || ""); }
function safeText(value, limit = 12000) { return String(value || "").trim().slice(0, limit); }
function normalizeInput(body) {
  const scene = body.scene === "creative" ? "remix" : body.scene;
  if (!validVideoId(body.videoId)) throw new Error("videoId 格式不正确");
  if (!["course", "remix"].includes(scene)) throw new Error("scene 只支持 course 或 remix");
  if (!["standard", "deep", "18min"].includes(body.mode || "standard")) throw new Error("mode 不受支持");
  return {
    ...body, scene, mode: body.mode || "standard", transcript: safeText(body.transcript),
    chapters: Array.isArray(body.chapters) ? body.chapters.slice(0, 80).map((x, i) => ({ t: Number(x.t) || 0, title: safeText(x.title || `章节${i + 1}`, 100) })) : [],
    remixOptions: { keepStructure: true, language: "zh", platform: "douyin", ...(body.remixOptions || {}) },
  };
}

function taskReport(input, plan) {
  return `# 二创构建报告\n\n- 场景：内容整理与二次创作\n- 模式：${input.mode}\n- 平台：${input.remixOptions.platform}\n- 保留原结构：${input.remixOptions.keepStructure ? "是" : "否"}\n- 章节数：${input.chapters.length}\n\n## 智能执行计划\n\n${plan.map((x, i) => `${i + 1}. ${x}`).join("\n")}\n`;
}

function createGenerateService({ root = process.cwd(), queue, runner, courseHandler = null, cloud = false, paidAuthorized = false } = {}) {
  if (!queue || !runner) throw new Error("queue 和 runner 必填");
  const buildRemix = async input => {
    const svmlDir = path.join(root, "data", "svml", input.videoId);
    const runDir = path.join(root, "data", "runs", input.videoId);
    const outputDir = path.join(root, "data", "outputs", input.videoId);
    const runPath = path.join(runDir, "remix.svrun"), runtimePath = path.join(runDir, "hypit.runtime.json");
    const outputPath = path.join(outputDir, "remix.mp4");
    const plan = ["读取原视频章节、转写和二创目标", "按原章节顺序生成语义绑定脚本与 B-roll 计划", "校验工程和依赖", cloud ? "查询云端费用并等待服务端授权" : "使用本地媒体、对齐与渲染能力", "构建成片并登记下载产物"];
    await queue.update(input.videoId, { state: "building", progress: 10, message: "正在生成二创工程", plan });
    await Promise.all([fs.mkdir(svmlDir, { recursive: true }), fs.mkdir(runDir, { recursive: true }), fs.mkdir(outputDir, { recursive: true })]);
    const files = {
      svml: path.join(svmlDir, "remix.svml"), direction: path.join(svmlDir, "direction.svs"), look: path.join(svmlDir, "look.svs"),
      run: runPath, runtime: runtimePath, report: path.join(outputDir, "remix.report.md"),
    };
    await Promise.all([
      fs.writeFile(files.svml, createRemixSvml(input), "utf8"), fs.writeFile(files.direction, directionSheet(), "utf8"),
      fs.writeFile(files.look, lookSheet(), "utf8"), fs.writeFile(files.run, createRun(input.videoId), "utf8"),
      fs.writeFile(files.runtime, runtimeConfig(cloud ? "cloud" : "local"), "utf8"), fs.writeFile(files.report, taskReport(input, plan), "utf8"),
    ]);
    await queue.update(input.videoId, { progress: 35, message: "正在校验并规划二创任务" });
    const built = await runner.execute({ runPath, runtimePath, outputPath, cloud, paidAuthorized });
    const artifacts = [outputPath, files.svml, files.report].filter(Boolean).map(file => ({ name: path.basename(file), path: path.relative(root, file).replaceAll("\\", "/") }));
    return queue.update(input.videoId, { state: "ready", progress: 100, message: "二创任务可下载", buildId: built.buildId, artifacts });
  };
  return {
    async generate(body) {
      const input = normalizeInput(body || {});
      if (input.scene === "course") {
        if (!courseHandler) return { delegated: true, scene: "course", message: "继续使用原课程复盘流程" };
        return courseHandler(input);
      }
      const task = await queue.create(input);
      setImmediate(() => buildRemix(input).catch(async error => {
        await queue.update(input.videoId, { state: "failed", progress: 0, message: "智能成片未完成，原二创工作台仍可继续使用", error: { reason: error.message, fallback: "保留脚本、分镜与提示词结果，稍后可重新构建" } });
      }));
      return task;
    },
    get(videoId) { if (!validVideoId(videoId)) throw new Error("videoId 格式不正确"); return queue.get(videoId); },
    normalizeInput,
  };
}

module.exports = { createGenerateService, normalizeInput };
