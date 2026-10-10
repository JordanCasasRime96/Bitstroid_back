"""Send one explicit interactive test; never runs on import or startup."""
import argparse
import re
from os import getenv

from config import load_env
from whatsapp_bot import lista
from whatsapp_respuestas import enviar_payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", required=True)
    parser.add_argument("--enviar", action="store_true", help="Autoriza un unico envio real")
    args = parser.parse_args()
    if not args.enviar or not re.fullmatch(r"[1-9][0-9]{7,14}", args.to):
        parser.error("Indica --enviar y un numero con codigo de pais")
    load_env()
    try:
        enviar_payload(args.to, lista("Hola, soy Bitstroid. Esta es una prueba del menu desplegable; estas opciones todavia no ejecutan solicitudes.",
            [("compras", "Mis compras"), ("entrega", "Solicitar entrega")], "prueba"), getenv("WHATSAPP_PHONE_NUMBER_ID", ""))
    except Exception as error:
        print("Prueba no enviada:", type(error).__name__, "HTTP", getattr(error, "code", "-"))
        raise SystemExit(1)
    print("Meta acepto el mensaje. Confirma su recepcion en WhatsApp.")


if __name__ == "__main__":
    main()
