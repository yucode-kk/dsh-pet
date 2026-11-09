/**
 * 「吃白饭」桌面 Helper 版：在桌宠自己那个透明窗里开饭。
 *
 * 与网页端的差别只在"在哪儿演"：网页端由 DSH 的 shell.overlay 槽承载、靠宿主 /anim + /position
 * 遥控宠物、用占用锁排他；桌面 Helper 里宠物就在同一个页面，所以直接点播自己的视频、用窗口
 * 内的像素坐标画米饭，不需要接口、锁与位置下发。剩下的演出节奏、米饭层、分数气泡与几何换算
 * 全部复用网页端那几份（../baifan/scene|geometry|constants|assets），台词与配置同一套。
 *
 * 结构：菜单点击 → toggle → run（下落 → 期待 → 掉饭 → 追着吃 → 吃饱收场）。
 * @module dsh-pet/src/client/baifan/desktop
 */
import { activityAssets, type ActivityAssets } from './assets.ts';
import { CFG, CSS, LINES, PIC, ROOT_ID, STYLE_TAG } from './constants.ts';
import { geometry, type Geometry } from './geometry.ts';
import { createScene, type RiceItem, type Scene } from './scene.ts';

/** 桌面 Helper 里 BASE 是 constants.js 的经典脚本全局（file:// 页面要靠它拼素材绝对地址）。 */
declare const BASE: string;

/** 一顿吃几份就收场（网页端靠丢锁收场，这里得自己定个终点）。 */
const TARGET_EATEN = 6;
/** 两次掉饭之间隔多久（毫秒）。 */
const SPAWN_MS = 3800;
/** 等一段点播动画播完的兜底时长（毫秒）：素材加载失败时不能把整场卡死。 */
const CLIP_TIMEOUT_MS = 7000;
/** 点播后最多等多久换源（毫秒）；不换源就当这段播不了，直接往下走。 */
const SWITCH_WAIT_MS = 2600;
/** 吃掉一份时米饭飞向嘴边的时长（毫秒），与空碗落地的时机对齐。 */
const FLY_MS = 1100;
/** 追饭循环里没饭可吃时的巡检间隔（毫秒）。 */
const IDLE_POLL_MS = 180;

/** 本模块用到的 PetSprite 子集（桌面 Helper 的原型对象，无类型声明）。 */
interface DesktopSprite {
  pet: { id?: string };
  animations: { baifan?: string[] };
  /** 当前在播的动画名（playback.js 里由 switchTo 写）。 */
  anim: string;
  facing: 'left' | 'right';
  front: 0 | 1;
  videoA: HTMLVideoElement | null;
  videoB: HTMLVideoElement | null;
  frozen?: boolean;
  sleeping?: boolean;
  playOnce(name: string): void;
  switchTo(name: string, once: boolean): void;
  playIdle(): void;
}

/** 对外面：菜单点一下就在开演/收场之间切换。 */
export interface DesktopBaifan {
  /** 菜单入口：没在演就开一场，在演就收场。 */
  toggle(sprite: DesktopSprite): void;
  /** 立即收场（幂等）。 */
  stop(): void;
  /** 这一场是否在跑。 */
  isRunning(): boolean;
}

/** 睡一会儿。 */
const sleep = (ms: number): Promise<void> => new Promise((resolve) => window.setTimeout(resolve, ms));

/** 按当前 BASE 拼玩法自带的几张图（helper 是 file://，页面同源 /dsh-pet-7340 取不到）。 */
function pics(): typeof PIC {
  const base = typeof BASE === 'string' ? BASE : '';
  return {
    bowlFull: base + '/pic/baifan/bowl-full.png',
    bowlEmpty: base + '/pic/baifan/bowl-empty.png',
    riceBall: base + '/pic/baifan/rice-ball.png',
  };
}

/** 注入覆盖层样式（只注入一次）。 */
function injectCss(): void {
  if (document.querySelector('style[data-plugin-css="' + STYLE_TAG + '"]') !== null) return;
  const tag = document.createElement('style');
  tag.dataset.pluginCss = STYLE_TAG;
  tag.textContent = CSS;
  document.head.appendChild(tag);
}

/**
 * 造一组桌面版吃白饭的控制面。
 *
 * 懒建覆盖层：第一次开演才往 body 挂 DOM，收场只清场上的饭、不摘覆盖层（下次开演省一次重排）。
 *
 * @returns 菜单入口、收场与运行态查询。
 */
