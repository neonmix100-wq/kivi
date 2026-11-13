import * as THREE from "three";
import { GLTFLoader } from "three/addons/loaders/GLTFLoader.js";
import { VRMLoaderPlugin, VRMUtils } from "@pixiv/three-vrm";

const STATE_URL = "/state.json";
const POLL_INTERVAL_MS = 100;
const REQUEST_TIMEOUT_MS = 2000;

const STATES = new Set(["idle", "listening", "thinking", "speaking", "happy", "error"]);
const VISEMES = new Set(["A", "I", "U", "E", "O", "CLOSED"]);
const SHAPE_NAMES = ["A", "I", "U", "E", "O"];

const VRM_EXPRESSION_NAMES = {
  A: ["aa", "a"],
  I: ["ih", "i"],
  U: ["ou", "u"],
  E: ["ee", "e"],
  O: ["oh", "o"],
};

const VISEME_WIDTH = { A: 1.25, I: 1.0, U: 0.62, E: 1.05, O: 0.85, CLOSED: 0.9 };

function mustElement(id) {
  const element = document.getElementById(id);
  if (!element) throw new Error(`avatar3d: missing #${id}`);
  return element;
}

const canvas = mustElement("stage");
const statePill = mustElement("state-pill");
const visemePill = mustElement("viseme-pill");
const sourcePill = mustElement("source-pill");
const messageElement = mustElement("message");
const bootError = mustElement("boot-error");

if (!(canvas instanceof HTMLCanvasElement)) {
  throw new Error("avatar3d: #stage must be a canvas");
}

function setText(element, text) {
  if (element && element.textContent !== text) element.textContent = text;
}

function showBootError(message) {
  bootError.hidden = false;
  setText(bootError, message);
  setText(messageElement, message);
}

function createRenderer(target) {
  try {
    const renderer = new THREE.WebGLRenderer({
      canvas: target,
      alpha: true,
      antialias: true,
      powerPreference: "low-power",
      preserveDrawingBuffer: false,
    });
    renderer.setClearColor(0x000000, 0);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.NoToneMapping;
    return renderer;
  } catch (error) {
    window.__kiviAvatarReady = true;
    showBootError(
      `WebGL недоступен: ${error?.message ?? error}. Нужен браузер с аппаратным GPU.`,
    );
    throw error;
  }
}

const renderer = createRenderer(canvas);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(
  30,
  Math.max(1, window.innerWidth) / Math.max(1, window.innerHeight),
  0.05,
  50,
);
camera.position.set(0, 1.3, 1.95);
camera.lookAt(0, 1.28, 0);

scene.add(new THREE.HemisphereLight(0xffffff, 0x203040, 2.2));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.6);
keyLight.position.set(2.2, 3.2, 3.5);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0x9fe8ff, 1.4);
fillLight.position.set(-2.8, 1.4, 2.2);
scene.add(fillLight);
const rimLight = new THREE.DirectionalLight(0x5fffe0, 1.8);
rimLight.position.set(0.5, 2.4, -3.2);
scene.add(rimLight);

const avatarRoot = new THREE.Group();
scene.add(avatarRoot);

