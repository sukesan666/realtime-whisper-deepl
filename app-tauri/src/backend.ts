/*
 * Backend abstraction seam.
 *
 * The UI talks only to this interface. Today it is backed by an in-memory mock
 * (`MockBackend`) that seeds the design's sample data and echoes commands into
 * the log. The Tauri → Python sidecar wiring (MIGRATION_PLAN §2/§3) will provide
 * a real implementation of `Backend` — invoking Rust commands and listening to
 * JSON-Lines events from the Python process — without the UI layer changing.
 */

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";

export interface Device {
  index: number;
  label: string;
}

/** Mirrors the Python AppConfig / config.json schema (subset used by the UI). */
export interface Settings {
  selectedDeviceIndex: number | null;
  whisperBackend: string;
  modelName: string;
  whisperCppExecutable: string;
  whisperCppModelPath: string;
  deviceMode: string;
  computeType: string;
  chunkSeconds: number;
  overlapSeconds: number;
  translationEnabled: boolean;
  overlayEnabled: boolean;
  overlayPosition: number[];
  overlaySize: number[];
}

export interface AppState {
  running: boolean;
  advancedOpen: boolean;
  logOpen: boolean;
  /** Rolling transcript history, oldest first. Capped at MAX_TRANSCRIPT_LINES. */
  english: string[];
  japanese: string[];
  logs: string[];
  devices: Device[];
  settings: Settings;
}

/** Keep this many transcript lines on screen; older lines spill to the log file. */
export const MAX_TRANSCRIPT_LINES = 50;

/** Choice lists mirror realtime_whisper/config.py. */
export const CHOICES = {
  backend: ["whisper.cpp", "faster-whisper"],
  model: ["tiny.en", "base.en", "small.en", "medium.en", "large-v3"],
  deviceMode: ["auto", "cuda", "cpu"],
  compute: ["auto", "float16", "int8_float16", "int8"],
} as const;

export type Listener = (state: AppState) => void;

export interface Backend {
  getState(): AppState;
  subscribe(fn: Listener): () => void;

  start(): void;
  stop(): void;
  setTranslation(on: boolean): void;
  setOverlay(on: boolean): void;
  setAdvancedOpen(open: boolean): void;
  setLogOpen(open: boolean): void;
  reloadDevices(): void;
  prepareWhisper(): void;
  prepareArgos(): void;
  updateSetting<K extends keyof Settings>(key: K, value: Settings[K]): void;
}

