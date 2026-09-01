"""
Integração com a API de gerenciamento de templates da Meta (WhatsApp Business
Management API). Usado pelas campanhas: cria/submete o template do texto que o
cliente escreveu e consulta o status de aprovação.

NÃO envia mensagem nenhuma — só cria o template e checa aprovação. O disparo em
si é feito com `enviar_template_whatsapp` (app.py) na Fase 2b.

Precisa de: waba_id da clínica + um token com a permissão whatsapp_business_management.
Nunca levanta exceção pra fora: retorna tuplas (ok, status, detalhe).
"""
import re
import time
import requests

GRAPH_URL = "https://graph.facebook.com/v21.0"

# Meta -> nosso status interno
_MAP_STATUS = {
    "APPROVED": "aprovado",
    "PENDING": "em_aprovacao",
    "IN_APPEAL": "em_aprovacao",
    "PENDING_DELETION": "em_aprovacao",
    "REJECTED": "reprovado",
    "DISABLED": "reprovado",
    "PAUSED": "reprovado",
}


def mapear_status(meta_status):
    """Converte o status da Meta pro nosso vocabulário interno."""
    return _MAP_STATUS.get((meta_status or "").upper(), "em_aprovacao")


def gerar_nome_template(clinica_id):
    """Nome único e válido pra Meta (minúsculo, só letras/números/_)."""
    return f"campanha_{int(clinica_id)}_{int(time.time())}"


def _corpo_valido(texto):
    """A Meta rejeita corpo vazio, só-variável, ou com espaços/linhas nas pontas.
    Devolve (ok, texto_limpo_ou_erro)."""
    t = (texto or "").strip()
    if len(t) < 3:
        return False, "a mensagem está muito curta."
    if len(t) > 1024:
        return False, "a mensagem passa de 1024 caracteres."
    # A Meta reprova template que é só uma variável, sem texto fixo.
    if re.fullmatch(r"(\{\{\d+\}\}\s*)+", t):
        return False, "a mensagem precisa ter texto, não só variáveis."
    return True, t


def submeter_template(waba_id, token, nome, corpo,
                      categoria="MARKETING", idioma="pt_BR"):
    """
    Cria e submete o template pra aprovação da Meta.
    Retorna (ok, status_interno, detalhe). NÃO envia mensagem.
    """
    if not waba_id or not token:
        return False, "erro", {"erro": "clínica sem WABA ID ou token configurado."}
    ok, corpo_limpo = _corpo_valido(corpo)
    if not ok:
        return False, "erro", {"erro": corpo_limpo}

    url = f"{GRAPH_URL}/{waba_id}/message_templates"
    payload = {
        "name": nome,
        "language": idioma,
        "category": categoria,
        "components": [{"type": "BODY", "text": corpo_limpo}],
    }
    try:
        r = requests.post(
            url, headers={"Authorization": f"Bearer {token}"},
            json=payload, timeout=30
        )
        data = r.json() if r.content else {}
        if r.status_code == 200 and data.get("id"):
            return True, mapear_status(data.get("status") or "PENDING"), data
        # Erro da Meta: extrai a mensagem legível.
        msg = (data.get("error") or {}).get("error_user_msg") \
            or (data.get("error") or {}).get("message") \
            or f"HTTP {r.status_code}"
        return False, "erro", {"erro": msg, "resposta": data}
    except Exception as e:
        return False, "erro", {"erro": str(e)}


def consultar_status_template(waba_id, token, nome):
    """
    Consulta o status atual do template na Meta.
    Retorna (ok, status_interno, detalhe).
    """
    if not waba_id or not token or not nome:
        return False, None, {"erro": "faltam waba_id, token ou nome do template."}
    url = f"{GRAPH_URL}/{waba_id}/message_templates"
    try:
        r = requests.get(
            url, headers={"Authorization": f"Bearer {token}"},
            params={"name": nome, "fields": "name,status,category"},
            timeout=30
        )
        data = r.json() if r.content else {}
        arr = data.get("data") or []
        if arr:
            meta_status = arr[0].get("status")
            return True, mapear_status(meta_status), {"meta_status": meta_status, "raw": arr[0]}
        return False, None, {"erro": "template não encontrado", "resposta": data}
    except Exception as e:
        return False, None, {"erro": str(e)}
