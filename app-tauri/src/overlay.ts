/*
 * Subtitle overlay window (label "overlay").
 *
 * A separate always-on-top, frameless, transparent Tauri window that mirrors the
 * latest English/Japanese transcript line — the Tauri equivalent of the old
 * Tkinter `OverlayWindow`. It listens to the same `backend-event` stream that the
 * Rust sidecar relay broadcasts to every window, and persists its own
 * position/size back to config.json via the bridge.
 */
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";

const enEl = document.getElementById("ov-en") as HTMLParagraphElement;
const jaEl = document.getElementById("ov-ja") as HTMLParagraphElement;

function last(arr: unknown): string {
  return Array.isArray(arr) && arr.length ? String(arr[arr.length - 1]) : "";
}

void listen<Record<string, unknown>>("backend-event", (e) => {
  const ev = e.payload;
  if (!ev || ev.event !== "transcript") return;
  enEl.textContent = last(ev.english);
  jaEl.textContent = last(ev.japanese);
});

// ---- persist position/size to config.json (debounced) ---------------------
const appWindow = getCurrentWindow();
let saveTimer: number | undefined;

async function savePlacement(): Promise<void> {
  try {
    const pos = await appWindow.outerPosition();
    const size = await appWindow.outerSize();
    const scale = await appWindow.scaleFactor();
    const patch = {
      overlay_position: [Math.round(pos.x / scale), Math.round(pos.y / scale)],
      overlay_size: [Math.round(size.width / scale), Math.round(size.height / scale)],
    };
    await invoke("send_backend_command", {
      message: JSON.stringify({ cmd: "update_settings", settings: patch }),
    });
  } catch {
    // window/backend not ready — ignore.
  }
}

function scheduleSave(): void {
  window.clearTimeout(saveTimer);
  saveTimer = window.setTimeout(savePlacement, 500);
}

void appWindow.onMoved(scheduleSave);
void appWindow.onResized(scheduleSave);