function createFallbackAvatar() {
  const root = new THREE.Group();
  root.name = "kivi-procedural-head";

  const skin = new THREE.MeshStandardMaterial({ color: 0xf0c9b8, roughness: 0.72, metalness: 0 });
  const skinShade = new THREE.MeshStandardMaterial({
    color: 0xd9a995,
    roughness: 0.78,
    metalness: 0,
  });
  const hair = new THREE.MeshStandardMaterial({
    color: 0x35c9be,
    roughness: 0.42,
    metalness: 0.05,
  });
  const hairDark = new THREE.MeshStandardMaterial({
    color: 0x209a94,
    roughness: 0.5,
    metalness: 0.05,
  });
  const eyeWhite = new THREE.MeshStandardMaterial({
    color: 0xf7ffff,
    roughness: 0.35,
    metalness: 0,
  });
  const eyeTeal = new THREE.MeshStandardMaterial({
    color: 0x1f9e9a,
    roughness: 0.3,
    metalness: 0.05,
  });
  const eyeDark = new THREE.MeshStandardMaterial({
    color: 0x0d2f31,
    roughness: 0.25,
    metalness: 0.05,
  });
  const mouthMat = new THREE.MeshStandardMaterial({
    color: 0x8e3a4a,
    roughness: 0.55,
    metalness: 0,
  });
  const outfit = new THREE.MeshStandardMaterial({
    color: 0xbfe8dc,
    roughness: 0.8,
    metalness: 0,
  });
  const cardigan = new THREE.MeshStandardMaterial({
    color: 0xf3efe6,
    roughness: 0.85,
    metalness: 0,
  });

  const torso = new THREE.Mesh(new THREE.CapsuleGeometry(0.24, 0.32, 8, 24), outfit);
  torso.position.set(0, 0.72, 0);
  torso.scale.set(1.35, 0.85, 0.6);
  root.add(torso);

  const cardiganMesh = new THREE.Mesh(new THREE.CapsuleGeometry(0.255, 0.22, 8, 24), cardigan);
  cardiganMesh.position.set(0, 0.8, -0.02);
  cardiganMesh.scale.set(1.4, 0.6, 0.7);
  root.add(cardiganMesh);

  const neck = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.085, 0.22, 20), skin);
  neck.position.set(0, 1.17, 0);
  root.add(neck);

  const headGroup = new THREE.Group();
  headGroup.position.set(0, 1.36, 0);
  root.add(headGroup);

  const head = new THREE.Mesh(new THREE.SphereGeometry(0.24, 48, 32), skin);
  head.scale.set(1.0, 1.06, 0.98);
  headGroup.add(head);

  for (const side of [-1, 1]) {
    const ear = new THREE.Mesh(new THREE.SphereGeometry(0.045, 16, 12), skin);
    ear.position.set(side * 0.232, -0.015, 0.005);
    ear.scale.set(0.42, 1.0, 0.62);
    headGroup.add(ear);
  }

  // Top cap stops above the brows; a back volume and side locks add length.
  const hairCap = new THREE.Mesh(
    new THREE.SphereGeometry(0.252, 48, 24, 0, Math.PI * 2, 0, Math.PI * 0.34),
    hair,
  );
  hairCap.position.y = 0.005;
  hairCap.scale.set(1.0, 1.04, 1.0);
  headGroup.add(hairCap);

  const backHair = new THREE.Mesh(new THREE.SphereGeometry(0.24, 32, 24), hair);
  backHair.position.set(0, -0.02, -0.1);
  backHair.scale.set(1.03, 1.08, 0.75);
  headGroup.add(backHair);

  for (const side of [-1, 1]) {
    const lock = new THREE.Mesh(new THREE.CapsuleGeometry(0.055, 0.24, 6, 12), hair);
    lock.position.set(side * 0.205, -0.17, -0.005);
    lock.rotation.z = side * 0.12;
    headGroup.add(lock);
  }

  const eyes = [];
  const gazes = [];
  for (const side of [-1, 1]) {
    const eye = new THREE.Group();
    eye.position.set(side * 0.09, 0.045, 0.212);

    const white = new THREE.Mesh(new THREE.SphereGeometry(0.043, 24, 16), eyeWhite);
    white.scale.set(1.0, 0.72, 0.55);
    eye.add(white);

    const gaze = new THREE.Group();
    const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.026, 20, 14), eyeTeal);
    pupil.scale.set(0.62, 0.85, 0.55);
    pupil.position.z = 0.024;
    gaze.add(pupil);

    const inner = new THREE.Mesh(new THREE.SphereGeometry(0.016, 16, 10), eyeDark);
    inner.scale.set(0.55, 0.8, 0.5);
    inner.position.set(0, -0.002, 0.036);
    gaze.add(inner);

    const glint = new THREE.Mesh(new THREE.SphereGeometry(0.007, 10, 8), eyeWhite);
    glint.position.set(-0.009, 0.011, 0.042);
    gaze.add(glint);

    eye.add(gaze);
    headGroup.add(eye);
    eyes.push(eye);
    gazes.push(gaze);
  }

  for (const side of [-1, 1]) {
    const brow = new THREE.Mesh(new THREE.BoxGeometry(0.075, 0.009, 0.012), hairDark);
    brow.position.set(side * 0.09, 0.112, 0.214);
    brow.rotation.z = side * -0.08;
    headGroup.add(brow);
  }

  const nose = new THREE.Mesh(new THREE.SphereGeometry(0.022, 14, 10), skinShade);
  nose.position.set(0, -0.012, 0.238);
  nose.scale.set(0.8, 0.62, 1.1);
  headGroup.add(nose);

  const jaw = new THREE.Group();
  jaw.position.set(0, -0.075, 0.222);
  headGroup.add(jaw);

  const chin = new THREE.Mesh(new THREE.SphereGeometry(0.07, 18, 12), skin);
  chin.position.set(0, -0.046, -0.02);
  chin.scale.set(1.2, 0.55, 0.5);
  jaw.add(chin);

  const mouth = new THREE.Mesh(new THREE.SphereGeometry(0.06, 24, 16), mouthMat);
  mouth.scale.set(0.9, 0.18, 0.35);
  jaw.add(mouth);

  const smile = new THREE.Mesh(new THREE.TorusGeometry(0.052, 0.006, 6, 24, Math.PI), mouthMat);
  smile.rotation.z = Math.PI;
  smile.position.set(0, -0.004, 0.008);
  smile.visible = false;
  jaw.add(smile);

  return {
    root,
    headGroup,
    jaw,
    mouth,
    smile,
    eyeLeft: eyes[0],
    eyeRight: eyes[1],
    gazeLeft: gazes[0],
    gazeRight: gazes[1],
  };
}

