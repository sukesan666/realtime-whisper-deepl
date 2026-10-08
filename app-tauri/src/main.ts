import { getCurrentWindow, getAllWindows, type Window } from "@tauri-apps/api/window";
import { LogicalPosition, LogicalSize } from "@tauri-apps/api/dpi";
import { createBackend, CHOICES, type AppState, type Settings } from "./backend";

const backend = createBackend();

/** Tauri window handle — guarded so the UI still renders in a plain browser (vite dev). */
function appWindow() {
  try {
    return getCurrentWindow();
  } catch {
    return null;
  }
}

function $<T extends HTMLElement = HTMLElement>(id: string): T {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing element #${id}`);
  return el as T;
}

function fillSelect(sel: HTMLSelectElement, options: string[], selected: string) {
  if (sel.dataset.filled !== options.join("|")) {
    sel.innerHTML = "";
    for (const opt of options) {
      const o = document.createElement("option");
      o.value = opt;
      o.textContent = opt;
      sel.appendChild(o);
    }
    sel.dataset.filled = options.join("|");
  }
  sel.value = selected;
}

function fillDeviceSelect(sel: HTMLSelectElement, state: AppState) {
  const labels = state.devices.map((d) => d.label).join("|");
  if (sel.dataset.filled !== labels) {
    sel.innerHTML = "";
    for (const d of state.devices) {
      const o = document.createElement("option");
      o.value = String(d.index);
      o.textContent = d.label;
      sel.appendChild(o);
    }
    sel.dataset.filled = labels;
  }
  if (state.settings.selectedDeviceIndex !== null) {
    sel.value = String(state.settings.selectedDeviceIndex);
  }
}

/** Only overwrite an input the user is not currently editing, to avoid caret jumps. */
function setInputValue(el: HTMLInputElement, value: string) {
  if (document.activeElement !== el) el.value = value;
}

/** Render transcript history as stacked lines (oldest → newest, top → bottom). */
function renderLines(container: HTMLElement, lines: string[]) {
  container.replaceChildren(
    ...lines.map((text) => {
      const div = document.createElement("div");
      div.className = "sub-line";
      div.textContent = text;
      return div;
    }),
  );
  container.scrollTop = container.scrollHeight;
}

// ---- overlay window control ------------------------------------------------
let overlayWin: Window | null = null;
let prevOverlayEnabled: boolean | null = null;

async function getOverlay(): Promise<Window | null> {
  if (!overlayWin) {
    try {
      overlayWin = (await getAllWindows()).find((w) => w.label === "overlay") ?? null;
    } catch {
      overlayWin = null;
    }
  }
  return overlayWin;
}

/** Show/hide the always-on-top overlay window, restoring its saved placement on show. */
async function applyOverlay(state: AppState): Promise<void> {
  const enabled = state.settings.overlayEnabled;
  if (enabled === prevOverlayEnabled) return;
  prevOverlayEnabled = enabled;
  const win = await getOverlay();
  if (!win) return;
  try {
    if (enabled) {
      const [x, y] = state.settings.overlayPosition ?? [80, 80];
      const [w, h] = state.settings.overlaySize ?? [720, 180];
      await win.setPosition(new LogicalPosition(x, y));
      await win.setSize(new LogicalSize(w, h));
      await win.setAlwaysOnTop(true);
      await win.show();
    } else {
      await win.hide();
    }
  } catch {
    // overlay window not available (e.g. plain browser dev) — ignore.
  }
}

