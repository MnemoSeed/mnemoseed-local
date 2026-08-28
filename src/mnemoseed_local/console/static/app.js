/* MnemoSeed Local Console — vanilla JS, zero-build.
   Overview: /healthz, /health, /api/v1/observability, /api/v1/config (or /config)
   Atlas: POST /memory/atlas (limit≤500, positions pca/unavailable), 3D + 2.5D fallback + List + Drawer
   Copy verbatim from docs/zh/design/ux/08-memory-atlas-spec.md §17.
*/

const DAEMON_URL = "http://localhost:7788";
const DAEMON_MSG = `Daemon unreachable at ${DAEMON_URL} — run mnemoseed-local up`;

// ---------- utils ----------
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => Array.from(r.querySelectorAll(s));
const esc = (s) => String(s).replace(/[&<>"']/g,(c)=>({"&":"&amp;","<":"&lt;","&gt;":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const fmtRel = (isoOrEpoch) => {
  if (!isoOrEpoch && isoOrEpoch !== 0) return "—";
  let d;
  if (typeof isoOrEpoch === "number") d = new Date(isoOrEpoch * 1000);
  else d = new Date(isoOrEpoch);
  if (isNaN(d)) return String(isoOrEpoch);
  const diff = Date.now() - d.getTime();
  const s = Math.floor(diff/1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s/60); if (m < 60) return `${m}m ago`;
  const h = Math.floor(m/60); if (h < 24) return `${h}h ago`;
  const dd = Math.floor(h/24); if (dd < 30) return `${dd}d ago`;
  return d.toLocaleDateString();
};
const fmtISO = (v) => {
  if (!v) return "—";
  if (typeof v === "number") return new Date(v*1000).toISOString();
  try { return new Date(v).toISOString(); } catch { return String(v); }
};
const fmtNum = (n) => { try { return new Intl.NumberFormat().format(n); } catch { return String(n); } };
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

// deterministic hash -> float in [0,1), used for fallback positions
function hash01(str){
  let h = 2166136261;
  for(let i=0;i<str.length;i++){ h ^= str.charCodeAt(i); h = Math.imul(h, 16777619); }
  return ((h >>> 0) % 1000000)/1000000;
}
function hashPos(id){
  // return [x in [-1,1], y in [0,1], z in [0,1]]
  const a = hash01(id+"|x");
  const b = hash01(id+"|y");
  const c = hash01(id+"|z");
  return [ a*2-1, b, c ];
}
function decayBand(w){
  if (w==null) return "unknown";
  if (w >= 0.4) return "healthy";
  if (w >= 0.15) return "rescue";
  return "fading";
}
function decayLabel(w){
  const b = decayBand(w);
  if (b==="healthy") return "Healthy";
  if (b==="rescue") return "Rescue";
  if (b==="fading") return "Fading";
  return "—";
}
let liveTimer = null;
function a11yLive(msg){
  const el = $("#a11y-live");
  if(!el) return;
  el.textContent = msg;
  clearTimeout(liveTimer);
  liveTimer = setTimeout(()=>{ el.textContent=""; }, 2000);
}

// fetch helpers — same-origin, no auth
async function fetchJson(path, opts){
  const url = path.startsWith("http") ? path : path;
  const res = await fetch(url, opts);
  if(!res.ok){
    const txt = await res.text().catch(()=>"");
    let detail = txt;
    try{ const j = JSON.parse(txt); detail = j.detail || j.error || txt; } catch{}
    const err = new Error(detail || `${res.status} ${res.statusText}`);
    err.status = res.status;
    err.body = txt;
    throw err;
  }
  return res.json();
}
async function tryFetchJson(paths, opts){
  let last;
  for(const p of paths){
    try{ return await fetchJson(p, opts); } catch(e){ last=e; if(e.status===404) continue; throw e; }
  }
  throw last;
}

// ---------- routing ----------
function currentRoute(){
  const h = location.hash || "#/overview";
  if(h.startsWith("#/memory/atlas")) return "atlas";
  if(h.startsWith("#/config")) return "config";
  if(h.startsWith("#/profiles")) return "profiles";
  if(h.startsWith("#/dream")) return "dream";
  return "overview";
}
function syncRoute(){
  const r = currentRoute();
  $$(".tab").forEach(a=>{
    const is = a.dataset.route===r;
    a.classList.toggle("is-active", is);
    if(is) a.setAttribute("aria-current","page"); else a.removeAttribute("aria-current");
  });
  $("#view-overview").hidden = r!=="overview";
  $("#view-atlas").hidden = r!=="atlas";
  $("#view-config").hidden = r!=="config";
  $("#view-profiles").hidden = r!=="profiles";
  $("#view-dream").hidden = r!=="dream";
  if(r==="atlas") ensureAtlasLoaded();
  if(r==="config") loadConfigPage();
  if(r==="profiles") loadProfilesPage();
  if(r==="dream") loadDreamPage();
}
window.addEventListener("hashchange", syncRoute);

// ---------- Overview ----------
let daemonReachable = true;
async function loadOverview(){
  const dot = $("#daemon-dot"), dotLabel = $("#daemon-dot-label");
  const banner = $("#daemon-unreachable");
  const statusBody = $("#status-body"), statusMeta = $("#status-meta");
  const obsBody = $("#observability-body"), obsFoot = $("#observability-foot");
  const healthBody = $("#health-body");
  const cfgBody = $("#overview-config-body"), cfgFoot = $("#overview-config-foot");

  // default loading skeletons already in HTML; replace on success
  let healthz = null, health = null, obs = null, cfg = null;
  let anyFail = false;
  let failReason = "";

  // fetch in parallel, each guarded
  const pHealthz = fetchJson("/healthz").then(v=>healthz=v).catch(e=>{ anyFail=true; failReason=e.message; healthz={error:String(e.message), status:e.status}; });
  const pHealth  = fetchJson("/health").then(v=>health=v).catch(e=>{ anyFail=true; health={error:String(e.message)}; });
  const pObs     = fetchJson("/api/v1/observability").then(v=>obs=v).catch(e=>{ anyFail=true; obs={error:String(e.message)}; });
  const pCfg     = tryFetchJson(["/api/v1/config","/config"]).then(v=>cfg=v).catch(e=>{ anyFail=true; cfg={error:String(e.message)}; });

  // timeout race: if fetch hangs, show daemon unreachable after 3s but still await
  const timeout = new Promise((_,rej)=> setTimeout(()=>rej(new Error("timeout")), 3500));
  try{
    await Promise.race([Promise.all([pHealthz,pHealth,pObs,pCfg]), timeout]);
  } catch(e){
    if(e.message==="timeout"){ anyFail=true; failReason="timeout"; }
  }
  // ensure all settled
  await Promise.allSettled([pHealthz,pHealth,pObs,pCfg]);

  const isDown = (healthz && healthz.error) && (health && health.error);
  // also treat network error (TypeError) as down
  const networkDown = String(failReason).toLowerCase().includes("failed to fetch") || String(failReason)==="timeout" || (healthz && healthz.error && String(healthz.error).includes("Failed to fetch"));
  daemonReachable = !networkDown && !isDown;

  if(!daemonReachable){
    banner.hidden = false;
    dot.className = "daemon-dot is-bad";
    dotLabel.textContent = "Daemon unreachable";
    statusBody.innerHTML = `<div class="banner banner-error" style="margin:0"><strong>${esc(DAEMON_MSG)}</strong><span class="banner-hint">Couldn’t load memories — ${esc(failReason||"network error")}. <button class="btn btn-sm" onclick="location.reload()">Retry</button></span></div>`;
    statusMeta.textContent = `Tried ${DAEMON_URL} — the console is only served when the daemon is running.`;
    obsBody.innerHTML = `<p class="muted">Observability unavailable while daemon is down.</p>`;
    healthBody.innerHTML = `<p class="muted">Drivers unavailable while daemon is down.</p>`;
    cfgBody.innerHTML = `<p class="muted">Config unavailable while daemon is down.</p>`;
    cfgFoot.textContent = "";
    return;
  }
  banner.hidden = true;
  dot.className = "daemon-dot is-ok";
  dotLabel.textContent = "Daemon up";

  // --- status card: from /healthz ---
  if(healthz && !healthz.error){
    const gate = healthz.gate || {};
    const deg = gate.degradations || [];
    const hard = gate.hard_missing || [];
    const preset = esc(healthz.preset||"—");
    const uptime = healthz.uptime_ms!=null ? `${fmtNum(Math.round(healthz.uptime_ms))} ms uptime` : "";
    const stores = healthz.stores ? Object.entries(healthz.stores).map(([k,v])=> `${esc(k)}: ${esc(Object.values(v).join(", ")||"—")}`).join(" · ") : "—";
    statusBody.innerHTML = `
      <div class="kv-grid">
        <dl class="kv"><dt>Status</dt><dd><span class="badge ${gate.ok ? 'badge-ok' : 'badge-danger'}">${gate.ok ? "ok":"degraded"}</span> ${esc(healthz.status||"ok")} · preset <code>${preset}</code></dd></dl>
        <dl class="kv"><dt>Uptime</dt><dd>${esc(uptime||"—")}</dd></dl>
        <dl class="kv"><dt>Stores</dt><dd>${stores}</dd></dl>
        ${deg.length ? `<dl class="kv"><dt>Degradations</dt><dd>${esc(deg.map(d=>d.feature||d.capability).join(", "))}</dd></dl>` : ""}
        ${hard.length ? `<dl class="kv"><dt>Hard missing</dt><dd>${esc(hard.map(d=>d.feature||d.capability).join(", "))}</dd></dl>` : ""}
      </div>`;
    statusMeta.textContent = `Migrations: ${healthz.migrations ? Object.entries(healthz.migrations).map(([k,v])=>`${k}@${v}`).join(" · ") : "—"} · ${uptime}`;
  } else {
    statusBody.innerHTML = `<p class="muted">Couldn’t load memories — ${esc(healthz?.error||"unknown")}. <button class="btn btn-sm" id="retry-healthz">Retry</button></p>`;
  }

  // --- observability: /api/v1/observability ---
  if(obs && !obs.error){
    const boot = obs.boot_started_at ? fmtISO(obs.boot_started_at) : "—";
    const ingest = obs.capture_ingest_count ?? 0;
    const mcp = obs.mcp_handshake_count ?? 0;
    obsBody.innerHTML = `
      <div class="kv-grid">
        <dl class="kv"><dt>Capture ingests</dt><dd>${fmtNum(ingest)}</dd></dl>
        <dl class="kv"><dt>MCP handshakes</dt><dd>${fmtNum(mcp)}</dd></dl>
        <dl class="kv"><dt>Boot</dt><dd title="${esc(boot)}">${esc(fmtRel(obs.boot_started_at))} · <span class="muted">${esc(boot)}</span></dd></dl>
      </div>`;
    obsFoot.textContent = obs.last_mcp_handshake_at ? `Last MCP handshake ${fmtRel(obs.last_mcp_handshake_at)}` : "No MCP handshake yet in this boot.";
  } else {
    obsBody.innerHTML = `<p class="muted">Couldn’t load memories — ${esc(obs?.error||"unknown")}. <button class="btn btn-sm">Retry</button></p>`;
  }

  // --- health drivers: /health ---
  if(health && !health.error){
    const d = health.drivers || {};
    healthBody.innerHTML = `
      <div class="kv-grid">
        <dl class="kv"><dt>Version</dt><dd>${esc(health.version||"—")} · preset ${esc(health.preset||"—")}</dd></dl>
        <dl class="kv"><dt>Vector</dt><dd>${esc(d.vector||"—")}</dd></dl>
        <dl class="kv"><dt>Graph</dt><dd>${esc(d.graph||"—")}</dd></dl>
        <dl class="kv"><dt>Meta</dt><dd>${esc(d.meta||"—")}</dd></dl>
        <dl class="kv"><dt>Embed</dt><dd>${esc(d.embed||"—")}</dd></dl>
      </div>`;
  } else {
    healthBody.innerHTML = `<p class="muted">Couldn’t load memories — ${esc(health?.error||"unknown")}.</p>`;
  }

  // --- config: GET /api/v1/config (redacted, names only) ---
  if(cfg && !cfg.error){
    const cfgObj = cfg.config || cfg;
    const restart = cfg.restart_required || {};
    // flatten config to dot paths, max 40 rows, never show values that look like secrets? server already redacted to names.
    const flat = flattenConfig(cfgObj);
    const rows = flat.slice(0, 60);
    if(rows.length===0){
      cfgBody.innerHTML = `<p class="muted">Config empty.</p>`;
    } else {
      cfgBody.innerHTML = `<div class="kv-grid">${rows.map(([k,v])=>{
        let val = v;
        // never show raw secret strings: if value looks like key/token, show env-var name hint
        if(typeof val==="string" && /(?:sk-|api_|key|token|secret|password|ghp_)/i.test(val)) val = "•••• (redacted)";
        if(typeof val==="object") val = JSON.stringify(val);
        return `<dl class="kv"><dt>${esc(k)}</dt><dd>${esc(String(val))}</dd></dl>`;
      }).join("")}</div>`;
    }
    const needs = Object.keys(restart).length ? `Restart required for: ${Object.keys(restart).join(", ")}` : "No restart required.";
    cfgFoot.textContent = needs + ` · ${flat.length} keys · redacted · read-only`;
  } else {
    cfgBody.innerHTML = `<p class="muted">Couldn’t load memories — ${esc(cfg?.error||"unknown")}.</p>`;
    cfgFoot.textContent = "";
  }
}

function flattenConfig(obj, prefix="", out=[]){
  if(obj==null || typeof obj!=="object" || Array.isArray(obj)){
    out.push([prefix||"(root)", obj]);
    return out;
  }
  for(const [k,v] of Object.entries(obj)){
    const path = prefix ? `${prefix}.${k}` : k;
    if(v && typeof v==="object" && !Array.isArray(v) && !(v instanceof Date)){
      // don't recurse into arrays of primitives
      flattenConfig(v, path, out);
    } else {
      out.push([path, v]);
    }
  }
  return out;
}

// ---------- Config tab ----------
// Closed writable-key schema, mirrored from the backend CONFIG_KEY_REGISTRY so
// the form can render the right input per key (zero-build, no schema endpoint).
// type: "bool" | "number" | "int" | "choice" | "string" | "env" | "json"
const CONFIG_GROUPS = [
  { id:"schedule", title:"Dream schedule", desc:"When automatic dreams fire — score-pool floor + idle + hard deadline." },
  { id:"tier", title:"Hardware tier & ensemble", desc:"Tier anchors verification, thresholds bound merge and the delta clamp. Batched reflection lives here — restart required." },
  { id:"decay", title:"Decay", desc:"Unreinforced memories fade through w = confidence \u00D7 exp(-\u03BB\u00B7days)." },
  { id:"capture", title:"Capture auto-recall", desc:"The mid-session recall pipeline — one switch turns it off." },
  { id:"recall", title:"Recall rescue", desc:"Cue-driven rescue band — below-main-floor pins return only when cue overlap clears the floor." },
  { id:"profiles", title:"Profiles bindings", desc:"Agent \u2192 profile persona map. Edited on the Profiles tab." },
];
const CONFIG_KEYS = [
  // dream schedule
  { key:"dream.auto_trigger", group:"schedule", type:"bool", label:"Automatic dreams", hint:"Off holds triggers as pending manual runs." },
  { key:"dream.floor_pool_points", group:"schedule", type:"number", label:"Pool floor", hint:"Eligible once the score pool holds this many points." },
  { key:"dream.idle_min_sec", group:"schedule", type:"number", label:"Idle window (s)", hint:"Profile must be idle at least this long." },
  { key:"dream.hard_deadline_sec", group:"schedule", type:"number", label:"Hard deadline (s)", hint:"Force a dream once the oldest chunk waits this long." },
  // tier & ensemble (reflect_batch lives here, restart-required)
  { key:"dream.hardware_tier", group:"tier", type:"choice", options:["standard","lite","advanced"], label:"Hardware tier", hint:"standard | lite | advanced — lite locks the ensemble off." },
  { key:"dream.ensemble", group:"tier", type:"choice", options:["off","verify","vote"], label:"Ensemble", hint:"off | verify | vote — dual-reflect verification layer." },
  { key:"dream.core_confidence_floor", group:"tier", type:"number", label:"Core confidence floor", hint:"[0,1] — core triples below it downgrade to the isolated graph." },
  { key:"dream.delta_budget_ceiling_tokens", group:"tier", type:"int", label:"Delta budget ceiling", hint:"\u2265 5000 — the dynamic delta clamp's ceiling." },
  { key:"dream.pool_forced_cap", group:"tier", type:"number", label:"Forced cap", hint:"\u2265 floor — the score pool's forced-consolidation cap." },
  { key:"dream.reflect_batch_max_tokens", group:"tier", type:"int", label:"Batch cap (tokens)", hint:"0 disables batching; positive slices oversized backlogs. Restart required.", restart:true },
  // decay
  { key:"decay.enabled", group:"decay", type:"bool", label:"Decay sweep", hint:"Master switch for the daily decay sweep." },
  { key:"decay.sweep_interval_s", group:"decay", type:"number", label:"Sweep interval (s)", hint:"Default once daily (86400)." },
  { key:"decay.min_apply_delta", group:"decay", type:"number", label:"Min apply delta", hint:"Skip writes below this weight change." },
  { key:"decay.lambda_per_type", group:"decay", type:"json", label:"\u03BB per type", hint:"Node-type \u2192 decay-rate map (replace semantics).", jsonKeys:["USER","HABIT","PREFERENCE","ANIMA","INTENTION","CONSTRAINT","EPISODE","SKILL_SEQUENCE","DECISION","PROJECT","TOOL","chunk","pin"] },
  // capture
  { key:"capture.auto_recall", group:"capture", type:"bool", label:"Auto-recall", hint:"Enable the mid-session recall pipeline." },
  { key:"capture.auto_recall_focal_floor", group:"capture", type:"number", label:"Focal floor", hint:"(0,1] — below it a chunk is never focal." },
  { key:"capture.auto_recall_budget_chars", group:"capture", type:"int", label:"Recall budget (chars)", hint:"The pending-recall selection budget." },
  // recall rescue — read-only mirror of [recall] (not yet writable via registry; file-scoped until backend promotes it)
  { key:"recall.rescue_floor", group:"recall", type:"number", label:"Rescue floor", hint:"(0,1] — lower bound of the rescue band.", readonly:true },
  { key:"recall.rescue_cue_min", group:"recall", type:"number", label:"Cue minimum", hint:"(0,1] — cue overlap a below-floor pin must clear.", readonly:true },
];
const LLM_ROLES = ["dream","dream_verifier","dream_vote"];
const LLM_FIELDS = [
  { key:"driver", type:"string", label:"Driver", hint:"ollama | openai_compatible | stub — the route's driver." },
  { key:"model", type:"string", label:"Model", hint:"The model this role generates with." },
  { key:"base_url", type:"string", label:"Base URL", hint:"Endpoint for the driver (e.g. http://localhost:11434)." },
  { key:"api_key_env", type:"env", label:"API key env", hint:"Env-var NAME only — the key value is never stored or shown.", secret:true },
  { key:"max_tokens", type:"int", label:"Max tokens", hint:"Optional generation cap." },
  { key:"provider", type:"string", label:"Provider", hint:"Optional provider label (openai-compatible)." },
  { key:"think", type:"bool", label:"Thinking", hint:"Enable thinking models (off by default for structured extraction)." },
  { key:"num_ctx", type:"int", label:"Context window", hint:"Ollama context size (e.g. 16384)." },
  { key:"num_predict", type:"int", label:"Max predict", hint:"Ollama generation token cap." },
];

// version history — never cached across writes, always fresh-fetched before rollback
let configVersions = null;
let configGeneration = null; // optimistic-lock generation from GET /api/v1/config (if backend provides it)
let configRawMeta = null; // full GET payload for restart_required etc.

async function refreshConfigGeneration(){
  try{
    const data = await fetchJson("/api/v1/config");
    configRawMeta = data;
    configGeneration = data.generation ?? data.config_generation ?? null;
  }catch(e){
    configGeneration = null;
    console.warn("refreshConfigGeneration failed", e);
  }
}

function cfgGroupFor(key){
  const dot = key.indexOf(".");
  const top = key.slice(0, dot);
  if(top==="dream") return key.startsWith("dream.reflect_batch") ? "tier" : (["hardware_tier","ensemble","core_confidence_floor","delta_budget_ceiling_tokens","pool_forced_cap","reflect_batch_max_tokens"].includes(key.split(".").pop()) ? "tier" : "schedule");
  if(top==="decay") return "decay";
  if(top==="capture") return "capture";
  if(top==="recall") return "recall";
  return "profiles";
}

async function loadConfigPage(){
  const body = $("#config-body"), foot = $("#config-foot");
  const errBanner = $("#config-error-banner");
  errBanner.hidden = true;
  body.innerHTML = `<div class="skeleton skeleton-line" style="width:80%"></div><div class="skeleton skeleton-line" style="width:60%"></div>`;
  try{
    const data = await fetchJson("/api/v1/config");
    configRawMeta = data;
    configGeneration = data.generation ?? data.config_generation ?? null;
    renderConfigForm(data.config || data);
    // also refresh version history quietly if user already opened it once
    if($("#config-versions-body").dataset.loaded === "true"){
      loadConfigVersions();
    }
  }catch(e){
    errBanner.hidden = false;
    const isDown = String(e.message||"").toLowerCase().includes("failed to fetch") || e.status===0;
    if(isDown){
      errBanner.innerHTML = `<strong>Daemon unreachable</strong> — <span>${esc(DAEMON_MSG)}</span> <span class="banner-hint">Start the daemon and refresh.</span>`;
    } else {
      errBanner.innerHTML = `<strong>Couldn\u2019t load config</strong> — <span>${esc(e.message||"network error")}</span>`;
    }
    body.innerHTML = "";
  }
}

function renderConfigForm(cfg){
  const body = $("#config-body");
  const groups = CONFIG_GROUPS.map(g => {
    const scalars = CONFIG_KEYS.filter(k => k.group===g.id);
    return { g, rows: scalars };
  });
  let html = "";
  for(const {g, rows} of groups){
    html += `<div class="cfg-group"><div class="cfg-group-head"><h3 class="cfg-group-title">${esc(g.title)}</h3><p class="cfg-group-desc">${esc(g.desc)}</p></div>`;
    html += rows.map(k => cfgRowHtml(k, cfg)).join("");
    if(g.id==="profiles"){
      html += profilesBindingRowHtml(cfg);
    }
    html += `</div>`;
  }
  // LLM roles section — 2 primary roles per spec (dream + dream_verifier); dream_vote as optional third when present
  html += `<div class="cfg-group"><div class="cfg-group-head"><h3 class="cfg-group-title">LLM roles</h3><p class="cfg-group-desc">Per-role routes for dream generation and the ensemble seats. Each field shows its env-var name only (never a value) and whether it live-applies.</p></div>`;
  for(const role of LLM_ROLES){
    const isPrimary = role==="dream" || role==="dream_verifier";
    const badgeExtra = role==="dream_vote" ? `<span class="badge badge-source">no default route</span>` : (isPrimary ? `<span class="badge badge-source">primary</span>` : "");
    html += `<div class="cfg-role"><div class="cfg-role-head"><span class="badge badge-muted">${esc(role)}</span>${badgeExtra}</div>`;
    for(const f of LLM_FIELDS){
      const key = `dream.llm.${role}.${f.key}`;
      html += cfgRowHtml({...f, key, group:"llm"}, cfg);
    }
    html += `</div>`;
  }
  html += `</div>`;

  body.innerHTML = html;
  $$("#config-body [data-save-key]").forEach(btn=>{
    btn.addEventListener("click", ()=> saveConfigKey(btn.dataset.saveKey));
  });
  $$("#config-body input[data-key], #config-body select[data-key], #config-body textarea[data-key]").forEach(inp=>{
    inp.addEventListener("keydown", (e)=>{ if(e.key==="Enter" && inp.tagName!=="TEXTAREA"){ e.preventDefault(); saveConfigKey(inp.dataset.key); } });
  });

  const restartHint = CONFIG_KEYS.filter(k=>k.restart).map(k=>k.key).concat(["preset","storage.vector","storage.graph","storage.meta","storage.embed","baseurl"]);
  const restartBanner = $("#config-restart-banner");
  restartBanner.hidden = false;
  $("#config-restart-hint").textContent = "Boot-scope keys (preset \u00B7 baseurl \u00B7 storage.*) and dream.reflect_batch_max_tokens only apply after a restart. All other keys are live-applied.";
  const genNote = configGeneration!=null ? ` · generation ${esc(String(configGeneration))}` : "";
  $("#config-foot").textContent = `Writes are versioned and audited (X-MnemoSeed-Actor: console). Rollback restores an earlier version as a new one \u2014 nothing is deleted${genNote}. DB is primary, config.toml is its mirror.`;
}

function cfgCurValue(key, cfg){
  const parts = key.split(".");
  let cur = cfg;
  for(const p of parts){ if(cur==null) return undefined; cur = cur[p]; }
  return cur;
}

function cfgRowHtml(k, cfg){
  const cur = cfgCurValue(k.key, cfg);
  const valStr = cur==null ? "\u2014" : (typeof cur==="object" ? JSON.stringify(cur) : String(cur));
  const isSecret = !!k.secret;
  const bootScoped = k.readonly || ["preset","baseurl"].includes(k.key) || k.key.startsWith("storage.") || k.key.startsWith("recall.");
  const restart = !!(k.restart || bootScoped);
  const sourceBadge = bootScoped
    ? `<span class="badge badge-boot" title="File-scoped; change it in config.toml and restart">file-scoped</span>`
    : `<span class="badge badge-source" title="DB is primary, file is the generated mirror">DB-primary</span>`;
  const liveBadge = `<span class="badge ${restart?"badge-restart":"badge-live"}" title="${restart?"Needs restart":"Applied to the running daemon immediately"}">${restart?"restart":"live"}</span>`;

  let input = "";
  if(bootScoped){
    const display = k.secret ? cfgSecretDisplay(cur) : `<code class="muted">${esc(valStr)}</code>`;
    const note = k.readonly ? "read-only in console" : "file-scoped \u00B7 restart required";
    input = `${display} <span class="badge badge-boot" title="${esc(note)}">${esc(note)}</span>`;
  } else if(k.type==="bool"){
    input = `<select class="select" data-key="${esc(k.key)}" aria-label="${esc(k.key)}"><option value="true" ${cur===true?"selected":""}>true</option><option value="false" ${cur===false?"selected":""}>false</option></select>`;
  } else if(k.type==="choice"){
    input = `<select class="select" data-key="${esc(k.key)}" aria-label="${esc(k.key)}">${(k.options||[]).map(o=>`<option value="${esc(o)}" ${cur===o?"selected":""}>${esc(o)}</option>`).join("")}</select>`;
  } else if(k.type==="json"){
    input = `<textarea class="input cfg-json" data-key="${esc(k.key)}" aria-label="${esc(k.key)}" rows="3" spellcheck="false">${esc(valStr)}</textarea>`;
  } else if(k.type==="env"){
    input = `<input class="input" data-key="${esc(k.key)}" aria-label="${esc(k.key)}" type="text" placeholder="e.g. FIREWORKS_API_KEY" value="${esc(cur||"")}" spellcheck="false">`;
  } else {
    const t = k.type==="int" ? "number" : "text";
    input = `<input class="input" data-key="${esc(k.key)}" aria-label="${esc(k.key)}" type="${t}" ${k.type==="int"?"step=\"1\"":""} value='${esc(cur==null?"":String(cur))}' spellcheck="false">`;
  }

  const badges = `${sourceBadge} ${liveBadge}`;
  const saveBtn = bootScoped ? "" : `<button class="btn btn-primary btn-sm" data-save-key="${esc(k.key)}" type="button">Save</button>`;
  return `<div class="cfg-row" data-x="${esc(k.key)}">
    <div class="cfg-key">
      <code>${esc(k.key)}</code>
      <span class="cfg-sub">${esc(k.label)}${k.hint?` \u2014 ${esc(k.hint)}`:""}</span>
    </div>
    <div class="cfg-value">
      <div class="cfg-edit">${input}</div>
      <div class="cfg-badges">${badges}</div>
      ${saveBtn}
    </div>
    <div class="cfg-err" data-err="${esc(k.key)}" hidden></div>
  </div>`;
}

function cfgSecretDisplay(v){
  if(v==null || v==="") return "\u2014";
  const names = String(v).split(",").map(s=>s.trim()).filter(Boolean);
  return names.length ? names.map(n=>`<code>${esc(n)}</code>`).join(", ") : "\u2014";
}

function profilesBindingRowHtml(cfg){
  const bindings = (cfg.profiles && cfg.profiles.agent_bindings) || {};
  const keys = Object.keys(bindings);
  const summary = keys.length ? keys.map(a=>`<code>${esc(a)} \u2192 ${esc(bindings[a])}</code>`).join(" \u00B7 ") : "<span class='muted'>no bindings</span>";
  return `<div class="cfg-row">
    <div class="cfg-key"><code>profiles.agent_bindings</code><span class="cfg-sub">Agent \u2192 profile map (replace semantics)</span></div>
    <div class="cfg-value">${summary}</div>
    <a href="#/profiles" class="btn btn-ghost btn-sm">Edit in Profiles</a>
  </div>`;
}

function specForKey(key){
  const scalar = CONFIG_KEYS.find(k => k.key === key);
  if (scalar) return scalar;
  const m = /^dream\.llm\.[^.]+\.(.+)$/.exec(key);
  if (m) {
    const field = LLM_FIELDS.find(f => f.key === m[1]);
    if (field) return { ...field, key };
  }
  return {};
}

async function saveConfigKey(key){
  const inp = $(`[data-key="${key}"]`);
  const errEl = $(`[data-err="${key}"]`);
  const row = $(`.cfg-row[data-x="${key}"]`);
  if(errEl) errEl.hidden = true;
  if(row) row.classList.remove("is-error");
  if(!inp){ return; }

  let value;
  const spec = specForKey(key);
  if(spec.type==="bool"){
    value = inp.value === "true";
  } else if(spec.type==="int"){
    const n = inp.value.trim();
    if(n==="" || !/^-?\d+$/.test(n)){ return cfgFieldError(key, "must be an integer"); }
    value = parseInt(n,10);
  } else if(spec.type==="json"){
    try{ value = JSON.parse(inp.value.trim()); }
    catch{ return cfgFieldError(key, "must be valid JSON (e.g. {\"USER\": 0.01})"); }
  } else if(spec.type==="choice" || spec.type==="string" || spec.type==="env"){
    value = inp.value;
  } else {
    const n = inp.value.trim();
    if(n===""){ return cfgFieldError(key, "value is required"); }
    const f = Number(n);
    if(Number.isNaN(f)){ return cfgFieldError(key, "must be a number"); }
    value = f;
  }

  const btn = $(`[data-save-key="${key}"]`);
  if(btn){ btn.disabled = true; btn.textContent = "Saving\u2026"; }
  try{
    const headers = {"content-type":"application/json", "x-mnemoseed-actor":"console"};
    if(configGeneration!=null){
      headers["if-match"] = String(configGeneration);
      headers["If-Match"] = String(configGeneration);
    }
    await fetchJson("/api/v1/config/set", {
      method:"POST",
      headers,
      body: JSON.stringify({ key_path: key, value }),
    });
    a11yLive(`Saved ${key}`);
    await loadConfigPage();
    if($("#config-versions-body").dataset.loaded === "true"){
      await loadConfigVersions();
    }
  }catch(e){
    if(e.status===409){
      cfgFieldError(key, "Someone else changed this \u2014 reloaded. Your change was not applied.");
      await loadConfigPage();
      if($("#config-versions-body").dataset.loaded === "true") await loadConfigVersions();
    } else if(e.status===422){
      const raw = (e.message||"").replace(/^config\[[^\]]*\]:\s*/, "");
      const msg = raw || "invalid value";
      cfgFieldError(key, msg);
    } else if(e.status===403){
      cfgFieldError(key, "Config writes are rejected \u2014 the daemon baseurl is non-loopback.");
    } else {
      cfgFieldError(key, e.message||"write failed");
    }
  } finally {
    if(btn){ btn.disabled = false; btn.textContent = "Save"; }
  }
}

