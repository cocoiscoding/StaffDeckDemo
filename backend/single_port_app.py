"""单端口应用入口模块。

将前端构建产物（Vite dist）和企业端 API 整合到同一个 FastAPI 应用中，
实现前后端同端口部署。

主要功能：

1. **静态资源服务**：挂载 Vite 打包后的 ``/assets`` 目录，
   通过 ``FrontendStaticFiles`` 强制修正 MIME 类型（解决 Windows 上的 JS 文件类型问题）。
2. **SPA 路由**：对 ``/chat``、``/enterprise``、``/workspace`` 等前端路由
   统一返回 ``index.html``，由前端路由器处理。
3. **站点聊天代理**：将 ``/api/site-chat/*`` 请求代理到独立的聊天上游服务。
4. **品牌图标**：从 dist 根目录提供 favicon 等品牌图标文件。
"""

import logging
import os
from pathlib import Path

import httpx
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from app import paths
from app.main import app


logger = logging.getLogger("staffdeck.static")
ROOT_DIR = paths.resource_dir()
# frozen: dist 被收集到 _MEIPASS/frontend-enterprise/dist
# dev:    resource_dir()==backend/，需回到仓库根找 frontend-enterprise
ENTERPRISE_DIST = (
    ROOT_DIR / "frontend-enterprise" / "dist"
    if paths.is_frozen()
    else ROOT_DIR.parent / "frontend-enterprise" / "dist"
)
SPA_INDEX_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}
SITE_CHAT_UPSTREAM = os.getenv(
    "STAFFDECK_SITE_CHAT_UPSTREAM",
    "http://127.0.0.1:10187",
).rstrip("/")
HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
FRONTEND_CONTENT_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".mjs": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
}


class FrontendStaticFiles(StaticFiles):
    """前端静态文件服务，确保 Vite 打包的资源在 Windows 上使用正确的 MIME 类型。

    Starlette 默认的 MIME 检测在 Windows 上可能将 .js 文件识别为
    ``application/javascript`` 而非 ``text/javascript``，导致浏览器拒绝执行。
    本类通过在 ``file_response`` 中根据文件后缀强制设置正确的 Content-Type 来修复此问题。
    """

    def file_response(
        self,
        full_path: os.PathLike[str],
        stat_result: os.stat_result,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        """生成文件响应，根据后缀名强制修正 MIME 类型。

        Args:
            full_path: 文件完整路径。
            stat_result: 文件 stat 结果。
            scope: ASGI scope 对象。
            status_code: HTTP 状态码，默认 200。

        Returns:
            修正了 MIME 类型的 ``Response`` 对象。
        """
        response = super().file_response(full_path, stat_result, scope, status_code)
        suffix = Path(full_path).suffix.lower()
        media_type = FRONTEND_CONTENT_TYPES.get(suffix)
        if media_type:
            detected_media_type = response.headers.get("Content-Type")
            response.headers["Content-Type"] = media_type
            detected_base_type = (detected_media_type or "").partition(";")[0].strip().lower()
            allowed_base_types = {media_type.partition(";")[0].lower()}
            if suffix in {".js", ".mjs"}:
                allowed_base_types.add("application/javascript")
            if detected_base_type not in allowed_base_types:
                logger.warning(
                    "Corrected frontend MIME suffix=%s detected=%s forced=%s",
                    suffix,
                    detected_media_type,
                    media_type,
                )
        return response


def spa_index_response(index_path: Path) -> FileResponse:
    """生成 SPA 入口 HTML 响应，附带禁止缓存的请求头。

    Args:
        index_path: ``index.html`` 文件路径。

    Returns:
        带有 no-cache 请求头的 ``FileResponse``。
    """
    return FileResponse(index_path, headers=SPA_INDEX_HEADERS)


@app.api_route(
    "/api/site-chat/{site_path:path}",
    methods=["GET", "POST", "OPTIONS"],
    include_in_schema=False,
)
async def site_chat_proxy(site_path: str, request: Request) -> StreamingResponse:
    """将站点聊天请求代理到上游聊天服务。

    支持 GET/POST/OPTIONS 方法，转发请求头（添加 x-forwarded-* 头），
    以流式方式转发响应体。连接超时为 600 秒，读取超时不限。

    Args:
        site_path: 请求路径（``/api/site-chat/`` 之后的部分）。
        request: Starlette 请求对象。

    Returns:
        流式 ``StreamingResponse``，透传上游响应的状态码和头部。
    """
    target_url = f"{SITE_CHAT_UPSTREAM}/api/site-chat/{site_path}"
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    request_headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    request_headers["x-forwarded-host"] = request.headers.get("host", "")
    request_headers["x-forwarded-proto"] = request.url.scheme
    if request.client:
        request_headers["x-forwarded-for"] = request.client.host

    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))
    upstream_request = client.build_request(
        request.method,
        target_url,
        headers=request_headers,
        content=await request.body(),
    )
    try:
        upstream_response = await client.send(upstream_request, stream=True)
    except Exception:
        await client.aclose()
        raise

    response_headers = {
        key: value
        for key, value in upstream_response.headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS
    }
    response_headers["x-accel-buffering"] = "no"

    async def stream_body():
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        finally:
            await upstream_response.aclose()
            await client.aclose()

    return StreamingResponse(
        stream_body(),
        status_code=upstream_response.status_code,
        headers=response_headers,
    )

app.mount(
    "/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="assets",
)
app.mount(
    "/enterprise/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="enterprise-assets",
)
app.mount(
    "/chat/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="chat-assets",
)
app.mount(
    "/workspace/assets",
    FrontendStaticFiles(directory=ENTERPRISE_DIST / "assets", check_dir=False),
    name="workspace-assets",
)


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/chat/")


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.png", include_in_schema=False)
@app.get("/staffdeck-icon.png", include_in_schema=False)
def brand_icon(request: Request) -> FileResponse:
    # 品牌图标：从前端 dist 根目录 serve（favicon.ico/png、apple-touch-icon）
    name = request.url.path.lstrip("/")
    target = ENTERPRISE_DIST / name
    if not target.exists():
        target = ENTERPRISE_DIST / "favicon.ico"
    return FileResponse(target)


@app.get("/enterprise", include_in_schema=False)
@app.get("/enterprise/{path:path}", include_in_schema=False)
def enterprise_app(path: str = "") -> FileResponse:
    return spa_index_response(ENTERPRISE_DIST / "index.html")


@app.get("/login", include_in_schema=False)
@app.get("/chat", include_in_schema=False)
@app.get("/chat/{path:path}", include_in_schema=False)
@app.get("/workspace", include_in_schema=False)
@app.get("/workspace/{path:path}", include_in_schema=False)
def chat_app(path: str = "") -> FileResponse:
    return spa_index_response(ENTERPRISE_DIST / "index.html")
