"use strict";
const ManualEditor=(()=>{
  const drafts=new Map();
  function mount({root,revision:r,esc,preview,save}) {
    const data=r.manual_editor;
    if(!data){root.textContent=r.snapshot.generation?'先以生成谱创建编辑候选，或修改 style／歌词后重新生成。':'编辑数据未加载，请刷新。';return;}
    let mode='sections',parts=structuredClone(data.sections),notes=structuredClone(data.notes),selectedBar=r.score.bars[0]?.bar_id;
    let words=r.snapshot.brief.lyrics,dragIndex=null;
    const draftKey=r.revision.snapshot_sha256;
    let dirty=drafts.has(draftKey);
    if(drafts.has(draftKey))({mode,parts,notes,selectedBar,words}=structuredClone(drafts.get(draftKey)));
    root.innerHTML=`<p class="hint">直接修改创作输入，另存新候选；原音频保留。先预览检查，再保存。钢琴卷帘表示谱面节拍，不是实唱时间。</p>
      <div class="editor-tabs"><button data-mode="sections">曲式与段落</button><button data-mode="lyrics">完整歌词</button><button data-mode="notes">音符与和弦 / 钢琴卷帘</button></div>
      <div class="editor-body"></div><div class="review-controls"><button class="editor-preview">预览并检查</button><button class="editor-save" disabled>另存修改版本</button><button class="editor-discard">放弃未保存修改</button></div><div class="editor-result" role="status"></div><div class="editor-sheet"></div>`;
    const body=root.querySelector('.editor-body'),status=root.querySelector('.editor-result'),saveButton=root.querySelector('.editor-save');
    let generation=0;
    const invalidate=()=>{dirty=true;generation++;drafts.set(draftKey,structuredClone({mode,parts,notes,selectedBar,words}));saveButton.disabled=true;status.textContent='有未保存的修改，请预览检查。';};
    const titles={intro:'前奏',verse:'主歌',pre_chorus:'预副歌',chorus:'副歌',bridge:'桥段',outro:'尾奏'};
    const operation=()=>mode==='lyrics'?{type:'lyrics',lyrics:words}:mode==='sections'?{type:'sections',sections:parts.map(p=>({kind:p.kind,voice:p.voice,abc_body:p.abc_body,lyrics:p.lyrics}))}:{type:'notes',edits:notes.filter((n,i)=>n.token!==data.notes[i].token||n.chord!==data.notes[i].chord).map(({event_id,token,chord})=>({event_id,token,chord}))};
    function draw() {
      saveButton.disabled=true;status.textContent='';root.querySelector('.editor-sheet').replaceChildren();
      if(mode==='lyrics') {
        body.innerHTML=`<label>完整歌词<textarea class="full-lyrics" rows="14">${esc(words)}</textarea></label>`;
        body.querySelector('textarea').oninput=e=>{words=e.target.value;invalidate();};return;
      }
      if(mode==='sections') {
        if(!data.structure_editable){body.innerHTML='<p class="hint">谱面和歌词段落暂不能一一对应。请先在完整歌词中补齐段落标签，或使用整曲调整。</p>';return;}
        body.innerHTML=parts.map((p,i)=>`<article class="section-edit-card" draggable="true" data-index="${i}"><div class="review-controls"><b>${i+1}</b><label>段落类型<select data-field="kind">${Object.entries(titles).map(([k,v])=>`<option value="${k}" ${p.kind===k?'selected':''}>${v}</option>`).join('')}</select></label><button data-action="up" ${i===0?'disabled':''}>上移</button><button data-action="down" ${i===parts.length-1?'disabled':''}>下移</button><button data-action="copy">复制</button><button data-action="delete">删除</button></div>
          <label>歌词（留空为纯音乐段）<textarea data-field="lyrics" rows="3">${esc(p.lyrics)}</textarea></label><label>本段乐谱 · 可增删小节<textarea data-field="abc_body" rows="3" spellcheck="false">${esc(p.abc_body)}</textarea></label><button data-action="extend">增加一小节休止</button><p class="hint">可拖动此段重新排序；多声部请在乐谱中分别调整各声部长度。</p></article>`).join('')+'<button class="add-section">新增段落</button>';
        body.querySelectorAll('.section-edit-card').forEach(card=>{
          const i=Number(card.dataset.index);
          card.querySelectorAll('[data-field]').forEach(el=>el.oninput=()=>{parts[i][el.dataset.field]=el.value;invalidate();});
          card.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>{
            const action=b.dataset.action;
            if(action==='up')[parts[i-1],parts[i]]=[parts[i],parts[i-1]];
            if(action==='down')[parts[i+1],parts[i]]=[parts[i],parts[i+1]];
            if(action==='copy')parts.splice(i+1,0,structuredClone(parts[i]));
            if(action==='delete')parts.splice(i,1);
            if(action==='extend')parts[i].abc_body=parts[i].abc_body.trimEnd()+'\nZ|\n';
            draw();invalidate();
          });
          card.ondragstart=e=>{if(e.target.closest('textarea,input,select')){e.preventDefault();return;}dragIndex=i;};
          card.ondragover=e=>e.preventDefault();
          card.ondrop=e=>{e.preventDefault();if(dragIndex!==null&&dragIndex!==i){parts.splice(i,0,parts.splice(dragIndex,1)[0]);dragIndex=null;draw();invalidate();}};
        });
        body.querySelector('.add-section').onclick=()=>{parts.push({kind:'verse',voice:r.score.voices[0]?.voice_id||'Vocal',abc_body:'Z4|\n',lyrics:''});draw();invalidate();};
        return;
      }
      const displayed=notes.filter(n=>n.bar_id===selectedBar);
      body.innerHTML=`<label>编辑哪个小节<select class="note-bar">${r.score.bars.map(b=>`<option value="${esc(b.bar_id)}" ${b.bar_id===selectedBar?'selected':''}>${esc(b.voice_id)} · ${b.ordinal}</option>`).join('')}</select></label><div class="piano-roll"></div><p class="hint">点击卷帘中的音符定位输入框。音符可写 C、^F、_B、c；数字为默认音长倍数，如 C2、D/2、z2。改变音长时同时调整本小节其他音符，确保拍数正确。</p>
        <table class="note-table"><thead><tr><th>音符</th><th>音高与时值</th><th>和弦</th></tr></thead><tbody>${displayed.map((n,i)=>`<tr><td>${i+1}</td><td><input aria-label="第 ${i+1} 个音符" data-note="${esc(n.event_id)}" value="${esc(n.token)}" maxlength="24"></td><td><input aria-label="第 ${i+1} 个音符的和弦" data-chord="${esc(n.event_id)}" value="${esc(n.chord)}" maxlength="80"></td></tr>`).join('')}</tbody></table>`;
      body.querySelector('.note-bar').onchange=e=>{selectedBar=e.target.value;draw();};
      body.querySelectorAll('[data-note],[data-chord]').forEach(input=>input.oninput=()=>{
        const note=notes.find(n=>n.event_id===(input.dataset.note||input.dataset.chord));
        note[input.dataset.note?'token':'chord']=input.value;invalidate();
      });
      const pitches=displayed.filter(n=>n.midi!==null).map(n=>n.midi),low=Math.min(48,...pitches)-1,high=Math.max(72,...pitches)+1;
      let total=displayed.reduce((s,n)=>s+n.duration.numerator/n.duration.denominator,0),position=0;
      const rects=displayed.map((n,i)=>{const length=n.duration.numerator/n.duration.denominator,x=40+position/total*650;position+=length;
        return n.midi==null?'':`<rect data-piano="${i}" tabindex="0" role="button" aria-label="选择第 ${i+1} 个音符" x="${x}" y="${10+(high-n.midi)/(high-low)*165}" width="${Math.max(4,length/total*650-2)}" height="6" fill="#607644"/>`;}).join('');
      body.querySelector('.piano-roll').innerHTML=`<svg viewBox="0 0 720 190" aria-label="当前小节钢琴卷帘"><rect width="720" height="190" fill="#f0f1e9"/>${Array.from({length:high-low+1},(_,i)=>`<line x1="35" x2="705" y1="${10+i/(high-low)*165}" y2="${10+i/(high-low)*165}" stroke="#d9ddcf"/>`).join('')}${rects}</svg>`;
      body.querySelectorAll('[data-piano]').forEach(el=>{const select=()=>body.querySelectorAll('[data-note]')[Number(el.dataset.piano)].focus();el.onclick=select;el.onkeydown=e=>{if(e.key==='Enter')select();};});
    }
    root.querySelectorAll('[data-mode]').forEach(b=>b.onclick=()=>{if(dirty&&mode!==b.dataset.mode){status.textContent='请先保存当前页签的修改，或放弃未保存修改，再切换编辑方式。';return;}mode=b.dataset.mode;generation++;draw();});
    root.querySelector('.editor-discard').onclick=()=>{drafts.delete(draftKey);mount({root,revision:r,esc,preview,save});};
    root.querySelector('.editor-preview').onclick=async()=>{
      const ticket=generation;
      try{const result=await preview(operation());if(ticket!==generation)return;status.textContent=result.can_save?`检查通过：${result.sections} 段、${result.bars} 小节。可另存。`:'请修正：'+result.diagnostics.map(d=>d.code).join('、');
        saveButton.disabled=!result.can_save;ABCJS.renderAbc(root.querySelector('.editor-sheet'),result.abc,{responsive:'resize'});
      }catch(e){status.textContent=e.message;saveButton.disabled=true;}
    };
    saveButton.onclick=async()=>{saveButton.disabled=true;try{await save(operation());drafts.delete(draftKey);}catch(e){status.textContent=e.message;}};
    draw();
  }
  return {mount};
})();
