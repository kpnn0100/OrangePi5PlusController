// IO Control (IO-01..10): the board's pins and buses for bring-up and debugging -
// GPIO lines (header view on known boards), I2C scan / transfer / register dump, SPI
// transfers, UART consoles, PWM, LEDs and ADC channels. Everything goes through the
// server's io.* ops (standard Linux interfaces), so the CLI and other pages see the same.

import { h, clear, icon, store, conn, run, sheet, seg, toggle, toast, errText, busy, fmtBytes } from "../core.js";
import terminal from "./terminal.js";

let infoCache = null;

async function loadInfo(force = false) {
  if (!infoCache || force) infoCache = await conn.call("io.info");
  return infoCache;
}

function head(root, title, sub, actions) {
  root.append(h("div.view-head", null, h("div", null, h("h1", null, title), h("div.sub", null, sub)),
    actions ? h("div.actions", null, actions) : null));
}

function problems(info, filter) {
  const list = ((info && info.problems) || []).filter((p) => !filter || p.startsWith(filter));
  return list.length ? h("div.io-note", null, icon("alert"), h("div", null, list.map((p) => h("div", null, p)))) : null;
}

const hex2 = (n) => "0x" + Number(n).toString(16).padStart(2, "0");
const field = (label, el, hint) => h("div.field", null, h("label", null, label), el, hint ? h("div.hint", null, hint) : null);
const input = (attrs) => h("input.input", { spellcheck: "false", autocomplete: "off", ...attrs });

function select(options, value, onChange) {
  return h("select.input", { onchange: (e) => onChange && onChange(e.target.value) },
    options.map(([v, l]) => h("option", { value: v, selected: String(v) === String(value) }, l)));
}

function hexdump(bytes, start = 0) {
  const lines = [];
  for (let i = 0; i < bytes.length; i += 16) {
    const row = bytes.slice(i, i + 16);
    const hx = row.map((b) => b.toString(16).padStart(2, "0")).join(" ").padEnd(48);
    const asc = row.map((b) => (b >= 32 && b < 127 ? String.fromCharCode(b) : ".")).join("");
    lines.push((start + i).toString(16).padStart(4, "0") + "  " + hx + " " + asc);
  }
  return h("pre.hexdump", null, lines.join("\n"));
}

