"use strict";

function xml(value) { return String(value ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&apos;"); }
function cleanId(value, fallback) { const id = String(value || "").toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, ""); return /^[a-z]/.test(id) ? id.slice(0, 48) : fallback; }

function chapterCopy(input) {
  const options = input.remixOptions || {}, chapters = input.chapters?.length ? input.chapters : [{ t: 0, title: "内容" }];
  return chapters.map((chapter, index) => {
    const title = String(chapter.title || `章节${index + 1}`);
    let line = `${options.person || "虚拟主播"}介绍${options.product || "新产品"}：${title}。`;
    if (index === 0 && options.newHook) line = options.newHook + "。" + line;
    if (index === chapters.length - 1 && options.cta) line += options.cta + "。";
    const nextAt = Number(chapters[index + 1]?.t), currentAt = Number(chapter.t) || 0;
    const sourceDuration = Number.isFinite(nextAt) && nextAt > currentAt ? nextAt - currentAt : Math.max(3, line.length / 5);
    return { id: cleanId(title, `chapter-${index + 1}`), line, broll: options.broll?.[index % Math.max(1, options.broll?.length || 1)] || title,
      requestDuration: Math.max(3, Math.min(15, Math.round(sourceDuration))), sourceAt: currentAt };
  });
}

function createRemixSvml(input) {
  const options = input.remixOptions || {}, language = options.language || "zh", parts = chapterCopy(input);
  const segments = parts.map((part, index) => `    <${part.id}><HOST>@{chapter-${index + 1}}${xml(part.line)}@{/chapter-${index + 1}}</${part.id}>`).join("\n");
  const media = parts.map((part, index) => `
  <text:Value id="direction-${index + 1}">竖屏社交短视频；人物：${xml(options.person || "虚拟主播")}；产品：${xml(options.product || "产品")}；本章节插入 B-roll：${xml(part.broll)}；保持原章节顺序与节奏。</text:Value>
  <text:Render id="prompt-${index + 1}" template={kit.performance}>
    <text:Set name="direction" text={direction-${index + 1}}/>
    <text:Set name="dialogue" text={story.segment.${part.id}.dialogue}/>
  </text:Render>
  <seedance:TextVideo id="performance-${index + 1}" model="mini" prompt={prompt-${index + 1}} duration="${part.requestDuration}" aspect-ratio="9:16" generate-audio="true"/>
  <pipeline:Normalize id="media-${index + 1}" source={performance-${index + 1}.video} clock={clock} video="primary-moving" audio="default" span-authority="video"/>
  <whisperx:SemanticTake id="take-${index + 1}" narrative={story} segment={story.segment.${part.id}} media={media-${index + 1}.media} language="${xml(language)}"/>`).join("\n");
  const takes = parts.map((_, i) => `    <time:Take source={take-${i + 1}.take}/>`).join("\n");
  return `<?svml using="@hypit/markup@1"?>
<svml>
  <import as="sound" from="@hypit/sound@1"/>
  <import as="performance" from="@hypit/performance@1"/>
  <import from="@hypit/script@1"/>
  <import as="text" from="@hypit/text@1"/>
  <import as="seedance" from="@hypit/seedance@1"/>
  <import as="pipeline" from="@hypit/media-pipeline@1"/>
  <import as="whisperx" from="@hypit/whisperx@1"/>
  <import as="time" from="@hypit/timeline-author@1"/>
  <import as="space" from="@hypit/spatial@1"/>
  <import as="program" from="@hypit/program-space@1"/>
  <import as="film" from="@hypit/film@1"/>
  <import as="render" from="@hypit/render-hyperframes@1"/>
  <import as="look" source="./look.svs"/>
  <import as="kit" source="./direction.svs"/>
  <script id="story">
${segments}
  </script>
  <space:Canvas id="canvas" width="720" height="1280"/>
  <program:Clock id="clock" frame-rate="30"/>
  <space:Frame id="full" within={canvas} left="0%" top="0%" right="100%" bottom="100%"/>
${media}
  <time:Timeline id="program" clock={clock}>
${takes}
  </time:Timeline>
  <sound:Style id="voice-style"/>
  <sound:Track id="voice" timeline={program.timeline}><sound:Use style={voice-style}/></sound:Track>
  <performance:Style id="picture-style" frame={full} appearance={look.media.performance}/>
  <performance:Track id="picture" timeline={program.timeline} canvas={canvas}><performance:Use style={picture-style} during="program"/></performance:Track>
  <film:Film id="main" canvas={canvas} timeline={program.timeline} appearance={look.film.main}><film:Track source={picture.visual}/><film:Track source={voice.audio}/></film:Film>
  <render:Video id="final" composition={main.composition} timeline={program.timeline}/>
</svml>\n`;
}

function createRun(videoId) { return `<?svml using="@hypit/run-markup@1"?>\n<svrun version="1">\n  <author source="../../svml/${xml(videoId)}/remix.svml"/>\n  <target output="final.video"/>\n</svrun>\n`; }
function directionSheet() { return `<?svml using="@hypit/text/svs@1"?>\n<sheet version="1" id="performance">\n  text-template.performance { separator: paragraph; }\n  text-template.performance.block.dialogue { kind: slot; order: 10; slot: dialogue; }\n  text-template.performance.block.direction { kind: slot; order: 20; slot: direction; }\n</sheet>\n`; }
function lookSheet() { return `<?svml using="@hypit/svs@1"?>\n<sheet version="1">\n  media.performance { fit: cover; stack-order: 10; }\n  film.main { background: "#10141c"; }\n</sheet>\n`; }
function runtimeConfig(kind = "local") {
  const endpoints = { "media.local": { use: "@hypit/provider-media-local" }, "whisperx.local": { use: "@hypit/provider-whisperx-local", config: { expectedModel: "small", expectedDevice: "cpu", expectedCompute: "int8", alignmentLanguages: ["zh", "en"] } }, "hyperframes.local": { use: "@hypit/provider-hyperframes-local" } };
  const profile = { format: "hypit.runtime-local@1", dataRoot: ".hypit/execution", credentials: {}, endpoints, bindings: { "@hypit/whisperx@1#whisperx-alignment": "whisperx.local" } };
  if (kind === "cloud") { profile.credentials.env = { use: "@hypit/credential-store-env" }; profile.endpoints["hypihub.default"] = { use: "@hypit/provider-hypihub", config: { baseUrl: "https://hypit.ai", apiKey: { store: "env", key: "HYPIT_HUB_API_KEY" } } }; profile.bindings["@hypit/seedance@1#seedance-2-mini"] = "hypihub.default"; }
  return JSON.stringify(profile, null, 2) + "\n";
}

module.exports = { createRemixSvml, createRun, directionSheet, lookSheet, runtimeConfig, chapterCopy };
