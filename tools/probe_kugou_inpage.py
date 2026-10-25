"""在酷狗页面上下文里调用候选接口(M6 调研用,非产品代码)。

原理:www.kugou.com 页面自身加载了 window.infSign(官方 H5 签名函数),
且页面 origin 调 gateway.kugou.com 不受 CORS 限制。因此注入 JS 让页面
自己签名并发请求,结果回传 Python。这样签名/路由/登录态全部由真实
前端环境决定,不存在「我猜错了参数」的干扰。

用法(仓库根):
    uv run python tools/probe_kugou_inpage.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel

_PROFILE_DIR = Path(os.environ.get("TEMP", ".")) / "neriplayer-kugou-probe-profile"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 每个候选:说明 + 路径 + 额外 GET 参数(不含签名参数)
_CANDIDATES = [
    ("v7/get_all_list", "https://gateway.kugou.com/v7/get_all_list",
     {"appid": "1005", "clientver": "20000", "plat": "1", "type": "1",
      "page": "1", "pagesize": "30"}),
    ("v2/get_all_list", "https://gateway.kugou.com/v2/get_all_list",
     {"appid": "1005", "clientver": "20000", "plat": "1", "type": "1",
      "page": "1", "pagesize": "30"}),
    ("v1/get_all_list", "https://gateway.kugou.com/v1/get_all_list",
     {"appid": "1005", "clientver": "20000", "plat": "1", "type": "1",
      "page": "1", "pagesize": "30"}),
    ("krm v1/get_all_list", "https://gateway.kugou.com/krm/v1/get_all_list",
     {"appid": "1005", "clientver": "20000", "plat": "1", "type": "1",
      "page": "1", "pagesize": "30"}),
    ("wwwapi uc/playlist", "https://wwwapi.kugou.com/uc/playlist",
     {"appid": "1005", "clientver": "20000", "plat": "1", "type": "1",
      "page": "1", "pagesize": "30"}),
    ("wwwapi v1/get_all_list", "https://wwwapi.kugou.com/v1/get_all_list",
     {"appid": "1005", "clientver": "20000", "plat": "1", "type": "1",
      "page": "1", "pagesize": "30"}),
    ("m plist/list uid", "https://m.kugou.com/plist/list/__UID__",
     {"json": "true", "page": "1"}),
    ("m user/plist", "https://m.kugou.com/user/plist",
     {"json": "true", "userid": "__UID__"}),
]

_INJECT = r"""
window.__kgResult = null;
(async () => {
  const out = [];
  const uid = String(window.__kgUid || '');
  const sign = window.infSign;
  const cands = window.__kgCands;
  const signOne = (params) => new Promise((resolve) => {
    try {
      sign(params, {}, {useH5: true, postType: 'json', log: false,
        callback: (signed) => resolve(signed)});
    } catch (e) { resolve(null); }
  });
  for (const c of cands) {
    const [name, rawUrl, extra] = c;
    const url0 = rawUrl.replace('__UID__', uid);
    const u = new URL(url0);
    for (const [k, v] of Object.entries(extra)) {
      u.searchParams.set(k, String(v).replace('__UID__', uid));
    }
    const rec = {name: name, url: u.toString(), ok: false};
    try {
      let target = u.toString();
      if (typeof sign === 'function') {
        const params = {};
        u.searchParams.forEach((v, k) => { params[k] = v; });
        const signed = await signOne(params);
        if (signed) {
          const u2 = new URL(url0);
          for (const [k, v] of Object.entries(signed)) {
            u2.searchParams.set(k, String(v));
          }
          target = u2.toString();
          rec.signed = true;
        } else {
          rec.signErr = 'sign callback returned nothing';
        }
      } else {
        rec.signErr = 'window.infSign missing';
      }
      const r = await fetch(target, {credentials: 'include'});
      const text = await r.text();
      rec.status = r.status;
      rec.len = text.length;
      rec.body = text.slice(0, 900);
      rec.ok = r.status === 200 && text.trim().startsWith('{');
    } catch (e) {
      rec.err = String(e);
    }
    out.push(rec);
  }
  window.__kgResult = JSON.stringify(out);
})();
"""


class Probe(QWidget):
    def __init__(self) -> None:
        super().__init__()
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self.resize(900, 600)
        self.label = QLabel("加载酷狗主页…")
        layout = QVBoxLayout(self)
        layout.addWidget(self.label)

        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._profile = QWebEngineProfile("kugou-probe", self)
        self._profile.setPersistentStoragePath(str(_PROFILE_DIR))
        self._profile.setCachePath(str(_PROFILE_DIR / "cache"))
        self._profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self._profile.setHttpUserAgent(_UA)
        self._view = QWebEngineView(self)
        self._page = QWebEnginePage(self._profile, self._view)
        self._view.setPage(self._page)
        layout.addWidget(self._view, stretch=1)

        self._view.loadFinished.connect(self._on_loaded)
        self._view.load(QUrl("https://www.kugou.com/"))

    def _on_loaded(self, ok: bool) -> None:
        if not ok:
            self.label.setText("主页加载失败")
            QTimer.singleShot(500, QApplication.quit)
            return
        self.label.setText("页面已加载,2 秒后注入探测…")
        QTimer.singleShot(2000, self._inject)

    def _inject(self) -> None:
        # 先把候选表与 uid 放进页面全局
        setup = (
            "window.__kgCands = " + json.dumps(_CANDIDATES, ensure_ascii=False) + ";"
        )
        self._page.runJavaScript(setup)
        self._page.runJavaScript(
            "document.cookie.split(';').map(s=>s.trim()).filter(s=>s.startsWith('KugooID='))[0]",
            self._after_uid,
        )

    def _after_uid(self, cookie_pair) -> None:
        uid = ""
        if cookie_pair and "=" in cookie_pair:
            uid = cookie_pair.split("=", 1)[1]
        self._page.runJavaScript(f"window.__kgUid = {json.dumps(uid)};")
        self._page.runJavaScript("typeof window.infSign", self._run)

    def _run(self, sign_type) -> None:
        self.label.setText(f"infSign 类型: {sign_type};执行探测…")
        self._page.runJavaScript(_INJECT)
        self._tries = 0
        self._poll = QTimer(self)
        self._poll.setInterval(1500)
        self._poll.timeout.connect(self._collect)
        self._poll.start()

    def _collect(self) -> None:
        self._tries += 1
        if self._tries > 25:
            self._poll.stop()
            self.label.setText("探测超时")
            QTimer.singleShot(300, QApplication.quit)
            return
        self._page.runJavaScript(
            "window.__kgResult === null ? '' : window.__kgResult", self._report
        )

    def _report(self, result) -> None:
        if not result:
            return
        self._poll.stop()
        out_file = Path(__file__).with_name("kugou_inpage.local.json")
        try:
            data = json.loads(result) if isinstance(result, str) else result
        except Exception:
            data = {"raw": str(result)}
        out_file.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if isinstance(data, list):
            for rec in data:
                print("=" * 74)
                print(rec.get("name"), "status=", rec.get("status"),
                      "len=", rec.get("len"), "signErr=", rec.get("signErr"))
                print((rec.get("body") or rec.get("err") or "")[:500])
        else:
            print(data)
        print("已写入", out_file)
        QTimer.singleShot(300, QApplication.quit)


def main() -> int:
    app = QApplication(sys.argv)
    probe = Probe()
    probe.show()
    QTimer.singleShot(60000, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