// ================================================================== GPIO / pins
function gpioSheet(p, onDone) {
  // p: {chip, line, name?, pin?, alt?, state?, held?}
  const held = p.held || null;
  const st = p.state || {};
  let mode = held ? held.mode : st.direction === "output" ? "output" : "input";
  let bias = held ? held.bias : "as-is";
  let drive = held ? held.drive : "push-pull";
  let edge = held ? held.edge : "none";
  let activeLow = held ? held.active_low : false;
  const deb = input({ value: held ? held.debounce_us || "" : "", placeholder: "0", inputmode: "numeric", style: { maxWidth: "120px" } });
  const valueBox = h("div.row");
  const opts = h("div.stack");
  const req = (value) => ({ chip: p.chip, line: p.line, mode, bias, drive, edge, active_low: activeLow,
                            debounce_us: Number(deb.value || 0), value });
  const renderOpts = () => clear(opts,
    field("Bias", seg([["as-is", "As is"], ["pull-up", "Pull-up"], ["pull-down", "Pull-down"], ["disabled", "None"]], bias, (v) => { bias = v; })),
    mode === "output" ? field("Drive", seg([["push-pull", "Push-pull"], ["open-drain", "Open drain"], ["open-source", "Open source"]], drive, (v) => { drive = v; }))
      : field("Edge events", seg([["none", "Off"], ["rising", "Rising"], ["falling", "Falling"], ["both", "Both"]], edge, (v) => { edge = v; }),
              "Edges are timestamped by the kernel and listed under Events"),
    mode === "input" ? field("Debounce (µs)", deb) : null,
    h("div.row", null, h("div.grow", null, h("b", null, "Active low"), h("div.hint", null, "1 means the pin is at 0 V")),
      toggle(activeLow, (v) => { activeLow = v; })));
  const renderValue = (v) => clear(valueBox,
    h("div.grow", null, h("div.label", null, "Level"),
      h("div", { style: { fontSize: "26px", fontWeight: 700, fontFamily: "var(--mono)", color: v ? "var(--ok)" : "var(--muted)" } },
        v === null || v === undefined ? "–" : v ? "1 HIGH" : "0 LOW")),
    mode === "output" ? [h("button.btn", { onclick: (e) => apply(0, e.currentTarget) }, "Set 0"),
                         h("button.btn.primary", { onclick: (e) => apply(1, e.currentTarget) }, "Set 1")] : null);
  async function apply(value, btn) {
    const r = await run("io.gpio.request", req(value === undefined ? (held && held.value) || 0 : value), { btn });
    if (r) { renderValue(r.value); onDone && onDone(); }
  }
  renderOpts();
  renderValue(held ? held.value : null);
  const title = p.pin ? `Pin ${p.pin} · ${p.name}` : `${p.chip.replace("/dev/", "")} line ${p.line}`;
  const s = sheet({
    title,
    body: h("div.stack", null,
      h("div.muted", { style: { fontSize: "13px" } },
        [`${p.chip} line ${p.line}`, p.alt && p.alt.length ? "also " + p.alt.join(", ") : null].filter(Boolean).join(" · ")),
      st.used && !held ? h("div.io-note", null, icon("alert"), `Used by "${st.consumer || "a driver"}" - the kernel will refuse the request.`) : null,
      field("Mode", seg([["input", "Input"], ["output", "Output"]], mode, (v) => { mode = v; renderOpts(); renderValue(null); }, { full: true })),
      opts, valueBox),
    foot: [
      held ? h("button.btn.ghost", { onclick: async (e) => { if (await run("io.gpio.release", { chip: p.chip, line: p.line }, { btn: e.currentTarget, ok: "Released" })) { s.close(); onDone && onDone(); } } }, "Release") : null,
      h("button.btn.primary", { onclick: (e) => apply(undefined, e.currentTarget) }, held ? "Apply" : "Request line"),
    ],
  });
  const off = store.on("io.gpio", (g) => {
    const hl = ((g && g.held) || []).find((x) => x.chip === p.chip && x.line === p.line);
    if (hl) renderValue(hl.value);
  }, { now: false });
  const close = s.close;
  s.close = () => { off(); close(); };
  return s;
}

