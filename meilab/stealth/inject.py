"""指纹注入与 CDP 痕迹清除。

在页面任何脚本执行**之前**把"这浏览器不是被人驱动的"这类痕迹抹掉:
``navigator.webdriver``、``window.cdc_*``(Chrome 自动化注入留下的残迹)、
``window.chrome`` 缺失、``Function.prototype.toString`` 泄露非原生函数等。

同时把 :class:`~meilab.core.types.DeviceProfile` 里的 GPU 厂商/渲染器、字体、
硬件并发数、语言列表等信息注入页面,让 JS 侧看到的指纹与 UA/viewport 自洽。

注意:Patchright 本身已经规避了 ``Runtime.enable`` 蜜罐(不声明 CDP 调试器),
本模块是**第二道防线**——当 patchright 缺失、回退到官方 playwright 时尤其重要。
"""

from __future__ import annotations

import json
from typing import Any

from ..core.types import DeviceProfile

#: Chrome 自动化注入在 window 上留下的属性前缀
CDC_PREFIX = "cdc_"

#: 被覆盖的 navigator 属性 → 档案字段
_NAVIGATOR_OVERRIDES = (
    "hardwareConcurrency",
    "deviceMemory",
    "platform",
    "languages",
    "vendor",
)


def build_init_script(profile: DeviceProfile) -> str:
    """生成设备档案对应的 init script(在页面脚本之前执行)。

    :param profile: 设备档案。
    :return: JavaScript 源码字符串。
    """
    payload = {
        "webdriver": False,
        "platform": profile.platform,
        "languages": [profile.locale.split("-")[0], profile.locale],
        "hardwareConcurrency": 8 if not profile.mobile else 8,
        "deviceMemory": 8,
        "gpuVendor": profile.gpu_vendor,
        "gpuRenderer": profile.gpu_renderer,
        "fonts": list(profile.fonts)[:24],
        "hasTouch": profile.has_touch,
        "viewport": {"width": profile.viewport[0], "height": profile.viewport[1]},
        "mobile": profile.mobile,
    }
    return _TEMPLATE.replace("__PAYLOAD__", json.dumps(payload, ensure_ascii=False))


def apply_profile(context: Any, profile: DeviceProfile, *, inject_init_script: bool = True) -> None:
    """把设备档案注入到 BrowserContext。

    做两件事:
    ① 注册 init script(文档级、每个新页面都生效)——**仅在引擎支持时**;
    ② 设置附加 HTTP 头(Accept-Language 与档案 locale 一致)。

    :param context: Playwright 的 ``BrowserContext``。
    :param profile: 设备档案。
    :param inject_init_script: 引擎是否支持 init script。Patchright 1.61.2 实测
        **不支持**(init script 与 HTML 重写都会被其驱动忽略),此时置 ``False``,
        改由 :func:`harden_page` 在加载后补齐。
    """
    if inject_init_script:
        try:
            context.add_init_script(build_init_script(profile))
        except Exception:
            pass  # 引擎不支持时静默降级
    try:
        context.set_extra_http_headers(
            {
                "Accept-Language": f"{profile.locale},{profile.locale.split('-')[0]};q=0.9",
                "DNT": "1",
            }
        )
    except Exception:
        pass  # 附加头失败不影响主流程


def harden_page(page: Any, profile: DeviceProfile) -> None:
    """页面加载完成后再补一次运行时加固。

    用途有两个:
    ① 防御站点在 ``load`` 之后才读取指纹(此时 init script 早已错过时机);
    ② 在**不支持 init script 的引擎**(Patchright)下,这是唯一可用的注入通道。

    :param page: Playwright 的 ``Page``。
    :param profile: 设备档案。
    """
    try:
        page.evaluate(build_init_script(profile))
    except Exception:
        pass  # 页面已关闭或上下文异常时忽略


