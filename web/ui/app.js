/*
 * Kivi local-first chat UI.
 * Vanilla ES2020, no build step, no frameworks, no CDN.
 *
 * Safety contract:
 *  - message text is never parsed as HTML; a tiny markdown renderer builds
 *    DOM nodes for **bold**, `code` and fenced code blocks only;
 *  - every fetch is wrapped in try/catch and surfaces errors in the UI;
 *  - localStorage keeps chat history and UI preferences only — no secrets.
 */
"use strict";

const APP_VERSION = "1.0.0";
const SERVER_CONVERSATION_ID = "__server_history__";
const STORAGE_KEYS = {
  conversations: "kivi.ui.v1.conversations",
  prefs: "kivi.ui.v1.prefs",
};
const LIMITS = {
  conversations: 40,
  messagesPerConversation: 300,
  messageChars: 20000,
  composerChars: 8000,
  ttsChars: 6000,
  titleChars: 56,
  serverMessages: 200,
};
const MEDIA_LIMITS = {
  promptChars: 500,
  gallerySize: 6,
  timeoutMs: 180000,
  sizes: [512, 768, 1024],
  models: ["flux", "turbo"],
};
// POST /media/video renders a short MP4 locally from one still frame: no video key.
const MEDIA_VIDEO = {
  durations: [2, 4, 6],
  minSeconds: 2,
  maxSeconds: 8,
  sizes: [512, 768],
  timeoutMs: 240000,
};
const AVATAR_STATES = new Set(["idle", "listening", "thinking", "speaking", "error"]);
const AVATAR_LABELS = {
  idle: "Спокойна",
  listening: "Слушает",
  thinking: "Думает",
  speaking: "Говорит",
  error: "Сбой",
};
const AVATAR_CAPTIONS = {
  idle: "Готова помочь. Слушаю чат и озвучивание.",
  listening: "Слушаю микрофон. Отпустите кнопку, чтобы остановить.",
  thinking: "Думаю над ответом…",
  speaking: "Озвучиваю ответ.",
  error: "Не удалось связаться с сервером.",
};
const VIEWS = new Set(["chat", "voices", "media", "files", "crm", "settings"]);
const CRM_FALLBACK_HINT =
  "Включи расширение AltCRM в Яндекс.Браузере и открой карточку заказа. " +
  "Альтернатива: Chrome с --remote-debugging-port=9222.";
const CRM_ORDER_MAX_CHARS = 120000;
// Mirrors the server-side crm.* schemas so the UI never posts invalid payloads.
const CRM_LIMITS = {
  commentChars: 5000,
  checklistChars: 2000,
  taskCommentChars: 500,
  whenChars: 100,
  rowNameChars: 200,
  rows: 20,
  priceMax: 100000000,
  warrantyMax: 3650,
  results: 60,
};
const CRM_CHECKLIST_FIELDS = Object.freeze([
  ["deadline", "Согласованные сроки ремонта"],
  ["parts", "Согласованные зап. части"],
  ["works", "Фактические выполняемые работы"],
  ["risks", "Согласованные риски"],
]);
const CRM_ACT_FIELDS = Object.freeze(["name", "price", "warranty"]);
const CRM_FIELD_LABELS = Object.freeze({
  comment: "Комментарий",
  deadline: "Согласованные сроки ремонта",
  parts: "Согласованные зап. части",
  works: "Фактические выполняемые работы",
  risks: "Согласованные риски",
  due: "Срок выполнения",
  when_text: "Срок выполнения",
  name: "Работа",
  price: "Цена",
  warranty: "Гарантия",
});
const CRM_TABS = Object.freeze(["fill", "act", "task"]);
const VOICE_PROVIDERS = Object.freeze({
  fish: {
    label: "Fish Audio (основной)",
    hint: "Fish Audio — облачный синтез. Серверу нужен ключ FISH_AUDIO_API_KEY; без него синтез вернёт 503.",
  },
  qwen: {
    label: "Qwen3-TTS",
    hint: "Qwen3-TTS — локальная модель: нужны скачанные веса и свободная видеопамять. Первый синтез может быть медленным.",
  },
  silero: {
    label: "Silero",
    hint: "Silero — локальный быстрый движок. Модель скачивается в data/models, ключ не нужен.",
  },
  piper: {
    label: "Piper",
    hint: "Piper — локальный движок на файлах .onnx. Ключ не нужен, тембр зависит от выбранного голоса.",
  },
});
const DEFAULT_PROVIDER_HINT = "Выберите движок и нажмите «Сохранить».";

const timeFmt = new Intl.DateTimeFormat("ru-RU", { hour: "2-digit", minute: "2-digit" });
const shortDateFmt = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short" });
const longDateFmt = new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "long" });
const fullDateFmt = new Intl.DateTimeFormat("ru-RU", {
  day: "numeric",
  month: "long",
  year: "numeric",
});
const numberFmt = new Intl.NumberFormat("ru-RU");

const state = {
  apiBase: "",
  apiNote: "",
  conversations: [],
  activeId: null,
  serverHistory: null,
  historyLoading: false,
  historyEpoch: 0,
  view: "chat",
  health: { status: "unknown", version: null, checkedAt: 0, error: "" },
  healthBusy: false,
  voices: [],
  voicesState: "idle",
  voicesError: "",
  voicesLoading: false,
  settings: null,
  settingsState: "idle",
  settingsError: "",
  settingsLoading: false,
  settingsDraft: "",
  settingsSaving: false,
  settingsSaveState: "idle",
  settingsSaveMessage: "",
  media: {
    prompt: "",
    width: 1024,
    height: 1024,
    model: "flux",
    mode: "image",
    duration: 4,
    loading: false,
    error: "",
    formError: "",
    result: null,
    // Data URLs are large: keep the last generations in memory only.
    gallery: [],
    seq: 0,
    // Video has its own request slot so it never blocks image generation.
    video: {
      loading: false,
      error: "",
      result: null,
      seq: 0,
    },
  },
  files: {
    path: ".",
    // Open folder tabs: each one owns a path plus its own back/forward stack.
    // The active tab's live state stays in path/history/historyIndex below and
    // is copied into its tab on every navigation (syncActiveFilesTab).
    tabs: [],
    activeTabId: "",
    tabSeq: 0,
    // Browser-like history: only directory loads push entries. The index is
    // moved before the request starts so back/forward states update instantly.
    history: [],
    historyIndex: -1,
    // True while the path input holds an unsubmitted edit; a render must not
    // overwrite what the user is typing.
    pathDirty: false,
    pathError: "",
    entries: [],
    truncated: false,
    state: "idle",
    error: "",
    epoch: 0,
    // Sort preference survives folder changes and reloads (see loadPrefs).
    sortKey: "name",
    sortDir: "asc",
    preview: null,
    previewEpoch: 0,
    previewTrigger: null,
    copyStatus: "",
    busy: "",
    mkdirOpen: false,
    mkdirError: "",
    renameOpen: false,
    renameError: "",
    trashOpen: false,
    trashError: "",
    transferOpen: false,
    transferMode: "",
    transferError: "",
    searchQuery: "",
    searchState: "idle",
    searchError: "",
    searchMatches: [],
    searchTruncated: false,
    searchEpoch: 0,
    menuOpen: false,
    menuPath: "",
    menuType: "",
    menuRow: null,
    menuIndex: 0,
    menuKeyboard: false,
    // Entry the rename/trash/transfer forms act on; null means "use preview".
    target: null,
    // Active drag-and-drop transfer; null when no file row is being dragged.
    drag: null,
  },
  crm: {
    statusState: "idle",
    connected: false,
    mode: "extension",
    hint: "",
    url: "",
    statusError: "",
    statusDetail: "",
    readState: "idle",
    order: "",
    readError: "",
    readHint: "",
    copyStatus: "",
    statusEpoch: 0,
    readEpoch: 0,
    stats: { total: 0, unique: 0 },
    tab: "fill",
    fill: {
      comment: "",
      deadline: "",
      parts: "",
      works: "",
      risks: "",
      confirm: false,
      busy: false,
      error: "",
      validation: "",
      results: [],
      status: "",
      statusKind: "",
      epoch: 0,
    },
    act: {
      rows: [],
      confirm: false,
      busy: false,
      error: "",
      validation: "",
      results: [],
      status: "",
      statusKind: "",
      epoch: 0,
    },
    task: {
      comment: "",
      when: "",
      confirm: false,
      busy: false,
      error: "",
      validation: "",
      results: [],
      status: "",
      statusKind: "",
      epoch: 0,
    },
  },
  sending: false,
  pendingConversationId: null,
  speakingMessageId: null,
  audio: null,
  audioUrl: null,
  ttsSeq: 0,
  beepCtx: null,
  mic: {
    active: false,
    requesting: false,
    want: false,
    background: false,
    stream: null,
    ctx: null,
    source: null,
    analyser: null,
    buffer: null,
    raf: 0,
    startTs: 0,
    recorder: null,
    chunks: [],
    pcmChunks: [],
    audioCtx: null,
    processor: null,
    sampleRate: 48000,
    sending: false,
    finishing: false,
    pressAt: 0,
    pressStartedActive: false,
  },
  wake: {
    enabled: false,
    auto: false,
    timer: 0,
    probing: false,
    command: false,
    commandTimer: 0,
    commandStarted: 0,
    lastVoiceAt: 0,
    heardVoice: false,
    requireWake: false,
  },
  prefs: {
    autoSpeak: false,
    avatarVisible: true,
    selectedVoiceId: null,
    lastActiveId: null,
  },
  clearArmed: false,
  clearTimer: 0,
  lastToast: { message: "", at: 0 },
  toastOnce: new Set(),
};

const els = {};

/* ------------------------------------------------------------------ */
/* Tiny helpers                                                       */
/* ------------------------------------------------------------------ */

function $id(id) {
  return document.getElementById(id);
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") {
      node.className = String(value);
    } else if (key === "text") {
      node.textContent = String(value);
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else if (key === "on") {
      for (const [event, handler] of Object.entries(value)) {
        node.addEventListener(event, handler);
      }
    } else {
      node.setAttribute(key, value === true ? "" : String(value));
    }
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child && child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function plural(count, forms) {
  const value = Math.abs(Number(count)) % 100;
  const last = value % 10;
  if (value > 10 && value < 20) return forms[2];
  if (last > 1 && last < 5) return forms[1];
  if (last === 1) return forms[0];
  return forms[2];
}

function firstString(...values) {
  for (const value of values) {
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function makeId(prefix) {
  try {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return `${prefix}_${window.crypto.randomUUID()}`;
    }
  } catch {
    /* fall through */
  }
  return `${prefix}_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

function toTimestamp(value, fallback = 0) {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    return value > 1e12 ? value : value * 1000;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) return parsed;
  }
  return fallback;
}

function formatTime(ts) {
  if (!ts) return "";
  const date = new Date(ts);
  return Number.isNaN(date.getTime()) ? "" : timeFmt.format(date);
}

function dayKey(ts) {
  const date = new Date(ts);
  return `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayLabel(ts) {
  const date = new Date(ts);
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const startDate = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const diff = Math.round((startToday - startDate) / 86400000);
  if (diff === 0) return "Сегодня";
  if (diff === 1) return "Вчера";
  return date.getFullYear() === now.getFullYear()
    ? longDateFmt.format(date)
    : fullDateFmt.format(date);
}

function formatHistoryTime(ts) {
  if (!ts) return "";
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return "";
  const now = new Date();
  if (dayKey(ts) === dayKey(now.getTime())) return formatTime(ts);
  if (date.getFullYear() === now.getFullYear()) return shortDateFmt.format(date);
  return fullDateFmt.format(date);
}

function storageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

function storageRemove(key) {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

/* ------------------------------------------------------------------ */
/* API                                                                */
/* ------------------------------------------------------------------ */

class ApiError extends Error {
  constructor(message, { status = 0, code = "", path = "" } = {}) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.path = path;
  }
}

function apiUrl(path) {
  return `${state.apiBase}${path.startsWith("/") ? path : `/${path}`}`;
}

async function apiRequest(path, options = {}) {
  const {
    method = "GET",
    body = null,
    timeout = 12000,
    expect = "json",
    rawBody = false,
    contentType = "application/json",
  } = options;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  const headers = {
    Accept: expect === "blob" ? "audio/wav, audio/*;q=0.9, */*;q=0.1" : "application/json",
  };
  let payload;
  if (body !== null) {
    headers["Content-Type"] = rawBody ? contentType : "application/json";
    payload = rawBody ? body : JSON.stringify(body);
  }
  try {
    const response = await fetch(apiUrl(path), {
      method,
      headers,
      body: payload,
      signal: controller.signal,
      cache: "no-store",
      credentials: "omit",
      mode: "cors",
    });
    if (!response.ok) {
      let code = "";
      try {
        const data = await response.json();
        if (data && typeof data.error === "string") code = data.error.slice(0, 160);
      } catch {
        /* non-JSON error body */
      }
      throw new ApiError(code || `HTTP ${response.status}`, {
        status: response.status,
        code,
        path,
      });
    }
    if (expect === "blob") return await response.blob();
    if (expect === "none") return null;
    const text = await response.text();
    if (!text) return {};
    try {
      return JSON.parse(text);
    } catch {
      throw new ApiError("Некорректный JSON в ответе сервера", {
        status: response.status,
        code: "bad-json",
        path,
      });
    }
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (error && error.name === "AbortError") {
      throw new ApiError("Таймаут запроса", { code: "timeout", path });
    }
    throw new ApiError("Сеть недоступна", { code: "network", path });
  } finally {
    window.clearTimeout(timer);
  }
}

function describeApiError(error, context = "запросе") {
  if (!(error instanceof ApiError)) {
    return `Не удалось выполнить ${context}: ${
      error instanceof Error ? error.message : "неизвестная ошибка"
    }.`;
  }
  if (error.code === "network") {
    return "Нет связи с локальным сервером Kivi. Проверьте, что запущен kivi serve, и откройте интерфейс с того же адреса (CORS не включён).";
  }
  if (error.code === "timeout") {
    return `Сервер не ответил вовремя (${context}). Попробуйте ещё раз.`;
  }
  if (error.status === 404) return `Сервер не поддерживает ${error.path || context} (404).`;
  if (error.status === 405) return `Метод ${error.path || context} не поддерживается сервером (405).`;
  if (error.status === 503) {
    return `Функция временно недоступна на сервере (503${error.code ? `: ${error.code}` : ""}).`;
  }
  if (error.status === 413) return "Запрос слишком длинный для локального API.";
  if (error.status === 400) {
    return `Сервер отклонил запрос${error.code ? `: ${error.code}` : ""}.`;
  }
  if (error.status >= 500) return `Сервер Kivi сообщил об ошибке (${error.status}).`;
  return error.message || `Не удалось выполнить ${context}.`;
}

function isUnsupported(error) {
  return error instanceof ApiError && [404, 405, 501, 503].includes(error.status);
}

/* ------------------------------------------------------------------ */
/* Preferences and stored conversations                               */
/* ------------------------------------------------------------------ */

function loadPrefs() {
  const raw = storageGet(STORAGE_KEYS.prefs);
  let parsed = null;
  if (raw) {
    try {
      const value = JSON.parse(raw);
      if (value && typeof value === "object" && !Array.isArray(value)) parsed = value;
    } catch {
      /* corrupted preferences are ignored */
    }
  }
  if (parsed) {
    state.prefs.autoSpeak = parsed.autoSpeak === true;
    state.prefs.avatarVisible = parsed.avatarVisible !== false;
    state.prefs.selectedVoiceId =
      typeof parsed.selectedVoiceId === "string" ? parsed.selectedVoiceId.slice(0, 120) : null;
    state.prefs.lastActiveId =
      typeof parsed.lastActiveId === "string" ? parsed.lastActiveId.slice(0, 80) : null;
    // Sorting is stored with the other preferences; an old or corrupted value
    // falls back to the same order the Files panel used before sorting existed.
    state.files.sortKey = FS_SORT_KEYS.has(parsed.filesSortKey)
      ? parsed.filesSortKey
      : "name";
    state.files.sortDir = parsed.filesSortDir === "desc" ? "desc" : "asc";
    // Tabs are restored before the first render; every stored path is validated
    // as workspace-relative so a corrupted value cannot escape the workspace.
    restoreFilesTabs(parsed.filesTabs, parsed.filesTabActive);
  }
  // The panel needs exactly one usable tab even without stored preferences.
  ensureFilesTabs();
}

function savePrefs() {
  storageSet(
    STORAGE_KEYS.prefs,
    JSON.stringify({
      autoSpeak: state.prefs.autoSpeak,
      avatarVisible: state.prefs.avatarVisible,
      selectedVoiceId: state.prefs.selectedVoiceId,
      lastActiveId: state.prefs.lastActiveId,
      filesSortKey: state.files.sortKey,
      filesSortDir: state.files.sortDir,
      // Open tabs persist as plain path/history pairs: tab ids are session-only
      // and are rebuilt on restore.
      filesTabs: state.files.tabs.map((tab) => ({
        path: tab.path,
        history: tab.history,
        historyIndex: tab.historyIndex,
      })),
      filesTabActive: Math.max(
        0,
        state.files.tabs.findIndex((tab) => tab.id === state.files.activeTabId),
      ),
    }),
  );
}

// Stored tab paths and history entries must stay inside the workspace: the
// address-bar validator rejects drive letters, a leading slash and "..".
function parseStoredFilesPath(raw) {
  if (typeof raw !== "string" || !raw.trim()) return "";
  const result = validateFilesPathInput(raw);
  return result.ok ? result.path : "";
}

function restoreFilesTabs(rawTabs, rawActive) {
  if (!Array.isArray(rawTabs)) return;
  const restored = [];
  // Remember which stored slot each surviving tab came from so the active tab
  // keeps its identity even when an invalid sibling is dropped.
  const rawIndices = [];
  rawTabs.slice(0, FS_TABS_LIMIT).forEach((item, rawIndex) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return;
    const path = parseStoredFilesPath(item.path);
    if (!path) return;
    const history = [];
    if (Array.isArray(item.history)) {
      // Oldest entries are dropped first if a stored stack grew too large.
      for (const entry of item.history.slice(-FS_HISTORY_LIMIT)) {
        const parsed = parseStoredFilesPath(entry);
        if (parsed) history.push(parsed);
      }
    }
    const index = Number(item.historyIndex);
    restored.push(makeFilesTab(path, history, Number.isInteger(index) ? index : null));
    rawIndices.push(rawIndex);
  });
  if (!restored.length) return;
  state.files.tabs = restored;
  const active = Number(rawActive);
  let activeIndex = 0;
  if (Number.isInteger(active) && active >= 0) {
    const mapped = rawIndices.indexOf(active);
    if (mapped >= 0) activeIndex = mapped;
    else if (active < restored.length) activeIndex = active;
  }
  state.files.activeTabId = restored[activeIndex].id;
  const tab = restored[activeIndex];
  state.files.path = tab.path;
  state.files.history = tab.history.slice();
  state.files.historyIndex = tab.historyIndex;
}

function ensureFilesTabs() {
  const fs = state.files;
  if (!Array.isArray(fs.tabs)) fs.tabs = [];
  if (!fs.tabs.length) {
    const tab = makeFilesTab(fs.path);
    fs.tabs = [tab];
    fs.activeTabId = tab.id;
    fs.path = tab.path;
    fs.history = tab.history.slice();
    fs.historyIndex = tab.historyIndex;
    return;
  }
  if (!fs.tabs.some((tab) => tab.id === fs.activeTabId)) {
    fs.activeTabId = fs.tabs[0].id;
    // Repair the live navigation state together with the active id so a later
    // sync cannot write a foreign path into the first tab.
    applyFilesTabState(fs.tabs[0]);
  }
}

function loadConversations() {
  const raw = storageGet(STORAGE_KEYS.conversations);
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return;
    state.conversations = parsed
      .map(normalizeStoredConversation)
      .filter(Boolean)
      .slice(0, LIMITS.conversations);
  } catch {
    showToast("Не удалось прочитать локальную историю: файл повреждён.", "error");
  }
}

function normalizeStoredConversation(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const messages = Array.isArray(raw.messages)
    ? raw.messages
        .map(normalizeStoredMessage)
        .filter(Boolean)
        .slice(-LIMITS.messagesPerConversation)
    : [];
  if (!messages.length) return null;
  const createdAt = toTimestamp(raw.createdAt, Date.now());
  return {
    id:
      typeof raw.id === "string" && raw.id.trim()
        ? raw.id.slice(0, 80)
        : makeId("chat"),
    title:
      typeof raw.title === "string" && raw.title.trim()
        ? raw.title.slice(0, 80)
        : "Новый чат",
    createdAt,
    updatedAt: toTimestamp(raw.updatedAt, createdAt),
    messages,
  };
}

function normalizeStoredMessage(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const role = raw.role === "user" ? "user" : raw.role === "assistant" ? "assistant" : "";
  if (!role) return null;
  const text = typeof raw.text === "string" ? raw.text.slice(0, LIMITS.messageChars) : "";
  if (!text.trim() && raw.error !== true) return null;
  return {
    id:
      typeof raw.id === "string" && raw.id.trim()
        ? raw.id.slice(0, 80)
        : makeId("m"),
    role,
    text,
    ts: toTimestamp(raw.ts, Date.now()),
    error: raw.error === true,
    retryText:
      typeof raw.retryText === "string" ? raw.retryText.slice(0, LIMITS.composerChars) : undefined,
    taskId: typeof raw.taskId === "string" ? raw.taskId.slice(0, 80) : null,
  };
}

function persistConversations() {
  const payload = state.conversations
    .filter((conversation) => conversation.messages.length)
    .slice(0, LIMITS.conversations)
    .map((conversation) => ({
      id: conversation.id,
      title: conversation.title,
      createdAt: conversation.createdAt,
      updatedAt: conversation.updatedAt,
      messages: conversation.messages.slice(-LIMITS.messagesPerConversation),
    }));
  if (!storageSet(STORAGE_KEYS.conversations, JSON.stringify(payload))) {
    showToastOnce(
      "storage",
      "Не удалось сохранить историю в браузере: localStorage недоступен или переполнен.",
      "error",
    );
  }
}

/* ------------------------------------------------------------------ */
/* API base                                                           */
/* ------------------------------------------------------------------ */

function isLoopbackHost(hostname) {
  const host = String(hostname || "")
    .toLowerCase()
    .replace(/^\[|\]$/g, "");
  return (
    host === "localhost" ||
    host.endsWith(".localhost") ||
    host === "::1" ||
    /^127(?:\.\d{1,3}){3}$/.test(host)
  );
}

function defaultApiBase() {
  if (window.location.protocol === "http:" || window.location.protocol === "https:") {
    return window.location.origin;
  }
  return "http://127.0.0.1:8765";
}

function normalizeApiBase(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  try {
    const url = new URL(raw.trim(), window.location.href);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    const sameOrigin = url.origin === window.location.origin;
    if (!sameOrigin && !isLoopbackHost(url.hostname)) return null;
    const path = url.pathname.replace(/\/+$/, "");
    return `${url.origin}${path === "/" ? "" : path}`;
  } catch {
    return null;
  }
}

function resolveApiBase() {
  const explicit = new URLSearchParams(window.location.search).get("api");
  if (explicit) {
    const normalized = normalizeApiBase(explicit);
    if (normalized) return { base: normalized, note: "" };
    return {
      base: defaultApiBase(),
      note: "Адрес API из параметра ?api= отклонён: разрешены http(s) и только локальные адреса (или текущий origin).",
    };
  }
  if (window.KIVI_CONFIG && typeof window.KIVI_CONFIG.apiBase === "string") {
    const configured = normalizeApiBase(window.KIVI_CONFIG.apiBase);
    if (configured) return { base: configured, note: "" };
  }
  return { base: defaultApiBase(), note: "" };
}

/* ------------------------------------------------------------------ */
/* Avatar                                                             */
/* ------------------------------------------------------------------ */

const avatarController = (() => {
  let current = "idle";

  function setState(name) {
    const next = typeof name === "string" ? name.trim().toLowerCase() : "";
    if (!AVATAR_STATES.has(next)) {
      console.warn(`KiviAvatar: неизвестное состояние "${String(name)}"`);
      return current;
    }
    current = next;
    const stage = $id("avatar");
    if (stage) stage.dataset.state = next;
    const label = $id("avatar-state-label");
    if (label) label.textContent = AVATAR_LABELS[next];
    const caption = $id("avatar-caption");
    if (caption) caption.textContent = AVATAR_CAPTIONS[next];
    const svg = stage ? stage.querySelector(".kivi-face") : null;
    if (svg) svg.setAttribute("aria-label", `Аватар Kivi: ${AVATAR_LABELS[next].toLowerCase()}`);
    return current;
  }

  return {
    setState,
    getState: () => current,
    states: Array.from(AVATAR_STATES),
  };
})();

window.KiviAvatar = avatarController;

function setAvatar(name) {
  return avatarController.setState(name);
}

function idleIfQuiet() {
  if (!state.mic.active && !state.audio && state.speakingMessageId === null) {
    setAvatar("idle");
  }
}

/* ------------------------------------------------------------------ */
/* Safe markdown-ish renderer (bold, inline code, fenced code, breaks) */
/* ------------------------------------------------------------------ */

function renderMarkdown(raw) {
  const fragment = document.createDocumentFragment();
  const source = typeof raw === "string" ? raw : String(raw ?? "");
  const text =
    source.length > LIMITS.messageChars
      ? `${source.slice(0, LIMITS.messageChars)}\n…`
      : source;
  const parts = text.split("```");
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const pre = document.createElement("pre");
      const code = document.createElement("code");
      const body = part.replace(/^\n/, "").replace(/\n$/, "");
      // Drop an optional language tag on the opening fence (```python).
      code.textContent = body.replace(/^[A-Za-z0-9_+#.-]{1,24}\r?\n/, "");
      pre.append(code);
      fragment.append(pre);
      return;
    }
    let chunk = part;
    if (index > 0 && chunk.startsWith("\n")) chunk = chunk.slice(1);
    if (index < parts.length - 1 && chunk.endsWith("\n")) chunk = chunk.slice(0, -1);
    appendInlineText(fragment, chunk);
  });
  return fragment;
}

function appendInlineText(parent, text) {
  const lines = text.split("\n");
  lines.forEach((line, index) => {
    if (index > 0) parent.append(document.createTextNode("\n"));
    appendInlineLine(parent, line);
  });
}

function appendInlineLine(parent, line) {
  let buffer = "";
  let index = 0;
  const flush = () => {
    if (buffer) {
      parent.append(document.createTextNode(buffer));
      buffer = "";
    }
  };
  while (index < line.length) {
    if (line[index] === "`") {
      const end = line.indexOf("`", index + 1);
      if (end > index + 1) {
        flush();
        const code = document.createElement("code");
        code.textContent = line.slice(index + 1, end);
        parent.append(code);
        index = end + 1;
        continue;
      }
    }
    if (line.startsWith("**", index)) {
      const end = line.indexOf("**", index + 2);
      if (end > index + 2) {
        flush();
        const strong = document.createElement("strong");
        strong.textContent = line.slice(index + 2, end);
        parent.append(strong);
        index = end + 2;
        continue;
      }
    }
    buffer += line[index];
    index += 1;
  }
  flush();
}

/* ------------------------------------------------------------------ */
/* Rendering: chat                                                    */
/* ------------------------------------------------------------------ */

function getConversation(id = state.activeId) {
  if (!id) return null;
  if (id === SERVER_CONVERSATION_ID) return state.serverHistory;
  return state.conversations.find((conversation) => conversation.id === id) || null;
}

function getActiveConversation() {
  return getConversation(state.activeId);
}

function isNearBottom(box) {
  if (!box) return true;
  return box.scrollHeight - box.scrollTop - box.clientHeight < 160;
}

function renderMessages({ forceScroll = false } = {}) {
  if (!els.messageList) return;
  const box = els.messages;
  const stick = forceScroll || isNearBottom(box);
  const conversation = getActiveConversation();
  const messages = conversation ? conversation.messages : [];
  els.welcome.hidden = messages.length > 0;
  els.messageList.replaceChildren();

  let previousDay = "";
  for (const message of messages) {
    const key = message.ts ? dayKey(message.ts) : "";
    if (key && key !== previousDay) {
      els.messageList.append(renderDateSeparator(message.ts));
      previousDay = key;
    }
    els.messageList.append(renderMessage(message));
  }
  if (state.sending) els.messageList.append(renderTyping());

  updateSpeakButtons();
  if (stick) {
    window.requestAnimationFrame(() => {
      box.scrollTop = box.scrollHeight;
    });
  }
}

function renderDateSeparator(ts) {
  return el("div", { class: "date-sep", role: "separator" }, el("span", { text: dayLabel(ts) }));
}

function renderMessage(message) {
  const isUser = message.role === "user";
  const article = el("article", {
    class: `msg msg-${isUser ? "user" : "assistant"}${message.error ? " msg-error" : ""}`,
    dataset: { messageId: message.id },
  });
  const avatar = el("div", { class: "msg-avatar", "aria-hidden": "true" }, isUser ? "Вы" : "K");
  const body = el("div", { class: "msg-body" });
  const bubble = el("div", { class: "bubble" });

  if (message.error) {
    bubble.append(el("p", { class: "error-title", text: "Не удалось получить ответ" }));
    bubble.append(el("p", { class: "error-detail", text: message.text || "Неизвестная ошибка." }));
  } else {
    const markdown = el("div", { class: "md" });
    markdown.append(renderMarkdown(message.text));
    bubble.append(markdown);
  }
  body.append(bubble);

  const meta = el("div", { class: "msg-meta" });
  if (message.ts) {
    const time = el("time", { class: "msg-time", text: formatTime(message.ts) });
    try {
      time.setAttribute("datetime", new Date(message.ts).toISOString());
    } catch {
      /* invalid date: no datetime attribute */
    }
    meta.append(time);
  }
  if (!isUser && !message.error) {
    const speaking = state.speakingMessageId === message.id;
    const speak = el("button", {
      type: "button",
      class: `msg-action${speaking ? " is-playing" : ""}`,
      dataset: { speakMessage: message.id },
      text: speaking ? "Стоп" : "Озвучить",
      "aria-label": speaking ? "Остановить озвучивание ответа" : "Озвучить ответ",
    });
    meta.append(speak);
  }
  if (message.error) {
    meta.append(
      el("button", {
        type: "button",
        class: "msg-action",
        dataset: { retryMessage: message.id },
        text: "Повторить",
        "aria-label": "Повторить запрос",
      }),
    );
  }
  if (meta.childNodes.length) body.append(meta);

  article.append(avatar, body);
  return article;
}

function renderTyping() {
  const article = el("div", { class: "msg msg-assistant msg-typing" });
  article.append(el("div", { class: "msg-avatar", "aria-hidden": "true" }, "K"));
  const body = el("div", { class: "msg-body" });
  const bubble = el("div", { class: "bubble typing-bubble" });
  const dots = el("span", { class: "typing-dots", "aria-hidden": "true" });
  dots.append(el("span"), el("span"), el("span"));
  bubble.append(dots, el("span", { class: "sr-only", text: "Kivi печатает…" }));
  body.append(bubble);
  article.append(body);
  return article;
}

function updateSpeakButtons() {
  document.querySelectorAll("[data-speak-message]").forEach((button) => {
    const isSpeaking = button.getAttribute("data-speak-message") === state.speakingMessageId;
    button.textContent = isSpeaking ? "Стоп" : "Озвучить";
    button.setAttribute(
      "aria-label",
      isSpeaking ? "Остановить озвучивание ответа" : "Озвучить ответ",
    );
    button.classList.toggle("is-playing", isSpeaking);
  });
}

function findMessageById(id) {
  const conversation = getActiveConversation();
  if (!conversation) return null;
  return conversation.messages.find((message) => message.id === id) || null;
}

/* ------------------------------------------------------------------ */
/* Rendering: history                                                 */
/* ------------------------------------------------------------------ */

function renderHistory() {
  if (!els.historyList) return;
  els.historyList.replaceChildren();
  const local = [...state.conversations].sort((a, b) => b.updatedAt - a.updatedAt);
  const entries = [];
  if (state.serverHistory && state.serverHistory.messages.length) {
    entries.push(state.serverHistory);
  }
  entries.push(...local);
  for (const conversation of entries) {
    els.historyList.append(renderHistoryItem(conversation));
  }
  els.historyEmpty.hidden = entries.length > 0;
  renderClearButton();
}

function renderHistoryItem(conversation) {
  const isServer = conversation.id === SERVER_CONVERSATION_ID;
  const item = el("li", { class: "history-item-wrap" });
  const button = el("button", {
    type: "button",
    class: "history-item",
    "aria-current": state.activeId === conversation.id ? "true" : null,
    title: conversation.title,
  });
  button.append(el("span", { class: "history-title", text: conversation.title || "Без названия" }));
  const meta = el("span", { class: "history-meta" });
  if (isServer) meta.append(el("span", { class: "history-badge", text: "API" }));
  meta.append(
    el("span", {
      text: `${conversation.messages.length} ${plural(conversation.messages.length, [
        "сообщение",
        "сообщения",
        "сообщений",
      ])} · ${formatHistoryTime(conversation.updatedAt)}`,
    }),
  );
  button.append(meta);
  button.addEventListener("click", () => selectConversation(conversation.id));
  item.append(button);

  if (!isServer) {
    const remove = el("button", {
      type: "button",
      class: "history-del",
      "aria-label": `Удалить чат «${conversation.title}»`,
      title: "Удалить чат",
      text: "×",
    });
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      deleteConversation(conversation.id);
    });
    item.append(remove);
  }
  return item;
}

function renderClearButton() {
  if (!els.clearHistory) return;
  els.clearHistory.textContent = state.clearArmed ? "Точно?" : "Очистить";
  els.clearHistory.classList.toggle("is-armed", state.clearArmed);
  els.clearHistory.setAttribute(
    "aria-label",
    state.clearArmed ? "Подтвердить очистку истории" : "Очистить историю чатов",
  );
  els.clearHistory.disabled = !state.serverHistory && state.conversations.length === 0;
}

/* ------------------------------------------------------------------ */
/* Chat actions                                                       */
/* ------------------------------------------------------------------ */

function titleFromText(text) {
  const cleaned = String(text || "").replace(/\s+/g, " ").trim();
  if (!cleaned) return "Новый чат";
  return cleaned.length > LIMITS.titleChars
    ? `${cleaned.slice(0, LIMITS.titleChars - 1)}…`
    : cleaned;
}

function createConversation() {
  const now = Date.now();
  const conversation = {
    id: makeId("chat"),
    title: "Новый чат",
    createdAt: now,
    updatedAt: now,
    messages: [],
  };
  state.conversations.unshift(conversation);
  while (state.conversations.length > LIMITS.conversations) state.conversations.pop();
  state.activeId = conversation.id;
  state.prefs.lastActiveId = conversation.id;
  savePrefs();
  return conversation;
}

function startNewChat({ focus = true } = {}) {
  if (state.sending) {
    showToast("Дождитесь ответа Kivi на предыдущее сообщение.", "info");
    return;
  }
  state.activeId = null;
  state.prefs.lastActiveId = null;
  savePrefs();
  stopAudio();
  resetClearHistory();
  setView("chat", { focus: false });
  renderHistory();
  renderMessages({ forceScroll: true });
  updateViewHeader();
  renderComposer();
  if (focus) els.composerInput.focus();
}

function selectConversation(id) {
  if (!getConversation(id)) return;
  if (state.sending && id !== state.activeId) {
    showToast("Дождитесь ответа Kivi, чтобы переключить чат.", "info");
    return;
  }
  state.activeId = id;
  state.prefs.lastActiveId = id === SERVER_CONVERSATION_ID ? null : id;
  savePrefs();
  resetClearHistory();
  closeSidebar();
  setView("chat", { focus: false });
  renderHistory();
  renderMessages({ forceScroll: true });
  updateViewHeader();
  renderComposer();
  window.requestAnimationFrame(() => {
    try {
      els.messages.focus({ preventScroll: true });
    } catch {
      els.messages.focus();
    }
  });
}

function deleteConversation(id) {
  const index = state.conversations.findIndex((conversation) => conversation.id === id);
  if (index < 0) return;
  if (state.sending && state.activeId === id) {
    showToast("Дождитесь ответа Kivi, чтобы удалить этот чат.", "info");
    return;
  }
  const [removed] = state.conversations.splice(index, 1);
  if (state.activeId === id) {
    state.activeId = null;
    state.prefs.lastActiveId = null;
    savePrefs();
    renderMessages({ forceScroll: true });
  }
  persistConversations();
  renderHistory();
  updateViewHeader();
  renderComposer();
  showToast(`Чат «${removed.title}» удалён.`, "info");
}

function armClearHistory() {
  if (els.clearHistory.disabled) return;
  if (state.sending) {
    showToast("Дождитесь ответа Kivi, чтобы очистить историю.", "info");
    return;
  }
  if (!state.clearArmed) {
    state.clearArmed = true;
    renderClearButton();
    state.clearTimer = window.setTimeout(resetClearHistory, 4000);
    return;
  }
  clearHistory();
}