export const pins = {
  id: "pins", title: "Pins", icon: "chip",

  mount(root) {
    let info = null;
    let header = null;
    let chip = null;
    const noteBox = h("div");
    const headerCard = h("div.card");
    const heldCard = h("div.card");
    const eventsCard = h("div.card");
    const chipSel = h("div");
    const linesCard = h("div.card", { style: { padding: "6px 14px" } });
    head(root, "Pins", "GPIO lines through the kernel's GPIO character device",
         h("button.btn.sm.ghost", { onclick: (e) => busy(e.currentTarget, () => load(true)) }, icon("refresh"), "Refresh"));
    root.append(noteBox, headerCard,
      h("div.grid.two", { style: { marginTop: "16px" } }, heldCard, eventsCard),
      h("div.section", null, h("div.section-title", null, "All lines of a chip", h("div.actions", null, chipSel)), linesCard));

    const g = () => store.get("io.gpio") || { held: [], events: [] };
    const heldOf = (c, l) => (g().held || []).find((x) => x.chip === c && x.line === l);

    function renderHeader() {
      if (!header) return clear(headerCard, h("div.row", null, h("span.spin"), h("span.muted", null, "Loading…")));
      if (!header.board) return clear(headerCard, h("div.muted", null, "No pin map for this board - use the line list below."));
      const byPin = Object.fromEntries(header.pins.map((p) => [p.pin, p]));
      const cell = (n) => {
        const p = byPin[n];
        const gp = !!p.gpio;
        const hl = gp && p.chip ? heldOf(p.chip, p.gpio.line) : null;
        const st = p.state || {};
        const cls = ["pin", n % 2 ? "odd" : "even", gp ? "gpio" : p.name === "GND" ? "gnd" : "pwr", hl ? "held" : ""].join(" ");
        const v = hl ? hl.value : null;
        return h("div", { class: cls, title: gp ? `${p.chip || "?"} line ${p.gpio.line} (GPIO ${p.gpio.number})` : p.name,
                          onclick: gp && p.chip ? () => gpioSheet({ chip: p.chip, line: p.gpio.line, name: p.name, pin: n, alt: p.alt, state: st, held: hl }, load) : null },
          h("span.num", null, String(n)),
          h("div.txt", null, h("span.pname", null, p.name),
            gp ? h("span.palt", null, st.used ? "used: " + (st.consumer || "driver") : (p.alt || []).slice(0, 2).join(" · ")) : null),
          v !== null && v !== undefined ? h("span.lv", { class: v ? "hi" : "lo" }, String(v)) : null);
      };
      const rows = [];
      for (let n = 1; n <= 40; n += 2) rows.push(cell(n), cell(n + 1));
      clear(headerCard, h("div.card-title", null, icon("chip"), header.board + " · 40-pin header"),
        h("div.header-grid", null, rows),
        h("div.hint", { style: { marginTop: "12px" } }, "Tap a GPIO pin to use it. ", header.enable_hint || ""));
    }

    function renderHeld() {
      const list = g().held || [];
      clear(heldCard, h("div.card-title", null, "Lines in use by the server", h("span.tag", null, String(list.length))),
        list.length ? h("div.list", null, list.map((l) => h("div.list-item.click", { onclick: () => gpioSheet({ chip: l.chip, line: l.line, pin: l.pin, name: l.pin ? "" : "", held: l }, load) },
          h("div.grow", null, h("div.title.mono", null, (l.pin ? `pin ${l.pin} · ` : "") + `${l.chip.replace("/dev/", "")}:${l.line}`),
            h("div.meta", null, [l.mode, l.bias !== "as-is" ? l.bias : null, l.mode === "output" ? l.drive : l.edge !== "none" ? l.edge + " edges" : null,
                                 l.active_low ? "active low" : null, l.events ? l.events + " events" : null].filter(Boolean).join(" · "))),
          h("span.lv.mono", { class: l.value ? "hi" : "lo", style: { fontWeight: 700 } }, l.value === null ? "?" : String(l.value)),
          l.mode === "output" ? h("button.btn.sm", { onclick: (e) => { e.stopPropagation(); run("io.gpio.set", { chip: l.chip, line: l.line, value: l.value ? 0 : 1 }); } }, "Toggle") : null)))
          : h("div.muted", { style: { fontSize: "13px" } }, "None. Requested lines stay set until released."));
    }

    function renderEvents() {
      const ev = (g().events || []).slice().reverse();
      clear(eventsCard, h("div.card-title", null, "Edge events",
          h("div.actions", null, ev.length ? h("button.btn.sm.ghost", { onclick: () => run("io.gpio.clear_events") }, "Clear") : null)),
        ev.length ? h("div.io-log", null, ev.map((e) => h("div", null,
          h("span.t", null, new Date(e.t * 1000).toISOString().slice(11, 23) + " "),
          (e.pin ? `pin ${e.pin} ` : "") + `${e.chip.replace("/dev/", "")}:${e.line} `, h("b", { style: { color: e.edge === "rising" ? "var(--ok)" : "var(--warn)" } }, e.edge))))
          : h("div.muted", { style: { fontSize: "13px" } }, "Request an input with edge events to see them here."));
    }

    async function renderLines() {
      const chips = (info && info.gpio) || [];
      clear(chipSel, chips.length ? select(chips.map((c) => [c.chip, `${c.chip.replace("/dev/", "")} ${c.label || ""}`]), chip,
                                          (v) => { chip = v; renderLines(); }) : null);
      if (!chip) return clear(linesCard, h("div.empty", null, "No GPIO chips."));
      let r;
      try { r = await conn.call("io.gpio.lines", { chip }); } catch (e) { return clear(linesCard, h("div.empty", null, errText(e))); }
      clear(linesCard, h("div.list", null, r.lines.map((l) => h("div.list-item.click", {
        onclick: () => gpioSheet({ chip, line: l.line, pin: l.pin, name: l.pin ? `pin ${l.pin}` : "", state: l, held: l.held ? heldOf(chip, l.line) : null }, load) },
        h("span.mono", { style: { width: "34px", color: "var(--muted)" } }, String(l.line)),
        h("div.grow", null, h("div.title", null, l.name || (l.pin ? `header pin ${l.pin}` : "line " + l.line)),
          h("div.meta", null, [l.direction, l.bias !== "as-is" ? l.bias : null, l.active_low ? "active low" : null,
                               l.consumer ? "used by " + l.consumer : l.held ? "held by the server" : null].filter(Boolean).join(" · "))),
        l.used && !l.held ? h("span.tag.warn", null, "in use") : null,
        l.value !== undefined ? h("span.lv.mono", { class: l.value ? "hi" : "lo" }, String(l.value)) : null))));
    }

    async function load(force) {
      try {
        info = await loadInfo(force);
        header = await conn.call("io.gpio.header");
      } catch (e) {
        toast(errText(e), "err");
      }
      clear(noteBox, problems(info, "GPIO"));
      if (!chip && info && info.gpio && info.gpio.length) chip = (info.gpio.find((c) => !c.error) || info.gpio[0]).chip;
      renderHeader();
      renderLines();
    }

    const offs = [store.on("io.gpio", () => { renderHeld(); renderEvents(); renderHeader(); })];
    renderHeader();
    renderHeld();
    renderEvents();
    const offReady = conn.on("ready", () => load(true));
    if (conn.status === "open") load(false);
    return () => { offs.forEach((f) => f()); offReady(); };
  },
};

