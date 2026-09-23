"use strict";

const fs = require("node:fs/promises");
const path = require("node:path");

const STATES = new Set(["pending", "building", "ready", "failed"]);

class BuildQueue {
  constructor({ root = path.resolve("data/queue") } = {}) {
    this.root = root;
    this.tasks = new Map();
  }

  async create(input) {
    const id = input.videoId;
    if (!/^[A-Za-z0-9_-]{1,80}$/.test(id || "")) throw new Error("videoId 格式不正确");
    const task = { id, scene: input.scene, state: "pending", progress: 0,
      message: "二创任务已进入队列", artifacts: [], createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() };
    this.tasks.set(id, task); await this.persist(task); return { ...task };
  }

  async update(id, patch) {
    const current = this.tasks.get(id) || await this.read(id);
    if (!current) throw new Error("任务不存在");
    if (patch.state && !STATES.has(patch.state)) throw new Error("非法任务状态");
    const next = { ...current, ...patch, id, updatedAt: new Date().toISOString() };
    this.tasks.set(id, next); await this.persist(next); return { ...next };
  }

  async get(id) { return this.tasks.get(id) || await this.read(id); }

  async persist(task) {
    await fs.mkdir(this.root, { recursive: true });
    await fs.writeFile(path.join(this.root, `${task.id}.json`), JSON.stringify(task, null, 2), "utf8");
  }

  async read(id) {
    try { return JSON.parse(await fs.readFile(path.join(this.root, `${id}.json`), "utf8")); }
    catch (error) { if (error.code === "ENOENT") return null; throw error; }
  }
}

module.exports = { BuildQueue, STATES };
