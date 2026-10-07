"""微信草稿 relay：微信接口的纯代理（零三方依赖，仅用标准库）。

端点（均受 X-API-Key 保护，若配置了 RELAY_API_KEY）：
  GET  /health          健康检查（含当前鉴权模式）
  POST /material        上传图片到素材库 -> {"media_id":..., "url":...}
  POST /draft           创建草稿 -> {"media_id":...}
  POST /draft-delete    删除草稿 -> {"errcode":0, "errmsg":"ok"}

  # 查询类（诊断用，原样透传微信响应，含 errcode）
  POST /draft-list      草稿列表 -> draft/batchget
  POST /draft-get       回读单篇草稿 -> draft/get
  POST /draft-count     草稿总数 -> draft/count
  POST /draft-update    修改草稿 -> draft/update
  POST /draft-switch    草稿箱/发布开关 -> draft/switch

  POST /cgi-bin/<path>  通用云调用代理（高级/调试用）：原样转发到 api.weixin.qq.com/<path>，
              返回 {'http_status':..., 'body':...}。仅转发已配置且账号有权限的路径；
              个人订阅号实测仅 draft/* + material/* 可用，其余路径需账号具备权限并加入「微信令牌」配置。

/material  JSON: {"name": "img/body1.png", "data_b64": "..."}
  -> {"media_id": "...", "url": "https://mmbiz.qpic.cn/..."}

/draft  JSON: {"title": "...", "content_html": "<section>...</section>",
               "thumb_media_id": "...", "author": "", "digest": "",
               "article_type": "news",        # 可选，默认 news；newspic = 贴图
               "image_media_ids": [],         # newspic 必填，永久 MediaID，≤20 张
               "content_source_url": "",      # 可选，"阅读原文"链接
               "need_open_comment": 1,        # 可选，默认 1
               "only_fans_can_comment": 0,    # 可选，默认 0
               "cover_crop": [...],           # 可选，封面裁剪 crop_percent_list
               "product_key": ""}             # 可选，文末商品卡
  -> {"media_id": "...", "article_type": "news"}

新建草稿（默认图文 news）：
  POST /draft  {"title":"...","content_html":"...","thumb_media_id":"..."}

新建贴图（newspic，首张图片即封面，image_media_ids 须为永久素材 MediaID）：
  POST /draft  {"title":"...","content_html":"纯文本","article_type":"newspic",
                "image_media_ids":["<永久media_id>","..."]}

写接口 + 查询接口都走云调用（开放接口服务）免鉴权。
个人订阅号实测可用范围：draft/* 全套 + material/*（add/get/batchget/del/count）。
markdown -> HTML 的转换在客户端（skill）完成，relay 不碰 markdown。
"""
import base64
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from app import config, wechat

# 版本标记：推到 main 触发云托管重新部署后，可用 GET /health 的 version 字段确认新版本已上线。
VERSION = "1.1.0"


def _upload_material(payload: dict) -> dict:
    name = payload.get("name") or "img.png"
    data = base64.b64decode(payload.get("data_b64") or "")
    if not data:
        raise ValueError("data_b64 不能为空")
    up = wechat.upload_image(data, name)
    return {"media_id": up["media_id"], "url": up["url"]}


def _create_draft(payload: dict) -> dict:
    title = (payload.get("title") or "").strip()
    html = payload.get("content_html") or ""
    if not title:
        raise ValueError("title 不能为空")
    if not html:
        raise ValueError("content_html 不能为空")

    article_type = (payload.get("article_type") or "news").strip()
    if article_type not in ("news", "newspic"):
        raise ValueError("article_type 只能是 news（图文）或 newspic（贴图）")

    # 贴图：图片列表必填；图文：封面 media_id 必填
    image_ids: list = []
    if article_type == "newspic":
        raw = payload.get("image_media_ids") or []
        if isinstance(raw, str):
            raw = [s.strip() for s in raw.split(",") if s.strip()]
        image_ids = [str(x).strip() for x in raw if str(x).strip()]
        if not image_ids:
            raise ValueError("article_type=newspic 时 image_media_ids 不能为空")
        if len(image_ids) > 20:
            raise ValueError("image_media_ids 最多 20 张")
    thumb = (payload.get("thumb_media_id") or "").strip()
    if article_type == "news" and not thumb:
        raise ValueError("article_type=news 时 thumb_media_id 不能为空")

    media_id = wechat.add_draft(
        title=title,
        content_html=html,
        thumb_media_id=thumb,
        author=payload.get("author") or config.DEFAULT_AUTHOR,
        digest=payload.get("digest") or "",
        article_type=article_type,
        image_media_ids=image_ids,
        content_source_url=(payload.get("content_source_url") or "").strip(),
        need_open_comment=int(payload.get("need_open_comment", 1)),
        only_fans_can_comment=int(payload.get("only_fans_can_comment", 0)),
        cover_crop=payload.get("cover_crop") or None,
        product_key=(payload.get("product_key") or "").strip(),
    )
    return {"media_id": media_id, "article_type": article_type}