// ========================================================================= I2C
export const i2c = {
  id: "i2c", title: "I2C", icon: "chip",

  mount(root) {
    let bus = null;
    let scanRes = null;
    const busSel = h("div");
    const scanCard = h("div.card");
    const addr = input({ value: "0x50", style: { fontFamily: "var(--mono)" } });
    const wr = input({ placeholder: "e.g. 00 or 10 ff", style: { fontFamily: "var(--mono)" } });
    const rd = input({ value: "1", inputmode: "numeric" });
    const out = h("div.stack");
    const log = [];
    const logBox = h("div.io-log");
    const noteBox = h("div");
    head(root, "I2C", "Scan buses, read and write devices (i2c-dev)", busSel);
    root.append(noteBox, scanCard,
      h("div.section", null, h("div.section-title", null, "Transfer"),
        h("div.card", null, h("div.io-form", null,
          field("Address", addr), field("Write bytes (hex)", wr, "Register address first, then data"), field("Read count", rd),
          h("div.row", null,
            h("button.btn.primary", { onclick: (e) => transfer(e.currentTarget) }, "Run"),
            h("button.btn", { onclick: (e) => dump(e.currentTarget) }, "Dump 256"))),
          out)),
      h("div.section", null, h("div.section-title", null, "History"), h("div.card", null, logBox)));

    const logLine = (text, err) => {
      log.unshift({ t: new Date().toISOString().slice(11, 19), text, err });
      log.length = Math.min(log.length, 60);
      clear(logBox, log.length ? log.map((l) => h("div", { style: { color: l.err ? "var(--err)" : "" } }, h("span.t", null, l.t + " "), l.text))
                               : h("div.muted", null, "Nothing yet."));
    };

    function renderScan() {
      const cells = [h("div.hdr")];
      for (let c = 0; c < 16; c++) cells.push(h("div.hdr", null, c.toString(16)));
      for (let r = 0; r < 0x80; r += 16) {
        cells.push(h("div.hdr", null, r.toString(16).padStart(2, "0")));
        for (let c = 0; c < 16; c++) {
          const a = r + c;
          const found = scanRes && scanRes.found.includes(a);
          const busyA = scanRes && scanRes.busy.includes(a);
          const inRange = a >= 0x08 && a <= 0x77;
          cells.push(h("div", { class: "cell" + (found ? " found" : busyA ? " busy" : ""),
                                title: busyA ? "used by a kernel driver" : found ? "answered" : "",
                                onclick: found || busyA ? () => { addr.value = hex2(a); } : null },
                       !inRange ? "" : found ? a.toString(16).padStart(2, "0") : busyA ? "UU" : scanRes ? "--" : ""));
        }
      }
      clear(scanCard, h("div.card-title", null, "Bus scan",
          h("div.actions", null, h("button.btn.sm.primary", { disabled: bus === null, onclick: (e) => scan(e.currentTarget) }, icon("refresh"), "Scan"))),
        h("div.i2c-grid", null, cells),
        h("div.hint", { style: { marginTop: "10px" } },
          scanRes ? `${scanRes.found.length} device(s) answered, ${scanRes.busy.length} claimed by drivers (UU). Tap one to use its address.`
                  : "Probes 0x08-0x77 like i2cdetect (read for EEPROM ranges, quick write elsewhere)."));
    }

    async function scan(btn) {
      await busy(btn, async () => {
        scanRes = await conn.call("io.i2c.scan", { bus });
        logLine(`scan i2c-${bus}: ${scanRes.found.map(hex2).join(" ") || "nothing"}`);
      });
      renderScan();
    }

    async function transfer(btn) {
      await busy(btn, async () => {
        try {
          const r = await conn.call("io.i2c.transfer", { bus, addr: addr.value, write: wr.value, read: Number(rd.value || 0) });
          clear(out, r.bytes.length ? hexdump(r.bytes) : h("div.muted", null, "Written."));
          logLine(`i2c-${bus} ${addr.value}: w[${wr.value}] r${rd.value} → ${r.read || "ok"}`);
        } catch (e) { logLine(`i2c-${bus} ${addr.value}: ${errText(e)}`, true); throw e; }
      });
    }

    async function dump(btn) {
      await busy(btn, async () => {
        const r = await conn.call("io.i2c.dump", { bus, addr: addr.value, start: 0, count: 256 });
        clear(out, hexdump(r.bytes, r.start));
        logLine(`dump i2c-${bus} ${addr.value}: 256 registers`);
      });
    }

    async function load() {
      try {
        const info = await loadInfo();
        clear(noteBox, problems(info, "I2C"));
        const buses = info.i2c || [];
        if (bus === null && buses.length) bus = buses[0].bus;
        clear(busSel, buses.length ? select(buses.map((b) => [b.bus, `i2c-${b.bus} ${(b.dt || []).join("/") || b.name}`]), bus,
                                           (v) => { bus = Number(v); scanRes = null; renderScan(); }) : h("span.muted", null, "No I2C buses"));
      } catch (e) { toast(errText(e), "err"); }
      renderScan();
    }
    renderScan();
    logLine("Ready.");
    const offReady = conn.on("ready", load);
    if (conn.status === "open") load();
    return () => offReady();
  },
};