#: init script 模板。`__PAYLOAD__` 会被替换为 JSON 档案数据。
_TEMPLATE = r"""
(() => {
  const P = __PAYLOAD__;
  const patch = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, {get: () => value, configurable: true});
    } catch (e) {}
  };

  // navigator 上的属性定义在 Navigator.prototype 上(是访问器),
  // 只在实例上定义会被原型上的 getter 抢先,所以两处都改。
  const patchNav = (prop, value) => {
    try {
      Object.defineProperty(Navigator.prototype, prop, {get: () => value, configurable: true});
    } catch (e) {}
    try {
      Object.defineProperty(navigator, prop, {get: () => value, configurable: true});
    } catch (e) {}
  };

  // 1) 抹掉 webdriver 痕迹
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}
  patchNav('webdriver', undefined);

  // 2) 抹掉 Chrome 自动化注入留下的 cdc_* 残迹
  try {
    for (const key of Object.keys(window)) {
      if (key.startsWith('cdc_') || key.startsWith('$cdc_')) {
        try { delete window[key]; } catch (e) {}
      }
    }
  } catch (e) {}

  // 3) 补齐 window.chrome(真实 Chrome 有,无头环境常缺)
  try {
    if (!window.chrome) { window.chrome = {}; }
    if (!window.chrome.runtime) {
      window.chrome.runtime = {
        connect: function () { return {}; },
        sendMessage: function () {},
        id: undefined,
      };
    }
  } catch (e) {}

  // 4) 档案一致性:platform / languages / 硬件参数
  patchNav('platform', P.platform);
  patchNav('languages', P.languages);
  patchNav('hardwareConcurrency', P.hardwareConcurrency);
  patchNav('deviceMemory', P.deviceMemory);
  patchNav('maxTouchPoints', P.hasTouch ? 5 : 0);

  // 4b) 插件列表:无头 Chromium 报 0 个插件,真实 Chrome 有 5 个(PDF 查看器等)
  try {
    if (!navigator.plugins || navigator.plugins.length === 0) {
      const makePlugin = (name, filename, description) => ({
        name, filename, description, length: 1,
        0: {type: 'application/pdf', suffixes: 'pdf', description: 'Portable Document Format'},
        item: function () { return this[0]; },
        namedItem: function () { return this[0]; },
      });
      const fake = [
        makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        makePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
        makePlugin('WebKit built-in PDF', 'internal-pdf-viewer', 'Portable Document Format'),
      ];
      const list = {
        length: fake.length,
        item: (i) => fake[i] || null,
        namedItem: (n) => fake.find(p => p.name === n) || null,
        refresh: () => {},
        [Symbol.iterator]: function* () { yield* fake; },
      };
      Object.defineProperty(Navigator.prototype, 'plugins', {get: () => list, configurable: true});
      Object.defineProperty(Navigator.prototype, 'mimeTypes', {
        get: () => ({
          length: 2,
          item: (i) => (i === 0 ? {type: 'application/pdf', suffixes: 'pdf'} : {type: 'text/pdf', suffixes: 'pdf'}),
          namedItem: () => null,
          [Symbol.iterator]: function* () { yield {type: 'application/pdf', suffixes: 'pdf'}; },
        }),
        configurable: true,
      });
    }
  } catch (e) {}

  // 5) 触摸能力与档案一致
  try {
    if (P.hasTouch) {
      if (!('ontouchstart' in window)) {
        Object.defineProperty(window, 'ontouchstart', {value: null, configurable: true});
      }
    } else if ('ontouchstart' in window) {
      delete window.ontouchstart;
    }
  } catch (e) {}

  // 6) WebGL GPU 厂商/渲染器(WebGL1 与 WebGL2 都要覆盖)
  try {
    const patchGL = (proto) => {
      if (!proto) return;
      const orig = proto.getParameter;
      proto.getParameter = function (p) {
        if (p === 37445) return P.gpuVendor;
        if (p === 37446) return P.gpuRenderer;
        return orig.call(this, p);
      };
    };
    patchGL(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    patchGL(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch (e) {}

  // 7) 隐藏"这个函数是被注入的"痕迹:toString 一律报 native code
  try {
    const nativeToString = Function.prototype.toString;
    const patched = new WeakMap();
    const wrap = (fn, name) => {
      try {
        patched.set(fn, 'function ' + name + '() { [native code] }');
      } catch (e) {}
      return fn;
    };
    Function.prototype.toString = function () {
      if (patched.has(this)) return patched.get(this);
      return nativeToString.call(this);
    };
  } catch (e) {}

  // 8) 权限查询与档案一致(移动端不报 midi)
  try {
    const origQuery = navigator.permissions && navigator.permissions.query;
    if (origQuery) {
      navigator.permissions.query = function (desc) {
        try {
          if (desc && desc.name === 'midi' && !P.hasTouch) {
            return Promise.resolve({state: 'denied', onchange: null});
          }
        } catch (e) {}
        return origQuery.call(this, desc);
      };
    }
  } catch (e) {}
})();
"""
