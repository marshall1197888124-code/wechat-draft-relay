"""微信服务端调用：access_token 缓存 + 素材库上传 + 草稿创建。

零三方依赖：仅用 Python 标准库 urllib（不再依赖 httpx）。

两种鉴权模式：
- 云调用（开放接口服务）模式（config.WX_CLOUDCALL=True）：
  部署在微信云托管、已开启「开放接口服务」开关并配置接口权限时，
  容器内直接以 HTTP 请求 api.weixin.qq.com，平台自动注入鉴权，无需 access_token。
- token 模式（默认本地 / 非云托管）：
  用 WX_APPID / WX_APPSECRET 换取 access_token，再携带调用接口。
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

_TOKEN_CACHE: dict = {"token": None, "exp": 0}

# 云调用模式用 HTTP（性能更好，平台侧旁加载会拦截并注入鉴权）；
# token 模式用 HTTPS 并携带 access_token。
_BASE = "http://api.weixin.qq.com" if config.WX_CLOUDCALL else "https://api.weixin.qq.com"

# 常见错误码 → 人话提示，方便排障
_ERR_HINTS = {
    40013: "（appid 无效；token 模式请检查 WX_APPID）",
    40164: "（调用 IP 不在白名单；非云托管部署需到公众号后台「IP白名单」加本机出口 IP）",
    41001: "（云调用模式：确认已在云托管控制台开启「开放接口服务」开关并重建版本）",
    48001: "（接口未授权；云调用需在「微信令牌」权限配置中加入该接口路径，如 /cgi-bin/draft/add、/cgi-bin/draft/delete）",
    40007: "（invalid media_id；贴图 image_media_ids 必须是 material/add_material 返回的永久素材 MediaID，不能用 media/uploadimg 的 url）",
    45002: "（正文超长；图文 content 需 <2 万字符且 <1M）",
    53404: "（账号已被限制带货能力；如需插商品卡请先删除商品或去掉 product_key）",
    53406: "（未开通带货能力；去掉 product_key 后重试）",
    85009: "（草稿接口频率受限，稍后重试）",
}


def using_cloudcall() -> bool:
    return config.WX_CLOUDCALL


def _url(path: str, params: dict | None = None) -> str:
    u = _BASE + path
    if params:
        u += "?" + urllib.parse.urlencode(params)
    return u


def _request_json(url: str, *, data: bytes | None = None, headers: dict | None = None,
                  method: str = "POST", timeout: int = 30) -> dict:
    """发请求并解析微信返回的 JSON。HTTPError 也尽量读回 body 转成 dict。"""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:  # 微信有时用非 200 返回错误体
        raw = e.read()
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise RuntimeError(f"微信接口 HTTP {e.code}: {raw[:200]!r}")
    return json.loads(raw.decode("utf-8", "replace"))


def _multipart_body(files: dict, boundary: str) -> tuple[bytes, str]:
    """files: {name: (filename, bytes, content_type)} → (body, content-type)"""
    parts = []
    for name, (filename, data, ctype) in files.items():
        parts.append(f"--{boundary}\r\n".encode("utf-8"))
        parts.append(
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode("utf-8")
        )
        parts.append(f"Content-Type: {ctype}\r\n\r\n".encode("utf-8"))
        parts.append(data if isinstance(data, (bytes, bytearray)) else bytes(data))
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    return body, f"multipart/form-data; boundary={boundary}"


def get_access_token() -> str:
    """token 模式：用 appid+secret 换取 access_token，本地缓存到过期前 60s。"""
    if config.WX_CLOUDCALL:
        raise RuntimeError(
            "当前为云调用模式，不应调用 get_access_token。"
            "请确认在微信云托管控制台开启了「开放接口服务」开关并重建版本。"
        )
    if not config.WX_APPID or not config.WX_APPSECRET:
        raise RuntimeError("token 模式缺少 WX_APPID / WX_APPSECRET")
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > now + 60:
        return _TOKEN_CACHE["token"]
    data = _request_json(
        _url("/cgi-bin/token", {
            "grant_type": "client_credential",
            "appid": config.WX_APPID,
            "secret": config.WX_APPSECRET,
        }),
        method="GET",
        timeout=20,
    )
    if "access_token" not in data:
        raise RuntimeError(f"获取 access_token 失败: {data}")
    _TOKEN_CACHE["token"] = data["access_token"]
    _TOKEN_CACHE["exp"] = now + data.get("expires_in", 7200)
    return _TOKEN_CACHE["token"]


def _auth_params() -> dict:
    """云调用模式返回空；token 模式返回 access_token 查询参数。"""
    if config.WX_CLOUDCALL:
        return {}
    return {"access_token": get_access_token()}


def _raise(api: str, data: dict):
    hint = _ERR_HINTS.get(data.get("errcode"), "")
    raise RuntimeError(f"{api} 失败: {data} {hint}")


def upload_image(data: bytes, filename: str = "img.png") -> dict:
    """上传图片到素材库，返回 {'media_id':..., 'url': mmbiz 链接}。"""
    url = _url("/cgi-bin/material/add_material", {"type": "image", **_auth_params()})
    boundary = "wechatrelay" + str(int(time.time() * 1000))
    body, ctype = _multipart_body({"media": (filename, data, "image/png")}, boundary)
    data_json = _request_json(url, data=body, headers={"Content-Type": ctype}, timeout=30)
    if data_json.get("errcode"):
        _raise("素材上传", data_json)
    return {"media_id": data_json["media_id"], "url": data_json["url"]}


def add_draft(
    title: str,
    content_html: str,
    thumb_media_id: str = "",
    author: str = "",
    digest: str = "",
    article_type: str = "news",
    image_media_ids: list | None = None,
    content_source_url: str = "",
    need_open_comment: int = 1,
    only_fans_can_comment: int = 0,
    cover_crop: list | None = None,
    product_key: str = "",
) -> str:
    """创建草稿，返回 media_id。

    article_type:
      news    图文消息（默认），必须给 thumb_media_id（永久 MediaID）
      newspic 图片消息/贴图，必须给 image_media_ids（永久 MediaID，≤20 张，首张即封面）

    所有可选参数不传时行为与旧版一致（need_open_comment=1、only_fans_can_comment=0）。
    """
    article = {
        "article_type": article_type,
        "title": title,
        "author": author,
        "digest": digest,
        "content": content_html,
        "need_open_comment": int(need_open_comment),
        "only_fans_can_comment": int(only_fans_can_comment),
    }
    if content_source_url:
        article["content_source_url"] = content_source_url
    if article_type == "newspic":
        # 贴图：图片走 image_info，首张即封面，不需要 thumb_media_id
        article["image_info"] = {"image_list": [{"image_media_id": m} for m in image_media_ids]}
    else:
        article["thumb_media_id"] = thumb_media_id
    if cover_crop:
        article["cover_info"] = {"crop_percent_list": cover_crop}
    if product_key:
        article["product_info"] = {"footer_product_info": {"product_key": product_key}}

    body = {"articles": [article]}
    data = _request_json(
        _url("/cgi-bin/draft/add", _auth_params()),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if data.get("errcode"):
        _raise("草稿创建", data)
    return data["media_id"]


def delete_draft(media_id: str) -> dict:
    """删除草稿，返回微信原始响应（成功为 {'errcode':0,'errmsg':'ok'}）。"""
    if not media_id:
        raise ValueError("media_id 不能为空")
    body = {"media_id": media_id}
    data = _request_json(
        _url("/cgi-bin/draft/delete", _auth_params()),
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    if data.get("errcode"):
        _raise("草稿删除", data)
    return data


# ── 查询类接口（诊断用）──────────────────────────────────────────────
# 统一返回微信原始响应（含 errcode/errmsg），由调用方判断是否成功；
# 不在此层 _raise，便于把错误原样透传给诊断端展示。

def _raw_post(path: str, payload: dict) -> dict:
    return _request_json(
        _url(path, _auth_params()),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )


def proxy_post(path: str, payload: dict) -> dict:
    """通用云调用代理（诊断/测试用）：原样转发到 api.weixin.qq.com/<path>，
    返回 {'http_status': int, 'body': <解析后的 dict 或原始文本>}。
    不在此层 _raise：任何 errcode / 非 200 都原样返回，便于测试端判断接口是否可达/已授权。
    """
    url = _url(path, _auth_params())
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:  # 微信常用非 200 返回错误体
        raw = e.read()
        status = e.code
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        body = raw.decode("utf-8", "replace")
    return {"http_status": status, "body": body}


def list_drafts(offset: int = 0, count: int = 20, no_content: int = 1) -> dict:
    """草稿列表。no_content=1 时只取元信息（media_id/标题/更新时间），不拉正文。"""
    return _raw_post("/cgi-bin/draft/batchget",
                     {"offset": offset, "count": count, "no_content": no_content})


def get_draft(media_id: str) -> dict:
    """回读单篇草稿完整内容（正文 HTML / 摘要 / 封面）。"""
    if not media_id:
        raise ValueError("media_id 不能为空")
    return _raw_post("/cgi-bin/draft/get", {"media_id": media_id})


def count_drafts() -> dict:
    """草稿总数。"""
    return _raw_post("/cgi-bin/draft/count", {})


def update_draft(media_id: str, articles: dict, index: int = 0) -> dict:
    """修改草稿（标题/正文/封面/摘要）。articles 为单篇图文 dict。"""
    if not media_id:
        raise ValueError("media_id 不能为空")
    if not articles:
        raise ValueError("articles 不能为空")
    return _raw_post("/cgi-bin/draft/update",
                     {"media_id": media_id, "index": index, "articles": articles})


def set_draft_switch(status: int | None = None) -> dict:
    """草稿箱/发布开关：status=0 关 / 1 开；不传 status 则返回当前开关状态。"""
    if status is None:
        return _raw_post("/cgi-bin/draft/switch", {})
    return _raw_post("/cgi-bin/draft/switch", {"status": status})