// ========================================================================= SPI
export const spi = {
  id: "spi", title: "SPI", icon: "chip",

  mount(root) {
    const body = h("div");
    head(root, "SPI", "Full-duplex transfers through spidev");
    root.append(body);
    let dev = null, mode = 0;
    const speed = input({ value: "1000000", inputmode: "numeric" });
    const bits = input({ value: "8", inputmode: "numeric" });
    const tx = input({ placeholder: "9f 00 00 00", style: { fontFamily: "var(--mono)" } });
    const out = h("div");

    async function load() {
      let info;
      try { info = await loadInfo(); } catch (e) { return toast(errText(e), "err"); }
      const devs = info.spi || [];
      if (!devs.length) {
        return clear(body, h("div.card", null, h("div.empty", null, icon("chip"), h("div", null, "No SPI device (spidev) is enabled."),
          h("div.hint", { style: { marginTop: "8px" } }, info.enable_hint || "Enable an spidev device-tree overlay and reboot."))));
      }
      dev = dev || devs[0].path;
      clear(body, h("div.card", null, h("div.io-form", null,
          field("Device", select(devs.map((d) => [d.path, `${d.path}${d.dt && d.dt.length ? " (" + d.dt.join("/") + ")" : ""}`]), dev, (v) => { dev = v; })),
          field("Mode (CPOL/CPHA)", seg([[0, "0"], [1, "1"], [2, "2"], [3, "3"]], mode, (v) => { mode = v; }, { full: true })),
          field("Speed (Hz)", speed), field("Bits per word", bits),
          h("div.field.wide", null, h("label", null, "Bytes to send (hex)"), tx,
            h("div.hint", null, "Reading clocks out bytes too: send 00s for every byte you want back")),
          h("button.btn.primary", { onclick: (e) => xfer(e.currentTarget) }, "Transfer")),
        h("div", { style: { marginTop: "14px" } }, out)));
    }

    async function xfer(btn) {
      await busy(btn, async () => {
        const r = await conn.call("io.spi.transfer", { device: dev, tx: tx.value, mode, speed_hz: Number(speed.value), bits: Number(bits.value) });
        clear(out, h("div.label", null, "Received"), hexdump(r.bytes));
      });
    }
    const offReady = conn.on("ready", load);
    if (conn.status === "open") load();
    return () => offReady();
  },
};

