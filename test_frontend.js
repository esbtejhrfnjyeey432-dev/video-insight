"use strict";

const fs = require("fs");
const vm = require("vm");

const html = fs.readFileSync("static/index.html", "utf8");
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)]
  .map((match) => match[1]).join("\n");

function functionSource(name) {
  const marker = "function " + name + "(";
  const start = scripts.indexOf(marker);
  if (start < 0) throw new Error("Missing function: " + name);
  const open = scripts.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < scripts.length; i += 1) {
    if (scripts[i] === "{") depth += 1;
    if (scripts[i] === "}") depth -= 1;
    if (depth === 0) return scripts.slice(start, i + 1);
  }
  throw new Error("Unclosed function: " + name);
}

const context = vm.createContext({ console });
["esc", "friendlyError", "creativeFailureCopy", "parseTimecode", "buildMarkdown", "safeFilename", "reportHtml"]
  .forEach((name) => vm.runInContext(functionSource(name), context));

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

new vm.Script(scripts);
assert(context.parseTimecode("01:02:03") === 3723, "HH:MM:SS conversion failed");
assert(context.parseTimecode("12:34") === 754, "MM:SS conversion failed");
assert(context.friendlyError("HTTP 403 anti-bot challenge", 400).includes("阻止"),
  "Platform restriction classification failed");
assert(context.friendlyError("服务繁忙", 503).includes("任务较多"),
  "Busy-service classification failed");
assert(context.creativeFailureCopy("script", new Error("HTTP 429")).reason.includes("额度"),
  "Creative quota failure classification failed");
assert(context.creativeFailureCopy("deconstruct", new Error("视频格式不支持")).action.includes("章节时间轴"),
  "Creative deconstruction fallback explanation failed");
assert(!context.safeFilename('a/b:c*?"<>|').match(/[\\/:*?"<>|]/),
  "Filename sanitization failed");

const sample = {
  title: "测试报告",
  category: "课程",
  tags: ["测试"],
  key_info: ["已编辑的关键信息"],
  chapters: [{time: "00:10", label: "第一章"}],
  speech_summary: "语音摘要",
  teaching: {is_teaching: false},
  remix: {cards: [], script: "口播", clips: [], quotes: []},
  overall_summary: "总体总结",
  _meta: {platform: "本地文件", duration: 30, speech: "仅画面分析"},
};
assert(context.buildMarkdown(sample).includes("已编辑的关键信息"),
  "Markdown export missed edited content");
assert(context.reportHtml(sample).includes("语音内容摘要"),
  "Word/PDF export missed speech summary");

for (const id of ["historyCard", "btnClearHistory", "videoPlayer", "btnEdit", "btnWord", "btnPdf",
  "creativeFailure", "btnCreativeRetry", "btnCreativeFallback", "subtitleFile",
  "understandingOutput", "sceneAssets", "targetTotalDuration", "referenceScript"]) {
  assert(html.includes('id="' + id + '"'), "Missing UI control: " + id);
}
for (const id of ["varyProduct", "varyPerson", "varyConflict"]) {
  assert(!html.includes('id="' + id + '"'), "Removed variation toggle is still visible: " + id);
}
assert(html.includes("视频配文案"), "Creative workflow should expose generic video post copy");
assert(!html.includes("小红书文案"), "Creative copy must not be tied to a specific platform");

console.log("Frontend feature tests OK");