const fallback = createFallbackAvatar();
avatarRoot.add(fallback.root);

let current = {
  state: "idle",
  viseme: "CLOSED",
  blendshapes: { A: 0, I: 0, U: 0, E: 0, O: 0 },
  vrmAvailable: false,
  vrmUrl: null,
  message: "",
};

let connected = false;
let polling = false;
let pollTimer = null;
let rendering = false;
let rafId = null;
let vrm = null;
let vrmLoading = false;
let loadedVrmUrl = null;
let failedVrmUrl = null;
let localMessage = "";
let lookTarget = null;

const clock = new THREE.Clock();

function clamp01(value) {
  const number = Number(value);
  if (Number.isNaN(number)) return 0;
  return Math.min(1, Math.max(0, number));
}

function safeVrmUrl(value) {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin) return null;
    if (!url.pathname.startsWith("/assets/")) return null;
    if (!url.pathname.toLowerCase().endsWith(".vrm")) return null;
    return url.pathname;
  } catch {
    return null;
  }
}

function validateState(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;

  const state = STATES.has(raw.state) ? raw.state : "idle";
  const viseme = VISEMES.has(raw.viseme) ? raw.viseme : "CLOSED";

  const rawShapes =
    raw.blendshapes && typeof raw.blendshapes === "object" && !Array.isArray(raw.blendshapes)
      ? raw.blendshapes
      : {};
  const blendshapes = {};
  for (const name of SHAPE_NAMES) blendshapes[name] = clamp01(rawShapes[name]);

  const rawVrm = raw.vrm && typeof raw.vrm === "object" && !Array.isArray(raw.vrm) ? raw.vrm : {};
  const requested = rawVrm.available === true;
  const url = requested ? safeVrmUrl(rawVrm.url) : null;
  const vrmAvailable = requested && url !== null;

  let message = typeof rawVrm.message === "string" ? rawVrm.message.slice(0, 240) : "";
  if (requested && !vrmAvailable) {
    message = "VRM-ссылка отклонена: разрешены только локальные /assets/*.vrm.";
  }

  return { state, viseme, blendshapes, vrmAvailable, vrmUrl: url, message };
}

function setConnected(value) {
  if (connected === value) return;
  connected = value;
  updateHud();
}