def _delete_draft(payload: dict) -> dict:
    media_id = (payload.get("media_id") or "").strip()
    if not media_id:
        raise ValueError("media_id 不能为空")
    return wechat.delete_draft(media_id)


# ── 查询类处理器（诊断用，原样透传微信响应）──────────────────────────
def _list_drafts(payload: dict) -> dict:
    return wechat.list_drafts(payload.get("offset", 0), payload.get("count", 20),
                              payload.get("no_content", 1))

def _get_draft(payload: dict) -> dict:
    return wechat.get_draft((payload.get("media_id") or "").strip())

def _count_drafts(payload: dict) -> dict:
    return wechat.count_drafts()

def _update_draft(payload: dict) -> dict:
    return wechat.update_draft(
        (payload.get("media_id") or "").strip(),
        payload.get("articles") or {},
        payload.get("index", 0),
    )

def _draft_switch(payload: dict) -> dict:
    status = payload.get("status")
    if status is None:
        return wechat.set_draft_switch()
    return wechat.set_draft_switch(int(status))

# 路径 -> 查询处理器（个人订阅号实测可用）
_QUERY_ROUTES = {
    "/draft-list": _list_drafts,
    "/draft-get": _get_draft,
    "/draft-count": _count_drafts,
    "/draft-update": _update_draft,
    "/draft-switch": _draft_switch,
}


class Handler(BaseHTTPRequestHandler):
    server_version = "wechat-draft-relay/1.0"

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静默默认访问日志
        pass

    def do_GET(self):
        if urlparse(self.path).path in ("/", "/health"):
            self._send_json(200, {
                "ok": True,
                "version": VERSION,
                "mode": "cloudcall" if config.WX_CLOUDCALL else "token",
            })
        else:
            self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path

        key = self.headers.get("X-API-Key")
        if config.RELAY_API_KEY and key != config.RELAY_API_KEY:
            self._send_json(401, {"ok": False, "error": "invalid api key"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))

            # 通用云调用代理（诊断/测试用）：/cgi-bin/<path> 原样转发到 api.weixin.qq.com
            if path.startswith("/cgi-bin/"):
                self._send_json(200, wechat.proxy_post(path, payload))
                return

            if path not in ("/material", "/draft", "/draft-delete") and path not in _QUERY_ROUTES:
                self._send_json(404, {"ok": False, "error": "not found"})
                return

            if path == "/material":
                result = _upload_material(payload)
            elif path == "/draft":
                result = _create_draft(payload)
            elif path == "/draft-delete":
                result = _delete_draft(payload)
            else:
                result = _QUERY_ROUTES[path](payload)
            self._send_json(200, result)
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"ok": False, "error": str(e)})


def serve():
    mode = "cloudcall(开放接口服务,免鉴权)" if config.WX_CLOUDCALL else "token(appid+secret)"
    print(f"[relay] 启动 | 鉴权模式={mode} | 端口={config.PORT}", flush=True)
    if not config.WX_CLOUDCALL and not (config.WX_APPID and config.WX_APPSECRET):
        print("[relay][warn] 既未开启云调用也未配置 WX_APPID/WX_APPSECRET，/material、/draft 调微信会失败", flush=True)
    if not config.RELAY_API_KEY:
        print("[relay][warn] RELAY_API_KEY 未设置，写接口对外公开，任何人可调用！", flush=True)
    httpd = ThreadingHTTPServer(("0.0.0.0", config.PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
