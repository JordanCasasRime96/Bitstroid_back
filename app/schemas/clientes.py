from pydantic import BaseModel

class SolicitudEnvioPayload(BaseModel):
    tipo: str
    destinatarioId: str = ""



class DestinatarioEnvioPayload(BaseModel):
    nombreCompleto: str
    dni: str
    direccion: str
    referencia: str = ""
    provincia: str
    distrito: str
    contacto: str
    predeterminado: bool = False
