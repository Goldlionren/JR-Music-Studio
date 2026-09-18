"use strict";
// Reference notes come from the bounded parser, not from the generated recording.
const ScoreReview = (() => {
  let context,
    sources = [],
    frame,
    generation = 0;
  function stop() {
    generation++;
    cancelAnimationFrame(frame);
    sources.forEach((node) => {
      try {
        node.stop();
      } catch {}
    });
    sources = [];
    document
      .querySelectorAll(".score-playing")
      .forEach((e) => e.classList.remove("score-playing"));
    const status = document.getElementById("scoreClock");
    if (status) status.textContent = "已停止";
  }
  document.addEventListener(
    "play",
    (event) => {
      if (event.target.tagName !== "AUDIO") return;
      stop();
      document.querySelectorAll("audio").forEach((a) => {
        if (a !== event.target) a.pause();
      });
    },
    true,
  );
  window.addEventListener("pagehide", stop);
  async function play(
    voice,
    selected,
    output,
    title = "输入谱",
    highlight = true,
  ) {
    stop();
    document.querySelectorAll("audio").forEach((a) => a.pause());
    const ticket = generation;
    context ||= new AudioContext();
    await context.resume();
    if (ticket !== generation) return;
    const bars = voice.bars.filter((b) => !selected || selected.has(b.bar_id));
    if (!bars.length) {
      output.textContent = "请先选择这个声部的小节。";
      return;
    }
    // Selected bars remain in score order. Gaps are skipped explicitly.
    let length = 0;
    const segments = bars.map((b) => {
      const s = { ...b, offset: length };
      length += b.end - b.start;
      return s;
    });
    const origin = context.currentTime + 0.08;
    const blocks = [];
    for (const segment of segments) {
      const previous = blocks.at(-1);
      if (previous && Math.abs(previous.end - segment.start) < 1e-7)
        previous.end = segment.end;
      else blocks.push({ ...segment });
    }
    for (const segment of blocks) {
      for (const note of voice.notes) {
        const start = Math.max(segment.start, note.start),
          end = Math.min(segment.end, note.end);
        if (end <= start) continue;
        // Contiguous selected measures share one block, preserving tied notes.
        const t = origin + segment.offset + start - segment.start;
        const until = origin + segment.offset + end - segment.start;
        const node = context.createOscillator(),
          gain = context.createGain();
        node.type = "triangle";
        node.frequency.value = 440 * 2 ** ((note.midi - 69) / 12);
        gain.gain.setValueAtTime(0, t);
        gain.gain.linearRampToValueAtTime(
          0.13,
          t + Math.min(0.006, (until - t) / 4),
        );
        gain.gain.setValueAtTime(0.13, Math.max(t, until - 0.012));
        gain.gain.linearRampToValueAtTime(0, until);
        node.connect(gain);
        gain.connect(context.destination);
        node.start(t);
        node.stop(until);
        node.onended = () => {
          node.disconnect();
          gain.disconnect();
        };
        sources.push(node);
      }
    }
    function tick() {
      if (ticket !== generation) return;
      const time = Math.max(0, context.currentTime - origin);
      const bar = segments.find(
        (s) => time >= s.offset && time < s.offset + s.end - s.start,
      );
      output.textContent = `${title} · ${Math.min(time, length).toFixed(1)} / ${length.toFixed(1)} 秒${bar ? " · " + voice.voice_id + " 第 " + bar.ordinal + " 小节" : ""}${selected ? " · 所选小节顺序播放" : ""}`;
      if (highlight)
        document
          .querySelectorAll("[data-bar]")
          .forEach((button) =>
            button.classList.toggle(
              "score-playing",
              button.dataset.bar === bar?.bar_id,
            ),
          );
      if (time < length) frame = requestAnimationFrame(tick);
      else {
        sources = [];
        document
          .querySelectorAll(".score-playing")
          .forEach((e) => e.classList.remove("score-playing"));
      }
    }
    tick();
  }
  function mount({
    root,
    revision,
    render,
    audioUrl,
    esc,
    selected,
    save,
    loadReport,
  }) {
    stop();
    const plan = revision.score_audition;
    const voices = plan?.status === "available" ? plan.voices : [];
    const defaultVoice =
      voices.find((v) => v.voice_id.toLowerCase() === "vocal") || voices[0];
    root.innerHTML = `<h3>输入谱与生成歌曲 · 对照试听</h3>
      <p class="hint">上方是创作输入谱，不是从歌曲转写的实唱谱。先听原谱，再听歌曲，比较音高走向与节奏。</p>
      <div class="review-controls"><label>谱面声部 <select id="scoreVoice" ${!voices.length ? "disabled" : ""}>${voices.length ? voices.map((v) => `<option ${v === defaultVoice ? "selected" : ""}>${esc(v.voice_id)}</option>`).join("") : '<option>当前谱面无法合成</option>'}</select>${voices.length === 1 ? '<small>当前谱面只有一个声部</small>' : ''}</label>
      <button id="playScore" ${!voices.length ? "disabled" : ""}>▶ 听输入谱</button><button id="playScoreSelection" ${!voices.length ? "disabled" : ""}>▶ 听所选小节</button><button id="stopScore">停止</button></div>
      <p class="hint">${esc(plan?.reason || "谱面试听不可用")} 使用合成音色，只比较旋律；不模拟歌手。</p><output id="scoreClock" aria-live="off">已停止</output>
      <div class="recording-check"><h4>当前候选的生成歌曲</h4><p class="hint">${esc(render ? "音频 " + render.render_id.slice(-8) + " · 与当前候选绑定；声学遵谱程度尚需核对。" : "当前候选尚无生成音频。")}</p>
      ${audioUrl ? `<audio id="reviewAudio" controls preload="metadata" src="${esc(audioUrl)}" aria-label="当前候选对照音频"></audio><div class="review-controls"><label>从歌曲第几秒听 <input id="reviewStart" type="number" value="0" min="0" step="0.1"></label><button id="playRecording">▶ 从此处听歌曲</button></div><p class="hint">请按听感寻找人声起点；这里的秒数不自动对应谱面小节。</p>` : ""}</div>
      <details><summary>记录这次对照</summary><label>你的判断 <select id="reviewVerdict"><option value="">请选择</option><option>旋律与节奏大体接近</option><option>音高走向不同</option><option>节奏或分句不同</option><option>音高与节奏均不同</option><option>无法判断</option></select></label><label>听到的差异<textarea id="reviewNote" maxlength="1800" rows="2" placeholder="例如：歌曲开头有哼唱，主歌第一句的音高走向与谱面不同。"></textarea></label><button id="saveReview" ${!render ? "disabled" : ""}>保存对照记录</button><p class="hint">记录绑定当前候选和这次音频，不自动 Accept，也不改写乐谱。</p></details>
      <div id="transcriptionReview"></div>`;
    const byId = (id) => root.querySelector("#" + id);
    const currentVoice = () =>
      voices.find((v) => v.voice_id === byId("scoreVoice").value);
    byId("playScore").onclick = () =>
      play(currentVoice(), null, byId("scoreClock")).catch(
        (e) => (byId("scoreClock").textContent = e.message),
      );
    byId("playScoreSelection").onclick = () =>
      play(currentVoice(), new Set(selected()), byId("scoreClock")).catch(
        (e) => (byId("scoreClock").textContent = e.message),
      );
    byId("stopScore").onclick = () => {
      stop();
      document.querySelectorAll("audio").forEach((a) => a.pause());
    };
    byId("scoreVoice").onchange = stop;
    if (audioUrl)
      byId("playRecording").onclick = async () => {
        const audio = byId("reviewAudio"),
          seconds = Number(byId("reviewStart").value);
        if (
          !Number.isFinite(seconds) ||
          seconds < 0 ||
          !Number.isFinite(audio.duration) ||
          seconds >= audio.duration
        ) {
          byId("scoreClock").textContent =
            "请输入歌曲时长以内的秒数，或等待音频加载。";
          return;
        }
        audio.currentTime = seconds;
        try {
          await audio.play();
        } catch (e) {
          byId("scoreClock").textContent = e.message;
        }
      };
    byId("saveReview").onclick = async () => {
      const verdict = byId("reviewVerdict").value;
      if (!verdict) {
        byId("scoreClock").textContent = "请先选择对照判断。";
        return;
      }
      const button = byId("saveReview");
      button.disabled = true;
      try {
        await save({
          revision_id: revision.revision.revision_id,
          render_id: render.render_id,
          content: `谱面 / 实唱人工对照：${verdict}\n输入谱 SHA：${revision.revision.abc_sha256}\n声部：${byId("scoreVoice").value || "未选"}；参考小节：${Array.from(selected()).join("、") || "全谱"}\n歌曲起点：${byId("reviewStart")?.value || 0} 秒（人工选择，非自动对齐）\n${byId("reviewNote").value}`,
        });
        button.textContent = "已保存对照记录";
      } catch (e) {
        byId("scoreClock").textContent = e.message;
        button.disabled = false;
      }
    };
    const reportRoot = byId("transcriptionReview");
    if (render && loadReport) {
      reportRoot.textContent = "正在读取转谱核验…";
      loadReport()
        .then((report) => {
          if (!reportRoot.isConnected) return;
          if (report.status !== "transcribed_estimate") {
            reportRoot.textContent = report.message;
            return;
          }
          const vocal = report.audition?.voices.find(
            (v) => v.voice_id.toLowerCase() === "vocal",
          );
          const source = voices.find(
            (v) => v.voice_id.toLowerCase() === "vocal",
          );
          const row = (label, a, b) =>
            `<tr><th>${label}</th><td>${esc(a)}</td><td>${esc(b)}</td></tr>`;
          reportRoot.innerHTML = `<h4>从这次音频转写的谱 · 模型估计</h4>
          <p class="hint">SheetSage2 分析原始音频所得，不是输入谱的副本。混音中的哼唱、乐器和人声可能识别混淆；未经人工逐音确认，不给准确率或通过结论。</p>
          <table class="score-comparison"><thead><tr><th>比较项</th><th>创作输入谱</th><th>音频转写谱</th></tr></thead><tbody>
          ${row("速度", plan?.bpm ? plan.bpm + " BPM" : "未知", report.audition?.bpm ? report.audition.bpm + " BPM" : report.tempo)}
          ${row("人声小节数", source?.bars.length ?? "未解析", vocal?.bars.length ?? "未解析")}
          ${row("谱面时长", source ? source.duration.toFixed(1) + " 秒" : "未知", vocal ? vocal.duration.toFixed(1) + " 秒" : "未知")}
          ${row("段落标签", revision.score.sections.map((s) => s.suggested_label).join(" / "), report.sections.join(" / "))}</tbody></table>
          <p class="hint">原音频 ${Number(report.source_duration).toFixed(1)} 秒。转写谱的时长来自估计速度与节奏量化，不等于实际时间线；两份谱的小节编号不直接对应。</p>
          <button id="playTranscription" ${!vocal ? "disabled" : ""}>▶ 听转写的人声旋律</button>
          <details><summary>查看转写谱（只读）</summary><div id="transcriptionSheet" aria-label="音频转写估计谱"></div><details><summary>转写 ABC 与证据</summary><pre>${esc(report.transcribed_abc)}</pre><p class="hint">音频 SHA：${esc(report.source_audio_sha256)}<br>转写 SHA：${esc(report.transcribed_abc_sha256)}<br>分析任务：${esc(report.prompt_id)}</p></details></details>`;
          reportRoot.querySelector("#playTranscription").onclick = () =>
            play(
              vocal,
              null,
              byId("scoreClock"),
              "音频转写估计谱",
              false,
            ).catch((e) => (byId("scoreClock").textContent = e.message));
          try {
            ABCJS.renderAbc(
              reportRoot.querySelector("#transcriptionSheet"),
              report.transcribed_abc,
              { responsive: "resize", staffwidth: 650 },
            );
          } catch {
            reportRoot.querySelector("#transcriptionSheet").textContent =
              "无法显示，请展开转写 ABC 查看。";
          }
        })
        .catch((e) => {
          if (reportRoot.isConnected)
            reportRoot.textContent = "转谱记录读取失败：" + e.message;
        });
    }
  }
  return { mount, stop, play };
})();