function resetClearHistory() {
  window.clearTimeout(state.clearTimer);
  state.clearTimer = 0;
  state.clearArmed = false;
  renderClearButton();
}

function clearHistory() {
  resetClearHistory();
  const hadServer = Boolean(state.serverHistory);
  state.historyEpoch += 1;
  state.conversations = [];
  state.serverHistory = null;
  state.activeId = null;
  state.prefs.lastActiveId = null;
  storageRemove(STORAGE_KEYS.conversations);
  savePrefs();
  stopAudio();
  stopMic();
  renderHistory();
  renderMessages({ forceScroll: true });
  updateViewHeader();
  renderComposer();
  showToast(
    hadServer
      ? "История очищена локально. Серверная история скрыта до перезагрузки страницы."
      : "История чатов очищена.",
    "info",
    6000,
  );
}

function resizeComposer() {
  if (!els.composerInput) return;
  const input = els.composerInput;
  input.style.height = "auto";
  const max = 176;
  input.style.height = `${Math.max(28, Math.min(input.scrollHeight, max))}px`;
  input.style.overflowY = input.scrollHeight > max ? "auto" : "hidden";
}

function renderComposer() {
  if (!els.sendBtn) return;
  const hasText = els.composerInput.value.trim().length > 0;
  els.sendBtn.disabled = state.sending || !hasText;
  els.composer.setAttribute("aria-busy", state.sending ? "true" : "false");
  const serverReadonly = state.activeId === SERVER_CONVERSATION_ID;
  els.readonlyNote.hidden = !serverReadonly;
  if (state.sending) {
    els.composerInput.placeholder = "Kivi отвечает…";
  } else if (serverReadonly) {
    els.composerInput.placeholder = "Новое сообщение начнёт новый чат…";
  } else {
    els.composerInput.placeholder = "Напишите Kivi…";
  }
}

function submitComposer() {
  const text = els.composerInput.value;
  if (!text.trim() || state.sending) return;
  void sendMessage(text);
}

async function sendMessage(rawText) {
  const text = String(rawText ?? "").trim().slice(0, LIMITS.composerChars);
  if (!text) return;
  if (state.sending) {
    showToast("Kivi ещё отвечает на предыдущее сообщение.", "info");
    return;
  }

  let conversation = getActiveConversation();
  if (!conversation || conversation.id === SERVER_CONVERSATION_ID) {
    conversation = createConversation();
  }

  const userMessage = { id: makeId("m"), role: "user", text, ts: Date.now() };
  const firstUserMessage = !conversation.messages.some((message) => message.role === "user");
  conversation.messages.push(userMessage);
  if (firstUserMessage) conversation.title = titleFromText(text);
  conversation.updatedAt = Date.now();

  state.activeId = conversation.id;
  state.prefs.lastActiveId = conversation.id;
  state.sending = true;
  state.pendingConversationId = conversation.id;
  els.composerInput.value = "";
  resizeComposer();
  renderComposer();
  renderHistory();
  renderMessages({ forceScroll: true });
  updateViewHeader();
  savePrefs();
  persistConversations();
  stopAudio();
  setAvatar("thinking");
  hideConnectionNotice();

  try {
    const data = await apiRequest("/chat", {
      method: "POST",
      body: { message: text },
      timeout: 180000,
    });
    if (!data || typeof data.answer !== "string") {
      throw new ApiError("Сервер вернул ответ без поля answer.", {
        path: "/chat",
        code: "bad-answer",
      });
    }
    const answer = data.answer.trim();
    if (!answer) {
      throw new ApiError("Сервер вернул пустой ответ.", { path: "/chat", code: "empty-answer" });
    }
    const assistantMessage = {
      id: makeId("m"),
      role: "assistant",
      text: answer,
      ts: Date.now(),
      taskId: typeof data.task_id === "string" ? data.task_id : null,
    };
    conversation.messages.push(assistantMessage);
    conversation.updatedAt = Date.now();
    hideConnectionNotice();
    if (state.health.status !== "online") {
      state.health = {
        status: "online",
        version: state.health.version,
        checkedAt: Date.now(),
        error: "",
      };
      updateStatusUI();
    }
    if (state.prefs.autoSpeak) {
      void speakText(answer, { messageId: assistantMessage.id });
    } else {
      setAvatar("idle");
    }
  } catch (error) {
    const detail = describeApiError(error, "чате");
    conversation.messages.push({
      id: makeId("m"),
      role: "assistant",
      text: detail,
      ts: Date.now(),
      error: true,
      retryText: text,
    });
    conversation.updatedAt = Date.now();
    setAvatar("error");
    if (error instanceof ApiError && error.code === "network") {
      state.health = {
        status: "offline",
        version: state.health.version,
        checkedAt: Date.now(),
        error: detail,
      };
      updateStatusUI();
      showConnectionNotice(detail);
    }
    window.setTimeout(() => {
      if (avatarController.getState() === "error" && !state.mic.active && !state.audio) {
        setAvatar("idle");
      }
    }, 3200);
  } finally {
    state.sending = false;
    state.pendingConversationId = null;
    conversation.messages = conversation.messages.slice(-LIMITS.messagesPerConversation);
    persistConversations();
    renderHistory();
    renderMessages();
    renderComposer();
    updateViewHeader();
  }
}

function retryMessage(messageId) {
  if (state.sending) {
    showToast("Kivi ещё отвечает. Попробуйте повторить позже.", "info");
    return;
  }
  const conversation = getActiveConversation();
  if (!conversation) return;
  const index = conversation.messages.findIndex((message) => message.id === messageId);
  if (index < 0) return;
  const failed = conversation.messages[index];
  const previous = conversation.messages[index - 1];
  const text =
    typeof failed.retryText === "string" && failed.retryText.trim()
      ? failed.retryText
      : previous && previous.role === "user"
        ? previous.text
        : "";
  if (!text) return;
  conversation.messages.splice(index, 1);
  if (previous && previous.role === "user") {
    conversation.messages.splice(index - 1, 1);
  }
  conversation.updatedAt = Date.now();
  renderMessages({ forceScroll: true });
  void sendMessage(text);
}

/* ------------------------------------------------------------------ */
/* Text-to-speech                                                     */
/* ------------------------------------------------------------------ */

function stopAudio() {
  state.ttsSeq += 1;
  const audio = state.audio;
  if (audio) {
    try {
      audio.pause();
    } catch {
      /* ignore */
    }
    try {
      audio.removeAttribute("src");
      audio.load();
    } catch {
      /* ignore */
    }
    state.audio = null;
  }
  if (state.audioUrl) {
    try {
      URL.revokeObjectURL(state.audioUrl);
    } catch {
      /* ignore */
    }
    state.audioUrl = null;
  }
  state.speakingMessageId = null;
  updateSpeakButtons();
  idleIfQuiet();
}

function describeTtsError(error) {
  if (error && error.name === "NotAllowedError") {
    return "Браузер заблокировал автозапуск звука. Нажмите «Озвучить» на сообщении.";
  }
  if (!(error instanceof ApiError)) {
    return error instanceof Error ? `Не удалось озвучить: ${error.message}` : "Не удалось озвучить ответ.";
  }
  if (error.code === "network") return "Не удалось озвучить: нет связи с локальным сервером.";
  if (error.code === "timeout") return "Синтез речи не ответил вовремя.";
  if (error.status === 404 || error.status === 405) {
    return "Озвучивание недоступно: сервер не предоставляет POST /tts.";
  }
  if (error.status === 503) return "Озвучивание не настроено на сервере (503).";
  if (error.status === 400) return "Сервер отклонил текст для озвучивания.";
  if (error.status >= 500) return "Сервер не смог синтезировать речь.";
  if (error.code === "empty-audio") return "Сервер вернул пустой аудиофайл.";
  return error.message || "Не удалось озвучить ответ.";
}

async function speakText(text, { messageId = null, silent = false } = {}) {
  const value = String(text || "").trim();
  if (!value) return;
  if (state.mic.active || state.mic.requesting) stopMic();
  stopAudio();
  const token = state.ttsSeq + 1;
  state.ttsSeq = token;
  state.speakingMessageId = messageId;
  updateSpeakButtons();
  setAvatar("thinking");

  try {
    const blob = await apiRequest("/tts", {
      method: "POST",
      body: { text: value.slice(0, LIMITS.ttsChars) },
      expect: "blob",
      timeout: 120000,
    });
    if (token !== state.ttsSeq) return;
    if (!blob || !blob.size) {
      throw new ApiError("Сервер вернул пустой аудиофайл.", { path: "/tts", code: "empty-audio" });
    }
    if (blob.type && /json|html|text\//i.test(blob.type)) {
      throw new ApiError("Сервер вернул не аудио.", { path: "/tts", code: "bad-audio" });
    }
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.preload = "auto";
    state.audio = audio;
    state.audioUrl = url;
    audio.addEventListener(
      "ended",
      () => {
        if (state.audio === audio) stopAudio();
      },
      { once: true },
    );
    audio.addEventListener(
      "error",
      () => {
        if (state.audio === audio) {
          stopAudio();
          showToast("Браузер не смог воспроизвести полученное аудио.", "error");
        }
      },
      { once: true },
    );
    await audio.play();
    if (token !== state.ttsSeq) return;
    setAvatar("speaking");
    updateSpeakButtons();
  } catch (error) {
    if (token !== state.ttsSeq) return;
    if (state.audio) {
      stopAudio();
    } else {
      state.speakingMessageId = null;
      updateSpeakButtons();
      idleIfQuiet();
    }
    const message = describeTtsError(error);
    if (!silent) showToast(message, "error");
    if (isUnsupported(error)) state.ttsUnavailable = true;
  }
}

/* ------------------------------------------------------------------ */
/* Audio preferences and test tone                                    */
/* ------------------------------------------------------------------ */

function syncAutoSpeakUI() {
  const enabled = state.prefs.autoSpeak;
  if (els.speakerToggle) {
    els.speakerToggle.classList.toggle("is-active", enabled);
    els.speakerToggle.setAttribute("aria-pressed", enabled ? "true" : "false");
    els.speakerToggle.setAttribute(
      "aria-label",
      `Автоозвучивание ответов: ${enabled ? "включено" : "выключено"}`,
    );
  }
  if (els.autoSpeakSetting) els.autoSpeakSetting.checked = enabled;
}

function setAutoSpeak(value, { announce = true } = {}) {
  const enabled = Boolean(value);
  if (state.prefs.autoSpeak === enabled) {
    syncAutoSpeakUI();
    return;
  }
  state.prefs.autoSpeak = enabled;
  savePrefs();
  syncAutoSpeakUI();
  if (!enabled) stopAudio();
  if (announce) {
    showToast(enabled ? "Автоозвучивание включено." : "Автоозвучивание выключено.", "info");
  }
}

function setAvatarVisible(visible, { persist = true } = {}) {
  state.prefs.avatarVisible = Boolean(visible);
  if (els.app) els.app.dataset.avatar = state.prefs.avatarVisible ? "on" : "off";
  if (els.avatarToggle) {
    els.avatarToggle.setAttribute("aria-pressed", state.prefs.avatarVisible ? "true" : "false");
    els.avatarToggle.setAttribute(
      "aria-label",
      state.prefs.avatarVisible ? "Скрыть панель аватара" : "Показать панель аватара",
    );
  }
  if (persist) savePrefs();
}

function applyPreferences() {
  setAvatarVisible(state.prefs.avatarVisible, { persist: false });
  syncAutoSpeakUI();
}

function setToneStatus(text, isError) {
  if (!els.testToneStatus) return;
  els.testToneStatus.textContent = text;
  els.testToneStatus.classList.toggle("is-error", Boolean(isError));
}

async function playTestTone() {
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) {
    setToneStatus("WebAudio недоступен в этом браузере.", true);
    showToast("WebAudio недоступен: проверка звука невозможна.", "error");
    return;
  }
  try {
    if (!state.beepCtx || state.beepCtx.state === "closed") {
      state.beepCtx = new Ctor();
    }
    const ctx = state.beepCtx;
    if (ctx.state === "suspended") await ctx.resume();
    const now = ctx.currentTime;
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = "sine";
    osc.frequency.setValueAtTime(440, now);
    gain.gain.setValueAtTime(0.0001, now);
    gain.gain.exponentialRampToValueAtTime(0.16, now + 0.04);
    gain.gain.exponentialRampToValueAtTime(0.0001, now + 0.72);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start(now);
    osc.stop(now + 0.78);
    osc.addEventListener(
      "ended",
      () => {
        try {
          osc.disconnect();
          gain.disconnect();
        } catch {
          /* ignore */
        }
      },
      { once: true },
    );
    setToneStatus("Сигнал 440 Гц отправлен на устройство вывода.", false);
  } catch (error) {
    console.error("Kivi UI: test tone failed", error);
    setToneStatus("Не удалось воспроизвести сигнал. Проверьте звук браузера.", true);
    showToast("Не удалось воспроизвести тестовый сигнал 440 Гц.", "error");
  }
}

/* ------------------------------------------------------------------ */
/* Microphone (push-to-talk placeholder, no server-side recording)    */
/* ------------------------------------------------------------------ */

function setMicStatus(text, kind = "") {
  if (!els.micStatus) return;
  els.micStatus.textContent = text;
  els.micStatus.classList.toggle("is-error", kind === "error");
  els.micStatus.classList.toggle("is-ok", kind === "ok");
}

function describeMicError(error) {
  const name = error && error.name ? String(error.name) : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Доступ к микрофону запрещён. Разрешите его в настройках браузера для этого адреса.";
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return "Микрофон не найден. Подключите устройство ввода.";
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return "Микрофон занят другим приложением.";
  }
  if (name === "OverconstrainedError") {
    return "Не удалось подобрать параметры микрофона.";
  }
  return "Не удалось получить доступ к микрофону.";
}

function startMicMeter(stream) {
  stopMicMeter();
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) return;
  try {
    const ctx = new Ctor();
    const source = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    state.mic.ctx = ctx;
    state.mic.source = source;
    state.mic.analyser = analyser;
    state.mic.buffer = new Uint8Array(analyser.fftSize);
    if (ctx.state === "suspended") {
      void ctx.resume().catch(() => {});
    }
    const tick = () => {
      if (!state.mic.active || !state.mic.analyser || !state.mic.buffer) return;
      state.mic.analyser.getByteTimeDomainData(state.mic.buffer);
      let sum = 0;
      for (let i = 0; i < state.mic.buffer.length; i += 1) {
        const value = (state.mic.buffer[i] - 128) / 128;
        sum += value * value;
      }
      const rms = Math.sqrt(sum / state.mic.buffer.length);
      const level = Math.max(2, Math.min(100, Math.round(rms * 260)));
      if (els.avatarMeterFill) els.avatarMeterFill.style.width = `${level}%`;
      state.mic.raf = window.requestAnimationFrame(tick);
    };
    state.mic.raf = window.requestAnimationFrame(tick);
  } catch (error) {
    console.warn("Kivi UI: mic meter unavailable", error);
  }
}

function stopMicMeter() {
  if (state.mic.raf) {
    window.cancelAnimationFrame(state.mic.raf);
    state.mic.raf = 0;
  }
  if (state.mic.source) {
    try {
      state.mic.source.disconnect();
    } catch {
      /* ignore */
    }
    state.mic.source = null;
  }
  state.mic.analyser = null;
  state.mic.buffer = null;
  if (state.mic.ctx) {
    const ctx = state.mic.ctx;
    state.mic.ctx = null;
    try {
      void ctx.close().catch(() => {});
    } catch {
      /* ignore */
    }
  }
  if (els.avatarMeterFill) els.avatarMeterFill.style.width = "0%";
}

function cleanupMic() {
  state.mic.active = false;
  state.mic.background = false;
  state.mic.recorder = null;
  state.mic.chunks = [];
  state.mic.pcmChunks = [];
  stopMicMeter();
  if (state.mic.processor) {
    try {
      state.mic.processor.disconnect();
    } catch {
      /* ignore */
    }
    state.mic.processor = null;
  }
  if (state.mic.audioCtx) {
    const ctx = state.mic.audioCtx;
    state.mic.audioCtx = null;
    try {
      void ctx.close().catch(() => {});
    } catch {
      /* ignore */
    }
  }
  if (state.mic.stream) {
    try {
      state.mic.stream.getTracks().forEach((track) => track.stop());
    } catch {
      /* ignore */
    }
    state.mic.stream = null;
  }
  if (els.micBtn) {
    els.micBtn.classList.remove("is-active");
    els.micBtn.setAttribute("aria-pressed", "false");
  }
  if (els.avatarPanel) els.avatarPanel.classList.remove("is-mic");
  idleIfQuiet();
}

function stopMic() {
  const wasActive = state.mic.active;
  state.mic.want = false;
  state.mic.active = false;
  stopMicMeter();
  if (state.mic.finishing || state.mic.sending) return;
  if (wasActive) {
    void finishMicRecording();
    return;
  }
  cleanupMic();
  setMicStatus("Микрофон выключен.", "ok");
}

function setWakeUI(enabled) {
  state.wake.enabled = enabled;
  if (!els.wakeBtn) return;
  els.wakeBtn.classList.toggle("is-active", enabled);
  els.wakeBtn.setAttribute("aria-pressed", enabled ? "true" : "false");
  els.wakeBtn.setAttribute(
    "aria-label",
    `Фоновое прослушивание имени «Киви»: ${enabled ? "включено" : "выключено"}`,
  );
}

function stopWakeMode({ keepAuto = false } = {}) {
  if (state.wake.timer) {
    window.clearInterval(state.wake.timer);
    state.wake.timer = 0;
  }
  if (state.wake.commandTimer) {
    window.clearInterval(state.wake.commandTimer);
    state.wake.commandTimer = 0;
  }
  state.wake.command = false;
  state.wake.requireWake = false;
  state.wake.probing = false;
  if (!keepAuto) state.wake.auto = false;
  setWakeUI(false);
  if (state.mic.background) {
    state.mic.background = false;
    state.mic.want = false;
    state.mic.active = false;
    cleanupMic();
    setMicStatus("Фоновое прослушивание выключено.");
  }
}

function trimMicSamples(seconds) {
  const rate = state.mic.sampleRate || 48000;
  let remaining = Math.max(1, Math.round(rate * seconds));
  const kept = [];
  for (let index = state.mic.pcmChunks.length - 1; index >= 0 && remaining > 0; index -= 1) {
    const chunk = state.mic.pcmChunks[index];
    const take = Math.min(chunk.length, remaining);
    kept.unshift(chunk.slice(chunk.length - take));
    remaining -= take;
  }
  state.mic.pcmChunks = kept;
}

function micRms(seconds = 0.25) {
  const rate = state.mic.sampleRate || 48000;
  const need = Math.max(1, Math.round(rate * seconds));
  let total = 0;
  let sum = 0;
  for (let index = state.mic.pcmChunks.length - 1; index >= 0 && total < need; index -= 1) {
    const chunk = state.mic.pcmChunks[index];
    const count = Math.min(chunk.length, need - total);
    for (let offset = chunk.length - count; offset < chunk.length; offset += 1) {
      const value = chunk[offset];
      sum += value * value;
    }
    total += count;
  }
  return total ? Math.sqrt(sum / total) : 0;
}

function normalizeTranscript(text) {
  return String(text || "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function hasWakeWord(text) {
  return /(^|\s)(киви|kivi)(\s|$)/i.test(normalizeTranscript(text));
}

function stripWakeWord(text) {
  return String(text || "")
    .replace(/^\s*(киви|kivi)\s*[,!?.-]?\s*/i, "")
    .trim();
}

const STT_HALLUCINATIONS = [
  "продолжение следует",
  "субтитры",
  "редактор субтитров",
  "спасибо за просмотр",
  "подписывайтесь",
  "ставьте лайки",
  "dimatorzok",
];

function looksLikeHallucination(text) {
  const value = normalizeTranscript(text);
  if (!value) return true;
  return STT_HALLUCINATIONS.some((item) => value === item || value.startsWith(`${item} `));
}

function scheduleWakeResume(delay = 1200) {
  if (!state.wake.auto) return;
  window.setTimeout(() => {
    if (!state.wake.auto) return;
    if (state.audio || state.speakingMessageId !== null) {
      scheduleWakeResume(900);
      return;
    }
    if (!state.mic.active) void startWakeMode();
  }, delay);
}

async function probeWake() {
  if (!state.wake.enabled || state.wake.probing || !state.mic.active) return;
  if (state.mic.sending || state.mic.finishing || state.audio || state.speakingMessageId !== null) {
    return;
  }
  const rate = state.mic.sampleRate || 48000;
  const needed = Math.round(rate * 2);
  const available = state.mic.pcmChunks.reduce((total, chunk) => total + chunk.length, 0);
  if (available < needed * 0.75) return;
  state.wake.probing = true;
  try {
    trimMicSamples(2);
    const total = state.mic.pcmChunks.reduce((sum, chunk) => sum + chunk.length, 0);
    const samples = new Float32Array(total);
    let offset = 0;
    for (const chunk of state.mic.pcmChunks) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    const wav = encodeWav(samples, rate);
    const data = await apiRequest("/wake", {
      method: "POST",
      body: wav,
      rawBody: true,
      contentType: "audio/wav",
      timeout: 12000,
    });
    if (!state.wake.enabled) return;
    if (data && data.detected === true) {
      window.clearInterval(state.wake.timer);
      state.wake.timer = 0;
      state.wake.command = true;
      state.wake.commandStarted = Date.now();
      state.wake.lastVoiceAt = Date.now();
      state.wake.heardVoice = false;
      state.wake.requireWake = true;
      state.mic.background = true;
      setMicStatus("Услышала «Киви». Говори, я подожду паузу…", "ok");
      setAvatar("listening");
      trimMicSamples(2.4);
      state.wake.commandTimer = window.setInterval(() => void probeWakeCommand(), 120);
    }
  } catch (error) {
    if (error instanceof ApiError && error.status === 404 && location.port !== "8766") {
      // The browser is showing a page served by an older Kivi process. Wake
      // is available on the current instance; move the whole window there.
      try {
        await fetch("http://127.0.0.1:8766/health", {
          mode: "no-cors",
          cache: "no-store",
        });
        window.location.replace("http://127.0.0.1:8766/?wake=1");
        return;
      } catch {
        /* current instance is not running; keep the clear error below */
      }
    }
    if (state.wake.enabled) {
      const hint =
        error instanceof ApiError && error.status === 404
          ? "Сервер не поддерживает /wake. Запусти KiviRestart.bat и открой актуальное окно."
          : describeApiError(error, "проверке имени");
      setMicStatus(`Фоновый wake: ${hint}`, "error");
    }
  } finally {
    state.wake.probing = false;
  }
}

async function startWakeMode() {
  if (!state.wake.auto || state.mic.active) return;
  setWakeUI(true);
  setMicStatus("Слушаю «Киви» в фоне…", "ok");
  await startMic({ background: true });
  if (!state.mic.active) {
    setWakeUI(false);
    return;
  }
  if (!state.wake.timer) {
    state.wake.timer = window.setInterval(() => void probeWake(), 900);
  }
}

function clearWakeCommand() {
  if (state.wake.commandTimer) {
    window.clearInterval(state.wake.commandTimer);
    state.wake.commandTimer = 0;
  }
  state.wake.command = false;
}

async function probeWakeCommand() {
  if (!state.wake.command || !state.mic.active) return;
  const now = Date.now();
  const rms = micRms(0.22);
  if (rms > 0.012) {
    state.wake.heardVoice = true;
    state.wake.lastVoiceAt = now;
  }
  const elapsed = now - state.wake.commandStarted;
  if (!state.wake.heardVoice && elapsed > 4200) {
    clearWakeCommand();
    state.wake.requireWake = false;
    state.mic.background = false;
    state.mic.active = false;
    cleanupMic();
    setMicStatus("Не услышала команду. Снова слушаю «Киви»…", "ok");
    scheduleWakeResume(500);
    return;
  }
  const silence = now - state.wake.lastVoiceAt;
  if (state.wake.heardVoice && silence >= 2000) {
    clearWakeCommand();
    state.mic.background = false;
    state.mic.active = false;
    stopMicMeter();
    setMicStatus("Распознаю команду…", "ok");
    void finishMicRecording();
    return;
  }
  if (elapsed > 12000) {
    clearWakeCommand();
    state.mic.background = false;
    state.mic.active = false;
    stopMicMeter();
    setMicStatus("Слишком долгая команда. Распознаю…", "ok");
    void finishMicRecording();
  }
}

function toggleWakeMode() {
  if (state.wake.enabled || state.wake.auto) {
    stopWakeMode();
    return;
  }
  state.wake.auto = true;
  void startWakeMode();
}

function encodeWav(samples, sampleRate) {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const write = (offset, text) => {
    for (let i = 0; i < text.length; i += 1) view.setUint8(offset + i, text.charCodeAt(i));
  };
  write(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  write(8, "WAVE");
  write(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  write(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i += 1) {
    const value = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(44 + i * 2, value < 0 ? value * 0x8000 : value * 0x7fff, true);
  }
  return buffer;
}

async function blobToWav(blob, targetRate = 16000) {
  const Ctor = window.AudioContext || window.webkitAudioContext;
  if (!Ctor) throw new Error("WebAudio недоступен");
  const ctx = new Ctor();
  try {
    const decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
    const channels = decoded.numberOfChannels;
    const length = Math.max(1, Math.ceil(decoded.duration * targetRate));
    const output = new Float32Array(length);
    const ratio = decoded.sampleRate / targetRate;
    for (let i = 0; i < length; i += 1) {
      const sourceIndex = Math.min(decoded.length - 1, Math.floor(i * ratio));
      let value = 0;
      for (let channel = 0; channel < channels; channel += 1) {
        value += decoded.getChannelData(channel)[sourceIndex] || 0;
      }
      output[i] = value / channels;
    }
    return encodeWav(output, targetRate);
  } finally {
    try {
      void ctx.close().catch(() => {});
    } catch {
      /* ignore */
    }
  }
}

async function finishMicRecording() {
  if (state.mic.finishing) return;
  state.mic.finishing = true;
  const chunks = state.mic.pcmChunks.slice();
  state.mic.pcmChunks = [];
  const sampleCount = chunks.reduce((total, chunk) => total + chunk.length, 0);
  if (chunks.length === 0 || sampleCount < state.mic.sampleRate * 0.25) {
    state.mic.finishing = false;
    cleanupMic();
    setMicStatus("Слишком короткая запись. Удерживай кнопку и говори.", "error");
    return;
  }
  state.mic.sending = true;
  setMicStatus("Распознаю речь…", "ok");
  setAvatar("thinking");
  try {
    const samples = new Float32Array(sampleCount);
    let offset = 0;
    for (const chunk of chunks) {
      samples.set(chunk, offset);
      offset += chunk.length;
    }
    const wav = encodeWav(samples, state.mic.sampleRate);
    const response = await fetch(apiUrl("/transcribe"), {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    let text = String(data.text || "").trim();
    if (!text || looksLikeHallucination(text)) {
      state.wake.requireWake = false;
      setMicStatus("Речь не распознана. Попробуй ещё раз.", "error");
      setAvatar("idle");
      cleanupMic();
      scheduleWakeResume(800);
      return;
    }
    if (state.wake.requireWake) {
      if (!hasWakeWord(text)) {
        state.wake.requireWake = false;
        setMicStatus("Не услышала обращение «Киви». Продолжаю слушать.", "ok");
        cleanupMic();
        scheduleWakeResume(700);
        return;
      }
      text = stripWakeWord(text);
      if (!text) {
        state.wake.requireWake = false;
        setMicStatus("Услышала «Киви». Скажи команду.", "ok");
        cleanupMic();
        scheduleWakeResume(700);
        return;
      }
    }
    state.wake.requireWake = false;
    setMicStatus(`Распознано: ${text}`, "ok");
    cleanupMic();
    els.composerInput.value = text;
    renderComposer();
    submitComposer();
    scheduleWakeResume(1600);
  } catch (error) {
    const detail = String((error && error.message) || error || "unknown");
    console.error("Kivi UI: speech recognition failed", error);
    setMicStatus(`Не удалось распознать речь: ${detail}`, "error");
    showToast(`Голосовой ввод: ${detail}`, "error");
    cleanupMic();
    setAvatar("idle");
  } finally {
    state.mic.finishing = false;
    state.mic.sending = false;
  }
}

async function startMic({ background = false } = {}) {
  if (state.mic.active || state.mic.sending) return;
  if (state.mic.requesting) {
    state.mic.want = true;
    return;
  }
  if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== "function") {
    setMicStatus(
      "Микрофон недоступен: браузер не поддерживает getUserMedia. Нужен Chrome, Edge или Firefox.",
      "error",
    );
    showToast("Микрофон недоступен в этом браузере.", "error");
    return;
  }
  state.mic.want = true;
  state.mic.requesting = true;
  state.mic.background = background;
  stopAudio();
  setMicStatus(background ? "Слушаю «Киви»…" : "Запрашиваю доступ к микрофону…");
  try {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      video: false,
    });
    if (!state.mic.want) {
      stream.getTracks().forEach((track) => track.stop());
      setMicStatus("Запись отменена. Микрофон выключен.");
      return;
    }
    state.mic.stream = stream;
    state.mic.active = true;
    state.mic.startTs = Date.now();
    state.mic.chunks = [];
    state.mic.pcmChunks = [];
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) throw new Error("WebAudio недоступен");
    const ctx = new Ctor();
    const source = ctx.createMediaStreamSource(stream);
    const processor = ctx.createScriptProcessor(4096, 1, 1);
    const sink = ctx.createGain();
    sink.gain.value = 0;
    processor.onaudioprocess = (event) => {
      if (!state.mic.active) return;
      const data = event.inputBuffer.getChannelData(0);
      state.mic.pcmChunks.push(new Float32Array(data));
    };
    source.connect(processor);
    processor.connect(sink);
    sink.connect(ctx.destination);
    if (ctx.state === "suspended") await ctx.resume();
    state.mic.audioCtx = ctx;
    state.mic.processor = processor;
    state.mic.sampleRate = ctx.sampleRate;
    if (background) setMicStatus("Слушаю «Киви»…");
    if (els.micBtn) {
      els.micBtn.classList.add("is-active");
      els.micBtn.setAttribute("aria-pressed", "true");
    }
    if (els.avatarPanel) els.avatarPanel.classList.add("is-mic");
    setAvatar("listening");
    setMicStatus(
      background
        ? "Слушаю «Киви» в фоне…"
        : "Слушаю… Отпусти кнопку, чтобы отправить речь.",
      "ok",
    );
    startMicMeter(stream);
  } catch (error) {
    state.mic.active = false;
    state.mic.want = false;
    stopMicMeter();
    if (els.micBtn) {
      els.micBtn.classList.remove("is-active");
      els.micBtn.setAttribute("aria-pressed", "false");
    }
    setAvatar("idle");
    const message = describeMicError(error);
    setMicStatus(message, "error");
    showToast(message, "error");
  } finally {
    state.mic.requesting = false;
  }
}

/* ------------------------------------------------------------------ */
/* Voices                                                             */
/* ------------------------------------------------------------------ */

function normalizeVoice(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const idRaw = raw.id ?? raw.voice_id ?? raw.voiceId ?? raw.slug ?? raw.name;
  const id = typeof idRaw === "string" ? idRaw.trim() : typeof idRaw === "number" ? String(idRaw) : "";
  if (!id) return null;
  const name = firstString(raw.name, raw.title, raw.label, raw.speaker) || id;
  const engine = firstString(raw.engine, raw.provider, raw.backend, raw.model) || "—";
  return {
    id: id.slice(0, 120),
    name: name.slice(0, 120),
    engine: engine.slice(0, 120),
    active: raw.active === true || raw.is_active === true || raw.selected === true,
    available: raw.available !== false,
  };
}

function engineLabel(engine) {
  const key = String(engine || "").toLowerCase();
  const labels = {
    fish: "Fish Audio",
    qwen: "Qwen3-TTS",
    qwen3: "Qwen3-TTS",
    silero: "Silero",
    piper: "Piper",
    edge: "Edge TTS",
  };
  return labels[key] || engine || "—";
}

function voiceIsSelected(voice) {
  if (state.prefs.selectedVoiceId) return state.prefs.selectedVoiceId === voice.id;
  return voice.active === true;
}

async function loadVoices({ force = false } = {}) {
  if (state.voicesLoading) return;
  state.voicesLoading = true;
  state.voicesState = "loading";
  state.voicesError = "";
  renderVoices();
  try {
    const data = await apiRequest("/voices", { timeout: 10000 });
    const raw = data && Array.isArray(data.voices) ? data.voices : [];
    state.voices = raw.map(normalizeVoice).filter(Boolean).slice(0, 200);
    state.voicesState = state.voices.length ? "ready" : "empty";
    hideConnectionNotice();
    const serverActive = state.voices.find((voice) => voice.active);
    if (serverActive) {
      // The server is authoritative; this also repairs a stale localStorage id
      // left over from a removed or renamed voice.
      state.prefs.selectedVoiceId = serverActive.id;
      savePrefs();
    } else if (state.prefs.selectedVoiceId) {
      const selectedKnown = state.voices.some(
        (voice) => voice.id === state.prefs.selectedVoiceId,
      );
      if (!selectedKnown) {
        state.prefs.selectedVoiceId = null;
        savePrefs();
      }
    }
  } catch (error) {
    state.voices = [];
    if (isUnsupported(error)) {
      state.voicesState = "missing";
      state.voicesError = "GET /voices не подключён на сервере.";
    } else {
      state.voicesState = "error";
      state.voicesError = describeApiError(error, "загрузке голосов");
      if (force) showToast(`Не удалось загрузить голоса: ${state.voicesError}`, "error");
    }
  } finally {
    state.voicesLoading = false;
    renderVoices();
    updateViewHeader();
  }
}

function showVoicesNote(text) {
  if (!els.voicesNote) return;
  els.voicesNote.hidden = false;
  els.voicesNote.textContent = text;
}

function hideVoicesNote() {
  if (!els.voicesNote) return;
  els.voicesNote.hidden = true;
  els.voicesNote.textContent = "";
}

function updateVoicesBadge() {
  if (!els.voicesCount) return;
  els.voicesCount.hidden = state.voices.length === 0;
  els.voicesCount.textContent = String(state.voices.length);
}

function renderVoices() {
  if (!els.voicesList || !els.voicesStatus) return;
  els.voicesStatus.replaceChildren();
  els.voicesList.replaceChildren();
  els.voicesStatus.classList.toggle("is-error", state.voicesState === "error");

  if (state.voicesState === "loading") {
    els.voicesStatus.append(el("span", { text: "Загружаю голоса…" }));
    els.voicesList.append(el("li", { class: "skeleton" }), el("li", { class: "skeleton" }));
  } else if (state.voicesState === "error") {
    els.voicesStatus.append(
      el("span", { text: state.voicesError || "Не удалось загрузить голоса." }),
    );
    els.voicesStatus.append(
      el("button", {
        type: "button",
        class: "btn btn-ghost btn-small",
        text: "Повторить",
        on: { click: () => void loadVoices({ force: true }) },
      }),
    );
  } else if (state.voicesState === "missing") {
    els.voicesStatus.append(
      el("span", {
        text: "Эндпоинт /voices пока не подключён на сервере — голоса недоступны.",
      }),
    );
  } else if (state.voicesState === "empty") {
    els.voicesStatus.append(el("span", { text: "Сервер вернул пустой список голосов." }));
  } else if (state.voicesState === "ready") {
    els.voicesStatus.append(
      el("span", {
        text: `${state.voices.length} ${plural(state.voices.length, [
          "голос",
          "голоса",
          "голосов",
        ])} · нажмите, чтобы выбрать`,
      }),
    );
    for (const voice of state.voices) els.voicesList.append(renderVoiceItem(voice));
  } else {
    els.voicesStatus.append(el("span", { text: "Голоса загружаются при открытии раздела." }));
  }
  updateVoicesBadge();
}

function renderVoiceItem(voice) {
  const selected = voiceIsSelected(voice);
  const unavailable = voice.available === false;
  const button = el("button", {
    type: "button",
    class: `voice-card${selected ? " is-selected" : ""}${unavailable ? " is-unavailable" : ""}`,
    "aria-pressed": selected ? "true" : "false",
    "aria-disabled": unavailable ? "true" : "false",
    "aria-label": `${voice.name}. Движок: ${engineLabel(voice.engine)}.${
      voice.active ? " Активен на сервере." : ""
    }${unavailable ? " Недоступен без ключа или модели." : ""}`,
    on: {
      click: () => {
        if (unavailable) {
          showToast(
            voice.engine === "fish"
              ? "Fish Astrea требует FISH_AUDIO_API_KEY. Ключ задаётся в окружении и после этого Kivi перезапускается."
              : "Голос недоступен: не установлена модель.",
            "error",
          );
          return;
        }
        void selectVoice(voice);
      },
    },
  });
  button.append(el("span", { class: "voice-orb", "aria-hidden": "true" }));
  const main = el("span", { class: "voice-main" });
  main.append(el("span", { class: "voice-name", text: voice.name }));
  main.append(el("span", { class: "voice-engine", text: engineLabel(voice.engine) }));
  button.append(main);
  const badges = el("span", { class: "voice-badges" });
  if (voice.active) badges.append(el("span", { class: "badge badge-active", text: "активен" }));
  if (unavailable) badges.append(el("span", { class: "badge", text: voice.engine === "fish" ? "нужен ключ" : "нет модели" }));
  if (selected && !voice.active) {
    badges.append(el("span", { class: "badge badge-selected", text: "выбран" }));
  }
  if (badges.childNodes.length) button.append(badges);
  return el("li", { class: "voice-item" }, button);
}

async function selectVoice(voice) {
  const previous = state.prefs.selectedVoiceId;
  state.prefs.selectedVoiceId = voice.id;
  savePrefs();
  hideVoicesNote();
  renderVoices();
  try {
    await apiRequest("/voices/active", {
      method: "POST",
      body: { id: voice.id },
      timeout: 10000,
    });
    state.voices = state.voices.map((item) => ({
      ...item,
      active: item.id === voice.id,
    }));
    showToast(`Голос «${voice.name}» выбран.`, "ok");
  } catch (error) {
    const serverActive = state.voices.find((item) => item.active);
    state.prefs.selectedVoiceId = serverActive ? serverActive.id : previous;
    savePrefs();
    if (isUnsupported(error)) {
      const reason =
        error.status === 503
          ? "Переключение голоса не настроено на сервере"
          : "Сервер не поддерживает POST /voices/active";
      showVoicesNote(`${reason}. Выбор отменён.`);
    } else {
      showVoicesNote(
        `Не удалось переключить голос на сервере: ${describeApiError(
          error,
          "выборе голоса",
        )} Выбор отменён.`,
      );
    }
  } finally {
    renderVoices();
  }
}

/* ------------------------------------------------------------------ */
/* Files (GET /fs/list, GET /fs/read)                                 */
/* ------------------------------------------------------------------ */

function normalizeFsPath(raw) {
  const text = typeof raw === "string" ? raw.trim().replace(/\\/g, "/") : ".";
  if (!text) return ".";
  if (text.toLowerCase() === "shell://") return "shell://";
  const parts = [];
  for (const segment of text.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      parts.pop();
      continue;
    }
    parts.push(segment);
  }
  return parts.length ? parts.join("/") : ".";
}

