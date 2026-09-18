"use strict";
const AudioAnalysis=(()=>{
  let cleanup=()=>{};
  const drafts=new Map();
  function mount({root,render,audioUrl,agents,get,post,esc,refresh,direct,error}){
    cleanup();let disposed=false,timer;cleanup=()=>{disposed=true;clearTimeout(timer);};
    if(!render||!audioUrl){root.innerHTML='';return;}
    root.innerHTML=`<details><summary>实际演唱 · 歌词时间线与歌曲检查</summary><p class="hint">按这次音频识别歌词并估计时间。识别可能漏字或误识别；可手动校正，输入乐谱的时间不作为实唱时间。</p>
      <div class="analysis-actions"><select class="analysis-agent" aria-label="分析执行伙伴">${(agents||[]).map(a=>`<option value="${esc(a)}">${a==='yinyue'?'银月':'小舞'}</option>`).join('')}</select><button class="analysis-start" ${agents?.length?'':'disabled'}>分析这次音频</button><button class="analysis-refresh">更新分析结果</button><button class="analysis-resume" hidden>继续核对 / 识别</button></div>
      <p class="analysis-status" role="status"></p><audio class="aligned-audio" controls preload="metadata" src="${esc(audioUrl)}"></audio><div class="audio-waveform"></div><div class="lyric-times"></div><div class="song-checks"></div><details><summary>识别原文与证据</summary><pre class="asr-evidence"></pre></details></details>`;
    const find=q=>root.querySelector(q),audio=find('audio'),status=find('.analysis-status');
    let timeline,analysis;
    async function checks(){
      const report=await get('/song-check');if(disposed)return;
      const box=find('.song-checks'),labels={unknown:'待分析',observed:'已有测量',review:'建议核对',manual_required:'需人工试听'};
      box.innerHTML=`<h4>完整歌曲检查</h4><p class="hint">测量结果用于找问题，不自动判断歌曲好坏或替你 Accept。人工校正时间不会抹掉原始识别的漏字提示。</p>
        <div class="review-controls">${report.listening_points.map((t,i)=>`<button data-check-seek="${t}">▶ ${['开头','中段','结尾'][i]} ${t.toFixed(1)} 秒</button>`).join('')}</div>
        ${report.checks.map((c,i)=>`<article class="song-check ${c.status}"><b>${esc(c.label)}</b> <small>${labels[c.status]}</small><p>${esc(c.evidence)}</p>${c.start!=null?`<button data-check-seek="${c.start}">定位试听 ${c.start.toFixed(1)} 秒</button>`:''}${c.instruction?`<button data-direction="${i}">填入下一版方向</button>`:''}</article>`).join('')}
        <label>人工整曲检查结论<select class="check-decision"><option value="undecided">尚未决定</option><option value="revise">需要再修改</option><option value="ready">我已试听，完整度可接受</option></select></label><label>检查笔记<textarea class="check-note" rows="3" placeholder="例如：声线前后一致；最后一句完整，但尾奏偏长。"></textarea></label><button class="save-check">保存本次检查</button><p class="check-status"></p>
        <details><summary>历次整曲检查（${report.decisions.length}）</summary>${report.decisions.map(d=>`<p>${esc(d.created_at)} · ${esc({ready:'完整度可接受',revise:'需要修改',undecided:'未决定'}[d.decision])}<br>${esc(d.note)}</p>`).join('')}</details>`;
      box.querySelectorAll('[data-check-seek]').forEach(b=>b.onclick=()=>{audio.currentTime=+b.dataset.checkSeek;audio.play().catch(()=>{});});
      box.querySelectorAll('[data-direction]').forEach(b=>b.onclick=()=>direct(report.checks[+b.dataset.direction].instruction));
      box.querySelector('.save-check').onclick=async()=>{try{await post('/song-check',{expected_report_sha256:report.report_sha256,decision:find('.check-decision').value,note:find('.check-note').value});await checks();find('.check-status').textContent='已保存检查证据；最终采用版本仍由你单独决定。';}catch(e){find('.check-status').textContent=error(e);}};
    }
    function drawLines(){
      const cached=drafts.get(render.render_id);
      if(cached&&cached.generation===timeline.generation)timeline=structuredClone(cached);
      const box=find('.lyric-times');
      box.innerHTML=`<p class="hint">${timeline.basis==='human_corrected'?'已保存人工校正':'自动估计，空白表示尚未定位'} · 时间单位：秒。可播放定位，再用“取当前”标记。</p><table class="lyric-time-table"><thead><tr><th>歌词</th><th>起点</th><th>终点</th></tr></thead><tbody>${timeline.lines.map((line,i)=>`<tr data-line="${i}"><td><button class="seek-line" data-i="${i}" ${line.start==null?'disabled':''}>▶</button><small>${esc(line.section)} · ${line.basis==='manual'?'人工校正':line.basis==='asr_estimate'?'估计':'待定位'}</small><div>${esc(line.text)}</div></td>${['start','end'].map(k=>`<td><input type="number" min="0" max="${render.verification.duration_seconds}" step="0.01" aria-label="第 ${i+1} 行${k==='start'?'起点':'终点'}" data-i="${i}" data-k="${k}" value="${line[k]??''}"><button data-mark="${k}" data-i="${i}">取当前</button></td>`).join('')}</tr>`).join('')}</tbody></table><button class="save-times">保存时间线校正</button><button class="discard-times">放弃未保存校正</button><p class="timeline-status"></p>`;
      const keep=()=>{drafts.set(render.render_id,structuredClone(timeline));find('.timeline-status').textContent='有未保存的校正';};
      box.querySelectorAll('input').forEach(input=>input.oninput=()=>{timeline.lines[+input.dataset.i][input.dataset.k]=input.value===''?null:Number(input.value);keep();});
      box.querySelectorAll('[data-mark]').forEach(button=>button.onclick=()=>{timeline.lines[+button.dataset.i][button.dataset.mark]=Math.round(audio.currentTime*100)/100;keep();drawLines();});
      box.querySelectorAll('.seek-line').forEach(button=>button.onclick=()=>{audio.currentTime=timeline.lines[+button.dataset.i].start;audio.play().catch(()=>{});});
      find('.save-times').onclick=async()=>{try{
        const saved=await post('/lyric-timeline',{expected_generation:timeline.generation,lines:timeline.lines.map(({line_id,start,end})=>({line_id,start,end}))});
        if(disposed)return;drafts.delete(render.render_id);timeline=saved;drawLines();await checks();find('.timeline-status').textContent='时间线已保存；音频和歌词原文保持不变。';
      }catch(e){find('.timeline-status').textContent=error(e);}};
      find('.discard-times').onclick=()=>{drafts.delete(render.render_id);load();};
    }
    audio.ontimeupdate=()=>{
      const t=audio.currentTime;
      root.querySelectorAll('[data-line]').forEach(row=>{const line=timeline?.lines[+row.dataset.line];row.classList.toggle('lyric-playing',line?.start!=null&&t>=line.start&&t<line.end);});
      const cursor=find('.audio-cursor');if(cursor)cursor.setAttribute('x1',Math.min(1000,t/render.verification.duration_seconds*1000)),cursor.setAttribute('x2',cursor.getAttribute('x1'));
    };
    async function load(){
      try{
        const [report,times]=await Promise.all([get('/analysis'),get('/lyric-timeline')]);if(disposed)return;
        analysis=report;timeline=times;
        const labels={not_analyzed:'尚未分析',preparing:'准备音频',queued:'等待音乐伙伴',working:'通过 Comfy MCP 提交转谱',analyzing:'核对转谱并识别歌词',completed:'分析完成',needs_attention:'分析需要处理',cancelled:'分析已撤回'};
        status.textContent=(labels[report.status]||report.status)+(report.status==='completed'&&report.asr?` · 整段 ${report.asr.measurements.duration.toFixed(1)} 秒已分析`:report.status==='analyzing'?report.stage==='recognizing_lyrics'?' · 本机识别歌词':' · 等待转谱结果':'')+(report.status!=='completed'&&report.progress?` · 已识别至 ${report.progress.through_seconds.toFixed(1)} 秒`:'')+(report.issue?' · '+error({message:report.issue}):'');
        find('.analysis-start').disabled=(report.status!=='not_analyzed'&&!report.can_restart)||!agents?.length;
        find('.analysis-start').textContent=report.can_restart?'重新准备并分析':'分析这次音频';
        find('.analysis-resume').hidden=!report.can_resume;
        drawLines();
        const asr=report.asr;
        if(asr){
          const wave=asr.measurements.waveform,n=wave.length;
          find('.audio-waveform').innerHTML=`<svg viewBox="0 0 1000 90" role="img" aria-label="点击波形定位歌曲"><path d="${wave.map((v,i)=>`M${i/n*1000},${45-v*40}v${v*80}`).join(' ')}" stroke="#7b8a60"/><line class="audio-cursor" x1="0" x2="0" y1="0" y2="90" stroke="#b07738"/></svg>`;
          find('.audio-waveform svg').onclick=e=>{const rect=e.currentTarget.getBoundingClientRect();audio.currentTime=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width))*render.verification.duration_seconds;};
          find('.asr-evidence').textContent=asr.segments.map(s=>`${s.start.toFixed(1)}–${s.end.toFixed(1)}  ${s.text}`).join('\n')+'\n\n模型：'+asr.model+'\n音频 SHA：'+asr.source_audio_sha256;
        }
        clearTimeout(timer);if(['preparing','queued','working','analyzing'].includes(report.status))timer=setTimeout(load,10000);
        if(report.status==='completed'||!find('.song-checks').textContent)await checks();
      }catch(e){if(!disposed)status.textContent=error(e);}
    }
    find('.analysis-start').onclick=async()=>{find('.analysis-start').disabled=true;try{await post('/analysis',{assigned_to:find('.analysis-agent').value});await load();}catch(e){status.textContent=error(e);find('.analysis-start').disabled=false;}};
    find('.analysis-refresh').onclick=()=>load();
    find('.analysis-resume').onclick=async()=>{try{await post('/analysis-resume',{});await load();}catch(e){status.textContent=error(e);}};
    load();
  }
  return {mount};
})();
