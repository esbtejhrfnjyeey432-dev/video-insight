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
["esc", "friendlyError", "parseTimecode", "buildMarkdown", "safeFilename", "reportHtml"]
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
assert(html.includes("现有分析成果已保留"),
  "Creative workflow should preserve usable results after an optional step fails");
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
assert(html.includes('esc(meta.speech || "仅画面分析")'),
  "Result metadata must disclose whether platform subtitles, ASR, or vision-only analysis was used");
assert(html.includes('vi_visitor_id') && html.includes('"X-Visitor-Id": visitorId'),
  "Paid generation quota must persist across entry and network changes");

for (const id of ["historyCard", "btnClearHistory", "videoPlayer", "btnEdit", "btnWord", "btnPdf",
  "subtitleFile", "understandingOutput", "sceneAssets", "targetTotalDuration", "referenceScript",
  "capabilityHub", "toolIntent", "btnClearIntent", "btnAgentAudit", "btnAgentContinuity",
  "btnAgentVariants", "agentToolOutput"]) {
  assert(html.includes('id="' + id + '"'), "Missing UI control: " + id);
}
const realTools = [...html.matchAll(/data-tool="([^"]+)"/g)].map((match) => match[1]);
assert(JSON.stringify(realTools) === JSON.stringify(["extract","dialogue","hooks","scripts","storyboard","course"]),
  "Capability hub must only expose the six connected workflows");
for (const unsupported of ["voice-clone", "ocr", "cover-generator", "batch-account"]) {
  assert(!realTools.includes(unsupported), "Unsupported empty tool was exposed: " + unsupported);
}
assert(html.includes("function activateToolIntent"), "Capability cards must route to a real workflow");
assert(html.includes("continueToolIntent();"), "Selected capability must continue after analysis");
assert(html.includes('/api/agent/workbench'), "Workbench Agent tools must call a real backend endpoint");
assert(html.includes('function applyAgentPatches'), "Continuity repair must be applicable, not display-only");
assert(html.includes('class="timeline-jump"') && html.includes('loadedmetadata'),
  "Chapter timeline must be an actionable, metadata-safe video seek control");
for (const path of ["creative.post_copy.titles.", "creative.highlights.",
  "creative.storyboard.", "course.learning_objectives.", "course.slides."]) {
  assert(html.includes(path), "Generated result is not wired to editable state: " + path);
}
assert(html.includes("function applyReportEditingState"),
  "Generated report fields must enter and leave edit mode consistently");
assert(html.includes("成片准备度") && html.includes('audit.status==="not_started"'),
  "An empty project must show a useful staged readiness state instead of a zero score");
assert(html.includes("请先完成第 03 步新脚本，再检查并修复连贯性"),
  "Continuity repair must not run before a script exists");
assert(html.includes("本次分析未提取到足够信息"),
  "Overall summary must have a visible grounded fallback");
for (const id of ["varyProduct", "varyPerson", "varyConflict"]) {
  assert(!html.includes('id="' + id + '"'), "Removed variation toggle is still visible: " + id);
}
assert(html.includes("视频配文案"), "Creative workflow should expose generic video post copy");
assert(!html.includes("小红书文案"), "Creative copy must not be tied to a specific platform");
assert(html.includes('id="btnPpt" data-course-only="1"'),
  "PPTX export should be marked as course-only");
assert(html.includes('scenario === "course" ? "inline-block" : "none"'),
  "PPTX export should be hidden for creative reports");
assert(html.includes('id="gridPayMask"') && html.includes('/api/creative/storyboard-grid/quote'),
  "Paid grid generation must check the server-side experience allowance first");
assert(html.includes("平台处理服务费") && html.includes('id="gridPayTotal"'),
  "Grid payment quote must disclose the service fee and total payable amount");

console.log("Frontend feature tests OK");