export function mountDesktopBaifan(): DesktopBaifan {
  /** 覆盖层与场面的懒建组件；null = 还没建过。 */
  let scene: Scene | null = null;
  /** 场次令牌：收场/重开时 +1，旧的那场在每个 await 后自弃。 */
  let token = 0;
  /** 本场令牌（run 与 eatOne 共用，避免把旧场的收尾写进新场）。 */
  let current = 0;
  /** 这一场是否在跑。 */
  let running = false;
  /** 已经吃了几份。 */
  let eaten = 0;
  /** 掉饭定时器；0 = 没在掉。 */
  let spawnTimer = 0;
  /** 当前指挥的宠物。 */
  let sprite: DesktopSprite | null = null;

  /** 建覆盖层（幂等）。 */
  function ensureLayer(): Scene {
    if (scene !== null) return scene;
    injectCss();
    const node = document.createElement('div');
    node.id = ROOT_ID;
    const toast = document.createElement('div');
    toast.className = 'bf-toast';
    node.appendChild(toast);
    document.body.appendChild(node);
    scene = createScene(node, toast, pics());
    return scene;
  }

  /** 当前前台视频（宠物正画面）。 */
  function videoOf(s: DesktopSprite): HTMLVideoElement | null {
    return s.front === 0 ? s.videoA : s.videoB;
  }

  /** 按当前视频盒子算几何量；量不到返回 null。 */
  function geomOf(s: DesktopSprite): Geometry | null {
    const video = videoOf(s);
    if (video === null) return null;
    const rect = video.getBoundingClientRect();
    if (rect.width < 20 || rect.height < 10) return null;
    const pose = {
      x: (rect.left + rect.width / 2) / window.innerWidth,
      y: (rect.top + rect.height / 2) / window.innerHeight,
      facing: s.facing,
    };
    return geometry(pose, { w: rect.width, h: rect.height });
  }

  /**
   * 点播一段一次性动画并等它播完。
   *
   * 不猜片长：盯 `sprite.anim`——换源后它等于目标名，等它被下一段换掉（ended 后回随机链）
   * 就说明这段结束了；素材加载失败 / 被防重吞掉时靠 SWITCH_WAIT_MS 放行，绝不把整场卡住。
   */
  function playClip(s: DesktopSprite, name: string): Promise<void> {
    if (!name) return Promise.resolve();
    s.playOnce(name);
    return new Promise((resolve) => {
      const started = performance.now();
      let armed = false;
      let settled = false;
      const finish = (): void => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeout);
        window.clearInterval(poll);
        resolve();
      };
      const timeout = window.setTimeout(finish, CLIP_TIMEOUT_MS);
      const poll = window.setInterval(() => {
        if (s.anim === name) {
          armed = true;
          return;
        }
        if (armed) {
          finish();
          return;
        }
        if (performance.now() - started > SWITCH_WAIT_MS) finish();
      }, 40);
    });
  }

  /** 循环播侧身跑（追饭时的底噪动画）。 */
  function loopChase(s: DesktopSprite, name: string): void {
    if (!name) return;
    s.switchTo(name, false);
  }

  /** 吃掉一份：点播吃动画 → 米饭飞向嘴边 → 空碗落地 → 弹分数。 */
  async function eatOne(s: DesktopSprite, item: RiceItem, geom: Geometry, assets: ActivityAssets): Promise<void> {
    const isBowl = item.type === 'bowl';
    const sceneRef = ensureLayer();
    sceneRef.detach(item);
    s.facing = item.x > geom.cx ? 'right' : 'left';
    const done = playClip(s, isBowl ? assets.eatBowl : assets.eatBall);
    const now = geomOf(s) ?? geom;
    const mouthX = now.cx + CFG.mouthDx * now.w * (s.facing === 'right' ? -1 : 1);
    sceneRef.flyToMouth(item, mouthX, now.mouthY, FLY_MS);
    if (isBowl) {
      window.setTimeout(() => sceneRef.dropEmptyBowl(mouthX, now.mouthY, now.scale), FLY_MS - 120);
    }
    await done;
    if (token !== current) return;
    eaten += 1;
    sceneRef.popScore(eaten, item.type, now);
  }

  /** 一场演出：下落 → 期待 → 掉饭 → 追着吃 → 吃饱收场。 */
  async function run(my: number, s: DesktopSprite): Promise<void> {
    const sceneRef = ensureLayer();
    const assets = activityAssets(Array.isArray(s.animations.baifan) ? s.animations.baifan : []);
    sceneRef.toast('吃白饭 —— 开饭了');
    await playClip(s, assets.descend);
    if (my !== token) return;
    await playClip(s, assets.expect);
    if (my !== token) return;

    sceneRef.toast('掉饭了——');
    const drop = (): void => {
      if (my !== token) return;
      const geom = geomOf(s);
      const scale = geom === null ? 1 : sceneRef.scaleOf(geom.w);
      sceneRef.spawn(scale);
      sceneRef.kick();
    };
    drop();
    spawnTimer = window.setInterval(drop, SPAWN_MS);
    loopChase(s, assets.chase);

    while (my === token && running) {
      const geom = geomOf(s);
      if (geom === null) {
        await sleep(IDLE_POLL_MS * 2);
        continue;
      }
      const target = sceneRef.nearest(geom.cx, s.facing);
      if (target !== null) {
        await eatOne(s, target, geom, assets);
        if (my !== token || eaten >= TARGET_EATEN) break;
        loopChase(s, assets.chase);
        continue;
      }
      await sleep(IDLE_POLL_MS);
    }
    if (my !== token) return;
    sceneRef.toast(LINES.full[0] ?? '吃饱了');
    await sleep(900);
    if (my === token) stopRun('');
  }

  /** 收场：停定时器、清场上的饭、回随机链；幂等。 */
  function stopRun(reason: string): void {
    const wasRunning = running;
    running = false;
    token += 1;
    if (spawnTimer !== 0) {
      window.clearInterval(spawnTimer);
      spawnTimer = 0;
    }
    scene?.clear();
    eaten = 0;
    const s = sprite;
    sprite = null;
    if (s !== null && !s.frozen && !s.sleeping) s.playIdle();
    if (reason !== '' && wasRunning) scene?.toast(reason);
  }

  return {
    toggle(next: DesktopSprite): void {
      if (running) {
        stopRun('收场了');
        return;
      }
      const names = Array.isArray(next?.animations?.baifan) ? next.animations.baifan : null;
      if (names === null || names.length < 5) {
        ensureLayer().toast('这只宠物没配吃白饭动画');
        return;
      }
      sprite = next;
      current = ++token;
      running = true;
      eaten = 0;
      void run(current, next).catch((error) => {
        console.warn('[dsh-pet] 吃白饭异常', error);
        stopRun('演出中断');
      });
    },
    stop(): void {
      if (running) stopRun('收场了');
    },
    isRunning: (): boolean => running,
  };
}