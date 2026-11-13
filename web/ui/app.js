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
    entries: [],
    truncated: false,
    state: "idle",
    error: "",
    epoch: 0,
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
    stream: null,
    ctx: null,
    source: null,
    analyser: null,
    buffer: null,
    raf: 0,
    startTs: 0,
    recorder: null,
    chunks: [],
    sending: false,
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
  } = options;
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeout);
  const headers = {
    Accept: expect === "blob" ? "audio/wav, audio/*;q=0.9, */*;q=0.1" : "application/json",
  };
  let payload;
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    payload = JSON.stringify(body);
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
  if (!raw) return;
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return;
    state.prefs.autoSpeak = parsed.autoSpeak === true;
    state.prefs.avatarVisible = parsed.avatarVisible !== false;
    state.prefs.selectedVoiceId =
      typeof parsed.selectedVoiceId === "string" ? parsed.selectedVoiceId.slice(0, 120) : null;
    state.prefs.lastActiveId =
      typeof parsed.lastActiveId === "string" ? parsed.lastActiveId.slice(0, 80) : null;
  } catch {
    /* corrupted preferences are ignored */
  }
}

function savePrefs() {
  storageSet(
    STORAGE_KEYS.prefs,
    JSON.stringify({
      autoSpeak: state.prefs.autoSpeak,
      avatarVisible: state.prefs.avatarVisible,
      selectedVoiceId: state.prefs.selectedVoiceId,
      lastActiveId: state.prefs.lastActiveId,
    }),
  );
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
  state.mic.recorder = null;
  state.mic.chunks = [];
  stopMicMeter();
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
  const recorder = state.mic.recorder;
  if (recorder && recorder.state !== "inactive") {
    try {
      recorder.stop();
      return;
    } catch {
      /* fall through to cleanup */
    }
  }
  cleanupMic();
  if (wasActive) {
    setMicStatus("Микрофон выключен.", "ok");
  }
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
  const chunks = state.mic.chunks.slice();
  state.mic.chunks = [];
  const duration = Date.now() - state.mic.startTs;
  if (chunks.length === 0 || duration < 350) {
    cleanupMic();
    setMicStatus("Слишком короткая запись. Удерживай кнопку и говори.", "error");
    return;
  }
  state.mic.sending = true;
  setMicStatus("Распознаю речь…", "ok");
  setAvatar("thinking");
  try {
    const blob = new Blob(chunks, { type: chunks[0].type || "audio/webm" });
    const wav = await blobToWav(blob);
    const response = await fetch(apiUrl("/transcribe"), {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: wav,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const text = String(data.text || "").trim();
    if (!text) {
      setMicStatus("Речь не распознана. Попробуй ещё раз.", "error");
      setAvatar("idle");
      return;
    }
    setMicStatus(`Распознано: ${text}`, "ok");
    cleanupMic();
    els.composerInput.value = text;
    renderComposer();
    submitComposer();
  } catch (error) {
    setMicStatus(
      "Не удалось распознать речь. Проверь микрофон и серверный STT.",
      "error",
    );
    showToast(`Голосовой ввод: ${error.message || error}`, "error");
    cleanupMic();
    setAvatar("idle");
  } finally {
    state.mic.sending = false;
  }
}

async function startMic() {
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
  stopAudio();
  setMicStatus("Запрашиваю доступ к микрофону…");
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
    if (window.MediaRecorder) {
      const mime = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"].find(
        (type) => window.MediaRecorder.isTypeSupported(type),
      );
      const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
      recorder.ondataavailable = (event) => {
        if (event.data && event.data.size > 0) state.mic.chunks.push(event.data);
      };
      recorder.onstop = () => {
        void finishMicRecording();
      };
      state.mic.recorder = recorder;
      recorder.start(200);
    }
    if (els.micBtn) {
      els.micBtn.classList.add("is-active");
      els.micBtn.setAttribute("aria-pressed", "true");
    }
    if (els.avatarPanel) els.avatarPanel.classList.add("is-mic");
    setAvatar("listening");
    setMicStatus("Слушаю… Отпусти кнопку, чтобы отправить речь.", "ok");
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
    if (serverActive && !state.prefs.selectedVoiceId) {
      state.prefs.selectedVoiceId = serverActive.id;
      savePrefs();
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
  const button = el("button", {
    type: "button",
    class: `voice-card${selected ? " is-selected" : ""}`,
    "aria-pressed": selected ? "true" : "false",
    "aria-label": `${voice.name}. Движок: ${engineLabel(voice.engine)}.${
      voice.active ? " Активен на сервере." : ""
    }`,
    on: { click: () => void selectVoice(voice) },
  });
  button.append(el("span", { class: "voice-orb", "aria-hidden": "true" }));
  const main = el("span", { class: "voice-main" });
  main.append(el("span", { class: "voice-name", text: voice.name }));
  main.append(el("span", { class: "voice-engine", text: engineLabel(voice.engine) }));
  button.append(main);
  const badges = el("span", { class: "voice-badges" });
  if (voice.active) badges.append(el("span", { class: "badge badge-active", text: "активен" }));
  if (selected && !voice.active) {
    badges.append(el("span", { class: "badge badge-selected", text: "выбран" }));
  }
  if (badges.childNodes.length) button.append(badges);
  return el("li", { class: "voice-item" }, button);
}

async function selectVoice(voice) {
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
    if (isUnsupported(error)) {
      const reason =
        error.status === 503
          ? "Переключение голоса не настроено на сервере"
          : "Сервер не поддерживает POST /voices/active";
      showVoicesNote(`${reason}. Выбор сохранён только в этом интерфейсе.`);
    } else {
      showVoicesNote(
        `Не удалось переключить голос на сервере: ${describeApiError(
          error,
          "выборе голоса",
        )} Выбор сохранён локально.`,
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
  if (els.filesMkdirInput) els.filesMkdirInput.value = "";
  if (els.filesRenameInput) els.filesRenameInput.value = "";
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
}

async function loadFiles(rawPath = state.files.path, { force = false, focus = false } = {}) {
  const path = normalizeFsPath(rawPath);
  if (!force && path === state.files.path && state.files.state === "loading") return;
  const changed = path !== state.files.path;
  state.files.path = path;
  if (changed) {
    if (state.files.preview) closeFilesPreview({ returnFocus: false });
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
    state.files.path =
      data && typeof data.path === "string" ? normalizeFsPath(data.path) : path;
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

function renderFilesChrome() {
  renderFilesCrumbs();
  renderFilesToolbar();
  renderFilesWriteForm();
  renderFilesPreview();
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
      title: crumb.path === "." ? "Рабочая папка" : `Открыть ${crumb.path}`,
      "aria-current": last ? "location" : null,
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

  for (const entry of fs.entries) els.filesList.append(renderFileRow(entry));
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

  for (const match of fs.searchMatches) {
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
        "aria-label":
          match.type === "dir"
            ? `Открыть папку ${match.name}, путь ${path}`
            : `Открыть файл ${match.name}, путь ${path}${
                match.size !== null ? `, ${formatFileSize(match.size)}` : ""
              }`,
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
  const path = fsJoin(state.files.path, entry.name);
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
        "aria-label":
          entry.type === "dir"
            ? `Открыть папку ${entry.name}`
            : `Открыть файл ${entry.name}${entry.size !== null ? `, ${formatFileSize(entry.size)}` : ""}`,
      })
    : el("div", { class: "file-row is-static" });

  const icon = el("span", {
    class: `file-icon is-${entry.type}`,
    "aria-hidden": "true",
  });
  icon.append(fileIcon(entry.type));
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
  const preview = state.files.preview;
  const open = Boolean(preview);
  const busy = Boolean(state.files.busy);
  const renaming = state.files.busy === "rename";
  const trashing = state.files.busy === "trash";
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
    if (els.filesRenameForm) els.filesRenameForm.hidden = true;
    if (els.filesPreviewRename) {
      els.filesPreviewRename.disabled = true;
      els.filesPreviewRename.setAttribute("aria-expanded", "false");
    }
    if (els.filesTrashForm) els.filesTrashForm.hidden = true;
    if (els.filesPreviewTrash) {
      els.filesPreviewTrash.disabled = true;
      els.filesPreviewTrash.setAttribute("aria-expanded", "false");
    }
    if (els.filesTransferForm) els.filesTransferForm.hidden = true;
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
    els.filesRenameSubmit.disabled = renaming;
    els.filesRenameSubmit.textContent = renaming ? "Переименовываю…" : "Переименовать";
  }
  if (els.filesRenameCancel) els.filesRenameCancel.disabled = renaming;
  const trashOpen = state.files.trashOpen;
  if (els.filesTrashForm) {
    els.filesTrashForm.hidden = !trashOpen;
    els.filesTrashForm.setAttribute("aria-busy", trashing ? "true" : "false");
  }
  if (els.filesTrashName) {
    els.filesTrashName.textContent = preview.name || fsName(preview.path);
  }
  if (els.filesTrashError) {
    els.filesTrashError.textContent = trashOpen ? state.files.trashError : "";
    els.filesTrashError.hidden = !trashOpen || !state.files.trashError;
  }
  if (els.filesTrashInput) els.filesTrashInput.disabled = trashing;
  if (els.filesTrashSubmit) {
    els.filesTrashSubmit.disabled = trashing;
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
    els.filesTransferSubmit.disabled = transferring;
    els.filesTransferSubmit.textContent = transferring
      ? transferMode === "move"
        ? "Перемещаю…"
        : "Копирую…"
      : transferMode === "move"
        ? "Переместить"
        : "Копировать";
  }
  if (els.filesTransferCancel) els.filesTransferCancel.disabled = transferring;
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
  const preview = state.files.preview;
  if (!preview || preview.loading || state.files.busy) return;
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
    els.filesRenameInput.value = preview.name || fsName(preview.path);
  }
  renderFilesChrome();
  focusFilesInput(els.filesRenameInput);
}

function closeFilesRenameForm({ returnFocus = true } = {}) {
  if (!state.files.renameOpen || state.files.busy === "rename") return;
  state.files.renameOpen = false;
  state.files.renameError = "";
  if (els.filesRenameInput) els.filesRenameInput.value = "";
  renderFilesPreview();
  if (returnFocus && els.filesPreviewRename) {
    try {
      els.filesPreviewRename.focus({ preventScroll: true });
    } catch {
      els.filesPreviewRename.focus();
    }
  }
}

function toggleFilesRenameForm() {
  if (state.files.renameOpen) closeFilesRenameForm();
  else openFilesRenameForm();
}

function openFilesTrashForm() {
  const preview = state.files.preview;
  if (!preview || preview.loading || state.files.busy) return;
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
  focusFilesInput(els.filesTrashInput);
}

function closeFilesTrashForm({ returnFocus = true } = {}) {
  if (!state.files.trashOpen || state.files.busy === "trash") return;
  state.files.trashOpen = false;
  state.files.trashError = "";
  if (els.filesTrashInput) els.filesTrashInput.value = "";
  renderFilesPreview();
  if (returnFocus && els.filesPreviewTrash) {
    try {
      els.filesPreviewTrash.focus({ preventScroll: true });
    } catch {
      els.filesPreviewTrash.focus();
    }
  }
}

function toggleFilesTrashForm() {
  if (state.files.trashOpen) closeFilesTrashForm();
  else openFilesTrashForm();
}

async function submitFilesTrash(event) {
  if (event) event.preventDefault();
  if (state.files.busy) return;
  const preview = state.files.preview;
  if (!preview) {
    closeFilesTrashForm({ returnFocus: false });
    return;
  }
  const name = preview.name || fsName(preview.path);
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
      body: { path: preview.path, confirm: typed },
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
  const preview = state.files.preview;
  if (!preview) {
    closeFilesRenameForm({ returnFocus: false });
    return;
  }
  const currentName = preview.name || fsName(preview.path);
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
  const source = preview.path;
  const destination = fsJoin(fsParent(source) || ".", check.name);
  state.files.busy = "rename";
  state.files.renameError = "";
  renderFilesChrome();
  let renamed = false;
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
    showToast(`Переименовано: ${currentName} → ${fsName(resolved)}`, "ok");
    await loadFiles(state.files.path, { force: true });
    // Keep search results accurate after a rename.
    if (filesSearchActive()) {
      await runFilesSearch(state.files.searchQuery);
    }
    if (state.files.preview && state.files.preview.path === source) {
      await loadFilesPreview(resolved, fileRow(resolved));
    }
  } catch (error) {
    state.files.renameError = describeFsWriteError(error, "переименование файла");
  } finally {
    state.files.busy = "";
    renderFilesChrome();
    if (!renamed && state.files.renameOpen) {
      focusFilesInput(els.filesRenameInput);
    }
  }
}

function openFilesTransferForm(mode) {
  const preview = state.files.preview;
  if (!preview || preview.loading || state.files.busy) return;
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
    els.filesTransferInput.value = previousValue || preview.path || "";
  }
  renderFilesChrome();
  focusFilesInput(els.filesTransferInput);
}

function closeFilesTransferForm({ returnFocus = true } = {}) {
  if (!state.files.transferOpen || state.files.busy === "transfer") return;
  const mode = state.files.transferMode;
  state.files.transferOpen = false;
  state.files.transferMode = "";
  state.files.transferError = "";
  if (els.filesTransferInput) els.filesTransferInput.value = "";
  renderFilesPreview();
  if (!returnFocus) return;
  const target = mode === "move" ? els.filesTransferMove : els.filesTransferCopy;
  if (!target) return;
  try {
    target.focus({ preventScroll: true });
  } catch {
    target.focus();
  }
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
  const preview = state.files.preview;
  if (!preview) {
    closeFilesTransferForm({ returnFocus: false });
    return;
  }
  const mode = state.files.transferMode === "move" ? "move" : "copy";
  const source = preview.path;
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
  const name = preview.name || fsName(source);
  const endpoint = mode === "move" ? "/fs/move" : "/fs/copy";
  state.files.busy = "transfer";
  state.files.transferError = "";
  renderFilesChrome();
  let transferred = false;
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
    } else if (state.files.preview) {
      focusFilesPreview();
    }
  } catch (error) {
    state.files.transferError = describeFilesTransferError(error, mode);
  } finally {
    state.files.busy = "";
    renderFilesChrome();
    if (!transferred && state.files.transferOpen) {
      focusFilesInput(els.filesTransferInput);
    }
  }
}

