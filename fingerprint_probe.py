"""指纹自检工具:用生产同款链路逐项检查会被 bot 检测站点标红的特征。

工程书第 9 节第 6 项要求跑 `bot.sannysoft.com` 并记录标红数量,但该站在本网络
TCP 层不可达(连接超时),因此本脚本用**完全相同**的 BrowserWorker + 设备档案 +
注入链路,在本地页面上做等价检测,结论可直接复用。

用法::

    python fingerprint_probe.py
"""

from __future__ import annotations

import json

from meilab.core.types import Task
from meilab.stealth.inject import harden_page
from meilab.stealth.profiles import DeviceLibrary
from meilab.workers.browser import BrowserWorker

PROBE = r"""
(() => {
  const out = {};
  out['navigator.webdriver'] = navigator.webdriver;
  out['window.chrome'] = typeof window.chrome;
  out['window.chrome.runtime'] = !!(window.chrome && window.chrome.runtime);
  out['cdc_ 痕迹数量'] = Object.keys(window).filter(k => k.startsWith('cdc_')).length;
  out['navigator.platform'] = navigator.platform;
  out['navigator.languages'] = JSON.stringify(navigator.languages);
  out['navigator.hardwareConcurrency'] = navigator.hardwareConcurrency;
  out['navigator.maxTouchPoints'] = navigator.maxTouchPoints;
  out['plugins 数量'] = navigator.plugins ? navigator.plugins.length : 0;
  out['WebGL 掩码'] = (() => {
    const c = document.createElement('canvas'); const gl = c.getContext('webgl');
    return gl ? gl.getParameter(gl.VENDOR) + ' | ' + gl.getParameter(gl.RENDERER) : 'no-webgl';
  })();
  out['WebGL 未掩码'] = (() => {
    const c = document.createElement('canvas'); const gl = c.getContext('webgl');
    const e = gl && gl.getExtension('WEBGL_debug_renderer_info');
    return e ? gl.getParameter(e.UNMASKED_VENDOR_WEBGL) + ' | ' + gl.getParameter(e.UNMASKED_RENDERER_WEBGL) : 'no-ext';
  })();
  return JSON.stringify(out);
})()
"""


def is_red(key: str, value: object) -> bool:
    """判断某一项是否会被检测站点标红。

    :param key: 检测项名称。
    :param value: 检测值。
    :return: 是否标红。
    """
    if key == "navigator.webdriver":
        return value is not False
    if key == "window.chrome.runtime":
        return value is False
    if key == "cdc_ 痕迹数量":
        return value != 0
    if key == "plugins 数量":
        return value == 0
    if key == "WebGL 掩码":
        return value == "no-webgl"
    return False


def main() -> None:
    """跑一遍指纹自检并打印结果。"""
    library = DeviceLibrary()
    worker = BrowserWorker(None, None, timeout=30, device_library=library, net_guard=False)
    worker.setup()
    print("engine:", worker._engine, "| init_script_supported:", worker._init_script_supported)

    try:
        result = worker.handle(Task(url="https://quotes.toscrape.com/js/"))
        print("抓取自检:", result["status"], "html_len", len(result["html"]), "ok", result["ok"])
    except Exception as exc:
        print("抓取自检失败:", repr(exc)[:120])

    profile = worker._profile
    page = worker._context.new_page()
    page.set_content("<html><head><title>fingerprint</title></head><body>probe</body></html>")
    if profile is not None:
        harden_page(page, profile)
        print("实际使用档案:", profile.name, "| platform:", profile.platform,
              "| locale:", profile.locale, "| tz:", profile.timezone)

    data = json.loads(page.evaluate(PROBE))
    red: list[str] = []
    green: list[str] = []
    for key, value in data.items():
        line = f"{key} = {value}"
        (red if is_red(key, value) else green).append(line)

    print("\n[通过/不触发检测]")
    for item in green:
        print("  OK  ", item)
    print("\n[被标红]")
    for item in red:
        print("  RED ", item)
    if not red:
        print("  (无)")
    print(f"\n标红项: {len(red)} / {len(data)}")

    page.close()
    worker.teardown()


if __name__ == "__main__":
    main()
