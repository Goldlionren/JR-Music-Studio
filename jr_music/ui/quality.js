"use strict";
const QualityPanel=(()=>{
  const drafts=new Map();
  const statusLabels={review:'建议核对',observed:'已有测量',unknown:'证据不足',manual_required:'需试听',intentional:'符合快唱意图'};
  const cycleLabels={preparing:'准备修订',revising:'伙伴修改 / 生成试听',analyzing:'自动复测新音频',awaiting_review:'等待你对比试听',needs_attention:'需要处理',closed:'本轮已记录'};
  function mount({root,render,agents,get,post,esc,error,seek,refresh,onAnalysisUpdate}){
    let disposed=false,timer,report,busy=false,lastAnalysis=null;
    const draft=drafts.get(render.render_id)||{codes:[],preserve:['style'],note:'',values:null};
    draft.reviews ||= {};
    drafts.set(render.render_id,draft);
    root.innerHTML='<h4>听感质量 · 检查与修订闭环</h4><p class="hint">依据 Terry 专业编曲／作词方法：方案执行和听感问题分开检查。每次最多一轮修改与生成，自动复测后由你试听决定；不自动 Accept。</p><div class="quality-policy"></div><div class="quality-observations"></div><div class="quality-submit"></div><div class="quality-cycles"></div><p class="quality-message" role="status"></p>';
    const find=q=>root.querySelector(q),message=t=>{if(!disposed)find('.quality-message').textContent=t;};
    const errors={QUALITY_CYCLE_ACTIVE:'这一版已有未结束的质量修订，请先完成或记录放弃。',CHECK_REPORT_CHANGED:'检查依据已经变化，请更新后重新选择。',QUALITY_POLICY_CHANGED:'检查目标已变化，请刷新后再保存。',QUALITY_LOCK_CONFLICT:'歌词与节奏同时锁定，无法精简密度；请解除其中一项。',QUALITY_INTENT_CONFLICT:'当前选择了说唱／快唱，不将字速作为缺陷。',INVALID_QUALITY_SELECTION:'请先勾选至少一个需要修改的问题。',INVALID_QUALITY_POLICY:'请检查目标范围：进入时间 0–120 秒、字速 0.5–10、连续窗口 5–60 秒。'};
    async function action(fn){
      if(busy)return;busy=true;root.querySelectorAll('button').forEach(b=>b.disabled=true);
      try{await fn();if(!disposed)await load();}catch(e){message(errors[e.message]||error(e));}
      finally{busy=false;if(!disposed)root.querySelectorAll('button').forEach(b=>b.disabled=b.dataset.unavailable==='true');}
    }
    function setup(){
      if(find('.quality-submit').children.length)return;
      draft.values ||= structuredClone(report.policy.values);
      const v=draft.values;
      find('.quality-policy').innerHTML='<details><summary>检查目标与阈值</summary><p class="hint">这些数字是可调整的检查提示，不是 Terry 教材的通用及格线。“首句进入”不能证明前面没有哼唱。</p>'+
        '<label>演唱意图<select data-policy="delivery"><option value="melodic">旋律演唱，关注密集吐字</option><option value="rap_or_fast">允许说唱 / 快唱</option></select></label>'+
        [['intro_min','首句最早进入（秒）',0,120],['intro_max','首句最晚进入（秒）',0,120],['max_han_per_second','字速提示（汉字/秒）',0.5,10],['dense_run_seconds','持续密集窗口（秒）',5,60],['short_gap_seconds','合并相邻句的间隙（秒）',0,3]].map(([k,label,min,max])=>'<label>'+label+'<input data-policy="'+k+'" type="number" min="'+min+'" max="'+max+'" step="0.1" value="'+v[k]+'"></label>').join('')+
        '<button class="quality-save-policy">保存检查目标</button></details>';
      find('[data-policy="delivery"]').value=v.delivery;
      root.querySelectorAll('[data-policy]').forEach(input=>input.oninput=()=>{draft.values[input.dataset.policy]=input.dataset.policy==='delivery'?input.value:Number(input.value);});
      find('.quality-save-policy').onclick=()=>action(async()=>{await post('/quality-policy',{values:draft.values,expected_generation:report.policy.generation});message('检查目标已保存；既有修订继续使用提交时冻结的目标。');});
      find('.quality-submit').innerHTML='<label>执行伙伴<select class="quality-agent">'+(agents||[]).map(a=>'<option value="'+esc(a)+'">'+(a==='yinyue'?'银月':'小舞')+'</option>').join('')+'</select></label>'+
        '<button class="quality-analyze">只检查歌词与波形</button><small class="hint">本机识别，不需要转谱，也不提交 GPU 生成。</small>'+
        '<fieldset><legend>这轮必须保留</legend>'+Object.entries({lyrics:'歌词',melody:'音高',rhythm:'节奏',chords:'和弦',structure:'段落结构',tempo:'速度',key:'调性',style:'整体风格'}).map(([k,label])=>'<label><input type="checkbox" data-quality-lock="'+k+'" '+(draft.preserve.includes(k)?'checked':'')+'>'+label+'</label>').join('')+'</fieldset>'+
        '<label>试听意见或补充要求<textarea class="quality-note" rows="3" maxlength="800" placeholder="例如：副歌持续像说唱，希望减掉重复表达，保留意象；开头希望有器乐前奏。"></textarea></label>'+
        '<button class="quality-revise primary" '+(!agents?.length?'disabled data-unavailable="true"':'')+'>按勾选问题修改一次并自动复测 ↗</button><p class="hint">勾选表示你确认要修改这些问题。新候选默认使用该伙伴当前 MCP 默认值，任务提交后固定；原版本保留。</p>';
      find('.quality-note').value=draft.note;
      find('.quality-note').oninput=e=>{draft.note=e.target.value;};
      root.querySelectorAll('[data-quality-lock]').forEach(input=>input.onchange=()=>{draft.preserve=Array.from(root.querySelectorAll('[data-quality-lock]:checked'),x=>x.dataset.qualityLock);});
      find('.quality-analyze').onclick=()=>action(async()=>{await post('/quality-analyze',{assigned_to:find('.quality-agent').value||'yinyue'});message('已开始歌词与波形分析。');});
      find('.quality-revise').onclick=()=>action(async()=>{
        if(JSON.stringify(draft.values)!==JSON.stringify(report.policy.values))throw new Error('请先保存检查目标，再提交修订。');
        await post('/quality-revise',{expected_report_sha256:report.report_sha256,codes:draft.codes,assigned_to:find('.quality-agent').value,preserve:draft.preserve,note:draft.note});
        message('已创建一轮质量修订。完成后自动分析，结果在下方对比。');await refresh();
      });
    }
    function drawChecks(){
      const analysisLabels={not_analyzed:'尚未分析',preparing:'准备中',queued:'排队中',working:'处理中',analyzing:'分析中',completed:'分析完成',needs_attention:'需要处理',cancelled:'已取消'};
      find('.quality-observations').innerHTML='<p class="hint">分析状态：'+esc(analysisLabels[report.analysis_status]||report.analysis_status)+' · 输入谱的密度估计与实唱时间线分别保留，不合并为总分。</p>'+
        report.checks.map(c=>'<article class="song-check '+c.status+'"><label><input type="checkbox" data-quality-code="'+c.code+'" '+(draft.codes.includes(c.code)?'checked':'')+' '+(c.status==='intentional'?'disabled':'')+'><b>'+esc(c.label)+'</b> · '+statusLabels[c.status]+'</label><p>'+esc(c.evidence)+'</p>'+
        (c.start!=null?'<button data-quality-seek="'+c.start+'">▶ 定位 '+c.start.toFixed(1)+' 秒</button>':'')+
        '<details><summary>方法依据与修改方向</summary><p>'+esc(c.instruction)+'</p>'+c.sources.map(s=>'<a target="_blank" rel="noreferrer" href="'+esc(s.url)+'">'+esc(s.skill+' '+s.section)+'</a>').join(' · ')+'</details></article>').join('');
      root.querySelectorAll('[data-quality-code]').forEach(c=>c.onchange=()=>{draft.codes=Array.from(root.querySelectorAll('[data-quality-code]:checked'),x=>x.dataset.qualityCode);});
      root.querySelectorAll('[data-quality-seek]').forEach(b=>b.onclick=()=>seek(+b.dataset.qualitySeek));
    }
    function drawCycles(cycles){
      const number=value=>value==null?'待测':typeof value==='boolean'?(value?'有风险':'未触发'):typeof value==='number'?value.toFixed(2):String(value);
      find('.quality-cycles').innerHTML=cycles.length?'<h4>修订前后复核</h4>'+cycles.map(c=>'<article class="song-check"><b>'+cycleLabels[c.state]+'</b> <small>'+esc(c.cycle_id.slice(-8))+'</small><p>源音频 '+esc(c.source_render_id.slice(-8))+(c.result_render_id?' → 新音频 '+esc(c.result_render_id.slice(-8)):'')+'</p>'+
        (c.issue?'<p>'+esc(error({message:c.issue}))+'</p>':'')+
        (c.comparison?'<table class="quality-comparison"><thead><tr><th>指标</th><th>修订前</th><th>修订后</th><th>解释</th></tr></thead><tbody>'+c.comparison.rows.map(r=>'<tr><td>'+esc(r.label)+'</td><td>'+number(r.before)+'</td><td>'+number(r.after)+'</td><td>'+esc(r.conclusion)+'</td></tr>').join('')+'</tbody></table><p class="hint">'+esc(c.comparison.limitation)+' 请在候选区比较两个版本，指标下降不等于音乐更好。</p>':'')+
        (c.state==='awaiting_review'?'<label>试听结论<select data-verdict="'+c.cycle_id+'"><option value="unclear">尚不能确定改善</option><option value="improved">试听后有改善</option><option value="worse">试听后更差</option></select></label><textarea data-review-note="'+c.cycle_id+'" rows="2" placeholder="哪些问题改善？是否损失意境、旋律或声线？"></textarea><button data-quality-review="'+c.cycle_id+'">记录结论，结束本轮</button>':'')+
        (c.state==='needs_attention'?'<button data-quality-refresh="'+c.cycle_id+'">核对本轮进度</button><button data-quality-abandon="'+c.cycle_id+'">记录放弃本轮</button>':'')+
        (c.state==='closed'?'<p>试听结论：'+esc({improved:'有改善',worse:'更差',unclear:'尚不确定',abandoned:'已放弃'}[c.verdict])+'；'+esc(c.review_note||'')+'。采用候选仍需单独 Accept。</p>':'')+'</article>').join(''):'';
      root.querySelectorAll('[data-quality-review]').forEach(b=>b.onclick=()=>action(async()=>{const id=b.dataset.qualityReview;await post('/quality-review',{cycle_id:id,verdict:find('[data-verdict="'+id+'"]').value,note:find('[data-review-note="'+id+'"]').value});message('结论已记录，没有自动采用候选。');}));
      root.querySelectorAll('[data-review-note]').forEach(input=>{const id=input.dataset.reviewNote;input.value=draft.reviews[id]?.note||'';input.oninput=()=>{draft.reviews[id]={...draft.reviews[id],note:input.value};};});
      root.querySelectorAll('[data-verdict]').forEach(input=>{const id=input.dataset.verdict;input.value=draft.reviews[id]?.verdict||'unclear';input.onchange=()=>{draft.reviews[id]={...draft.reviews[id],verdict:input.value};};});
      root.querySelectorAll('[data-quality-refresh]').forEach(b=>b.onclick=()=>action(()=>post('/quality-refresh',{cycle_id:b.dataset.qualityRefresh})));
      root.querySelectorAll('[data-quality-abandon]').forEach(b=>b.onclick=()=>action(()=>post('/quality-review',{cycle_id:b.dataset.qualityAbandon,verdict:'abandoned',note:'制作人放弃本轮；历史任务与证据保留。'})));
    }
    async function load(){
      try{
        const result=await get('/quality');if(disposed)return;
        report=result.report;setup();drawChecks();
        const analysisKey=report.analysis_status+':'+report.asr_sha256;
        if(lastAnalysis!==null&&lastAnalysis!==analysisKey)onAnalysisUpdate?.();
        lastAnalysis=analysisKey;
        // Do not wipe a producer's unsaved listening review while polling.
        if(!root.querySelector('[data-review-note]:focus')&&!root.querySelector('[data-verdict]:focus'))drawCycles(result.cycles);
        clearTimeout(timer);
        timer=setTimeout(load,result.cycles.some(c=>['preparing','revising','analyzing'].includes(c.state))||['preparing','queued','working','analyzing'].includes(report.analysis_status)?10000:30000);
      }catch(e){message(errors[e.message]||error(e));}
    }
    load();
    return ()=>{disposed=true;clearTimeout(timer);};
  }
  return {mount};
})();