function fsJoin(dir, name) {
  const base = normalizeFsPath(dir);
  return base === "." ? name : `${base}/${name}`;
}

function fsParent(path) {
  const normalized = normalizeFsPath(path);
  if (normalized === ".") return null;
  const index = normalized.lastIndexOf("/");
  return index === -1 ? "." : normalized.slice(0, index);
}

function fsName(path) {
  const normalized = normalizeFsPath(path);
  if (normalized === ".") return ".";
  const index = normalized.lastIndexOf("/");
  return index === -1 ? normalized : normalized.slice(index + 1);
}

// Image previews go through GET /fs/raw only; everything else keeps /fs/read.
// The suffix list is the single switch that enables the raw request.
const FS_IMAGE_MIME = Object.freeze({
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  webp: "image/webp",
  gif: "image/gif",
  bmp: "image/bmp",
});
const FS_IMAGE_MIME_LABELS = Object.freeze({
  "image/png": "PNG",
  "image/jpeg": "JPEG",
  "image/webp": "WebP",
  "image/gif": "GIF",
  "image/bmp": "BMP",
});
// The server caps raw previews at 8 MiB; mirror that cap here so an oversized
// or malformed response never reaches the DOM.
const FS_IMAGE_MAX_BYTES = 8 * 1024 * 1024;
const FS_IMAGE_DATA_URL_LIMIT = Math.ceil((FS_IMAGE_MAX_BYTES * 4) / 3) + 64;

function fsImageSuffix(path) {
  const name = fsName(path).toLowerCase();
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1) : "";
}

function isFsImagePath(path) {
  return Object.prototype.hasOwnProperty.call(FS_IMAGE_MIME, fsImageSuffix(path));
}

function fsImageMimeLabel(mime) {
  return FS_IMAGE_MIME_LABELS[mime] || "";
}

function parseFsImageDataUrl(raw) {
  if (typeof raw !== "string") return null;
  const text = raw.trim();
  const match = /^data:(image\/[a-z0-9.+-]+);base64,([A-Za-z0-9+/]+={0,2})$/.exec(text);
  if (!match) return null;
  const mime = match[1].toLowerCase();
  if (!Object.prototype.hasOwnProperty.call(FS_IMAGE_MIME_LABELS, mime)) return null;
  const base64 = match[2];
  const padding = base64.endsWith("==") ? 2 : base64.endsWith("=") ? 1 : 0;
  return {
    src: text,
    mime,
    // Approximate the decoded size; only the status line needs it.
    bytes: Math.max(0, Math.floor((base64.length * 3) / 4) - padding),
  };
}

function describeFilesImageError(error) {
  if (error instanceof ApiError) {
    const detail = error.code ? `: ${error.code}` : "";
    if (error.status === 400) {
      return (
        "Не удалось показать изображение (400" +
        detail +
        "): сервер отклонил путь, формат или размер (лимит 8 МБ)."
      );
    }
    if (error.status === 503) {
      return (
        "Предпросмотр изображений временно недоступен: " +
        "файловые операции не настроены на сервере (503)."
      );
    }
  }
  return describeFilesError(error, "чтении изображения");
}

function fsCrumbs(path) {
  const normalized = normalizeFsPath(path);
  const crumbs = [{ label: ".", path: "." }];
  if (normalized === ".") return crumbs;
  let current = "";
  for (const segment of normalized.split("/")) {
    current = current ? `${current}/${segment}` : segment;
    crumbs.push({ label: segment, path: current });
  }
  return crumbs;
}

// Directory history is capped so a long session stays bounded; the oldest
// entries fall off and the cursor always points at the last entry.
const FS_HISTORY_LIMIT = 100;
// The tab strip is capped so the panel and localStorage stay bounded. The
// limit disables "+" instead of silently dropping an open tab.
const FS_TABS_LIMIT = 8;
const FS_PATH_LIMIT = 512;
// Colons are checked separately as a drive prefix, so they are excluded here
// together with the other characters Windows rejects in a path segment.
const FS_PATH_FORBIDDEN = /[:*?"<>|\u0000-\u001f]/;

// The address-bar parser is stricter than normalizeFsPath: the latter silently
// resolves "..", while typed input must be rejected instead of escaping.
function validateFilesPathInput(raw) {
  const text = typeof raw === "string" ? raw.trim() : "";
  if (!text) {
    return { ok: false, path: "", error: "Введите путь к папке внутри рабочей папки." };
  }
  if (text.length > FS_PATH_LIMIT) {
    return { ok: false, path: "", error: `Путь длиннее ${FS_PATH_LIMIT} символов.` };
  }
  if (text.toLowerCase() === "shell://") {
    return { ok: true, path: "shell://" };
  }
  if (/^[\\/]/.test(text)) {
    return {
      ok: false,
      path: "",
      error:
        "Абсолютный путь запрещён: путь должен быть внутри рабочей папки, " +
        "например docs/reports.",
    };
  }
  if (/^[A-Za-z]:/.test(text)) {
    return { ok: true, path: normalizeFsPath(text) };
  }
  const normalized = text.replace(/\\/g, "/");
  for (const segment of normalized.split("/")) {
    if (!segment || segment === ".") continue;
    if (segment === "..") {
      return {
        ok: false,
        path: "",
        error: "Переход вверх через «..» запрещён: откройте папку внутри рабочей папки.",
      };
    }
    if (FS_PATH_FORBIDDEN.test(segment)) {
      return {
        ok: false,
        path: "",
        error: 'В пути недопустимы символы : * ? " < > | и служебные символы.',
      };
    }
  }
  return { ok: true, path: normalizeFsPath(normalized), error: "" };
}

function recordFilesHistory(path) {
  const fs = state.files;
  const next = normalizeFsPath(path);
  // Same folder twice in a row is a reload, not a navigation: keep the stack.
  if (fs.historyIndex >= 0 && fs.history[fs.historyIndex] === next) return;
  fs.history = fs.history.slice(0, fs.historyIndex + 1);
  fs.history.push(next);
  if (fs.history.length > FS_HISTORY_LIMIT) {
    fs.history = fs.history.slice(fs.history.length - FS_HISTORY_LIMIT);
  }
  fs.historyIndex = fs.history.length - 1;
}

// /fs/list may normalize the requested path (for example "." for the root).
// Keep the history entry in sync with what was actually opened.
function updateFilesHistoryPath(requested, resolved) {
  const fs = state.files;
  const index = fs.historyIndex;
  if (index < 0 || index >= fs.history.length) return;
  if (fs.history[index] !== requested) return;
  fs.history[index] = resolved;
}

function stepFilesHistory(delta) {
  const fs = state.files;
  const nextIndex = fs.historyIndex + delta;
  if (nextIndex < 0 || nextIndex >= fs.history.length) return false;
  const path = fs.history[nextIndex];
  // Move the cursor first: the toolbar disables states before the request ends.
  fs.historyIndex = nextIndex;
  fs.pathDirty = false;
  fs.pathError = "";
  clearFilesSearch({ render: false });
  void loadFiles(path, { focus: true, recordHistory: false });
  return true;
}

/* ------------------------------------------------------------------ */
/* Files tabs                                                         */
/* ------------------------------------------------------------------ */

// A tab keeps its path and its own history stack; ids stay session-only so a
// restore never reuses a stale DOM id.
function makeFilesTab(rawPath, rawHistory = null, rawHistoryIndex = null) {
  const path = normalizeFsPath(rawPath);
  const history = [];
  if (Array.isArray(rawHistory)) {
    for (const item of rawHistory) {
      if (typeof item !== "string") continue;
      const next = normalizeFsPath(item);
      if (!history.length || history[history.length - 1] !== next) history.push(next);
    }
  }
  if (!history.length) history.push(path);
  let index = Number.isInteger(rawHistoryIndex) ? rawHistoryIndex : history.length - 1;
  if (index < 0 || index >= history.length) index = history.length - 1;
  if (history[index] !== path) {
    const existing = history.indexOf(path);
    if (existing >= 0) {
      index = existing;
    } else {
      history.push(path);
      index = history.length - 1;
    }
  }
  if (history.length > FS_HISTORY_LIMIT) {
    const overflow = history.length - FS_HISTORY_LIMIT;
    history.splice(0, overflow);
    index = Math.max(0, index - overflow);
  }
  state.files.tabSeq += 1;
  return {
    id: `ftab${state.files.tabSeq}`,
    path,
    history,
    historyIndex: index,
  };
}

function filesTabLabel(path) {
  const normalized = normalizeFsPath(path);
  return normalized === "." ? "Рабочая папка" : fsName(normalized);
}

function filesTabFullLabel(path) {
  const normalized = normalizeFsPath(path);
  return normalized === "." ? "Рабочая папка (корень)" : normalized;
}

// The active tab's live path/history lives on state.files; copy it back into
// the tab before switching away or persisting preferences.
function syncActiveFilesTab({ persist = true } = {}) {
  const fs = state.files;
  const tab = fs.tabs.find((item) => item.id === fs.activeTabId);
  if (!tab) return;
  tab.path = normalizeFsPath(fs.path);
  tab.history = fs.history.slice();
  tab.historyIndex = fs.historyIndex;
  if (!tab.history.length) {
    tab.history = [tab.path];
    tab.historyIndex = 0;
  }
  if (persist) savePrefs();
}

// Replace the live navigation state with a tab's snapshot. The tab path is the
// authority; history only needs a matching cursor for the back/forward buttons.
function applyFilesTabState(tab) {
  const fs = state.files;
  const history = tab.history.slice();
  let index = tab.historyIndex;
  if (!history.length) {
    history.push(tab.path);
    index = 0;
  } else if (!Number.isInteger(index) || index < 0 || index >= history.length) {
    index = history.length - 1;
  }
  if (history[index] !== tab.path) {
    const existing = history.indexOf(tab.path);
    if (existing >= 0) {
      index = existing;
    } else {
      history.splice(index + 1);
      history.push(tab.path);
      index = history.length - 1;
    }
  }
  fs.history = history;
  fs.historyIndex = index;
  fs.path = tab.path;
}

function revealFilesTabButton(tabId) {
  const list = els.filesTablist;
  if (!list) return;
  const button = list.querySelector(`[data-files-tab-id="${tabId}"]`);
  if (!button) return;
  // Horizontal reveal only: the page must not jump vertically when a tab is
  // activated while the file list below is scrolled.
  const item = button.closest(".files-tab") || button;
  const listRect = list.getBoundingClientRect();
  const itemRect = item.getBoundingClientRect();
  const pad = 6;
  if (itemRect.left < listRect.left + pad) {
    list.scrollLeft -= listRect.left + pad - itemRect.left;
  } else if (itemRect.right > listRect.right - pad) {
    list.scrollLeft += itemRect.right - (listRect.right - pad);
  }
}

function focusFilesTabButton(tabId) {
  if (!els.filesTablist) return;
  const button = els.filesTablist.querySelector(`[data-files-tab-id="${tabId}"]`);
  if (!button) return;
  try {
    button.focus({ preventScroll: true });
  } catch {
    button.focus();
  }
  revealFilesTabButton(tabId);
}

function activateFilesTab(tabId, { focus = false } = {}) {
  const fs = state.files;
  const target = fs.tabs.find((tab) => tab.id === tabId);
  if (!target) return false;
  if (fs.busy) {
    showToast("Дождитесь завершения текущей операции с файлами.", "info");
    return false;
  }
  if (target.id === fs.activeTabId) {
    if (focus) focusFilesTabButton(target.id);
    return false;
  }
  const tabFocused = Boolean(
    els.filesTablist && els.filesTablist.contains(document.activeElement),
  );
  // Snapshot the tab being left before replacing the live navigation state.
  syncActiveFilesTab({ persist: false });
  fs.activeTabId = target.id;
  // Abort in-flight list, preview and search responses of the previous tab.
  fs.epoch += 1;
  fs.previewEpoch += 1;
  fs.searchEpoch += 1;
  // Switching tabs closes preview/search and every write form so no dialog
  // keeps editing a file from another folder.
  clearFilesSearch({ render: false });
  if (fs.preview) closeFilesPreview({ returnFocus: false });
  fs.preview = null;
  fs.previewTrigger = null;
  fs.copyStatus = "";
  fs.pathDirty = false;
  fs.pathError = "";
  resetFilesWriteForms();
  applyFilesTabState(target);
  fs.entries = [];
  fs.truncated = false;
  fs.state = "idle";
  fs.error = "";
  syncActiveFilesTab();
  renderFiles();
  updateViewHeader();
  if (focus || tabFocused) focusFilesTabButton(target.id);
  // force re-fetches even when the tab path equals the live path, so the list
  // and breadcrumbs of the restored tab are always rebuilt; recordHistory:false
  // keeps that tab's stored stack intact.
  void loadFiles(fs.path, { force: true, recordHistory: false });
  return true;
}

function openFilesTab(rawPath = null, { focus = true } = {}) {
  const fs = state.files;
  if (fs.busy) {
    showToast("Дождитесь завершения текущей операции с файлами.", "info");
    return false;
  }
  if (fs.tabs.length >= FS_TABS_LIMIT) {
    showToast(`Открыто максимум ${FS_TABS_LIMIT} вкладок. Закройте одну из них.`, "info");
    return false;
  }
  // "+" starts at the workspace root instead of cloning the current folder.
  const path = normalizeFsPath(
    typeof rawPath === "string" && rawPath.trim() ? rawPath : ".",
  );
  const tab = makeFilesTab(path);
  fs.tabs.push(tab);
  return activateFilesTab(tab.id, { focus });
}

function closeFilesTab(tabId, { focus = true } = {}) {
  const fs = state.files;
  if (fs.busy) {
    showToast("Дождитесь завершения текущей операции с файлами.", "info");
    return false;
  }
  if (fs.tabs.length <= 1) {
    showToast("Последняя вкладка остаётся открытой.", "info");
    return false;
  }
  const index = fs.tabs.findIndex((tab) => tab.id === tabId);
  if (index === -1) return false;
  const wasActive = fs.tabs[index].id === fs.activeTabId;
  fs.tabs.splice(index, 1);
  if (!wasActive) {
    // Closing a background tab never disturbs the active folder.
    renderFiles();
    savePrefs();
    if (focus) focusFilesTabButton(fs.activeTabId);
    return true;
  }
  // Activate the right neighbour, or the new last tab when closing the end.
  const neighbor = fs.tabs[Math.min(index, fs.tabs.length - 1)];
  return activateFilesTab(neighbor.id, { focus });
}

function stepFilesTab(delta) {
  const fs = state.files;
  if (fs.tabs.length < 2) return false;
  const index = fs.tabs.findIndex((tab) => tab.id === fs.activeTabId);
  if (index === -1) return false;
  const direction = delta < 0 ? -1 : 1;
  const next = (index + direction + fs.tabs.length) % fs.tabs.length;
  return activateFilesTab(fs.tabs[next].id, { focus: true });
}

// Roving-tabindex keyboard model inside the tablist (ARIA tabs pattern).
function onFilesTablistKeydown(event) {
  if (!els.filesTablist) return;
  const target = event.target instanceof Element ? event.target.closest("[role='tab']") : null;
  if (!target) return;
  const tabId = target.getAttribute("data-files-tab-id") || "";
  const index = state.files.tabs.findIndex((tab) => tab.id === tabId);
  if (index === -1) return;
  const count = state.files.tabs.length;
  let next = -1;
  if (event.key === "ArrowRight") next = (index + 1) % count;
  else if (event.key === "ArrowLeft") next = (index + count - 1) % count;
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = count - 1;
  if (next === -1) return;
  event.preventDefault();
  if (next === index) return;
  activateFilesTab(state.files.tabs[next].id, { focus: true });
}

// Middle-click closes a tab, like a browser. Mousedown is cancelled too so the
// middle-click autoscroll cursor never starts.
function onFilesTablistAuxClick(event) {
  if (event.button !== 1) return;
  const item = event.target instanceof Element ? event.target.closest(".files-tab") : null;
  if (!item) return;
  const main = item.querySelector("[data-files-tab-id]");
  const tabId = main ? main.getAttribute("data-files-tab-id") : "";
  if (!tabId) return;
  event.preventDefault();
  closeFilesTab(tabId);
}

function onFilesTablistMouseDown(event) {
  if (event.button !== 1) return;
  if (event.target instanceof Element && event.target.closest(".files-tab")) {
    event.preventDefault();
  }
}

// A viewport change can turn a wide strip into a scrollable one; keep the
// active tab discoverable without touching the page's vertical scroll.
function onFilesTabsResize() {
  if (state.view !== "files") return;
  revealFilesTabButton(state.files.activeTabId);
}

function syncFilesPathInput() {
  if (!els.filesPathInput) return;
  const focused = document.activeElement === els.filesPathInput;
  // While the user edits the field, a render must not overwrite the draft.
  if (state.files.pathDirty && focused) return;
  // The field lost focus before submit (for example a preview click): drop the
  // abandoned draft together with its validation message.
  if (state.files.pathDirty) {
    state.files.pathDirty = false;
    state.files.pathError = "";
  }
  const value = state.files.path;
  if (els.filesPathInput.value !== value) els.filesPathInput.value = value;
}

function renderFilesPathError() {
  if (els.filesPathError) {
    els.filesPathError.textContent = state.files.pathError;
    els.filesPathError.hidden = !state.files.pathError;
  }
  if (!els.filesPathInput) return;
  if (state.files.pathError) els.filesPathInput.setAttribute("aria-invalid", "true");
  else els.filesPathInput.removeAttribute("aria-invalid");
}

function onSubmitFilesPath(event) {
  if (event) event.preventDefault();
  const result = validateFilesPathInput(
    els.filesPathInput ? els.filesPathInput.value : "",
  );
  if (!result.ok) {
    state.files.pathError = result.error;
    state.files.pathDirty = true;
    renderFilesPathError();
    if (els.filesPathInput) {
      try {
        els.filesPathInput.focus({ preventScroll: true });
      } catch {
        els.filesPathInput.focus();
      }
    }
    return;
  }
  state.files.pathDirty = false;
  state.files.pathError = "";
  clearFilesSearch({ render: false });
  if (result.path === state.files.path) {
    // Enter on the current folder behaves like Refresh without moving focus
    // through the address bar again.
    void loadFiles(result.path, { force: true, focus: true });
    return;
  }
  void loadFiles(result.path, { focus: true });
}

function onFilesPathInput() {
  state.files.pathDirty = true;
  if (!state.files.pathError) return;
  state.files.pathError = "";
  renderFilesPathError();
}

function onFilesPathKeydown(event) {
  if (event.key !== "Escape") return;
  event.preventDefault();
  event.stopPropagation();
  state.files.pathDirty = false;
  state.files.pathError = "";
  if (els.filesPathInput) els.filesPathInput.value = state.files.path;
  renderFilesPathError();
}

function formatFileSize(bytes) {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} Б`;
  const units = ["КБ", "МБ", "ГБ", "ТБ"];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const rounded = value >= 10 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${numberFmt.format(rounded)} ${units[unit]}`;
}

function normalizeFsEntries(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const entries = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const name = typeof item.name === "string" ? item.name.trim() : "";
    // Names are single path segments; reject anything that could escape the dir.
    if (!name || name === "." || name === ".." || /[\\/]/.test(name)) continue;
    if (seen.has(name)) continue;
    seen.add(name);
    const type = item.type === "dir" || item.type === "file" ? item.type : "other";
    const size = Number(item.size);
    entries.push({
      name,
      type,
      size: Number.isFinite(size) && size >= 0 ? size : null,
    });
  }
  const order = { dir: 0, file: 1, other: 2 };
  entries.sort((a, b) => {
    if (order[a.type] !== order[b.type]) return order[a.type] - order[b.type];
    return a.name.localeCompare(b.name, "ru", { numeric: true, sensitivity: "base" });
  });
  return entries;
}

function normalizeFsSearchMatches(raw) {
  if (!Array.isArray(raw)) return [];
  const seen = new Set();
  const matches = [];
  for (const item of raw) {
    if (!item || typeof item !== "object") continue;
    const rawPath = typeof item.path === "string" ? item.path.trim() : "";
    if (!rawPath) continue;
    // Search paths are workspace-relative; normalize to keep joins predictable.
    const path = normalizeFsPath(rawPath);
    if (path === "." || seen.has(path)) continue;
    seen.add(path);
    const type =
      item.type === "dir" || item.type === "file"
        ? item.type
        : item.type === "other"
          ? "other"
          : "file";
    const size = Number(item.size);
    matches.push({
      path,
      name: fsName(path),
      type,
      size: Number.isFinite(size) && size >= 0 ? size : null,
    });
    if (matches.length >= 200) break;
  }
  const order = { dir: 0, file: 1, other: 2 };
  matches.sort((a, b) => {
    if (order[a.type] !== order[b.type]) return order[a.type] - order[b.type];
    return a.path.localeCompare(b.path, "ru", { numeric: true, sensitivity: "base" });
  });
  return matches;
}

// /fs/list and /fs/search expose name/type/size only, with no mtime. The panel
// therefore offers name/size/type and never invents a "modified" sort value.
const FS_SORT_KEYS = new Set(["name", "size", "type"]);
// Folders stay grouped first in both directions, as they were before sorting.
const FS_TYPE_ORDER = Object.freeze({ dir: 0, file: 1, other: 2 });

function compareFsSortText(left, right) {
  const a = typeof left === "string" ? left : "";
  const b = typeof right === "string" ? right : "";
  return a.localeCompare(b, "ru", { numeric: true, sensitivity: "base" });
}

