from uuid import UUID
from pydantic import BaseModel, Field

class LoginPayload(BaseModel):
    credencial: str = Field(max_length=254)
    password: str = Field(max_length=120)



class RegistroPayload(BaseModel):
    apodo: str = Field(min_length=1, max_length=60)
    whatsapp: str = Field(min_length=1, max_length=25)
    password: str = Field(min_length=1, max_length=120)
    nombres: str = Field(default="", max_length=160)
    correo: str = Field(default="", max_length=254)



class VerificarRegistroPayload(BaseModel):
    verificacionId: UUID
    verificacionToken: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$")



class CambiarContrasenaPayload(BaseModel):
    contrasena_actual: str
    contrasena_nueva: str = Field(min_length=1, max_length=120)



class EstablecerContrasenaPayload(BaseModel):
    contrasena_nueva: str = Field(min_length=1, max_length=120)



class FotoPerfilPayload(BaseModel):
    contenidoBase64: str = Field(min_length=1, max_length=350000)
