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
const VIEWS = new Set(["chat", "voices", "settings"]);

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
/* Settings                                                           */
/* ------------------------------------------------------------------ */

function normalizeSettings(raw) {
  const data = raw && typeof raw === "object" && !Array.isArray(raw) ? raw : {};
  const rate = Number(data.sample_rate);
  return {
    provider: firstString(data.voice_provider, data.provider, data.voiceProvider),
    model: firstString(data.model, data.voice_model, data.voiceModel),
    sampleRate: Number.isFinite(rate) && rate > 0 ? rate : 0,
  };
}

async function loadSettings({ force = false } = {}) {
  if (state.settingsLoading) return;
  state.settingsLoading = true;
  state.settingsState = "loading";
  state.settingsError = "";
  renderSettings();
  try {
    const data = await apiRequest("/settings", { timeout: 10000 });
    state.settings = normalizeSettings(data);
    state.settingsState = "ready";
    hideConnectionNotice();
  } catch (error) {
    state.settings = null;
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

function renderSettings() {
  if (!els.settingProvider) return;
  const settings = state.settings;
  els.settingProvider.textContent = settings && settings.provider ? settings.provider : "—";
  els.settingModel.textContent = settings && settings.model ? settings.model : "—";
  els.settingRate.textContent =
    settings && settings.sampleRate ? `${numberFmt.format(settings.sampleRate)} Гц` : "—";

  if (state.settingsState === "loading") {
    els.settingsStatus.textContent = "Загрузка…";
  } else if (state.settingsState === "error") {
    els.settingsStatus.textContent = state.settingsError || "Не удалось загрузить настройки.";
  } else if (state.settingsState === "missing") {
    els.settingsStatus.textContent =
      state.settingsError || "Сервер не предоставляет GET /settings.";
  } else if (!settings || (!settings.provider && !settings.model && !settings.sampleRate)) {
    els.settingsStatus.textContent =
      "Сервер вернул пустые настройки: данные о голосе недоступны.";
  } else {
    els.settingsStatus.textContent = "Данные получены от локального API.";
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
  } else {
    els.viewTitle.textContent = "Настройки";
    els.viewSubtitle.textContent = "Локальные параметры и данные API";
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
  if (
    name === "settings" &&
    (state.settingsState === "idle" || state.settingsState === "error")
  ) {
    void loadSettings();
    void checkHealth({ silent: true });
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
  if (els.app && els.app.classList.contains("nav-open")) {
    closeSidebar({ returnFocus: true });
  }
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
  els.settingsRetry.addEventListener("click", () => {
    void checkHealth();
    void loadSettings({ force: true });
  });
  els.testTone.addEventListener("click", () => void playTestTone());
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
      settings: () => loadSettings({ force: true }),
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
    settingProvider: $id("setting-provider"),
    settingModel: $id("setting-model"),
    settingRate: $id("setting-rate"),
    settingsStatus: $id("settings-status"),
    autoSpeakSetting: $id("auto-speak-setting"),
    testTone: $id("test-tone"),
    testToneStatus: $id("test-tone-status"),
    settingsConnectionStatus: $id("settings-connection-status"),
    settingsVersion: $id("settings-version"),
    settingsApi: $id("settings-api"),
    settingsConnectionNote: $id("settings-connection-note"),
    settingsRetry: $id("settings-retry"),
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