function fsSortTypeLabel(item) {
  if (!item || item.type === "dir") return "папка";
  const name =
    typeof item.name === "string" && item.name ? item.name : fsName(item.path || "");
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

function sortFsItems(items, key, direction) {
  if (!Array.isArray(items)) return [];
  if (items.length < 2) return items.slice();
  const sortKey = FS_SORT_KEYS.has(key) ? key : "name";
  const sign = direction === "desc" ? -1 : 1;
  return items.slice().sort((a, b) => {
    const groupDiff =
      (FS_TYPE_ORDER[a.type] ?? 2) - (FS_TYPE_ORDER[b.type] ?? 2);
    if (groupDiff !== 0) return groupDiff;
    let result = 0;
    if (sortKey === "size") {
      const aSize = Number.isFinite(a.size) ? a.size : -1;
      const bSize = Number.isFinite(b.size) ? b.size : -1;
      result = aSize - bSize;
    } else if (sortKey === "type") {
      result = compareFsSortText(fsSortTypeLabel(a), fsSortTypeLabel(b));
    }
    // Name is the key for "name" and the stable tie-breaker for the rest;
    // search results may share a basename, so their relative path breaks ties.
    if (result === 0) result = compareFsSortText(a.name, b.name);
    if (result === 0) result = compareFsSortText(a.path, b.path);
    return result * sign;
  });
}

function filesSearchActive() {
  return Boolean(state.files.searchQuery);
}

function describeFilesSearchError(error) {
  if (error instanceof ApiError) {
    const detail = error.code ? `: ${error.code}` : "";
    if (error.status === 400) {
      return `Сервер отклонил поисковый запрос (400${detail}). Введите более короткий запрос.`;
    }
    if (error.status === 503) {
      return "Поиск временно недоступен: файловые операции не настроены на сервере (503).";
    }
    if (error.status === 404 || error.status === 405) {
      return `Сервер не поддерживает поиск (${error.status}). Обновите Kivi.`;
    }
    if (error.status >= 500) {
      return `Сервер Kivi не смог выполнить поиск (${error.status}). Повторите попытку.`;
    }
  }
  return describeFilesError(error, "поиске файлов");
}

function describeFilesError(error, context) {
  if (error instanceof ApiError) {
    const detail = error.code ? `: ${error.code}` : "";
    if (error.status === 403) return `Доступ к этому пути запрещён (403${detail}).`;
    if (error.status === 404) return "Путь не найден (404).";
    if (error.status === 413) return `Файл слишком большой для предпросмотра (413${detail}).`;
    if (error.status === 400) return `Сервер отклонил путь (400${detail}).`;
  }
  return describeApiError(error, context);
}

const FS_NAME_LIMIT = 128;
const FS_NAME_FORBIDDEN = /[\\/:*?"<>|\u0000-\u001f]/;
const FS_RESERVED_NAME = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$/i;

function validateFsName(raw, label) {
  const name = typeof raw === "string" ? raw.trim() : "";
  if (!name) return { ok: false, name: "", error: `Введите ${label || "имя"}.` };
  if (name === "." || name === "..") {
    return { ok: false, name: "", error: "Имя не может быть «.» или «..»." };
  }
  if (name.length > FS_NAME_LIMIT) {
    return { ok: false, name: "", error: `Имя длиннее ${FS_NAME_LIMIT} символов.` };
  }
  if (FS_NAME_FORBIDDEN.test(name)) {
    return {
      ok: false,
      name: "",
      error: 'Имя не должно содержать \\ / : * ? " < > | и служебные символы.',
    };
  }
  if (/[. ]$/.test(name)) {
    return { ok: false, name: "", error: "Имя не должно заканчиваться точкой или пробелом." };
  }
  if (FS_RESERVED_NAME.test(name)) {
    return { ok: false, name: "", error: "Это имя зарезервировано Windows. Выберите другое." };
  }
  return { ok: true, name, error: "" };
}

function describeFsWriteError(error, context) {
  if (error instanceof ApiError) {
    const code = String(error.code || "").toLowerCase();
    if (error.status === 503) {
      return (
        "Файловые операции временно недоступны: " +
        "они не настроены на сервере (503)."
      );
    }
    if (error.status === 403) return "Сервер запретил изменять этот путь (403).";
    if (error.status === 404 || error.status === 405) {
      return `Сервер не поддерживает ${error.path || "эту операцию"} (${error.status}). Обновите Kivi.`;
    }
    if (error.status === 400) {
      if (code.includes("already exists")) {
        return "Элемент с таким именем уже существует. Файлы и папки не перезаписываются.";
      }
      if (code.includes("parent") && code.includes("does not exist")) {
        return "Текущая папка больше недоступна. Обновите список и попробуйте снова.";
      }
      if (code.includes("source") && code.includes("does not exist")) {
        return "Файл не найден: возможно, его уже переименовали. Обновите список.";
      }
      if (code.includes("confirm") && code.includes("match")) {
        return "Подтверждение не совпало с именем. Введите имя точно и повторите.";
      }
      if (code.includes("recycle bin") || code.includes("send2trash")) {
        return "Не удалось переместить элемент в Корзину Windows. Проверьте права доступа и повторите.";
      }
      if (code.includes("workspace root")) {
        return "Корневую папку нельзя переместить в корзину.";
      }
      if (code.includes("symlink")) {
        return "Символические ссылки не поддерживаются для файловых операций.";
      }
      return `Сервер отклонил операцию${error.code ? `: ${error.code}` : ""}.`;
    }
  }
  return describeFilesError(error, context);
}

const FS_DEST_LIMIT = 512;

function validateFsDestination(raw, source) {
  const text = typeof raw === "string" ? raw.trim() : "";
  if (!text) return { ok: false, path: "", error: "Введите путь назначения." };
  if (text.length > FS_DEST_LIMIT) {
    return { ok: false, path: "", error: `Путь длиннее ${FS_DEST_LIMIT} символов.` };
  }
  const normalizedText = text.replace(/\\/g, "/");
  if (normalizedText.startsWith("/") || /^[a-zA-Z]:/.test(normalizedText)) {
    return {
      ok: false,
      path: "",
      error: "Путь должен быть относительным: например, архив/файл.md.",
    };
  }
  const segments = [];
  for (const rawSegment of normalizedText.split("/")) {
    if (!rawSegment || rawSegment === ".") continue;
    if (rawSegment === "..") {
      return {
        ok: false,
        path: "",
        error: "Путь не должен выходить за пределы рабочей папки.",
      };
    }
    const check = validateFsName(rawSegment, "имя в пути");
    if (!check.ok) return { ok: false, path: "", error: check.error };
    segments.push(check.name);
  }
  if (!segments.length) {
    return { ok: false, path: "", error: "Укажите файл или папку назначения." };
  }
  const destination = segments.join("/");
  if (destination === normalizeFsPath(source)) {
    return {
      ok: false,
      path: "",
      error: "Путь совпадает с исходным. Укажите другой путь.",
    };
  }
  if (fsDestinationKnown(destination)) {
    return {
      ok: false,
      path: "",
      error:
        "Элемент с таким путём уже существует. Перезапись запрещена: " +
        "выберите другое имя или папку.",
    };
  }
  return { ok: true, path: destination, error: "" };
}

function fsDestinationKnown(destination) {
  // The server is the final authority; this only catches obvious local collisions.
  if (fsParent(destination) === normalizeFsPath(state.files.path)) {
    const name = fsName(destination);
    if (state.files.entries.some((entry) => entry.name === name)) return true;
  }
  return state.files.searchMatches.some((match) => match.path === destination);
}

function describeFilesTransferError(error, mode) {
  const action = mode === "move" ? "перемещение файла" : "копирование файла";
  if (error instanceof ApiError && error.status === 400) {
    const code = String(error.code || "").toLowerCase();
    if (code.includes("already exists")) {
      return (
        "Элемент с таким путём уже существует. Перезапись запрещена: " +
        "выберите другое имя или папку."
      );
    }
    if (code.includes("source") && code.includes("does not exist")) {
      return "Исходный файл не найден: возможно, его уже переместили. Обновите список.";
    }
    if (code.includes("parent") && code.includes("does not exist")) {
      return "Папка назначения не найдена. Проверьте путь и создайте папку заранее.";
    }
    if (code.includes("into itself")) {
      return "Нельзя скопировать или переместить папку внутрь самой себя.";
    }
    if (code.includes("symlink")) {
      return "Символические ссылки не поддерживаются для файловых операций.";
    }
    // The API reports a generic 400 for rejected transfers.
    return (
      `Сервер отклонил ${action}. Проверьте путь назначения: возможно, он занят ` +
      "или папки назначения нет. Перезапись запрещена."
    );
  }
  return describeFsWriteError(error, action);
}

function fileRow(path) {
  if (!els.filesList) return null;
  const rows = els.filesList.querySelectorAll("button.file-row");
  for (const row of rows) {
    if (row.dataset.fsPath === path) return row;
  }
  return null;
}

function fileIcon(kind) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const paths =
    kind === "dir"
      ? [
          "M3.5 7.5A2.5 2.5 0 0 1 6 5h4l2.2 2.5H18a2.5 2.5 0 0 1 2.5 2.5v7A2.5 2.5 0 0 1 18 19.5H6A2.5 2.5 0 0 1 3.5 17Z",
        ]
      : ["M7 3.5h6.6L18 8v12.5H7Z", "M13.5 3.5V8H18"];
  for (const d of paths) {
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

function filesTarget() {
  if (state.files.target) return state.files.target;
  const preview = state.files.preview;
  if (!preview) return null;
  return {
    path: preview.path,
    name: preview.name || fsName(preview.path),
    type: "file",
    loading: preview.loading === true,
  };
}

function setFilesTarget(target) {
  if (!target || !target.path) {
    state.files.target = null;
    return;
  }
  state.files.target = {
    path: target.path,
    name: target.name || fsName(target.path),
    type: target.type === "dir" ? "dir" : "file",
  };
}

function syncFilesTarget() {
  if (state.files.renameOpen || state.files.trashOpen || state.files.transferOpen) return;
  state.files.target = null;
}

function resetFilesWriteForms() {
  state.files.mkdirOpen = false;
  state.files.mkdirError = "";
  state.files.renameOpen = false;
  state.files.renameError = "";
  state.files.trashOpen = false;
  state.files.trashError = "";
  state.files.transferOpen = false;
  state.files.transferMode = "";
  state.files.transferError = "";
  state.files.target = null;
  if (els.filesMkdirInput) els.filesMkdirInput.value = "";
  if (els.filesRenameInput) els.filesRenameInput.value = "";
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
}

async function undoFilesOperation() {
  if (state.files.busy) return;
  state.files.busy = "undo";
  renderFiles();
  try {
    await apiRequest("/fs/undo", { method: "POST", body: {} });
    showToast("Последняя файловая операция отменена.", "ok");
    await loadFiles(state.files.path, { force: true, focus: true });
  } catch (error) {
    showToast(
      `Не удалось отменить операцию: ${describeApiError(error, "отмене")}`,
      "error",
    );
  } finally {
    state.files.busy = "";
    renderFiles();
  }
}

async function loadFiles(
  rawPath = state.files.path,
  { force = false, focus = false, recordHistory = true } = {},
) {
  const path = normalizeFsPath(rawPath);
  if (!force && path === state.files.path && state.files.state === "loading") return;
  const changed = path !== state.files.path;
  // Every directory load is a navigation except back/forward replays, which
  // moved the cursor themselves.
  if (recordHistory) recordFilesHistory(path);
  state.files.path = path;
  // Keep the active tab's label and stack in step before the request lands.
  syncActiveFilesTab();
  state.files.pathDirty = false;
  state.files.pathError = "";
  if (changed) {
    if (state.files.preview) closeFilesPreview({ returnFocus: false });
    // A directory change always leaves search mode: results belong to the
    // previous folder and would otherwise keep replacing the new list.
    if (filesSearchActive()) clearFilesSearch({ render: false });
    resetFilesWriteForms();
    state.files.entries = [];
    state.files.truncated = false;
  }
  state.files.state = "loading";
  state.files.error = "";
  const epoch = ++state.files.epoch;
  renderFiles();
  updateViewHeader();

  try {
    const data = await apiRequest(`/fs/list?path=${encodeURIComponent(path)}`, {
      timeout: 15000,
    });
    if (epoch !== state.files.epoch) return;
    const entries = normalizeFsEntries(data && data.entries);
    const resolved =
      data && typeof data.path === "string" ? normalizeFsPath(data.path) : path;
    updateFilesHistoryPath(path, resolved);
    state.files.path = resolved;
    state.files.entries = entries;
    state.files.truncated = Boolean(data && data.truncated === true);
    state.files.state = entries.length ? "ready" : "empty";
  } catch (error) {
    if (epoch !== state.files.epoch) return;
    state.files.entries = [];
    state.files.truncated = false;
    state.files.state = "error";
    state.files.error = describeFilesError(error, "получении списка файлов");
    if (force) showToast(`Не удалось открыть папку: ${state.files.error}`, "error");
  } finally {
    if (epoch !== state.files.epoch) return;
    // /fs/list may have resolved a different path (for example "." for root).
    syncActiveFilesTab();
    renderFiles();
    updateViewHeader();
    if (focus) focusFilesList();
  }
}

function focusFilesSearchInput() {
  if (!els.filesSearchInput) return;
  try {
    els.filesSearchInput.focus({ preventScroll: true });
  } catch {
    els.filesSearchInput.focus();
  }
}

function normalizeFilesSearchQuery(raw) {
  return typeof raw === "string" ? raw.trim().slice(0, 128) : "";
}

async function runFilesSearch(rawQuery) {
  const query = normalizeFilesSearchQuery(rawQuery);
  if (!query) {
    clearFilesSearch({ focusInput: true });
    return;
  }
  if (els.filesSearchInput && els.filesSearchInput.value !== query) {
    els.filesSearchInput.value = query;
  }
  resetFilesWriteForms();
  state.files.searchQuery = query;
  state.files.searchState = "loading";
  state.files.searchError = "";
  state.files.searchMatches = [];
  state.files.searchTruncated = false;
  const epoch = ++state.files.searchEpoch;
  renderFiles();
  updateViewHeader();

  try {
    const data = await apiRequest(`/fs/search?q=${encodeURIComponent(query)}`, {
      timeout: 20000,
    });
    if (epoch !== state.files.searchEpoch) return;
    const matches = normalizeFsSearchMatches(data && data.matches);
    state.files.searchMatches = matches;
    state.files.searchTruncated = Boolean(data && data.truncated === true);
    state.files.searchState = matches.length ? "ready" : "empty";
  } catch (error) {
    if (epoch !== state.files.searchEpoch) return;
    state.files.searchMatches = [];
    state.files.searchTruncated = false;
    state.files.searchState = "error";
    state.files.searchError = describeFilesSearchError(error);
  } finally {
    if (epoch !== state.files.searchEpoch) return;
    renderFiles();
    updateViewHeader();
  }
}

function clearFilesSearch({ resetInput = true, focusInput = false, render = true } = {}) {
  state.files.searchEpoch += 1;
  state.files.searchQuery = "";
  state.files.searchState = "idle";
  state.files.searchError = "";
  state.files.searchMatches = [];
  state.files.searchTruncated = false;
  if (resetInput && els.filesSearchInput) els.filesSearchInput.value = "";
  if (render) renderFiles();
  if (focusInput) focusFilesSearchInput();
}

function refreshFilesView() {
  if (filesSearchActive()) void runFilesSearch(state.files.searchQuery);
  else void loadFiles(state.files.path, { force: true });
}

function onSubmitFilesSearch(event) {
  if (event) event.preventDefault();
  const query = els.filesSearchInput ? els.filesSearchInput.value : "";
  if (!normalizeFilesSearchQuery(query)) {
    clearFilesSearch({ focusInput: true });
    return;
  }
  void runFilesSearch(query);
}

function onFilesSearchInput() {
  const hasText = Boolean(els.filesSearchInput && els.filesSearchInput.value.trim());
  if (els.filesSearchClear) {
    els.filesSearchClear.hidden = !hasText && !state.files.searchQuery;
  }
  // Erasing the query (including the native search clear control) restores the folder.
  if (state.files.searchQuery && !hasText) {
    clearFilesSearch();
    focusFilesSearchInput();
  }
}

async function loadFilesPreview(rawPath, trigger = null) {
  const path = normalizeFsPath(rawPath);
  const name = fsName(path);
  const image = isFsImagePath(path);
  const kind = image ? "image" : "text";
  if (!state.files.preview || state.files.preview.path !== path) resetFilesWriteForms();
  const epoch = ++state.files.previewEpoch;
  state.files.previewTrigger = trigger;
  state.files.copyStatus = "";
  state.files.preview = {
    kind,
    path,
    name,
    content: null,
    src: "",
    mime: "",
    bytes: 0,
    truncated: false,
    loading: true,
    error: "",
  };
  renderFiles();

  try {
    // Non-images never hit /fs/raw, and images never hit /fs/read: one request
    // per preview keeps large binary scans out of the text path.
    const endpoint = image ? "/fs/raw" : "/fs/read";
    const data = await apiRequest(`${endpoint}?path=${encodeURIComponent(path)}`, {
      timeout: 20000,
    });
    if (epoch !== state.files.previewEpoch) return;
    const resolvedPath =
      data && typeof data.path === "string" ? normalizeFsPath(data.path) : path;
    if (image) {
      const raw = data && typeof data.image === "string" ? data.image : "";
      if (raw.length > FS_IMAGE_DATA_URL_LIMIT) {
        throw new ApiError("Изображение слишком большое для предпросмотра (лимит 8 МБ).", {
          status: 413,
          code: "image-too-large",
          path,
        });
      }
      const parsed = parseFsImageDataUrl(raw);
      if (!parsed) {
        throw new ApiError("Сервер вернул некорректные данные изображения.", {
          code: "bad-image",
          path,
        });
      }
      state.files.preview = {
        kind: "image",
        path: resolvedPath,
        name: fsName(resolvedPath),
        content: null,
        src: parsed.src,
        mime: parsed.mime,
        bytes: parsed.bytes,
        truncated: false,
        loading: false,
        error: "",
      };
    } else {
      const content = data && typeof data.content === "string" ? data.content : "";
      state.files.preview = {
        kind: "text",
        path: resolvedPath,
        name: fsName(resolvedPath),
        content,
        src: "",
        mime: "",
        bytes: 0,
        truncated: Boolean(data && data.truncated === true),
        loading: false,
        error: "",
      };
    }
  } catch (error) {
    if (epoch !== state.files.previewEpoch) return;
    state.files.preview = {
      kind,
      path,
      name,
      content: null,
      src: "",
      mime: "",
      bytes: 0,
      truncated: false,
      loading: false,
      error: image
        ? describeFilesImageError(error)
        : describeFilesError(error, "чтении файла"),
    };
  } finally {
    if (epoch !== state.files.previewEpoch) return;
    renderFiles();
    focusFilesPreview();
    // On narrow screens the preview sits below the list; reveal it.
    if (
      window.matchMedia("(max-width: 899px)").matches &&
      els.filesPreviewCard &&
      !els.filesPreviewCard.hidden
    ) {
      try {
        els.filesPreviewCard.scrollIntoView({ block: "start" });
      } catch {
        /* scrolling is optional */
      }
    }
  }
}

function closeFilesPreview({ returnFocus = true } = {}) {
  const trigger = state.files.previewTrigger;
  const path = state.files.preview ? state.files.preview.path : "";
  if (!state.files.preview && !trigger) return;
  state.files.previewEpoch += 1;
  state.files.preview = null;
  state.files.previewTrigger = null;
  state.files.copyStatus = "";
  state.files.renameOpen = false;
  state.files.renameError = "";
  state.files.trashOpen = false;
  state.files.trashError = "";
  state.files.transferOpen = false;
  state.files.transferMode = "";
  state.files.transferError = "";
  state.files.target = null;
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
  renderFilesList();
  renderFilesPreview();
  if (!returnFocus) return;
  // The trigger row is often replaced by a re-render; re-find it by path.
  const target = trigger && trigger.isConnected ? trigger : path ? fileRow(path) : null;
  if (target) {
    try {
      target.focus();
    } catch {
      /* focus is best effort */
    }
  }
}

function focusFilesList() {
  const first =
    els.filesList && els.filesList.querySelector("button.file-row:not(.is-static)");
  const target = first || els.filesListTitle;
  if (!target) return;
  try {
    target.focus({ preventScroll: true });
  } catch {
    target.focus();
  }
}

function focusFilesPreview() {
  if (!els.filesPreviewTitle) return;
  try {
    els.filesPreviewTitle.focus({ preventScroll: true });
  } catch {
    els.filesPreviewTitle.focus();
  }
}

function renderFiles() {
  renderFilesChrome();
  renderFilesList();
}

// The tab strip is rebuilt from state; focus inside it is restored to the
// active tab so a rebuild never drops the keyboard user on <body>.
let filesRenderedActiveTabId = "";

function renderFilesTabs() {
  const fs = state.files;
  if (!els.filesTablist) return;
  const focused = document.activeElement;
  const tabFocused = Boolean(focused && els.filesTablist.contains(focused));
  els.filesTablist.replaceChildren();
  const canClose = fs.tabs.length > 1;
  for (const tab of fs.tabs) {
    const active = tab.id === fs.activeTabId;
    const fullLabel = filesTabFullLabel(tab.path);
    const item = el("div", {
      class: `files-tab${active ? " is-active" : ""}`,
      role: "presentation",
    });
    const main = el("button", {
      type: "button",
      class: "files-tab-main",
      id: `files-tab-${tab.id}`,
      role: "tab",
      "aria-selected": active ? "true" : "false",
      "aria-controls": "files-tabpanel",
      "aria-label": `Вкладка: ${fullLabel}`,
      title: fullLabel,
      tabindex: active ? "0" : "-1",
      dataset: { filesTabId: tab.id },
      disabled: fs.busy ? true : null,
    });
    main.append(el("span", { class: "files-tab-label", text: filesTabLabel(tab.path) }));
    main.addEventListener("click", () => activateFilesTab(tab.id, { focus: true }));
    item.append(main);
    if (canClose) {
      const close = el("button", {
        type: "button",
        class: "files-tab-close",
        "aria-label": `Закрыть вкладку: ${fullLabel}`,
        "aria-keyshortcuts": "Control+W",
        title: "Закрыть вкладку (Ctrl+W)",
        dataset: { filesTabClose: tab.id },
        disabled: fs.busy ? true : null,
      });
      close.append(el("span", { "aria-hidden": "true", text: "×" }));
      close.addEventListener("click", () => closeFilesTab(tab.id, { focus: true }));
      item.append(close);
    }
    els.filesTablist.append(item);
  }
  const activeTab = fs.tabs.find((tab) => tab.id === fs.activeTabId);
  if (els.filesTabpanel) {
    if (activeTab) {
      els.filesTabpanel.setAttribute("aria-labelledby", `files-tab-${activeTab.id}`);
      els.filesTabpanel.removeAttribute("aria-label");
    } else {
      els.filesTabpanel.removeAttribute("aria-labelledby");
      els.filesTabpanel.setAttribute("aria-label", "Содержимое открытой папки");
    }
  }
  if (els.filesTabAdd) {
    els.filesTabAdd.disabled = Boolean(fs.busy) || fs.tabs.length >= FS_TABS_LIMIT;
  }
  if (tabFocused && activeTab) focusFilesTabButton(activeTab.id);
  // Reveal a newly active tab once; later re-renders must not fight the user's
  // own horizontal scrolling of a long tab strip.
  if (activeTab && filesRenderedActiveTabId !== activeTab.id) {
    filesRenderedActiveTabId = activeTab.id;
    revealFilesTabButton(activeTab.id);
  }
}

function renderFilesChrome() {
  renderFilesTabs();
  renderFilesCrumbs();
  renderFilesToolbar();
  renderFilesWriteForm();
  renderFilesPreview();
  syncFilesPathInput();
  renderFilesPathError();
}

function renderFilesCrumbs() {
  if (!els.filesBreadcrumbs) return;
  els.filesBreadcrumbs.replaceChildren();
  const crumbs = fsCrumbs(state.files.path);
  crumbs.forEach((crumb, index) => {
    if (index > 0) {
      els.filesBreadcrumbs.append(
        el("span", { class: "files-crumb-sep", "aria-hidden": "true", text: "/" }),
      );
    }
    const last = index === crumbs.length - 1;
    const button = el("button", {
      type: "button",
      class: `files-crumb${last ? " is-current" : ""}`,
      title:
        crumb.path === "."
          ? "Рабочая папка: сюда можно перетащить файл"
          : `Открыть ${crumb.path}; сюда можно перетащить файл`,
      "aria-current": last ? "location" : null,
      dataset: { fsCrumbPath: crumb.path },
      disabled: !last && state.files.busy ? true : null,
      on: {
        click: () => {
          if (last) return;
          clearFilesSearch({ render: false });
          void loadFiles(crumb.path, { focus: true });
        },
      },
    });
    button.append(el("span", { text: crumb.label }));
    els.filesBreadcrumbs.append(button);
  });
}

function renderFilesToolbar() {
  const fs = state.files;
  const searching = filesSearchActive();
  const loading = fs.state === "loading";
  const searchLoading = searching && fs.searchState === "loading";
  const busy = Boolean(fs.busy);
  if (els.filesBack) {
    els.filesBack.disabled = fs.historyIndex <= 0 || busy;
  }
  if (els.filesForward) {
    els.filesForward.disabled =
      fs.historyIndex < 0 || fs.historyIndex >= fs.history.length - 1 || busy;
  }
  if (els.filesPathInput) els.filesPathInput.disabled = busy;
  if (els.filesPathSubmit) els.filesPathSubmit.disabled = busy;
  if (els.filesUp) els.filesUp.disabled = fs.path === "." || loading || busy;
  if (els.filesRefresh) els.filesRefresh.disabled = loading || searchLoading || busy;
  if (els.filesMkdir) {
    els.filesMkdir.disabled = loading || busy;
    els.filesMkdir.setAttribute("aria-expanded", fs.mkdirOpen ? "true" : "false");
  }
  if (els.filesSearchForm) {
    els.filesSearchForm.setAttribute("aria-busy", searchLoading ? "true" : "false");
  }
  if (els.filesSearchSubmit) els.filesSearchSubmit.disabled = busy;
  if (els.filesSearchClear) {
    const hasText = Boolean(els.filesSearchInput && els.filesSearchInput.value.trim());
    els.filesSearchClear.hidden = !searching && !hasText;
    els.filesSearchClear.disabled = busy;
  }
  if (els.filesCount) {
    const value = searching
      ? fs.searchState === "loading"
        ? "…"
        : fs.searchState === "ready"
          ? String(fs.searchMatches.length)
          : fs.searchState === "empty"
            ? "0"
            : "—"
      : loading
        ? "…"
        : fs.state === "ready"
          ? String(fs.entries.length)
          : fs.state === "empty"
            ? "0"
            : "—";
    els.filesCount.textContent = value;
  }
  if (els.filesNote) {
    const note = searching
      ? fs.searchTruncated
        ? "Показаны не все совпадения: сервер ограничил выдачу. Уточните запрос."
        : ""
      : fs.truncated
        ? "Сервер вернул неполный список: показаны не все элементы папки."
        : "";
    els.filesNote.textContent = note;
    els.filesNote.hidden = !note;
  }
  renderFilesSort();
}

function renderFilesSort() {
  const sortKey = FS_SORT_KEYS.has(state.files.sortKey) ? state.files.sortKey : "name";
  const descending = state.files.sortDir === "desc";
  if (els.filesSortKey && els.filesSortKey.value !== sortKey) {
    els.filesSortKey.value = sortKey;
  }
  if (!els.filesSortDir) return;
  const label = descending ? "По убыванию" : "По возрастанию";
  if (els.filesSortDirLabel && els.filesSortDirLabel.textContent !== label) {
    els.filesSortDirLabel.textContent = label;
  }
  els.filesSortDir.classList.toggle("is-desc", descending);
  const ariaLabel =
    `Направление сортировки: ${label.toLowerCase()}. ` +
    `Переключить на ${descending ? "возрастание" : "убывание"}`;
  if (els.filesSortDir.getAttribute("aria-label") !== ariaLabel) {
    els.filesSortDir.setAttribute("aria-label", ariaLabel);
  }
  els.filesSortDir.title = `Сортировка ${label.toLowerCase()}: нажмите, чтобы изменить направление`;
}

function activeFilesRowPath() {
  const active = document.activeElement;
  if (!active || !els.filesList || !els.filesList.contains(active)) return "";
  return typeof active.dataset.fsPath === "string" ? active.dataset.fsPath : "";
}

function setFilesSort(key, direction) {
  const nextKey = FS_SORT_KEYS.has(key) ? key : "name";
  const nextDir = direction === "desc" ? "desc" : "asc";
  if (nextKey === state.files.sortKey && nextDir === state.files.sortDir) return;
  // Rows are keyed by path: restore the focused row after the reorder. The
  // preview and action targets are path-based too, so they stay on the file.
  const focusedPath = activeFilesRowPath();
  state.files.sortKey = nextKey;
  state.files.sortDir = nextDir;
  savePrefs();
  renderFilesList();
  renderFilesSort();
  if (focusedPath) focusFileRow(focusedPath);
}

function onFilesSortKeyChange() {
  if (!els.filesSortKey) return;
  setFilesSort(els.filesSortKey.value, state.files.sortDir);
}

function onFilesSortDirClick() {
  setFilesSort(state.files.sortKey, state.files.sortDir === "desc" ? "asc" : "desc");
}

function renderFilesWriteForm() {
  const fs = state.files;
  const open = fs.mkdirOpen;
  const busy = fs.busy === "mkdir";
  if (els.filesMkdirForm) {
    els.filesMkdirForm.hidden = !open;
    els.filesMkdirForm.setAttribute("aria-busy", busy ? "true" : "false");
  }
  if (els.filesMkdirError) {
    els.filesMkdirError.textContent = open ? fs.mkdirError : "";
    els.filesMkdirError.hidden = !open || !fs.mkdirError;
  }
  if (els.filesMkdirInput) els.filesMkdirInput.disabled = busy;
  if (els.filesMkdirSubmit) {
    els.filesMkdirSubmit.disabled = busy;
    els.filesMkdirSubmit.textContent = busy ? "Создаю…" : "Создать";
  }
  if (els.filesMkdirCancel) els.filesMkdirCancel.disabled = busy;
}

function renderFilesList() {
  if (!els.filesList || !els.filesStatus) return;
  // A rebuild replaces the drag source row, so any in-flight drag state is stale.
  if (state.files.drag) clearFilesDrag();
  // Rows are rebuilt below: an open menu would point at a detached anchor.
  if (state.files.menuOpen) closeFilesMenu({ returnFocus: false });
  const fs = state.files;
  const searching = filesSearchActive();
  const loading = searching ? fs.searchState === "loading" : fs.state === "loading";
  els.filesList.replaceChildren();
  els.filesStatus.replaceChildren();
  els.filesStatus.classList.toggle(
    "is-error",
    searching ? fs.searchState === "error" : fs.state === "error",
  );
  if (els.filesListTitle) {
    els.filesListTitle.textContent = searching ? "Результаты поиска" : "Содержимое";
  }
  if (els.filesEmpty) {
    const empty = searching ? fs.searchState === "empty" : fs.state === "empty";
    els.filesEmpty.hidden = !empty;
    els.filesEmpty.textContent = searching
      ? `По запросу «${fs.searchQuery}» ничего не найдено. Проверьте имя или попробуйте другой запрос.`
      : "В этой папке нет файлов и подпапок.";
  }
  els.filesList.setAttribute("aria-busy", loading ? "true" : "false");
  if (searching) {
    renderFilesSearchList();
    return;
  }

  if (fs.state === "loading") {
    els.filesStatus.append(el("span", { text: "Загружаю список файлов…" }));
    for (let index = 0; index < 4; index += 1) {
      els.filesList.append(el("li", { class: "files-item", "aria-hidden": "true" },
        el("div", { class: "skeleton files-skeleton" }),
      ));
    }
    return;
  }
  if (fs.state === "error") {
    els.filesStatus.append(
      el("span", { text: fs.error || "Не удалось загрузить список файлов." }),
    );
    els.filesStatus.append(
      el("button", {
        type: "button",
        class: "btn btn-ghost btn-small",
        text: "Повторить",
        on: { click: () => void loadFiles(fs.path, { force: true }) },
      }),
    );
    return;
  }
  if (fs.state === "empty") {
    els.filesStatus.append(el("span", { text: "Папка пуста." }));
    return;
  }
  if (fs.state !== "ready") {
    els.filesStatus.append(
      el("span", { text: "Откройте раздел «Файлы», чтобы загрузить список." }),
    );
    return;
  }

  const dirs = fs.entries.filter((entry) => entry.type === "dir").length;
  const files = fs.entries.filter((entry) => entry.type === "file").length;
  const parts = [];
  if (dirs) parts.push(`${dirs} ${plural(dirs, ["папка", "папки", "папок"])}`);
  if (files) parts.push(`${files} ${plural(files, ["файл", "файла", "файлов"])}`);
  const others = fs.entries.length - dirs - files;
  if (others) parts.push(`${others} другое`);
  els.filesStatus.append(el("span", { text: parts.join(" · ") || "Нет элементов." }));

  for (const entry of sortFsItems(fs.entries, fs.sortKey, fs.sortDir)) {
    els.filesList.append(renderFileRow(entry));
  }
}

function renderFilesSearchList() {
  const fs = state.files;
  const query = fs.searchQuery;
  if (fs.searchState === "loading") {
    els.filesStatus.append(el("span", { text: `Ищу «${query}»…` }));
    for (let index = 0; index < 4; index += 1) {
      els.filesList.append(el("li", { class: "files-item", "aria-hidden": "true" },
        el("div", { class: "skeleton files-skeleton" }),
      ));
    }
    return;
  }
  if (fs.searchState === "error") {
    els.filesStatus.append(
      el("span", { text: fs.searchError || "Не удалось выполнить поиск." }),
    );
    els.filesStatus.append(
      el("button", {
        type: "button",
        class: "btn btn-ghost btn-small",
        text: "Повторить",
        on: { click: () => void runFilesSearch(fs.searchQuery) },
      }),
    );
    return;
  }
  if (fs.searchState === "empty") {
    els.filesStatus.append(
      el("span", { text: `Ничего не найдено по запросу «${query}».` }),
    );
    return;
  }
  if (fs.searchState !== "ready") {
    els.filesStatus.append(
      el("span", { text: "Введите запрос и нажмите «Найти»." }),
    );
    return;
  }

  const dirs = fs.searchMatches.filter((match) => match.type === "dir").length;
  const files = fs.searchMatches.filter((match) => match.type === "file").length;
  const parts = [];
  if (dirs) parts.push(`${dirs} ${plural(dirs, ["папка", "папки", "папок"])}`);
  if (files) parts.push(`${files} ${plural(files, ["файл", "файла", "файлов"])}`);
  const others = fs.searchMatches.length - dirs - files;
  if (others) parts.push(`${others} другое`);
  els.filesStatus.append(
    el("span", { text: `${parts.join(" · ") || "Нет результатов"} · запрос «${query}»` }),
  );

  for (const match of sortFsItems(fs.searchMatches, fs.sortKey, fs.sortDir)) {
    els.filesList.append(renderFilesSearchRow(match));
  }
}

function renderFilesSearchRow(match) {
  const path = match.path;
  const previewing = Boolean(state.files.preview && state.files.preview.path === path);
  const interactive = match.type === "dir" || match.type === "file";
  const meta =
    match.type === "dir"
      ? "папка"
      : match.type === "file"
        ? formatFileSize(match.size) || "файл"
        : "другое";
  const node = interactive
    ? el("button", {
        type: "button",
        class: `file-row is-search${previewing ? " is-active" : ""}`,
        dataset: { fsPath: path, fsType: match.type },
        // Only files are transfer sources; folders stay click-only drop targets.
        draggable: match.type === "file" ? "true" : null,
        "aria-label":
          match.type === "dir"
            ? `Открыть папку ${match.name}, путь ${path}`
            : `Открыть файл ${match.name}, путь ${path}${
                match.size !== null ? `, ${formatFileSize(match.size)}` : ""
              }`,
        "aria-haspopup": "menu",
        "aria-expanded": "false",
        "aria-controls": "files-menu",
      })
    : el("div", { class: "file-row is-static is-search" });

  const icon = el("span", {
    class: `file-icon is-${match.type}`,
    "aria-hidden": "true",
  });
  icon.append(fileIcon(match.type));
  node.append(icon);
  const text = el("span", { class: "file-search-text" });
  text.append(el("span", { class: "file-name", text: match.name }));
  text.append(el("span", { class: "file-path", text: path, title: path }));
  node.append(text);
  node.append(el("span", { class: "file-meta", text: meta }));

  if (match.type === "dir") {
    node.addEventListener("click", () => {
      clearFilesSearch({ render: false });
      void loadFiles(path, { focus: true });
    });
  } else if (match.type === "file") {
    node.addEventListener("click", () => void loadFilesPreview(path, node));
  } else {
    node.setAttribute("aria-disabled", "true");
  }
  return el("li", { class: "files-item" }, node);
}

function renderFileRow(entry) {
  const path = entry.path || fsJoin(state.files.path, entry.name);
  const previewing = Boolean(state.files.preview && state.files.preview.path === path);
  const interactive = entry.type === "dir" || entry.type === "file";
  const meta =
    entry.type === "dir"
      ? "папка"
      : entry.type === "file"
        ? formatFileSize(entry.size) || "файл"
        : "другое";
  const node = interactive
    ? el("button", {
        type: "button",
        class: `file-row${previewing ? " is-active" : ""}`,
        dataset: { fsPath: path, fsType: entry.type },
        // Only files are transfer sources; folders stay click-only drop targets.
        draggable: entry.type === "file" ? "true" : null,
        "aria-label":
          entry.type === "dir"
            ? `Открыть папку ${entry.name}`
            : `Открыть файл ${entry.name}${entry.size !== null ? `, ${formatFileSize(entry.size)}` : ""}`,
        "aria-haspopup": "menu",
        "aria-expanded": "false",
        "aria-controls": "files-menu",
      })
    : el("div", { class: "file-row is-static" });

  const icon = el("span", {
    class: `file-icon is-${entry.type}`,
    "aria-hidden": "true",
  });
  if (entry.type === "file" && isFsImagePath(path)) {
    icon.classList.add("has-thumb");
    icon.append(
      el("img", {
        class: "file-thumb",
        src: apiUrl(`/fs/thumb?path=${encodeURIComponent(path)}`),
        alt: "",
        loading: "lazy",
      }),
    );
  } else {
    icon.append(fileIcon(entry.type));
  }
  node.append(icon);
  node.append(el("span", { class: "file-name", text: entry.name }));
  node.append(el("span", { class: "file-meta", text: meta }));

  if (entry.type === "dir") {
    node.addEventListener("click", () => void loadFiles(path, { focus: true }));
  } else if (entry.type === "file") {
    node.addEventListener("click", () => void loadFilesPreview(path, node));
  } else {
    node.setAttribute("aria-disabled", "true");
  }
  return el("li", { class: "files-item" }, node);
}

function renderFilesPreview() {
  if (!els.filesPreviewCard) return;
  // The write forms live outside the preview card now, but they still follow
  // preview/action state on every preview update.
  renderFilesActionForms();
  const preview = state.files.preview;
  const open = Boolean(preview);
  const busy = Boolean(state.files.busy);
  els.filesPreviewCard.hidden = !open;
  if (els.filesLayout) els.filesLayout.classList.toggle("has-preview", open);
  if (!open) {
    els.filesPreviewCard.setAttribute("aria-busy", "false");
    if (els.filesPreviewBody) els.filesPreviewBody.textContent = "";
    if (els.filesPreviewImage) {
      els.filesPreviewImage.hidden = true;
      els.filesPreviewImage.setAttribute("aria-busy", "false");
    }
    if (els.filesPreviewImg) {
      if (els.filesPreviewImg.hasAttribute("src")) els.filesPreviewImg.removeAttribute("src");
      els.filesPreviewImg.alt = "";
    }
    if (els.filesPreviewStatus) els.filesPreviewStatus.textContent = "";
    if (els.filesPreviewError) {
      els.filesPreviewError.hidden = true;
      els.filesPreviewError.textContent = "";
    }
    if (els.filesPreviewRename) {
      els.filesPreviewRename.disabled = true;
      els.filesPreviewRename.setAttribute("aria-expanded", "false");
    }
    if (els.filesPreviewTrash) {
      els.filesPreviewTrash.disabled = true;
      els.filesPreviewTrash.setAttribute("aria-expanded", "false");
    }
    if (els.filesTransferCopy) {
      els.filesTransferCopy.disabled = true;
      els.filesTransferCopy.setAttribute("aria-expanded", "false");
    }
    if (els.filesTransferMove) {
      els.filesTransferMove.disabled = true;
      els.filesTransferMove.setAttribute("aria-expanded", "false");
    }
    return;
  }

  const loading = preview.loading === true;
  const error = preview.error || "";
  const isImage = preview.kind === "image";
  const content = preview.content;
  const hasContent = !isImage && typeof content === "string";
  const src = isImage && typeof preview.src === "string" ? preview.src : "";
  const hasImage = isImage && Boolean(src);
  els.filesPreviewTitle.textContent = preview.name || fsName(preview.path);
  els.filesPreviewPath.textContent = preview.path;
  els.filesPreviewCard.setAttribute("aria-busy", loading ? "true" : "false");
  els.filesPreviewLoading.hidden = !loading;
  if (els.filesPreviewLoadingText) {
    els.filesPreviewLoadingText.textContent = isImage
      ? "Загружаю изображение…"
      : "Читаю файл…";
  }
  els.filesPreviewError.hidden = !error;
  els.filesPreviewError.textContent = error;
  els.filesPreviewStatus.classList.toggle("is-error", Boolean(error));
  els.filesPreviewStatus.textContent = loading
    ? isImage
      ? "Загрузка изображения…"
      : "Загрузка файла…"
    : error
      ? ""
      : isImage
        ? hasImage
          ? `${fsImageMimeLabel(preview.mime)} · ${formatFileSize(preview.bytes)}`
          : "Изображение недоступно."
        : !hasContent || content === ""
          ? "Файл пуст."
          : `${formatFileSize(new TextEncoder().encode(content).length)} · UTF-8${
              preview.truncated ? " · фрагмент" : ""
            }`;
  els.filesPreviewBody.hidden = isImage || !hasContent || loading || Boolean(error);
  if (!isImage && hasContent) {
    if (els.filesPreviewBody.textContent !== content) {
      els.filesPreviewBody.textContent = content;
    }
  } else if (els.filesPreviewBody.textContent) {
    els.filesPreviewBody.textContent = "";
  }
  if (els.filesPreviewImage) {
    els.filesPreviewImage.hidden = !hasImage || loading || Boolean(error);
    els.filesPreviewImage.setAttribute("aria-busy", loading ? "true" : "false");
  }
  if (els.filesPreviewImg) {
    if (hasImage && !loading && !error) {
      if (els.filesPreviewImg.getAttribute("src") !== src) {
        els.filesPreviewImg.src = src;
      }
      const alt = `Предпросмотр изображения: ${preview.name || fsName(preview.path)}`;
      if (els.filesPreviewImg.alt !== alt) els.filesPreviewImg.alt = alt;
    } else {
      if (els.filesPreviewImg.hasAttribute("src")) els.filesPreviewImg.removeAttribute("src");
      els.filesPreviewImg.alt = "";
    }
  }
  if (els.filesPreviewTruncated) {
    els.filesPreviewTruncated.hidden = !preview.truncated || loading || Boolean(error);
  }
  const renameOpen = state.files.renameOpen;
  const trashOpen = state.files.trashOpen;
  const transferOpen = state.files.transferOpen;
  const transferMode = state.files.transferMode === "move" ? "move" : "copy";
  if (els.filesPreviewRename) {
    els.filesPreviewRename.disabled = loading || busy;
    els.filesPreviewRename.setAttribute("aria-expanded", renameOpen ? "true" : "false");
  }
  if (els.filesTransferCopy) {
    els.filesTransferCopy.disabled = loading || busy;
    els.filesTransferCopy.setAttribute(
      "aria-expanded",
      transferOpen && transferMode === "copy" ? "true" : "false",
    );
  }
  if (els.filesTransferMove) {
    els.filesTransferMove.disabled = loading || busy;
    els.filesTransferMove.setAttribute(
      "aria-expanded",
      transferOpen && transferMode === "move" ? "true" : "false",
    );
  }
  if (els.filesPreviewTrash) {
    els.filesPreviewTrash.disabled = loading || busy;
    els.filesPreviewTrash.setAttribute("aria-expanded", trashOpen ? "true" : "false");
  }
  if (els.filesPreviewClose) els.filesPreviewClose.disabled = busy;
  if (els.filesCopy) {
    els.filesCopy.disabled = !hasContent || isImage || loading || Boolean(error) || busy;
  }
  if (els.filesPreviewRefresh) els.filesPreviewRefresh.disabled = loading || busy;
  if (els.filesCopyStatus) els.filesCopyStatus.textContent = state.files.copyStatus;
}

// Rename / trash / transfer forms are shared by the preview actions and by the
// row context menu. They act on filesTarget(): the explicit menu target first,
// then the open preview.
function renderFilesActionForms() {
  const target = filesTarget();
  const renameOpen = state.files.renameOpen;
  const renaming = state.files.busy === "rename";
  if (els.filesRenameTitle) {
    els.filesRenameTitle.textContent =
      target && target.type === "dir" ? "Переименовать папку" : "Переименовать файл";
  }
  if (els.filesRenameForm) {
    els.filesRenameForm.hidden = !renameOpen;
    els.filesRenameForm.setAttribute("aria-busy", renaming ? "true" : "false");
  }
  if (els.filesRenameError) {
    els.filesRenameError.textContent = renameOpen ? state.files.renameError : "";
    els.filesRenameError.hidden = !renameOpen || !state.files.renameError;
  }
  if (els.filesRenameInput) els.filesRenameInput.disabled = renaming;
  if (els.filesRenameSubmit) {
    els.filesRenameSubmit.disabled = renaming || !target;
    els.filesRenameSubmit.textContent = renaming ? "Переименовываю…" : "Переименовать";
  }
  if (els.filesRenameCancel) els.filesRenameCancel.disabled = renaming;

  const trashOpen = state.files.trashOpen;
  const trashing = state.files.busy === "trash";
  if (els.filesTrashForm) {
    els.filesTrashForm.hidden = !trashOpen;
    els.filesTrashForm.setAttribute("aria-busy", trashing ? "true" : "false");
  }
  if (els.filesTrashName) {
    els.filesTrashName.textContent = target ? target.name : "";
  }
  if (els.filesTrashError) {
    els.filesTrashError.textContent = trashOpen ? state.files.trashError : "";
    els.filesTrashError.hidden = !trashOpen || !state.files.trashError;
  }
  if (els.filesTrashInput) els.filesTrashInput.disabled = trashing;
  if (els.filesTrashSubmit) {
    els.filesTrashSubmit.disabled = trashing || !target;
    els.filesTrashSubmit.textContent = trashing ? "Перемещаю…" : "В корзину";
  }
  if (els.filesTrashCancel) els.filesTrashCancel.disabled = trashing;

  const transferOpen = state.files.transferOpen;
  const transferMode = state.files.transferMode === "move" ? "move" : "copy";
  const transferring = state.files.busy === "transfer";
  if (els.filesTransferForm) {
    els.filesTransferForm.hidden = !transferOpen;
    els.filesTransferForm.setAttribute("aria-busy", transferring ? "true" : "false");
  }
  if (els.filesTransferTitle) {
    els.filesTransferTitle.textContent =
      transferMode === "move" ? "Переместить файл" : "Копировать файл";
  }
  if (els.filesTransferHint) {
    els.filesTransferHint.textContent =
      transferMode === "move"
        ? "Укажите полный путь вместе с новым именем. Файл переедет из текущей папки; " +
          "существующие элементы не перезаписываются."
        : "Укажите полный путь вместе с новым именем. Файл останется на месте; " +
          "существующие элементы не перезаписываются.";
  }
  if (els.filesTransferError) {
    els.filesTransferError.textContent = transferOpen ? state.files.transferError : "";
    els.filesTransferError.hidden = !transferOpen || !state.files.transferError;
  }
  if (els.filesTransferInput) els.filesTransferInput.disabled = transferring;
  if (els.filesTransferSubmit) {
    els.filesTransferSubmit.disabled = transferring || !target;
    els.filesTransferSubmit.textContent = transferring
      ? transferMode === "move"
        ? "Перемещаю…"
        : "Копирую…"
      : transferMode === "move"
        ? "Переместить"
        : "Копировать";
  }
  if (els.filesTransferCancel) els.filesTransferCancel.disabled = transferring;
}

async function copyTextToClipboard(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall back to execCommand below */
    }
  }
  const area = document.createElement("textarea");
  area.className = "files-copy-fallback";
  area.value = text;
  area.setAttribute("readonly", "readonly");
  area.setAttribute("aria-hidden", "true");
  area.tabIndex = -1;
  document.body.append(area);
  const selection = document.getSelection();
  const previous =
    selection && selection.rangeCount > 0 ? selection.getRangeAt(0).cloneRange() : null;
  area.select();
  area.setSelectionRange(0, text.length);
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  area.remove();
  if (previous && selection) {
    selection.removeAllRanges();
    selection.addRange(previous);
  }
  if (!ok) throw new Error("clipboard unavailable");
  return true;
}

async function copyFilesPreview() {
  const preview = state.files.preview;
  if (!preview || typeof preview.content !== "string") return;
  if (els.filesCopy) els.filesCopy.disabled = true;
  try {
    await copyTextToClipboard(preview.content);
    state.files.copyStatus = "Скопировано в буфер обмена.";
  } catch {
    state.files.copyStatus = "Не удалось скопировать. Выделите текст и нажмите Ctrl+C.";
    showToast("Буфер обмена недоступен в этом браузере.", "error");
  } finally {
    renderFilesPreview();
  }
}

function focusFilesInput(input) {
  if (!input) return;
  try {
    input.focus({ preventScroll: true });
  } catch {
    input.focus();
  }
  try {
    input.select();
  } catch {
    /* selection is best effort */
  }
}

function focusFileRow(path) {
  const row = fileRow(path);
  if (!row) return false;
  try {
    row.focus({ preventScroll: false });
  } catch {
    row.focus();
  }
  return true;
}

function revealFilesForm(form) {
  if (!form) return;
  try {
    form.scrollIntoView({ block: "nearest" });
  } catch {
    /* scrolling is optional */
  }
}

function openFilesMkdirForm() {
  if (state.files.busy || state.files.state === "loading") return;
  // A new folder belongs to the current directory: leave search mode first.
  if (filesSearchActive()) clearFilesSearch();
  state.files.mkdirOpen = true;
  state.files.mkdirError = "";
  state.files.renameOpen = false;
  state.files.renameError = "";
  state.files.trashOpen = false;
  state.files.trashError = "";
  state.files.transferOpen = false;
  state.files.transferMode = "";
  state.files.transferError = "";
  state.files.target = null;
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
  if (els.filesMkdirInput) els.filesMkdirInput.value = "";
  renderFilesChrome();
  focusFilesInput(els.filesMkdirInput);
}

function closeFilesMkdirForm({ returnFocus = true } = {}) {
  if (!state.files.mkdirOpen || state.files.busy === "mkdir") return;
  state.files.mkdirOpen = false;
  state.files.mkdirError = "";
  renderFilesToolbar();
  renderFilesWriteForm();
  if (returnFocus && els.filesMkdir) {
    try {
      els.filesMkdir.focus({ preventScroll: true });
    } catch {
      els.filesMkdir.focus();
    }
  }
}

function toggleFilesMkdirForm() {
  if (state.files.mkdirOpen) closeFilesMkdirForm();
  else openFilesMkdirForm();
}

function openFilesRenameForm() {
  const target = filesTarget();
  if (!target || target.loading || state.files.busy) return;
  state.files.renameOpen = true;
  state.files.renameError = "";
  state.files.mkdirOpen = false;
  state.files.mkdirError = "";
  state.files.trashOpen = false;
  state.files.trashError = "";
  state.files.transferOpen = false;
  state.files.transferMode = "";
  state.files.transferError = "";
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
  if (els.filesRenameInput) {
    els.filesRenameInput.value = target.name || fsName(target.path);
  }
  renderFilesChrome();
  revealFilesForm(els.filesRenameForm);
  focusFilesInput(els.filesRenameInput);
}

function closeFilesRenameForm({ returnFocus = true } = {}) {
  if (!state.files.renameOpen || state.files.busy === "rename") return;
  const target = filesTarget();
  const targetPath = target ? target.path : "";
  state.files.renameOpen = false;
  state.files.renameError = "";
  if (els.filesRenameInput) els.filesRenameInput.value = "";
  syncFilesTarget();
  renderFilesPreview();
  if (!returnFocus) return;
  if (state.files.preview && els.filesPreviewRename && !els.filesPreviewRename.disabled) {
    try {
      els.filesPreviewRename.focus({ preventScroll: true });
    } catch {
      els.filesPreviewRename.focus();
    }
    return;
  }
  if (targetPath && focusFileRow(targetPath)) return;
  focusFilesList();
}

function toggleFilesRenameForm() {
  if (state.files.renameOpen) closeFilesRenameForm();
  else openFilesRenameForm();
}

function openFilesTrashForm() {
  const target = filesTarget();
  if (!target || target.loading || state.files.busy) return;
  state.files.trashOpen = true;
  state.files.trashError = "";
  state.files.renameOpen = false;
  state.files.renameError = "";
  state.files.mkdirOpen = false;
  state.files.mkdirError = "";
  state.files.transferOpen = false;
  state.files.transferMode = "";
  state.files.transferError = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  renderFilesChrome();
  revealFilesForm(els.filesTrashForm);
  focusFilesInput(els.filesTrashInput);
}

function closeFilesTrashForm({ returnFocus = true } = {}) {
  if (!state.files.trashOpen || state.files.busy === "trash") return;
  const target = filesTarget();
  const targetPath = target ? target.path : "";
  state.files.trashOpen = false;
  state.files.trashError = "";
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  syncFilesTarget();
  renderFilesPreview();
  if (!returnFocus) return;
  if (state.files.preview && els.filesPreviewTrash && !els.filesPreviewTrash.disabled) {
    try {
      els.filesPreviewTrash.focus({ preventScroll: true });
    } catch {
      els.filesPreviewTrash.focus();
    }
    return;
  }
  if (targetPath && focusFileRow(targetPath)) return;
  focusFilesList();
}

function toggleFilesTrashForm() {
  if (state.files.trashOpen) closeFilesTrashForm();
  else openFilesTrashForm();
}

async function submitFilesTrash(event) {
  if (event) event.preventDefault();
  if (state.files.busy) return;
  const target = filesTarget();
  if (!target) {
    closeFilesTrashForm({ returnFocus: false });
    return;
  }
  const name = target.name || fsName(target.path);
  const typed = els.filesTrashInput ? els.filesTrashInput.value.trim() : "";
  if (!typed) {
    state.files.trashError = `Введите имя «${name}», чтобы подтвердить.`;
    renderFilesPreview();
    focusFilesInput(els.filesTrashInput);
    return;
  }
  if (typed !== name) {
    state.files.trashError = `Имя не совпадает. Введите «${name}» без изменений.`;
    renderFilesPreview();
    focusFilesInput(els.filesTrashInput);
    return;
  }
  // The server repeats this check; POST only fires on an exact name match.
  state.files.busy = "trash";
  state.files.trashError = "";
  renderFilesChrome();
  let trashed = false;
  try {
    await apiRequest("/fs/trash", {
      method: "POST",
      body: { path: target.path, confirm: typed },
      timeout: 20000,
    });
    trashed = true;
    state.files.trashOpen = false;
    if (els.filesTrashInput) els.filesTrashInput.value = "";
    closeFilesPreview({ returnFocus: false });
    showToast(`Перемещено в Корзину: ${name}`, "ok");
    if (filesSearchActive()) {
      // Refresh the hidden directory list too, so clearing search shows the removal.
      await loadFiles(state.files.path, { force: true });
      await runFilesSearch(state.files.searchQuery);
      focusFilesList();
    } else {
      await loadFiles(state.files.path, { force: true, focus: true });
    }
  } catch (error) {
    state.files.trashError = describeFsWriteError(error, "перемещение в корзину");
  } finally {
    state.files.busy = "";
    syncFilesTarget();
    renderFilesChrome();
    if (!trashed && state.files.trashOpen) {
      focusFilesInput(els.filesTrashInput);
    }
  }
}

async function submitFilesMkdir(event) {
  if (event) event.preventDefault();
  if (state.files.busy) return;
  const check = validateFsName(
    els.filesMkdirInput ? els.filesMkdirInput.value : "",
    "имя папки",
  );
  if (!check.ok) {
    state.files.mkdirError = check.error;
    renderFilesWriteForm();
    focusFilesInput(els.filesMkdirInput);
    return;
  }
  const target = fsJoin(state.files.path, check.name);
  state.files.busy = "mkdir";
  state.files.mkdirError = "";
  renderFilesChrome();
  let created = "";
  try {
    const data = await apiRequest("/fs/mkdir", {
      method: "POST",
      body: { path: target },
      timeout: 15000,
    });
    created = data && typeof data.path === "string" ? normalizeFsPath(data.path) : target;
    state.files.mkdirOpen = false;
    if (els.filesMkdirInput) els.filesMkdirInput.value = "";
    showToast(`Папка создана: ${created}`, "ok");
    await loadFiles(state.files.path, { force: true });
  } catch (error) {
    state.files.mkdirError = describeFsWriteError(error, "создание папки");
  } finally {
    state.files.busy = "";
    renderFilesChrome();
    if (created) {
      if (!focusFileRow(created)) focusFilesList();
    } else if (state.files.mkdirOpen) {
      focusFilesInput(els.filesMkdirInput);
    }
  }
}

async function submitFilesRename(event) {
  if (event) event.preventDefault();
  if (state.files.busy) return;
  const target = filesTarget();
  if (!target) {
    closeFilesRenameForm({ returnFocus: false });
    return;
  }
  const currentName = target.name || fsName(target.path);
  const check = validateFsName(
    els.filesRenameInput ? els.filesRenameInput.value : "",
    "новое имя",
  );
  if (!check.ok) {
    state.files.renameError = check.error;
    renderFilesPreview();
    focusFilesInput(els.filesRenameInput);
    return;
  }
  if (check.name === currentName) {
    state.files.renameError = "Имя не изменилось. Введите другое имя.";
    renderFilesPreview();
    focusFilesInput(els.filesRenameInput);
    return;
  }
  const source = target.path;
  const destination = fsJoin(fsParent(source) || ".", check.name);
  state.files.busy = "rename";
  state.files.renameError = "";
  renderFilesChrome();
  let renamed = false;
  let renamedPath = "";
  let focusHandled = false;
  try {
    const data = await apiRequest("/fs/rename", {
      method: "POST",
      body: { source, destination },
      timeout: 15000,
    });
    const resolved =
      data && typeof data.destination === "string"
        ? normalizeFsPath(data.destination)
        : destination;
    state.files.renameOpen = false;
    renamed = true;
    renamedPath = resolved;
    showToast(`Переименовано: ${currentName} → ${fsName(resolved)}`, "ok");
    await loadFiles(state.files.path, { force: true });
    // Keep search results accurate after a rename.
    if (filesSearchActive()) {
      await runFilesSearch(state.files.searchQuery);
    }
    if (state.files.preview && state.files.preview.path === source) {
      await loadFilesPreview(resolved, fileRow(resolved));
      focusHandled = true;
    }
  } catch (error) {
    state.files.renameError = describeFsWriteError(error, "переименование файла");
  } finally {
    state.files.busy = "";
    syncFilesTarget();
    renderFilesChrome();
    if (!renamed && state.files.renameOpen) {
      focusFilesInput(els.filesRenameInput);
    } else if (renamed && !focusHandled) {
      // No preview followed the rename: keep the renamed row reachable.
      if (!focusFileRow(renamedPath)) focusFilesList();
    }
  }
}

function openFilesTransferForm(mode) {
  const target = filesTarget();
  if (!target || target.loading || state.files.busy) return;
  const transferMode = mode === "move" ? "move" : "copy";
  const previousValue =
    state.files.transferOpen && els.filesTransferInput ? els.filesTransferInput.value : "";
  state.files.transferOpen = true;
  state.files.transferMode = transferMode;
  state.files.transferError = "";
  state.files.renameOpen = false;
  state.files.renameError = "";
  state.files.trashOpen = false;
  state.files.trashError = "";
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  if (els.filesTransferInput) {
    els.filesTransferInput.value = previousValue || target.path || "";
  }
  renderFilesChrome();
  revealFilesForm(els.filesTransferForm);
  focusFilesInput(els.filesTransferInput);
}

function closeFilesTransferForm({ returnFocus = true } = {}) {
  if (!state.files.transferOpen || state.files.busy === "transfer") return;
  const target = filesTarget();
  const targetPath = target ? target.path : "";
  const mode = state.files.transferMode;
  state.files.transferOpen = false;
  state.files.transferMode = "";
  state.files.transferError = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
  syncFilesTarget();
  renderFilesPreview();
  if (!returnFocus) return;
  const trigger = mode === "move" ? els.filesTransferMove : els.filesTransferCopy;
  if (state.files.preview && trigger && !trigger.disabled) {
    try {
      trigger.focus({ preventScroll: true });
    } catch {
      trigger.focus();
    }
    return;
  }
  if (targetPath && focusFileRow(targetPath)) return;
  focusFilesList();
}

function toggleFilesTransferForm(mode) {
  if (state.files.transferOpen && state.files.transferMode === mode) {
    closeFilesTransferForm();
    return;
  }
  openFilesTransferForm(mode);
}

async function submitFilesTransfer(event) {
  if (event) event.preventDefault();
  if (state.files.busy) return;
  const target = filesTarget();
  if (!target) {
    closeFilesTransferForm({ returnFocus: false });
    return;
  }
  const mode = state.files.transferMode === "move" ? "move" : "copy";
  const source = target.path;
  const check = validateFsDestination(
    els.filesTransferInput ? els.filesTransferInput.value : "",
    source,
  );
  if (!check.ok) {
    state.files.transferError = check.error;
    renderFilesPreview();
    focusFilesInput(els.filesTransferInput);
    return;
  }
  const destination = check.path;
  const name = target.name || fsName(source);
  const endpoint = mode === "move" ? "/fs/move" : "/fs/copy";
  state.files.busy = "transfer";
  state.files.transferError = "";
  renderFilesChrome();
  let transferred = false;
  let transferredPath = "";
  let focusHandled = false;
  try {
    const data = await apiRequest(endpoint, {
      method: "POST",
      body: { source, destination },
      timeout: 30000,
    });
    const resolved =
      data && typeof data.destination === "string"
        ? normalizeFsPath(data.destination)
        : destination;
    state.files.transferOpen = false;
    state.files.transferMode = "";
    if (els.filesTransferInput) els.filesTransferInput.value = "";
    transferred = true;
    transferredPath = resolved;
    showToast(
      mode === "move"
        ? `Перемещено: ${name} → ${resolved}`
        : `Скопировано: ${name} → ${resolved}`,
      "ok",
    );
    await loadFiles(state.files.path, { force: true });
    // Keep active search results accurate after the transfer.
    if (filesSearchActive()) {
      await runFilesSearch(state.files.searchQuery);
    }
    if (mode === "move" && state.files.preview && state.files.preview.path === source) {
      // Follow the moved file so the preview does not point at a missing path.
      await loadFilesPreview(resolved, fileRow(resolved));
      focusHandled = true;
    } else if (state.files.preview) {
      focusFilesPreview();
      focusHandled = true;
    }
  } catch (error) {
    state.files.transferError = describeFilesTransferError(error, mode);
  } finally {
    state.files.busy = "";
    syncFilesTarget();
    renderFilesChrome();
    if (!transferred && state.files.transferOpen) {
      focusFilesInput(els.filesTransferInput);
    } else if (transferred && !focusHandled) {
      // No preview to follow: keep the result keyboard-reachable.
      if (mode === "copy" && transferredPath) {
        if (!focusFileRow(transferredPath)) focusFilesList();
      } else {
        focusFilesList();
      }
    }
  }
}

/* ------------------------------------------------------------------ */
/* Files drag-and-drop transfer (POST /fs/move, POST /fs/copy)        */
/*                                                                    */
/* Files are the only drag sources; directory rows and breadcrumbs    */
/* are drop targets, and empty browser space drops into the open      */
/* folder. A plain drop moves, Ctrl/Alt copies. The server is the     */
/* authority on collisions: the UI never pre-checks overwrite. The    */
/* row context menu stays the keyboard fallback.                      */
/* ------------------------------------------------------------------ */

function filesDragCopyMode(event) {
  return event && (event.ctrlKey || event.altKey || event.metaKey) ? "copy" : "move";
}

function filesDragDestination(drag, targetDir) {
  return fsJoin(targetDir, drag.name);
}

function filesDropTargetFor(node) {
  if (!(node instanceof Element)) return null;
  const row = node.closest("button.file-row");
  if (row) {
    if (!els.filesList || !els.filesList.contains(row)) return null;
    // File rows are not drop targets: dropping onto one does nothing.
    if (row.dataset.fsType !== "dir") return null;
    const raw = row.dataset.fsPath || "";
    if (!raw) return null;
    return { path: normalizeFsPath(raw), element: row, kind: "dir" };
  }
  const crumb = node.closest("button.files-crumb");
  if (crumb) {
    if (!els.filesBreadcrumbs || !els.filesBreadcrumbs.contains(crumb)) return null;
    const raw = crumb.dataset.fsCrumbPath || "";
    if (!raw) return null;
    return { path: normalizeFsPath(raw), element: crumb, kind: "crumb" };
  }
  // Empty space inside the browser means the folder that is currently open.
  if (els.filesBrowser && els.filesBrowser.contains(node)) {
    return {
      path: normalizeFsPath(state.files.path),
      element: els.filesBrowser,
      kind: "current",
    };
  }
  return null;
}

function clearFilesDropHighlight() {
  const drag = state.files.drag;
  if (!drag) return;
  if (drag.over && drag.over.isConnected) {
    drag.over.classList.remove("is-drop-target", "is-drop-copy");
  }
  drag.over = null;
  drag.targetDir = "";
  drag.target = "";
  drag.sameFolder = false;
}

function renderFilesDragIndicator() {
  const node = els.filesDragIndicator;
  if (!node) return;
  const drag = state.files.drag;
  if (!drag) {
    node.hidden = true;
    node.textContent = "";
    node.classList.remove("is-copy", "is-noop");
    return;
  }
  const copy = drag.mode === "copy";
  node.hidden = false;
  node.classList.toggle("is-copy", copy);
  node.classList.toggle("is-noop", !drag.target && drag.sameFolder === true);
  let text;
  if (drag.target) {
    text =
      `${copy ? "Копирование" : "Перемещение"}: ${drag.source} → ${drag.target}. ` +
      "Ctrl или Alt — копирование.";
  } else if (drag.sameFolder) {
    text = `Файл «${drag.name}» уже находится в этой папке. Выберите другую папку.`;
  } else {
    text =
      `${copy ? "Копирование" : "Перемещение"}: «${drag.name}». ` +
      "Наведите на папку или на путь выше; Ctrl или Alt — копирование.";
  }
  if (node.textContent !== text) node.textContent = text;
}

function setFilesDropTarget(target, mode) {
  const drag = state.files.drag;
  if (!drag) return;
  const copy = mode === "copy";
  if (drag.over !== target.element) {
    if (drag.over && drag.over.isConnected) {
      drag.over.classList.remove("is-drop-target", "is-drop-copy");
    }
    drag.over = target.element;
    target.element.classList.add("is-drop-target");
  }
  target.element.classList.toggle("is-drop-copy", copy);
  drag.targetDir = target.path;
  drag.target = filesDragDestination(drag, target.path);
  drag.sameFolder = false;
  renderFilesDragIndicator();
}

function clearFilesDrag() {
  const drag = state.files.drag;
  if (!drag) return;
  if (drag.over && drag.over.isConnected) {
    drag.over.classList.remove("is-drop-target", "is-drop-copy");
  }
  state.files.drag = null;
  if (els.filesList) {
    for (const row of els.filesList.querySelectorAll(".file-row.is-dragging")) {
      row.classList.remove("is-dragging");
    }
  }
  renderFilesDragIndicator();
}

function onFilesDragStart(event) {
  const row = event.target instanceof Element ? event.target.closest("button.file-row") : null;
  if (!row || row.dataset.fsType !== "file") {
    event.preventDefault();
    return;
  }
  if (state.files.busy) {
    event.preventDefault();
    return;
  }
  const raw = row.dataset.fsPath || "";
  const source = raw ? normalizeFsPath(raw) : "";
  if (!source) {
    event.preventDefault();
    return;
  }
  if (state.files.menuOpen) closeFilesMenu({ returnFocus: false });
  state.files.drag = {
    source,
    name: fsName(source),
    mode: filesDragCopyMode(event),
    target: "",
    targetDir: "",
    sameFolder: false,
    over: null,
  };
  row.classList.add("is-dragging");
  if (event.dataTransfer) {
    try {
      event.dataTransfer.effectAllowed = "copyMove";
    } catch {
      /* older browsers may reject the assignment */
    }
    try {
      // The path is only a fallback; state.files.drag remains authoritative.
      event.dataTransfer.setData("text/plain", source);
    } catch {
      /* ignore */
    }
  }
  renderFilesDragIndicator();
}

function onFilesDragOver(event) {
  const drag = state.files.drag;
  if (!drag) return;
  drag.mode = filesDragCopyMode(event);
  const target = filesDropTargetFor(event.target);
  if (!target) {
    clearFilesDropHighlight();
    renderFilesDragIndicator();
    return;
  }
  const destination = filesDragDestination(drag, target.path);
  if (destination === normalizeFsPath(drag.source)) {
    clearFilesDropHighlight();
    drag.sameFolder = true;
    renderFilesDragIndicator();
    return;
  }
  // Only a valid target accepts the drop; every other spot keeps the native
  // "no drop" cursor instead of promising an action the server would refuse.
  event.preventDefault();
  if (event.dataTransfer) {
    try {
      event.dataTransfer.dropEffect = drag.mode === "copy" ? "copy" : "move";
    } catch {
      /* dropEffect is a hint only */
    }
  }
  setFilesDropTarget(target, drag.mode);
}

function onFilesDragOverDocument(event) {
  if (!state.files.drag) return;
  const node = event.target;
  if (!(node instanceof Node)) return;
  const inside =
    (els.filesBrowser && els.filesBrowser.contains(node)) ||
    (els.filesBreadcrumbs && els.filesBreadcrumbs.contains(node));
  if (inside) return;
  // The pointer left both drop zones: hide the highlight but keep dragging.
  if (state.files.drag.target || state.files.drag.over || state.files.drag.sameFolder) {
    clearFilesDropHighlight();
    renderFilesDragIndicator();
  }
}

function onFilesDragEnd() {
  clearFilesDrag();
}

function onFilesDropOutside(event) {
  if (!state.files.drag) return;
  // A drop-zone handler clears the drag itself; anything left here is an
  // outside drop, so suppress the browser default and hide the highlight.
  event.preventDefault();
  clearFilesDrag();
}

function onFilesDrop(event) {
  const drag = state.files.drag;
  if (!drag) return;
  // Stop the browser from treating the drop as navigation even when the spot
  // is not a drop zone.
  event.preventDefault();
  const target = filesDropTargetFor(event.target);
  const mode = filesDragCopyMode(event);
  const source = drag.source;
  const name = drag.name;
  const destination = target ? filesDragDestination(drag, target.path) : "";
  const sameFolder = Boolean(target) && destination === normalizeFsPath(source);
  // Clear before confirm() so the highlight never outlives the dialog.
  clearFilesDrag();
  if (!target) return;
  if (sameFolder) {
    showToast(`Файл «${name}» уже находится в этой папке.`, "info", 6000);
    return;
  }
  if (state.files.busy) return;
  const copy = mode === "copy";
  const confirmed = window.confirm(
    `${copy ? "Копировать файл?" : "Переместить файл?"}\n\n` +
      `Источник: ${source}\n` +
      `Назначение: ${destination}\n` +
      `Действие: ${copy ? "копирование" : "перемещение"}\n\n` +
      "Существующие элементы не перезаписываются. Продолжить?",
  );
  if (!confirmed) return;
  void runFilesDropTransfer({ source, destination, name, mode });
}

async function runFilesDropTransfer({ source, destination, name, mode }) {
  if (state.files.busy) return;
  const copy = mode === "copy";
  const endpoint = copy ? "/fs/copy" : "/fs/move";
  state.files.busy = "transfer";
  state.files.transferError = "";
  renderFilesChrome();
  let transferred = false;
  let resolved = destination;
  let focusHandled = false;
  try {
    const data = await apiRequest(endpoint, {
      method: "POST",
      body: { source, destination },
      timeout: 30000,
    });
    resolved =
      data && typeof data.destination === "string"
        ? normalizeFsPath(data.destination)
        : destination;
    transferred = true;
    showToast(
      copy ? `Скопировано: ${name} → ${resolved}` : `Перемещено: ${name} → ${resolved}`,
      "ok",
    );
    await loadFiles(state.files.path, { force: true });
    // Keep active search results accurate after the transfer.
    if (filesSearchActive()) await runFilesSearch(state.files.searchQuery);
    if (!copy && state.files.preview && state.files.preview.path === source) {
      // Follow the moved file so the preview does not point at a missing path.
      await loadFilesPreview(resolved, fileRow(resolved));
      focusHandled = true;
    } else if (state.files.preview) {
      focusFilesPreview();
      focusHandled = true;
    }
  } catch (error) {
    showToast(describeFilesTransferError(error, mode), "error", 9000);
  } finally {
    state.files.busy = "";
    syncFilesTarget();
    renderFilesChrome();
    if (transferred && !focusHandled) {
      // The list was rebuilt, so keep the result keyboard-reachable.
      const focused = copy ? focusFileRow(source) : focusFileRow(resolved);
      if (!focused) focusFilesList();
    }
  }
}

function onFilesListKeydown(event) {
  if (!els.filesList) return;
  const rows = Array.from(els.filesList.querySelectorAll("button.file-row"));
  if (!rows.length) return;
  const current = rows.indexOf(document.activeElement);
  // Shift+F10, the ContextMenu key and its legacy "Apps" alias open the row
  // menu anchored to the focused row.
  if (
    event.key === "ContextMenu" ||
    event.key === "Apps" ||
    (event.key === "F10" && event.shiftKey)
  ) {
    if (current === -1) return;
    event.preventDefault();
    openFilesMenuForRow(rows[current], { keyboard: true });
    return;
  }
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  if (current === -1) return;
  let next = current;
  if (event.key === "ArrowDown") next = Math.min(rows.length - 1, current + 1);
  else if (event.key === "ArrowUp") next = Math.max(0, current - 1);
  else if (event.key === "Home") next = 0;
  else if (event.key === "End") next = rows.length - 1;
  if (next === current) return;
  event.preventDefault();
  try {
    rows[next].focus({ preventScroll: false });
  } catch {
    rows[next].focus();
  }
}

/* ------------------------------------------------------------------ */
/* Files context menu (right click / Shift+F10 / ContextMenu key)      */
/* ------------------------------------------------------------------ */

const FILES_MENU_MARGIN = 8;
const FILES_MENU_LABELS = Object.freeze({
  open: "Открыть",
  preview: "Открыть/предпросмотр",
  copy: "Копировать",
  move: "Переместить",
  rename: "Переименовать",
  trash: "В корзину",
  path: "Копировать путь",
});

function filesMenuItemsFor(type) {
  const busy = Boolean(state.files.busy);
  if (type === "dir") {
    return [
      { action: "open", label: FILES_MENU_LABELS.open },
      { action: "rename", label: FILES_MENU_LABELS.rename, disabled: busy },
      { action: "trash", label: FILES_MENU_LABELS.trash, danger: true, disabled: busy },
      { action: "path", label: FILES_MENU_LABELS.path, separatorBefore: true },
    ];
  }
  return [
    { action: "open", label: FILES_MENU_LABELS.preview },
    { action: "copy", label: FILES_MENU_LABELS.copy, disabled: busy },
    { action: "move", label: FILES_MENU_LABELS.move, disabled: busy },
    { action: "rename", label: FILES_MENU_LABELS.rename, disabled: busy },
    { action: "trash", label: FILES_MENU_LABELS.trash, danger: true, disabled: busy },
    { action: "path", label: FILES_MENU_LABELS.path, separatorBefore: true },
  ];
}

function filesMenuButtons() {
  if (!els.filesMenu) return [];
  return Array.from(els.filesMenu.querySelectorAll("button[data-fs-menu-action]:not(:disabled)"));
}

function focusFilesMenuItem(index) {
  const buttons = filesMenuButtons();
  if (!buttons.length) return;
  const safe = ((index % buttons.length) + buttons.length) % buttons.length;
  state.files.menuIndex = safe;
  const button = buttons[safe];
  try {
    button.focus({ preventScroll: true });
  } catch {
    button.focus();
  }
}

function renderFilesMenuItems(items) {
  const menu = els.filesMenu;
  if (!menu) return;
  menu.replaceChildren();
  for (const item of items) {
    if (item.separatorBefore) {
      menu.append(el("div", { class: "files-menu-sep", role: "separator" }));
    }
    menu.append(
      el("button", {
        type: "button",
        class: `files-menu-item${item.danger ? " is-danger" : ""}`,
        role: "menuitem",
        tabindex: "-1",
        "data-fs-menu-action": item.action,
        disabled: item.disabled ? true : null,
      }, el("span", { text: item.label })),
    );
  }
}

function positionFilesMenu(x, y, row, keyboard) {
  const menu = els.filesMenu;
  if (!menu) return;
  const viewportW = window.innerWidth;
  const viewportH = window.innerHeight;
  // Cap the height before measuring so a long menu cannot push itself offscreen.
  menu.style.maxHeight = `${Math.max(120, viewportH - FILES_MENU_MARGIN * 2)}px`;
  const rect = menu.getBoundingClientRect();
  const width = rect.width;
  const height = rect.height;
  let left;
  let top;
  if (keyboard || !Number.isFinite(x) || !Number.isFinite(y)) {
    const rowRect = row ? row.getBoundingClientRect() : null;
    left = rowRect ? rowRect.left : FILES_MENU_MARGIN;
    if (rowRect) {
      const spaceBelow = viewportH - rowRect.bottom;
      // Flip above the row when the menu would overflow the bottom edge.
      top =
        spaceBelow >= height + FILES_MENU_MARGIN
          ? rowRect.bottom + 4
          : Math.max(FILES_MENU_MARGIN, rowRect.top - height - 4);
    } else {
      top = FILES_MENU_MARGIN;
    }
  } else {
    left = x;
    top = y;
    // Flip to the opposite side of the pointer, then clamp to the viewport.
    if (left + width > viewportW - FILES_MENU_MARGIN) left = x - width;
    if (top + height > viewportH - FILES_MENU_MARGIN) top = y - height;
  }
  left = Math.max(FILES_MENU_MARGIN, Math.min(left, viewportW - FILES_MENU_MARGIN - width));
  top = Math.max(FILES_MENU_MARGIN, Math.min(top, viewportH - FILES_MENU_MARGIN - height));
  menu.style.left = `${Math.round(left)}px`;
  menu.style.top = `${Math.round(top)}px`;
}

function openFilesMenu({ row, path, type, x, y, keyboard = false }) {
  const menu = els.filesMenu;
  if (!menu || !path) return;
  closeFilesMenu({ returnFocus: false });
  const menuType = type === "dir" ? "dir" : "file";
  const name = fsName(path);
  state.files.menuOpen = true;
  state.files.menuRow = row || null;
  state.files.menuPath = path;
  state.files.menuType = menuType;
  state.files.menuKeyboard = Boolean(keyboard);
  state.files.menuIndex = 0;
  renderFilesMenuItems(filesMenuItemsFor(menuType));
  menu.setAttribute(
    "aria-label",
    `${menuType === "dir" ? "Действия с папкой" : "Действия с файлом"} ${
      name ? name.slice(0, 120) : path.slice(0, 120)
    }`,
  );
  menu.hidden = false;
  positionFilesMenu(x, y, row, keyboard);
  if (row && row.isConnected) {
    row.setAttribute("aria-expanded", "true");
    row.classList.add("is-menu-open");
  }
  focusFilesMenuItem(0);
}

function closeFilesMenu({ returnFocus = false } = {}) {
  const menu = els.filesMenu;
  if (!menu || !state.files.menuOpen) return;
  const row = state.files.menuRow;
  const path = state.files.menuPath;
  state.files.menuOpen = false;
  state.files.menuRow = null;
  state.files.menuPath = "";
  state.files.menuType = "";
  state.files.menuIndex = 0;
  state.files.menuKeyboard = false;
  menu.hidden = true;
  menu.replaceChildren();
  menu.style.left = "";
  menu.style.top = "";
  menu.style.maxHeight = "";
  if (row && row.isConnected) {
    row.setAttribute("aria-expanded", "false");
    row.classList.remove("is-menu-open");
  }
  if (!returnFocus) return;
  const targetRow = row && row.isConnected ? row : path ? fileRow(path) : null;
  if (!targetRow) return;
  try {
    targetRow.focus({ preventScroll: true });
  } catch {
    targetRow.focus();
  }
}

function openFilesMenuForRow(row, { keyboard = false, x = NaN, y = NaN } = {}) {
  if (!row || !row.dataset) return;
  const path = row.dataset.fsPath || "";
  const type = row.dataset.fsType === "dir" ? "dir" : row.dataset.fsType === "file" ? "file" : "";
  if (!path || !type) return;
  openFilesMenu({ row, path, type, x, y, keyboard });
}

function onFilesListContextMenu(event) {
  if (!els.filesList) return;
  const row =
    event.target instanceof Element ? event.target.closest("button.file-row") : null;
  if (!row || !els.filesList.contains(row)) return;
  event.preventDefault();
  openFilesMenuForRow(row, { x: event.clientX, y: event.clientY });
}

async function copyFilesPath(path) {
  const value = normalizeFsPath(path);
  if (!value) return;
  try {
    await copyTextToClipboard(value);
    showToast(`Путь скопирован: ${value}`, "ok");
  } catch {
    showToast("Не удалось скопировать путь: буфер обмена недоступен.", "error");
  }
}

function activateFilesMenuItem(action) {
  const path = state.files.menuPath;
  const type = state.files.menuType;
  if (!path || !type) {
    closeFilesMenu({ returnFocus: false });
    return;
  }
  const keyboard = state.files.menuKeyboard;
  closeFilesMenu({ returnFocus: keyboard });
  const target = { path, name: fsName(path), type };
  switch (action) {
    case "open":
      if (type === "dir") {
        if (filesSearchActive()) clearFilesSearch({ render: false });
        void loadFiles(path, { focus: true });
      } else {
        void loadFilesPreview(path, fileRow(path));
      }
      break;
    case "copy":
    case "move": {
      if (state.files.busy) break;
      setFilesTarget(target);
      openFilesTransferForm(action);
      syncFilesTarget();
      break;
    }
    case "rename": {
      if (state.files.busy) break;
      setFilesTarget(target);
      openFilesRenameForm();
      syncFilesTarget();
      break;
    }
    case "trash": {
      if (state.files.busy) break;
      setFilesTarget(target);
      openFilesTrashForm();
      syncFilesTarget();
      break;
    }
    case "path":
      void copyFilesPath(path);
      break;
    default:
      break;
  }
}

function onFilesMenuItemClick(event) {
  if (!(event.target instanceof Element)) return;
  const button = event.target.closest("button[data-fs-menu-action]");
  if (!button || !els.filesMenu || !els.filesMenu.contains(button)) return;
  event.preventDefault();
  activateFilesMenuItem(button.dataset.fsMenuAction || "");
}

function onFilesMenuKeydown(event) {
  if (!state.files.menuOpen) return;
  const buttons = filesMenuButtons();
  const current = buttons.indexOf(document.activeElement);
  if (event.key === "ArrowDown") {
    event.preventDefault();
    focusFilesMenuItem(current + 1);
    return;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    focusFilesMenuItem(current - 1);
    return;
  }
  if (event.key === "Home") {
    event.preventDefault();
    focusFilesMenuItem(0);
    return;
  }
  if (event.key === "End") {
    event.preventDefault();
    focusFilesMenuItem(buttons.length - 1);
    return;
  }
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    if (current !== -1) {
      activateFilesMenuItem(buttons[current].dataset.fsMenuAction || "");
    } else {
      closeFilesMenu({ returnFocus: true });
    }
    return;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeFilesMenu({ returnFocus: true });
    return;
  }
  if (event.key === "Tab") {
    // Let Tab move focus; just dismiss the menu.
    closeFilesMenu({ returnFocus: false });
  }
}

