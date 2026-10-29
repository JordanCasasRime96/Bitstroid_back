import re
from fastapi import HTTPException

def _normalizar_whatsapp(valor: str) -> str:
    digitos = re.sub(r"\D+", "", valor or "")
    if len(digitos) == 9 and not (valor or "").strip().startswith("+"):
        digitos = f"51{digitos}"
    if not re.match(r"^[0-9]{8,15}$", digitos):
        raise HTTPException(status_code=400, detail="WhatsApp invalido")
    return digitos



def _email_valido(valor: str) -> bool:
    return bool(re.match(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", valor, flags=re.I))