function cfgFieldError(key, msg){
  const errEl = $(`[data-err="${key}"]`);
  const row = $(`.cfg-row[data-x="${key}"]`);
  if(errEl){ errEl.hidden = false; errEl.textContent = msg; }
  if(row) row.classList.add("is-error");
  a11yLive(`${key}: ${msg}`);
  if(/locks the ensemble off/i.test(msg)){
    const el = $(`[data-err="${key}"]`);
    if(el) el.textContent = msg + " \u2014 pick 'off' for ensemble, or change hardware_tier off 'lite' first.";
  }
  if(/must be <= dream\.delta_budget_ceiling_tokens/i.test(msg) && errEl) errEl.textContent = msg + " \u2014 raise the delta ceiling, or lower this cap.";
  if(/requires the 'isolated' graph instance/i.test(msg) && errEl) errEl.textContent = msg + " \u2014 add [storage.graph.instances.isolated] to config.toml (driver = \"sqlite_graph\").";
  if(/must be >= dream\.core_confidence_floor/i.test(msg) && errEl) errEl.textContent = msg + " \u2014 lower the core confidence floor first.";
  if(/must be <= dream\.pool_forced_cap/i.test(msg) && errEl) errEl.textContent = msg + " \u2014 raise pool_forced_cap first.";
}

