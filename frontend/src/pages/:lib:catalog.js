/* File: /lib/catalog.js
   A tiny shared source-of-truth loader + query helpers.
   Works as UMD: window.GradmateCatalog OR ES module default export.
*/
(function (root, factory) {
  if (typeof define === 'function' && define.amd) {
    define([], factory);
  } else if (typeof module === 'object' && module.exports) {
    module.exports = factory();
  } else {
    root.GradmateCatalog = factory();
  }
}(typeof self !== 'undefined' ? self : this, function () {
  const state = {
    url: null,
    data: null,      // the full JSON
    byId: new Map(), // id -> course
    byCode: new Map()// code -> course
  };

  async function load(url) {
    if (state.data && (!url || url === state.url)) return state.data;
    state.url = url || state.url || '/data/catalog.json';
    const res = await fetch(state.url, { cache: 'no-store' });
    if (!res.ok) throw new Error(`Catalog load failed: ${res.status}`);
    const json = await res.json();
    state.data = json;

    // index
    state.byId.clear();
    state.byCode.clear();
    (json.courses || []).forEach(c => {
      if (!c) return;
      if (c.id)   state.byId.set(String(c.id).toUpperCase(), c);
      if (c.code) state.byCode.set(String(c.code).toUpperCase(), c);
    });
    return json;
  }

  function _ensureLoaded() {
    if (!state.data) throw new Error('GradmateCatalog: call load(url) first.');
  }

  function getCourse(key) {
    _ensureLoaded();
    if (!key) return null;
    const k = String(key).toUpperCase();
    return state.byId.get(k) || state.byCode.get(k) || null;
  }

  function search({ q = '', subject, tag, level } = {}) {
    _ensureLoaded();
    const needle = q.trim().toLowerCase();
    return (state.data.courses || []).filter(c => {
      if (subject && String(c.subject).toUpperCase() !== String(subject).toUpperCase()) return false;
      if (tag && !(c.tags || []).map(t => String(t).toUpperCase()).includes(String(tag).toUpperCase())) return false;
      if (level && String(c.level).toLowerCase() !== String(level).toLowerCase()) return false;
      if (!needle) return true;
      const hay = `${c.code} ${c.title} ${c.description || ''}`.toLowerCase();
      return hay.includes(needle);
    });
  }

  function listTags() {
    _ensureLoaded();
    const s = new Set();
    (state.data.courses || []).forEach(c => (c.tags || []).forEach(t => s.add(t)));
    return Array.from(s).sort();
  }

  function getRequisites(courseKey) {
    _ensureLoaded();
    const c = getCourse(courseKey);
    if (!c) return { prerequisites: [], corequisites: [] };
    const prereqIds = (c.prerequisites || []).map(id => getCourse(id) || { id, code: id });
    const coreqIds  = (c.corequisites  || []).map(id => getCourse(id) || { id, code: id });
    return { prerequisites: prereqIds, corequisites: coreqIds };
  }

  function getRequirements() {
    _ensureLoaded();
    return state.data.requirements || { areas: [] };
  }

  // convenience: fill a <datalist> / <select> with course codes
  function hydrateOptions(el, { showTitle = true } = {}) {
    _ensureLoaded();
    if (!el) return;
    el.innerHTML = '';
    (state.data.courses || []).forEach(c => {
      const opt = document.createElement(el.tagName === 'DATALIST' ? 'option' : 'option');
      opt.value = c.code;
      opt.textContent = showTitle && c.title ? `${c.code} — ${c.title}` : c.code;
      el.appendChild(opt);
    });
  }

  return {
    load, getCourse, search, listTags, getRequisites, getRequirements, hydrateOptions,
    // expose raw data if needed
    get data(){ return state.data; }, get url(){ return state.url; }
  };
}));