function onFilesListKeydown(event) {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  if (!els.filesList) return;
  const rows = Array.from(els.filesList.querySelectorAll("button.file-row"));
  if (!rows.length) return;
  const current = rows.indexOf(document.activeElement);
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
  if (event.key !== "Escape") return;
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
    event.preventDefault();
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
    stopMic();
  };
  button.addEventListener("pointerdown", press);
  button.addEventListener("pointerup", release);
  button.addEventListener("pointercancel", release);
  button.addEventListener("lostpointercapture", release);
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
  window.addEventListener("blur", () => stopMic());
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
    els.filesRefresh.addEventListener("click", refreshFilesView);
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
    els.filesList.addEventListener("keydown", onFilesListKeydown);
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
        files: {
          path: state.files.path,
          state: state.files.state,
          entries: state.files.entries.length,
          preview: state.files.preview ? state.files.preview.path : null,
          previewLoading: Boolean(state.files.preview && state.files.preview.loading),
          busy: state.files.busy,
          mkdirOpen: state.files.mkdirOpen,
          renameOpen: state.files.renameOpen,
          trashOpen: state.files.trashOpen,
          transferOpen: state.files.transferOpen,
          transferMode: state.files.transferMode,
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
    filesRefresh: $id("files-refresh"),
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
    filesListTitle: $id("files-list-title"),
    filesStatus: $id("files-status"),
    filesList: $id("files-list"),
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
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init, { once: true });
} else {
  init();
}