function onFilesMenuPointerDown(event) {
  if (!state.files.menuOpen) return;
  if (
    event.target instanceof Element &&
    els.filesMenu &&
    els.filesMenu.contains(event.target)
  ) {
    return;
  }
  closeFilesMenu({ returnFocus: false });
}

function onFilesMenuScroll(event) {
  if (!state.files.menuOpen) return;
  if (
    event &&
    event.target instanceof Element &&
    els.filesMenu &&
    els.filesMenu.contains(event.target)
  ) {
    return;
  }
  closeFilesMenu({ returnFocus: false });
}

function onFilesMenuResize() {
  if (state.files.menuOpen) closeFilesMenu({ returnFocus: false });
}

/* ------------------------------------------------------------------ */
/* CRM (GET /crm/status, GET /crm/read)                               */
/* ------------------------------------------------------------------ */

function normalizeCrmStatus(raw) {
  const data = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  return {
    connected: data.connected === true,
    mode: data.mode === "cdp" ? "cdp" : "extension",
    hint: typeof data.hint === "string" ? data.hint.trim().slice(0, 800) : "",
    url: typeof data.url === "string" ? data.url.trim().slice(0, 500) : "",
    error: typeof data.error === "string" ? data.error.trim().slice(0, 300) : "",
  };
}

function describeCrmError(error, context) {
  if (!(error instanceof ApiError)) {
    return `Не удалось выполнить ${context}: ${
      error instanceof Error ? error.message : "неизвестная ошибка"
    }.`;
  }
  if (error.status === 503) {
    return "CRM не настроен на сервере (503). Запустите Kivi с поддержкой CRM и обновите страницу.";
  }
  if (error.status === 403) {
    return "Сервер запретил действие (403). Включите расширение AltCRM и откройте карточку заказа, затем повторите.";
  }
  if (error.status === 404) return `Сервер не поддерживает ${error.path || context} (404).`;
  if (error.status === 400) {
    return `Сервер отклонил запрос${error.code ? `: ${error.code}` : ""}.`;
  }
  if (error.status >= 500) {
    return `Сервер Kivi не смог выполнить действие (${error.status}). Проверьте карточку и повторите позже.`;
  }
  return describeApiError(error, context);
}

async function loadCrmStats() {
  try {
    const data = await apiRequest("/crm/stats", { timeout: 3000 });
    if (!data || typeof data !== "object") return;
    state.crm.stats = {
      total: Number.isFinite(data.total) ? data.total : 0,
      unique: Number.isFinite(data.unique) ? data.unique : 0,
    };
    renderCrm();
  } catch {
    // Optional endpoint: older servers simply have no read log.
  }
}