async function loadConfigVersions(){
  const body = $("#config-versions-body");
  body.dataset.loaded = "true";
  body.innerHTML = `<div class="skeleton skeleton-line" style="width:70%"></div><div class="skeleton skeleton-line" style="width:55%"></div>`;
  try{
    const data = await fetchJson("/api/v1/config/versions");
    const versions = data.versions || data || [];
    configVersions = versions;
    renderConfigVersions(versions);
  }catch(e){
    body.innerHTML = `<p class="muted">Couldn\u2019t load versions \u2014 ${esc(e.message||"network error")}.</p>`;
  }
}

function renderConfigVersions(versions){
  const body = $("#config-versions-body");
  if(!versions || versions.length===0){
    body.innerHTML = `<p class="muted">No versions yet \u2014 writes appear here once you save a key.</p>`;
    return;
  }
  const sorted = [...versions].sort((a,b)=> (b.version_id||b.version||0) - (a.version_id||a.version||0));
  const rows = sorted.slice(0, 50).map(v=>{
    const key = esc(v.key||v.key_path||"\u2014");
    const ver = esc(String(v.version||"\u2014"));
    const vid = esc(String(v.version_id||"\u2014"));
    const val = v.value==null ? "\u2014" : esc(typeof v.value==="object" ? JSON.stringify(v.value) : String(v.value));
    const when = v.updated_at ? esc(fmtRel(v.updated_at)) : "\u2014";
    const whenTitle = v.updated_at ? esc(fmtISO(v.updated_at)) : "";
    return `<tr>
      <td><code>${key}</code></td>
      <td>v${ver}</td>
      <td class="ver-val" title="${val}">${val}</td>
      <td title="${whenTitle}">${when}</td>
      <td><code>${vid}</code></td>
      <td><button class="btn btn-ghost btn-sm" data-rollback="${esc(String(v.version_id))}" type="button">Restore</button></td>
    </tr>`;
  }).join("");
  body.innerHTML = `<table class="ver-table"><thead><tr><th>Key</th><th>Version</th><th>Value</th><th>Updated</th><th>Version ID</th><th></th></tr></thead><tbody>${rows}</tbody></table><p class="small muted">Restore creates a new version (append-only) and live-applies it. Version IDs are fetched fresh on every load \u2014 never cached.</p>`;
  $$("#config-versions-body [data-rollback]").forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      const vid = btn.dataset.rollback;
      btn.disabled = true; btn.textContent = "Restoring\u2026";
      try{
        // always refetch versions fresh before rollback (never use cached id)
        const fresh = await fetchJson("/api/v1/config/versions");
        const found = (fresh.versions||fresh||[]).find(x=> String(x.version_id)===String(vid));
        const targetId = found ? found.version_id : vid;
        await fetchJson("/api/v1/config/rollback", {
          method:"POST",
          headers:{"content-type":"application/json","x-mnemoseed-actor":"console"},
          body: JSON.stringify({ version_id: Number(targetId) }),
        });
        a11yLive(`Restored ${vid}`);
        await loadConfigPage();
        await loadConfigVersions();
      }catch(e){
        a11yLive(`Rollback failed: ${e.message}`);
        const banner = $("#config-error-banner");
        banner.hidden = false;
        banner.innerHTML = `<strong>Rollback failed</strong> — <span>${esc(e.message||"error")}</span>`;
      }finally{
        btn.disabled = false; btn.textContent = "Restore";
      }
    });
  });
}

// ---------- Profiles tab ----------
let profilesStore = []; // {profile_id, display_name, created_at, archived}

async function loadProfilesPage(){
  const errBanner = $("#profiles-error-banner");
  errBanner.hidden = true;
  await refreshProfilesList();
  await refreshBindingsEditor();
}

async function refreshProfilesList(){
  const body = $("#profiles-list-body");
  try{
    const data = await fetchJson("/api/v1/profiles");
    profilesStore = data.profiles || [];
    renderProfilesList();
    refreshOrphanBanner();
  }catch(e){
    body.innerHTML = `<p class="muted">Couldn’t load profiles — ${esc(e.message||"network error")}.</p>`;
  }
}

function renderProfilesList(){
  const body = $("#profiles-list-body");
  const archBanner = $("#profiles-archived-banner");
  const hasArchived = profilesStore.some(p=> p.archived);
  if(archBanner) archBanner.hidden = !hasArchived;
  if(profilesStore.length===0){
    body.innerHTML = `<p class="muted">No profiles yet — the <code>default</code> profile is implicit and always available. Create one above.</p>`;
    return;
  }
  const rows = profilesStore.map(p=>{
    const arch = p.archived;
    return `<tr>
      <td><code>${esc(p.profile_id)}</code></td>
      <td>${esc(p.display_name||"\u2014")}</td>
      <td>${p.created_at ? esc(fmtRel(p.created_at)) : "\u2014"}</td>
      <td>${arch ? `<span class="badge badge-archived">archived</span>` : `<span class="badge badge-live">active</span>`}</td>
      <td>
        <button class="btn btn-ghost btn-sm" data-archive="${esc(p.profile_id)}" data-archived="${arch}" type="button">${arch ? "Unarchive" : "Archive"}</button>
      </td>
    </tr>`;
  }).join("");
  body.innerHTML = `<table class="profile-table"><thead><tr><th>Profile ID</th><th>Display name</th><th>Created</th><th>Status</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
  $$("#profiles-list-body [data-archive]").forEach(b=> b.addEventListener("click", ()=> archiveProfile(b.dataset.archive, b.dataset.archived==="true")));
}

async function archiveProfile(id, currentlyArchived){
  const next = !currentlyArchived;
  try{
    await fetchJson("/api/v1/profiles/archive", {
      method:"POST", headers:{"content-type":"application/json", "x-mnemoseed-actor":"console"},
      body: JSON.stringify({ profile_id: id, archived: next }),
    });
    a11yLive(`${id} ${next?"archived":"unarchived"}`);
    await refreshProfilesList();
  }catch(e){
    const banner = $("#profiles-error-banner");
    banner.hidden = false;
    banner.innerHTML = `<strong>Archive failed</strong> — <span>${esc(e.message||"error")}</span>`;
  }
}

async function refreshDrawnBindings(){
  // returns the current bindings from config (fresh)
  try{
    const data = await fetchJson("/api/v1/config");
    return (data.config && data.config.profiles && data.config.profiles.agent_bindings) || {};
  }catch{ return {}; }
}

async function refreshBindingsEditor(){
  const editor = $("#bindings-editor");
  const bindings = await refreshDrawnBindings();
  const profileIds = new Set(["default", ...profilesStore.map(p=>p.profile_id)]);
  const entries = Object.entries(bindings);
  renderBindings(entries.map(([agent, pid])=>({agent, pid})));
  // refresh orphan warning using live profile list
  refreshOrphanBanner();
}

function renderBindings(entries){
  const editor = $("#bindings-editor");
  if(!entries.length){
    editor.innerHTML = `<p class="muted">No bindings — add one to route an agent's captures to a profile.</p>`;
    return;
  }
  const profileIds = ["default", ...profilesStore.map(p=>p.profile_id)];
  editor.innerHTML = entries.map((e,i)=>{
    const orphan = !profileIds.includes(e.pid);
    return `<div class="binding-row ${orphan?"is-orphan":""}" data-i="${i}">
      <input class="input binding-agent" type="text" value="${esc(e.agent)}" placeholder="agent label (e.g. planner)" spellcheck="false">
      <select class="select binding-pid">
        <option value="">Pick a profile…</option>
        ${["default", ...profilesStore.map(p=>p.profile_id)].filter(unique).map(id=>`<option value="${esc(id)}" ${id===e.pid?"selected":""}>${esc(id)}</option>`).join("")}
        ${orphan && !profileIds.includes(e.pid) ? `<option value="${esc(e.pid)}" selected>${esc(e.pid)} (missing)</option>` : ""}
      </select>
      <button class="binding-remove" type="button" aria-label="Remove binding" title="Remove">×</button>
      ${orphan?`<span class="badge badge-warn">orphan</span>`:""}
    </div>`;
  }).join("");
  $$("#bindings-editor .binding-remove").forEach(b=> b.addEventListener("click", ()=>{
    b.closest(".binding-row").remove();
    if(!$$("#bindings-editor .binding-row").length) renderBindings([]);
  }));
}