function timestamp(): string {
  const d = new Date();
  const p = (n: number, w = 2) => String(n).padStart(w, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())},${p(d.getMilliseconds(), 3)}`
  );
}

/** Local calendar date as YYYY-MM-DD (used for the daily transcript log filename). */
function dateStamp(): string {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** Append overflowed transcript lines to the on-disk daily transcript log (via Rust). */
async function persistOverflow(lines: string[]): Promise<void> {
  if (lines.length === 0) return;
  const stamped = lines.map((l) => `${timestamp()} ${l}`);
  try {
    await invoke("append_transcript_log", { lines: stamped, date: dateStamp() });
  } catch {
    // Not running under Tauri (e.g. plain vite dev) — persistence is a no-op there.
  }
}

class MockBackend implements Backend {
  private listeners = new Set<Listener>();
  private demoTimer: number | null = null;
  private demoIndex = 0;

  private state: AppState = {
    running: false,
    advancedOpen: false,
    logOpen: false,
    english: [
      "so today we're going to take a look at how the new render pipeline actually handles occlusion culling in real time",
    ],
    japanese: [
      "今日は、新しいレンダリングパイプラインがリアルタイムでオクルージョンカリングをどう処理しているかを見ていきます",
    ],
    logs: [
      "Refreshing WASAPI loopback devices",
      "Selected audio device: 22: スピーカー (4- High Definition Audio) [Loopback] [2ch 48000Hz]",
      "Python version: 3.14.5",
      "OS: Windows 10",
      "Whisper backend: whisper.cpp",
      "Whisper model: medium.en",
    ].map((m) => `${timestamp()} [INFO] ${m}`),
    devices: [
      { index: 22, label: "22: スピーカー (4- High Definition Audio) [Loopback]" },
      { index: 8, label: "8: ヘッドフォン (Realtek(R) Audio) [Loopback]" },
    ],
    settings: {
      selectedDeviceIndex: 22,
      whisperBackend: "whisper.cpp",
      modelName: "medium.en",
      whisperCppExecutable:
        "D:\\Github\\Realtime-Whisper\\whisper.cpp\\vulkan\\bin\\whisper-cli.exe",
      whisperCppModelPath:
        "D:\\Github\\Realtime-Whisper\\whisper.cpp\\models\\ggml-medium.en.bin",
      deviceMode: "auto",
      computeType: "auto",
      chunkSeconds: 4.0,
      overlapSeconds: 1.5,
      translationEnabled: true,
      overlayEnabled: true,
      overlayPosition: [80, 80],
      overlaySize: [720, 180],
    },
  };

  // Placeholder transcript stream — replaced by real sidecar events later.
  private demoLines: Array<{ en: string; ja: string }> = [
    {
      en: "the depth buffer gets populated first before we run any of the shading passes",
      ja: "シェーディングパスを走らせる前に、まず深度バッファが埋められます",
    },
    {
      en: "and that lets us reject fragments that are completely hidden behind other geometry",
      ja: "これにより、他のジオメトリの背後に完全に隠れたフラグメントを棄却できます",
    },
    {
      en: "the nice thing is this scales really well with scene complexity",
      ja: "良いのは、これがシーンの複雑さに対して非常によくスケールする点です",
    },
    {
      en: "so even in a dense environment the overhead stays pretty low",
      ja: "そのため、密な環境でもオーバーヘッドはかなり低く保たれます",
    },
    {
      en: "next we'll look at how temporal reprojection reuses last frame's results",
      ja: "次に、時間的リプロジェクションが前フレームの結果をどう再利用するか見ていきます",
    },
  ];

  getState(): AppState {
    return this.state;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn(this.state);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn(this.state);
  }

  private log(message: string): void {
    this.state.logs = [...this.state.logs, `${timestamp()} [INFO] ${message}`].slice(-500);
  }

  /** Append a transcript line to history, cap at 50, spill the overflow to the log file. */
  private pushTranscript(english: string, japanese: string | null): void {
    if (english) {
      const next = [...this.state.english, english];
      const overflow = next.splice(0, Math.max(0, next.length - MAX_TRANSCRIPT_LINES));
      this.state.english = next;
      void persistOverflow(overflow.map((l) => `[EN] ${l}`));
    }
    if (japanese) {
      const next = [...this.state.japanese, japanese];
      const overflow = next.splice(0, Math.max(0, next.length - MAX_TRANSCRIPT_LINES));
      this.state.japanese = next;
      void persistOverflow(overflow.map((l) => `[JA] ${l}`));
    }
  }

  private startDemoStream(): void {
    if (this.demoTimer !== null) return;
    this.demoTimer = window.setInterval(() => {
      const line = this.demoLines[this.demoIndex % this.demoLines.length];
      this.demoIndex += 1;
      this.pushTranscript(line.en, this.state.settings.translationEnabled ? line.ja : null);
      this.emit();
    }, 2000);
  }

  private stopDemoStream(): void {
    if (this.demoTimer !== null) {
      window.clearInterval(this.demoTimer);
      this.demoTimer = null;
    }
  }

  start(): void {
    this.state.running = true;
    this.log("Capture started (mock)");
    this.startDemoStream();
    this.emit();
  }

  stop(): void {
    this.state.running = false;
    this.log("Stopped (mock)");
    this.stopDemoStream();
    this.emit();
  }

  setTranslation(on: boolean): void {
    this.state.settings.translationEnabled = on;
    if (!on) this.state.japanese = [];
    this.log(`Translation ${on ? "ON" : "OFF"}`);
    this.emit();
  }

  setOverlay(on: boolean): void {
    this.state.settings.overlayEnabled = on;
    this.log(`Overlay ${on ? "ON" : "OFF"}`);
    this.emit();
  }

  setAdvancedOpen(open: boolean): void {
    this.state.advancedOpen = open;
    this.emit();
  }

  setLogOpen(open: boolean): void {
    this.state.logOpen = open;
    this.emit();
  }

  reloadDevices(): void {
    this.log("Refreshing WASAPI loopback devices (mock)");
    this.emit();
  }

  prepareWhisper(): void {
    this.log("Preparing Whisper model (mock)");
    this.emit();
  }

  prepareArgos(): void {
    this.log("Checking DeepL en→ja API (mock)");
    this.emit();
  }

  updateSetting<K extends keyof Settings>(key: K, value: Settings[K]): void {
    this.state.settings[key] = value;
    this.emit();
  }
}

// ---------------------------------------------------------------------------
// Sidecar backend — talks to the Python bridge (realtime_whisper.bridge) via the
// Rust `backend-event` event stream (in) and `send_backend_command` command (out).
// ---------------------------------------------------------------------------

const DEFAULT_SETTINGS: Settings = {
  selectedDeviceIndex: null,
  whisperBackend: "whisper.cpp",
  modelName: "medium.en",
  whisperCppExecutable: "",
  whisperCppModelPath: "",
  deviceMode: "auto",
  computeType: "auto",
  chunkSeconds: 4.0,
  overlapSeconds: 1.5,
  translationEnabled: false,
  overlayEnabled: false,
  overlayPosition: [80, 80],
  overlaySize: [720, 180],
};

/** camelCase UI key ↔ snake_case config.json key. */
const SETTINGS_KEYS: Array<[keyof Settings, string]> = [
  ["selectedDeviceIndex", "selected_device_index"],
  ["whisperBackend", "whisper_backend"],
  ["modelName", "model_name"],
  ["whisperCppExecutable", "whisper_cpp_executable"],
  ["whisperCppModelPath", "whisper_cpp_model_path"],
  ["deviceMode", "device_mode"],
  ["computeType", "compute_type"],
  ["chunkSeconds", "chunk_seconds"],
  ["overlapSeconds", "overlap_seconds"],
  ["translationEnabled", "translation_enabled"],
  ["overlayEnabled", "overlay_enabled"],
  ["overlayPosition", "overlay_position"],
  ["overlaySize", "overlay_size"],
];

function settingsFromSnake(raw: Record<string, unknown>, base: Settings): Settings {
  const out = { ...base };
  for (const [camel, snake] of SETTINGS_KEYS) {
    if (raw[snake] !== undefined) (out as Record<string, unknown>)[camel] = raw[snake];
  }
  return out;
}

function snakeKey(camel: keyof Settings): string {
  return SETTINGS_KEYS.find(([c]) => c === camel)?.[1] ?? String(camel);
}

class SidecarBackend implements Backend {
  private listeners = new Set<Listener>();
  private state: AppState = {
    running: false,
    advancedOpen: false,
    logOpen: false,
    english: [],
    japanese: [],
    logs: [],
    devices: [],
    settings: { ...DEFAULT_SETTINGS },
  };

  constructor() {
    void listen<Record<string, unknown>>("backend-event", (e) => this.onEvent(e.payload));
    // The bridge may have emitted its initial "ready" before our listener was
    // attached; re-request a snapshot (with a couple of retries while it boots).
    this.send({ cmd: "get_state" });
    window.setTimeout(() => this.send({ cmd: "get_state" }), 400);
    window.setTimeout(() => this.send({ cmd: "get_state" }), 1500);
  }

  getState(): AppState {
    return this.state;
  }

  subscribe(fn: Listener): () => void {
    this.listeners.add(fn);
    fn(this.state);
    return () => this.listeners.delete(fn);
  }

  private emit(): void {
    for (const fn of this.listeners) fn(this.state);
  }

  private send(msg: object): void {
    void invoke("send_backend_command", { message: JSON.stringify(msg) }).catch(() => {
      // Backend not up yet (or not running under Tauri) — ignore.
    });
  }

  private appendLog(line: string): void {
    this.state.logs = [...this.state.logs, line].slice(-500);
  }

  private onEvent(ev: Record<string, unknown>): void {
    if (!ev || typeof ev !== "object") return;
    switch (ev.event) {
      case "ready":
      case "settings":
        if (ev.settings && typeof ev.settings === "object") {
          this.state.settings = settingsFromSnake(ev.settings as Record<string, unknown>, this.state.settings);
        }
        if (Array.isArray(ev.devices)) this.state.devices = ev.devices as Device[];
        this.emit();
        break;
      case "devices":
        if (Array.isArray(ev.devices)) this.state.devices = ev.devices as Device[];
        this.emit();
        break;
      case "transcript":
        this.state.english = Array.isArray(ev.english) ? (ev.english as string[]) : [];
        this.state.japanese = Array.isArray(ev.japanese) ? (ev.japanese as string[]) : [];
        this.emit();
        break;
      case "log":
        if (typeof ev.line === "string") {
          this.appendLog(ev.line);
          this.emit();
        }
        break;
      case "status":
        if (typeof ev.text === "string") {
          this.appendLog(`${timestamp()} [STATUS] ${ev.text}`);
          this.emit();
        }
        break;
      case "running":
        this.state.running = Boolean(ev.value);
        this.emit();
        break;
      case "error":
        if (typeof ev.text === "string") {
          this.appendLog(`${timestamp()} [ERROR] ${ev.text}`);
          this.emit();
        }
        break;
      case "exit":
        this.state.running = false;
        this.appendLog(`${timestamp()} [INFO] backend process exited`);
        this.emit();
        break;
    }
  }

  start(): void {
    this.send({ cmd: "start" });
  }

  stop(): void {
    this.send({ cmd: "stop" });
  }

  setTranslation(on: boolean): void {
    this.state.settings.translationEnabled = on;
    if (!on) this.state.japanese = [];
    this.emit();
    this.send({ cmd: "set_translation", value: on });
  }

  setOverlay(on: boolean): void {
    this.state.settings.overlayEnabled = on;
    this.emit();
    this.send({ cmd: "set_overlay", value: on });
  }

  setAdvancedOpen(open: boolean): void {
    this.state.advancedOpen = open;
    this.emit();
  }

  setLogOpen(open: boolean): void {
    this.state.logOpen = open;
    this.emit();
  }

  reloadDevices(): void {
    this.send({ cmd: "reload_devices" });
  }

  prepareWhisper(): void {
    this.send({ cmd: "prepare_whisper" });
  }

  prepareArgos(): void {
    this.send({ cmd: "prepare_argos" });
  }

  updateSetting<K extends keyof Settings>(key: K, value: Settings[K]): void {
    this.state.settings[key] = value;
    this.emit();
    this.send({ cmd: "update_settings", settings: { [snakeKey(key)]: value } });
  }
}

/** True when running inside the Tauri webview (vs. plain `vite dev` in a browser). */
function isTauri(): boolean {
  return typeof (window as unknown as { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__ !== "undefined";
}

/**
 * Factory. Under Tauri we drive the real Python sidecar; in a plain browser we
 * fall back to the in-memory mock so the UI still renders during `vite dev`.
 */
export function createBackend(): Backend {
  return isTauri() ? new SidecarBackend() : new MockBackend();
}