async function loadCrmStatus({ force = false } = {}) {
  if (state.crm.statusState === "checking") return;
  const epoch = ++state.crm.statusEpoch;
  state.crm.statusState = "checking";
  state.crm.statusError = "";
  state.crm.statusDetail = "";
  renderCrm();
  updateViewHeader();

  try {
    const data = await apiRequest("/crm/status", { timeout: 8000 });
    if (epoch !== state.crm.statusEpoch) return;
    const status = normalizeCrmStatus(data);
    state.crm.connected = status.connected;
    state.crm.mode = status.mode;
    state.crm.hint = status.hint || (status.connected ? "" : CRM_FALLBACK_HINT);
    state.crm.url = status.url;
    state.crm.statusDetail = status.error;
    state.crm.statusState = status.connected ? "ready" : "offline";
    if (!status.connected) {
      // A fresh offline check invalidates any order text read earlier.
      state.crm.readEpoch += 1;
      state.crm.readState = "idle";
      state.crm.order = "";
      state.crm.readError = "";
      state.crm.readHint = "";
      state.crm.copyStatus = "";
      resetCrmActionFeedback();
    }
  } catch (error) {
    if (epoch !== state.crm.statusEpoch) return;
    state.crm.connected = false;
    state.crm.url = "";
    state.crm.hint = CRM_FALLBACK_HINT;
    state.crm.statusDetail = "";
    state.crm.readEpoch += 1;
    state.crm.readState = "idle";
    state.crm.order = "";
    state.crm.readError = "";
    state.crm.readHint = "";
    state.crm.copyStatus = "";
    resetCrmActionFeedback();
    if (isUnsupported(error)) {
      state.crm.statusState = "unavailable";
      state.crm.statusError =
        error.status === 503
          ? "CRM не настроен на сервере (503). Запустите Kivi с поддержкой CRM."
          : "Сервер не поддерживает GET /crm/status (404).";
    } else {
      state.crm.statusState = "error";
      state.crm.statusError = describeCrmError(error, "проверке подключения CRM");
      if (force) showToast(state.crm.statusError, "error");
    }
  } finally {
    if (epoch !== state.crm.statusEpoch) return;
    renderCrm();
    updateViewHeader();
  }
  void loadCrmStats();
}

async function readCrmOrder() {
  if (state.crm.readState === "loading") return;
  if (!state.crm.connected) {
    await loadCrmStatus({ force: true });
    return;
  }

  const epoch = ++state.crm.readEpoch;
  state.crm.readState = "loading";
  state.crm.order = "";
  state.crm.readError = "";
  state.crm.readHint = "";
  state.crm.copyStatus = "";
  renderCrm();
  updateViewHeader();

  try {
    const data = await apiRequest("/crm/read", { timeout: 45000 });
    if (epoch !== state.crm.readEpoch) return;
    const order =
      data && typeof data.order === "string" ? data.order.slice(0, CRM_ORDER_MAX_CHARS) : "";
    const hint =
      data && typeof data.hint === "string" ? data.hint.trim().slice(0, 800) : "";
    if (data && data.ok === true) {
      state.crm.order = order;
      state.crm.readState = order.trim() ? "ready" : "empty";
    } else {
      state.crm.readState = "blocked";
      state.crm.readHint =
        hint || "AltCRM недоступен: откройте вкладку заказа и проверьте подключение.";
    }
  } catch (error) {
    if (epoch !== state.crm.readEpoch) return;
    state.crm.readState = "error";
    state.crm.readError = describeCrmError(error, "чтении заказа");
  } finally {
    if (epoch !== state.crm.readEpoch) return;
    renderCrm();
    updateViewHeader();
  }
  void loadCrmStats();
}

async function copyCrmOrder() {
  if (state.crm.readState !== "ready" || !state.crm.order) return;
  if (els.crmCopy) els.crmCopy.disabled = true;
  try {
    await copyTextToClipboard(state.crm.order);
    state.crm.copyStatus = "Скопировано в буфер обмена.";
  } catch {
    state.crm.copyStatus = "Не удалось скопировать. Выделите текст и нажмите Ctrl+C.";
    showToast("Буфер обмена недоступен в этом браузере.", "error");
  } finally {
    renderCrm();
  }
}

function renderCrm() {
  if (!els.crmStatusBadge || !els.crmStatus) return;
  const crm = state.crm;

  const badgeMap = {
    idle: { label: "не проверено", cls: "is-idle" },
    checking: { label: "проверка…", cls: "is-checking" },
    ready: { label: "подключено", cls: "is-online" },
    offline: { label: "офлайн", cls: "is-offline" },
    error: { label: "ошибка", cls: "is-error" },
    unavailable: { label: "недоступно", cls: "is-error" },
  };
  const badge = badgeMap[crm.statusState] || badgeMap.idle;
  els.crmStatusBadge.classList.remove(
    "is-idle",
    "is-checking",
    "is-online",
    "is-offline",
    "is-error",
  );
  els.crmStatusBadge.classList.add(badge.cls);
  if (els.crmStatusBadgeText) els.crmStatusBadgeText.textContent = badge.label;

  const extensionMode = crm.mode === "extension";
  let statusText = extensionMode
    ? "Откройте раздел «CRM», чтобы проверить расширение AltCRM."
    : "Откройте раздел «CRM», чтобы проверить подключение к Chrome.";
  let statusIsError = false;
  if (crm.statusState === "checking") {
    statusText = extensionMode
      ? "Проверяю подключение расширения AltCRM…"
      : "Проверяю подключение к Chrome по CDP…";
  } else if (crm.statusState === "ready") {
    statusText = extensionMode
      ? "Расширение AltCRM на связи. Откройте карточку заказа и нажмите «Прочитать заказ»."
      : "Chrome на связи. Откройте карточку заказа и нажмите «Прочитать заказ».";
  } else if (crm.statusState === "offline") {
    statusText = extensionMode
      ? "Расширение AltCRM не подключено. Включите его в Яндекс.Браузере и нажмите «Проверить»."
      : "Chrome с CDP не найден. Выполните инструкцию ниже, затем нажмите «Проверить».";
  } else if (crm.statusState === "error" || crm.statusState === "unavailable") {
    statusText = crm.statusError || "Не удалось проверить подключение к CRM.";
    statusIsError = true;
  }
  els.crmStatus.textContent = statusText;
  els.crmStatus.classList.toggle("is-error", statusIsError);
  const statsEl = document.getElementById("crm-stats");
  if (statsEl) {
    const stats = crm.stats || { total: 0, unique: 0 };
    statsEl.textContent = stats.unique
      ? `Прочитано: ${stats.total} · уникальных заказов: ${stats.unique} / 10`
      : "Локальный журнал чтений пуст.";
  }

  if (els.crmHint) els.crmHint.hidden = crm.statusState !== "offline";
  if (els.crmHintText) els.crmHintText.textContent = crm.hint || CRM_FALLBACK_HINT;
  if (els.crmHintError) {
    const detail = crm.statusState === "offline" ? crm.statusDetail : "";
    els.crmHintError.hidden = !detail;
    els.crmHintError.textContent = detail ? `Техническая причина: ${detail}` : "";
  }
  if (els.crmEndpointRow) {
    const open = crm.statusState === "ready" && Boolean(crm.url);
    els.crmEndpointRow.hidden = !open;
    if (els.crmEndpoint) els.crmEndpoint.textContent = open ? crm.url : "";
  }

  const checking = crm.statusState === "checking";
  if (els.crmCheck) {
    els.crmCheck.disabled = checking;
    els.crmCheck.textContent = checking ? "Проверяю…" : "Проверить";
  }
  if (els.crmRead) {
    els.crmRead.disabled = checking || !crm.connected || crm.readState === "loading";
  }

  const readState = crm.readState;
  const loading = readState === "loading";
  const hasOrder = readState === "ready" && Boolean(crm.order);
  if (els.crmOrderCard) els.crmOrderCard.setAttribute("aria-busy", loading ? "true" : "false");
  if (els.crmOrderLoading) els.crmOrderLoading.hidden = !loading;
  if (els.crmOrderError) {
    els.crmOrderError.hidden = readState !== "error";
    els.crmOrderError.textContent = readState === "error" ? crm.readError : "";
  }
  if (els.crmOrderText) {
    els.crmOrderText.hidden = !hasOrder;
    els.crmOrderText.textContent = hasOrder ? crm.order : "";
  }
  if (els.crmOrderEmpty) {
    let emptyText = "";
    if (!loading && !hasOrder && readState !== "error") {
      if (readState === "empty") {
        emptyText = extensionMode
          ? "Сервер вернул пустой текст заказа. Откройте карточку в Яндекс.Браузере."
          : "Сервер вернул пустой текст заказа. Откройте карточку во вкладке Chrome.";
      } else if (readState === "blocked") {
        emptyText = crm.readHint || "AltCRM недоступен: откройте вкладку заказа.";
      } else if (crm.connected) {
        emptyText = "Карточка ещё не прочитана. Нажмите «Прочитать заказ».";
      } else {
        emptyText = extensionMode
          ? "Включите расширение AltCRM и откройте карточку заказа. Действия ниже только заполняют поля."
          : "Сначала подключитесь к Chrome, затем нажмите «Прочитать заказ». Действия ниже только заполняют поля.";
      }
    }
    els.crmOrderEmpty.hidden = !emptyText;
    els.crmOrderEmpty.textContent = emptyText;
  }

  if (els.crmOrderStatus) {
    let orderStatus = "";
    if (loading) {
      orderStatus = "Читаю карточку заказа… Это может занять несколько секунд.";
    } else if (readState === "ready") {
      orderStatus = `${numberFmt.format(crm.order.length)} ${plural(crm.order.length, [
        "символ",
        "символа",
        "символов",
      ])} · только чтение`;
    } else if (readState === "empty") {
      orderStatus = "Пустой ответ сервера.";
    } else if (readState === "blocked") {
      orderStatus = "Заказ не прочитан: нужна открытая карточка AltCRM.";
    } else if (readState === "error") {
      orderStatus = "Не удалось прочитать заказ.";
    } else if (crm.connected) {
      orderStatus = "Готово к чтению открытой вкладки.";
    } else {
      orderStatus = extensionMode
        ? "Ожидаю подключения расширения AltCRM."
        : "Ожидаю подключения к Chrome.";
    }
    els.crmOrderStatus.textContent = orderStatus;
    els.crmOrderStatus.classList.toggle("is-error", readState === "error");
  }

  if (els.crmCopy) els.crmCopy.disabled = !hasOrder;
  if (els.crmCopyStatus) els.crmCopyStatus.textContent = crm.copyStatus;
  renderCrmActions();
}

/* ------------------------------------------------------------------ */
/* CRM actions (POST /crm/fill, /crm/act, /crm/task)                  */
/*                                                                    */
/* Prepare-only by design: every call fills fields on the open card,  */
/* never saves, sends or creates anything, and every submit needs the */
/* explicit human confirmation checkbox.                             */
/* ------------------------------------------------------------------ */

function crmReady() {
  return state.crm.statusState === "ready" && state.crm.connected;
}

function makeCrmActRow() {
  return { id: makeId("actrow"), name: "", price: "", warranty: "" };
}

function crmActRemoveIcon() {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", "M6 6l12 12M18 6 6 18");
  svg.append(path);
  return svg;
}

function resetCrmActionFeedback() {
  for (const key of CRM_TABS) {
    const action = state.crm[key];
    action.epoch += 1;
    action.busy = false;
    action.confirm = false;
    action.error = "";
    action.validation = "";
    action.results = [];
    action.status = "";
    action.statusKind = "";
  }
  if (els.crmFillConfirm) els.crmFillConfirm.checked = false;
  if (els.crmActConfirm) els.crmActConfirm.checked = false;
  if (els.crmTaskConfirm) els.crmTaskConfirm.checked = false;
}

function parseCrmInt(raw, max, label) {
  const text = String(raw ?? "")
    .trim()
    .replace(/[\s\u00a0]+/g, "");
  if (!text) return { ok: true, value: 0 };
  if (!/^\d+$/.test(text)) return { ok: false, error: `${label}: введите целое число.` };
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < 0 || value > max) {
    return {
      ok: false,
      error: `${label}: допустимо от 0 до ${numberFmt.format(max)}.`,
    };
  }
  return { ok: true, value };
}

function normalizeCrmResults(raw) {
  if (!Array.isArray(raw)) return [];
  return raw.slice(0, CRM_LIMITS.results).map((item, index) => {
    const data = item && typeof item === "object" && !Array.isArray(item) ? item : {};
    const rawField =
      typeof data.field === "string" && data.field.trim()
        ? data.field.trim().slice(0, 200)
        : "";
    const field = rawField
      ? CRM_FIELD_LABELS[rawField] || rawField
      : `Результат ${index + 1}`;
    return {
      field,
      ok: data.ok === true,
      error: typeof data.error === "string" ? data.error.trim().slice(0, 400) : "",
      note: typeof data.note === "string" ? data.note.trim().slice(0, 400) : "",
    };
  });
}

function renderCrmResultsList(list, results) {
  if (!list) return;
  const signature = JSON.stringify(results);
  if (list.dataset.crmResults === signature) {
    list.hidden = results.length === 0;
    return;
  }
  list.dataset.crmResults = signature;
  list.replaceChildren();
  if (!results.length) {
    list.hidden = true;
    return;
  }
  for (const item of results) {
    const row = el("li", { class: `crm-result${item.ok ? " is-ok" : " is-error"}` });
    const head = el("div", { class: "crm-result-head" });
    head.append(el("span", { class: "crm-result-field", text: item.field }));
    head.append(
      el("span", {
        class: "crm-result-state",
        text: item.ok ? "заполнено" : "ошибка",
      }),
    );
    row.append(head);
    const detail = item.ok ? item.note : item.error || item.note;
    if (detail) row.append(el("p", { class: "crm-result-note", text: detail }));
    list.append(row);
  }
  list.hidden = false;
}

function setCrmFormFeedback(action, errorEl, statusEl, resultsEl) {
  if (errorEl) {
    const text = action.error || action.validation;
    if (errorEl.textContent !== text) errorEl.textContent = text;
    errorEl.hidden = !text;
  }
  if (statusEl) {
    if (statusEl.textContent !== action.status) statusEl.textContent = action.status;
    statusEl.classList.toggle("is-ok", action.statusKind === "ok");
    statusEl.classList.toggle("is-partial", action.statusKind === "partial");
  }
  renderCrmResultsList(resultsEl, action.results);
}

function crmFillHasInput() {
  const fill = state.crm.fill;
  return Boolean(
    fill.comment.trim() ||
      CRM_CHECKLIST_FIELDS.some(([key]) => fill[key].trim().length > 0),
  );
}

function crmActReady() {
  const act = state.crm.act;
  if (!crmReady() || act.busy || !act.confirm || !act.rows.length) return false;
  return act.rows.every((row) => row.name.trim().length > 0);
}

function renderCrmTabs() {
  const entries = [
    ["fill", els.crmTabFill, els.crmFillPanel],
    ["act", els.crmTabAct, els.crmActPanel],
    ["task", els.crmTabTask, els.crmTaskPanel],
  ];
  for (const [key, tab, panel] of entries) {
    if (!tab) continue;
    const active = state.crm.tab === key;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", active ? "true" : "false");
    tab.tabIndex = active ? 0 : -1;
    if (panel) panel.hidden = !active;
  }
}

function setCrmTab(name, { focus = false } = {}) {
  if (!CRM_TABS.includes(name)) return;
  state.crm.tab = name;
  renderCrmActions();
  if (!focus) return;
  const tabs = { fill: els.crmTabFill, act: els.crmTabAct, task: els.crmTabTask };
  const tab = tabs[name];
  if (tab) tab.focus();
}

function onCrmTabKeydown(event) {
  const current = CRM_TABS.indexOf(state.crm.tab);
  if (current < 0) return;
  let next = "";
  if (event.key === "ArrowRight") next = CRM_TABS[(current + 1) % CRM_TABS.length];
  else if (event.key === "ArrowLeft") {
    next = CRM_TABS[(current + CRM_TABS.length - 1) % CRM_TABS.length];
  } else if (event.key === "Home") next = CRM_TABS[0];
  else if (event.key === "End") next = CRM_TABS[CRM_TABS.length - 1];
  if (!next) return;
  event.preventDefault();
  setCrmTab(next, { focus: true });
}

function renderCrmActRows() {
  if (!els.crmActRows) return;
  els.crmActRows.replaceChildren();
  state.crm.act.rows.forEach((row, index) => {
    const wrap = el("div", { class: "crm-act-row", dataset: { rowId: row.id } });
    wrap.append(
      renderCrmActField(row, "name", "Работа", index + 1, {
        type: "text",
        placeholder: "например, замена экрана",
        maxlength: CRM_LIMITS.rowNameChars,
        class: "crm-act-field-name",
      }),
      renderCrmActField(row, "price", "Цена, ₽", index + 1, {
        type: "number",
        inputmode: "numeric",
        min: 0,
        max: CRM_LIMITS.priceMax,
        step: 1,
        placeholder: "0",
        class: "crm-act-field-price",
      }),
      renderCrmActField(row, "warranty", "Гарантия, дней", index + 1, {
        type: "number",
        inputmode: "numeric",
        min: 0,
        max: CRM_LIMITS.warrantyMax,
        step: 1,
        placeholder: "0",
        class: "crm-act-field-warranty",
      }),
    );
    const remove = el("button", {
      type: "button",
      class: "icon-btn crm-act-remove",
      dataset: { actRemove: row.id },
      "aria-label": `Удалить строку ${index + 1}`,
      title: "Удалить строку",
    });
    const removeIcon = crmActRemoveIcon();
    remove.append(removeIcon);
    wrap.append(remove);
    els.crmActRows.append(wrap);
  });
}

function renderCrmActField(row, field, label, number, opts) {
  const input = el("input", {
    class: "crm-input",
    type: opts.type,
    inputmode: opts.inputmode || null,
    min: opts.min ?? null,
    max: opts.max ?? null,
    step: opts.step ?? null,
    maxlength: opts.maxlength ?? null,
    placeholder: opts.placeholder || null,
    autocomplete: "off",
    spellcheck: "false",
    value: row[field],
    dataset: { actField: field },
    "aria-label": `${label}, строка ${number}`,
  });
  const wrap = el("label", { class: `field ${opts.class || ""}`.trim() });
  wrap.append(el("span", { class: "field-label", text: label }), input);
  return wrap;
}

function renderCrmActions() {
  if (!els.crmToolsCard) return;
  const crm = state.crm;
  const connected = crmReady();
  const busy = crm.fill.busy || crm.act.busy || crm.task.busy;
  els.crmToolsCard.setAttribute("aria-busy", busy ? "true" : "false");

  if (els.crmToolsGate) {
    let text = "Действия недоступны: подключите расширение AltCRM и нажмите «Проверить».";
    let cls = "is-off";
    if (busy) {
      text = "Выполняю действие на открытой карточке…";
      cls = "is-checking";
    } else if (connected) {
      text = "Подключено. Kivi только заполнит поля — сохранение и создание остаются за вами.";
      cls = "is-ready";
    } else if (crm.statusState === "checking") {
      text = "Проверяю подключение к AltCRM…";
      cls = "is-checking";
    }
    if (els.crmToolsGate.textContent !== text) els.crmToolsGate.textContent = text;
    els.crmToolsGate.classList.remove("is-off", "is-ready", "is-checking");
    els.crmToolsGate.classList.add(cls);
  }

  renderCrmTabs();

  const fill = crm.fill;
  if (els.crmFillForm) {
    els.crmFillForm.setAttribute("aria-busy", fill.busy ? "true" : "false");
  }
  if (els.crmFillFieldset) els.crmFillFieldset.disabled = !connected || fill.busy;
  if (els.crmFillSubmit) {
    els.crmFillSubmit.disabled =
      !connected || fill.busy || !fill.confirm || !crmFillHasInput();
    els.crmFillSubmit.textContent = fill.busy
      ? "Заполняю…"
      : "Заполнить без сохранения";
    els.crmFillSubmit.setAttribute("aria-busy", fill.busy ? "true" : "false");
  }
  setCrmFormFeedback(fill, els.crmFillError, els.crmFillStatus, els.crmFillResults);

  const act = crm.act;
  if (els.crmActForm) {
    els.crmActForm.setAttribute("aria-busy", act.busy ? "true" : "false");
  }
  if (els.crmActFieldset) els.crmActFieldset.disabled = !connected || act.busy;
  if (els.crmActSubmit) {
    els.crmActSubmit.disabled = !crmActReady();
    els.crmActSubmit.textContent = act.busy
      ? "Добавляю…"
      : "Добавить строки без сохранения";
    els.crmActSubmit.setAttribute("aria-busy", act.busy ? "true" : "false");
  }
  if (els.crmActAdd) {
    const atLimit = act.rows.length >= CRM_LIMITS.rows;
    els.crmActAdd.disabled = !connected || act.busy || atLimit;
    els.crmActAdd.textContent = atLimit ? "Лимит строк" : "Добавить строку";
  }
  if (els.crmActRows) {
    const removeDisabled = !connected || act.busy || act.rows.length <= 1;
    els.crmActRows.querySelectorAll("[data-act-remove]").forEach((button) => {
      button.disabled = removeDisabled;
    });
  }
  setCrmFormFeedback(act, els.crmActError, els.crmActStatus, els.crmActResults);

  const task = crm.task;
  if (els.crmTaskForm) {
    els.crmTaskForm.setAttribute("aria-busy", task.busy ? "true" : "false");
  }
  if (els.crmTaskFieldset) els.crmTaskFieldset.disabled = !connected || task.busy;
  if (els.crmTaskSubmit) {
    els.crmTaskSubmit.disabled =
      !connected || task.busy || !task.confirm || !task.comment.trim();
    els.crmTaskSubmit.textContent = task.busy ? "Готовлю…" : "Подготовить задачу";
    els.crmTaskSubmit.setAttribute("aria-busy", task.busy ? "true" : "false");
  }
  setCrmFormFeedback(task, els.crmTaskError, els.crmTaskStatus, els.crmTaskResults);
}

function syncCrmFillFields() {
  const fill = state.crm.fill;
  if (els.crmFillComment) {
    fill.comment = els.crmFillComment.value.slice(0, CRM_LIMITS.commentChars);
  }
  if (els.crmFillDeadline) {
    fill.deadline = els.crmFillDeadline.value.slice(0, CRM_LIMITS.checklistChars);
  }
  if (els.crmFillParts) {
    fill.parts = els.crmFillParts.value.slice(0, CRM_LIMITS.checklistChars);
  }
  if (els.crmFillWorks) {
    fill.works = els.crmFillWorks.value.slice(0, CRM_LIMITS.checklistChars);
  }
  if (els.crmFillRisks) {
    fill.risks = els.crmFillRisks.value.slice(0, CRM_LIMITS.checklistChars);
  }
}

function syncCrmTaskFields() {
  const task = state.crm.task;
  if (els.crmTaskComment) {
    task.comment = els.crmTaskComment.value.slice(0, CRM_LIMITS.taskCommentChars);
  }
  if (els.crmTaskWhen) {
    task.when = els.crmTaskWhen.value.slice(0, CRM_LIMITS.whenChars);
  }
}

function onCrmFillInput() {
  syncCrmFillFields();
  if (state.crm.fill.validation) state.crm.fill.validation = "";
  renderCrmActions();
}

function onCrmTaskInput() {
  syncCrmTaskFields();
  if (state.crm.task.validation) state.crm.task.validation = "";
  renderCrmActions();
}

function onCrmActInput(event) {
  const target = event.target;
  if (!(target instanceof Element)) return;
  const field = target.getAttribute("data-act-field");
  const rowEl = target.closest(".crm-act-row");
  if (!field || !rowEl || !CRM_ACT_FIELDS.includes(field)) return;
  const row = state.crm.act.rows.find((item) => item.id === rowEl.dataset.rowId);
  if (!row) return;
  row[field] = target.value;
  if (state.crm.act.validation) state.crm.act.validation = "";
  renderCrmActions();
}

function onCrmConfirmChange(kind, input) {
  const action = state.crm[kind];
  action.confirm = Boolean(input && input.checked);
  if (action.validation) action.validation = "";
  renderCrmActions();
}

function buildCrmFillPayload() {
  const fill = state.crm.fill;
  const comment = fill.comment.trim().slice(0, CRM_LIMITS.commentChars);
  const checklist = {};
  for (const [key] of CRM_CHECKLIST_FIELDS) {
    const value = fill[key].trim().slice(0, CRM_LIMITS.checklistChars);
    if (value) checklist[key] = value;
  }
  if (!comment && !Object.keys(checklist).length) return null;
  return { comment, checklist };
}

function buildCrmActPayload() {
  const act = state.crm.act;
  if (!act.rows.length) return { error: "Добавьте хотя бы одну строку акта." };
  if (act.rows.length > CRM_LIMITS.rows) {
    return { error: `Не больше ${CRM_LIMITS.rows} строк за один раз.` };
  }
  const rows = [];
  for (let index = 0; index < act.rows.length; index += 1) {
    const row = act.rows[index];
    const number = index + 1;
    const name = row.name.trim().slice(0, CRM_LIMITS.rowNameChars);
    if (!name) return { error: `Строка ${number}: укажите название работы.` };
    const price = parseCrmInt(row.price, CRM_LIMITS.priceMax, `Строка ${number}, цена`);
    if (!price.ok) return { error: price.error };
    const warranty = parseCrmInt(
      row.warranty,
      CRM_LIMITS.warrantyMax,
      `Строка ${number}, гарантия`,
    );
    if (!warranty.ok) return { error: warranty.error };
    rows.push({ name, price: price.value, warranty: warranty.value });
  }
  return { rows };
}

function buildCrmTaskPayload() {
  const task = state.crm.task;
  const comment = task.comment.trim().slice(0, CRM_LIMITS.taskCommentChars);
  if (!comment) return { error: "Введите комментарий задачи." };
  const when = task.when.replace(/\s+/g, " ").trim().slice(0, CRM_LIMITS.whenChars);
  return { comment, when_text: when };
}

function crmWriteErrorMessage(kind, detail) {
  const action =
    kind === "fill"
      ? "заполнить поля"
      : kind === "act"
        ? "добавить строки акта"
        : "подготовить задачу";
  const clean = String(detail || "")
    .trim()
    .replace(/[.\s]+$/, "")
    .slice(0, 300);
  return `Не удалось ${action}${clean ? `: ${clean}` : ""}. Проверьте, что карточка AltCRM открыта, и повторите.`;
}

function crmWriteSuccessMessage(kind, okCount, total) {
  if (kind === "fill") {
    return `Готово: заполнено ${okCount} из ${total}. Kivi ничего не сохранил — проверьте карточку и нажмите «Сохранить» вручную.`;
  }
  if (kind === "act") {
    return `Готово: строки добавлены (${okCount} из ${total}). Kivi ничего не сохранил — проверьте акт и нажмите «Сохранить» вручную.`;
  }
  return `Готово: поля задачи заполнены (${okCount} из ${total}). Kivi не нажимал «Создать задачу» — создайте её вручную.`;
}

function crmWritePartialMessage(kind, okCount, total) {
  const failed = total - okCount;
  const tail =
    kind === "task"
      ? "Kivi не создавал задачу — проверьте поля и создайте её вручную."
      : "Проверьте ошибки и карточку; сохранение остаётся за вами.";
  return `Выполнено ${okCount} из ${total}, ошибок: ${failed}. ${tail}`;
}

function applyCrmWriteResponse(kind, action, data) {
  const payload = data && typeof data === "object" && !Array.isArray(data) ? data : {};
  const results = normalizeCrmResults(payload.results);
  const ok = payload.ok === true;
  const message =
    typeof payload.message === "string" ? payload.message.trim().slice(0, 400) : "";
  action.results = results;
  action.error = "";
  action.status = "";
  action.statusKind = "";
  if (!ok) {
    const detail = firstString(payload.error, message);
    action.error = crmWriteErrorMessage(kind, detail);
    if (!results.length) return;
  }
  const total = results.length;
  const okCount = results.filter((item) => item.ok).length;
  if (!total) {
    if (ok && message) {
      action.status = message;
      action.statusKind = "ok";
    }
    return;
  }
  if (ok && okCount === total) {
    action.status = crmWriteSuccessMessage(kind, okCount, total);
    action.statusKind = "ok";
  } else {
    action.status = crmWritePartialMessage(kind, okCount, total);
    action.statusKind = "partial";
  }
}

async function submitCrmFill(event) {
  if (event) event.preventDefault();
  const fill = state.crm.fill;
  if (fill.busy) return;
  fill.validation = "";
  fill.error = "";
  if (!crmReady()) {
    fill.validation = "CRM не подключён. Включите расширение AltCRM и нажмите «Проверить».";
    renderCrmActions();
    return;
  }
  const payload = buildCrmFillPayload();
  if (!payload) {
    fill.validation = "Заполните комментарий или хотя бы одно поле чек-листа.";
    renderCrmActions();
    if (els.crmFillComment) els.crmFillComment.focus();
    return;
  }
  if (!fill.confirm) {
    fill.validation = "Подтвердите, что «Сохранить» нажмёте вы сами.";
    renderCrmActions();
    return;
  }
  const epoch = ++fill.epoch;
  fill.results = [];
  fill.status = "";
  fill.statusKind = "";
  fill.busy = true;
  renderCrmActions();
  try {
    const data = await apiRequest("/crm/fill", {
      method: "POST",
      body: payload,
      timeout: 60000,
    });
    if (epoch !== fill.epoch) return;
    applyCrmWriteResponse("fill", fill, data);
  } catch (error) {
    if (epoch !== fill.epoch) return;
    fill.error = describeCrmError(error, "заполнении полей карточки");
  } finally {
    if (epoch === fill.epoch) {
      fill.busy = false;
      fill.confirm = false;
      if (els.crmFillConfirm) els.crmFillConfirm.checked = false;
      renderCrmActions();
    }
  }
}

async function submitCrmAct(event) {
  if (event) event.preventDefault();
  const act = state.crm.act;
  if (act.busy) return;
  act.validation = "";
  act.error = "";
  if (!crmReady()) {
    act.validation = "CRM не подключён. Включите расширение AltCRM и нажмите «Проверить».";
    renderCrmActions();
    return;
  }
  const payload = buildCrmActPayload();
  if (payload.error) {
    act.validation = payload.error;
    renderCrmActions();
    return;
  }
  if (!act.confirm) {
    act.validation = "Подтвердите, что «Сохранить» в акте нажмёте вы сами.";
    renderCrmActions();
    return;
  }
  const epoch = ++act.epoch;
  act.results = [];
  act.status = "";
  act.statusKind = "";
  act.busy = true;
  renderCrmActions();
  try {
    const data = await apiRequest("/crm/act", {
      method: "POST",
      body: { rows: payload.rows },
      timeout: 60000,
    });
    if (epoch !== act.epoch) return;
    applyCrmWriteResponse("act", act, data);
  } catch (error) {
    if (epoch !== act.epoch) return;
    act.error = describeCrmError(error, "добавлении строк акта");
  } finally {
    if (epoch === act.epoch) {
      act.busy = false;
      act.confirm = false;
      if (els.crmActConfirm) els.crmActConfirm.checked = false;
      renderCrmActions();
    }
  }
}

async function submitCrmTask(event) {
  if (event) event.preventDefault();
  const task = state.crm.task;
  if (task.busy) return;
  task.validation = "";
  task.error = "";
  if (!crmReady()) {
    task.validation = "CRM не подключён. Включите расширение AltCRM и нажмите «Проверить».";
    renderCrmActions();
    return;
  }
  const payload = buildCrmTaskPayload();
  if (payload.error) {
    task.validation = payload.error;
    renderCrmActions();
    if (els.crmTaskComment) els.crmTaskComment.focus();
    return;
  }
  if (!task.confirm) {
    task.validation = "Подтвердите, что задачу создадите вы сами.";
    renderCrmActions();
    return;
  }
  const epoch = ++task.epoch;
  task.results = [];
  task.status = "";
  task.statusKind = "";
  task.busy = true;
  renderCrmActions();
  try {
    const data = await apiRequest("/crm/task", {
      method: "POST",
      body: payload,
      timeout: 60000,
    });
    if (epoch !== task.epoch) return;
    applyCrmWriteResponse("task", task, data);
  } catch (error) {
    if (epoch !== task.epoch) return;
    task.error = describeCrmError(error, "подготовке задачи");
  } finally {
    if (epoch === task.epoch) {
      task.busy = false;
      task.confirm = false;
      if (els.crmTaskConfirm) els.crmTaskConfirm.checked = false;
      renderCrmActions();
    }
  }
}

function addCrmActRow() {
  const act = state.crm.act;
  if (!crmReady() || act.busy || act.rows.length >= CRM_LIMITS.rows) return;
  act.rows.push(makeCrmActRow());
  act.validation = "";
  renderCrmActRows();
  renderCrmActions();
  const rowEls = els.crmActRows ? els.crmActRows.querySelectorAll("[data-row-id]") : [];
  const last = rowEls[rowEls.length - 1];
  const input = last ? last.querySelector('[data-act-field="name"]') : null;
  if (input) input.focus();
}

function removeCrmActRow(id) {
  const act = state.crm.act;
  if (!crmReady() || act.busy || act.rows.length <= 1) return;
  const index = act.rows.findIndex((row) => row.id === id);
  if (index < 0) return;
  act.rows.splice(index, 1);
  act.validation = "";
  renderCrmActRows();
  renderCrmActions();
}

/* ------------------------------------------------------------------ */
/* Media (POST /media/image, POST /media/video)                       */
/* ------------------------------------------------------------------ */

function mediaSize(raw, fallback = 1024) {
  const value = Number(raw);
  return MEDIA_LIMITS.sizes.includes(value) ? value : fallback;
}

function mediaVideoSize(raw, fallback = 512) {
  const value = Number(raw);
  if (MEDIA_VIDEO.sizes.includes(value)) return value;
  // The video endpoint accepts 512 or 768 only; the image form may still hold 1024.
  if (Number.isFinite(value) && value > 768) return 768;
  return fallback;
}

function mediaVideoDuration(raw, fallback = 4) {
  const value = Number(raw);
  return MEDIA_VIDEO.durations.includes(value) ? value : fallback;
}

function mediaVideoSeconds(raw, fallback = 4) {
  const value = Number(raw);
  return Number.isInteger(value) &&
    value >= MEDIA_VIDEO.minSeconds &&
    value <= MEDIA_VIDEO.maxSeconds
    ? value
    : fallback;
}

function mediaModel(raw, fallback = "flux") {
  const value = typeof raw === "string" ? raw.trim().toLowerCase() : "";
  return MEDIA_LIMITS.models.includes(value) ? value : fallback;
}

function mediaModelLabel(model) {
  return model === "turbo" ? "turbo" : "flux";
}

function isImageDataUrl(value) {
  return typeof value === "string" && /^data:image\/[a-z0-9.+-]+;base64,/i.test(value);
}

function isVideoDataUrl(value) {
  return typeof value === "string" && /^data:video\/[a-z0-9.+-]+;base64,/i.test(value);
}

function mediaFileName(item) {
  const match = /^data:image\/([a-z0-9.+-]+)[;,]/i.exec(item && item.image ? item.image : "");
  let ext = match ? match[1].toLowerCase() : "png";
  if (ext === "jpeg") ext = "jpg";
  else if (ext === "svg+xml") ext = "svg";
  const stamp = new Date(item && item.ts ? item.ts : Date.now())
    .toISOString()
    .replace(/[:.]/g, "-")
    .slice(0, 19);
  return `kivi-media-${stamp}.${ext}`;
}

function describeMediaError(error) {
  if (!(error instanceof ApiError)) {
    return error instanceof Error
      ? `Не удалось сгенерировать изображение: ${error.message}.`
      : "Не удалось сгенерировать изображение.";
  }
  if (error.code === "bad-image") {
    return "Сервер ответил без изображения. Проверьте, что POST /media/image возвращает поле image с data URL.";
  }
  if (error.code === "network") {
    return "Нет связи с локальным сервером Kivi. Проверьте, что запущен kivi serve, и откройте интерфейс с того же адреса (CORS не включён).";
  }
  if (error.code === "timeout") {
    return "Сервер не ответил вовремя: генерация заняла слишком много времени. Попробуйте размер 512 и модель turbo.";
  }
  if (error.status === 400) {
    return `Сервер отклонил запрос${error.code ? `: ${error.code}` : ""}. Проверьте описание и размеры изображения.`;
  }
  if (error.status === 404) return "Сервер не поддерживает POST /media/image (404). Обновите Kivi.";
  if (error.status === 405) return "Метод POST /media/image не поддерживается сервером (405).";
  if (error.status === 413) return "Описание слишком длинное для локального API.";
  if (error.status === 503) {
    return `Генерация изображений не настроена на сервере (503${error.code ? `: ${error.code}` : ""}). Проверьте настройки media в kivi.`;
  }
  if (error.status >= 500) {
    return `Сервер не смог сгенерировать изображение (${error.status}). Попробуйте ещё раз или уменьшите размер.`;
  }
  return error.message || "Не удалось сгенерировать изображение.";
}

