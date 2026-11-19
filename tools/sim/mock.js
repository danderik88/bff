// Pygmy app simulator: a fake Pygmy inside the page. Sample printers only; nothing leaves the browser.
// The control panel around the phone drives it through window.SIM; notifications go up with postMessage.
(() => {
  const PLATES = ["Textured PEI Plate", "High Temp Plate", "Cool Plate", "Supertack Plate", "Engineering Plate", "Textured Cool Plate"];
  const PROFILES = [
    { id: "GFA00", name: "Bambu PLA Basic", material: "PLA", vendor: "Bambu", min: 190, max: 230 },
    { id: "GFA01", name: "Bambu PLA Matte", material: "PLA", vendor: "Bambu", min: 190, max: 230 },
    { id: "GFG99", name: "Generic PETG", material: "PETG", vendor: "Generic", min: 220, max: 260 },
    { id: "GFB99", name: "Generic ABS", material: "ABS", vendor: "Generic", min: 240, max: 270 },
    { id: "GFU99", name: "Generic TPU", material: "TPU", vendor: "Generic", min: 200, max: 250 },
  ];
  const tray = (slot, material, name, color, remain, active) => ({ slot, empty: false, active: !!active, material, name, color, remain, profile_id: "GFA01" });
  const empty = slot => ({ slot, empty: true, active: false });
  // AMS a printer gets when the panel fits one (any printer, any time).
  const AMS = {
    a1: () => [{ unit: "A", humidity: 4, slots: [tray("A1", "PLA", "PLA Basic", "AC95D5FF", 80, true), tray("A2", "PLA", "PLA Basic", "FFFFFFFF", 55), empty("A3"), tray("A4", "PETG", null, "1F1F1FFF", null)] }],
    a1mini: () => [{ unit: "A", humidity: 4, slots: [tray("A1", "PLA", "PLA Basic", "00AE42FF", 90, true), tray("A2", "PLA", "PLA Basic", "FF6A13FF", 35), empty("A3"), empty("A4")] }],
    p1s: () => [{ unit: "A", humidity: 5, slots: [tray("A1", "PLA", "PLA Basic", "E4002BFF", 71), empty("A2"), tray("A3", "PLA", "PLA Matte", "000000FF", 96, true), tray("A4", "PETG", null, "FFFFFFFF", null)] },
                { unit: "B", humidity: 3, slots: [tray("B1", "PLA", "PLA Basic", "0086D6FF", 40), tray("B2", "PLA", "PLA Basic", "F4EE2AFF", 88), tray("B3", "PLA", "PLA Silk", "C0C0C0FF", 55), empty("B4")] }],
    // AMS 2 Pro: the P2S can dry it (temperature and hours from the app, countdown from the printer).
    p2s: () => [{ unit: "A", model: "AMS 2 Pro", can_dry: true, drying: false, dry_time: 0, humidity: 2, slots: [tray("A1", "PETG", "PETG HF", "2E7D32FF", 64), tray("A2", "PLA", "PLA Basic", "FFFFFFFF", 82), tray("A3", "PLA", "PLA Matte", "1F1F1FFF", 47), tray("A4", "TPU", null, "E4002BFF", null)] }],
  };
  const printer = (name, model, file, layers, mins, noz, bed, fan, spool, ams) =>
    ({ model, name, state: "RUNNING", file, progress: 20, layers, mins, noz, bed, light: true, speed: 2, fan, spool, units: ams ? ams() : [], runout: false, offline: false });
  // Sample printers only: generic names, nothing of the real setup.
  const P = {
    a1: printer("Workshop A1", "A1", "Desk_organiser", 120, 95, 220, 65, 60, { material: "PLA", color: "AC95D5FF" }),
    a1mini: printer("Desk A1 mini", "A1 mini", "Cable_clip", 40, 25, 220, 60, 100, { material: "PLA", color: "00AE42FF" }, AMS.a1mini),
    p1s: printer("Studio P1S", "P1S", "Silicone_Cartridge_Cap", 90, 72, 220, 55, 80, { material: null, color: null }, AMS.p1s),
    p2s: printer("Lab P2S", "P2S", null, 0, 0, 0, 0, 0, { material: null, color: null }, AMS.p2s),
  };
  P.p2s.progress = 0; P.p2s.state = "IDLE"; P.p2s.light = false;
  P.a1mini.progress = 0; P.a1mini.state = "IDLE";
  const added = {};
  const get = a => P[a] || added[a] || P.a1;  // a stale saved printer falls back to the A1
  let requests = [{ id: 7, url: "https://makerworld.com/models/sample-phone-stand", status: "working", note: null, plates: [1], plate_index: 0, file_name: null }];
  const files = [{ file_id: 11, name: "Cable_clip.gcode.3mf", filament: "PLA", nozzle: "0.4", time_s: 1440, weight_g: 6.2, plate: "Textured PEI Plate" }];
  const plate = { a1: "Textured PEI Plate", a1mini: "Textured PEI Plate", p1s: "Textured PEI Plate", p2s: "Textured PEI Plate" };
  let speed = 1, found = false;

  const say = (a, text) => parent.postMessage({ sim: "notify", title: get(a).name, text }, "*");
  const dur = m => (m >= 60 ? Math.floor(m / 60) + " h " : "") + (m % 60 ? (m % 60) + " min" : "");
  const material = p => (p.units.flatMap(u => u.slots).find(s => s.active) || p.spool).material || "PLA";
  const layer = p => Math.round(p.layers * p.progress / 100);
  function finish(a, ok) {
    const p = get(a);
    p.state = ok ? "FINISH" : "FAILED"; if (ok) p.progress = 100;
    p.noz = 0; p.bed = 0; p.fan = 0; p.runout = false;
    for (const u of p.units) for (const s of u.slots) s.active = false;
    say(a, ok ? `✅ ${p.file} finished in ${dur(p.mins)} · ${material(p)} · ${p.layers} layers. Clear the plate.`
              : `❌ ${p.file} failed at ${Math.round(p.progress)} % (layer ${layer(p)} of ${p.layers}).`);
  }

  // A print moves on by itself so the page looks alive; the panel sets how fast.
  setInterval(() => {
    for (const [a, p] of [...Object.entries(P), ...Object.entries(added)]) {
      if (p.state !== "RUNNING" || p.offline) continue;
      p.progress = Math.min(100, p.progress + 0.4 * speed);
      if (p.progress >= 100) finish(a, true);
    }
    // Drying counts down one minute per tick (sped up by the panel like a print).
    for (const p of Object.values(P)) for (const u of p.units) if (u.drying && (u.dry_time = Math.max(0, u.dry_time - 3 * speed)) === 0) u.drying = false;
  }, 2000);

  // Print now: an upload becomes a draft request; slicing walks it to ready like the real slicer.
  let nextReq = 20, nextFile = 30;
  function upload(url) {
    const name = decodeURIComponent(url.split("/")[3].split("?")[0]);
    const r = { id: nextReq++, url: null, status: "draft", note: null, plates: [1], plate_index: 0, file_name: name, facts: { plates: 1, colors: [] } };
    requests = [r, ...requests];
    return { id: r.id, facts: r.facts };
  }
  function slice(a, x) {
    const r = requests.find(q => q.id === x.id); if (!r) throw new Error("Request not found");
    r.status = "pending";
    setTimeout(() => { r.status = "working"; }, 1200);
    setTimeout(() => {
      const f = { file_id: nextFile++, name: r.file_name.replace(/\.(stl|step|stp|3mf)$/i, "") + ".gcode.3mf", filament: "PETG", nozzle: "0.4", time_s: 2820, weight_g: 18.4, plate: plate[a] };
      files.unshift(f); Object.assign(r, { status: "ready", file_id: f.file_id });
    }, 4500);
    return { request_id: r.id };
  }
  // A plate preview drawn on the fly: the bed and one part, top view.
  function preview() {
    const c = document.createElement("canvas"); c.width = 320; c.height = 320; const g = c.getContext("2d");
    g.fillStyle = "#2b2f35"; g.fillRect(0, 0, 320, 320); g.strokeStyle = "#555b63";
    for (let i = 0; i <= 320; i += 32) { g.beginPath(); g.moveTo(i, 0); g.lineTo(i, 320); g.moveTo(0, i); g.lineTo(320, i); g.stroke(); }
    g.fillStyle = "#2E7D32"; g.fillRect(120, 110, 80, 100); g.fillStyle = "#43a047"; g.fillRect(132, 122, 56, 76);
    return { png: c.toDataURL("image/png").split(",")[1] };
  }

  // What the control panel can do.
  window.SIM = {
    state: a => { const p = get(a); return { ams: !!p.units.length, runout: p.runout, offline: p.offline, found }; },
    speed: x => { speed = x; },
    ams(a, on) { const p = get(a); p.units = on ? AMS[a]() : []; if (!on && !p.spool.material) p.spool = { material: "PLA", color: "FFFFFFFF" }; },
    runout(a, on) {
      const p = get(a); p.runout = on;
      if (on && p.state === "RUNNING") { p.state = "PAUSE"; say(a, `⚠️ Filament ran out. Print paused: needs a hand.`); }
      if (!on && p.state === "PAUSE") p.state = "RUNNING";
    },
    offline(a, on) { get(a).offline = on; },
    found(on) { found = on; },
    finish: a => { if (["RUNNING", "PAUSE"].includes(get(a).state)) finish(a, true); },
    start(a) {
      const p = get(a);
      Object.assign(p, { state: "RUNNING", progress: 1, runout: false, spag: null, spagMuted: false, noz: 220, bed: p.model === "A1 mini" ? 60 : 65, fan: 60 });
      const s = p.units.flatMap(u => u.slots).find(x => !x.empty); if (s) s.active = true;
    },
    spaghetti(a) {
      const p = get(a); if (p.spagOff || p.state !== "RUNNING") return;
      p.spag = "failure"; p.spagMuted = false;
      if (p.spagPause) p.state = "PAUSE";  // Pause on spaghetti is on: Pygmy pauses and says so
      say(a, p.spagPause
        ? `🍝 Possible spaghetti on ${p.file} at layer ${layer(p)}: the print is paused. Check the camera.`
        : `🍝 Possible spaghetti on ${p.file} at layer ${layer(p)}: check the camera. Pygmy does not pause.`);
    },
    // The plate photo the real Pygmy attaches to prepare_print_start: a real camera photo when the build has one.
    photo: a => {
      const photos = (window.SIM_PHOTOS || {})[a];
      return photos && photos.length ? photos[0] : "data:image/svg+xml," + encodeURIComponent(bedSvg(get(a)));
    },
    askPlate() {
      requests = [{ id: requests.length + 8, url: "https://makerworld.com/models/sample-hook", status: "blocked",
                    note: "Which plate is on the printer?", plates: [1], plate_index: 0, file_name: null }, ...requests];
    },
  };

  const status = a => {
    const p = get(a);
    if (p.offline) return { connected: false, state: "", temperatures: {}, printer: a, attention: [] };
    const run = p.state === "RUNNING" || p.state === "PAUSE";
    const s = {
      connected: true, state: p.state, subtask_name: p.state === "IDLE" ? null : p.file, progress: Math.round(p.progress),
      remaining_time: run ? Math.round(p.mins * (100 - p.progress) / 100) : 0,
      layer_num: layer(p), total_layers: p.layers,
      temperatures: { bed: run || p.heat ? p.bed - 0.4 : 24, bed_target: run || p.heat ? p.bed : 0, nozzle: run || p.heat ? p.noz - 1 : 26, nozzle_target: run || p.heat ? p.noz : 0, nozzle_heating: false },
      door_open: false, speed_level: p.speed, chamber_light: p.light, print_error: 0, stage: run ? 0 : 255, prepare_percent: 100,
      fans: { part: run ? p.fan : 0, aux: (p.fanx || {}).aux ?? (run && p.model === "P1S" ? 40 : 0), chamber: (p.fanx || {}).chamber ?? (run && p.model === "P1S" ? 30 : 0) },
      xcam: { buildplate_marker_detector: true }, options: { auto_recovery: true, sound_enable: false, filament_tangle_detect: true, ...(p.opts || {}), bff_spaghetti: !p.spagOff, bff_spaghetti_pause: !!p.spagPause },
      printer: a, hms_count: p.runout ? 1 : 0, awaiting_plate_clear: p.state === "FINISH", spool: p.spool,
      attention: p.runout ? [{ description: "Filament ran out. Load new filament, then resume.", handling: "persona" }] : [],
      // Axes limits per model, as Pygmy sends them (A1 and P1 measured; A1 mini not yet).
      motion_style: p.kind === "klipper" ? "jog" : "absolute",
      motion: p.kind === "klipper" ? { x: [0, 350], y: [0, 350], z: [0, 330], safe: null, keep_out: null, first: { x: 175, y: 175, z: 15 } }
        : p.model === "A1" ? { x: [-48, 256], y: [0, 256], z: [0, 256], safe: { x: -48, y: 256 }, keep_out: null, first: { x: -48, y: 256, z: 10 } }
        : /^P1/.test(p.model) ? { x: [0, 256], y: [0, 256], z: [0, 250], safe: null, keep_out: [0, 0, 20, 30], first: { x: 128, y: 128, z: 10 } } : false,
      spaghetti: run && !p.spagOff ? { verdict: p.spag ? (p.spagMuted ? "dismissed" : p.spag) : "safe", score: p.spag ? 0.71 : 0.02, error: null,
        paused: !!(p.spag && p.spagPause) } : { verdict: null, score: null, error: null },
      // Klipper reports a real position and homes one axis at a time; the rest don't report either.
      axes: p.kind === "klipper" ? { homed: true, homed_axes: "xyz", pos: { ...p.pos } }
        : { homed: false, pos: { x: null, y: null, z: null } },
    };
    if (p.units.length) {
      s.ams_units = p.units;
      s.ams = p.units.flatMap((u, i) => u.slots.filter(x => !x.empty).map(x => ({ ...x, index: i * 4 + Number(x.slot[1]) - 1 })));
    }
    return s;
  };
  const all = () => [...Object.keys(P), ...Object.keys(added)];
  const info = a => {
    const p = get(a), s = status(a);
    const ams = (s.ams || []).map(t => ({ slot: t.slot, material: t.material, color: t.color, profile_id: t.profile_id, subtype: t.name, remain_pct: t.remain, grams_remaining: null, grams_source: "unknown" }));
    const ext = p.spool.material ? [{ slot: "external", material: p.spool.material, color: p.spool.color, grams_remaining: 640, grams_source: "tracked" }] : [];
    const filaments = [...ams, ...ext];
    return {
      printer: a, available_printers: all(),
      names: Object.fromEntries(all().map(k => [k, get(k).name])), models: Object.fromEntries(all().map(k => [k, get(k).model])),
      profile: { fans: /^P1/.test(p.model) ? ["part", "aux", "chamber"] : ["part"] }, model: p.model, slicer_model: "Bambu Lab " + p.model, nozzle: { diameter: "0.4", type: "hardened_steel" },
      slicer: true, ams_present: !!p.units.length, filaments, default_slot: filaments[0] ? filaments[0].slot : null,
      connected: !p.offline, state: p.state, free: p.state !== "RUNNING", not_free_reason: null, awaiting_plate_clear: p.state === "FINISH",
      attention: s.attention, plate: { state: "unknown", reason: "" }, plate_type: plate[a] || null,
      choices: { nozzle_diameters: [0.2, 0.4, 0.6, 0.8], nozzle_types: ["stainless_steel", "hardened_steel"], plate_types: PLATES, spool_profiles: PROFILES },
    };
  };
  const ok = { outcome: "confirmed" };
  // Temperatures: nozzle/bed as numbers, or heater + target.
  const pend = {};
  function heat(a, x) {
    const p = get(a), t = { ...x }; if (x.heater && x.target != null) t[x.heater] = x.target;
    if (typeof t.nozzle === "number") p.noz = t.nozzle;
    if (typeof t.bed === "number") p.bed = t.bed;
    p.heat = p.noz > 0 || p.bed > 0; return ok;
  }
  const FOUND = [
    { ip: "192.168.1.31", serial: "SIM-P1P", model: "C11", model_name: "P1P", name: "P1P", supported: true },
    { ip: "192.168.1.32", serial: "SIM-P2S", model: "N7", model_name: "P2S", name: "P2S", supported: false },
    // A Klipper printer that Moonraker answers for: no access code, the model comes from the printer.cfg.
    { ip: "192.168.1.40", kind: "klipper", model: "KLIPPER-GENERIC", model_name: "Voron 2.4", name: "Voron 2.4", port: 7125, supported: true }];
  const KLIPPER_MODELS = [{ code: "KLIPPER-GENERIC", name: "Other Klipper printer" },
    { code: "KLIPPER-SV08", name: "Sovol SV08" }, { code: "KLIPPER-IR3V2", name: "IdeaFormer IR3 V2" }];
  // A Klipper printer: it reports its own position and homes per axis, so the app jogs relative.
  const klipper = name => ({ kind: "klipper", model: "KLIPPER-GENERIC", name, state: "IDLE", progress: 0, file: null, layers: 0, mins: 0,
    noz: 0, bed: 0, light: false, speed: 2, fan: 0, pos: { x: 148.2, y: 132.6, z: 12.4 },
    spool: { material: "PLA", color: "FFFFFFFF" }, units: [], runout: false, offline: false });
  const tools = {
    status, printer_info: info,
    // What the app's printer picker needs: the aliases plus their display names and models.
    list_printers: () => ({ printers: all(), names: Object.fromEntries(all().map(k => [k, get(k).name])), models: Object.fromEntries(all().map(k => [k, get(k).model])) }),
    camera_urls: a => ({ kind: "jpeg", rtsp: "rtsp://bff:demo@bff.local:8554/" + a }),
    camera_webrtc: () => { throw new Error("This printer has no video stream"); },
    print_requests: () => ({ requests }), ready_files: () => ({ files }),
    slice_request: slice, plate_preview: preview, mw_status: () => ({ available: false }),
    queue_list: () => ({ jobs: [] }),  // ponytail: an empty queue; the simulator does not model queued jobs
    ams_dry: (a, x) => {
      const u = get(a).units.find(v => v.unit === x.unit); if (!u || !u.can_dry) throw new Error("This AMS cannot dry");
      if (x.action === "stop") Object.assign(u, { drying: false, dry_time: 0 }); else Object.assign(u, { drying: true, dry_time: Math.round((x.hours || 8) * 60), dry_target: x.temp });
      return ok;
    },
    pause: a => { get(a).state = "PAUSE"; return ok; },
    resume: a => { const p = get(a); p.state = "RUNNING"; p.runout = false; return ok; },
    prepare_stop: a => ({ printer: a, summary: "Stop the print on " + get(a).name + ". This cannot be undone.", confirm_token: "sim", expires_in_s: 120 }),
    confirm_stop: a => { finish(a, false); return ok; },
    set_light: (a, x) => { get(a).light = !!x.on; return ok; },
    set_print_speed: (a, x) => { get(a).speed = x.level; return ok; },
    // Like the real printer: the new speed shows up in its report ~10 s later.
    set_fan: (a, x) => {
      const p = get(a), k = x.fan || "part", from = k === "part" ? p.fan : ((p.fanx || {})[k] ?? 0), put = v => { if (k === "part") p.fan = v; else (p.fanx ||= {})[k] = v; };
      [[3000, 0.55], [5000, 0.85], [7000, 1]].forEach(([t, f]) => setTimeout(() => put(Math.round((from + (x.percent - from) * f) / 10) * 10), t));
      return ok;
    },
    set_plate: (a, x) => { plate[a] = x.plate; return ok; },
    reply_request: (a, x) => { const r = requests.find(r => r.id === x.request_id); if (r) { r.status = "working"; r.note = null; } return ok; },
    request_print: (a, x) => {
      const r = { id: requests.length + 8, url: x.url, copies: x.copies || 1, status: "pending", note: null, plates: [1], plate_index: 0, file_name: null };
      requests = [r, ...requests];
      // A draft: the fetch lands a moment later. A Printables page with several meshes lists them for the app's checkboxes.
      if (x.draft) setTimeout(() => {
        const printables = /printables/.test(x.url || "");
        Object.assign(r, { status: "draft", url: null, file_name: printables ? "handle-holder" : "sample-cable-clip",
          facts: { plates: 1, colors: [], ...(printables ? { files: ["handle.stl", "bit_holder.stl", "cap.stl"] } : {}) } });
      }, 1500);
      return { request_id: r.id };
    },
    cancel_request: (a, x) => { requests = requests.filter(r => r.id !== x.request_id); return ok; },
    discover_printers: () => ({ printers: found ? FOUND.map(f => ({ ...f, managed: !!added[String(f.name).toLowerCase().replace(/[^a-z0-9]/g, "")] })) : [],
      klipper_models: KLIPPER_MODELS }),
    add_printer: (a, x) => {
      if (x.kind === "klipper") {  // Moonraker: no access code, the name is the only thing Pygmy asks
        const name = String(x.name || x.label || "Klipper printer").trim();
        const alias = name.toLowerCase().replace(/[^a-z0-9]/g, "") || "klipper";
        added[alias] = klipper(name);
        return { printer: alias, model_name: x.label || "Klipper" };
      }
      if (!/^[A-Za-z0-9]{8}$/.test((x.access_code || "").trim())) throw new Error("access code: 8 characters");
      const model = x.enclosure ? "P1S" : "P1P", alias = String(x.name || model).toLowerCase().replace(/[^a-z0-9]/g, "") || "printer";
      added[alias] = { ...printer(model, null, 0, 0, 0, 0, 0, { material: "PLA", color: "FFFFFFFF" }), name: x.name || model, state: "IDLE", progress: 0, light: false };
      return { printer: alias, model_name: model };
    },
    set_access_code: () => ({ ok: true }),
    set_temperature: (a, x) => heat(a, x),
    confirm_temperature: (a, x) => heat(a, pend[a] || {}),
    confirm_print_start: a => { const p = get(a), f = pend[a] || {}; p.file = (files.find(x => x.file_id === f.file_id) || files[0]).name.replace(/\.gcode\.3mf$/, ""); SIM.start(a); return ok; },
    remove_printer: (a, x) => { delete added[x.printer]; return { ok: true }; },
    // Filament: an AMS slot (any time) or the external spool, from the same editor.
    set_spool: (a, x) => {
      const p = get(a), pr = PROFILES.find(f => f.id === x.profile_id) || {}, v = { material: pr.material || "PLA", color: x.color, profile_id: x.profile_id };
      if (!x.slot) { if (p.state === "RUNNING") throw new Error("Only while the printer is idle"); p.spool = { ...p.spool, ...v }; return ok; }
      const sl = p.units.flatMap(u => u.slots).find(t => t.slot === x.slot); if (!sl) throw new Error("No slot " + x.slot);
      // Like the real printer: the new filament shows up in its report ~10 s later.
      setTimeout(() => Object.assign(sl, v, { empty: false, name: null, remain: null }), 10000); return ok;
    },
    read_filament: (a, x) => { const p = get(a), u = x.slot[0]; if (["RUNNING", "PAUSE"].includes(p.state) && p.units.some(v => v.unit === u && v.slots.some(t => t.active))) throw new Error("This AMS is feeding the print"); return ok; },
    plate_cleared: a => { const p = get(a); if (p.state === "FINISH") p.state = "IDLE"; return ok; },
    clear_errors: a => { const p = get(a); p.runout = false; return ok; },
    dismiss_spaghetti: a => { get(a).spagMuted = true; return { spaghetti: "dismissed until this print ends" }; },
    set_print_option: (a, x) => {
      const p = get(a);
      if (x.option === "bff_spaghetti") p.spagOff = !x.enabled;
      else if (x.option === "bff_spaghetti_pause") p.spagPause = !!x.enabled;
      else (p.opts ||= {})[x.option] = x.enabled;
      return ok;
    },
    // Klipper: absolute home (one axis at a time in the real link) and relative jog from the reported position.
    home: (a, x) => { const p = get(a); if (p.kind === "klipper") p.pos = { ...p.pos, x: 0, y: 0, ...((x.axes || "all") === "all" ? { z: 0 } : {}) }; return ok; },
    jog: (a, x) => {
      const p = get(a); if (p.kind === "klipper" && p.pos && x.axis) p.pos[x.axis] = Math.round((p.pos[x.axis] + x.distance) * 100) / 100;
      return ok;
    },
    motors_off: () => ok,
  };

  // The camera: real photos when the build put some in (cycled like a GIF), else a drawn bed.
  const shot = {};
  function bedSvg(p) {
    const h = Math.round(70 * (p.progress || 0) / 100);
    const col = p.units.flatMap(u => u.slots).find(s => s.active);
    const c = "#" + ((col && col.color) || p.spool.color || "999999FF").slice(0, 6);
    return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 360"><defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1d2228"/><stop offset="1" stop-color="#3a4048"/></linearGradient></defs>`
      + `<rect width="640" height="360" fill="url(#g)"/><polygon points="80,330 560,330 500,190 140,190" fill="#2b2f35" stroke="#555b63"/>`
      + `<rect x="290" y="${270 - h}" width="60" height="${h}" fill="${c}" stroke="#0006"/><rect x="250" y="40" width="140" height="60" rx="8" fill="#d9dbde"/>`
      + `<rect x="314" y="100" width="12" height="${Math.max(0, 160 - h)}" fill="#8b9097"/></svg>`;
  }
  async function frame(a) {
    const p = get(a), photos = (window.SIM_PHOTOS || {})[a];
    if (photos && photos.length && !p.offline) { shot[a] = ((shot[a] || 0) + 1) % photos.length;
      // Decode the data URL by hand: the published page may block fetch() of data: URLs.
      const bin = atob(photos[shot[a]].split(",")[1]);
      return new Blob([Uint8Array.from(bin, ch => ch.charCodeAt(0))], { type: "image/jpeg" }); }
    return new Blob([bedSvg(p)], { type: "image/svg+xml" });
  }

  const json = body => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  const reply = (id, out, err) => json({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: JSON.stringify(out) }], isError: !!err } });
  window.fetch = async (url, opts = {}) => {
    url = String(url);
    if (url.startsWith("/camera/")) {
      const a = decodeURIComponent(url.slice(8));
      return get(a).offline ? new Response("", { status: 503 }) : new Response(await frame(a), { status: 200 });
    }
    if (url.startsWith("/model/")) return json(upload(url));
    if (url === "/" && opts.method === "POST") {
      const msg = JSON.parse(opts.body), name = msg.params.name, args = msg.params.arguments || {}, a = args.printer || "a1";
      if (name === "prepare_print_start") {  // the real one carries a plate photo too; the simulator shows the drawn bed
        pend[a] = args;
        const f = files.find(x => x.file_id === args.file_id) || {};
        return reply(msg.id, { printer: a, confirm_token: "sim", expires_in_s: 120,
          text: `Start '${String(f.name || "the print").replace(/\.gcode\.3mf$/, "")}' on ${get(a).name}: ${f.filament || "PLA"}, `
            + `nozzle ${f.nozzle || "0.4"} mm, about ${Math.round((f.time_s || 0) / 60)} min`
            + `${f.weight_g != null ? ", " + Math.round(f.weight_g) + " g" : ""}. Look at the photo: the plate must be clear.` });
      }
      if (!tools[name] && name.startsWith("prepare_")) {
        pend[a] = args;
        return reply(msg.id, { printer: a, summary: name.slice(8).replace(/_/g, " ") + " on " + get(a).name + " (simulated).", confirm_token: "sim", expires_in_s: 120 });
      }
      if (!tools[name]) return reply(msg.id, ok);  // ponytail: the simulator accepts what it does not model
      try { return reply(msg.id, tools[name](a, args)); } catch (e) { return reply(msg.id, { errore: e.message }, true); }
    }
    return new Response("", { status: 404 });
  };
})();
