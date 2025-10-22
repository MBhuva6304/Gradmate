/* courses.js (fixed)
   - Instant fallback render, then hydrate from catalog.json (/data/catalog.json)
   - Filters: Level (000–599), Credits (1–4), Fulfillment tags
   - Search includes title/code/subject and syncs with ?q= in URL
   - Drawer shows details + requisites + where it counts
*/

// ================= Fallback data (safe to edit) =================
const FALLBACK_DATA = {
  courses: [
    { id:"MATH-091B", subject:"MATH", number:"091B", code:"MATH 091B", title:"Support Course for GE Mathematics — Business", units:1, level:"lower", description:"Credit/No Credit pre-baccalaureate support for the Business Math course (MATH 103); just-in-time remediation aligned to lecture meetings. May be repeated once for credit.", prerequisites:[], corequisites:["MATH-103"], tags:["SUPPORT","MATH-LOWER"], restrictions:{text:""}, repeatable:false },
    { id:"MATH-091C", subject:"MATH", number:"091C", code:"MATH 091C", title:"Support Course for GE Mathematics — STEM", units:1, level:"lower", description:"Credit/No Credit support for the Calculus sequence; just-in-time remediation aligned to MATH 102 lecture meetings. May be repeated once for credit.", prerequisites:[], corequisites:["MATH-102","MATH-102L"], tags:["SUPPORT","MATH-LOWER"], restrictions:{text:""}, repeatable:false },
    { id:"MATH-091S", subject:"MATH", number:"091S", code:"MATH 091S", title:"Support Course for GE Mathematics — Statistics", units:1, level:"lower", description:"Credit/No Credit support for Introductory Statistics aligned to MATH 141 lecture meetings. May be repeated once for credit.", prerequisites:[], corequisites:["MATH-141","MATH-141L"], tags:["SUPPORT","MATH-LOWER"], restrictions:{text:""}, repeatable:false },
    { id:"MATH-102", subject:"MATH", number:"102",  code:"MATH 102",  title:"Pre-Calculus I", units:3, level:"lower", description:"Preparation for calculus: functions, rational expressions, algebraic skills for science/engineering/math majors.", prerequisites:[], corequisites:[], tags:["PRECALC","MATH-LOWER"], restrictions:{text:"Not open to some majors may apply by campus policy (confirm locally)."}, repeatable:false },
    { id:"MATH-102L", subject:"MATH", number:"102L", code:"MATH 102L", title:"Pre-Calculus I Lab", units:1, level:"lower", description:"Laboratory aligned to MATH 102; reinforces problem solving and lecture concepts.", prerequisites:[], corequisites:["MATH-102"], tags:["LAB","MATH-LOWER"], restrictions:{text:""}, repeatable:false, pairedWith:"MATH-102" },
    { id:"MATH-103", subject:"MATH", number:"103",  code:"MATH 103",  title:"Mathematical Methods for Business", units:3, level:"lower", description:"Algebra and calculus concepts for business applications; matrices, derivatives, quantitative reasoning.", prerequisites:[], corequisites:[], tags:["GE-QR","GE-MATH","MATH-LOWER"], restrictions:{text:""}, repeatable:false },
    { id:"MATH-141", subject:"MATH", number:"141",  code:"MATH 141",  title:"Introductory Statistics", units:3, level:"lower", description:"Introduction to statistical reasoning and methods.", prerequisites:[], corequisites:[], tags:["GE-QR","MATH-LOWER"], restrictions:{text:""}, repeatable:false },
    { id:"MATH-141L", subject:"MATH", number:"141L", code:"MATH 141L", title:"Introductory Statistics Lab", units:1, level:"lower", description:"Laboratory activities to reinforce MATH 141 concepts. Credit/No Credit.", prerequisites:[], corequisites:["MATH-141"], tags:["LAB","MATH-LOWER"], restrictions:{text:""}, repeatable:false, pairedWith:"MATH-141" }
  ],
  prereqEdges:{ edges:[], coreq:[
    {course:"MATH-091B",of:["MATH-103"]},
    {course:"MATH-091C",of:["MATH-102","MATH-102L"]},
    {course:"MATH-102L",of:["MATH-102"]},
    {course:"MATH-091S",of:["MATH-141","MATH-141L"]},
    {course:"MATH-141L",of:["MATH-141"]}
  ], equivalents:[] },
  requirements:{
    areas:[
      { id:"GE-QR", name:"General Education – Quantitative Reasoning", targetUnits:3, eligible:{byTags:["GE-QR","GE-MATH"]}, minGrade:"C-", notes:"Choose one 3-unit QR/GE math course (e.g., MATH 103 or MATH 141)." },
      { id:"MATH-PRECALC", name:"Precalculus Preparation", targetUnits:3, eligible:{courseIds:["MATH-102"]}, notes:"For CS calculus pathway; campuses may vary." },
      { id:"LAB-ENRICH", name:"Math Lab Enrichment (optional)", targetUnits:0, eligible:{byTags:["LAB"]}, notes:"Lab/support courses may not count toward degree units; include for planning visibility." }
    ]
  }
};