function describeMediaVideoError(error) {
  if (!(error instanceof ApiError)) {
    return error instanceof Error
      ? `Не удалось сгенерировать видео: ${error.message}.`
      : "Не удалось сгенерировать видео.";
  }
  if (error.code === "bad-video") {
    return "Сервер ответил без видео. Проверьте, что POST /media/video возвращает поле video с data URL.";
  }
  if (error.code === "network") {
    return "Нет связи с локальным сервером Kivi. Проверьте, что запущен kivi serve, и откройте интерфейс с того же адреса (CORS не включён).";
  }
  if (error.code === "timeout") {
    return "Сервер не ответил вовремя: сборка видео заняла слишком много времени. Попробуйте 512 px и 2 секунды.";
  }
  if (error.status === 400) {
    return `Сервер отклонил параметры видео${error.code ? `: ${error.code}` : ""}. Проверьте описание, размер (512 или 768) и длительность 2–6 секунд.`;
  }
  if (error.status === 404) return "Сервер не поддерживает POST /media/video (404). Обновите Kivi.";
  if (error.status === 405) return "Метод POST /media/video не поддерживается сервером (405).";
  if (error.status === 413) return "Описание слишком длинное для локального API.";
  if (error.status === 503) {
    return `Локальная генерация видео не настроена на сервере (503${error.code ? `: ${error.code}` : ""}). Перезапустите kivi serve — обработчик media.video не подключён.`;
  }
  if (error.status >= 500) {
    return `Сервер не смог собрать видео (${error.status}${error.code ? `: ${error.code}` : ""}). Частая причина — нет ffmpeg: установите imageio-ffmpeg и повторите.`;
  }
  return error.message || "Не удалось сгенерировать видео.";
}

function noteMediaNetworkFailure(message) {
  state.health = {
    status: "offline",
    version: state.health.version,
    checkedAt: Date.now(),
    error: message,
  };
  updateStatusUI();
}

function mediaStatusText() {
  const media = state.media;
  if (media.mode === "video") {
    const video = media.video;
    if (video.loading) return "Генерирую видео… Это может занять до минуты.";
    if (media.formError) return "Нужно описание.";
    if (video.error) return "Видео не получилось.";
    if (video.result) {
      const result = video.result;
      return `Готово: видео ${result.duration} с · ${result.width}×${result.height} · ${mediaModelLabel(result.model)}.`;
    }
    return "Опишите видео и нажмите «Сгенерировать видео».";
  }
  if (media.loading) return "Генерирую изображение… Это может занять до минуты.";
  if (media.formError) return "Нужно описание.";
  if (media.error) return "Генерация не удалась.";
  if (media.result) {
    const count = media.gallery.length;
    const suffix = count
      ? ` В этой сессии: ${count} ${plural(count, [
          "изображение",
          "изображения",
          "изображений",
        ])}.`
      : "";
    return `Готово: ${mediaModelLabel(media.result.model)} · ${media.result.width}×${media.result.height}.${suffix}`;
  }
  return "Опишите изображение и нажмите «Сгенерировать».";
}

function mediaVideoStatusText() {
  const video = state.media.video;
  if (video.loading) return "Генерирую видео… Это может занять до минуты.";
  if (video.error) return "Видео не получилось.";
  if (video.result) {
    const result = video.result;
    return `Готово: видео ${result.duration} с · ${result.width}×${result.height} · ${mediaModelLabel(result.model)}.`;
  }
  return "Видео появится здесь после генерации.";
}

function renderMediaPromptCount() {
  if (!els.mediaPrompt || !els.mediaPromptCount) return;
  const length = els.mediaPrompt.value.length;
  els.mediaPromptCount.textContent = `${length} / ${MEDIA_LIMITS.promptChars}`;
  els.mediaPromptCount.classList.toggle(
    "is-near-limit",
    length >= MEDIA_LIMITS.promptChars - 20,
  );
}

function renderMediaMode() {
  const isVideo = state.media.mode === "video";
  if (els.mediaModeImage) els.mediaModeImage.checked = !isVideo;
  if (els.mediaModeVideo) els.mediaModeVideo.checked = isVideo;
  if (els.mediaDuration) els.mediaDuration.disabled = !isVideo;
  if (els.mediaModeHint) {
    els.mediaModeHint.textContent = isVideo
      ? "Видео: 512 или 768 px, 2, 4 или 6 секунд. Собирается локально из одного кадра."
      : "Изображение: 512, 768 или 1024 px. Модель flux или turbo.";
  }
  for (const select of [els.mediaWidth, els.mediaHeight]) {
    if (!select) continue;
    const option = select.querySelector('option[value="1024"]');
    if (!option) continue;
    // 1024 is an image size only: keep the video request within the API contract.
    option.disabled = isVideo;
    if (isVideo && select.value === "1024") select.value = "768";
  }
}

function renderMediaVideo() {
  const video = state.media.video;
  const result = video.result;
  const hasVideo = Boolean(result && isVideoDataUrl(result.video));
  const visible =
    state.media.mode === "video" || video.loading || Boolean(result) || Boolean(video.error);
  if (els.mediaVideoCard) {
    els.mediaVideoCard.hidden = !visible;
    els.mediaVideoCard.setAttribute("aria-busy", video.loading ? "true" : "false");
  }
  if (els.mediaVideoLoading) els.mediaVideoLoading.hidden = !video.loading;
  if (els.mediaVideoEmpty) {
    els.mediaVideoEmpty.hidden = video.loading || hasVideo || Boolean(video.error);
  }
  if (els.mediaVideoPlayer) {
    els.mediaVideoPlayer.hidden = !hasVideo;
    if (hasVideo) {
      if (els.mediaVideoPlayer.getAttribute("src") !== result.video) {
        els.mediaVideoPlayer.src = result.video;
      }
      els.mediaVideoPlayer.setAttribute(
        "aria-label",
        `Видео по запросу: ${result.prompt} (${result.duration} с)`.slice(0, 320),
      );
    } else if (els.mediaVideoPlayer.hasAttribute("src")) {
      els.mediaVideoPlayer.removeAttribute("src");
      try {
        els.mediaVideoPlayer.load();
      } catch {
        /* the element may be detached in tests */
      }
    }
  }
  if (els.mediaVideoMeta) els.mediaVideoMeta.hidden = !hasVideo;
  if (els.mediaVideoDownload) els.mediaVideoDownload.hidden = !hasVideo;
  if (els.mediaVideoStatus) {
    els.mediaVideoStatus.textContent = mediaVideoStatusText();
    els.mediaVideoStatus.classList.toggle(
      "is-error",
      Boolean(video.error) && !video.loading,
    );
  }
  if (els.mediaVideoError) {
    els.mediaVideoError.textContent = video.error || "";
    els.mediaVideoError.hidden = !video.error || video.loading;
  }
  if (!hasVideo) {
    if (els.mediaVideoBadge) els.mediaVideoBadge.textContent = "—";
    return;
  }
  const modelLabel = mediaModelLabel(result.model);
  if (els.mediaVideoPrompt) els.mediaVideoPrompt.textContent = result.prompt;
  if (els.mediaVideoModel) els.mediaVideoModel.textContent = modelLabel;
  if (els.mediaVideoSize) {
    els.mediaVideoSize.textContent = `${result.width} × ${result.height}`;
  }
  if (els.mediaVideoDurationValue) {
    els.mediaVideoDurationValue.textContent = `${result.duration} с`;
  }
  if (els.mediaVideoBadge) els.mediaVideoBadge.textContent = `${modelLabel} · ${result.duration} с`;
  if (els.mediaVideoDownload) {
    els.mediaVideoDownload.href = result.video;
    els.mediaVideoDownload.setAttribute("download", "kivi-video.mp4");
    els.mediaVideoDownload.setAttribute("aria-label", "Скачать видео MP4 (kivi-video.mp4)");
  }
}

function renderMediaResult() {
  const media = state.media;
  const result = media.result;
  const hasImage = Boolean(result && isImageDataUrl(result.image));
  if (els.mediaResultImage) {
    els.mediaResultImage.hidden = !hasImage;
    if (hasImage) {
      if (els.mediaResultImage.getAttribute("src") !== result.image) {
        els.mediaResultImage.src = result.image;
      }
      els.mediaResultImage.alt = `Изображение по запросу: ${result.prompt}`.slice(0, 320);
    } else if (els.mediaResultImage.getAttribute("src")) {
      els.mediaResultImage.removeAttribute("src");
    }
  }
  if (els.mediaResultMeta) els.mediaResultMeta.hidden = !hasImage;
  if (els.mediaDownload) els.mediaDownload.hidden = !hasImage;
  if (!hasImage) {
    if (els.mediaResultBadge) els.mediaResultBadge.textContent = "—";
    return;
  }
  const modelLabel = mediaModelLabel(result.model);
  if (els.mediaResultPrompt) els.mediaResultPrompt.textContent = result.prompt;
  if (els.mediaResultModel) els.mediaResultModel.textContent = modelLabel;
  if (els.mediaResultSize) els.mediaResultSize.textContent = `${result.width} × ${result.height}`;
  if (els.mediaResultBadge) els.mediaResultBadge.textContent = modelLabel;
  if (els.mediaDownload) {
    const fileName = mediaFileName(result);
    els.mediaDownload.href = result.image;
    els.mediaDownload.setAttribute("download", fileName);
    els.mediaDownload.setAttribute("aria-label", `Скачать изображение (${fileName})`);
  }
}

function renderMediaGallery() {
  if (!els.mediaGalleryList) return;
  const gallery = state.media.gallery;
  if (els.mediaGalleryPanel) els.mediaGalleryPanel.hidden = gallery.length === 0;
  els.mediaGalleryList.replaceChildren();
  if (!gallery.length) return;
  const currentId = state.media.result ? state.media.result.id : "";
  for (const item of gallery) {
    const current = item.id === currentId;
    const button = el("button", {
      type: "button",
      class: `media-thumb${current ? " is-current" : ""}`,
      "aria-label": `Показать в результате: ${item.prompt.slice(0, 120)} (${mediaModelLabel(
        item.model,
      )}, ${item.width}×${item.height})`,
      "aria-current": current ? "true" : null,
      on: { click: () => showMediaResult(item) },
    });
    button.append(el("img", { src: item.image, alt: "", loading: "lazy" }));
    const meta = el("span", { class: "media-thumb-meta" });
    meta.append(
      el("span", { class: "media-thumb-model", text: mediaModelLabel(item.model) }),
      el("span", { text: `${item.width}×${item.height}` }),
    );
    button.append(meta);
    els.mediaGalleryList.append(el("li", { class: "media-thumb-item" }, button));
  }
}

function renderMedia() {
  if (!els.mediaForm) return;
  const media = state.media;
  const isVideo = media.mode === "video";
  const busy = isVideo ? media.video.loading : media.loading;
  els.mediaForm.setAttribute("aria-busy", busy ? "true" : "false");
  renderMediaMode();
  if (els.mediaSubmit) {
    els.mediaSubmit.disabled = busy;
    els.mediaSubmit.setAttribute(
      "aria-label",
      busy
        ? isVideo
          ? "Идёт генерация видео"
          : "Идёт генерация изображения"
        : isVideo
          ? "Сгенерировать видео"
          : "Сгенерировать изображение",
    );
  }
  if (els.mediaSubmitLabel) {
    els.mediaSubmitLabel.textContent = busy
      ? isVideo
        ? "Готовлю видео…"
        : "Генерирую…"
      : isVideo
        ? "Сгенерировать видео"
        : "Сгенерировать";
  }
  if (els.mediaStatus) {
    els.mediaStatus.textContent = mediaStatusText();
    const activeError = isVideo
      ? media.video.error || media.formError
      : media.error || media.formError;
    els.mediaStatus.classList.toggle("is-error", Boolean(activeError) && !busy);
  }
  if (els.mediaError) {
    // Request errors belong to their own mode; validation is shared by the form.
    const errorText = media.formError || (isVideo ? "" : media.error);
    els.mediaError.textContent = errorText;
    els.mediaError.hidden = !errorText || busy;
  }
  if (els.mediaResultCard) {
    els.mediaResultCard.setAttribute("aria-busy", media.loading ? "true" : "false");
  }
  if (els.mediaLoading) els.mediaLoading.hidden = !media.loading;
  if (els.mediaEmpty) els.mediaEmpty.hidden = media.loading || Boolean(media.result);
  renderMediaResult();
  renderMediaVideo();
  renderMediaGallery();
  if (state.view === "media") updateViewHeader();
}

function showMediaResult(item) {
  if (!item || !item.image) return;
  state.media.result = item;
  state.media.error = "";
  state.media.formError = "";
  renderMedia();
}

function setMediaMode(mode) {
  const next = mode === "video" ? "video" : "image";
  if (state.media.mode === next) return;
  state.media.mode = next;
  // Errors of the other mode should not follow the user into the new one.
  state.media.error = "";
  state.media.formError = "";
  state.media.video.error = "";
  renderMedia();
}

function clearMediaGallery() {
  if (!state.media.gallery.length) return;
  state.media.gallery = [];
  renderMedia();
  showToast("Галерея изображений этой сессии очищена. Текущие результаты остались.", "info");
}

function onMediaPromptInput() {
  renderMediaPromptCount();
  if (state.media.error || state.media.formError || state.media.video.error) {
    state.media.error = "";
    state.media.formError = "";
    state.media.video.error = "";
    renderMedia();
  }
}

function readMediaPrompt() {
  return String(els.mediaPrompt.value || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MEDIA_LIMITS.promptChars);
}

async function submitMedia() {
  if (!els.mediaPrompt) return;
  const mode = state.media.mode;
  const busy = mode === "video" ? state.media.video.loading : state.media.loading;
  if (busy) {
    showToast(
      mode === "video"
        ? "Видео ещё генерируется. Подождите."
        : "Изображение ещё генерируется. Подождите.",
      "info",
    );
    return;
  }
  const prompt = readMediaPrompt();
  if (!prompt) {
    state.media.formError =
      mode === "video"
        ? "Введите описание видео — без него генерация не начнётся."
        : "Введите описание изображения — без него генерация не начнётся.";
    renderMedia();
    els.mediaPrompt.focus();
    return;
  }
  if (mode === "video") {
    await submitMediaVideo(prompt);
    return;
  }
  await submitMediaImage(prompt);
}

async function submitMediaImage(prompt) {
  const width = mediaSize(els.mediaWidth ? els.mediaWidth.value : state.media.width);
  const height = mediaSize(els.mediaHeight ? els.mediaHeight.value : state.media.height);
  const model = mediaModel(els.mediaModel ? els.mediaModel.value : state.media.model);

  state.media.seq += 1;
  const seq = state.media.seq;
  state.media.prompt = prompt;
  state.media.width = width;
  state.media.height = height;
  state.media.model = model;
  state.media.loading = true;
  state.media.error = "";
  state.media.formError = "";
  renderMedia();
  setAvatar("thinking");

  try {
    const data = await apiRequest("/media/image", {
      method: "POST",
      body: { prompt, width, height, model },
      timeout: MEDIA_LIMITS.timeoutMs,
    });
    if (seq !== state.media.seq) return;
    const image = data && typeof data.image === "string" ? data.image.trim() : "";
    if (!isImageDataUrl(image)) {
      throw new ApiError("Сервер вернул ответ без изображения.", {
        path: "/media/image",
        code: "bad-image",
      });
    }
    const result = {
      id: makeId("img"),
      image,
      prompt: firstString(data.prompt, prompt).slice(0, MEDIA_LIMITS.promptChars),
      model: mediaModel(data.model, model),
      width,
      height,
      ts: Date.now(),
    };
    state.media.result = result;
    state.media.gallery = [result, ...state.media.gallery].slice(0, MEDIA_LIMITS.gallerySize);
    hideConnectionNotice();
    if (state.view !== "media") {
      showToast("Изображение готово. Откройте раздел «Медиа».", "ok");
    }
  } catch (error) {
    if (seq !== state.media.seq) return;
    state.media.error = describeMediaError(error);
    if (error instanceof ApiError && error.code === "network") {
      noteMediaNetworkFailure(state.media.error);
    }
    if (state.view !== "media") {
      showToast(`Медиа: ${state.media.error}`, "error");
    }
  } finally {
    if (seq === state.media.seq) {
      state.media.loading = false;
      renderMedia();
      // A disabled submit button loses focus; hand it back unless the user moved on.
      const active = document.activeElement;
      if (
        state.media.mode === "image" &&
        els.mediaSubmit &&
        (!active || active === document.body || active === els.mediaSubmit)
      ) {
        try {
          els.mediaSubmit.focus({ preventScroll: true });
        } catch {
          els.mediaSubmit.focus();
        }
      }
      idleIfQuiet();
    }
  }
}

async function submitMediaVideo(prompt) {
  const width = mediaVideoSize(els.mediaWidth ? els.mediaWidth.value : state.media.width);
  const height = mediaVideoSize(els.mediaHeight ? els.mediaHeight.value : state.media.height);
  const duration = mediaVideoDuration(
    els.mediaDuration ? els.mediaDuration.value : state.media.duration,
  );
  const model = mediaModel(els.mediaModel ? els.mediaModel.value : state.media.model);

  const video = state.media.video;
  video.seq += 1;
  const seq = video.seq;
  state.media.prompt = prompt;
  state.media.width = width;
  state.media.height = height;
  state.media.duration = duration;
  state.media.model = model;
  video.loading = true;
  video.error = "";
  state.media.error = "";
  state.media.formError = "";
  renderMedia();
  setAvatar("thinking");

  try {
    const data = await apiRequest("/media/video", {
      method: "POST",
      body: { prompt, width, height, duration, model },
      timeout: MEDIA_VIDEO.timeoutMs,
    });
    if (seq !== video.seq) return;
    const raw = data && typeof data.video === "string" ? data.video.trim() : "";
    if (!isVideoDataUrl(raw)) {
      throw new ApiError("Сервер вернул ответ без видео.", {
        path: "/media/video",
        code: "bad-video",
      });
    }
    const size = data && Array.isArray(data.size) ? data.size : [];
    const result = {
      id: makeId("vid"),
      video: raw,
      prompt: firstString(data && data.prompt, prompt).slice(0, MEDIA_LIMITS.promptChars),
      model: mediaModel(data && data.model, model),
      width: mediaVideoSize(size[0], width),
      height: mediaVideoSize(size[1], height),
      duration: mediaVideoSeconds(data && data.duration, duration),
      ts: Date.now(),
    };
    video.result = result;
    hideConnectionNotice();
    if (state.view !== "media") {
      showToast("Видео готово. Откройте раздел «Медиа».", "ok");
    }
  } catch (error) {
    if (seq !== video.seq) return;
    video.error = describeMediaVideoError(error);
    if (error instanceof ApiError && error.code === "network") {
      noteMediaNetworkFailure(video.error);
    }
    if (state.view !== "media") {
      showToast(`Видео: ${video.error}`, "error");
    }
  } finally {
    if (seq === video.seq) {
      video.loading = false;
      renderMedia();
      // A disabled submit button loses focus; hand it back unless the user moved on.
      const active = document.activeElement;
      if (
        state.media.mode === "video" &&
        els.mediaSubmit &&
        (!active || active === document.body || active === els.mediaSubmit)
      ) {
        try {
          els.mediaSubmit.focus({ preventScroll: true });
        } catch {
          els.mediaSubmit.focus();
        }
      }
      idleIfQuiet();
    }
  }
}

/* ------------------------------------------------------------------ */
/* Settings                                                           */
/* ------------------------------------------------------------------ */

function voiceProviderInfo(value) {
  return Object.prototype.hasOwnProperty.call(VOICE_PROVIDERS, value)
    ? VOICE_PROVIDERS[value]
    : null;
}

function voiceProviderLabel(value) {
  const info = voiceProviderInfo(value);
  return info ? info.label : value ? String(value) : "—";
}

function normalizeSettings(raw) {
  const data = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const rate = Number(data.sample_rate);
  return {
    provider: firstString(
      data.voice_provider,
      data.provider,
      data.voiceProvider,
    ).toLowerCase(),
    model: firstString(data.model, data.voice_model, data.voiceModel),
    speaker: firstString(data.speaker, data.voice_speaker, data.voiceSpeaker),
    fishModel: firstString(data.fish_model, data.fishModel),
    sampleRate: Number.isFinite(rate) && rate > 0 ? rate : 0,
  };
}

function isSettingsDirty() {
  if (!state.settings) return Boolean(state.settingsDraft);
  return Boolean(state.settingsDraft) && state.settingsDraft !== state.settings.provider;
}

function resetSettingsSaveState() {
  state.settingsSaveState = "idle";
  state.settingsSaveMessage = "";
}

async function loadSettings({ force = false } = {}) {
  if (state.settingsLoading || state.settingsSaving) return;
  state.settingsLoading = true;
  state.settingsState = "loading";
  state.settingsError = "";
  renderSettings();
  try {
    const data = await apiRequest("/settings", { timeout: 10000 });
    state.settings = normalizeSettings(data);
    state.settingsDraft = state.settings.provider;
    resetSettingsSaveState();
    state.settingsState = "ready";
    hideConnectionNotice();
  } catch (error) {
    state.settings = null;
    state.settingsDraft = "";
    resetSettingsSaveState();
    if (isUnsupported(error)) {
      state.settingsState = "missing";
      state.settingsError = "GET /settings не подключён на сервере.";
    } else {
      state.settingsState = "error";
      state.settingsError = describeApiError(error, "загрузке настроек");
      if (force) showToast(`Не удалось загрузить настройки: ${state.settingsError}`, "error");
    }
  } finally {
    state.settingsLoading = false;
    renderSettings();
  }
}

function syncSettingsProviderOptions() {
  const select = els.settingProviderSelect;
  if (!select) return;
  const extra = select.querySelector('option[data-extra="1"]');
  const provider = state.settings ? state.settings.provider : "";
  if (provider && !voiceProviderInfo(provider)) {
    const option = extra || el("option", { dataset: { extra: "1" } });
    option.value = provider;
    option.textContent = `${provider} (неизвестный серверу движок)`;
    if (!extra) select.append(option);
  } else if (extra) {
    extra.remove();
  }
}

function renderSettings() {
  if (!els.settingProviderSelect || !els.settingModel) return;
  syncSettingsProviderOptions();

  const settings = state.settings;
  const draft = state.settingsDraft || "";
  const draftInfo = voiceProviderInfo(draft);
  els.settingProviderSelect.value = draft;
  els.settingProviderSelect.disabled = state.settingsLoading || state.settingsSaving;
  els.settingProviderHint.textContent = draftInfo ? draftInfo.hint : DEFAULT_PROVIDER_HINT;

  els.settingModel.textContent = settings && settings.model ? settings.model : "—";
  els.settingRate.textContent =
    settings && settings.sampleRate ? `${numberFmt.format(settings.sampleRate)} Гц` : "—";
  els.settingSpeaker.textContent = settings && settings.speaker ? settings.speaker : "—";
  els.settingFishModel.textContent =
    settings && settings.fishModel ? settings.fishModel : "—";

  if (state.settingsState === "loading") {
    els.settingsStatus.textContent = "Загрузка настроек с локального сервера…";
  } else if (state.settingsState === "error") {
    els.settingsStatus.textContent = state.settingsError || "Не удалось загрузить настройки.";
  } else if (state.settingsState === "missing") {
    els.settingsStatus.textContent =
      state.settingsError || "Сервер не предоставляет GET /settings.";
  } else if (!settings || (!settings.provider && !settings.model && !settings.sampleRate)) {
    els.settingsStatus.textContent =
      "Сервер вернул пустые настройки: данные о голосе недоступны.";
  } else if (settings.provider) {
    els.settingsStatus.textContent = `Текущий движок: ${voiceProviderLabel(
      settings.provider,
    )}. Данные получены от локального API.`;
  } else {
    els.settingsStatus.textContent = "Сервер вернул настройки без голосового движка.";
  }

  const dirty = isSettingsDirty();
  const saving = state.settingsSaving;
  els.settingsSave.disabled = saving || state.settingsLoading || !dirty;
  els.settingsSave.textContent = saving ? "Сохраняем…" : "Сохранить";
  els.settingsSave.classList.toggle("is-busy", saving);
  els.settingsSave.setAttribute("aria-busy", saving ? "true" : "false");
  if (!state.settings) {
    els.settingsSave.title = "Сначала загрузите настройки с сервера.";
  } else if (saving) {
    els.settingsSave.title = "Сохранение уже выполняется.";
  } else if (!dirty) {
    els.settingsSave.title = "Изменений нет.";
  } else {
    els.settingsSave.title = "Сохранить выбранный движок на сервере.";
  }

  let message = state.settingsSaveMessage;
  if (!message) {
    if (saving) message = "Сохраняем на сервере…";
    else if (dirty) message = "Изменение ещё не сохранено.";
  }
  els.settingsSaveStatus.textContent = message;
  els.settingsSaveStatus.classList.toggle("is-error", state.settingsSaveState === "error");
  els.settingsSaveStatus.classList.toggle("is-ok", state.settingsSaveState === "saved");
}

function describeSettingsSaveError(error) {
  if (!(error instanceof ApiError)) {
    return `Не удалось сохранить настройку: ${
      error instanceof Error ? error.message : "неизвестная ошибка"
    }.`;
  }
  if (!error.status) return describeApiError(error, "сохранении настроек");
  if (error.status === 400) {
    return "Сервер отклонил выбранный движок (400). Обновите настройки и попробуйте снова.";
  }
  if (error.status === 503) {
    return "Переключение движка недоступно на сервере (503). Проверьте, что kivi serve запущен с флагом --ui.";
  }
  if (error.status === 404 || error.status === 405) {
    return "Сервер не поддерживает сохранение настроек (POST /settings).";
  }
  if (error.status >= 500) {
    return `Сервер не смог сохранить настройку (${error.status}). Подробности — в логе kivi serve.`;
  }
  return describeApiError(error, "сохранении настроек");
}

async function saveVoiceProvider() {
  if (state.settingsSaving || state.settingsLoading) return;
  const draft = state.settingsDraft || "";
  if (!draft || !voiceProviderInfo(draft)) {
    state.settingsSaveState = "error";
    state.settingsSaveMessage = "Выберите голосовой движок из списка.";
    renderSettings();
    return;
  }
  if (state.settings && draft === state.settings.provider) {
    renderSettings();
    return;
  }
  state.settingsSaving = true;
  state.settingsSaveState = "saving";
  state.settingsSaveMessage = "Сохраняем на сервере…";
  renderSettings();
  try {
    const data = await apiRequest("/settings", {
      method: "POST",
      body: { voice_provider: draft },
      timeout: 10000,
    });
    const previous = state.settings;
    const saved = normalizeSettings(data);
    if (!saved.provider) saved.provider = draft;
    if (!saved.model && previous) saved.model = previous.model;
    if (!saved.speaker && previous) saved.speaker = previous.speaker;
    if (!saved.fishModel && previous) saved.fishModel = previous.fishModel;
    if (!saved.sampleRate && previous) saved.sampleRate = previous.sampleRate;
    state.settings = saved;
    state.settingsDraft = saved.provider;
    state.settingsState = "ready";
    state.settingsError = "";
    state.settingsSaveState = "saved";
    state.settingsSaveMessage = `Сохранено: ${voiceProviderLabel(saved.provider)}.`;
    hideConnectionNotice();
    showToast(`Голосовой движок: ${voiceProviderLabel(saved.provider)}.`, "ok");
  } catch (error) {
    state.settingsSaveState = "error";
    state.settingsSaveMessage = describeSettingsSaveError(error);
    showToast(state.settingsSaveMessage, "error", 7000);
    if (error instanceof ApiError && error.code === "network") {
      state.health = {
        status: "offline",
        version: state.health.version,
        checkedAt: Date.now(),
        error: state.settingsSaveMessage,
      };
      updateStatusUI();
    }
  } finally {
    state.settingsSaving = false;
    renderSettings();
    const active = document.activeElement;
    if (
      els.settingsSave &&
      els.settingProviderSelect &&
      (!active || active === document.body || active === els.settingsSave)
    ) {
      try {
        els.settingProviderSelect.focus({ preventScroll: true });
      } catch {
        els.settingProviderSelect.focus();
      }
    }
  }
}

/* ------------------------------------------------------------------ */
/* Health and status                                                  */
/* ------------------------------------------------------------------ */

function showConnectionNotice(message) {
  if (!els.connectionNotice) return;
  els.connectionNotice.hidden = false;
  els.connectionNoticeText.textContent = message || "Нет связи с локальным сервером.";
}

function hideConnectionNotice() {
  if (els.connectionNotice) els.connectionNotice.hidden = true;
}

function updateStatusUI() {
  if (!els.serverPill) return;
  const health = state.health;
  els.serverPill.classList.toggle("is-online", health.status === "online");
  els.serverPill.classList.toggle("is-offline", health.status === "offline");
  els.serverPill.classList.toggle("is-checking", health.status === "checking");
  let text = "офлайн";
  if (health.status === "checking") text = "проверка…";
  else if (health.status === "online") {
    text = health.version ? `онлайн · v${health.version}` : "онлайн";
  } else if (health.status === "degraded") text = "сервер";
  els.serverPillText.textContent = text;
  const label =
    health.status === "online"
      ? "Сервер Kivi доступен. Нажмите, чтобы проверить снова."
      : health.error || "Сервер Kivi недоступен. Нажмите, чтобы проверить.";
  els.serverPill.title = label;
  els.serverPill.setAttribute("aria-label", label);
  els.versionLabel.textContent = health.version ? `v${health.version}` : "v—";
  renderConnectionSettings();
}

function renderConnectionSettings() {
  if (!els.settingsConnectionStatus) return;
  const map = {
    checking: "проверка…",
    online: "онлайн",
    degraded: "сервер отвечает с ошибкой",
    offline: "офлайн",
    unknown: "неизвестно",
  };
  els.settingsConnectionStatus.textContent = map[state.health.status] || "неизвестно";
  els.settingsVersion.textContent = state.health.version || "—";
  els.settingsApi.textContent = state.apiBase || "—";
  els.settingsApi.title = state.apiBase || "";
}

async function checkHealth({ silent = false } = {}) {
  if (state.healthBusy) return;
  state.healthBusy = true;
  if (!silent || state.health.status === "unknown") {
    state.health.status = "checking";
    updateStatusUI();
  }
  try {
    const data = await apiRequest("/health", { timeout: 5000 });
    state.health = {
      status: data && data.status === "ok" ? "online" : "degraded",
      version:
        data && typeof data.version === "string" ? data.version.slice(0, 40) : null,
      checkedAt: Date.now(),
      error: "",
    };
    hideConnectionNotice();
  } catch (error) {
    state.health = {
      status: "offline",
      version: null,
      checkedAt: Date.now(),
      error: describeApiError(error, "проверке сервера"),
    };
    showConnectionNotice(state.health.error);
  } finally {
    state.healthBusy = false;
    updateStatusUI();
  }
}

/* ------------------------------------------------------------------ */
/* Server history                                                     */
/* ------------------------------------------------------------------ */

function normalizeServerMessage(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const roleRaw = typeof raw.role === "string" ? raw.role.toLowerCase() : "";
  const role =
    roleRaw === "user" || roleRaw === "human"
      ? "user"
      : roleRaw === "assistant" || roleRaw === "kivi" || roleRaw === "bot" || roleRaw === "model"
        ? "assistant"
        : "";
  if (!role) return null;
  const textRaw = raw.content ?? raw.text ?? raw.message;
  if (typeof textRaw !== "string" || !textRaw.trim()) return null;
  return {
    id: typeof raw.id === "string" ? raw.id.slice(0, 80) : makeId("srv"),
    role,
    text: textRaw.slice(0, LIMITS.messageChars),
    ts: toTimestamp(raw.created_at ?? raw.createdAt ?? raw.timestamp ?? raw.ts, 0),
    taskId: typeof raw.task_id === "string" ? raw.task_id.slice(0, 80) : null,
  };
}

async function loadServerHistory({ force = false } = {}) {
  if (state.historyLoading) return;
  state.historyLoading = true;
  const epoch = state.historyEpoch;
  try {
    const data = await apiRequest("/history?limit=50", { timeout: 10000 });
    if (epoch !== state.historyEpoch) return;
    const raw = data && Array.isArray(data.messages) ? data.messages : [];
    const messages = raw
      .map(normalizeServerMessage)
      .filter(Boolean)
      .slice(-LIMITS.serverMessages);
    if (messages.length) {
      state.serverHistory = {
        id: SERVER_CONVERSATION_ID,
        title: "История с сервера",
        createdAt: messages[0].ts || Date.now(),
        updatedAt: messages[messages.length - 1].ts || Date.now(),
        messages,
        server: true,
      };
      hideConnectionNotice();
    } else {
      state.serverHistory = null;
    }
  } catch (error) {
    if (epoch !== state.historyEpoch) return;
    state.serverHistory = null;
    if (!isUnsupported(error) && force) {
      showToast(
        `История с сервера недоступна: ${describeApiError(error, "истории")}`,
        "error",
      );
    }
  } finally {
    state.historyLoading = false;
    if (epoch !== state.historyEpoch) return;
    if (state.activeId === SERVER_CONVERSATION_ID && !state.serverHistory) {
      state.activeId = null;
    }
    renderHistory();
    if (state.view === "chat") {
      renderMessages();
      updateViewHeader();
      renderComposer();
    }
  }
}

/* ------------------------------------------------------------------ */
/* Views and navigation                                               */
/* ------------------------------------------------------------------ */

function updateViewHeader() {
  if (!els.viewTitle) return;
  if (state.view === "chat") {
    const conversation = getActiveConversation();
    els.viewTitle.textContent = conversation ? conversation.title : "Новый чат";
    let subtitle = "Локальный чат · без облака по умолчанию";
    if (conversation && conversation.id === SERVER_CONVERSATION_ID) {
      subtitle = "История сервера · только чтение";
    } else if (state.sending) {
      subtitle = "Kivi думает…";
    } else if (conversation) {
      const count = conversation.messages.length;
      subtitle = `${count} ${plural(count, [
        "сообщение",
        "сообщения",
        "сообщений",
      ])} · локальная история`;
    }
    els.viewSubtitle.textContent = subtitle;
  } else if (state.view === "voices") {
    els.viewTitle.textContent = "Голоса";
    els.viewSubtitle.textContent = state.voices.length
      ? `${state.voices.length} ${plural(state.voices.length, [
          "голос",
          "голоса",
          "голосов",
        ])} с локального сервера`
      : "Библиотека голосов Kivi";
  } else if (state.view === "media") {
    const count = state.media.gallery.length;
    const hasVideo = Boolean(state.media.video.result);
    els.viewTitle.textContent = "Медиа";
    if (count) {
      els.viewSubtitle.textContent = `${count} ${plural(count, [
        "изображение",
        "изображения",
        "изображений",
      ])} в этой сессии${hasVideo ? " · есть видео" : ""}`;
    } else if (hasVideo) {
      els.viewSubtitle.textContent = "Видео в этой сессии · локальный API";
    } else {
      els.viewSubtitle.textContent = "Генерация изображений и видео · локальный API";
    }
  } else if (state.view === "files") {
    const fs = state.files;
    els.viewTitle.textContent = "Файлы";
    if (filesSearchActive()) {
      if (fs.searchState === "loading") {
        els.viewSubtitle.textContent = `Ищу «${fs.searchQuery}»…`;
      } else if (fs.searchState === "error") {
        els.viewSubtitle.textContent = "Поиск не удался";
      } else if (fs.searchState === "empty") {
        els.viewSubtitle.textContent = `Ничего не найдено: «${fs.searchQuery}»`;
      } else {
        const count = fs.searchMatches.length;
        els.viewSubtitle.textContent = `${count} ${plural(count, [
          "результат",
          "результата",
          "результатов",
        ])} · «${fs.searchQuery}»`;
      }
    } else if (fs.state === "loading") {
      els.viewSubtitle.textContent = "Читаю папку…";
    } else if (fs.state === "error") {
      els.viewSubtitle.textContent = "Не удалось открыть папку";
    } else if (fs.path === ".") {
      els.viewSubtitle.textContent = "Рабочая папка · без удаления";
    } else {
      els.viewSubtitle.textContent = `${fs.path} · без удаления`;
    }
  } else if (state.view === "crm") {
    const crm = state.crm;
    els.viewTitle.textContent = "CRM";
    if (crm.statusState === "checking") {
      els.viewSubtitle.textContent = "Проверяю подключение CRM…";
    } else if (crm.statusState === "ready") {
      els.viewSubtitle.textContent = "AltCRM · подключено · без сохранения";
    } else if (crm.statusState === "offline") {
      els.viewSubtitle.textContent = "AltCRM · расширение не подключено";
    } else if (crm.statusState === "error" || crm.statusState === "unavailable") {
      els.viewSubtitle.textContent = "AltCRM · ошибка подключения";
    } else {
      els.viewSubtitle.textContent = "AltCRM · заполнение без сохранения";
    }
  } else if (state.view === "settings") {
    els.viewTitle.textContent = "Настройки";
    els.viewSubtitle.textContent = "Локальные параметры и данные API";
  } else {
    els.viewTitle.textContent = "Kivi";
    els.viewSubtitle.textContent = "Локальный ассистент";
  }
}