function unique(v,i,a){ return a.indexOf(v)===i; }

function collectBindings(){
  const out = {};
  $$("#bindings-editor .binding-row").forEach(row=>{
    const agent = row.querySelector(".binding-agent").value.trim();
    const pid = row.querySelector(".binding-pid").value;
    if(agent && pid) out[agent] = pid;
  });
  return out;
}

async function saveBindings(){
  const fb = $("#bindings-feedback");
  const value = collectBindings();
  fb.textContent = "Saving\u2026";
  try{
    const headers = {"content-type":"application/json", "x-mnemoseed-actor":"console"};
    if(configGeneration!=null){ headers["if-match"] = String(configGeneration); headers["If-Match"] = String(configGeneration); }
    await fetchJson("/api/v1/config/set", {
      method:"POST", headers,
      body: JSON.stringify({ key_path: "profiles.agent_bindings", value }),
    });
    fb.textContent = "Saved.";
    a11yLive("Bindings saved");
    await refreshConfigGeneration();
    await refreshBindingsEditor();
    refreshOrphanBanner();
    if($("#config-versions-body").dataset.loaded === "true") await loadConfigVersions();
  }catch(e){
    if(e.status===409){
      fb.textContent = "Someone else changed this \u2014 reloaded. Your change was not applied.";
      await refreshBindingsEditor();
    } else if(e.status===422){
      fb.textContent = `Invalid: ${(e.message||"").replace(/^config\[[^\]]*\]:\s*/, "")}`;
    } else {
      fb.textContent = `Couldn\u2019t save \u2014 ${e.message||"error"}`;
    }
    a11yLive("Bindings save failed");
  }
}

function refreshOrphanBanner(){
  const banner = $("#profiles-orphan-banner");
  const listEl = $("#profiles-orphan-list");
  const profileIds = new Set(["default", ...profilesStore.map(p=>p.profile_id)]);
  const orphans = [];
  $$("#bindings-editor .binding-row.is-orphan .binding-pid").forEach(sel=>{
    if(sel.value && !profileIds.has(sel.value)) orphans.push(sel.value);
  });
  if(orphans.length){
    banner.hidden = false;
    listEl.textContent = `${[...new Set(orphans)].join(", ")} — repoint these bindings or the captures will route to a profile that doesn't exist.`;
  } else {
    banner.hidden = true;
  }
}

// ---------- Dream tab ----------
let dreamAbort = null;

async function loadDreamPage(){
  const errBanner = $("#dream-error-banner");
  errBanner.hidden = true;
  // populate profile selector
  try{
    const data = await fetchJson("/api/v1/profiles");
    const ids = new Set(["default", ...(data.profiles||[]).map(p=>p.profile_id)]);
    const sel = $("#dream-profile");
    sel.innerHTML = Array.from(ids).map(id=>`<option value="${esc(id)}">${esc(id)}</option>`).join("");
  }catch{ /* keep default */ }
  await loadDreamStatus();
  await loadDreamAutoToggle();
}

async function loadDreamStatus(){
  const body = $("#dream-status-body"), foot = $("#dream-status-foot");
  const poolBody = $("#dream-pool-body");
  try{
    const data = await fetchJson("/memory/dream_status", {
      method:"POST", headers:{"content-type":"application/json"},
      body: JSON.stringify({ profile_id: $("#dream-profile").value || "default" }),
    });
    const state = data.state || "unknown";
    const pending = data.pending_queue!=null ? data.pending_queue : (data.pending_manual||0);
    body.innerHTML = `<div class="kv-grid">
      <dl class="kv"><dt>State</dt><dd><span class="badge">${esc(state)}</span></dd></dl>
      <dl class="kv"><dt>Pending manual</dt><dd>${fmtNum(data.pending_manual ?? 0)}</dd></dl>
      <dl class="kv"><dt>Pending queue</dt><dd>${fmtNum(data.pending_queue ?? 0)}</dd></dl>
      ${data.current_range ? `<dl class="kv"><dt>Current range</dt><dd>${esc(data.current_range.start)} → ${esc(data.current_range.end)}</dd></dl>` : ""}
    </div>`;
    foot.textContent = data.last_event ? `Last event ${fmtRel(data.last_event.fired_at)} · ${esc(data.last_event.kind)}` : "No dream event yet.";
    renderDreamPool(data);
  }catch(e){
    body.innerHTML = `<p class="muted">Couldn’t load dream status — ${esc(e.message||"network error")}.</p>`;
    poolBody.innerHTML = `<p class="muted">Score pool unavailable — ${esc(e.message||"network error")}.</p>`;
  }
}

function renderDreamPool(data){
  const poolBody = $("#dream-pool-body");
  const pool = data.pool || {};
  const balance = pool.balance ?? 0;
  const threshold = pool.threshold ?? 0;
  const pct = threshold>0 ? clamp((balance/threshold)*100, 0, 100) : 0;
  const overFloor = balance >= threshold && threshold>0;
  const commitment = pool.lifetime_filed ?? null;
  const watermark = data.watermark;
  const hist = data.history || {};
  const failEntries = Object.entries(hist.extract_failures || {});
  const failSummary = failEntries.length ? failEntries.map(([c,n])=>`${esc(c)} ${n}`).join(" \u00B7 ") : "none";
  poolBody.innerHTML = `<div class="kv-grid dream-kv">
    <dl class="kv"><dt>Pool balance</dt><dd class="pool-chip"><span class="pool-balance">${fmtNum(balance)}</span> <span class="muted">/ floor ${fmtNum(threshold)}</span> ${overFloor?`<span class="badge badge-warn">at floor</span>`:`<span class="badge badge-muted">${fmtNum(Math.max(0, threshold-balance))} to floor</span>`}</dd></dl>
    <dl class="kv"><dt>Progress</dt><dd><div class="progress" title="${pct.toFixed(1)}% of floor"><div class="progress-bar ${overFloor?"is-healthy":""}" style="width:${pct}%"></div></div><span class="small muted">${pct.toFixed(0)}% — ${overFloor?"eligible once idle":"collecting"}</span></dd></dl>
    <dl class="kv"><dt>Lifetime filed</dt><dd>${commitment==null?"\u2014":fmtNum(commitment)}</dd></dl>
    <dl class="kv"><dt>Watermark</dt><dd>${watermark ? `${esc(String(watermark.start))} \u2192 ${esc(String(watermark.end))}` : "\u2014"}</dd></dl>
    <dl class="kv"><dt>Committed runs</dt><dd>${fmtNum(hist.committed_runs ?? 0)}${hist.last_commit_at ? ` \u00B7 last ${esc(fmtRel(hist.last_commit_at))}` : ""}</dd></dl>
    <dl class="kv"><dt>Extract failures</dt><dd>${esc(failSummary)}</dd></dl>
  </div>`;
  renderDreamReflect();
}

async function renderDreamReflect(){
  const body = $("#dream-reflect-body");
  if(!body) return;
  try{
    const data = await fetchJson("/api/v1/config");
    const cap = data.config && data.config.dream ? data.config.dream.reflect_batch_max_tokens : null;
    const ceiling = data.config && data.config.dream ? data.config.dream.delta_budget_ceiling_tokens : null;
    const val = cap==null ? "\u2014" : fmtNum(cap);
    const ceilNote = ceiling!=null ? ` (ceiling ${fmtNum(ceiling)})` : "";
    const hint = cap===0 ? "0 \u2014 batching disabled, single-pack reflect" : `${val}${ceilNote}`;
    body.innerHTML = `<dl class="kv"><dt>Batch cap</dt><dd><span class="pool-balance">${esc(hint)}</span> <span class="badge badge-restart">restart required</span></dd></dl><p class="small muted">Must be \u2264 delta ceiling; 0 restores legacy single-pack. Change does not hot-apply.</p>`;
  }catch{
    body.innerHTML = `<p class="muted">Couldn\u2019t load reflect cap \u2014 ${esc(DAEMON_MSG)}</p>`;
  }
}

async function loadDreamAutoToggle(){
  const toggle = $("#dream-auto-toggle");
  const banner = $("#dream-error-banner");
  try{
    const data = await fetchJson("/api/v1/config");
    const on = !!(data.config && data.config.dream && data.config.dream.auto_trigger);
    toggle.setAttribute("aria-checked", String(on));
    toggle.disabled = false;
    toggle.setAttribute("aria-disabled", "false");
    if(banner) banner.hidden = true;
  }catch(e){
    toggle.setAttribute("aria-checked", "false");
    const isDown = String(e.message||"").toLowerCase().includes("failed to fetch") || e.status===0 || e.status==null;
    if(isDown){
      toggle.disabled = true;
      toggle.setAttribute("aria-disabled", "true");
    } else {
      toggle.disabled = false;
      toggle.setAttribute("aria-disabled", "false");
    }
    if(banner){
      banner.hidden = false;
      if(isDown){
        banner.innerHTML = `<strong>Daemon unreachable</strong> — <span>${esc(DAEMON_MSG)}</span> <span class="banner-hint">Start the daemon and refresh.</span>`;
      } else {
        banner.innerHTML = `<strong>Couldn\u2019t load automatic dreams</strong> — <span>${esc(e.message||"network error")}</span>`;
      }
    }
  }
}

function initDreamEvents(){
  $("#dream-run").addEventListener("click", runDreamOnce);
  $("#dream-cancel").addEventListener("click", cancelDreamOnce);
  $("#dream-auto-toggle").addEventListener("click", toggleAutoDream);
  $("#dream-profile").addEventListener("change", loadDreamStatus);
}

async function runDreamOnce(){
  const runBtn = $("#dream-run"), cancelBtn = $("#dream-cancel"), fb = $("#dream-run-feedback");
  const profile = $("#dream-profile").value || "default";
  runBtn.disabled = true;
  cancelBtn.hidden = false;
  fb.textContent = "Running… this can take a while.";
  dreamAbort = new AbortController();
  const timeout = setTimeout(()=>{ dreamAbort.abort(); }, 30000);
  try{
    const data = await fetchJson("/memory/dream_once", {
      method:"POST", headers:{"content-type":"application/json"},
      body: JSON.stringify({ profile_id: profile }),
      signal: dreamAbort.signal,
    });
    const launched = data.launched;
    fb.textContent = launched ? "Launched — check status above." : `Not launched (state: ${data.state||"unknown"}).`;
    a11yLive(launched ? "Dream launched" : "Dream not launched");
    loadDreamStatus();
  }catch(e){
    if(e.name==="AbortError" || (e.status && e.status===499)){
      fb.textContent = "Timed out — the dream may still be running. Check status in a moment.";
    } else {
      fb.textContent = `Couldn’t run — ${e.message||"error"}`;
    }
    a11yLive("Dream run failed");
  } finally {
    clearTimeout(timeout);
    dreamAbort = null;
    runBtn.disabled = false;
    cancelBtn.hidden = true;
  }
}

function cancelDreamOnce(){
  if(dreamAbort){ dreamAbort.abort(); }
  $("#dream-run-feedback").textContent = "Cancelled.";
}

async function toggleAutoDream(){
  const toggle = $("#dream-auto-toggle");
  const cur = toggle.getAttribute("aria-checked")==="true";
  const next = !cur;
  try{
    const headers = {"content-type":"application/json", "x-mnemoseed-actor":"console"};
    if(configGeneration!=null){ headers["if-match"] = String(configGeneration); headers["If-Match"] = String(configGeneration); }
    await fetchJson("/api/v1/config/set", {
      method:"POST", headers,
      body: JSON.stringify({ key_path: "dream.auto_trigger", value: next }),
    });
    toggle.setAttribute("aria-checked", String(next));
    a11yLive(`Automatic dreams ${next?"on":"off"}`);
    await refreshConfigGeneration();
  }catch(e){
    if(e.status===409){
      a11yLive("Toggle failed: someone else changed this — reloaded");
      await loadDreamAutoToggle();
    } else {
      a11yLive(`Toggle failed: ${e.message||"error"}`);
    }
    const banner = $("#dream-error-banner");
    banner.hidden = false;
    if(e.status===422){
      banner.innerHTML = `<strong>Toggle failed</strong> — <span>${esc((e.message||"").replace(/^config\[[^\]]*\]:\s*/, ""))}</span>`;
    } else if(e.status===409){
      banner.innerHTML = `<strong>Toggle failed</strong> — <span>Someone else changed this \u2014 reloaded. Try again.</span>`;
    } else {
      banner.innerHTML = `<strong>Toggle failed</strong> — <span>${esc(e.message||"error")}</span>`;
    }
  }
}

