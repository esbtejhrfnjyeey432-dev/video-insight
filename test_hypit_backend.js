"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const os = require("node:os");
const fs = require("node:fs/promises");
const path = require("node:path");
const { createRemixSvml, createRun, runtimeConfig } = require("./src/hypit/remixTemplate");
const { BuildQueue } = require("./src/queue/queue");
const { parseBuildId } = require("./src/hypit/runner");
const sample = { videoId: "abc123", chapters: [{ t: 0, title: "钩子" }, { t: 8, title: "痛点" }], remixOptions: { newHook: "别再手抄笔记了", product: "AI 笔记 App", person: "虚拟主播小知", language: "zh", cta: "左下角免费体验", broll: ["产品界面", "用户惊讶"] } };

test("模板使用语义选区并生成竖屏二创工程", () => { const text = createRemixSvml(sample); assert.match(text, /width="720" height="1280"/); assert.match(text, /@\{chapter-1\}/); assert.match(text, /AI 笔记 App/); assert.match(text, /产品界面/); assert.doesNotMatch(text, /Claude Code|Codex|\/hypit/); });
test("运行文件包含真实视频 ID", () => assert.match(createRun("abc123"), /svml\/abc123\/remix\.svml/));
test("云端运行配置通过环境变量读取密钥", () => { const value = JSON.parse(runtimeConfig("cloud")); assert.equal(value.endpoints["hypihub.default"].config.apiKey.key, "HYPIT_HUB_API_KEY"); assert.equal(JSON.stringify(value).includes("sk-"), false); });
test("队列持久化四种状态", async () => { const root = await fs.mkdtemp(path.join(os.tmpdir(), "vi-queue-")); const q = new BuildQueue({ root }); await q.create({ videoId: "task1", scene: "remix" }); await q.update("task1", { state: "building" }); await q.update("task1", { state: "ready" }); assert.equal((await q.get("task1")).state, "ready"); });
test("解析构建编号", () => { assert.equal(parseBuildId('{"buildId":"build-123"}'), "build-123"); assert.equal(parseBuildId("Build ID: job_99"), "job_99"); });
