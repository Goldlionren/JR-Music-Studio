"use strict";
const $ = (id) => document.getElementById(id);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const state = {
  csrf: null,
  libraries: [],
  lib: null,
  pid: null,
  data: null,
  selected: [],
  focus: null,
  bars: new Set(),
  renders: {},
  busy: false,
  tab: "score",
  load: 0,
};
const names = { yinyue: "银月", xiaowu: "小舞", producer: "制作人" };
const messages = {
  MCP_ROUTING_UNAVAILABLE: 'MCP 路由尚未启用，请检查服务配置。',
  MCP_DISCOVERY_REQUIRED: '请先读取 MCP 列表，再选择可用的生成服务器。',
  MCP_SERVER_UNAVAILABLE: '所选 MCP 当前不可用，请读取列表核对状态或选择其他服务器。',
  MCP_CONFIGURATION_CHANGED: '该 MCP 配置在任务提交后发生变化，请重新读取列表并提交新任务。',
  MCP_BROKER_UNAVAILABLE: '暂时无法连接 Hermes 的 MCP 探测助手，请检查网络和服务。',
  MCP_OFFLINE_OR_PROBE_FAILED: '离线或探测超时',
  MCP_STAGING_TRANSPORT_UNSUPPORTED: '暂不支持该传输方式',
  MCP_WORKFLOW_TOOL_UNAVAILABLE: '不提供工作流生成工具',
  MCP_WORKFLOW_TOOL_EXCLUDED: '工作流生成工具被禁用',
  MCP_DISABLED: 'Hermes 中已禁用',
  TARGET_YUE2_NODES_MISSING: '缺少 YuE2 节点',
  TARGET_YUE2_MODEL_MISSING: '缺少 YuE2 模型',

  EXPORT_SCORE_UNSUPPORTED: "谱面尚未完整通过校验，请先处理解析或拍数问题再导出。",
  EXPORT_TEMPO_REQUIRED: "请先在谱面设置明确速度 Q:1/4=…，再导出。",
  EXPORT_TEMPO_RANGE: "谱面速度超出 MIDI 可表达范围。",
  EXPORT_METER_UNSUPPORTED: "当前拍号暂不能可靠导出为 MIDI。",
  EXPORT_TIMING_RESOLUTION: "音符时值超出无损 MIDI 导出的精度范围。",
  EXPORT_SIZE_LIMIT: "谱面超过导出上限（15 个声部、10000 个事件、1 小时）。",
  EXPORT_PITCH_RANGE: "音高超出当前 MIDI / MusicXML 共同支持的范围。",
  EXPORT_UNANCHORED_CHORD: "存在没有对应音符或休止符的和弦标记，请先检查谱面。",
  EXPORT_INVALID_TEXT: "谱面或歌词含外部乐谱格式不接受的控制字符，请先清理原文。",
  EXPORT_TIMING_RANGE: "谱面时间位置超出 MIDI 格式范围。",
  CREATIVE_JSON_DUPLICATE_KEY: "回复含有重复字段，无法安全判定采用哪份内容，已停止。原回复保留，可重新创作。",
  CREATIVE_FORMAT_AMBIGUOUS: "规格存在多份冲突字段，格式检查无法自动选择，已停止。原回复保留。",
  CREATIVE_RESPONSE_TRUNCATED: "模型回复被截断，不能靠补括号恢复缺失内容。请重新创作。",
  CREATIVE_FORMAT_COMPLETION_UNVERIFIED: "尚不能确认模型完整结束，未自动修复回复。",
  CREATIVE_RECOVERY_SOURCE_MISSING: "找不到原会话的回复文件，未重新调用模型。请检查原机文件或选择重新创作。",
  FORMAT_RECOVERY_NOT_AVAILABLE: "此任务已提交提案或进入渲染，不能按格式错误重新恢复。请核对当前记录。",
  FORMAT_RECEIPT_MISMATCH: "服务器复核格式修复记录不一致，未提交渲染。",
  INVALID_FORMAT_RECEIPT: "格式检查记录无法验证，未提交渲染。",
  HERMES_PROVIDER_TIMEOUT: "Hermes 等待模型服务超时，没有收到完整创作回复，也没有提交音频生成。可重新尝试。",
  INVALID_SONGCRAFT_SELECTION: "请选择列表中可用的创作方法。",
  SONGCRAFT_SNAPSHOT_INVALID: "任务保存的创作材料校验失败，已停止调用，请检查任务记录。",
  LYRIC_DENSITY_LIMIT: "歌词仍超过本次精简要求的字数或单句密度上限，未提交音频生成。可重新尝试精简。",
  PRODUCTION_SPECS_REQUIRED: "专业技能回复缺少编曲或作词规格单，未提交生成。",
  ABC_LYRICS_MISMATCH: "谱内歌词与独立歌词不一致，未提交生成。",
  INVALID_PRODUCTION_SPECS: "专业规格单结构不完整，未提交生成。",
  SPEC_SCORE_MISMATCH: "编曲规格中的小节或拍速与乐谱不一致，未提交生成。",
  SPEC_LYRICS_MISMATCH: "作词规格与实际歌词不一致，未提交生成。",
  LYRIC_DENSITY_UNMEASURABLE: "缺少完整配谱或明确速度，无法核对本次歌词密度限制，未提交音频生成。",
  SCORE_LYRIC_MAP_INVALID: "配谱数据格式不完整，请检查歌词行、声部和音符选择。",
  SCORE_LYRIC_MAP_RANGE: "配谱引用了不存在的音符、倒置范围或延音线中间的起音，请重新选择。",
  SCORE_LYRIC_MAP_UNITS: "请为每个字词组填写文字和完整的起止音符。",
  SCORE_LYRIC_MAP_TEXT: "字词组尚未按顺序覆盖原句全部歌词，请核对遗漏、重复或改字。",
  SCORE_LYRIC_MAP_OVERLAP: "同一声部的歌词或字词范围重叠，请重新分配音符。",
  SCORE_LYRIC_MAP_INCOMPLETE: "创作回复缺少部分歌词的字词配谱，未提交渲染。可重新尝试。",
  SCORE_LYRIC_MAP_UNSUPPORTED: "当前记谱尚不能可靠定位音符，请先修正谱面。",
  SCORE_LYRIC_MAP_CONFLICT: "配谱关系已在另一处更新，请刷新并核对后再保存。",
  HEAD_CONFLICT:
    "当前采用版本已被另一处更新。请刷新后重新核对，系统没有覆盖它。",
  STALE_BASE: "来源版本不匹配，请重新选择。",
  UNSUPPORTED_SCORE_FOR_EDIT:
    "这份 ABC 超出当前安全编辑范围，可以试听，但暂不能局部修改。",
  INVALID_EDIT_REGION: "请至少选择一个可修改的小节。",
  NO_EDITABLE_NOTES: "所选范围没有可安全修改的音符。",
  SESSION_REQUIRED: "本地会话已过期，请刷新页面。",
  CSRF_DENIED: "本地会话已过期，请刷新页面。",
  COMMAND_ALREADY_STARTED: "任务已经开始，不能当作尚未执行的任务撤回。",
  SERVICE_UNAVAILABLE: "本地服务暂时不可用，请检查后刷新。",
  HERMES_UNAVAILABLE: "Hermes 接入未配置，任务未发送。",
  STALE_CREATION_PLAN: "创作讨论已更新，请查看最新方案后再操作。",
  CREATION_BUSY: "上一条想法还在处理中，收到回复后可以继续讨论。",
  PLAN_NOT_READY: "请等创作方案完成后再确认。",
  SECTION_LYRICS_UNMAPPED: "该段落与歌词暂不能唯一对应，请选择整曲调整。",
  SCORE_REQUIRES_NOTATION_REPAIR: "新谱面含无法安全解释的记谱，请查看任务记录；没有生成音频。",
  SECTION_SCOPE_VIOLATION: "回复超出了所选段落范围，未替换来源版本。",
  TIMEOUTEXPIRED: "模型超过 8 分钟仍未完成，任务已停止。可重试一次或缩小修改范围。",
  INVALID_CREATIVE_RESPONSE_JSON: "回复格式未通过，原始回复已保存。可先检查原回复并继续；这不会重新创作。",
  INVALID_CREATIVE_RESPONSE_FIELDS: "模型回复缺少必要字段或带有额外字段，可重新尝试。",
  MODEL_INTERRUPTED_REVIEW_REQUIRED: "模型执行中断且没有完整结果，可手动重新尝试。",
  RETRY_REQUIRES_RECONCILIATION: "该任务可能已有产物或正在生成，请先核对进度，不能直接重投。",
  LYRICS_LOCK_VIOLATION: "回复改动了已锁定歌词，系统已拦截，没有保存新候选。",
  MELODY_LOCK_VIOLATION: "回复改动了已锁定旋律音高，系统已拦截。",
  RHYTHM_LOCK_VIOLATION: "回复改动了已锁定节奏，系统已拦截。",
  STYLE_LOCK_VIOLATION: "回复改动了已锁定整体风格，系统已拦截。",
  CHORDS_LOCK_VIOLATION: "回复改动了已锁定和弦，系统已拦截。",
  STRUCTURE_LOCK_VIOLATION: "回复改动了已锁定段落结构，系统已拦截。",
  TEMPO_LOCK_VIOLATION: "回复改动了已锁定速度，系统已拦截。",
  KEY_LOCK_VIOLATION: "回复改动了已锁定调性，系统已拦截。",
  MANUAL_SCORE_INVALID: "修改后的谱面未通过检查，请先预览并调整拍数或记谱。",
  NO_MUSICAL_CHANGE: "ABC、歌词和生成风格没有实际变化。只改规格说明不会改变生成输入；可重新尝试，在保留项之外落实修改。",
  ANALYSIS_PREFLIGHT_FAILED: "分析工作流在预检时被拒绝，未提交 GPU 任务。可在对应音频的分析面板重新准备。",
  ANALYSIS_RECEIPT_UNKNOWN: "分析提交回执不完整，请在对应音频面板继续核对；系统不会自动重复提交。",
  ANALYSIS_HISTORY_PENDING: "暂未找到分析完成记录，可继续核对已有提交。",
  ASR_ANALYSIS_FAILED: "本机歌词识别未完成，可继续识别，已完成的转谱会保留。",
  SHEETSAGE_ANALYSIS_FAILED: "旋律转谱执行失败，已保留原音频和提交证据。",
  AUDIO_ANALYSIS_TIMEOUT: "音频分析超时，可继续核对已有结果。",
  INVALID_TIMELINE: "请填写有效的起止秒数：起点早于终点，保持歌词顺序，且不超出音频时长。",
  TIMELINE_CONFLICT: "另一处已更新歌词时间线，请读取最新版本后再保存。",
  CHECK_REPORT_CHANGED: "分析或时间线已更新，请查看最新检查结果后再保存结论。",
  LOCK_REQUIRES_SUPPORTED_SCORE: "当前记谱暂不能可靠验证这些音乐约束，请先修正谱面或仅保留歌词。",
};
function errorText(e) {
  return messages[e.message] || e.message || "操作未完成，请刷新核对。";
}
function notify(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => ($("toast").hidden = true), 4000);
}
function warn(message, retry) {
  if (!retry && sessionStorage.getItem("jr-pending")) retry = retryPending;
  $("notice").replaceChildren(document.createTextNode(message));
  $("notice").hidden = false;
  if (retry) {
    const b = document.createElement("button");
    b.textContent = "重试同一请求";
    b.onclick = retry;
    $("notice").append(b);
  }
}
async function api(path, body) {
  const options = { headers: {}, credentials: "same-origin" };
  if (body) {
    options.method = "POST";
    options.headers = {
      "Content-Type": "application/json",
      "X-JR-CSRF": state.csrf,
    };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("网络中断，结果尚未确认。");
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new Error("网络响应不完整，结果尚未确认。");
  }
  if (!result.ok) throw new Error(result.error.code);
  return result.data;
}
function route(suffix = "") {
  return `/api/${state.lib}/projects/${state.pid}${suffix}`;
}
async function mutate(path, body) {
  if (sessionStorage.getItem("jr-pending"))
    throw new Error("先核对上一次未确认的请求，再进行新的操作。");
  if (state.busy) throw new Error("请等待当前操作完成。");
  state.busy = true;
  const packet = {
    path,
    body: { ...body, idempotency_key: crypto.randomUUID() },
  };
  sessionStorage.setItem("jr-pending", JSON.stringify(packet));
  try {
    const result = await api(path, packet.body);
    sessionStorage.removeItem("jr-pending");
    $("notice").hidden = true;
    return result;
  } catch (e) {
    if (e.message.startsWith("网络") || e.message === "SERVICE_UNAVAILABLE") {
      warn(
        "操作结果尚未确认。再次提交时会复用原请求，不会生成第二个任务。",
        retryPending,
      );
    } else sessionStorage.removeItem("jr-pending");
    throw e;
  } finally {
    state.busy = false;
  }
}
async function retryPending() {
  const raw = sessionStorage.getItem("jr-pending");
  if (!raw) return;
  const packet = JSON.parse(raw);
  try {
    await api(packet.path, packet.body);
    sessionStorage.removeItem("jr-pending");
    $("notice").hidden = true;
    await loadProject(false);
    notify("已核对原请求。");
  } catch (e) {
    if (!e.message.startsWith("网络") && e.message !== "SERVICE_UNAVAILABLE") {
      sessionStorage.removeItem("jr-pending");
      warn(errorText(e));
    } else warn(errorText(e), retryPending);
  }
}
function candidate(rid) {
  return state.data?.revisions.find((r) => r.revision.revision_id === rid);
}
function label(rid) {
  const r = candidate(rid);
  if (!r) return "未知版本";
  const rootOf = (value) => {
    let root = value;
    while (
      root.revision.parent_revision_id &&
      candidate(root.revision.parent_revision_id)
    )
      root = candidate(root.revision.parent_revision_id);
    return root;
  };
  const root = rootOf(r),
    roots = state.data.revisions.filter((x) => !x.revision.parent_revision_id),
    i = roots.indexOf(root);
  const family = state.data.revisions.filter((x) => rootOf(x) === root),
    version = family.indexOf(r);
  const trialArm = state.data.commands.find(c=>c.result_revision_id===root.revision.revision_id && /^[ABC]$/.test(c.experiment_arm || ''))?.experiment_arm;
  return `${trialArm || (i < 26 ? String.fromCharCode(65 + i) : "候选 " + (i + 1))}${version ? " · 修订 " + version : ""}`;
}
function doneRenders(r) {
  return r.renders.filter(
    (j) =>
      j.state === "succeeded" &&
      j.assets.some(
        (a) =>
          a.media_type === "audio/wav" &&
          a.verification === "decoded_pcm_verified",
      ),
  );
}
function currentRender(r) {
  const done = doneRenders(r);
  return (
    done.find((j) => j.render_id === state.renders[r.revision.revision_id]) ||
    done.at(-1)
  );
}
function date(value) {
  return new Date(value).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
function short(id) {
  return id?.slice(-8) || "—";
}
function sidebar() {
  const query = $("search").value.toLowerCase();
  $("projects").innerHTML = state.libraries
    .map(
      (l) =>
        `<div class="library-group">${esc(l.label)}</div>` +
        l.projects
          .filter((p) => p.title.toLowerCase().includes(query))
          .map(
            (p) =>
              `<button class="project-button ${state.pid === p.project_id ? "active" : ""}" data-library="${l.id}" data-project="${p.project_id}">${esc(p.title)}<small>${p.head_revision_id ? "已有采用版本" : "待制作人选择"} · ${date(p.created_at)}</small></button>`,
          )
          .join(""),
    )
    .join("");
  $("projects")
    .querySelectorAll("button")
    .forEach(
      (b) =>
        (b.onclick = () => openProject(b.dataset.library, b.dataset.project)),
    );
}
async function libraries() {
  state.libraries = await api("/api/libraries");
  sidebar();
}
async function openProject(lib, pid) {
  saveCreationDraft();
  ScoreReview.stop();
  saveDraft();
  $("instruction").value = "";
  $("feedback").value = "";
  document.querySelectorAll("audio").forEach((a) => a.pause());
  state.lib = lib;
  state.pid = pid;
  state.focus = null;
  state.selected = [];
  state.bars.clear();
  state.data = null;
  state.creationDigest = null;
  restoreCreationDraft();
  localStorage.setItem("jr-project", JSON.stringify({ lib, pid }));
  await loadProject(true);
  restoreDraft();
  sidebar();
}
async function loadProject(full = false) {
  if (!state.pid) return;
  const generation = ++state.load;
  const path = route();
  try {
    const data = await api(path);
    if (generation !== state.load || path !== route()) return;
    const changed =
      !state.data ||
      JSON.stringify(
        data.revisions.map((r) => [
          r.revision.revision_id,
          r.score_lyric_map?.generation,
          r.renders.map((j) => [j.render_id, j.state]),
        ]),
      ) !==
        JSON.stringify(
          state.data.revisions.map((r) => [
            r.revision.revision_id,
            r.score_lyric_map?.generation,
            r.renders.map((j) => [j.render_id, j.state]),
          ]),
        );
    state.data = data;
    $("connection").textContent = "已连接 · 本地";
    $("projectTitle").textContent = data.project.title;
    $("libraryLabel").textContent =
      state.libraries.find((l) => l.id === state.lib)?.label || "创作工程";
    if (!state.selected.length) {
      let ready = data.revisions.filter((r) => doneRenders(r).length);
      if (!ready.length) ready = data.revisions;
      state.selected = ready
        .slice(-3)
        .map((r) => r.revision.revision_id)
        .sort((a, b) => label(a).localeCompare(label(b)));
    }
    if (!state.focus)
      state.focus =
        state.selected[0] || data.revisions[0]?.revision.revision_id;
    head();
    if (full || changed) {
      cards();
      versionList();
      workbench();
    }
    activity();
    creation();
  } catch (e) {
    $("connection").textContent = "连接中断";
    warn(errorText(e));
  }
}
function head() {
  const p = state.data.project;
  $("headLabel").textContent = p.head_revision_id
    ? `${label(p.head_revision_id)} · ${short(p.head_revision_id)}`
    : "尚未选择版本";
  $("headHelp").textContent = p.head_revision_id
    ? `第 ${p.head_generation} 次选择 · 其他候选完整保留`
    : "试听偏好会单独保存，由你决定最终版本。";
  document.querySelectorAll("#compare .candidate").forEach((card) => {
    const rid = card.dataset.rid,
      r = candidate(rid);
    if (!r) return;
    const badge = card.querySelector(".card-top > .badge");
    const selected = p.head_revision_id === rid;
    const rejected =
      state.data.decisions.filter((d) => d.target_revision_id === rid).at(-1)
        ?.action === "reject";
    badge.classList.toggle("selected", selected);
    badge.textContent = selected
      ? "已采用"
      : rejected
        ? "已 Reject"
        : currentRender(r)
          ? "可试听"
          : "未渲染";
  });
}
function cards() {
  const previous = {};
  document.querySelectorAll("#compare audio").forEach((a) => {
    previous[a.dataset.asset] = { time: a.currentTime, playing: !a.paused };
    a.pause();
  });
  if (!state.data.revisions.length) {
    $("compare").innerHTML =
      '<div class="empty" style="grid-column:1/-1">首版歌曲会出现在这里。先在上方讨论创作方案，再确认生成试听；已有作品也可以直接导入候选。</div>';
    return;
  }
  $("compare").innerHTML =
    state.selected
      .map((rid) => {
        const r = candidate(rid),
          rev = r.revision,
          j = currentRender(r),
          audio = j?.assets.find((a) => a.media_type === "audio/wav"),
          lyrics = r.snapshot.brief.lyrics
            .replace(/^\[[^\]]+\]\s*/gm, "")
            .split("\n")
            .filter(Boolean)
            .slice(0, 2)
            .join("\n");
        const rejected =
          state.data.decisions
            .filter((d) => d.target_revision_id === rid)
            .at(-1)?.action === "reject";
        return `<article class="candidate ${rid === state.focus ? "focus" : ""}" data-rid="${rid}"><div class="card-top"><div class="candidate-label"><span class="letter">${esc(label(rid).split(" ")[0])}</span><div><div class="candidate-title">${esc(label(rid))}</div><div class="badge">${esc(names[rev.author_id] || rev.author_id)} · ${short(rid)}</div></div></div><span class="badge ${state.data.project.head_revision_id === rid ? "selected" : ""}">${state.data.project.head_revision_id === rid ? "已采用" : rejected ? "已 Reject" : j ? "可试听" : "未渲染"}</span></div><div class="card-body"><p class="preview">${esc(lyrics)}</p>${audio ? `<audio controls preload="metadata" data-asset="${audio.asset_id}" aria-label="候选 ${esc(label(rid))} 音频" src="${route("/audio/" + audio.asset_id)}"></audio><div class="audio-info"><span>${j.verification?.duration_seconds ?? "?"} s · ${j.verification?.sample_rate / 1000 || 48} kHz · YuE2 · ${esc(j.production_binding?.mcp_alias || j.server?.hardware || "历史渲染")}</span><a href="${route("/audio/" + audio.asset_id)}" download="${esc(label(rid).replaceAll(" ", "-"))}.wav">WAV ↓</a></div>` : '<p class="hint">尚无已验证音频。可交给 Hermes 生成试听。</p>'}${
          doneRenders(r).length > 1
            ? `<select class="render-choice" aria-label="${esc(label(rid))} 的渲染版本">${doneRenders(
                r,
              )
                .map(
                  (x) =>
                    `<option value="${x.render_id}" ${x.render_id === j.render_id ? "selected" : ""}>${date(x.created_at)} · ${short(x.render_id)}</option>`,
                )
                .join("")}</select>`
            : ""
        }<div class="card-actions"><button class="view">${rid === state.focus ? "正在查看" : "查看 / 指挥"}</button><button class="accept" ${!j ? "disabled" : ""}>Accept</button><button class="reject">Reject</button></div></div></article>`;
      })
      .join("") +
    Array.from(
      { length: Math.max(0, 3 - state.selected.length) },
      () =>
        '<div class="candidate-empty">从「所有版本」选择候选<br>加入对比试听</div>',
    ).join("");
  $("compare")
    .querySelectorAll(".candidate")
    .forEach((card) => {
      const rid = card.dataset.rid;
      card.querySelector(".view").onclick = () => focus(rid);
      card.querySelector(".accept").onclick = () => decision("accept", rid);
      card.querySelector(".reject").onclick = () => decision("reject", rid);
      const select = card.querySelector("select");
      if (select)
        select.onchange = () => {
          state.renders[rid] = select.value;
          cards();
          if (rid === state.focus) workbench();
        };
      const audio = card.querySelector("audio");
      if (audio) {
        audio.addEventListener("play", () =>
          document.querySelectorAll("audio").forEach((other) => {
            if (other !== audio) other.pause();
          }),
        );
        audio.addEventListener("error", () =>
          warn("音频读取失败，请刷新核对本地服务和归档文件。"),
        );
        const saved = previous[audio.dataset.asset];
        if (saved)
          audio.addEventListener(
            "loadedmetadata",
            () => {
              audio.currentTime = Math.min(saved.time, audio.duration || 0);
              if (saved.playing) audio.play().catch(() => {});
            },
            { once: true },
          );
      }
    });
}
function focus(rid) {
  document.querySelectorAll("audio").forEach((a) => a.pause());
  saveDraft();
  state.focus = rid;
  restoreDraft();
  state.bars.clear();
  document.querySelectorAll(".candidate").forEach((c) => {
    c.classList.toggle("focus", c.dataset.rid === rid);
    c.querySelector(".view").textContent =
      c.dataset.rid === rid ? "正在查看" : "查看 / 指挥";
  });
  workbench();
}
function versionList() {
  $("versions").hidden = !state.data.revisions.length;
  $("versionCount").textContent = `(${state.data.revisions.length})`;
  $("versionList").innerHTML = state.data.revisions
    .map((r) => {
      const id = r.revision.revision_id;
      return `<div class="version-row"><input type="checkbox" id="select-${id}" data-id="${id}" ${state.selected.includes(id) ? "checked" : ""}><label for="select-${id}">${esc(label(id))} · ${short(id)}<small>${esc(r.revision.summary)}</small></label><button data-id="${id}">查看</button></div>`;
    })
    .join("");
  $("versionList")
    .querySelectorAll("input")
    .forEach(
      (c) =>
        (c.onchange = () => {
          if (c.checked && state.selected.length >= 3) {
            c.checked = false;
            notify("最多对比三个候选，请先取消一个。");
            return;
          }
          state.selected = c.checked
            ? [...state.selected, c.dataset.id]
            : state.selected.filter((id) => id !== c.dataset.id);
          cards();
          workbench();
        }),
    );
  $("versionList")
    .querySelectorAll("button")
    .forEach((b) => (b.onclick = () => focus(b.dataset.id)));
}
function workbench() {
  ScoreReview.stop();
  const r = candidate(state.focus);
  $("workbench").hidden = !r;
  if (!r) return;
  $("focusLabel").textContent = label(state.focus);
  $("editBase").textContent =
    `基于 ${label(state.focus)} · ${short(state.focus)} 创建新候选`;
  $("durationHint").textContent =
    `当前候选上限 ${r.snapshot.brief.max_duration} 秒，修改沿用此时长。重新生成若改变时长，会另存新版本。300 秒是上限，歌曲可能提前结束；任务在后台运行，可离开页面。`;
  const oldScope = $("editScope").value;
  $("editScope").innerHTML = '<option value="song">整曲 · 风格与整体方向</option>' +
    (r.direction_scopes || []).map(s => `<option value="section:${esc(s.section_id)}" ${!s.lyrics_available ? 'disabled' : ''}>${esc(s.title)} · 段落调整${s.lyrics_available ? '' : '（歌词未定位）'}</option>`).join('') +
    '<option value="pitch">精细 · 所选小节音高</option>';
  if ([...$("editScope").options].some(o => o.value === oldScope && !o.disabled)) $("editScope").value = oldScope;
  $("abc").textContent = r.snapshot.abc;
  const exportInfo=r.score_export;
  $("scoreExport").innerHTML=exportInfo?.available
    ? `<div class="review-controls"><strong>导出当前候选</strong><a class="button" href="${route(`/revisions/${state.focus}/score.mid`)}" download>MIDI ↓</a><a class="button" href="${route(`/revisions/${state.focus}/score.musicxml`)}" download>MusicXML ↓</a></div><p class="hint">${exportInfo.voice_count} 个声部 · 谱面时长 ${Math.floor(exportInfo.duration_seconds/60)}:${String(Math.floor(exportInfo.duration_seconds%60)).padStart(2,'0')}。MIDI 用于外部钢琴卷帘；MusicXML 用于乐谱与歌词。导出已保存版本，未保存的编辑请先另存候选。${exportInfo.warnings.map(esc).join(' ')}</p>`
    : `<p class="hint">暂不能导出：${esc(messages[exportInfo?.reason]||exportInfo?.reason||'请刷新加载导出信息。')}</p>`;
  $("sheet").replaceChildren();
  try {
    ABCJS.renderAbc("sheet", r.snapshot.abc, {
      responsive: "resize",
      staffwidth: 720,
      add_classes: true,
      selectionColor: "#b66d33",
      clickListener: (note) => {
        if (typeof note.startChar !== "number") return;
        const offset = new TextEncoder().encode(
          r.snapshot.abc.slice(0, note.startChar),
        ).length;
        const end = new TextEncoder().encode(
          r.snapshot.abc.slice(0, note.endChar ?? note.startChar + 1),
        ).length;
        const event = r.score.events.find(
          (e) => e.kind === "note" && e.byte_start < end && e.byte_end > offset,
        );
        if (event) toggleBar(event.bar_id);
      },
    });
  } catch {
    $("sheet").textContent = "这份 ABC 暂无法显示为五线谱，请查看原文。";
  }
  const meterIssues = r.score_repair?.changes || [];
  $("scoreStatus").textContent =
    r.score.status === "supported"
      ? `已完整解析 ${r.score.bars.length} 小节，支持谱面试听与受保护的音高调整。`
      : meterIssues.length
        ? `已识别全部 ${r.score.bars.length} 小节的音符；${meterIssues.map(c => `${c.voice_id} 第 ${c.ordinal} 小节为 ${c.before_beats} 拍，应为 ${c.after_beats} 拍`).join('；')}。可按原文试听，修正后可做精细音高编辑。`
        : `谱面仍有待支持的记谱：${[...new Set(r.score.parse_diagnostics.map(d => d.code))].join('、')}。可查看原文或使用整曲调整。`;
  $("repairScore").hidden = !r.score_repair?.available;
  const reviewRender = currentRender(r);
  const reviewWav = reviewRender?.assets.find(
    (a) => a.media_type === "audio/wav",
  );
  const sourceRoute=route();
  AudioAnalysis.mount({root:$("audioAnalysis"),render:reviewRender,esc,error:errorText,
    audioUrl:reviewWav?sourceRoute+'/audio/'+reviewWav.asset_id:null,
    agents:state.data.agents,
    get:suffix=>api(sourceRoute+'/renders/'+reviewRender.render_id+suffix),
    post:(suffix,value)=>mutate(sourceRoute+'/renders/'+reviewRender.render_id+suffix,value),
    direct: instruction=>{if(route()!==sourceRoute||state.focus!==r.revision.revision_id)return;
      $("editScope").value='song';$("editScope").dispatchEvent(new Event('change'));
      $("instruction").value=instruction;$("instruction").dispatchEvent(new Event('input'));$("instruction").focus();},
    refresh:()=>{if(route()===sourceRoute)return loadProject(true);}});
  ScoreReview.mount({
    root: $("scoreReview"),
    revision: r,
    render: reviewRender,
    audioUrl: reviewWav ? route("/audio/" + reviewWav.asset_id) : null,
    esc,
    selected: () => state.bars,
    loadReport: reviewRender
      ? () => api(route("/renders/" + reviewRender.render_id + "/score-review"))
      : null,
    save: async (payload) => {
      await mutate(route("/feedback"), payload);
      await loadProject();
      notify("对照记录已保存。");
    },
  });
  $("lyricsGrid").innerHTML = state.selected
    .map(
      (id) =>
        `<div class="lyrics-column"><h3>${esc(label(id))}${id === state.focus ? " · 当前" : ""}</h3><p>${esc(candidate(id).snapshot.brief.lyrics)}</p></div>`,
    )
    .join("");
  const meta = {
    "版本 ID": r.revision.revision_id,
    来源版本: r.revision.parent_revision_id
      ? label(r.revision.parent_revision_id) +
        " · " +
        short(r.revision.parent_revision_id)
      : "原创候选",
    创建时间: date(r.revision.created_at),
    说明: r.revision.summary,
    受保护改动:
      r.protected_edits
        ?.flat()
        .map((e) => `${e.event_id}：${e.expected_pitch} → ${e.new_pitch}`)
        .join("；") || "此版本没有受保护编辑清单",
    风格: r.snapshot.brief.style,
    时长上限: r.snapshot.brief.max_duration + " 秒",
    随机种子: r.snapshot.brief.seed,
    "快照 SHA": r.revision.snapshot_sha256,
  };
  $("metadata").innerHTML = Object.entries(meta)
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`)
    .join("");
  bars();
  ScoreLyrics.mount({root:$("scoreLyricEditor"),revision:r,esc,error:errorText,selected:()=>state.bars,
    save:async rows=>{
      await mutate(sourceRoute+'/score-lyric-map',{revision_id:r.revision.revision_id,expected_snapshot_sha256:r.revision.snapshot_sha256,
        expected_generation:r.score_lyric_map.generation,rows});
      if(route()===sourceRoute){await loadProject(true);notify('配谱关系已保存，原谱与音频没有改变。');}
    }});
  ManualEditor.mount({root:$("manualEditor"),revision:r,esc,
    preview: operation=>mutate(sourceRoute+'/manual-preview',{revision_id:r.revision.revision_id,expected_snapshot_sha256:r.revision.snapshot_sha256,operation}),
    save: async operation=>{
      const rev=await mutate(sourceRoute+'/manual-edit',{revision_id:r.revision.revision_id,expected_snapshot_sha256:r.revision.snapshot_sha256,operation});
      if(route()!==sourceRoute)return;
      state.focus=rev.revision_id;state.bars.clear();state.selected=[...state.selected.slice(-2),rev.revision_id];
      await loadProject(true);notify('编辑已另存新候选，可按所选时长生成试听。');
    }});
}
function toggleBar(id) {
  if (candidate(state.focus)?.score_audition.status !== "available") return;
  if (state.bars.has(id)) state.bars.delete(id);
  else state.bars.add(id);
  bars();
}
function bars() {
  const r = candidate(state.focus);
  if (!r) return;
  lyricNavigation(r);
  $("bars").innerHTML = r.score.bars
    .map(
      (b) =>
        `<button class="bar-button" data-bar="${esc(b.bar_id)}" aria-pressed="${state.bars.has(b.bar_id)}" ${r.score_audition.status !== "available" ? "disabled" : ""}>${esc(b.voice_id)} · ${b.ordinal}</button>`,
    )
    .join("");
  $("bars")
    .querySelectorAll("button")
    .forEach((b) => (b.onclick = () => toggleBar(b.dataset.bar)));
  $("scopeLabel").textContent = state.bars.size
    ? `${state.bars.size} 个小节 · ` +
      r.score.bars
        .filter((b) => state.bars.has(b.bar_id))
        .map((b) => `${b.voice_id} ${b.ordinal}`)
        .join("、")
    : "尚未选择小节";
  const scope = $("editScope").value;
  const pitch = scope === 'pitch';
  $("preserveFields").hidden = pitch;
  if (!pitch) {
    const section = r.direction_scopes?.find(s => 'section:' + s.section_id === scope);
    $("scopeLabel").textContent = section ? `${section.title} · 第 ${section.bars[0]}–${section.bars.at(-1)} 小节` : '整首歌曲';
    $("scopeLyrics").hidden = true;
  }
  $("scopeHelp").textContent = pitch
    ? '只修改所选小节的音高，歌词、节奏与结构保留。拍数异常时请先修正谱面。'
    : scope === 'song'
      ? '可调整整体风格、编配、旋律或歌词；写明要保留的内容。另存候选并重新生成整首试听。'
      : '可调整本段旋律、节奏、歌词与演唱提示；其他段落的谱面和歌词逐字保留。音频会整首重新生成，听感可能变化。';
  $("instruction").placeholder = pitch ? '例如：让选中的旋律收束得更平静，减少向上的跳进。' : scope === 'song' ? '例如：整体改为更克制的暗色民谣，保留歌词和主旋律，减少前奏。' : '例如：这一段情绪更紧张，鼓点更强，保留歌词。';
  $("sendEdit").disabled = pitch && (!state.bars.size || r.score.status !== "supported");
}
function lyricNavigation(r) {
  const mapping=r.score_lyric_map;
  const declared=!!mapping?.rows.length;
  const groups = declared?[{title:'歌词配谱',mode:'declared',bars:[],items:mapping.items.map(item=>({...item}))}]:r.lyric_navigation?.groups || [];
  const lookup = new Map(r.score.bars.map((b) => [b.bar_id, b]));
  const selectedLyrics = [];
  const range = (bars) => {
    if (!bars.length) return "未确定对应小节";
    const consecutive = bars.every((b, i) => b.ordinal === bars[0].ordinal + i);
    const numbers =
      consecutive && bars.length > 1
        ? `${bars[0].ordinal}–${bars[bars.length - 1].ordinal}`
        : bars.map((b) => b.ordinal).join("、");
    return `${bars[0].voice_id} · 第 ${numbers} 小节`;
  };
  const control = (ids, caption, text) => {
    const selected = ids.filter((id) => state.bars.has(id)).length;
    return `<button class="lyric-bar" data-lyric-bars="${esc(JSON.stringify(ids))}" aria-pressed="${selected === ids.length ? "true" : selected ? "mixed" : "false"}" ${r.score_audition.status !== "available" ? "disabled" : ""}><small>${esc(caption)}</small><span>${esc(text)}</span></button>`;
  };
  $("lyricNavigation").innerHTML = groups.length
    ? `<h3>按歌词找小节</h3><p class="hint">${declared?'按已保存的创作配谱定位。时间由输入谱计算，不是实唱时间。未配谱 '+mapping.unmapped_count+' 行。':'当前按段落与行序推测，尚非明确配谱。可展开下方“编辑歌词配谱关系”保存对应。'} 点击歌词可选中 / 取消小节。</p>` +
      groups
        .map((g) => {
          const lineMode = g.mode === "line_order_reference" || g.mode==='declared';
          let body;
          if (lineMode) {
            body =
              `<div class="lyric-bar-grid">` +
              g.items
                .map((item) => {
                  if(!item.bar_ids.length)return `<p class="hint">未配谱：${esc(item.text)}</p>`;
                  let caption = range(
                    item.bar_ids.map((id) => lookup.get(id)),
                  );
                  if(g.mode==='declared')caption+=' · '+(item.level==='word_groups'?'字词组配谱':'句级配谱')+(item.start==null?' · 理论时间未知':` · 理论 ${item.start.toFixed(2)}–${item.end.toFixed(2)} 秒`);
                  if (item.bar_ids.some((id) => state.bars.has(id)))
                    selectedLyrics.push(`${caption}：${item.text}`);
                  return control(item.bar_ids, caption, item.text)+(item.unit_positions?.length?`<div class="score-lyric-units">${item.unit_positions.map(u=>`<span title="${esc('第 '+u.start_bar+' 小节第 '+u.start_note+' 音符 → 第 '+u.end_bar+' 小节第 '+u.end_note+' 音符')}">${esc(u.text)}<small>${u.start==null?'时间未知':u.start.toFixed(2)+'–'+u.end.toFixed(2)+' 秒'}</small></span>`).join('')}</div>`:'');
                })
                .join("") +
              `</div>`;
          } else {
            const text = g.items.map((item) => item.text).join("\n");
            body = `<p class="lyric-section-text">${esc(text)}</p>`;
            if (g.bars.length) {
              body += control(
                g.bars.map((b) => b.bar_id),
                range(g.bars),
                "选择整段小节（无法逐句定位）",
              );
              if (g.bars.some((b) => state.bars.has(b.bar_id)))
                selectedLyrics.push(
                  `${g.title}（仅段落参考，所选小节无法逐句定位）：\n${text}`,
                );
            }
          }
          return `<div class="lyric-section"><h4>${esc(g.title)} <small>${g.mode==='declared'?'已保存 · 第 '+mapping.generation+' 版':lineMode ? "行序参考" : g.bars.length ? "仅段落范围" : "暂无法定位，请对照乐谱"}</small></h4>${body}</div>`;
        })
        .join("")
    : `<p class="hint">当前候选没有歌词，可直接选择下方小节。</p>`;
  const density=r.lyric_density;
  if(r.production_specs){
    $("lyricNavigation").insertAdjacentHTML('afterbegin',`<details class="production-specs"><summary>编曲与作词规格单 · ${r.production_specs.record.scope==='scoped_segment'?'本次修改段落':'整曲'}</summary><p class="hint">创作规格与自查记录；不是实际音频测量或独立评审。</p><pre>${esc(JSON.stringify(r.production_specs.specs,null,2))}</pre></details>`);
  }
  if(density){
    const risk=density.dense_runs?.length?`连续密集段：${density.dense_runs.map(s=>`${s.section} 第 ${s.first_line}–${s.last_line} 行，约 ${(s.end-s.start).toFixed(0)} 秒`).map(esc).join('；')}。抒情演唱建议精简歌词，让一句跨多个小节，留出延音与换气。`:density.status==='unavailable'?'配谱、速度或语言信息不足，暂不能可靠估算。':'未触发连续密集提示，仍需试听确认演唱是否舒展。';
    $("lyricNavigation").insertAdjacentHTML('afterbegin',`<div class="hint lyric-density"><strong>歌词密度检查</strong><p>${density.line_count} 行 · ${density.han_characters} 个汉字${density.max_line_han_per_second==null?'':` · 最密一句约 ${density.max_line_han_per_second.toFixed(2)} 字／谱面秒`}。</p><p>${risk}</p><small>仅按输入谱与配谱估算，不是实唱速度或说唱判定。</small></div>`);
  }
  $("lyricNavigation")
    .querySelectorAll("[data-lyric-bars]")
    .forEach((button) => {
      button.onclick = () => {
        const ids = JSON.parse(button.dataset.lyricBars);
        const remove = ids.every((id) => state.bars.has(id));
        ids.forEach((id) =>
          remove ? state.bars.delete(id) : state.bars.add(id),
        );
        bars();
        // Keep keyboard focus after refreshing selection styles.
        Array.from($("lyricNavigation").querySelectorAll("[data-lyric-bars]"))
          .find((next) => next.dataset.lyricBars === button.dataset.lyricBars)
          ?.focus({ preventScroll: true });
      };
    });
  $("scopeLyrics").hidden = !selectedLyrics.length;
  $("scopeLyrics").textContent = `歌词参考\n${selectedLyrics.join("\n")}`;
}
function creationDraftKey() {
  return `jr-creation:${state.lib}:${state.pid}`;
}
function saveCreationDraft() {
  if (!state.pid) return;
  localStorage.setItem(
    creationDraftKey(),
    JSON.stringify({
      message: $("creationMessage").value,
      agent: $("creationAgent").value,
      mcp: $("creationMcp").value,
      duration: $("creationDuration").value,
      songcraft: $("creationSongcraft").value,
      songcraftPolicy: "professional-20260918",
    }),
  );
}
function restoreCreationDraft() {
  let draft;
  try {
    draft = JSON.parse(localStorage.getItem(creationDraftKey()));
  } catch {}
  $("creationMessage").value = draft?.message || "";
  $("creationAgent").value = draft?.agent || "yinyue";
  $("creationDuration").value = draft?.duration || "300";
  $("creationSongcraft").value = draft?.songcraftPolicy === "professional-20260918" ? (draft.songcraft || "professional") : "professional";
  setMcpChoice("creationMcp",draft?.mcp || "");
  state.creationProject = null;
}
function creation() {
  if (!state.data) return;
  $("creationPanel").hidden = false;
  const plans = state.data.commands.filter((c) => c.kind === "plan");
  const latest = plans.at(-1);
  const composition =
    latest &&
    state.data.commands.find(
      (c) => c.kind === "compose" && c.plan_command_id === latest.command_id,
    );
  $("creationPlanHeading").textContent = composition
    ? "已确认的创作方案"
    : "待你确认的创作方案";
  const busy =
    latest && ["queued", "working", "preparing"].includes(latest.state);
  if (state.creationProject !== state.pid) {
    $("creationPanel").open = !state.data.revisions.length;
    state.creationProject = state.pid;
    // Resume saved discussions in another browser without silently resetting controls.
    if (!localStorage.getItem(creationDraftKey()) && latest) {
      $("creationAgent").value = latest.assigned_to;
      setMcpChoice("creationMcp",latest.production_binding?.mcp_alias || "");
      $("creationDuration").value = String(latest.target_duration);
      $("creationSongcraft").value = "professional";
    }
  }
  $("creationStatus").textContent = busy
    ? "音乐伙伴正在整理方案…"
    : composition
      ? composition.state === "completed"
        ? "首版已完成，前往候选试听"
        : composition.state === "needs_attention"
          ? "首版任务需要处理，详见任务记录"
          : composition.state === "cancelled"
            ? "首版任务已撤回"
            : composition.state === 'rendering'
              ? "词曲已通过校验，音频生成／归档中…"
              : ['format_check','format_repair'].includes(composition.progress?.stage)
                ? "正在检查与修复原回复格式，词曲不变…"
                : composition.progress?.stage === 'validating'
                  ? "正在校验词曲、配谱与专业规格…"
                  : "方案已确认，正在创作词曲与规格…"
      : latest?.result
        ? "方案已就绪，可继续讨论或确认生成"
        : "描述想法，与音乐伙伴一起确定方向";
  const digest = JSON.stringify(plans);
  if (digest !== state.creationDigest) {
    state.creationDigest = digest;
    $("creationConversation").innerHTML = plans.length
      ? plans
          .map(
            (c) =>
              `<div class="creation-turn"><strong>你 · ${date(c.created_at)}</strong><p>${esc(c.instruction)}</p></div>` +
              (c.result
                ? `<div class="creation-turn agent"><strong>${esc(names[c.assigned_to])}</strong><p>${esc(c.result.reply)}</p></div>`
                : `<p class="hint">${c.state === "needs_attention" ? "本次讨论未完成：" + esc(c.issue) + "。可在下方补充或重新发送想法。" : c.state === "cancelled" ? "本条已撤回。" : "等待音乐伙伴回复…"}</p>`),
          )
          .join("")
      : '<p class="muted">可以从主题、一个画面、一段故事或几句歌词开始。不确定的部分，让音乐伙伴帮你一起构思。</p>';
    if (latest?.result) {
      const labels = {
        title: "工作歌名",
        concept: "主题与表达",
        style: "音乐与演唱方向",
        structure: "段落结构",
        lyric_direction: "歌词方向",
      };
      $("creationPlan").innerHTML = `<dl>${Object.entries(labels)
        .map(([k, v]) => `<dt>${v}</dt><dd>${esc(latest.result.plan[k])}</dd>`)
        .join(
          "",
        )}<dt>生成服务器</dt><dd>${esc(latest.production_binding?.mcp_alias || "历史默认 · jr_music_3060")}（方案已固定）</dd><dt>生成上限</dt><dd>${latest.target_duration} 秒 · ${esc(names[latest.assigned_to])}</dd><dt>创作方法</dt><dd>${esc(latest.songcraft?.label || '不启用 · 当前基线')}${latest.songcraft ? ' · v'+esc(latest.songcraft.version) : ''}</dd></dl>`;
    } else {
      $("creationPlan").innerHTML =
        `<p class="muted">${busy ? "正在根据你的想法整理方案…" : "发送想法后，方案会出现在这里。"}</p>`;
    }
    $("creationConversation").scrollTop = $(
      "creationConversation",
    ).scrollHeight;
  }
  $("sendIdea").disabled =
    state.busy || busy || !$("creationMessage").value.trim();
  $("creationAgent").disabled = !!busy;
  $("creationDuration").disabled = !!busy;
  $("creationSongcraft").disabled = !!busy;
  $("creationMcp").disabled = !!busy;
  const unsent =
    $("creationMessage").value.trim() ||
    (latest &&
      ($("creationAgent").value !== latest.assigned_to ||
        $("creationSongcraft").value !== (latest.songcraft?.selection || 'none') ||
        ($("creationMcp").value && $("creationMcp").value !== latest.production_binding?.mcp_alias) ||
        Number($("creationDuration").value) !== latest.target_duration));
  $("confirmCreation").disabled =
    state.busy ||
    busy ||
    latest?.state !== "completed" ||
    !!composition ||
    !!unsent;
  $("confirmCreation").textContent = composition
    ? composition.state === 'needs_attention' ? "首版需要处理，请看下方任务记录" : composition.state === 'completed' ? "首版已完成，前往候选试听" : "首版任务进行中，请看下方进度"
    : unsent && latest?.result
      ? "先发送新意见，再确认方案"
      : "确认方案并生成首版 ↗";
}
for (const id of ["creationMessage", "creationAgent", "creationDuration", "creationSongcraft", "creationMcp"]) {
  $(id).addEventListener("input", () => {
    saveCreationDraft();
    creation();
  });
}
$("sendIdea").onclick = async () => {
  const instruction = $("creationMessage").value.trim();
  if (!instruction || state.busy) return;
  const latest = state.data.commands.filter((c) => c.kind === "plan").at(-1);
  const sourceRoute = route();
  $("sendIdea").disabled = true;
  try {
    await mutate(route("/creation-discuss"), {
      assigned_to: $("creationAgent").value,
      ...($("creationMcp").value ? {mcp_alias:$("creationMcp").value} : {}),
      songcraft_selection: $("creationSongcraft").value,
      instruction,
      target_duration: Number($("creationDuration").value),
      parent_command_id: latest?.command_id || null,
    });
    // Preserve any text typed while this request was in flight.
    if (route() !== sourceRoute) return;
    if ($("creationMessage").value.trim() === instruction)
      $("creationMessage").value = "";
    saveCreationDraft();
    await loadProject();
    notify("想法已保存，音乐伙伴会在这里回复。此时还不会生成音频。");
  } catch (e) {
    warn(errorText(e));
  } finally {
    creation();
  }
};
$("confirmCreation").onclick = async () => {
  const latest = state.data.commands.filter((c) => c.kind === "plan").at(-1);
  if (!latest?.plan_sha256 || $("confirmCreation").disabled) return;
  const sourceRoute = route();
  $("confirmCreation").disabled = true;
  try {
    await mutate(route("/creation-confirm"), {
      plan_command_id: latest.command_id,
      expected_plan_sha256: latest.plan_sha256,
    });
    if (route() !== sourceRoute) return;
    await loadProject();
    notify("方案已确认，开始创作并生成首版。完成后会出现在候选试听区。");
  } catch (e) {
    warn(errorText(e));
  } finally {
    creation();
  }
};
function activity() {
  const detail = c => {
    const end = ['completed','needs_attention','cancelled'].includes(c.state) ? Date.parse(c.updated_at) : Date.now();
    const elapsed = Math.max(0, Math.floor((end-Date.parse(c.created_at))/1000));
    const model = c.progress?.model_evidence === 'session_log' ? `实际模型 ${c.progress.model}` : '实际模型待回执确认';
    const stage = {starting:'启动会话',composing:'创作词曲与规格',format_check:'检查原回复格式',format_repair:'修复回复封装，词曲不变',validating:'校验词曲、配谱与规格',dispatching:'提交工作流',collecting:'回收音频'}[c.progress?.stage];
    const stale = ['working','rendering'].includes(c.state) && c.progress?.reported_at && Date.now()-Date.parse(c.progress.reported_at)>45000;
    return `${Math.floor(elapsed/60)} 分 ${elapsed%60} 秒${c.format_recovery?'（累计，含等待恢复）':''} · ${c.kind==='analyze'?'音频分析，不调用创作模型':c.kind==='render'?'原样渲染，不调用创作模型':model}${c.state==='working'&&stage?' · '+stage:''}${stale?' · 心跳更新较慢，请核对连接':''}${c.format_recovery?' · 复用原回复继续':''}${c.retry_of?' · 失败任务的新尝试':''}`;
  };
  const statuses = {
    preparing: "准备中",
    queued: "等待 agent",
    working: "Hermes 创作中",
    rendering: "渲染 / 归档中",
    analyzing: "转谱 / 歌词识别中",
    completed: "已完成",
    needs_attention: "需要处理",
    cancelled: "已撤回",
  };
  const commandStatus = (c) => {
    if (c.state !== "rendering") return statuses[c.state] || c.state;
    const job = state.data.revisions
      .flatMap((r) => r.renders)
      .find((j) => j.render_id === c.render_id);
    return (
      {
        queued: "等待 GPU",
        running: "GPU 生成中",
        collecting: "校验并归档",
        submission_unknown: "核对提交结果",
        prepared: "准备提交",
        created: "准备工作流",
      }[job?.state] || "生成 / 核对中"
    );
  };
  $("commands").innerHTML = state.data.commands.length
    ? state.data.commands
        .slice()
        .reverse()
        .map(
          (c) =>
            `<div class="command-row"><span class="command-state">${esc(commandStatus(c))}</span><div class="command-content">${c.experiment_arm ? `试验 ${esc(c.experiment_arm)} · ` : ""}${esc(names[c.assigned_to])} · ${esc(c.instruction)}<small>${c.kind === "analyze" ? esc(label(c.source_revision_id))+" → 音频分析" : c.kind === "plan" ? "创作讨论" : c.kind === "compose" ? "已确认方案 → 原创首版" : esc(label(c.source_revision_id)) + " → " + (c.result_revision_id ? esc(label(c.result_revision_id)) : "新候选")} · ${date(c.created_at)}</small><small>${esc(detail(c))}</small>${c.production_binding ? `<small>生成服务器：${esc(c.production_binding.mcp_alias)}</small>` : ""}${c.songcraft ? `<small>创作方法：${esc(c.songcraft.label)} · v${esc(c.songcraft.version)} · ${esc(c.songcraft.sha256.slice(0,8))}</small>` : ""}${c.format_check?`<small>${c.format_check.status==='repaired'?`已自动修复回复格式（${c.format_check.operation_count} 项），词曲原文未改`:'回复格式检查通过'} · 原回复 ${esc(c.format_check.raw_sha256.slice(0,8))} · 校验记录 ${esc(c.format_check.report_sha256.slice(0,8))}</small>`:''}${c.issue?`<p class="hint">${esc(messages[c.issue]||c.issue)}</p>`:''}</div>${c.state === "completed" && c.result_revision_id ? `<button data-open="${c.result_revision_id}">试听新候选</button>` : c.state === "queued" || c.state === "preparing" ? `<button data-cancel="${c.command_id}">撤回</button>` : c.render_id ? `<button data-refresh="${c.command_id}">核对进度</button>` : c.can_recover_format ? `<button data-recover="${c.command_id}">检查原回复并继续</button><button data-retry="${c.command_id}">重新创作</button>` : c.can_retry ? `<button data-retry="${c.command_id}">重新尝试</button>` : ""}</div>`,
        )
        .join("")
    : '<div class="empty">创作讨论、首版生成和后续修改的进度都会保存在这里。</div>';
  $("commands")
    .querySelectorAll("[data-open]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          const id = b.dataset.open;
          if (!state.selected.includes(id))
            state.selected = [...state.selected.slice(-2), id];
          focus(id);
          cards();
          versionList();
          $("compare").scrollIntoView({ block: "center" });
        }),
    );
  for (const action of ["cancel", "refresh", "retry", "recover"])
    $("commands")
      .querySelectorAll(`[data-${action}]`)
      .forEach(
        (b) =>
          (b.onclick = async () => {
            b.disabled = true;
            try {
              await mutate(
                route(`/commands/${b.dataset[action]}/${action==='recover'?'format-recover':action}`),
                {},
              );
              await loadProject();
            } catch (e) {
              warn(errorText(e));
            } finally {
              b.disabled = false;
            }
          }),
      );
  const notes = state.data.events.filter((e) => e.type === "producer_feedback");
  const history = [
    ...(state.data.historical_feedback || []).map((entry) => {
      const f = entry.payload.feedback;
      const scope =
        {
          overall: "整体",
          lyrics: "歌词",
          melody: "旋律",
          audio_performance: "演唱",
        }[f.scope] || f.scope;
      return {
        at: entry.created_at,
        html: `<b>历史反馈 · ${esc(scope)}</b><br>${esc(f.free_text)}<small>${date(entry.created_at)} · ${f.evaluation_mode === "lyrics_only" ? "仅歌词评价" : "已记录试听"} · 保留原实验绑定，未自动采用`,
      };
    }),
    ...notes.map((n) => ({
      at: n.created_at,
      html: `<b>${esc(label(n.revision_id))} · 听感记录</b><br>${esc(n.content)}<small>${date(n.created_at)} · ${n.render_id ? "绑定实际试听音频" : "文本反馈"} · ${short(n.render_id)}`,
    })),
    ...state.data.decisions.map((d) => ({
      at: d.created_at,
      html: `<b>${d.action.toUpperCase()} · ${esc(label(d.target_revision_id))}</b>　${esc(d.reason)}<small>${date(d.created_at)} · ${short(d.target_revision_id)}${d.selected_render_id ? " · 音频 " + short(d.selected_render_id) : ""}${d.action !== "reject" ? `　<button data-rollback="${d.target_revision_id}">Rollback 到此版本</button>` : ""}`,
    })),
  ].sort((a, b) => b.at.localeCompare(a.at));
  $("timeline").innerHTML = history
    .map((h) => `<div class="timeline-row">${h.html}</small></div>`)
    .join("");
  $("timeline")
    .querySelectorAll("[data-rollback]")
    .forEach(
      (b) => (b.onclick = () => decision("rollback", b.dataset.rollback)),
    );
}
function modal(title, html, confirm, callback) {
  $("dialogTitle").textContent = title;
  $("dialogBody").innerHTML = html;
  $("confirmDialog").textContent = confirm;
  $("dialogError").textContent = "";
  $("dialog").showModal();
  $("dialogForm").onsubmit = async (e) => {
    e.preventDefault();
    $("confirmDialog").disabled = true;
    try {
      await callback();
      $("dialog").close();
    } catch (e) {
      $("dialogError").textContent = errorText(e);
    } finally {
      $("confirmDialog").disabled = false;
    }
  };
}
function decision(action, rid) {
  const project = { ...state.data.project },
    r = candidate(rid),
    render = currentRender(r),
    path = route("/decisions");
  modal(
    `${action.toUpperCase()} · ${label(rid)}`,
    `<p>${action === "reject" ? "记录拒绝理由；不会切换当前采用版本，原文和音频会保留。" : "将此版本设为当前采用。其他候选和全部历史会保留。"}</p><p class="hint">版本 ${short(rid)} · ${render ? "音频 " + short(render.render_id) : "无已完成音频"}<br>当前采用：${project.head_revision_id ? esc(label(project.head_revision_id)) : "尚未选择"} · 第 ${project.head_generation} 次选择</p><label>制作理由<textarea id="decisionReason" required maxlength="4000" rows="3" placeholder="这版值得保留什么，或为什么暂不采用？"></textarea></label>`,
    action.toUpperCase(),
    async () => {
      await mutate(path, {
        target_revision_id: rid,
        action_name: action,
        expected_head_revision_id: project.head_revision_id,
        expected_head_generation: project.head_generation,
        reason: $("decisionReason").value,
        selected_render_id: render?.render_id || null,
      });
      await loadProject(false);
      await libraries();
      cards();
      notify("制作决定已保存。");
    },
  );
}
async function sendCommand(kind) {
  const r = candidate(state.focus);
  if (!r) return;
  const instruction =
    kind === "render"
      ? `保留当前 ABC 与歌词，按 ${$("renderDuration").value} 秒上限生成试听`
      : $("instruction").value.trim();
  if (!instruction) {
    notify("先写下你希望怎么改。");
    return;
  }
  const b = $(kind === "render" ? "sendRender" : "sendEdit");
  b.disabled = true;
  try {
    await mutate(route("/commands"), {
      revision_id: state.focus,
      expected_snapshot_sha256: r.revision.snapshot_sha256,
      assigned_to: $("agent").value,
      ...($("editMcp").value ? {mcp_alias:$("editMcp").value} : {}),
      kind,
      instruction,
      allowed_bar_ids: kind === "pitch_edit" ? [...state.bars] : [],
      ...(kind !== 'render' ? {songcraft_selection:$("editSongcraft").value} : {}),
      ...(kind === 'direction' ? {scope: $("editScope").value === 'song' ? {mode:'song'} : {mode:'section',section_id:$("editScope").value.slice(8)}} : {}),
      ...(kind === 'direction' ? {preserve:[...$("preserveFields").querySelectorAll('input:checked')].map(e=>e.value)} : {}),
      ...(kind === "render"
        ? { target_duration: Number($("renderDuration").value) }
        : {}),
    });
    $("instruction").value = "";
    await loadProject();
    notify("任务已进入所选 agent 的队列。");
  } catch (e) {
    warn(errorText(e));
  } finally {
    b.disabled = false;
    bars();
  }
}
const drafts = new Map();
function draftKey() {
  return `${state.lib}/${state.pid}/${state.focus}`;
}
function saveDraft() {
  if (state.focus)
    drafts.set(draftKey(), {
      instruction: $("instruction").value,
      feedback: $("feedback").value,
      mcp: $("editMcp").value,
      songcraft: $("editSongcraft").value,
      songcraftPolicy: "professional-20260918",
    });
}
function restoreDraft() {
  const d = drafts.get(draftKey()) || {};
  $("instruction").value = d.instruction || "";
  $("feedback").value = d.feedback || "";
  setMcpChoice("editMcp",d.mcp || "");
  $("editSongcraft").value = d.songcraftPolicy === "professional-20260918" ? (d.songcraft || "professional") : "professional";
}
const mcpCatalogs = {};
const mcpChoices = {};
const mcpFields = {creationMcp:'creationAgent',editMcp:'agent'};
function setMcpChoice(id, value) {
  mcpChoices[id+':'+$(mcpFields[id]).value] = value;
  paintMcp(id);
}
function paintMcp(id) {
  const actor=$(mcpFields[id]).value, data=mcpCatalogs[actor];
  const value=mcpChoices[id+':'+actor] || '';
  const rows=data?.servers || [];
  const optionsHtml = `<option value="">使用此伙伴默认 · ${esc(data?.default_alias || '读取中')}</option>`+
    rows.map(r=>`<option value="${esc(r.alias)}" ${r.available?'':'disabled'}>${esc(r.alias)}${r.hardware?' · '+esc(r.hardware):''}${r.alias===data.default_alias?' · 默认':''}${r.available?'':' · '+esc(messages[r.reason] || r.reason || '不可用')}</option>`).join('')+
    (value && !rows.some(r=>r.alias===value) ? `<option value="${esc(value)}" disabled>${esc(value)} · 已不在当前列表</option>` : '');
  if($(id).dataset.optionsHtml!==optionsHtml){$(id).innerHTML=optionsHtml;$(id).dataset.optionsHtml=optionsHtml;}
  if($(id).value!==value)$(id).value=value;
  $(id+'Status').textContent=data?.refreshing?'正在从 Hermes 读取并核验 MCP，约需一分钟…':data?.error?(messages[data.error]||data.error):data?.checked_at?`来自 ${names[actor]} · ${new Date(data.checked_at*1000).toLocaleTimeString()} 核验。新任务使用所选服务器；已提交任务保持原选择。`:'点击「读取 MCP 列表」从此伙伴获取实际配置。';
  $(id+'Refresh').disabled=!!data?.refreshing;
  $(id+'Default').disabled=!value || !rows.find(r=>r.alias===value)?.available;
}
async function mcpStatus() {
  const actors=[...new Set(Object.values(mcpFields).map(id=>$(id).value))];
  await Promise.all(actors.map(async actor=>{
    try {mcpCatalogs[actor]=await api('/api/mcp-servers/'+actor);} catch(e) {mcpCatalogs[actor]={error:e.code||e.message};}
  }));
  Object.keys(mcpFields).forEach(paintMcp);
}
for(const [id,agent] of Object.entries(mcpFields)) {
  $(id).addEventListener('change',()=>{setMcpChoice(id,$(id).value);if(id==='creationMcp')saveCreationDraft();});
  $(agent).addEventListener('change',()=>{paintMcp(id);mcpStatus();});
  $(id+'Refresh').onclick=async()=>{
    const actor=$(agent).value;
    try {mcpCatalogs[actor]=await mutate('/api/mcp-servers/'+actor+'/refresh',{});Object.keys(mcpFields).forEach(paintMcp);}
    catch(e){warn(errorText(e));}
  };
  $(id+'Default').onclick=async()=>{
    const actor=$(agent).value, alias=$(id).value;
    if(!alias)return;
    try {mcpCatalogs[actor]=await mutate('/api/mcp-servers/'+actor+'/default',{mcp_alias:alias});Object.keys(mcpFields).forEach(paintMcp);notify(`${names[actor]} 默认生成服务器已设为 ${alias}，Hermes Desktop 同样生效。`);}
    catch(e){warn(errorText(e));}
  };
}
async function agentStatus() {
  await mcpStatus();
  try {
    const values = await api("/api/agents");
    for (const option of [
      ...$("agent").options,
      ...$("creationAgent").options,
    ]) {
      const v = values[option.value];
      option.textContent = `${names[option.value]} · ${v?.online ? (v.state === "working" ? "忙碌，可排队" : "在线") : "离线，任务会等待"}`;
    }
  } catch {
    for (const option of [...$("agent").options, ...$("creationAgent").options])
      option.textContent = names[option.value] + " · 连接待检查";
  }
}
$("sendEdit").onclick = () => sendCommand($("editScope").value === 'pitch' ? 'pitch_edit' : 'direction');
$("editScope").onchange = bars;
$("repairScore").onclick = () => {
  const r = candidate(state.focus);
  const rid = state.focus, path = route('/score-repair');
  if (!r?.score_repair?.available) return;
  modal('修正谱面拍数', `<p>在以下小节内等比例调整所有音符与休止符时值，保留音高、音符顺序与相对节奏。此操作会改变这些小节的总时长。</p><ul>${r.score_repair.changes.map(c => `<li>${esc(c.voice_id)} 第 ${c.ordinal} 小节：${esc(c.before_beats)} → ${esc(c.after_beats)} 拍，时值乘 ${esc(c.duration_scale)}</li>`).join('')}</ul><p>另存一个谱面候选；原谱和原歌曲保留，新候选需要重新生成歌曲才能对照试听。</p>`, '修正并另存', async () => {
    const rev = await mutate(path, {revision_id:rid,expected_snapshot_sha256:r.revision.snapshot_sha256});
    state.focus = rev.revision_id;
    state.bars.clear();
    state.selected = [...state.selected.slice(-2), rev.revision_id];
    await loadProject(true);
    notify('修正谱面已另存，可试听输入谱或生成新歌曲。');
  });
};
$("sendRender").onclick = () => sendCommand("render");
$("saveFeedback").onclick = async () => {
  const r = candidate(state.focus),
    content = $("feedback").value.trim();
  if (!r || !content) return;
  const b = $("saveFeedback");
  b.disabled = true;
  try {
    await mutate(route("/feedback"), {
      revision_id: state.focus,
      render_id: currentRender(r)?.render_id || null,
      content,
    });
    $("feedback").value = "";
    await loadProject();
    notify("听感已保存，当前采用版本没有改变。");
  } catch (e) {
    warn(errorText(e));
  } finally {
    b.disabled = false;
  }
};
$("newProject").onclick = () =>
  modal(
    "新建歌曲",
    '<p class="hint">先起一个工作名，随后可以和银月或小舞讨论这首歌。无需已有歌词或乐谱。</p><label>项目名称<input id="newTitle" required maxlength="200" placeholder="给这首歌起一个工作名"></label>',
    "创建，开始构思",
    async () => {
      const p = await mutate("/api/studio/projects", {
        title: $("newTitle").value,
      });
      await libraries();
      await openProject("studio", p.project_id);
      $("creationPanel").open = true;
      $("creationPanel").scrollIntoView({ block: "start" });
      $("creationMessage").focus();
    },
  );
$("addCandidate").onclick = () => {
  if (!state.pid) return;
  const path = route("/revisions");
  modal(
    "导入原创候选",
    `<p class="hint">保存 ABC 和歌词作为独立候选，之后再交给 Hermes 渲染。时长上限支持 300 秒，模型可以提前结束。</p><label>ABC 原文<textarea id="importABC" rows="5" required placeholder="X:1&#10;T:歌名&#10;M:4/4&#10;L:1/8&#10;Q:1/4=80&#10;K:C&#10;C2 D2 E2 G2 |"></textarea></label><label>歌词<textarea id="importLyrics" rows="3" required></textarea></label><label>风格<input id="importStyle" required value="Acoustic folk pop, natural singing, gentle guitar."></label><label>时长上限（秒）<input id="importDuration" type="number" min="1" max="300" value="300" required></label><label>随机种子<input id="importSeed" type="number" min="0" max="9007199254740991" value="260916" required></label>`,
    "保存候选",
    async () => {
      const rev = await mutate(path, {
        abc: $("importABC").value,
        brief: {
          style: $("importStyle").value,
          lyrics: $("importLyrics").value,
          checkpoint: "yue2_3b_int8_convrot.safetensors",
          seed: Number($("importSeed").value),
          max_duration: Number($("importDuration").value),
        },
        summary: "制作人在 Producer Console 导入原创候选",
      });
      state.focus = rev.revision_id;
      state.selected = [...state.selected.slice(-2), rev.revision_id];
      await loadProject(true);
    },
  );
};
$("search").oninput = sidebar;
$("refresh").onclick = async () => {
  await libraries();
  await loadProject(true);
};
$("closeDialog").onclick = $("cancelDialog").onclick = () =>
  $("dialog").close();
$("historyButton").onclick = () =>
  $("timeline").scrollIntoView({ block: "center" });
document.querySelectorAll("[data-tab]").forEach(
  (b) =>
    (b.onclick = () => {
      state.tab = b.dataset.tab;
      document
        .querySelectorAll("[data-tab]")
        .forEach((x) => x.setAttribute("aria-selected", String(x === b)));
      for (const name of ["score", "lyrics", "abc"])
        $(name + "View").hidden = name !== state.tab;
    }),
);
async function boot() {
  try {
    state.csrf = (await api("/api/session")).csrf;
    const songcraftOptions = (await api('/api/songcraft')).map(s=>`<option value="${esc(s.id)}">${esc(s.label)}</option>`).join('');
    for (const id of ['creationSongcraft','editSongcraft']) $(id).innerHTML = songcraftOptions;
    await libraries();
    await agentStatus();
    let saved;
    try {
      saved = JSON.parse(localStorage.getItem("jr-project"));
    } catch {}
    const valid =
      saved &&
      state.libraries
        .find((l) => l.id === saved.lib)
        ?.projects.some((p) => p.project_id === saved.pid);
    const initial = valid
      ? saved
      : {
          lib: "experiments",
          pid: state.libraries
            .find((l) => l.id === "experiments")
            ?.projects.find((p) => p.title.includes("三候选"))?.project_id,
        };
    if (!initial.pid) {
      const l = state.libraries.find((l) => l.projects.length);
      if (l) {
        initial.lib = l.id;
        initial.pid = l.projects[0].project_id;
      }
    }
    if (initial.pid) await openProject(initial.lib, initial.pid);
    else {
      $("connection").textContent = "已连接";
      $("compare").innerHTML =
        '<div class="empty">点击左侧「新建歌曲」，从一个想法开始创作。</div>';
      $("workbench").hidden = true;
    }
    if (sessionStorage.getItem("jr-pending"))
      warn("有一个上次未确认的操作，可以安全核对原请求。", retryPending);
    setInterval(() => {
      if (!document.hidden && !state.busy && !$("dialog").open) {
        loadProject(false);
        agentStatus();
      }
    }, 5000);
  } catch (e) {
    warn(errorText(e));
    $("connection").textContent = "连接失败";
  }
}
boot();