// ======================================================================== UART
export const uart = {
  id: "uart", title: "UART", icon: "plug",

  mount(root) {
    const ctl = h("div.card", { style: { marginBottom: "12px" } });
    const termHost = h("div");
    head(root, "UART", "Serial consoles · shared like shells: the CLI and other browsers see the same");
    root.append(ctl, termHost);
    let active = null;             // terminal id shown
    let modemTimer = null;

    const openPort = async () => {
      let info;
      try { info = await conn.call("io.uart.ports"); } catch (e) { return toast(errText(e), "err"); }
      const ports = info.ports || [];
      let port = (ports.find((p) => !p.console) || ports[0] || {}).port;
      let baud = 115200, parity = "none", stop = 1, flow = "none", dataBits = 8;
      const s = sheet({
        title: "Open a serial port",
        body: h("div.stack", null,
          field("Port", select(ports.map((p) => [p.port, `${p.port} · ${p.driver}${p.dt.length ? " · " + p.dt.join("/") : ""}${p.console ? " (kernel console)" : ""}`]), port, (v) => { port = v; })),
          field("Baud rate", select(info.bauds.map((b) => [b, String(b)]), baud, (v) => { baud = Number(v); })),
          h("div.io-form", null,
            field("Data bits", select([[8, "8"], [7, "7"], [6, "6"], [5, "5"]], 8, (v) => { dataBits = Number(v); })),
            field("Parity", select([["none", "None"], ["even", "Even"], ["odd", "Odd"]], "none", (v) => { parity = v; })),
            field("Stop bits", select([[1, "1"], [2, "2"]], 1, (v) => { stop = Number(v); })),
            field("Flow control", select([["none", "None"], ["rtscts", "RTS/CTS"], ["xonxoff", "XON/XOFF"]], "none", (v) => { flow = v; }))),
          h("div.hint", null, "Header UARTs need their overlay enabled (for example uart3-m1 on /dev/ttyS3).")),
        foot: [h("button.btn.ghost", { onclick: () => s.close() }, "Cancel"),
               h("button.btn.primary", { onclick: async (e) => {
                 const r = await run("io.uart.open", { port, baud, parity, stop_bits: stop, flow, data_bits: dataBits }, { btn: e.currentTarget });
                 if (r) { s.close(); active = r.term; termHost.showTerm && termHost.showTerm(r.term); renderCtl(); }
               } }, "Open")],
      });
    };

    function current() {
      return (store.get("terminals") || []).find((t) => t.kind === "serial" && t.term === active)
          || (store.get("terminals") || []).find((t) => t.kind === "serial");
    }

    async function renderCtl() {
      const t = current();
      clearInterval(modemTimer);
      if (!t) return clear(ctl, h("div.row", null, h("div.grow.muted", null, "No port open."),
        h("button.btn.sm.primary", { onclick: openPort }, icon("plus"), "Open a port")));
      active = t.term;
      const st = t.settings || {};
      const hexIn = input({ placeholder: "hex bytes, e.g. 55 aa 01", style: { fontFamily: "var(--mono)" } });
      const modem = h("div.row", { style: { gap: "6px", flexWrap: "wrap" } });
      const renderModem = (m) => clear(modem,
        ["dtr", "rts"].map((k) => h("button.btn.sm", { class: m[k] ? "primary" : "", onclick: () => conn.call("io.uart.modem", { term: t.term, [k]: !m[k] }).then(renderModem).catch((e) => toast(errText(e), "err")) }, k.toUpperCase())),
        ["cts", "dsr", "cd", "ri"].map((k) => h("span.tag", { class: m[k] ? "ok" : "" }, k.toUpperCase())));
      clear(ctl, h("div.stack", null,
        h("div.row", { style: { flexWrap: "wrap" } },
          h("div.grow", null, h("b.mono", null, t.port), h("span.muted", { style: { marginLeft: "8px", fontSize: "13px" } },
            `${st.baud} ${st.data_bits}${(st.parity || "n")[0].toUpperCase()}${st.stop_bits} · flow ${st.flow} · rx ${fmtBytes(t.rx_bytes)} tx ${fmtBytes(t.tx_bytes)}`)),
          select(((await conn.call("io.uart.ports").catch(() => ({ bauds: [st.baud] }))).bauds || [st.baud]).map((b) => [b, b + " baud"]), st.baud,
                 (v) => run("io.uart.config", { term: t.term, baud: Number(v) }, { ok: "Baud rate " + v })),
          h("button.btn.sm", { onclick: () => run("io.uart.break", { term: t.term }, { ok: "Break sent" }) }, "Break")),
        h("div.row", { style: { flexWrap: "wrap" } }, modem, h("div.grow"),
          h("div.row", { style: { minWidth: "260px", flex: "1" } }, hexIn,
            h("button.btn.sm", { onclick: () => run("io.uart.send", { term: t.term, hex: hexIn.value }, { ok: "Sent" }) }, icon("send"), "Send hex")))));
      const poll = () => conn.call("io.uart.modem", { term: t.term }).then(renderModem).catch(() => {});
      poll();
      modemTimer = setInterval(poll, 1500);
    }

    const cleanupTerm = terminal.mount(termHost, { kind: "serial", onNew: openPort });
    const off = store.on("terminals", () => renderCtl());
    return () => { off(); clearInterval(modemTimer); cleanupTerm && cleanupTerm(); };
  },
};