function setView(name, { focus = true } = {}) {
  if (!VIEWS.has(name)) return;
  closeFilesMenu({ returnFocus: false });
  state.view = name;
  document.querySelectorAll("[data-view]").forEach((section) => {
    section.hidden = section.dataset.view !== name;
  });
  document.querySelectorAll("[data-panel]").forEach((button) => {
    const active = button.dataset.panel === name;
    button.classList.toggle("is-active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  updateViewHeader();
  closeSidebar();
  if (name === "voices" && (state.voicesState === "idle" || state.voicesState === "error")) {
    void loadVoices();
  }
  if (name === "files" && (state.files.state === "idle" || state.files.state === "error")) {
    void loadFiles(state.files.path);
  }
  if (
    name === "settings" &&
    (state.settingsState === "idle" || state.settingsState === "error")
  ) {
    void loadSettings();
    void checkHealth({ silent: true });
  }
  if (
    name === "crm" &&
    (state.crm.statusState === "idle" ||
      state.crm.statusState === "offline" ||
      state.crm.statusState === "error" ||
      state.crm.statusState === "unavailable")
  ) {
    void loadCrmStatus();
  }
  if (focus) {
    try {
      els.viewTitle.focus({ preventScroll: true });
    } catch {
      els.viewTitle.focus();
    }
  }
}

function openSidebar() {
  if (!els.app) return;
  els.app.classList.add("nav-open");
  els.navScrim.hidden = false;
  els.navToggle.setAttribute("aria-expanded", "true");
  els.navToggle.setAttribute("aria-label", "Закрыть меню");
  const target = document.querySelector(".nav-btn.is-active") || els.newChat;
  if (target) target.focus({ preventScroll: true });
}

function closeSidebar({ returnFocus = false } = {}) {
  if (!els.app) return;
  const wasOpen = els.app.classList.contains("nav-open");
  els.app.classList.remove("nav-open");
  if (els.navScrim) els.navScrim.hidden = true;
  if (els.navToggle) {
    els.navToggle.setAttribute("aria-expanded", "false");
    els.navToggle.setAttribute("aria-label", "Открыть меню");
  }
  if (wasOpen && returnFocus && els.navToggle) {
    try {
      els.navToggle.focus({ preventScroll: true });
    } catch {
      els.navToggle.focus();
    }
  }
}

/* ------------------------------------------------------------------ */
/* Toasts                                                             */
/* ------------------------------------------------------------------ */

function showToast(message, kind = "info", ttl = 4500) {
  if (!els.toasts) return;
  const text = String(message || "").trim().slice(0, 400);
  if (!text) return;
  const now = Date.now();
  if (state.lastToast.message === text && now - state.lastToast.at < 6000) return;
  state.lastToast = { message: text, at: now };
  while (els.toasts.children.length >= 4) {
    els.toasts.firstElementChild.remove();
  }
  const toast = el("div", { class: `toast toast-${kind}` });
  toast.append(el("span", { class: "toast-text", text }));
  let timer = 0;
  const dismiss = () => {
    window.clearTimeout(timer);
    toast.classList.add("is-leaving");
    window.setTimeout(() => toast.remove(), 200);
  };
  const close = el("button", {
    type: "button",
    class: "toast-close",
    "aria-label": "Закрыть уведомление",
    text: "×",
    on: { click: dismiss },
  });
  toast.append(close);
  els.toasts.append(toast);
  timer = window.setTimeout(dismiss, Math.max(1500, ttl));
}

function showToastOnce(key, message, kind) {
  if (state.toastOnce.has(key)) return;
  state.toastOnce.add(key);
  showToast(message, kind, 7000);
}

/* ------------------------------------------------------------------ */
/* Events                                                             */
/* ------------------------------------------------------------------ */

function onComposerKeydown(event) {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    submitComposer();
  }
}

function onGlobalKeydown(event) {
  // Browser-style folder history while the Files panel is open. Text inputs
  // keep normal editing because Alt+arrows are not used for caret motion.
  if (
    state.view === "files" &&
    event.altKey &&
    !event.ctrlKey &&
    !event.metaKey &&
    (event.key === "ArrowLeft" || event.key === "ArrowRight")
  ) {
    event.preventDefault();
    stepFilesHistory(event.key === "ArrowLeft" ? -1 : 1);
    return;
  }
  // Browser-style tab shortcuts, scoped to the Files view: other panels keep
  // the browser's own Ctrl+T / Ctrl+W / Ctrl+Tab behaviour.
  if (state.view === "files" && event.ctrlKey && !event.altKey && !event.metaKey) {
    const key = event.key.toLowerCase();
    const target = event.target;
    const editing =
      target instanceof HTMLElement &&
      (target.matches("input, textarea") || target.isContentEditable);
    if (!event.shiftKey && key === "z" && !editing) {
      event.preventDefault();
      void undoFilesOperation();
      return;
    }
    if (!event.shiftKey && (key === "t" || key === "w")) {
      // Always swallow the browser command here: closing the Kivi page by
      // accident is worse than a no-op on the last tab.
      event.preventDefault();
      if (key === "t") openFilesTab();
      else closeFilesTab(state.files.activeTabId);
      return;
    }
    if (event.key === "Tab") {
      event.preventDefault();
      stepFilesTab(event.shiftKey ? -1 : 1);
      return;
    }
  }
  if (event.key !== "Escape") return;
  // Escape also drops a drag highlight; the browser cancels the native drag.
  if (state.files.drag) {
    clearFilesDrag();
    return;
  }
  if (state.files.menuOpen) {
    event.preventDefault();
    closeFilesMenu({ returnFocus: true });
    return;
  }
  const searchFocused =
    Boolean(els.filesSearchForm) && els.filesSearchForm.contains(document.activeElement);
  if (
    searchFocused &&
    (state.files.searchQuery || (els.filesSearchInput && els.filesSearchInput.value))
  ) {
    event.preventDefault();
    clearFilesSearch({ focusInput: true });
    return;
  }
  if (els.app && els.app.classList.contains("nav-open")) {
    closeSidebar({ returnFocus: true });
  }
  if (state.files.mkdirOpen) {
    closeFilesMkdirForm();
    return;
  }
  if (state.files.renameOpen) {
    closeFilesRenameForm();
    return;
  }
  if (state.files.trashOpen) {
    closeFilesTrashForm();
    return;
  }
  if (state.files.transferOpen) {
    closeFilesTransferForm();
    return;
  }
  if (state.files.preview) closeFilesPreview();
  resetClearHistory();
  if (state.mic.active) stopMic();
}

function onMessageAction(event) {
  const target =
    event.target instanceof Element
      ? event.target.closest("[data-speak-message], [data-retry-message]")
      : null;
  if (!target) return;
  if (target.hasAttribute("data-speak-message")) {
    const id = target.getAttribute("data-speak-message");
    if (state.speakingMessageId === id) {
      stopAudio();
      return;
    }
    const message = findMessageById(id);
    if (message) void speakText(message.text, { messageId: id });
    return;
  }
  if (target.hasAttribute("data-retry-message")) {
    retryMessage(target.getAttribute("data-retry-message"));
  }
}

function bindMicControls() {
  const button = els.micBtn;
  if (!button) return;
  const press = (event) => {
    if (event.type === "pointerdown" && event.pointerType === "mouse" && event.button !== 0) {
      return;
    }
    if (state.wake.enabled || state.wake.auto) stopWakeMode();
    event.preventDefault();
    state.mic.pressAt = Date.now();
    state.mic.pressStartedActive = state.mic.active;
    if (event.pointerId !== undefined && typeof button.setPointerCapture === "function") {
      try {
        button.setPointerCapture(event.pointerId);
      } catch {
        /* ignore */
      }
    }
    void startMic();
  };
  const release = (event) => {
    if (event) event.preventDefault();
    const held = Date.now() - (state.mic.pressAt || 0);
    // Short first tap: keep listening until the next tap. Long press or a
    // second tap: stop and transcribe. This supports both hold-to-talk and
    // click-to-talk without changing the server contract.
    if (!state.mic.pressStartedActive && held < 700 && state.mic.active) {
      setMicStatus("Слушаю… Нажми ещё раз, чтобы отправить.", "ok");
      return;
    }
    stopMic();
  };
  button.addEventListener("pointerdown", press);
  button.addEventListener("pointerup", release);
  button.addEventListener("pointercancel", release);
  button.addEventListener("keydown", (event) => {
    if ((event.key === " " || event.key === "Enter") && !event.repeat) {
      event.preventDefault();
      void startMic();
    }
  });
  button.addEventListener("keyup", (event) => {
    if (event.key === " " || event.key === "Enter") {
      event.preventDefault();
      stopMic();
    }
  });
  button.addEventListener("contextmenu", (event) => event.preventDefault());
  if (els.wakeBtn) {
    els.wakeBtn.addEventListener("click", toggleWakeMode);
  }
  window.addEventListener("blur", () => {
    if (!state.mic.background) stopMic();
  });
}

function bindEvents() {
  els.newChat.addEventListener("click", () => startNewChat());
  document.querySelectorAll("[data-panel]").forEach((button) => {
    button.addEventListener("click", () => setView(button.dataset.panel));
  });
  els.clearHistory.addEventListener("click", armClearHistory);
  els.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    submitComposer();
  });
  els.composerInput.addEventListener("keydown", onComposerKeydown);
  els.composerInput.addEventListener("input", () => {
    resizeComposer();
    renderComposer();
  });
  els.speakerToggle.addEventListener("click", () => {
    setAutoSpeak(!state.prefs.autoSpeak);
  });
  els.autoSpeakSetting.addEventListener("change", () => {
    setAutoSpeak(els.autoSpeakSetting.checked);
  });
  els.avatarToggle.addEventListener("click", () => {
    setAvatarVisible(!state.prefs.avatarVisible);
  });
  els.navToggle.addEventListener("click", () => {
    if (els.app.classList.contains("nav-open")) closeSidebar({ returnFocus: true });
    else openSidebar();
  });
  els.navScrim.addEventListener("click", () => closeSidebar({ returnFocus: true }));
  els.serverPill.addEventListener("click", () => void checkHealth());
  els.connectionRetry.addEventListener("click", () => void checkHealth());
  els.voicesRefresh.addEventListener("click", () => void loadVoices({ force: true }));
  els.settingsRefresh.addEventListener("click", () => {
    void loadSettings({ force: true });
    void checkHealth();
  });
  if (els.settingProviderSelect) {
    els.settingProviderSelect.addEventListener("change", () => {
      if (state.settingsSaving) return;
      state.settingsDraft = els.settingProviderSelect.value;
      resetSettingsSaveState();
      renderSettings();
    });
  }
  if (els.settingsSave) {
    els.settingsSave.addEventListener("click", () => void saveVoiceProvider());
  }
  els.settingsRetry.addEventListener("click", () => {
    void checkHealth();
    void loadSettings({ force: true });
  });
  els.testTone.addEventListener("click", () => void playTestTone());
  if (els.mediaForm) {
    els.mediaForm.addEventListener("submit", (event) => {
      event.preventDefault();
      void submitMedia();
    });
    if (els.mediaModeImage) {
      els.mediaModeImage.addEventListener("change", () => {
        if (els.mediaModeImage.checked) setMediaMode("image");
      });
    }
    if (els.mediaModeVideo) {
      els.mediaModeVideo.addEventListener("change", () => {
        if (els.mediaModeVideo.checked) setMediaMode("video");
      });
    }
    els.mediaPrompt.addEventListener("input", onMediaPromptInput);
    els.mediaPrompt.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        void submitMedia();
      }
    });
    els.mediaClear.addEventListener("click", clearMediaGallery);
  }
  if (els.filesRefresh) {
    if (els.filesTablist) {
      els.filesTablist.addEventListener("keydown", onFilesTablistKeydown);
      els.filesTablist.addEventListener("auxclick", onFilesTablistAuxClick);
      els.filesTablist.addEventListener("mousedown", onFilesTablistMouseDown);
    }
    if (els.filesTabAdd) {
      els.filesTabAdd.addEventListener("click", () => openFilesTab());
    }
    window.addEventListener("resize", onFilesTabsResize);
    els.filesRefresh.addEventListener("click", refreshFilesView);
    els.filesBack.addEventListener("click", () => stepFilesHistory(-1));
    els.filesForward.addEventListener("click", () => stepFilesHistory(1));
    if (els.filesShell) {
      els.filesShell.addEventListener("click", () => {
        clearFilesSearch({ render: false });
        void loadFiles("shell://", { focus: true });
      });
    }
    if (els.filesUndo) {
      els.filesUndo.addEventListener("click", () => void undoFilesOperation());
    }
    els.filesPathForm.addEventListener("submit", onSubmitFilesPath);
    els.filesPathInput.addEventListener("input", onFilesPathInput);
    els.filesPathInput.addEventListener("keydown", onFilesPathKeydown);
    els.filesSearchForm.addEventListener("submit", onSubmitFilesSearch);
    els.filesSearchClear.addEventListener("click", () =>
      clearFilesSearch({ focusInput: true }),
    );
    els.filesSearchInput.addEventListener("input", onFilesSearchInput);
    els.filesMkdir.addEventListener("click", toggleFilesMkdirForm);
    els.filesMkdirForm.addEventListener("submit", (event) => void submitFilesMkdir(event));
    els.filesMkdirCancel.addEventListener("click", () => closeFilesMkdirForm());
    els.filesMkdirInput.addEventListener("input", () => {
      if (!state.files.mkdirError) return;
      state.files.mkdirError = "";
      renderFilesWriteForm();
    });
    els.filesUp.addEventListener("click", () => {
      const parent = fsParent(state.files.path);
      if (!parent) return;
      clearFilesSearch({ render: false });
      void loadFiles(parent, { focus: true });
    });
    if (els.filesSortKey) {
      els.filesSortKey.addEventListener("change", onFilesSortKeyChange);
    }
    if (els.filesSortDir) {
      els.filesSortDir.addEventListener("click", onFilesSortDirClick);
    }
    els.filesList.addEventListener("keydown", onFilesListKeydown);
    els.filesList.addEventListener("contextmenu", onFilesListContextMenu);
    els.filesList.addEventListener("dragstart", onFilesDragStart);
    if (els.filesBrowser) {
      els.filesBrowser.addEventListener("dragover", onFilesDragOver);
      els.filesBrowser.addEventListener("drop", onFilesDrop);
    }
    els.filesBreadcrumbs.addEventListener("dragover", onFilesDragOver);
    els.filesBreadcrumbs.addEventListener("drop", onFilesDrop);
    document.addEventListener("dragend", onFilesDragEnd);
    document.addEventListener("dragover", onFilesDragOverDocument);
    document.addEventListener("drop", onFilesDropOutside);
    if (els.filesMenu) {
      els.filesMenu.addEventListener("click", onFilesMenuItemClick);
      els.filesMenu.addEventListener("keydown", onFilesMenuKeydown);
    }
    els.filesPreviewClose.addEventListener("click", () => closeFilesPreview());
    els.filesPreviewRefresh.addEventListener("click", () => {
      const preview = state.files.preview;
      if (preview) void loadFilesPreview(preview.path, state.files.previewTrigger);
    });
    els.filesCopy.addEventListener("click", () => void copyFilesPreview());
    els.filesTransferCopy.addEventListener("click", () => toggleFilesTransferForm("copy"));
    els.filesTransferMove.addEventListener("click", () => toggleFilesTransferForm("move"));
    els.filesTransferForm.addEventListener("submit", (event) => void submitFilesTransfer(event));
    els.filesTransferCancel.addEventListener("click", () => closeFilesTransferForm());
    els.filesTransferInput.addEventListener("input", () => {
      if (!state.files.transferError) return;
      state.files.transferError = "";
      renderFilesPreview();
    });
    els.filesPreviewRename.addEventListener("click", toggleFilesRenameForm);
    els.filesRenameForm.addEventListener("submit", (event) => void submitFilesRename(event));
    els.filesRenameCancel.addEventListener("click", () => closeFilesRenameForm());
    els.filesRenameInput.addEventListener("input", () => {
      if (!state.files.renameError) return;
      state.files.renameError = "";
      renderFilesPreview();
    });
    els.filesPreviewTrash.addEventListener("click", toggleFilesTrashForm);
    els.filesTrashForm.addEventListener("submit", (event) => void submitFilesTrash(event));
    els.filesTrashCancel.addEventListener("click", () => closeFilesTrashForm());
    els.filesTrashInput.addEventListener("input", () => {
      if (!state.files.trashError) return;
      state.files.trashError = "";
      renderFilesPreview();
    });
  }
  document.addEventListener("pointerdown", onFilesMenuPointerDown, true);
  window.addEventListener("scroll", onFilesMenuScroll, true);
  window.addEventListener("resize", onFilesMenuResize);
  if (els.crmCheck) {
    els.crmCheck.addEventListener("click", () => void loadCrmStatus({ force: true }));
    els.crmRead.addEventListener("click", () => void readCrmOrder());
    els.crmCopy.addEventListener("click", () => void copyCrmOrder());
  }
  if (els.crmToolsCard) {
    const tabs = [els.crmTabFill, els.crmTabAct, els.crmTabTask];
    for (const tab of tabs) {
      if (!tab) continue;
      tab.addEventListener("click", () => setCrmTab(tab.dataset.crmTab || ""));
      tab.addEventListener("keydown", onCrmTabKeydown);
    }
    if (els.crmFillForm) {
      els.crmFillForm.addEventListener("submit", (event) => void submitCrmFill(event));
      els.crmFillForm.addEventListener("input", onCrmFillInput);
      els.crmFillConfirm.addEventListener("change", () =>
        onCrmConfirmChange("fill", els.crmFillConfirm),
      );
    }
    if (els.crmActForm) {
      els.crmActForm.addEventListener("submit", (event) => void submitCrmAct(event));
      els.crmActRows.addEventListener("input", onCrmActInput);
      els.crmActAdd.addEventListener("click", addCrmActRow);
      els.crmActRows.addEventListener("click", (event) => {
        const button =
          event.target instanceof Element ? event.target.closest("[data-act-remove]") : null;
        if (!button) return;
        removeCrmActRow(button.getAttribute("data-act-remove") || "");
      });
      els.crmActConfirm.addEventListener("change", () =>
        onCrmConfirmChange("act", els.crmActConfirm),
      );
    }
    if (els.crmTaskForm) {
      els.crmTaskForm.addEventListener("submit", (event) => void submitCrmTask(event));
      els.crmTaskForm.addEventListener("input", onCrmTaskInput);
      els.crmTaskConfirm.addEventListener("change", () =>
        onCrmConfirmChange("task", els.crmTaskConfirm),
      );
    }
  }
  els.messageList.addEventListener("click", onMessageAction);
  document.querySelectorAll("[data-prompt]").forEach((chip) => {
    chip.addEventListener("click", () => {
      const prompt = chip.getAttribute("data-prompt") || "";
      if (prompt) void sendMessage(prompt);
    });
  });
  document.addEventListener("keydown", onGlobalKeydown);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopMic();
    } else {
      void checkHealth({ silent: true });
    }
  });
  window.addEventListener("pagehide", cleanupOnUnload);
  window.addEventListener("unhandledrejection", (event) => {
    if (event.reason && event.reason.name === "AbortError") return;
    console.error("Kivi UI: необработанный promise", event.reason);
    const detail =
      event.reason instanceof Error ? event.reason.message : String(event.reason ?? "");
    showToast(`Внутренняя ошибка интерфейса: ${detail}`.slice(0, 220), "error", 7000);
    event.preventDefault();
  });
  window.addEventListener("error", (event) => {
    console.error("Kivi UI: ошибка", event.error || event.message);
    showToast("Ошибка интерфейса. Подробности — в консоли браузера.", "error");
  });
  const mobileQuery = window.matchMedia("(max-width: 899px)");
  const handleViewportChange = () => {
    if (!mobileQuery.matches) closeSidebar();
  };
  if (typeof mobileQuery.addEventListener === "function") {
    mobileQuery.addEventListener("change", handleViewportChange);
  } else if (typeof mobileQuery.addListener === "function") {
    mobileQuery.addListener(handleViewportChange);
  }
}

function cleanupOnUnload() {
  stopMic();
  if (state.audio) {
    try {
      state.audio.pause();
    } catch {
      /* ignore */
    }
  }
  if (state.beepCtx) {
    try {
      void state.beepCtx.close();
    } catch {
      /* ignore */
    }
  }
}

/* ------------------------------------------------------------------ */
/* Debug hook                                                         */
/* ------------------------------------------------------------------ */

function installDebugHook() {
  window.KIVI_UI = Object.freeze({
    version: APP_VERSION,
    getState() {
      return {
        apiBase: state.apiBase,
        view: state.view,
        health: { ...state.health },
        activeId: state.activeId,
        conversations: state.conversations.length,
        serverHistory: state.serverHistory ? state.serverHistory.messages.length : 0,
        voices: state.voices.length,
        settings: state.settings ? { ...state.settings } : null,
        settingsDraft: state.settingsDraft,
        settingsSaving: state.settingsSaving,
        settingsSaveState: state.settingsSaveState,
        media: {
          loading: state.media.loading,
          gallery: state.media.gallery.length,
          model: state.media.model,
          error: state.media.error,
        },
        mic: {
          active: state.mic.active,
          sending: state.mic.sending,
          pcmChunks: state.mic.pcmChunks.length,
          samples: state.mic.pcmChunks.reduce((total, chunk) => total + chunk.length, 0),
          sampleRate: state.mic.sampleRate,
          error: els.micStatus ? els.micStatus.textContent : "",
        },
        files: {
          path: state.files.path,
          state: state.files.state,
          entries: state.files.entries.length,
          sortKey: state.files.sortKey,
          sortDir: state.files.sortDir,
          // Directory history is exposed so the browser-like navigation can be
          // asserted from the UI without reaching into module scope.
          history: state.files.history.slice(),
          historyIndex: state.files.historyIndex,
          activeTab: state.files.activeTabId,
          // Tab snapshots let a browser test assert per-tab paths/history.
          tabs: state.files.tabs.map((tab) => ({
            path: tab.path,
            active: tab.id === state.files.activeTabId,
            history: tab.history.slice(),
            historyIndex: tab.historyIndex,
          })),
          pathError: state.files.pathError,
          preview: state.files.preview ? state.files.preview.path : null,
          previewLoading: Boolean(state.files.preview && state.files.preview.loading),
          busy: state.files.busy,
          mkdirOpen: state.files.mkdirOpen,
          renameOpen: state.files.renameOpen,
          trashOpen: state.files.trashOpen,
          transferOpen: state.files.transferOpen,
          transferMode: state.files.transferMode,
          menuOpen: state.files.menuOpen,
          menuPath: state.files.menuPath || null,
          target: state.files.target ? state.files.target.path : null,
          searchQuery: state.files.searchQuery,
          searchState: state.files.searchState,
          searchMatches: state.files.searchMatches.length,
          searchTruncated: state.files.searchTruncated,
        },
        crm: {
          statusState: state.crm.statusState,
          connected: state.crm.connected,
          readState: state.crm.readState,
          orderChars: state.crm.order.length,
          tab: state.crm.tab,
          fillBusy: state.crm.fill.busy,
          actBusy: state.crm.act.busy,
          taskBusy: state.crm.task.busy,
          actRows: state.crm.act.rows.length,
          fillResults: state.crm.fill.results.length,
          actResults: state.crm.act.results.length,
          taskResults: state.crm.task.results.length,
        },
        autoSpeak: state.prefs.autoSpeak,
        avatar: avatarController.getState(),
        sending: state.sending,
      };
    },
    newChat: () => startNewChat(),
    setPanel: (name) => setView(name),
    send: (text) => sendMessage(String(text ?? "")),
    refresh: {
      health: () => checkHealth(),
      history: () => loadServerHistory({ force: true }),
      voices: () => loadVoices({ force: true }),
      files: refreshFilesView,
      settings: () => loadSettings({ force: true }),
      crm: () => loadCrmStatus({ force: true }),
    },
    setApiBase(value) {
      const normalized = normalizeApiBase(value);
      if (!normalized) {
        showToast("Адрес API отклонён: разрешены http(s) и только локальные адреса.", "error");
        return false;
      }
      state.apiBase = normalized;
      renderConnectionSettings();
      void checkHealth();
      void loadServerHistory({ force: true });
      return true;
    },
    clearHistory,
    stopAudio,
  });
}

/* ------------------------------------------------------------------ */
/* Boot                                                               */
/* ------------------------------------------------------------------ */

function cacheDom() {
  Object.assign(els, {
    app: $id("app"),
    sidebar: $id("sidebar"),
    navScrim: $id("nav-scrim"),
    navToggle: $id("nav-toggle"),
    newChat: $id("new-chat"),
    historyList: $id("history-list"),
    historyEmpty: $id("history-empty"),
    clearHistory: $id("clear-history"),
    versionLabel: $id("version-label"),
    viewTitle: $id("view-title"),
    viewSubtitle: $id("view-subtitle"),
    serverPill: $id("server-pill"),
    serverPillText: $id("server-pill-text"),
    avatarToggle: $id("avatar-toggle"),
    messages: $id("messages"),
    welcome: $id("welcome"),
    messageList: $id("message-list"),
    connectionNotice: $id("connection-notice"),
    connectionNoticeText: $id("connection-notice-text"),
    connectionRetry: $id("connection-retry"),
    readonlyNote: $id("readonly-note"),
    composer: $id("composer"),
    composerInput: $id("composer-input"),
    composerHint: $id("composer-hint"),
    micBtn: $id("mic-btn"),
    wakeBtn: $id("wake-btn"),
    micStatus: $id("mic-status"),
    speakerToggle: $id("speaker-toggle"),
    sendBtn: $id("send-btn"),
    voicesCount: $id("voices-count"),
    voicesRefresh: $id("voices-refresh"),
    voicesNote: $id("voices-note"),
    voicesStatus: $id("voices-status"),
    voicesList: $id("voices-list"),
    settingsRefresh: $id("settings-refresh"),
    settingProviderSelect: $id("settings-provider-select"),
    settingProviderHint: $id("settings-provider-hint"),
    settingModel: $id("setting-model"),
    settingRate: $id("setting-rate"),
    settingSpeaker: $id("setting-speaker"),
    settingFishModel: $id("setting-fish-model"),
    settingsSave: $id("settings-save"),
    settingsSaveStatus: $id("settings-save-status"),
    settingsStatus: $id("settings-status"),
    autoSpeakSetting: $id("auto-speak-setting"),
    testTone: $id("test-tone"),
    testToneStatus: $id("test-tone-status"),
    settingsConnectionStatus: $id("settings-connection-status"),
    settingsVersion: $id("settings-version"),
    settingsApi: $id("settings-api"),
    settingsConnectionNote: $id("settings-connection-note"),
    settingsRetry: $id("settings-retry"),
    mediaForm: $id("media-form"),
    mediaModeImage: $id("media-mode-image"),
    mediaModeVideo: $id("media-mode-video"),
    mediaModeHint: $id("media-mode-hint"),
    mediaPrompt: $id("media-prompt"),
    mediaPromptCount: $id("media-prompt-count"),
    mediaWidth: $id("media-width"),
    mediaHeight: $id("media-height"),
    mediaModel: $id("media-model"),
    mediaDuration: $id("media-duration"),
    mediaSubmit: $id("media-submit"),
    mediaSubmitLabel: $id("media-submit-label"),
    mediaStatus: $id("media-status"),
    mediaError: $id("media-error"),
    mediaResultCard: $id("media-result-card"),
    mediaResultImage: $id("media-result-image"),
    mediaResultMeta: $id("media-result-meta"),
    mediaResultPrompt: $id("media-result-prompt"),
    mediaResultModel: $id("media-result-model"),
    mediaResultSize: $id("media-result-size"),
    mediaResultBadge: $id("media-result-badge"),
    mediaDownload: $id("media-download"),
    mediaEmpty: $id("media-empty"),
    mediaLoading: $id("media-loading"),
    mediaVideoCard: $id("media-video-card"),
    mediaVideoBadge: $id("media-video-badge"),
    mediaVideoPlayer: $id("media-video-player"),
    mediaVideoEmpty: $id("media-video-empty"),
    mediaVideoLoading: $id("media-video-loading"),
    mediaVideoStatus: $id("media-video-status"),
    mediaVideoError: $id("media-video-error"),
    mediaVideoMeta: $id("media-video-meta"),
    mediaVideoPrompt: $id("media-video-prompt"),
    mediaVideoModel: $id("media-video-model"),
    mediaVideoSize: $id("media-video-size"),
    mediaVideoDurationValue: $id("media-video-duration"),
    mediaVideoDownload: $id("media-video-download"),
    mediaGalleryPanel: $id("media-gallery-panel"),
    mediaGalleryList: $id("media-gallery"),
    mediaClear: $id("media-clear"),
    filesTablist: $id("files-tablist"),
    filesTabAdd: $id("files-tab-add"),
    filesTabpanel: $id("files-tabpanel"),
    filesRefresh: $id("files-refresh"),
    filesBack: $id("files-back"),
    filesForward: $id("files-forward"),
    filesShell: $id("files-shell"),
    filesUndo: $id("files-undo"),
    filesPathForm: $id("files-path-form"),
    filesPathInput: $id("files-path-input"),
    filesPathSubmit: $id("files-path-submit"),
    filesPathError: $id("files-path-error"),
    filesSortKey: $id("files-sort-key"),
    filesSortDir: $id("files-sort-dir"),
    filesSortDirLabel: $id("files-sort-dir-label"),
    filesSearchForm: $id("files-search-form"),
    filesSearchInput: $id("files-search-input"),
    filesSearchSubmit: $id("files-search-submit"),
    filesSearchClear: $id("files-search-clear"),
    filesMkdir: $id("files-mkdir"),
    filesMkdirForm: $id("files-mkdir-form"),
    filesMkdirInput: $id("files-mkdir-input"),
    filesMkdirSubmit: $id("files-mkdir-submit"),
    filesMkdirCancel: $id("files-mkdir-cancel"),
    filesMkdirError: $id("files-mkdir-error"),
    filesBreadcrumbs: $id("files-breadcrumbs"),
    filesUp: $id("files-up"),
    filesLayout: $id("files-layout"),
    filesBrowser: $id("files-browser"),
    filesDragIndicator: $id("files-drag-indicator"),
    filesListTitle: $id("files-list-title"),
    filesStatus: $id("files-status"),
    filesList: $id("files-list"),
    filesMenu: $id("files-menu"),
    filesEmpty: $id("files-empty"),
    filesNote: $id("files-note"),
    filesCount: $id("files-count"),
    filesPreviewCard: $id("files-preview-card"),
    filesPreviewTitle: $id("files-preview-title"),
    filesPreviewPath: $id("files-preview-path"),
    filesPreviewClose: $id("files-preview-close"),
    filesPreviewStatus: $id("files-preview-status"),
    filesPreviewError: $id("files-preview-error"),
    filesPreviewLoading: $id("files-preview-loading"),
    filesPreviewLoadingText: $id("files-preview-loading-text"),
    filesPreviewImage: $id("files-preview-image"),
    filesPreviewImg: $id("files-preview-img"),
    filesPreviewBody: $id("files-preview-body"),
    filesPreviewTruncated: $id("files-preview-truncated"),
    filesPreviewRefresh: $id("files-preview-refresh"),
    filesPreviewRename: $id("files-preview-rename"),
    filesRenameForm: $id("files-rename-form"),
    filesRenameTitle: $id("files-rename-form-title"),
    filesRenameInput: $id("files-rename-input"),
    filesRenameSubmit: $id("files-rename-submit"),
    filesRenameCancel: $id("files-rename-cancel"),
    filesRenameError: $id("files-rename-error"),
    filesPreviewTrash: $id("files-preview-trash"),
    filesTrashForm: $id("files-trash-form"),
    filesTrashName: $id("files-trash-name"),
    filesTrashInput: $id("files-trash-input"),
    filesTrashSubmit: $id("files-trash-submit"),
    filesTrashCancel: $id("files-trash-cancel"),
    filesTrashError: $id("files-trash-error"),
    filesTransferForm: $id("files-transfer-form"),
    filesTransferTitle: $id("files-transfer-title"),
    filesTransferInput: $id("files-transfer-input"),
    filesTransferSubmit: $id("files-transfer-submit"),
    filesTransferCancel: $id("files-transfer-cancel"),
    filesTransferHint: $id("files-transfer-hint"),
    filesTransferError: $id("files-transfer-error"),
    filesTransferCopy: $id("files-transfer-copy"),
    filesTransferMove: $id("files-transfer-move"),
    filesCopy: $id("files-copy"),
    filesCopyStatus: $id("files-copy-status"),
    crmStatusBadge: $id("crm-status-badge"),
    crmStatusBadgeText: $id("crm-status-badge-text"),
    crmStatus: $id("crm-status"),
    crmHint: $id("crm-hint"),
    crmHintText: $id("crm-hint-text"),
    crmHintError: $id("crm-hint-error"),
    crmEndpointRow: $id("crm-endpoint-row"),
    crmEndpoint: $id("crm-endpoint"),
    crmCheck: $id("crm-check"),
    crmRead: $id("crm-read"),
    crmOrderCard: $id("crm-order-card"),
    crmOrderStatus: $id("crm-order-status"),
    crmOrderError: $id("crm-order-error"),
    crmOrderLoading: $id("crm-order-loading"),
    crmOrderText: $id("crm-order-text"),
    crmOrderEmpty: $id("crm-order-empty"),
    crmCopy: $id("crm-copy"),
    crmCopyStatus: $id("crm-copy-status"),
    crmToolsCard: $id("crm-tools-card"),
    crmToolsGate: $id("crm-tools-gate"),
    crmTabFill: $id("crm-tab-fill"),
    crmTabAct: $id("crm-tab-act"),
    crmTabTask: $id("crm-tab-task"),
    crmFillPanel: $id("crm-fill-panel"),
    crmActPanel: $id("crm-act-panel"),
    crmTaskPanel: $id("crm-task-panel"),
    crmFillForm: $id("crm-fill-form"),
    crmFillFieldset: $id("crm-fill-fieldset"),
    crmFillComment: $id("crm-fill-comment"),
    crmFillDeadline: $id("crm-fill-deadline"),
    crmFillParts: $id("crm-fill-parts"),
    crmFillWorks: $id("crm-fill-works"),
    crmFillRisks: $id("crm-fill-risks"),
    crmFillConfirm: $id("crm-fill-confirm"),
    crmFillSubmit: $id("crm-fill-submit"),
    crmFillError: $id("crm-fill-error"),
    crmFillStatus: $id("crm-fill-status"),
    crmFillResults: $id("crm-fill-results"),
    crmActForm: $id("crm-act-form"),
    crmActFieldset: $id("crm-act-fieldset"),
    crmActRows: $id("crm-act-rows"),
    crmActAdd: $id("crm-act-add"),
    crmActConfirm: $id("crm-act-confirm"),
    crmActSubmit: $id("crm-act-submit"),
    crmActError: $id("crm-act-error"),
    crmActStatus: $id("crm-act-status"),
    crmActResults: $id("crm-act-results"),
    crmTaskForm: $id("crm-task-form"),
    crmTaskFieldset: $id("crm-task-fieldset"),
    crmTaskComment: $id("crm-task-comment"),
    crmTaskWhen: $id("crm-task-when"),
    crmTaskConfirm: $id("crm-task-confirm"),
    crmTaskSubmit: $id("crm-task-submit"),
    crmTaskError: $id("crm-task-error"),
    crmTaskStatus: $id("crm-task-status"),
    crmTaskResults: $id("crm-task-results"),
    avatarPanel: $id("avatar-panel"),
    avatar: $id("avatar"),
    avatarStateLabel: $id("avatar-state-label"),
    avatarCaption: $id("avatar-caption"),
    avatarMeterFill: $id("avatar-meter-fill"),
    toasts: $id("toasts"),
  });
}

function init() {
  cacheDom();
  loadPrefs();
  loadConversations();
  if (state.prefs.lastActiveId) {
    const restored = state.conversations.find(
      (conversation) => conversation.id === state.prefs.lastActiveId,
    );
    if (restored) state.activeId = restored.id;
  }

  const resolved = resolveApiBase();
  state.apiBase = resolved.base;
  state.apiNote = resolved.note;

  bindEvents();
  bindMicControls();
  applyPreferences();
  stopAudio();

  renderHistory();
  renderMessages({ forceScroll: true });
  renderComposer();
  renderSettings();
  renderVoices();
  renderMedia();
  renderMediaPromptCount();
  renderFiles();
  renderCrm();
  if (!state.crm.act.rows.length) state.crm.act.rows.push(makeCrmActRow());
  renderCrmActRows();
  renderCrmActions();
  updateViewHeader();
  setView("chat", { focus: false });
  resizeComposer();
  setAvatar("idle");

  state.health.status = "checking";
  updateStatusUI();
  installDebugHook();

  if (resolved.note) showToast(resolved.note, "error", 9000);

  void checkHealth();
  void loadServerHistory();

  window.setInterval(() => {
    if (!document.hidden) void checkHealth({ silent: true });
  }, 30000);

  if (new URLSearchParams(location.search).get("wake") === "1") {
    window.setTimeout(() => {
      if (!state.wake.auto) toggleWakeMode();
    }, 700);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