// ---------- Atlas state ----------
const atlasState = {
  profile: "default",
  kind: "all",
  type: "all",
  decay: "all",
  time: "all",
  timeFrom: null,
  timeTo: null,
  entities: "",
  density: "auto",
  viewMode: "3d", // "3d" | "list"
  items: [], // AtlasItem[]
  total: 0,
  window_truncated: false,
  positions: null, // {id:[x,y,z]} | null
  algo: null,
  fallback2d: false,
  selectedId: null,
  sort: "recent",
  _fetchAbort: null,
};

let atlasRenderer = null; // Three.js renderer + scene, or null for 2d fallback
let atlas2dCtx = null;
let atlasPendingRetry3d = false;
let virtual = { start:0, end:0, rowH:56, overscan:8 };

function atlasQueryHash(){
  const p = new URLSearchParams();
  p.set("profile", atlasState.profile);
  p.set("kind", atlasState.kind);
  if(atlasState.type!=="all") p.set("type", atlasState.type);
  p.set("decay", atlasState.decay);
  p.set("time", atlasState.time);
  if(atlasState.entities) p.set("entities", atlasState.entities);
  p.set("density", atlasState.density);
  p.set("mode", atlasState.viewMode);
  if(atlasState.selectedId) p.set("selected", atlasState.selectedId);
  return p.toString();
}
function applyHashToState(){
  const h = location.hash || "";
  const q = h.includes("?") ? h.split("?")[1] : "";
  if(!q) return;
  const p = new URLSearchParams(q);
  if(p.get("profile")) atlasState.profile = p.get("profile");
  if(p.get("kind")) atlasState.kind = p.get("kind");
  if(p.get("type")) atlasState.type = p.get("type");
  if(p.get("decay")) atlasState.decay = p.get("decay");
  if(p.get("time")) atlasState.time = p.get("time");
  if(p.get("entities")) atlasState.entities = p.get("entities");
  if(p.get("density")) atlasState.density = p.get("density");
  if(p.get("mode")==="list"||p.get("mode")==="3d") atlasState.viewMode = p.get("mode");
  if(p.get("selected")) atlasState.selectedId = p.get("selected");
}
function pushAtlasHash(){
  const base = "#/memory/atlas";
  const qs = atlasQueryHash();
  const next = qs ? `${base}?${qs}` : base;
  if(location.hash!==next) history.replaceState(null, "", next);
}

async function loadProfiles(){
  const sel = $("#atlas-profile");
  try{
    const data = await fetchJson("/api/v1/profiles");
    const profiles = data.profiles || [];
    // keep default always
    const ids = new Set(["default", ...profiles.map(p=>p.profile_id)]);
    sel.innerHTML = `<option value="">Pick a profile to explore</option>` + Array.from(ids).map(id=>`<option value="${esc(id)}">${esc(id)}</option>`).join("");
    sel.value = atlasState.profile;
  } catch(e){
    // keep default option, show error in banner
  }
}

function ensureAtlasLoaded(){
  // called when route becomes atlas
  applyHashToState();
  syncAtlasControlsFromState();
  syncAtlasViewMode();
  loadProfiles().then(()=>{ fetchAtlas(); });
}

function syncAtlasControlsFromState(){
  $("#atlas-profile").value = atlasState.profile || "";
  $("#f-kind").value = atlasState.kind;
  $("#f-type").value = atlasState.type;
  $("#f-decay").value = atlasState.decay;
  $("#f-time").value = atlasState.time;
  $("#f-entities").value = atlasState.entities;
  $("#f-density").value = atlasState.density;
  $("#time-custom-row").hidden = atlasState.time!=="custom";
  // Type visibility: No dead inputs — hide Type when Kind===Chunks
  $("#field-type").hidden = atlasState.kind==="chunks";
}

function syncAtlasViewMode(){
  const is3d = atlasState.viewMode==="3d";
  $("#mode-3d").classList.toggle("is-active", is3d);
  $("#mode-3d").setAttribute("aria-pressed", String(is3d));
  $("#mode-list").classList.toggle("is-active", !is3d);
  $("#mode-list").setAttribute("aria-pressed", String(!is3d));
  // stage toggles — but 3D fallback forces 2d canvas even in 3d mode; list is independent
  $("#atlas-list-wrap").hidden = is3d;
  $("#atlas-canvas-wrap").hidden = !is3d;
  if(!is3d) renderList();
  else renderCanvas();
}

function buildAtlasRequestBody(){
  const filter = {};
  if(atlasState.type!=="all" && atlasState.kind!=="chunks") filter.node_types = [atlasState.type];
  // decay -> min/max
  if(atlasState.decay==="healthy"){ filter.min_decay = 0.4; }
  else if(atlasState.decay==="rescue"){ filter.min_decay = 0.15; filter.max_decay = 0.4; }
  else if(atlasState.decay==="fading"){ filter.max_decay = 0.15; }
  else if(atlasState.decay==="never"){ filter.flags = ["never_decay"]; }
  // time
  const now = Date.now();
  if(atlasState.time==="7d") filter.ingested_after = new Date(now - 7*86400000).toISOString();
  else if(atlasState.time==="30d") filter.ingested_after = new Date(now - 30*86400000).toISOString();
  else if(atlasState.time==="90d") filter.ingested_after = new Date(now - 90*86400000).toISOString();
  else if(atlasState.time==="custom"){
    if(atlasState.timeFrom) filter.ingested_after = new Date(atlasState.timeFrom).toISOString();
    if(atlasState.timeTo) filter.ingested_before = new Date(atlasState.timeTo).toISOString();
  }
  if(atlasState.entities.trim()){
    filter.entities = atlasState.entities.split(",").map(s=>s.trim()).filter(Boolean);
  }
  const body = {
    profile_id: atlasState.profile || "default",
    kind: atlasState.kind==="all" ? "both" : atlasState.kind,
    filter,
    sort: atlasState.sort || "recent",
    offset: 0,
    limit: 500,
  };
  return body;
}

async function fetchAtlas(){
  const countEl = $("#atlas-count"), bannerErr = $("#atlas-banner-error"), bannerTrunc = $("#atlas-banner-truncated");
  const bannerFallback = $("#atlas-banner-fallback"), loading = $("#atlas-loading");
  const statusbar = $("#atlas-statusbar");
  const empty = $("#atlas-empty"), filteredEmpty = $("#atlas-filtered-empty"), listEmpty = $("#list-empty");
  const profile = atlasState.profile;

  // profile required
  if(!profile){
    countEl.textContent = "Pick a profile to explore";
    bannerErr.hidden = true;
    bannerTrunc.hidden = true;
    empty.hidden = false;
    empty.querySelector(".empty-title").textContent = "Pick a profile to explore";
    statusbar.textContent = "";
    return;
  }
  empty.hidden = true;
  filteredEmpty.hidden = true;
  listEmpty.hidden = true;
  bannerErr.hidden = true;
  bannerTrunc.hidden = true;

  // density cap
  const densityCap = atlasState.density==="auto" ? 2000 : parseInt(atlasState.density,10);

  loading.hidden = false;
  countEl.textContent = "Loading your memories…";
  statusbar.textContent = "";

  // abort previous
  if(atlasState._fetchAbort) atlasState._fetchAbort.abort();
  const ac = new AbortController();
  atlasState._fetchAbort = ac;

  try{
    const body = buildAtlasRequestBody();
    const data = await fetchJson("/memory/atlas", {
      method:"POST",
      headers:{"content-type":"application/json"},
      body: JSON.stringify(body),
      signal: ac.signal,
    });
    // contract: {items, total, offset, limit, window_truncated, positions, algo}
    atlasState.items = Array.isArray(data.items) ? data.items : [];
    atlasState.total = data.total ?? atlasState.items.length;
    atlasState.window_truncated = !!data.window_truncated;
    atlasState.positions = data.positions || null;
    atlasState.algo = data.algo || null;

    // Honest window_truncated amber banner
    if(atlasState.window_truncated){
      bannerTrunc.hidden = false;
      $("#truncated-detail").textContent = `${fmtNum(atlasState.total)} total · showing ${fmtNum(atlasState.items.length)} · +${fmtNum(Math.max(0, atlasState.total - atlasState.items.length))} hidden — tighten filters or raise Density`;
    }

    // degraded null+unavailable -> hash fallback already prepared in positions==null handling
    if(atlasState.positions==null && atlasState.algo==="unavailable"){
      // keep fallback flag but don't show 2.5D fallback unless 3D actually fails; hash positions will be used
      statusbar.textContent = `Degraded — no dense vectors, using hash layout. · ${fmtNum(atlasState.total)} total`;
    }

    // density: deterministic sampling if over cap
    if(atlasState.items.length > densityCap){
      const keep = new Set();
      // deterministic sampling: keep hash(id) % step
      const step = Math.ceil(atlasState.items.length / densityCap);
      // For determinism, sort by id hash
      const sorted = [...atlasState.items].sort((a,b)=> hash01(a.id||a.chunk_id||a.node_id) - hash01(b.id||b.chunk_id||b.node_id));
      for(let i=0;i<sorted.length;i+=step){ keep.add(sorted[i].id||sorted[i].chunk_id||sorted[i].node_id); if(keep.size>=densityCap) break; }
      atlasState.items = atlasState.items.filter(it=> keep.has(it.id||it.chunk_id||it.node_id));
      bannerTrunc.hidden = false;
      // append hidden count
      const hidden = (data.total ?? atlasState.items.length) - atlasState.items.length;
      if(hidden>0) $("#truncated-detail").textContent = `${fmtNum(data.total)} total · +${fmtNum(hidden)} hidden — tighten filters or raise Density`;
    }

    // counts
    const rescue = atlasState.items.filter(it=> decayBand(it.decay_weight) === "rescue").length;
    const fading = atlasState.items.filter(it=> decayBand(it.decay_weight) === "fading").length;
    countEl.textContent = `${fmtNum(atlasState.total)} items · Rescue ${fmtNum(rescue)} · Fading ${fmtNum(fading)}`;
    if(atlasState.items.length===0){
      filteredEmpty.hidden = false;
      listEmpty.hidden = false;
    }

    // statusbar
    if(!bannerTrunc.hidden) statusbar.textContent = ($("#truncated-detail").textContent || "") ;
    else statusbar.textContent = `window_truncated=${atlasState.window_truncated} · ${fmtNum(atlasState.items.length)} shown`;

    pushAtlasHash();
    // render stage
    if(atlasState.viewMode==="list") renderList();
    else await renderCanvas();

    // keep selected if still present
    if(atlasState.selectedId && !atlasState.items.find(it=> (it.id||it.chunk_id||it.node_id)===atlasState.selectedId)){
      // keep drawer but mark not found
    } else if(atlasState.selectedId){
      openDrawer(atlasState.selectedId);
    }

  } catch(e){
    if(e.name==="AbortError") return;
    const msg = e.status===404 ? "Atlas API not yet available — backend POST /memory/atlas is pending (C-1 T2). Your filters still work once the endpoint lands." : (e.message||"network error");
    bannerErr.textContent = `Couldn’t load memories — ${msg}. `;
    const retry = document.createElement("button");
    retry.className="btn btn-sm"; retry.textContent="Retry"; retry.addEventListener("click", fetchAtlas);
    bannerErr.appendChild(retry);
    bannerErr.hidden = false;
    empty.hidden = false;
    countEl.textContent = "Couldn’t load";
    loading.hidden = true;
    statusbar.textContent = `Error ${e.status||""} — ${msg}`;
    // fallback demo: if 404, keep existing items empty but don't break layout
    return;
  } finally {
    loading.hidden = true;
  }
}

// ---------- 3D + 2.5D rendering ----------
async function renderCanvas(){
  const wrap = $("#atlas-canvas-wrap");
  const canvas3d = $("#atlas-canvas");
  const canvas2d = $("#atlas-canvas-2d");
  const bannerFallback = $("#atlas-banner-fallback");
  if(!wrap) return;

  // decide: try 3D first unless we are already in fallback
  if(atlasState.fallback2d){
    canvas3d.hidden = true;
    canvas2d.hidden = false;
    bannerFallback.hidden = false;
    render2d();
    return;
  }

  // attempt dynamic import Three
  let THREE, OrbitCtor;
  try{
    // Use import map; may fail offline/CSP
    const mod = await import("three");
    THREE = mod;
    const addons = await import("three/addons/controls/OrbitControls.js");
    OrbitCtor = addons.OrbitControls;
  } catch(e){
    atlasState.fallback2d = true;
    canvas3d.hidden = true;
    canvas2d.hidden = false;
    bannerFallback.hidden = false;
    bannerFallback.firstChild.textContent = "3D unavailable — showing 2.5D fallback. ";
    render2d();
    a11yLive("3D unavailable — showing 2.5D fallback");
    return;
  }

  bannerFallback.hidden = true;
  canvas3d.hidden = false;
  canvas2d.hidden = true;
  try{
    await render3d(THREE, OrbitCtor);
  } catch(e){
    // any render failure -> fallback
    console.warn("3d render failed, falling back", e);
    atlasState.fallback2d = true;
    canvas3d.hidden = true;
    canvas2d.hidden = false;
    bannerFallback.hidden = false;
    render2d();
  }
}