// ================================================================ PWM & LEDs
export const pwm = {
  id: "pwm", title: "PWM & LEDs", icon: "sliders",

  mount(root) {
    const noteBox = h("div");
    const pwmBox = h("div.stack");
    const ledCard = h("div.card", { style: { padding: "6px 14px" } });
    head(root, "PWM & LEDs", "sysfs PWM channels and the board's LEDs");
    root.append(noteBox, h("div.section-title", null, "PWM"), pwmBox,
      h("div.section", null, h("div.section-title", null, "LEDs"), ledCard));

    function renderPwm(chips) {
      if (!chips || !chips.length) return clear(pwmBox, h("div.card", null, h("div.muted", null, "No PWM controllers enabled (they need a device-tree overlay).")));
      clear(pwmBox, chips.map((c) => h("div.card", null,
        h("div.card-title", null, icon("sliders"), `pwmchip${c.chip}`, h("span.tag", null, (c.dt || []).join("/") || c.node || "")),
        c.channels.map((ch) => {
          const period = ch.period_ns || 1000000;
          const freq = input({ value: ch.exported ? String(Math.round(1e9 / period)) : "1000", inputmode: "decimal", style: { maxWidth: "140px" } });
          const dutyPct = ch.exported && ch.period_ns ? Math.round((ch.duty_ns / ch.period_ns) * 1000) / 10 : 50;
          const dutyVal = h("span.mono", { style: { width: "56px", textAlign: "right" } }, dutyPct + " %");
          const duty = h("input", { type: "range", min: "0", max: "100", step: "0.5", value: String(dutyPct),
            oninput: (e) => { dutyVal.textContent = e.target.value + " %"; },
            onchange: (e) => set({ freq_hz: Number(freq.value), duty_pct: Number(e.target.value) }) });
          const set = (p) => run("io.pwm.set", { chip: c.chip, channel: ch.channel, ...p }).then((r) => r && reload());
          return h("div.stack", { style: { gap: "10px" } },
            h("div.row", null, h("b", null, `Channel ${ch.channel}`),
              h("span.muted", { style: { fontSize: "13px" } }, ch.exported ? `${ch.duty_ns} / ${ch.period_ns} ns · ${ch.polarity}` : "not exported"),
              h("div.grow"), toggle(ch.enabled, (on) => set({ freq_hz: Number(freq.value), duty_pct: Number(duty.value), enabled: on }), { disabled: !c.writable, label: "Output on" })),
            h("div.io-form", null,
              field("Frequency (Hz)", h("div.row", null, freq, h("button.btn.sm", { onclick: () => set({ freq_hz: Number(freq.value), duty_pct: Number(duty.value) }) }, "Set"))),
              field("Polarity", seg([["normal", "Normal"], ["inversed", "Inversed"]], ch.polarity || "normal", (v) => set({ polarity: v })))),
            field("Duty cycle", h("div.range-row", null, duty, dutyVal)),
            ch.exported ? h("div", null, h("button.btn.sm.ghost", { onclick: () => run("io.pwm.unexport", { chip: c.chip, channel: ch.channel }).then(reload) }, "Unexport")) : null);
        }))));
    }

    function renderLeds(leds) {
      clear(ledCard, (leds || []).length ? h("div.list", null, leds.map((l) => h("div.list-item", null,
        h("div.grow", null, h("div.title.mono", null, l.name), h("div.meta", null, `trigger ${l.trigger} · max ${l.max}`)),
        l.max > 1 ? h("input", { type: "range", min: "0", max: String(l.max), value: String(l.brightness), style: { width: "120px" },
                                 disabled: !l.writable, onchange: (e) => run("io.led.set", { name: l.name, brightness: Number(e.target.value) }).then(reload) })
                  : toggle(l.brightness > 0, (on) => run("io.led.set", { name: l.name, brightness: on ? 1 : 0, trigger: "none" }).then(reload),
                           { disabled: !l.writable, label: l.name }),
        h("select.input", { style: { width: "auto", maxWidth: "150px" }, disabled: !l.writable,
                            onchange: (e) => run("io.led.set", { name: l.name, trigger: e.target.value }).then(reload) },
          l.triggers.map((t) => h("option", { value: t, selected: t === l.trigger }, t))))))
        : h("div.empty", null, "No LEDs."));
    }

    async function reload() {
      try {
        const [p, l] = await Promise.all([conn.call("io.pwm.list"), conn.call("io.led.list")]);
        renderPwm(p.chips);
        renderLeds(l.leds);
      } catch (e) { toast(errText(e), "err"); }
    }
    async function load() {
      try { clear(noteBox, problems(await loadInfo(), "PWM")); } catch (e) { /* shown by reload */ }
      reload();
    }
    const offReady = conn.on("ready", load);
    if (conn.status === "open") load();
    return () => offReady();
  },
};