// ================= Implementation (safe-guarded) =================
(function(){
  // DOM gets queried AFTER DOMContentLoaded to avoid nulls
  document.addEventListener('DOMContentLoaded', start);

  let DATA = FALLBACK_DATA;

  function start(){
    // ---- DOM refs (optional; guarded) ----
    const grid          = document.getElementById('grid');
    const search        = document.getElementById('search');
    const resultCount   = document.getElementById('resultCount');
    const drawer        = document.getElementById('drawer');
    const dTitle        = document.getElementById('d_title');
    const dBody         = document.getElementById('d_body');
    const levelFilter   = document.getElementById('levelFilter');
    const creditsFilter = document.getElementById('creditsFilter');
    const fulfillFilter = document.getElementById('fulfillFilter');

    // If no grid/search exists on this page, quietly exit (lets other pages still reuse this file)
    if (!grid) return;

    // ---------- URL helpers ----------
    const getQueryParam = (name)=>{
      const u = new URL(window.location.href);
      return u.searchParams.get(name) || '';
    };
    const setQueryParam = (name, value)=>{
      const u = new URL(window.location.href);
      if (value) u.searchParams.set(name, value); else u.searchParams.delete(name);
      history.replaceState(null, '', u.toString());
    };

    // ---------- Utils ----------
    const byId       = id => DATA.courses.find(c => String(c.id) === String(id));
    const fmtUnits   = u => `${u} unit${u===1?'':'s'}`;
    const eligibility= c => (Array.isArray(c.prerequisites) && c.prerequisites.length ? 'blocked' : 'ok');
    const subjectNice= s => s ? (s[0] + s.slice(1).toLowerCase()) : '';
    const numberValue= c => parseInt(String(c.number||'').replace(/\D/g,''),10) || 0;

    // ---------- Render ----------
    function render(filter = '') {
      const q   = (filter || '').trim().toLowerCase();
      const lvl = levelFilter?.value || '';
      const cr  = creditsFilter?.value || '';
      const ful = fulfillFilter?.value || '';

      const items = (DATA.courses || []).filter(c => {
        const searchHit =
          (c.title||'').toLowerCase().includes(q) ||
          (c.code||'').toLowerCase().includes(q)  ||
          (c.subject||'').toLowerCase().includes(q);

        // level range "000-599"
        const n = numberValue(c);
        const inLevel = !lvl || (()=>{ const [a,b]=lvl.split('-').map(Number); return n>=a && n<=b; })();

        // credits
        const inCredits = !cr || (Number(c.units) === Number(cr));

        // fulfillment tag (substring match)
        const lc = (ful || '').toLowerCase();
        const tags = (c.tags || []).map(t=>String(t).toLowerCase());
        const inFulfill = !ful || tags.some(t => t.includes(lc));

        return searchHit && inLevel && inCredits && inFulfill;
      });

      if (resultCount) resultCount.textContent = `${items.length} result${items.length===1?'':'s'}`;
      grid.innerHTML = '';

      items.forEach(c => {
        const badgeClass = eligibility(c) === 'ok' ? 'ok' : 'block';
        const badgeText  = badgeClass === 'ok' ? 'Eligible' : 'Blocked';
        const niceCode   = `${subjectNice(c.subject)} ${String(c.number||'').toUpperCase().replace(/\s+/g,'')} (${c.units})`;

        const el = document.createElement('article');
        el.className = 'card';
        el.innerHTML = `
          <div class="row">
            <div class="code">${niceCode}</div>
            <span class="badge ${badgeClass}" style="
              margin-left:auto;font-size:12px;padding:3px 8px;border-radius:999px;
              border:1px solid ${badgeClass==='ok'?'rgba(22,163,74,.3)':'rgba(217,119,6,.3)'};
              background:${badgeClass==='ok'?'rgba(22,163,74,.12)':'rgba(217,119,6,.12)'};
              color:${badgeClass==='ok'?'#065F2A':'#7C2D12'};">
              ${badgeText}
            </span>
          </div>
          <div class="title">${c.title||''}</div>
          <div class="tags">${(c.tags||[]).map(t=>`<span class="tag">${t}</span>`).join('')}</div>
          <div class="row">
            <div class="spacer"></div>
            <button class="btn btn-ghost" data-action="details" data-id="${c.id}">Details</button>
          </div>
        `;
        el.querySelector('[data-action="details"]').addEventListener('click',()=>openDetails(c.id));
        grid.appendChild(el);
      });
    }

    // ---------- Drawer ----------
    function openDetails(id){
      if (!drawer || !dTitle || !dBody) return;
      const c = byId(id);
      if(!c) return;

      // compute “counts toward”
      const areas = DATA?.requirements?.areas || [];
      const applies = [];
      for (const r of areas){
        const byIds  = r?.eligible?.courseIds || [];
        const byTags = r?.eligible?.byTags    || [];
        if (byIds.includes(c.id)) applies.push(r.name);
        if (byTags.length && (c.tags||[]).some(t => byTags.includes(t))) applies.push(r.name);
      }

      dTitle.textContent = `${c.title || c.code || 'Course'}`;
      dBody.innerHTML = `
        <div class="muted">${subjectNice(c.subject)} ${String(c.number||'').toUpperCase()} · ${fmtUnits(c.units)} · ${(c.level||'').replace(/^\w/,s=>s.toUpperCase())}</div>
        <h3>Description</h3>
        <p>${c.description || '—'}</p>

        <h3>Counts toward</h3>
        <div class="tags">${applies.length?applies.map(a=>`<span class="tag">${a}</span>`).join(''): '<span class="muted">—</span>'}</div>

        <h3>Requisites</h3>
        <div class="kv">
          <div class="muted">Prerequisites</div>
          <div>${(c.prerequisites && c.prerequisites.length)? c.prerequisites.join(', ') : 'None'}</div>
          <div class="muted">Corequisites</div>
          <div>${(c.corequisites && c.corequisites.length)? c.corequisites.join(', ') : 'None'}</div>
        </div>
      `;
      drawer.classList.add('open');
      drawer.setAttribute('aria-hidden','false');
    }
    function closeDrawer(){
      if (!drawer) return;
      drawer.classList.remove('open');
      drawer.setAttribute('aria-hidden','true');
    }
    document.addEventListener('keydown',(e)=>{ if(e.key==='Escape') closeDrawer(); });
    if (drawer) drawer.addEventListener('click', (e)=>{ if(e.target===drawer) closeDrawer(); });

    // ---------- Wire filters + search (with debounce) ----------
    const debounce = (fn,ms=120)=>{let t;return (...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms);}};

    [levelFilter, creditsFilter, fulfillFilter].forEach(el=>{
      el && el.addEventListener('change', ()=>render(search?.value || ''));
    });

    if (search){
      search.addEventListener('input', debounce(e => {
        const val = (e.target.value||'').trim();
        setQueryParam('q', val);
        render(val);
      }, 110));
      const initialQ = getQueryParam('q');
      if (initialQ) search.value = initialQ;
    }

    // ---------- First paint with fallback ----------
    render(search?.value || '');

    // ---------- Hydrate from catalog.json (/data/catalog.json) ----------
    (async function hydrate(){
      // Build coreq map into course objects if provided separately
      const mergeCoreqs = (data)=>{
        try{
          const map = new Map((data.courses||[]).map(c=>[String(c.id), c]));
          const coreqEdges = data?.prereqEdges?.coreq || [];
          coreqEdges.forEach(edge=>{
            const child = map.get(String(edge.course));
            if (!child) return;
            child.corequisites = Array.from(new Set([...(child.corequisites||[]), ...(edge.of||[])]));
          });
        }catch(_){}
        return data;
      };

      const tryFetch = async (p)=>{
        const r = await fetch(p, { cache:'no-store' });
        if (!r.ok) throw new Error(r.statusText || r.status);
        const j = await r.json();
        // accept {courses:[]} or a plain array
        const data = Array.isArray(j) ? { courses:j } : j;
        if (!data?.courses?.length) throw new Error('No courses in catalog');
        return mergeCoreqs(data);
      };

      try{
        // Try local dir first, then /data/, then root
        const paths = ['catalog.json','/data/catalog.json','/catalog.json'];
        let loaded = null, lastErr = null;
        for (const p of paths){
          try{ loaded = await tryFetch(p); break; }
          catch(e){ lastErr = e; }
        }
        if (!loaded) throw lastErr || new Error('Unable to load catalog.json');
        DATA = loaded;
        render(search?.value || '');
      }catch(err){
        // Keep fallback silently; if needed, log:
        console.warn('[courses.js] Using fallback data. Reason:', err?.message||err);
      }
    })();
  }
})();