function colorForItem(item){
  const map = {
    USER: getComputedStyle(document.documentElement).getPropertyValue("--atlas-c1").trim() || "#4F7CAC",
    HABIT: "var(--atlas-c2)", HABIT_c: "#5B8C5A",
    PREFERENCE: "#8A6BC9", ANIMA: "#CC6B8A", INTENTION: "#D9983A", CONSTRAINT:"#7AB0C2",
    EPISODE:"#6EC1A0", SKILL_SEQUENCE:"#9BB55A", DECISION:"#BE7AC9", PROJECT:"#8BBEE0", TOOL:"#E0B56A"
  };
  // We'll return hex directly via palette
  const palette = {
    USER:"#4F7CAC", HABIT:"#5B8C5A", PREFERENCE:"#8A6BC9", ANIMA:"#CC6B8A", INTENTION:"#D9983A",
    CONSTRAINT:"#7AB0C2", EPISODE:"#6EC1A0", SKILL_SEQUENCE:"#9BB55A", DECISION:"#BE7AC9", PROJECT:"#8BBEE0", TOOL:"#E0B56A"
  };
  if(item.kind==="chunk" || item.kind==="chunks") return item.flags?.explicit_pin ? "#E07A5F" : "#9AA0A6";
  const t = item.node_type || item.type || "USER";
  return palette[t] || "#4F7CAC";
}
function hexToRgb(hex){
  const h = hex.replace("#","").trim();
  const n = parseInt(h,16);
  return [(n>>16)&255, (n>>8)&255, n&255];
}

let threeState = null;
async function render3d(THREE, OrbitControls){
  const canvas = $("#atlas-canvas");
  const wrap = $("#atlas-canvas-wrap");
  if(!canvas || atlasState.items.length===0){
    // empty: nothing to render, keep canvas dark
    if(threeState && threeState.renderer){
      threeState.renderer.dispose();
      threeState = null;
    }
    return;
  }
  // dispose previous
  if(threeState && threeState.renderer){
    window.removeEventListener("resize", threeState.onResize);
    threeState.renderer.dispose();
    threeState = null;
  }

  const rect = wrap.getBoundingClientRect();
  const w = Math.max(320, rect.width - 2);
  const h = 520;
  canvas.width = w * window.devicePixelRatio;
  canvas.height = h * window.devicePixelRatio;
  canvas.style.width = w+"px";
  canvas.style.height = h+"px";

  const renderer = new THREE.WebGLRenderer({canvas, antialias:true, alpha:false});
  renderer.setPixelRatio(window.devicePixelRatio);
  renderer.setSize(w,h,false);
  renderer.setClearColor(0x0F1115, 1);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(52, w/h, 0.1, 100);
  camera.position.set(0, 1.6, 4.2);

  const controls = new OrbitControls(camera, canvas);
  controls.enableDamping = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  controls.dampingFactor = 0.08;
  controls.minDistance = 1.2;
  controls.maxDistance = 12;
  controls.maxPolarAngle = Math.PI * 0.48;

  // lights — subtle
  const dir = new THREE.DirectionalLight(0xffffff, 0.7); dir.position.set(2,4,3); scene.add(dir);
  scene.add(new THREE.AmbientLight(0xffffff, 0.55));

  // build points
  const n = atlasState.items.length;
  const geom = new THREE.BufferGeometry();
  const positions = new Float32Array(n*3);
  const colors = new Float32Array(n*3);
  const sizes = new Float32Array(n);

  // map items -> positions: use backend positions if available, else hash fallback
  for(let i=0;i<n;i++){
    const it = atlasState.items[i];
    const id = it.id || it.chunk_id || it.node_id || String(i);
    let x,y,z;
    if(atlasState.positions && atlasState.positions[id]){
      const p = atlasState.positions[id];
      x = p[0]; y = p[1]; z = p[2];
    } else if(atlasState.positions && Array.isArray(atlasState.positions[id])){ // just in case
      [x,y,z]=atlasState.positions[id];
    } else {
      [x,y,z] = hashPos(id);
      // y is time already [0,1], but for 3d we want y up; spec says y=time, so keep
      // For hash fallback keep same
    }
    // clamp y to [0,1], z from decay already [0,1]
    positions[i*3]= x;
    positions[i*3+1]= y*2 -1; // map y [0,1] -> [-1,1] for camera friendliness
    positions[i*3+2]= z*2 -1;

    const hex = colorForItem(it);
    const [r,g,b] = hexToRgb(hex);
    colors[i*3]= r/255; colors[i*3+1]= g/255; colors[i*3+2]= b/255;

    let s = 6;
    if(it.score!=null) s = 5 + clamp(Number(it.score)*2, 0, 8);
    else if(it.hit_count!=null) s = 5 + clamp(Math.log10(Number(it.hit_count)+1)*3, 0, 7);
    // rescue band: shrink 30%
    const wgt = it.decay_weight;
    if(wgt!=null && wgt>=0.15 && wgt<0.4) s *= 0.7;
    sizes[i]= s;
  }
  geom.setAttribute("position", new THREE.BufferAttribute(positions,3));
  geom.setAttribute("color", new THREE.BufferAttribute(colors,3));
  geom.setAttribute("size", new THREE.BufferAttribute(sizes,1));

  // PointsMaterial with vertex colors + size attenuation via shader
  const mat = new THREE.PointsMaterial({
    size: 0.07,
    vertexColors: true,
    sizeAttenuation: true,
    transparent: true,
    opacity: 0.92,
    depthWrite: false,
  });
  // apply per-point opacity via custom? simplest: overall opacity mapped from decay via second pass dim
  const points = new THREE.Points(geom, mat);
  points.frustumCulled = true;
  scene.add(points);

  // subtle grid on floor
  const grid = new THREE.GridHelper(4, 12, 0x1F2937, 0x1F2937);
  grid.position.y = -1.2;
  grid.material.opacity = 0.35;
  grid.material.transparent = true;
  scene.add(grid);

  // raycaster picking
  const raycaster = new THREE.Raycaster();
  raycaster.params.Points.threshold = 0.08;
  const mouse = new THREE.Vector2();
  let hoverId = null;

  function pick(event){
    const rect = canvas.getBoundingClientRect();
    mouse.x = ((event.clientX - rect.left)/rect.width)*2 -1;
    mouse.y = -((event.clientY - rect.top)/rect.height)*2 +1;
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObject(points);
    if(hits.length){
      const idx = hits[0].index;
      if(idx!=null) return atlasState.items[idx];
    }
    return null;
  }

  const hoverCard = $("#atlas-hover");
  canvas.addEventListener("mousemove", (e)=>{
    const it = pick(e);
    if(!it){
      hoverCard.hidden = true;
      canvas.style.cursor = "grab";
      return;
    }
    canvas.style.cursor = "pointer";
    const id = it.id || it.chunk_id || it.node_id;
    hoverCard.hidden = false;
    hoverCard.style.left = (e.offsetX)+"px";
    hoverCard.style.top = (e.offsetY)+"px";
    const title = (it.text_head || it.text || it.props?.statement || it.statement || id || "").slice(0,80);
    const decay = it.decay_weight!=null ? `Decay ${Number(it.decay_weight).toFixed(2)}` : "Decay —";
    const time = fmtRel(it.ingested_at || it.valid_from || it.updated_at);
    hoverCard.innerHTML = `
      <p class="hc-title">${esc(title)}</p>
      <div class="hc-meta">
        <span class="badge">${esc(it.kind||"")}</span>
        ${it.node_type?`<span class="badge badge-muted">${esc(it.node_type)}</span>`:""}
        ${it.flags?.conflict?`<span class="badge badge-warn">Conflict</span>`:""}
        ${it.flags?.pending?`<span class="badge badge-pending">Pending</span>`:""}
        ${it.flags?.explicit_pin?`<span class="badge badge-pin">Pinned</span>`:""}
      </div>
      <div class="small muted" style="margin-top:6px; display:flex; gap:8px; align-items:center">
        <span class="progress" style="flex:1"><span class="progress-bar ${decayBand(it.decay_weight)}" style="width:${clamp((it.decay_weight||0)*100,0,100)}%"></span></span>
        <span>${esc(decay)}</span>
      </div>
      <p class="small muted" style="margin:4px 0 0">${esc(time)} · <span title="${esc(fmtISO(it.ingested_at||it.valid_from))}">${esc((it.entities||[]).slice(0,3).join(", ")||"no entities")}</span></p>
    `;
  });
  canvas.addEventListener("mouseleave", ()=>{ hoverCard.hidden = true; });
  canvas.addEventListener("click", (e)=>{
    const it = pick(e);
    if(!it) return;
    const id = it.id || it.chunk_id || it.node_id;
    atlasState.selectedId = id;
    pushAtlasHash();
    openDrawer(id);
    // animate focus: lerp camera target slightly
  });

  // keyboard
  wrap.addEventListener("keydown", (e)=>{
    if(e.key==="f"||e.key==="F"){
      if(atlasState.selectedId){
        // find index and focus
        const idx = atlasState.items.findIndex(it=> (it.id||it.chunk_id||it.node_id)===atlasState.selectedId);
        if(idx>=0){
          const x = positions[idx*3], y = positions[idx*3+1], z = positions[idx*3+2];
          controls.target.set(x,y,z);
          camera.position.set(x+0.6, y+0.6, z+1.2);
          controls.update();
        }
      }
    }
    if(e.key==="r"||e.key==="R"){ controls.reset(); }
    if(e.key==="Escape"){ atlasState.selectedId=null; pushAtlasHash(); closeDrawerSoft(); }
  });

  function animate(){
    if(!threeState || threeState.renderer!==renderer) return;
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }
  function onResize(){
    const r = wrap.getBoundingClientRect();
    const nw = Math.max(320, r.width-2);
    camera.aspect = nw/h;
    camera.updateProjectionMatrix();
    renderer.setSize(nw,h,false);
    canvas.style.width = nw+"px";
  }
  window.addEventListener("resize", onResize);
  threeState = {renderer, scene, camera, controls, onResize, geom, mat, points};

  // honor reduced-motion
  if(window.matchMedia("(prefers-reduced-motion: reduce)").matches){
    controls.enableDamping = false;
  }

  animate();
}

function render2d(){
  const canvas = $("#atlas-canvas-2d");
  const wrap = $("#atlas-canvas-wrap");
  if(!canvas) return;
  const rect = wrap.getBoundingClientRect();
  const w = Math.max(320, rect.width - 2);
  const h = 520;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = w*dpr;
  canvas.height = h*dpr;
  canvas.style.width = w+"px";
  canvas.style.height = h+"px";
  const ctx = canvas.getContext("2d");
  if(!ctx){ return; }
  atlas2dCtx = ctx;
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.clearRect(0,0,w,h);
  // bg
  ctx.fillStyle = "#0F1115";
  ctx.fillRect(0,0,w,h);
  // subtle grid isometric
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 1;
  for(let i=0;i<8;i++){
    const y = 40 + i*60;
    ctx.beginPath(); ctx.moveTo(20, y); ctx.lineTo(w-20, y+18); ctx.stroke();
  }

  if(atlasState.items.length===0){
    ctx.fillStyle = "#94A3B8";
    ctx.font = "12px system-ui";
    ctx.textAlign="center";
    ctx.fillText("No points — adjust filters", w/2, h/2);
    return;
  }

  // project: x' = x - z*0.5, y' = y - z*0.5, size 1/(1+z)
  const pts = atlasState.items.map(it=>{
    const id = it.id||it.chunk_id||it.node_id||"";
    let x,y,z;
    if(atlasState.positions && atlasState.positions[id]){ const p=atlasState.positions[id]; x=p[0]; y=p[1]; z=p[2]; }
    else {[x,y,z]=hashPos(id);}
    const isoX = x - z*0.5;
    const isoY = y - z*0.5;
    return {it, x:isoX, y:isoY, z, id};
  });
  // depth sort back-to-front (z ascending: farther first)
  pts.sort((a,b)=> a.z - b.z);

  // map iso [-1,1] to canvas
  const pad = 24;
  const mapX = (v)=> pad + (v+1)/2 * (w - pad*2);
  const mapY = (v)=> pad + (v+1)/2 * (h - 40); // keep 40 for legend area

  // hover tracking for 2.5d pick
  let hover = null;
  const hoverCard = $("#atlas-hover");
  function draw(){
    ctx.clearRect(0,0,w,h);
    ctx.fillStyle="#0F1115"; ctx.fillRect(0,0,w,h);
    for(const p of pts){
      const cx = mapX(p.x), cy = mapY(p.y);
      const scale = 1/(1+p.z*0.6);
      const r = (p.it.score!=null ? 3+clamp(Number(p.it.score),0,3) : (p.it.hit_count!=null? 3+clamp(Math.log10(Number(p.it.hit_count)+1),0,2):3)) * scale * 1.6;
      const hex = colorForItem(p.it);
      const [rr,gg,bb] = hexToRgb(hex);
      const alpha = 0.35 + 0.65 * (p.it.decay_weight ?? 0.5);
      ctx.globalAlpha = alpha;
      ctx.fillStyle = `rgb(${rr} ${gg} ${bb})`;
      ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI*2); ctx.fill();
      // inner highlight
      ctx.globalAlpha = Math.min(1, alpha+0.15);
      ctx.fillStyle="rgba(255,255,255,0.18)";
      ctx.beginPath(); ctx.arc(cx- r*0.25, cy- r*0.25, r*0.35, 0, Math.PI*2); ctx.fill();
      // conflict ring
      if(p.it.flags?.conflict){
        ctx.globalAlpha=0.95; ctx.strokeStyle="#FB923C"; ctx.lineWidth=1.6;
        ctx.beginPath(); ctx.arc(cx, cy, r+3, 0, Math.PI*2); ctx.stroke();
      }
      if(p.it.flags?.pending){
        ctx.strokeStyle="#A78BFA"; ctx.setLineDash([3,3]); ctx.beginPath(); ctx.arc(cx, cy, r+5, 0, Math.PI*2); ctx.stroke(); ctx.setLineDash([]);
      }
      // store screen pos for picking
      p.cx=cx; p.cy=cy; p.r=r;
    }
    ctx.globalAlpha=1;
  }
  draw();

  // picking: nearest point within 14px
  function nearest(mx,my){
    let best=null, bestD=1e9;
    for(const p of pts){
      const dx=mx-p.cx, dy=my-p.cy;
      const d=Math.hypot(dx,dy);
      if(d < Math.max(14, p.r+6) && d<bestD){ best=p; bestD=d; }
    }
    return best;
  }
  canvas.onmousemove = (e)=>{
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const p = nearest(mx,my);
    if(!p){ hover=null; hoverCard.hidden=true; canvas.style.cursor="default"; return; }
    hover=p;
    canvas.style.cursor="pointer";
    hoverCard.hidden=false;
    hoverCard.style.left = (mx)+"px";
    hoverCard.style.top = (my)+"px";
    const it=p.it;
    const title=(it.text_head||it.text||it.props?.statement||p.id||"").slice(0,80);
    hoverCard.innerHTML=`<p class="hc-title">${esc(title)}</p><div class="hc-meta"><span class="badge">${esc(it.kind||"")}</span>${it.node_type?`<span class="badge badge-muted">${esc(it.node_type)}</span>`:""}</div><p class="small muted" style="margin-top:6px">${esc(fmtRel(it.ingested_at||it.valid_from))} · ${esc((it.entities||[]).slice(0,3).join(", ")||"—")}</p>`;
  };
  canvas.onmouseleave = ()=>{ hoverCard.hidden=true; };
  canvas.onclick = (e)=>{
    const rect = canvas.getBoundingClientRect();
    const mx=e.clientX-rect.left, my=e.clientY-rect.top;
    const p=nearest(mx,my);
    if(!p) return;
    atlasState.selectedId=p.id; pushAtlasHash(); openDrawer(p.id);
  };
}