// ========================================================================= ADC
export const adc = {
  id: "adc", title: "ADC", icon: "signal",

  mount(root) {
    const body = h("div.stack");
    head(root, "ADC", "Analog inputs (IIO) · refreshed every second");
    root.append(body);
    let timer = null;
    async function tick() {
      let r;
      try { r = await conn.call("io.adc.read"); } catch (e) { return; }
      if (!r.devices.length) return clear(body, h("div.card", null, h("div.empty", null, "No ADC (IIO voltage channels) on this machine.")));
      clear(body, r.devices.map((d) => h("div.card", null,
        h("div.card-title", null, icon("signal"), d.name, h("span.tag", null, d.device)),
        h("div.list", null, d.channels.map((c) => {
          const max = c.scale ? 4096 * c.scale : null;
          return h("div.list-item", null, h("span.mono", { style: { width: "54px" } }, "ch" + c.channel),
            h("div.grow", null, h("div.bar", null, h("i", { style: { width: max && c.mv !== null ? Math.min(100, (c.mv / max) * 100) + "%" : "0%" } }))),
            h("span.mono", { style: { width: "64px", textAlign: "right", color: "var(--muted)" } }, String(c.raw)),
            h("span.mono", { style: { width: "90px", textAlign: "right", fontWeight: 600 } }, c.mv !== null ? c.mv.toFixed(1) + " mV" : "–"));
        })))));
    }
    const start = () => { clearInterval(timer); tick(); timer = setInterval(tick, 1000); };
    const offReady = conn.on("ready", start);
    if (conn.status === "open") start();
    return () => { offReady(); clearInterval(timer); };
  },
};
