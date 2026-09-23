"use strict";

const http = require("node:http");
const { URL } = require("node:url");
const { BuildQueue } = require("./queue/queue");
const { createRunner } = require("./hypit/runner");
const { createGenerateService } = require("./routes/generate");

const port = Number(process.env.HYPIT_WORKER_PORT || 3188);
const root = process.cwd();
const queue = new BuildQueue();
const runner = createRunner({ workspace: root });
const service = createGenerateService({ root, queue, runner, cloud: process.env.HYPIT_RUNTIME === "cloud", paidAuthorized: process.env.HYPIT_PAID_AUTHORIZED === "true" });

function reply(res, status, value) { const body = JSON.stringify(value); res.writeHead(status, { "content-type": "application/json; charset=utf-8", "content-length": Buffer.byteLength(body) }); res.end(body); }
function body(req) { return new Promise((resolve, reject) => { let raw = ""; req.on("data", chunk => { raw += chunk; if (raw.length > 2_000_000) reject(new Error("请求内容过大")); }); req.on("end", () => { try { resolve(raw ? JSON.parse(raw) : {}); } catch (_) { reject(new Error("JSON 格式不正确")); } }); req.on("error", reject); }); }

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
    if (req.method === "GET" && url.pathname === "/health") return reply(res, 200, { ok: true, service: "creative-builder" });
    if (req.method === "POST" && url.pathname === "/api/generate") return reply(res, 202, await service.generate(await body(req)));
    const match = req.method === "GET" && url.pathname.match(/^\/api\/generate\/([A-Za-z0-9_-]+)$/);
    if (match) { const task = await service.get(match[1]); return task ? reply(res, 200, task) : reply(res, 404, { detail: "任务不存在" }); }
    return reply(res, 404, { detail: "接口不存在" });
  } catch (error) { return reply(res, 400, { detail: error.message }); }
});

if (require.main === module) server.listen(port, "127.0.0.1", () => console.log(`Creative builder listening on 127.0.0.1:${port}`));
module.exports = { server };