// ---------- List virtualization ----------
function renderList(){
  const wrap = $("#atlas-list"), spacer = $("#list-spacer"), vp = $("#list-viewport");
  const n = atlasState.items.length;
  const rowH = virtual.rowH;
  if(!wrap||!spacer||!vp) return;
  spacer.style.height = (n*rowH)+"px";
  // scroll handler
  function onScroll(){
    const scrollTop = wrap.scrollTop;
    const viewH = wrap.clientHeight;
    const start = Math.max(0, Math.floor(scrollTop/rowH) - virtual.overscan);
    const end = Math.min(n, Math.ceil((scrollTop+viewH)/rowH) + virtual.overscan);
    if(start===virtual.start && end===virtual.end) return;
    virtual.start=start; virtual.end=end;
    vp.style.transform = `translateY(${start*rowH}px)`;
    vp.innerHTML = atlasState.items.slice(start,end).map((it, idx)=>{
      const absIdx = start+idx;
      const id = it.id || it.chunk_id || it.node_id || String(absIdx);
      const isSel = atlasState.selectedId===id;
      const title = esc((it.text_head || it.text || it.props?.statement || id).slice(0,120));
      const type = esc(it.node_type || "—");
      const ents = (it.entities||[]).slice(0,2).map(esc).join(", ") + ((it.entities||[]).length>2 ? ` +${(it.entities.length-2)}` : "");
      const decay = it.decay_weight!=null ? Number(it.decay_weight).toFixed(2) : "—";
      const decayCls = decayBand(it.decay_weight);
      const barW = clamp((it.decay_weight||0)*100,0,100);
      const barCls = decayCls==="healthy" ? "is-healthy" : decayCls==="rescue" ? "is-rescue" : decayCls==="fading" ? "is-fading" : "";
      const updated = esc(fmtRel(it.ingested_at||it.valid_from||it.updated_at));
      const updatedTitle = esc(fmtISO(it.ingested_at||it.valid_from));
      const flags = [];
      if(it.flags?.conflict) flags.push('<span class="badge badge-warn" title="Conflict">●</span>');
      if(it.flags?.pending) flags.push('<span class="badge badge-pending" title="Pending">◍</span>');
      if(it.flags?.needs_reconcile) flags.push('<span class="badge badge-muted">RC</span>');
      if(it.flags?.explicit_pin) flags.push('<span class="badge badge-pin">PIN</span>');
      const hits = it.hit_count ?? (it.score!=null ? Number(it.score).toFixed(2) : "—");
      const kindIcon = it.kind==="chunks"||it.kind==="chunk" ? "⬡" : "●";
      return `<div class="list-row ${isSel?"is-selected":""}" role="row" tabindex="0" data-id="${esc(id)}" data-idx="${absIdx}" aria-selected="${isSel}">
        <span class="col col-kind" role="cell">${kindIcon} ${esc(it.kind||"")}${it.flags?.explicit_pin?' <span class="badge badge-pin" style="padding:0 4px; font-size:9px">P</span>':""}</span>
        <span class="col col-title" role="cell" title="${title}">${title}</span>
        <span class="col col-type" role="cell">${type}</span>
        <span class="col col-entities" role="cell" title="${esc((it.entities||[]).join(", "))}">${esc(ents||"—")}</span>
        <span class="col col-decay" role="cell"><span class="progress" style="width:54px; display:inline-block; vertical-align:middle"><span class="progress-bar ${barCls}" style="width:${barW}%"></span></span> <span style="font-variant-numeric: tabular-nums">${decay}</span></span>
        <span class="col col-updated" role="cell" title="${updatedTitle}">${updated}</span>
        <span class="col col-flags" role="cell">${flags.join(" ")||"—"}</span>
        <span class="col col-hits" role="cell" style="font-variant-numeric: tabular-nums">${esc(String(hits))}</span>
      </div>`;
    }).join("");

    // row click
    $$("#list-viewport .list-row").forEach(row=>{
      row.addEventListener("click", ()=>{
        const id=row.dataset.id;
        atlasState.selectedId=id; pushAtlasHash();
        openDrawer(id);
        // update selection visuals
        $$("#list-viewport .list-row").forEach(r=> r.classList.toggle("is-selected", r.dataset.id===id));
      });
      row.addEventListener("keydown",(e)=>{
        if(e.key==="Enter"){ row.click(); }
      });
    });

  }
  wrap.onscroll = onScroll;
  // initial
  onScroll();
  // also observe resize
  new ResizeObserver(onScroll).observe(wrap);
}

// ---------- Drawer ----------
function closeDrawerSoft(){
  $("#drawer-content").hidden = true;
  $("#drawer-empty").hidden = false;
}
function openDrawer(id){
  const it = atlasState.items.find(x=> (x.id||x.chunk_id||x.node_id)===id);
  if(!it){
    // still show unknown
    $("#drawer-empty").hidden = false;
    $("#drawer-content").hidden = true;
    return;
  }
  $("#drawer-empty").hidden = true;
  $("#drawer-content").hidden = false;
  // Header
  $("#d-title").textContent = (it.text_head || it.text || it.props?.statement || it.statement || id);
  $("#d-kind").textContent = it.kind || "—";
  $("#d-type").textContent = it.node_type || it.type || "—";
  $("#d-pinned").hidden = !it.flags?.explicit_pin;
  $("#d-consolidated").hidden = !it.flags?.consolidated;
  $("#d-version").textContent = it.version ? `v${it.version}` : (it.valid_from ? "current" : "");
  const short = id.slice(0,8);
  const idEl = $("#d-id");
  idEl.textContent = `ID ${short}… (click to copy full)`;
  idEl.title = id;
  idEl.onclick = async ()=>{
    try{ await navigator.clipboard.writeText(id); a11yLive("Copied!"); idEl.textContent="Copied!"; setTimeout(()=> idEl.textContent=`ID ${short}… (click to copy full)`, 1200); } catch{ a11yLive("Copy failed"); }
  };
  $("#d-profile").textContent = `profile ${atlasState.profile}`;
  // session link: if session_id present, link to recent tail (honest)
  const sess = it.session_id || it.provenance?.session_id || "";
  $("#d-session").innerHTML = sess ? ` · Session <code>${esc(sess.slice(0,8))}</code> — <a href="#" onclick="return false" title="View recent tail via POST /session/recent">view recent tail</a>` : "";
  const tVal = it.ingested_at || it.valid_from || it.updated_at;
  $("#d-time").textContent = tVal ? `${fmtISO(tVal)} · ${fmtRel(tVal)}` : "—";
  $("#d-time").title = tVal ? fmtISO(tVal) : "";

  // Provenance
  const prov = it.provenance || {};
  $("#d-asserted-by").textContent = prov.asserted_by || it.asserted_by || "—";
  $("#d-source").textContent = prov.source || it.source || "—";
  $("#d-confidence").textContent = (prov.confidence ?? it.confidence ?? "—").toString();
  $("#d-asserted-at").textContent = prov.asserted_at ? fmtISO(prov.asserted_at) : (it.asserted_at?fmtISO(it.asserted_at):"—");
  const hist = prov.history || it.history || [];
  const histEl = $("#d-history");
  if(hist.length){
    histEl.innerHTML = hist.slice(0,6).map(h=> `<div class="kv" style="padding:6px 8px"><span class="small muted">${esc(h.at?fmtISO(h.at):"—")} · ${esc(h.action||h.actor||"—")}</span><span class="small">${esc(h.detail||"")}</span></div>`).join("");
  } else {
    histEl.innerHTML = `<p class="small muted">No history — ${esc(it.source||"—")}</p>`;
  }
  $("#d-gaps").hidden = !it.flags?.peripheral_gaps && !it.peripheral_gaps;
  $("#d-promotion").hidden = !(it.promotion_status==="promoted" || it.flags?.promotion);

  // Decay
  const w = it.decay_weight;
  $("#d-decay-val").textContent = w!=null ? Number(w).toFixed(2) : "—";
  const band = decayBand(w);
  const labelEl = $("#d-decay-label");
  labelEl.textContent = decayLabel(w);
  labelEl.className = "badge " + (band==="healthy"?"": band==="rescue"?"badge-warn": band==="fading"?"badge-fading":"");
  const bar = $("#d-decay-bar");
  bar.style.width = `${clamp((w||0)*100,0,100)}%`;
  bar.className = "progress-bar " + (band==="healthy"?"is-healthy": band==="rescue"?"is-rescue": band==="fading"?"is-fading":"");
  // λ from spec: chunk 0.03, pin 0.005, fact 0.01 etc — we show generic
  const lambdaMap = { USER:0.012, HABIT:0.01, PREFERENCE:0.008, ANIMA:0.01, INTENTION:0.012, CONSTRAINT:0.005, EPISODE:0.03, SKILL_SEQUENCE:0.01, DECISION:0.01, PROJECT:0.01, TOOL:0.01 };
  const lam = it.flags?.explicit_pin ? 0.005 : (lambdaMap[it.node_type] ?? 0.01);
  $("#d-lambda").textContent = `λ = ${lam} (${esc(it.node_type||it.kind||"chunk")})`;
  const half = Math.log(2)/lam;
  $("#d-half").textContent = `Half-life ~${Math.round(half)} days`;
  $("#d-last-reinforced").textContent = it.last_reinforced ? `Last reinforced ${fmtRel(it.last_reinforced)}` : "Last reinforced —";
  $("#d-reinforce-count").textContent = it.reinforce_count!=null ? `Reinforced ${it.reinforce_count} times` : "Reinforced —";
  $("#d-hit-count").textContent = it.hit_count!=null ? `Hit ${it.hit_count} times · Last hit ${fmtRel(it.last_hit_at)}` : (it.score!=null?`Score ${it.score}`:"Hits —");
  // forecast 30/90
  function forecast(days){
    if(w==null) return "—";
    const v = (it.confidence ?? 1) * Math.exp(-lam*days);
    // simplified: w * exp(-lam*days) approximated
    const f = w * Math.exp(-lam*days);
    return f.toFixed(2);
  }
  $("#d-forecast-30").textContent = forecast(30);
  $("#d-forecast-90").textContent = forecast(90);

  // Scores
  const scoreEl = $("#d-score");
  scoreEl.textContent = it.score!=null ? `Score ${Number(it.score).toFixed(2)}` : (it.confidence!=null?`Confidence ${it.confidence}`:"Score —");
  $("#d-rescued").hidden = !(w!=null && w>=0.15 && w<0.4);
  $("#d-pending-note").hidden = !it.flags?.pending && !it.pending_consolidation;
  $("#d-score-note").hidden = it.score!=null;

  // Graph
  const edgesEl = $("#d-edges");
  if(it.node_type){
    edgesEl.innerHTML = `<li class="muted">Graph edges load on demand — showing placeholder for 1-hop neighbors.</li>`;
    // attempt to fetch edges if backend exists (optional)
    // we don't have edge endpoint wired for Atlas yet; keep honest placeholder
  } else {
    edgesEl.innerHTML = `<li class="muted">Chunks have no graph edges — edges are for nodes.</li>`;
  }

  // Timeline
  const tlEl = $("#d-timeline"), noTl = $("#d-no-timeline");
  if(it.node_type){
    // valid_from chain: show current version
    tlEl.hidden = false;
    noTl.hidden = true;
    tlEl.innerHTML = `<li><strong>v${esc(String(it.version||"?"))} current</strong> · Valid ${esc(fmtISO(it.valid_from))} → now</li>`;
    // if we could fetch POST /memory/timeline, would list chain; placeholder honest
    if(it.version && Number(it.version)>1){
      tlEl.innerHTML += `<li class="muted">Earlier versions available via POST /memory/timeline {node_id}</li>`;
    }
    $("#d-turns").textContent = "";
  } else {
    tlEl.hidden = true;
    noTl.hidden = false;
    const turns = (it.turn_start!=null && it.turn_end!=null) ? `Turns ${it.turn_start}→${it.turn_end}` : "";
    $("#d-turns").textContent = turns;
  }

  // Audit
  const auditEl = $("#d-audit");
  auditEl.innerHTML = `<li class="muted">Audit for this item — POST /memory/audit {node_id|chunk_id} (3 rows max here, View all in Audit)</li>`;

  // Actions wire (idempotent: re-assign onclick)
  $("#a-copy-text").onclick = async ()=>{
    const txt = it.text || it.text_head || it.props?.statement || "";
    if(!txt) return a11yLive("Nothing to copy");
    try{ await navigator.clipboard.writeText(txt); a11yLive("Copied."); $("#action-feedback").textContent="Copied."; setTimeout(()=>$("#action-feedback").textContent="",1500);} catch{ a11yLive("Copy failed — select and copy manually"); }
  };
  $("#a-copy-id").onclick = async ()=>{
    try{ await navigator.clipboard.writeText(id); a11yLive("Copied!"); $("#action-feedback").textContent="Copied!"; setTimeout(()=>$("#action-feedback").textContent="",1500);} catch{}
  };
  $("#a-export").onclick = async ()=>{
    try{
      const data = await fetchJson("/memory/export", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({profile_id: atlasState.profile, offset:0, limit:1})});
      const blob = new Blob([JSON.stringify(it,null,2)], {type:"application/json"});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href=url; a.download=`${id.slice(0,8)}.json`; a.click(); URL.revokeObjectURL(url);
      a11yLive("Exported.");
    } catch(e){
      // fallback: download local JSON without server
      const blob = new Blob([JSON.stringify(it,null,2)], {type:"application/json"});
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a"); a.href=url; a.download=`${id.slice(0,8)}.json`; a.click(); URL.revokeObjectURL(url);
      a11yLive("Exported (local).");
    }
  };
  // Pin again -> POST /memory/remember or /memory/reinforce
  $("#a-pin").onclick = async ()=>{
    if(!confirm("Pin again (reinforce) this memory?")) return;
    try{
      const txt = it.text || it.text_head || it.props?.statement || "";
      const res = await fetchJson("/memory/remember", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({profile_id: atlasState.profile, text: txt})});
      $("#action-feedback").textContent="Reinforced.";
      a11yLive("Reinforced.");
      fetchAtlas();
    } catch(e){
      // try reinforce verb
      try{
        await fetchJson("/memory/reinforce", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({profile_id: atlasState.profile, node_id: it.node_type? id: undefined, chunk_id: !it.node_type? id: undefined})});
        $("#action-feedback").textContent="Reinforced.";
        a11yLive("Reinforced.");
        fetchAtlas();
      } catch(e2){
        $("#action-feedback").textContent=`Couldn’t load memories — ${e2.message}`;
        a11yLive(`Couldn’t reinforce — ${e2.message}`);
      }
    }
  };
  $("#a-supersede").onclick = async ()=>{
    const dlg = $("#dlg-supersede");
    $("#supersede-input").value="";
    dlg.showModal();
    dlg.addEventListener("close", async ()=>{
      if(dlg.returnValue!=="confirm") return;
      const succ = $("#supersede-input").value.trim();
      if(!succ){ $("#action-feedback").textContent="Supersede requires a successor node ID."; return; }
      try{
        await fetchJson("/memory/supersede", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify({profile_id: atlasState.profile, superseded_node_id: id, successor_node_id: succ})});
        $("#action-feedback").textContent="Superseded.";
        a11yLive("Superseded."); fetchAtlas();
      } catch(e){ $("#action-feedback").textContent=`Couldn’t supersede — ${e.message}`; }
    }, {once:true});
  };
  $("#a-forget").onclick = async ()=>{
    const dlg = $("#dlg-forget");
    dlg.showModal();
    dlg.addEventListener("close", async ()=>{
      if(dlg.returnValue!=="confirm") return;
      try{
        const payload = it.node_type ? {profile_id: atlasState.profile, node_id: id} : {profile_id: atlasState.profile, chunk_id: id};
        await fetchJson("/memory/forget_this", {method:"POST", headers:{"content-type":"application/json"}, body: JSON.stringify(payload)});
        $("#action-feedback").textContent="Forgotten.";
        a11yLive("Forgotten."); atlasState.selectedId=null; pushAtlasHash(); closeDrawerSoft(); fetchAtlas();
      } catch(e){ $("#action-feedback").textContent=`Couldn’t forget — ${e.message}`; }
    }, {once:true});
  };

  // collapsible sections
  $$("#drawer-content .drawer-sec").forEach(sec=>{
    const btn = sec.querySelector(".drawer-sec-head");
    const body = sec.querySelector(".drawer-sec-body");
    if(!btn || !body) return;
    // ensure initial state matches is-open
    const open = sec.classList.contains("is-open");
    btn.setAttribute("aria-expanded", String(open));
    body.hidden = !open;
    btn.onclick = ()=>{
      const isOpen = sec.classList.toggle("is-open");
      btn.setAttribute("aria-expanded", String(isOpen));
      body.hidden = !isOpen;
    };
  });
}