function render(state: AppState) {
  const s: Settings = state.settings;
  void applyOverlay(state);

  // Running state → window class drives Start/Stop visuals.
  const twWindow = document.querySelector<HTMLElement>(".tw-window")!;
  twWindow.classList.toggle("running", state.running);
  $<HTMLElement>("btn-start").querySelector<HTMLElement>(".start-label")!.textContent =
    state.running ? "Stop" : "Start";

  // Toggle buttons.
  const translationBtn = $("btn-translation");
  translationBtn.classList.toggle("active", s.translationEnabled);
  translationBtn.querySelector<HTMLElement>(".state")!.textContent = s.translationEnabled
    ? "ON"
    : "OFF";

  const topmostBtn = $("btn-topmost");
  topmostBtn.classList.toggle("active", s.overlayEnabled);
  topmostBtn.querySelector<HTMLElement>(".state")!.textContent = s.overlayEnabled ? "ON" : "OFF";

  $("btn-advanced").querySelector<HTMLElement>(".arrow")!.textContent = state.advancedOpen
    ? "▲"
    : "▼";
  $("log-arrow").textContent = state.logOpen ? "▲" : "▼";

  // Collapsible panels.
  ($("advanced-panel") as HTMLElement).hidden = !state.advancedOpen;
  ($("log-body") as HTMLElement).hidden = !state.logOpen;

  // Caption.
  $("chunk-caption").textContent = `chunk ${s.chunkSeconds}s · overlap ${s.overlapSeconds}s`;

  // Subtitle history — stacked lines, newest pinned to the bottom-left.
  renderLines($("text-en"), state.english);
  renderLines($("text-ja"), state.japanese);

  // Advanced fields.
  fillDeviceSelect($<HTMLSelectElement>("fld-device"), state);
  fillSelect($<HTMLSelectElement>("fld-model"), [...CHOICES.model], s.modelName);
  fillSelect($<HTMLSelectElement>("fld-device-mode"), [...CHOICES.deviceMode], s.deviceMode);
  fillSelect($<HTMLSelectElement>("fld-backend"), [...CHOICES.backend], s.whisperBackend);
  fillSelect($<HTMLSelectElement>("fld-compute"), [...CHOICES.compute], s.computeType);
  setInputValue($<HTMLInputElement>("fld-exe"), s.whisperCppExecutable);
  setInputValue($<HTMLInputElement>("fld-model-path"), s.whisperCppModelPath);
  setInputValue($<HTMLInputElement>("fld-chunk"), String(s.chunkSeconds));
  setInputValue($<HTMLInputElement>("fld-overlap"), String(s.overlapSeconds));

  // Log lines.
  const logBody = $("log-body");
  logBody.textContent = state.logs.join("\n");
  logBody.scrollTop = logBody.scrollHeight;
}

function wire() {
  // Window controls.
  $("btn-min").addEventListener("click", () => appWindow()?.minimize());
  $("btn-max").addEventListener("click", () => appWindow()?.toggleMaximize());
  $("btn-close").addEventListener("click", () => appWindow()?.close());

  // Toolbar.
  $("btn-start").addEventListener("click", () =>
    backend.getState().running ? backend.stop() : backend.start(),
  );
  $("btn-translation").addEventListener("click", () =>
    backend.setTranslation(!backend.getState().settings.translationEnabled),
  );
  $("btn-topmost").addEventListener("click", () =>
    backend.setOverlay(!backend.getState().settings.overlayEnabled),
  );
  $("btn-advanced").addEventListener("click", () =>
    backend.setAdvancedOpen(!backend.getState().advancedOpen),
  );
  $("btn-log").addEventListener("click", () => backend.setLogOpen(!backend.getState().logOpen));

  // Advanced actions.
  $("btn-prepare-whisper").addEventListener("click", () => backend.prepareWhisper());
  $("btn-reload-devices").addEventListener("click", () => backend.reloadDevices());
  $("btn-prepare-argos").addEventListener("click", () => backend.prepareArgos());

  // Advanced field edits → settings.
  $<HTMLSelectElement>("fld-device").addEventListener("change", (e) =>
    backend.updateSetting("selectedDeviceIndex", Number((e.target as HTMLSelectElement).value)),
  );
  $<HTMLSelectElement>("fld-model").addEventListener("change", (e) =>
    backend.updateSetting("modelName", (e.target as HTMLSelectElement).value),
  );
  $<HTMLSelectElement>("fld-device-mode").addEventListener("change", (e) =>
    backend.updateSetting("deviceMode", (e.target as HTMLSelectElement).value),
  );
  $<HTMLSelectElement>("fld-backend").addEventListener("change", (e) =>
    backend.updateSetting("whisperBackend", (e.target as HTMLSelectElement).value),
  );
  $<HTMLSelectElement>("fld-compute").addEventListener("change", (e) =>
    backend.updateSetting("computeType", (e.target as HTMLSelectElement).value),
  );
  $<HTMLInputElement>("fld-exe").addEventListener("change", (e) =>
    backend.updateSetting("whisperCppExecutable", (e.target as HTMLInputElement).value),
  );
  $<HTMLInputElement>("fld-model-path").addEventListener("change", (e) =>
    backend.updateSetting("whisperCppModelPath", (e.target as HTMLInputElement).value),
  );
  $<HTMLInputElement>("fld-chunk").addEventListener("change", (e) =>
    backend.updateSetting("chunkSeconds", Number((e.target as HTMLInputElement).value) || 0),
  );
  $<HTMLInputElement>("fld-overlap").addEventListener("change", (e) =>
    backend.updateSetting("overlapSeconds", Number((e.target as HTMLInputElement).value) || 0),
  );

  // Hotkeys: Ctrl+Enter start, Esc stop (parity with the Tkinter app).
  window.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.key === "Enter") backend.start();
    else if (e.key === "Escape") backend.stop();
  });
}

window.addEventListener("DOMContentLoaded", () => {
  wire();
  backend.subscribe(render);
});
