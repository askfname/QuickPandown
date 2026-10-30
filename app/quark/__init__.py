"""夸克网盘 API 客户端包"""

from app.quark.client import UA_ELECTRON, UA_PC, QuarkApiError, QuarkClient

__all__ = ["UA_PC", "UA_ELECTRON", "QuarkApiError", "QuarkClient"]