// ---------- Drawer drag resize (320-480) ----------
function initDrawerDrag(){
  const drawer = $("#drawer");
  const handle = $("#drawer-drag");
  if(!drawer||!handle) return;
  let startX=0, startW=0, dragging=false;
  handle.addEventListener("pointerdown", (e)=>{
    dragging=true; startX=e.clientX; startW=drawer.getBoundingClientRect().width;
    handle.setPointerCapture(e.pointerId);
    document.body.style.userSelect="none";
  });
  handle.addEventListener("pointermove", (e)=>{
    if(!dragging) return;
    const dx = startX - e.clientX;
    const next = clamp(startW + dx, 320, 480);
    drawer.style.width = next+"px";
    document.documentElement.style.setProperty("--drawer-w", next+"px");
  });
  const up = (e)=>{
    if(!dragging) return;
    dragging=false;
    document.body.style.userSelect="";
    try{ handle.releasePointerCapture(e.pointerId);}catch{}
  };
  handle.addEventListener("pointerup", up);
  handle.addEventListener("pointercancel", up);
}

// ---------- Events ----------
function initEvents(){
  $("#overview-refresh").addEventListener("click", loadOverview);
  $("#atlas-refresh").addEventListener("click", fetchAtlas);
  $("#atlas-share").addEventListener("click", async ()=>{
    const url = location.href;
    try{ await navigator.clipboard.writeText(url); a11yLive("Copied link — Share view"); } catch{ a11yLive(url); }
  });

  // Config / Profiles / Dream tab wiring
  const cfgVerBtn = $("#config-versions-refresh");
  if(cfgVerBtn) cfgVerBtn.addEventListener("click", loadConfigVersions);
  $("#profiles-refresh").addEventListener("click", refreshProfilesList);
  $("#profile-create-form").addEventListener("submit", async (e)=>{
    e.preventDefault();
    const id = $("#profile-create-id").value.trim();
    const name = $("#profile-create-name").value.trim();
    const fb = $("#profile-create-feedback");
    if(!id){ fb.textContent = "Profile ID is required."; return; }
    fb.textContent = "Creating…";
    try{
      await fetchJson("/api/v1/profiles", {
        method:"POST", headers:{"content-type":"application/json", "x-mnemoseed-actor":"console"},
        body: JSON.stringify({ profile_id: id, display_name: name }),
      });
      fb.textContent = `Created ${id}.`;
      a11yLive(`Profile ${id} created`);
      $("#profile-create-id").value = ""; $("#profile-create-name").value = "";
      await refreshProfilesList();
      await refreshBindingsEditor();
    }catch(e){
      fb.textContent = e.status===409 ? `Profile "${id}" already exists.` : `Couldn’t create — ${e.message||"error"}`;
    }
  });
  $("#bindings-add").addEventListener("click", ()=>{
    // append an empty binding row
    const editor = $("#bindings-editor");
    if(!$$("#bindings-editor .binding-row").length && editor.querySelector(".muted")){
      editor.innerHTML = "";
    }
    const div = document.createElement("div");
    div.className = "binding-row";
    const profileIds = ["default", ...profilesStore.map(p=>p.profile_id)];
    div.innerHTML = `<input class="input binding-agent" type="text" placeholder="agent label (e.g. planner)" spellcheck="false"><select class="select binding-pid"><option value="">Pick a profile…</option>${profileIds.filter(unique).map(id=>`<option value="${esc(id)}">${esc(id)}</option>`).join("")}</select><button class="binding-remove" type="button" aria-label="Remove binding" title="Remove">×</button>`;
    editor.appendChild(div);
    div.querySelector(".binding-remove").addEventListener("click", ()=>{
      div.remove();
      if(!$$("#bindings-editor .binding-row").length) renderBindings([]);
    });
  });
  $("#bindings-save").addEventListener("click", saveBindings);
  initDreamEvents();
  $("#mode-3d").addEventListener("click", ()=>{ atlasState.viewMode="3d"; pushAtlasHash(); syncAtlasViewMode(); });
  $("#mode-list").addEventListener("click", ()=>{ atlasState.viewMode="list"; pushAtlasHash(); syncAtlasViewMode(); });
  $("#atlas-profile").addEventListener("change", (e)=>{ atlasState.profile=e.target.value; atlasState.selectedId=null; pushAtlasHash(); syncAtlasControlsFromState(); fetchAtlas(); });
  $("#f-kind").addEventListener("change", (e)=>{ atlasState.kind=e.target.value; pushAtlasHash(); syncAtlasControlsFromState(); debounceFetch(); });
  $("#f-type").addEventListener("change", (e)=>{ atlasState.type=e.target.value; pushAtlasHash(); debounceFetch(); });
  $("#f-decay").addEventListener("change", (e)=>{ atlasState.decay=e.target.value; pushAtlasHash(); debounceFetch(); });
  $("#f-time").addEventListener("change", (e)=>{
    atlasState.time=e.target.value;
    $("#time-custom-row").hidden = atlasState.time!=="custom";
    pushAtlasHash(); debounceFetch();
  });
  $("#f-time-from").addEventListener("change", (e)=>{ atlasState.timeFrom=e.target.value; debounceFetch(); });
  $("#f-time-to").addEventListener("change", (e)=>{ atlasState.timeTo=e.target.value; debounceFetch(); });
  let entTimer=null;
  $("#f-entities").addEventListener("input", (e)=>{
    clearTimeout(entTimer);
    entTimer=setTimeout(()=>{ atlasState.entities=e.target.value; pushAtlasHash(); debounceFetch(); }, 300);
  });
  // also support "/" focusing entities
  window.addEventListener("keydown", (e)=>{
    if(e.key==="/" && document.activeElement!==$("#f-entities") && currentRoute()==="atlas"){
      e.preventDefault(); $("#f-entities").focus();
    }
    if(e.key==="Escape"){
      if(atlasState.selectedId){ atlasState.selectedId=null; pushAtlasHash(); closeDrawerSoft(); }
    }
  });
  $("#f-density").addEventListener("change", (e)=>{ atlasState.density=e.target.value; pushAtlasHash(); fetchAtlas(); });
  $("#f-clear").addEventListener("click", ()=>{
    atlasState.kind="all"; atlasState.type="all"; atlasState.decay="all"; atlasState.time="all"; atlasState.entities=""; atlasState.timeFrom=null; atlasState.timeTo=null;
    syncAtlasControlsFromState(); pushAtlasHash(); fetchAtlas();
  });
  $("#clear-from-empty").addEventListener("click", ()=> $("#f-clear").click());
  $$(".js-clear-filters").forEach(b=> b.addEventListener("click", ()=> $("#f-clear").click()));
  $("#retry-3d").addEventListener("click", ()=>{
    atlasState.fallback2d=false;
    $("#atlas-banner-fallback").hidden=true;
    renderCanvas();
  });
  $("#drawer-close").addEventListener("click", ()=>{ atlasState.selectedId=null; pushAtlasHash(); closeDrawerSoft(); });
}

let debounceTimer=null;
function debounceFetch(){
  clearTimeout(debounceTimer);
  debounceTimer=setTimeout(fetchAtlas, 300);
}

// ---------- boot ----------
document.addEventListener("DOMContentLoaded", ()=>{
  initEvents();
  initDrawerDrag();
  syncRoute();
  loadOverview();
  // if hash already atlas, ensureAtlasLoaded called via syncRoute
  // live interval: refresh observability dot quietly every 12s when on overview
  setInterval(()=>{
    if(currentRoute()==="overview" && daemonReachable) loadOverview();
  }, 12000);
});

// expose for inline handlers / tests
window.__mnemoseed_console = { fetchAtlas, loadOverview, atlasState, hashPos, decayBand };
