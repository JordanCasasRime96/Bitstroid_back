from typing import Any
from pydantic import BaseModel, Field

class InteraccionPublicacionPayload(BaseModel):
    publicacionId: str
    categoria: str = ""
    evento: str = "click"



class ConfirmacionPagoPayload(BaseModel):
    total: float = Field(ge=0)
    items: list[dict[str, Any]] = Field(default_factory=list)
    comprobanteNombre: str
    comprobanteMimeType: str
    comprobanteContenidoBase64: str
    invitadoNombre: str = ""
    invitadoWhatsapp: str = ""



class SolicitudCarritoPayload(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)
    invitadoNombre: str = ""
    invitadoWhatsapp: str = ""