async function pollOnce() {
  const controller = new AbortController();
  const timeoutId = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetch(`${STATE_URL}?t=${Date.now()}`, {
      cache: "no-store",
      signal: controller.signal,
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const next = validateState(await response.json());
    if (next) {
      applyState(next);
      setConnected(true);
    }
  } catch (error) {
    if (connected && error?.name !== "AbortError") {
      console.warn("avatar3d: state poll failed", error);
    }
    setConnected(false);
  } finally {
    window.clearTimeout(timeoutId);
  }
}

async function pollLoop() {
  if (!polling) return;
  await pollOnce();
  if (polling && !document.hidden) {
    pollTimer = window.setTimeout(pollLoop, POLL_INTERVAL_MS);
  }
}

function startPolling() {
  if (polling || document.hidden) return;
  polling = true;
  void pollLoop();
}

function stopPolling() {
  polling = false;
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function updateHud() {
  setText(statePill, current.state);
  setText(visemePill, current.viseme);
  const usingVrm = Boolean(vrm && vrm.scene.visible);
  setText(sourcePill, vrmLoading ? "loading VRM…" : usingVrm ? "VRM" : "procedural");

  let message;
  if (!connected) {
    message = "state.json недоступен — повторяю запрос…";
  } else if (localMessage) {
    message = localMessage;
  } else if (usingVrm) {
    message = current.message || "VRM-модель загружена.";
  } else {
    message =
      current.message ||
      "Процедурная голова: положите assets/avatar/kivi.vrm, чтобы включить VRM.";
  }
  setText(messageElement, message);
}

function applyState(next) {
  current = next;
  if (next.vrmAvailable && next.vrmUrl) {
    if (loadedVrmUrl === next.vrmUrl && vrm) {
      vrm.scene.visible = true;
      fallback.root.visible = false;
      localMessage = "";
    } else if (failedVrmUrl !== next.vrmUrl) {
      void loadVrm(next.vrmUrl);
    } else {
      localMessage = `VRM не загрузился: ${next.vrmUrl}. Активен procedural fallback.`;
    }
  } else {
    if (vrm) vrm.scene.visible = false;
    fallback.root.visible = true;
    localMessage = "";
  }
  updateHud();
}

function optimizeVrm(scene, model) {
  try {
    VRMUtils.removeUnnecessaryVertices(scene);
  } catch (error) {
    console.warn("avatar3d: removeUnnecessaryVertices skipped", error);
  }
  try {
    VRMUtils.combineSkeletons(scene);
  } catch (error) {
    console.warn("avatar3d: combineSkeletons skipped", error);
  }
  try {
    VRMUtils.rotateVRM0(model);
  } catch (error) {
    console.warn("avatar3d: rotateVRM0 skipped", error);
  }
  scene.traverse((object) => {
    if (object.isMesh) object.frustumCulled = false;
  });
}

function frameVrm(model) {
  const box = new THREE.Box3().setFromObject(model.scene);
  if (box.isEmpty()) return;
  const size = new THREE.Vector3();
  const center = new THREE.Vector3();
  box.getSize(size);
  box.getCenter(center);
  const height = Math.max(0.5, size.y);
  // Fit the whole character vertically. A fixed close camera cuts the head
  // off for tall VRMs; derive the distance from the vertical FOV instead.
  const fov = THREE.MathUtils.degToRad(camera.fov);
  const distance = ((height * 1.08) / 2) / Math.tan(fov / 2) + size.z * 0.5;
  camera.position.set(center.x, center.y + height * 0.02, center.z + distance);
  camera.lookAt(center.x, center.y, center.z);
  camera.updateProjectionMatrix();
}

function disposeVrm() {
  if (!vrm) return;
  avatarRoot.remove(vrm.scene);
  try {
    VRMUtils.deepDispose(vrm.scene);
  } catch (error) {
    console.warn("avatar3d: deepDispose skipped", error);
  }
  vrm = null;
  loadedVrmUrl = null;
}

async function loadVrm(url) {
  if (vrmLoading || failedVrmUrl === url) return;
  vrmLoading = true;
  setText(sourcePill, "loading VRM…");
  try {
    const loader = new GLTFLoader();
    loader.register((parser) => new VRMLoaderPlugin(parser));
    const gltf = await loader.loadAsync(url);
    const model = gltf.userData.vrm;
    if (!model) throw new Error("файл не является VRM-моделью");

    optimizeVrm(gltf.scene, model);
    disposeVrm();
    vrm = model;
    avatarRoot.add(model.scene);
    try {
      frameVrm(model);
    } catch (error) {
      console.warn("avatar3d: VRM framing skipped", error);
    }
    loadedVrmUrl = url;
    failedVrmUrl = null;
    fallback.root.visible = false;
    model.scene.visible = true;
    localMessage = "";
    updateHud();
  } catch (error) {
    failedVrmUrl = url;
    if (vrm) vrm.scene.visible = false;
    fallback.root.visible = true;
    localMessage = `VRM load failed: ${error?.message ?? error}. Procedural fallback active.`;
    updateHud();
    console.warn("avatar3d: VRM load failed", error);
  } finally {
    vrmLoading = false;
    updateHud();
  }
}

function poseFor(state, elapsed, level) {
  switch (state) {
    case "listening":
      return {
        pitch: -0.04 + Math.sin(elapsed * 1.4) * 0.015,
        yaw: 0.06,
        roll: 0.07,
      };
    case "thinking":
      return {
        pitch: 0.07,
        yaw: -0.18 + Math.sin(elapsed * 0.7) * 0.03,
        roll: -0.04,
      };
    case "speaking":
      return {
        pitch: Math.sin(elapsed * 5.5) * 0.02 * level,
        yaw: 0,
        roll: 0.012,
      };
    case "happy":
      return {
        pitch: -0.03 + Math.sin(elapsed * 3) * 0.01,
        yaw: 0.02,
        roll: 0.055,
      };
    case "error":
      return {
        pitch: -0.12,
        yaw: 0.1 + Math.sin(elapsed * 16) * 0.03,
        roll: -0.08,
      };
    default:
      return {
        pitch: Math.sin(elapsed * 1.2) * 0.012,
        yaw: Math.sin(elapsed * 0.6) * 0.02,
        roll: 0,
      };
  }
}

function gazeTargetFor(state, elapsed) {
  switch (state) {
    case "thinking":
      return { x: -0.35, y: 1.5 };
    case "listening":
      return { x: 0.02, y: 1.34 };
    case "error":
      return { x: 0.22, y: 1.18 };
    case "happy":
      return { x: 0, y: 1.42 };
    default:
      return { x: Math.sin(elapsed * 0.4) * 0.08, y: 1.35 };
  }
}

function gazeOffsetFor(state) {
  switch (state) {
    case "thinking":
      return { x: -0.012, y: 0.008 };
    case "error":
      return { x: 0.009, y: -0.005 };
    case "listening":
      return { x: 0, y: 0.002 };
    case "happy":
      return { x: 0, y: 0.004 };
    default:
      return { x: 0, y: 0 };
  }
}

function blinkAmount(elapsed) {
  const phase = elapsed % 4.7;
  if (phase < 0.1) return Math.sin((phase / 0.1) * Math.PI);
  if (phase > 4.55) return Math.sin(((phase - 4.55) / 0.15) * Math.PI);
  return 0;
}

function updateFallback(delta, elapsed, level) {
  const pose = poseFor(current.state, elapsed, level);
  const damp = 1 - Math.exp(-7 * delta);

  fallback.headGroup.rotation.x += (pose.pitch - fallback.headGroup.rotation.x) * damp;
  fallback.headGroup.rotation.y += (pose.yaw - fallback.headGroup.rotation.y) * damp;
  fallback.headGroup.rotation.z += (pose.roll - fallback.headGroup.rotation.z) * damp;

  const blink = blinkAmount(elapsed);
  fallback.eyeLeft.scale.y = 1 - blink * 0.86;
  fallback.eyeRight.scale.y = 1 - blink * 0.86;

  const gaze = gazeOffsetFor(current.state);
  fallback.gazeLeft.position.x += (gaze.x - fallback.gazeLeft.position.x) * damp;
  fallback.gazeLeft.position.y += (gaze.y - fallback.gazeLeft.position.y) * damp;
  fallback.gazeRight.position.x += (gaze.x - fallback.gazeRight.position.x) * damp;
  fallback.gazeRight.position.y += (gaze.y - fallback.gazeRight.position.y) * damp;

  const width = VISEME_WIDTH[current.viseme] ?? 1.0;
  fallback.mouth.scale.set(
    width * (0.85 + level * 0.25),
    0.18 + level * 1.9,
    0.35 + level * 0.15,
  );
  fallback.jaw.position.y = -0.075 - level * 0.018;
  fallback.jaw.scale.y = 1 + level * 0.08;

  const smiling = current.state === "happy" && level < 0.06;
  fallback.smile.visible = smiling;
  fallback.mouth.visible = !smiling;

  const breathing = Math.sin(elapsed * 1.7) * 0.006;
  const bounce = current.state === "happy" ? Math.abs(Math.sin(elapsed * 4)) * 0.008 : 0;
  fallback.root.position.y = breathing + bounce;
}

function setExpression(manager, name, weight) {
  if (manager.getExpression(name)) manager.setValue(name, weight);
}

function applyVrmExpressions(model, frame, blink) {
  const manager = model.expressionManager;
  if (!manager) return;

  for (const name of SHAPE_NAMES) {
    const weight = frame.blendshapes[name] ?? 0;
    for (const expressionName of VRM_EXPRESSION_NAMES[name]) {
      if (manager.getExpression(expressionName)) {
        manager.setValue(expressionName, weight);
      }
    }
  }

  setExpression(manager, "blink", blink);
  setExpression(manager, "happy", frame.state === "happy" ? 0.45 : 0);
  setExpression(manager, "sad", frame.state === "error" ? 0.3 : 0);
}

function applyVrmGaze(model, state, elapsed, damp) {
  if (!model.lookAt) return;
  if (!lookTarget) {
    lookTarget = new THREE.Object3D();
    lookTarget.name = "kivi-gaze-target";
    scene.add(lookTarget);
  }
  if (model.lookAt.target !== lookTarget) model.lookAt.target = lookTarget;
  const target = gazeTargetFor(state, elapsed);
  lookTarget.position.x += (target.x - lookTarget.position.x) * damp;
  lookTarget.position.y += (target.y - lookTarget.position.y) * damp;
  lookTarget.position.z = 2;
}

function updateVrm(delta, elapsed, level) {
  if (!vrm) return;
  const pose = poseFor(current.state, elapsed, level);
  const damp = 1 - Math.exp(-7 * delta);

  const head = vrm.humanoid?.getNormalizedBoneNode("head");
  const neck = vrm.humanoid?.getNormalizedBoneNode("neck");
  if (head) {
    head.rotation.x += (pose.pitch - head.rotation.x) * damp;
    head.rotation.y += (pose.yaw - head.rotation.y) * damp;
    head.rotation.z += (pose.roll - head.rotation.z) * damp;
  }
  if (neck) {
    neck.rotation.x += (pose.pitch * 0.35 - neck.rotation.x) * damp;
    neck.rotation.y += (pose.yaw * 0.35 - neck.rotation.y) * damp;
    neck.rotation.z += (pose.roll * 0.35 - neck.rotation.z) * damp;
  }

  const breathing = Math.sin(elapsed * 1.7) * 0.004;
  const bounce = current.state === "happy" ? Math.abs(Math.sin(elapsed * 4)) * 0.006 : 0;
  vrm.scene.position.y = breathing + bounce;

  applyVrmExpressions(vrm, current, blinkAmount(elapsed));
  applyVrmGaze(vrm, current.state, elapsed, damp);
  vrm.update(delta);
}

function resize() {
  const width = Math.max(1, window.innerWidth);
  const height = Math.max(1, window.innerHeight);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}

function renderFrame() {
  rafId = null;
  if (document.hidden) return;
  const delta = Math.min(clock.getDelta(), 0.05);
  const elapsed = clock.elapsedTime;
  const level = Math.max(...SHAPE_NAMES.map((name) => current.blendshapes[name] ?? 0));
  if (vrm && vrm.scene.visible) {
    updateVrm(delta, elapsed, level);
  } else {
    updateFallback(delta, elapsed, level);
  }
  renderer.render(scene, camera);
  rafId = window.requestAnimationFrame(renderFrame);
}

function startRendering() {
  if (rendering || document.hidden) return;
  rendering = true;
  clock.getDelta();
  rafId = window.requestAnimationFrame(renderFrame);
}

function stopRendering() {
  rendering = false;
  if (rafId !== null) {
    window.cancelAnimationFrame(rafId);
    rafId = null;
  }
}

window.addEventListener("resize", resize);

canvas.addEventListener("webglcontextlost", (event) => {
  event.preventDefault();
  stopRendering();
  stopPolling();
  showBootError("WebGL context lost. Перезагрузите страницу, чтобы вернуть аватар.");
});

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    stopRendering();
    stopPolling();
    setText(messageElement, "Пауза: вкладка скрыта, рендер и polling остановлены.");
  } else {
    startRendering();
    startPolling();
    updateHud();
  }
});

resize();
updateHud();
window.__kiviAvatarReady = true;
startPolling();
startRendering();
console.info("Kivi avatar3d ready");
